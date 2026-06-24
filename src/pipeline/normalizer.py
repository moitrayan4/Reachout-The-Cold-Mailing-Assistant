"""Normalise raw postings into a uniform dict and compute dedup fingerprints."""

from __future__ import annotations
import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from ..sources.base import RawPosting

_logger = logging.getLogger("assistant.pipeline.normalizer")

# ---------------------------------------------------------------------------
# Stipend parsing
# ---------------------------------------------------------------------------

_STIPEND_RE = re.compile(
    r"(?:rs\.?|inr|₹)?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:k|,000)?"
    r"\s*(?:/?(?:per|p\.?)?(?:month|mo|m))?"
    r"(?:\s*-\s*(?:rs\.?|inr|₹)?\s*(\d[\d,]*(?:\.\d+)?)(?:k)?)?",
    re.IGNORECASE,
)

def parse_stipend(text: Optional[str]) -> tuple[Optional[int], bool]:
    """Return (amount_in_inr, stipend_stated).

    - (None, False) => no stipend text found => keep
    - (0, True)     => parsing failed but something was stated
    - (N, True)     => N INR/month
    """
    if not text or not text.strip():
        return None, False

    text_lower = text.lower()
    if any(w in text_lower for w in ["not disclosed", "negotiable", "n/a", "na", "unpaid"]):
        return None, False

    m = _STIPEND_RE.search(text.replace(",", ""))
    if not m:
        return None, True  # something stated but unparseable — keep, stated=True

    val = float(m.group(1))
    if val < 100:  # "25k" expressed without the k suffix but as "25"
        val *= 1000
    if "k" in text.lower():
        val *= 1000
    return int(val), True


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_posted_date(text: Optional[str], first_seen: datetime) -> Optional[datetime]:
    """Best-effort date parsing from free-form text."""
    if not text:
        return None

    text_l = text.lower().strip()

    # "X days ago" / "Xd ago"
    m = re.search(r"(\d+)\s*(?:day|d)", text_l)
    if m:
        return first_seen - timedelta(days=int(m.group(1)))

    # "X hours ago"
    m = re.search(r"(\d+)\s*(?:hour|hr|h)", text_l)
    if m:
        return first_seen - timedelta(hours=int(m.group(1)))

    # "X weeks? ago"
    m = re.search(r"(\d+)\s*(?:week|wk|w)", text_l)
    if m:
        return first_seen - timedelta(weeks=int(m.group(1)))

    # "X months? ago"
    m = re.search(r"(\d+)\s*(?:month|mo)", text_l)
    if m:
        return first_seen - timedelta(days=int(m.group(1)) * 30)

    # absolute date strings
    for fmt in ("%d %b %Y", "%d %B %Y", "%B %d, %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def _norm_str(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().strip())


def compute_fingerprint(company: str, role: str, source_url: str) -> str:
    """sha256(company|role|canonical_url) — primary dedup key."""
    key = f"{_norm_str(company)}|{_norm_str(role)}|{source_url.rstrip('/').lower()}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def compute_soft_key(company: str, role: str) -> str:
    """sha256(company|role) — secondary key for cross-site dedup."""
    key = f"{_norm_str(company)}|{_norm_str(role)}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Location / remote
# ---------------------------------------------------------------------------

_INDIA_KEYWORDS = re.compile(
    r"\b(india|bangalore|bengaluru|mumbai|delhi|hyderabad|chennai|pune|kolkata|"
    r"noida|gurgaon|gurugram|ahmedabad|jaipur|surat|lucknow|chandigarh|kochi|"
    r"remote.?india|pan.?india)\b",
    re.IGNORECASE,
)
_REMOTE_KEYWORDS = re.compile(r"\b(remote|work.?from.?home|wfh|anywhere)\b", re.IGNORECASE)


def classify_location(location: Optional[str], remote_text: Optional[str]) -> tuple[bool, bool]:
    """Return (is_india, is_remote)."""
    combined = " ".join(filter(None, [location, remote_text]))
    is_remote = bool(_REMOTE_KEYWORDS.search(combined))
    is_india = bool(_INDIA_KEYWORDS.search(combined))
    return is_india, is_remote


# ---------------------------------------------------------------------------
# Main normalise function
# ---------------------------------------------------------------------------

def normalise(raw: RawPosting, first_seen: Optional[datetime] = None) -> dict:
    """Convert a RawPosting to a normalised dict ready for the Opportunity model."""
    now = first_seen or datetime.utcnow()

    stipend_inr, stipend_stated = parse_stipend(raw.stipend_text)
    posted_date = parse_posted_date(raw.posted_date_text, now)
    is_india, is_remote = classify_location(raw.location, raw.remote_text)

    ppo_flag = bool(raw.ppo_text and re.search(r"\bppo\b", raw.ppo_text, re.IGNORECASE))
    fte_flag = bool(raw.fte_text and re.search(r"\b(fte|full.?time)\b", raw.fte_text, re.IGNORECASE))

    fp = compute_fingerprint(raw.company, raw.role, raw.source_url)

    # Target-company watcher flags (set by CompanyCareersSource via RawPosting.extra).
    extra = raw.extra or {}
    is_target_company = bool(extra.get("target_company"))
    priority = bool(extra.get("priority"))
    batch_2028 = extra.get("batch_signal") == "match"
    # A priority listing is always treated as India-eligible (it came from a
    # watched India careers page), so downstream location checks never drop it.
    if priority:
        is_india = True

    return {
        "fingerprint": fp,
        "soft_key": compute_soft_key(raw.company, raw.role),
        "company": raw.company.strip(),
        "role": raw.role.strip(),
        "location": raw.location,
        "remote": is_remote,
        "is_india": is_india,
        "stipend_inr": stipend_inr,
        "stipend_stated": stipend_stated,
        "currency": "INR",
        "ppo_flag": ppo_flag,
        "fte_flag": fte_flag,
        "duration": raw.duration_text,
        "start_timing": raw.start_date_text,
        "posted_date": posted_date,
        "first_seen": now,
        "source_urls": [raw.source_url],
        "source_site": raw.source_site,
        "is_startup": raw.is_startup_hint,
        "eligibility_text": raw.eligibility_text,
        "raw_snapshot_ref": raw.raw_html_snapshot,
        "is_target_company": is_target_company,
        "company_category": extra.get("company_category"),
        "batch_2028": batch_2028,
        "priority": priority,
    }

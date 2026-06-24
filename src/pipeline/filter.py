"""EligibilityFilter — strict pass/fail gate for scraped postings."""

from __future__ import annotations
import logging
import re
from datetime import datetime
from typing import List, Tuple

from ..llm.groq_client import get_llm
from langchain_core.messages import HumanMessage, SystemMessage

_logger = logging.getLogger("assistant.pipeline.filter")

# ---------------------------------------------------------------------------
# Batch / eligibility patterns
# ---------------------------------------------------------------------------

_PASS_BATCH = re.compile(
    r"\b(2027.?28|2028|pre.?final|penultimate|3rd.?year|third.?year|"
    r"fresher|fresh(?:ers)?|entry.?level|final.?year.+?(?:3rd|third|pre.?final))\b",
    re.IGNORECASE,
)
_FAIL_BATCH = re.compile(
    r"\b(2025|2026|2027|senior|experienced|[2-9]\+\s*(?:year|yr)|"
    r"[2-9]\s*to\s*\d+\s*(?:year|yr)|mid.?level|lead|manager|[5-9]\s*year)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# LLM-based classifier for ambiguous cases
# ---------------------------------------------------------------------------

_BATCH_SYSTEM = """You are an internship eligibility classifier.
Given the eligibility text from an internship posting, decide if a student
graduating in 2028 (currently in 3rd year / pre-final year / penultimate year)
is eligible.

Reply with ONLY a JSON object: {"eligible": "yes" | "no" | "unsure"}
- "yes" if the posting explicitly targets 2028 grads, pre-final, 3rd year, penultimate, OR freshers/entry-level with no year specified
- "no" if it targets 2025/2026/2027 graduates, senior candidates, or requires experience
- "unsure" for any other case (treated as FAIL by the filter)"""


def _llm_classify_batch(eligibility_text: str) -> str:
    """Return 'yes', 'no', or 'unsure'."""
    try:
        llm = get_llm()
        msgs = [
            SystemMessage(content=_BATCH_SYSTEM),
            HumanMessage(content=f"Eligibility text: {eligibility_text[:1000]}"),
        ]
        resp = llm.invoke(msgs)
        raw = resp.content.strip().replace("```json", "").replace("```", "").strip()
        import json as _json
        data = _json.loads(raw)
        return data.get("eligible", "unsure")
    except Exception as exc:
        _logger.warning("LLM batch classifier failed: %s; defaulting to unsure", exc)
        return "unsure"


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------

def check_recency(record: dict, max_age_days: int,
                  drop_unknown: bool = False) -> Tuple[bool, str]:
    """(a) Must be posted within max_age_days.

    Unknown date is NOT the same as old. Scraped listings frequently lack a
    machine-readable posted-date, so by default we keep them (unknown != stale)
    rather than silently discarding the majority. Set ``drop_unknown=True`` to
    restore the strict fail-closed behaviour.
    """
    posted = record.get("posted_date")
    first_seen = record.get("first_seen", datetime.utcnow())

    if posted is None:
        if drop_unknown:
            return False, "date_unknown"
        return True, "pass_date_unknown"

    age = (first_seen - posted).days
    if age > max_age_days:
        return False, f"too_old_{age}d"
    return True, "ok"


def check_batch(record: dict) -> Tuple[bool, str]:
    """(b) Batch/freshers check. Keyword first; LLM for ambiguous."""
    elig = record.get("eligibility_text", "") or ""
    role = record.get("role", "") or ""
    combined = f"{elig} {role}"

    # Check PASS first so "2027-28" wins over the bare "2027" fail pattern.
    if _PASS_BATCH.search(combined):
        return True, "pass_batch_keyword"

    if _FAIL_BATCH.search(combined):
        return False, "fail_batch_keyword"

    # Ambiguous — ask LLM (cached)
    if elig.strip():
        verdict = _llm_classify_batch(combined)
        if verdict == "yes":
            return True, "pass_batch_llm"
        return False, f"fail_batch_llm_{verdict}"

    # No eligibility text at all → assume eligible (freshers often not specified)
    return True, "pass_batch_no_text"


def check_stipend(record: dict, min_inr: int) -> Tuple[bool, str]:
    """(c) Stipend rule: reject only if stated AND <= min_inr."""
    stated = record.get("stipend_stated", False)
    amount = record.get("stipend_inr")

    if not stated:
        return True, "pass_stipend_not_stated"

    if amount is None:
        return True, "pass_stipend_ambiguous"

    if amount > min_inr:
        return True, "pass_stipend"

    return False, f"fail_stipend_{amount}"


def check_location(record: dict) -> Tuple[bool, str]:
    """(d) Location: India OK; non-India only if remote."""
    is_india = record.get("is_india", False)
    is_remote = record.get("is_remote", record.get("remote", False))

    if is_india:
        return True, "pass_location_india"
    if is_remote:
        return True, "pass_location_remote"
    return False, "fail_location_non_india_non_remote"


# ---------------------------------------------------------------------------
# Main filter
# ---------------------------------------------------------------------------

class EligibilityFilter:
    def __init__(self, settings):
        self.max_age = settings.max_posting_age_days
        self.min_stipend = settings.stipend_min_inr
        self.drop_unknown_date = getattr(settings, "drop_unknown_date", False)

    def filter(self, records: List[dict]) -> Tuple[List[dict], List[Tuple[dict, str]]]:
        """Return (passed, dropped_with_reason)."""
        passed = []
        dropped = []

        for rec in records:
            ok, reason = self._check_all(rec)
            if ok:
                # Add helpful labels
                if not rec.get("stipend_stated"):
                    rec["stipend_label"] = "stipend not stated"
                elif rec.get("ppo_flag"):
                    rec["stipend_label"] = f"Rs.{rec['stipend_inr']:,}/mo + PPO"
                else:
                    amt = rec.get("stipend_inr")
                    rec["stipend_label"] = f"Rs.{amt:,}/mo" if amt else "stipend not stated"
                passed.append(rec)
            else:
                _logger.debug("Dropped %s @ %s: %s", rec.get("company"), rec.get("role"), reason)
                dropped.append((rec, reason))

        return passed, dropped

    def _check_all(self, rec: dict) -> Tuple[bool, str]:
        # MUST-SHOW: an internship from a watched target company that matches the
        # owner's 2028 batch is mandatory. It still must be batch-eligible and
        # India/remote, but bypasses the stipend floor and posting-age cutoff so
        # a dream-company opening is never silently dropped.
        if rec.get("priority"):
            for check, args in [
                (check_batch, (rec,)),
                (check_location, (rec,)),
            ]:
                ok, reason = check(*args)
                if not ok:
                    return False, reason
            return True, "pass_priority_target_company"

        for check, args in [
            (check_recency, (rec, self.max_age, self.drop_unknown_date)),
            (check_batch, (rec,)),
            (check_stipend, (rec, self.min_stipend)),
            (check_location, (rec,)),
        ]:
            ok, reason = check(*args)
            if not ok:
                return False, reason
        return True, "all_pass"

"""Email discovery — domain resolution + evidence-based, verified addresses.

This never returns a *guessed* address as if it were usable. An email is only
returned with ``status='verified'`` when it is published on the company's own
site / the web, or SMTP-confirmed. Pattern guesses are returned separately as
``unverified_candidates`` for transparency and are NOT used as recipients.
"""

from __future__ import annotations
import logging
import re
from typing import List, Optional

from .email_verify import (
    harvest_company_emails, smtp_verify, domain_has_mx,
    is_platform_domain,
)

_logger = logging.getLogger("assistant.contacts.email_finder")


# ---------------------------------------------------------------------------
# Domain resolution
# ---------------------------------------------------------------------------

def _company_tokens(company: str) -> List[str]:
    stop = {"the", "inc", "llc", "ltd", "pvt", "private", "limited", "technologies",
            "technology", "solutions", "solution", "labs", "systems", "software",
            "services", "global", "group", "company", "co", "corp", "and", "it"}
    toks = re.sub(r"[^a-z0-9\s]", " ", company.lower()).split()
    return [t for t in toks if t not in stop and len(t) > 1]


# Common two-label public suffixes (no PSL dependency). Used so that
# "careers.zerodha.co.in" resolves to the apex "zerodha.co.in", not "zerodha.co".
_TWO_PART_SUFFIXES = {
    "co.in", "net.in", "org.in", "gov.in", "ac.in", "co.uk", "org.uk", "com.au",
    "co.za", "com.sg", "co.jp", "com.br", "co.id", "com.my", "co.nz",
}


def _registrable(domain: str) -> str:
    """Return the apex/registrable domain (brand + public suffix).

    ``careers.zerodha.com`` -> ``zerodha.com``; ``foo.zerodha.co.in`` ->
    ``zerodha.co.in``.
    """
    parts = domain.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def _brand_label(domain: str) -> str:
    """The brand portion of the apex domain (``careers.zerodha.com`` -> ``zerodha``)."""
    return _registrable(domain).split(".")[0]


def _domain_matches_company(domain: str, company: str) -> bool:
    """Require the domain's brand label to overlap with a company token."""
    if not domain or is_platform_domain(domain):
        return False
    brand = _brand_label(domain)
    tokens = _company_tokens(company)
    if not tokens:
        return False
    joined = "".join(tokens)
    for t in tokens:
        if t in brand or brand in t:
            return True
    # acronym / concatenated forms, e.g. "webitsolution" vs "web it solution"
    return joined[:6] in brand or brand[:6] in joined


def _domain_primary_rank(domain: str, company: str) -> int:
    """0 = brand exactly matches the company (the main corporate domain),
    1 = brand merely *contains* a company token but is longer (a subsidiary,
    e.g. ``zerodhafundhouse`` vs ``zerodha``), 2 = looser overlap."""
    brand = _brand_label(domain)
    tokens = _company_tokens(company)
    joined = "".join(tokens)
    if brand == joined or brand in tokens:
        return 0
    if any(brand.startswith(t) and len(brand) > len(t) for t in set(tokens) | {joined}):
        return 1
    return 2


def get_company_domain(company: str) -> Optional[str]:
    """Resolve the company's real website domain via web search, validated.

    Returns None rather than a misleading guess when nothing trustworthy is found.
    Prefers the main corporate apex (e.g. zerodha.com) over subsidiary brands
    (zerodhafundhouse.com) and over sub-domains (careers.zerodha.com).
    """
    from .web_search import search_web

    candidates: List[str] = []
    # Gather across ALL queries — DDG results are non-deterministic, so breaking
    # after the first query that yields anything can miss the apex domain and
    # leave only a subsidiary to choose from.
    for q in (f"{company} official website", f"{company} careers", company):
        for r in search_web(q, max_results=5):
            href = r.get("href", "") or ""
            m = re.search(r"https?://([^/]+)", href)
            if not m:
                continue
            domain = re.sub(r"^www\.", "", m.group(1).lower())
            if domain and not is_platform_domain(domain) and domain not in candidates:
                candidates.append(domain)

    matching = [d for d in candidates if _domain_matches_company(d, company)]
    if not matching:
        return None

    # Also consider each candidate's apex, so a sub-domain like
    # careers.zerodha.com contributes the real apex zerodha.com to the pool.
    for d in list(matching):
        apex = _registrable(d)
        if apex not in matching and _domain_matches_company(apex, company):
            matching.append(apex)

    mx = {d: domain_has_mx(d) for d in matching}
    matching.sort(key=lambda d: (
        _domain_primary_rank(d, company),       # main corporate domain first
        0 if d == _registrable(d) else 1,        # apex before sub-domain
        0 if mx.get(d) else 1,                    # deliverable (has MX) first
        len(_brand_label(d)),                     # shorter brand as tie-break
        d,
    ))
    return matching[0]


# ---------------------------------------------------------------------------
# Name <-> email matching
# ---------------------------------------------------------------------------

def _name_matches_email(first: str, last: str, email: str) -> bool:
    local = email.split("@")[0].lower()
    first = (first or "").lower().strip()
    last = (last or "").lower().strip()
    if not local:
        return False
    if first and last:
        f = first[0]
        forms = {f"{first}.{last}", f"{first}{last}", f"{f}{last}", f"{first}_{last}",
                 f"{f}.{last}", f"{last}.{first}", f"{last}{f}", f"{first}.{last[0]}"}
        if local in forms:
            return True
    # Looser: both name parts present in the local part.
    if first and last and first in local and last in local:
        return True
    if first and len(first) > 3 and local == first:
        return True
    return False


def generate_email_candidates(first: str, last: str, domain: str) -> list:
    """Pattern guesses — for transparency only, never used as recipients."""
    first = (first or "").lower().strip()
    last = (last or "").lower().strip()
    f = first[0] if first else ""
    out = []
    for tmpl in [f"{first}.{last}", f"{first}{last}", f"{f}{last}",
                 f"{first}_{last}", f"{f}.{last}", f"{first}"]:
        e = f"{tmpl}@{domain}"
        if "@" in e and not e.startswith("@") and e not in out:
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_email(first: str, last: str, company: str,
               domain: Optional[str] = None) -> dict:
    """Find a *verified* email for a named person at a company.

    Result dict::

        {
          "email": <str|None>,        # only set when status == 'verified'
          "status": "verified" | "unverified" | "no_domain",
          "source": "published" | "smtp" | None,
          "domain": <str|None>,
          "evidence": <str>,          # where it was found / why unverified
          "unverified_candidates": [...]   # pattern guesses, NOT for sending
        }
    """
    if not domain:
        domain = get_company_domain(company)
    if not domain:
        return {"email": None, "status": "no_domain", "source": None,
                "domain": None, "evidence": "Could not resolve a trustworthy "
                "company domain; refusing to guess.", "unverified_candidates": []}

    harvest = harvest_company_emails(company, domain)

    # 1) A published personal email that matches this person == real & verified.
    for fe in harvest.personal_emails():
        if _name_matches_email(first, last, fe.email):
            return {"email": fe.email, "status": "verified", "source": "published",
                    "domain": domain,
                    "evidence": f"Published at {fe.source_url}",
                    "unverified_candidates": []}

    # 2) SMTP-confirm a pattern candidate (works only where port 25 is open).
    candidates = generate_email_candidates(first, last, domain)
    for cand in candidates[:4]:
        status = smtp_verify(cand)
        if status == "valid":
            return {"email": cand, "status": "verified", "source": "smtp",
                    "domain": domain, "evidence": "SMTP RCPT accepted (250)",
                    "unverified_candidates": []}
        if status == "invalid":
            continue
        # catch_all / unknown -> cannot confirm; keep looking.

    # 3) Hunter.io (costs a credit; only reached when free methods fail). Hunter
    #    finds AND verifies in one call — accepted only if deliverable/published.
    from . import hunter
    hres = hunter.find_email(first, last, company, domain)
    if hres and hres.get("email"):
        hres.setdefault("domain", domain)
        hres["unverified_candidates"] = []
        return hres

    # 4) Nothing confirmed. Return guesses transparently, but no usable email.
    role = [fe.email for fe in harvest.role_emails()]
    return {
        "email": None,
        "status": "unverified",
        "source": None,
        "domain": domain,
        "evidence": ("No published address matched this person and SMTP could not "
                     "confirm a guess (port 25 may be blocked)."),
        "unverified_candidates": candidates[:4],
        "role_emails": role,
    }


def find_generic_company_email(company: str, domain: Optional[str] = None) -> dict:
    """Find a real, published role mailbox (careers@/hr@/info@) as a fallback."""
    if not domain:
        domain = get_company_domain(company)
    if not domain:
        return {"email": None, "status": "no_domain", "domain": None}

    harvest = harvest_company_emails(company, domain)
    roles = harvest.role_emails()
    # Prefer hiring-oriented mailboxes.
    priority = ("careers", "career", "jobs", "hr", "recruit", "talent",
                "internship", "hiring", "people", "contact", "info", "hello")
    for key in priority:
        for fe in roles:
            if fe.email.split("@")[0].startswith(key):
                return {"email": fe.email, "status": "verified", "source": "published",
                        "domain": domain, "evidence": f"Published at {fe.source_url}"}
    if roles:
        fe = roles[0]
        return {"email": fe.email, "status": "verified", "source": "published",
                "domain": domain, "evidence": f"Published at {fe.source_url}"}

    # Fallback to Hunter (costs a credit) for a verified generic mailbox.
    from . import hunter
    hres = hunter.generic_company_email(company, domain)
    if hres and hres.get("email"):
        hres.setdefault("domain", domain)
        return hres

    return {"email": None, "status": "unverified", "domain": domain}

"""HRRefresher — trust-checks a startup before outreach."""

from __future__ import annotations
import logging
import re
from typing import Tuple, List

import httpx

_logger = logging.getLogger("assistant.agents.hr_refresher")

_SCAM_SIGNALS = [
    "registration fee", "registration charge", "pay to apply", "deposit required",
    "send your bank", "personal bank", "payment required", "earn from home",
    "no experience required earn", "earn ₹",
]


def verify_startup(company: str, posting_text: str = "", source_url: str = "") -> Tuple[str, List[str]]:
    """Return (verdict, reasons). verdict in: trusted | low_trust | unverified."""
    reasons: List[str] = []
    trust_score = 0

    domain = _guess_domain(company)

    # 1. Check for scam signals in posting text
    text_lower = posting_text.lower()
    for signal in _SCAM_SIGNALS:
        if signal in text_lower:
            reasons.append(f"SCAM SIGNAL: '{signal}' found in posting text")
            return "low_trust", reasons

    # 2. Domain age / existence check
    try:
        resp = httpx.get(f"https://{domain}", timeout=8, follow_redirects=True)
        if resp.status_code < 400:
            trust_score += 20
            reasons.append(f"Company website accessible: {domain}")
        else:
            reasons.append(f"Company website returned {resp.status_code}")
    except Exception:
        reasons.append("Company website not accessible or timed out")

    # 3. LinkedIn company page
    try:
        resp = httpx.get(
            f"https://api.linkedin.com/v2/organizations?q=vanityName&vanityName={company.lower().replace(' ', '')}",
            timeout=8,
        )
        if resp.status_code == 200:
            trust_score += 20
            reasons.append("LinkedIn company page found")
    except Exception:
        pass

    # 4. Source URL domain trust
    if source_url and any(site in source_url for site in ["linkedin.com", "naukri.com", "internshala.com", "wellfound.com", "unstop.com"]):
        trust_score += 30
        reasons.append(f"Posted on verified platform: {_extract_site(source_url)}")

    # 5. Company name signals
    if re.search(r"\b(pvt\.?\s*ltd|private limited|inc\.|corp\.|technologies|solutions|labs|ai|tech)\b",
                 company, re.IGNORECASE):
        trust_score += 10
        reasons.append("Registered company name pattern")

    if trust_score >= 50:
        verdict = "trusted"
    elif trust_score >= 20:
        verdict = "unverified"
    else:
        verdict = "low_trust"
        reasons.append("Insufficient trust signals — please review carefully before outreach")

    return verdict, reasons


def _guess_domain(company: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", company.lower())
    words = cleaned.split()
    if words:
        return words[0] + ".com"
    return "unknown.com"


def _extract_site(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url[:30]

"""Evidence-based email discovery & verification.

The previous pipeline *guessed* email patterns (e.g. ``first.last@domain``) and
presented them as usable contacts even when nothing confirmed they exist. That
produced hallucinated recipients. This module instead:

1. Harvests emails that are *actually published* on the company's own website
   and in search results (these are real by definition), and
2. Best-effort SMTP-verifies an address where the network permits it (port 25 is
   often blocked, in which case the result is "unknown", never a false "valid").

An email is only treated as usable for sending when it is either published on a
page belonging to the company or SMTP-confirmed.
"""

from __future__ import annotations
import logging
import re
import smtplib
from dataclasses import dataclass, field
from typing import List, Optional, Set

import httpx

_logger = logging.getLogger("assistant.contacts.email_verify")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Lightly de-obfuscate "name [at] domain [dot] com" style addresses.
_OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\s+at\s+)\s*"
    r"([A-Za-z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\s+dot\s+)\s*([A-Za-z]{2,})",
    re.IGNORECASE,
)

_ROLE_LOCALPARTS = {
    "hr", "careers", "career", "jobs", "job", "recruitment", "recruiting",
    "talent", "people", "hiring", "internship", "internships", "work",
    "contact", "info", "hello", "hi", "reach", "connect", "support", "team",
    "admin", "office", "enquiry", "enquiries", "inquiry",
}
# Generic mailbox providers a real corporate address would not normally use.
_FREE_MAIL = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "protonmail.com", "live.com", "rediffmail.com", "aol.com",
}
# Domains that are website-builders / aggregators, not real employers.
_PLATFORM_DOMAINS = {
    "sentry.io", "wix.com", "webs.com", "godaddy.com", "wordpress.com",
    "squarespace.com", "shopify.com", "example.com", "domain.com",
    "sentry-cdn.com", "cloudflare.com", "google.com", "gstatic.com",
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

_CONTACT_PATHS = [
    "", "contact", "contact-us", "contactus", "about", "about-us",
    "team", "our-team", "careers", "career", "jobs", "people",
]


@dataclass
class FoundEmail:
    email: str
    source_url: str
    is_role: bool          # generic role address (hr@, careers@) vs personal
    on_company_domain: bool


@dataclass
class EmailHarvest:
    domain: Optional[str]
    emails: List[FoundEmail] = field(default_factory=list)

    def role_emails(self) -> List[FoundEmail]:
        return [e for e in self.emails if e.is_role and e.on_company_domain]

    def personal_emails(self) -> List[FoundEmail]:
        return [e for e in self.emails if not e.is_role and e.on_company_domain]


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _normalise_obfuscated(text: str) -> str:
    return _OBFUSCATED_RE.sub(lambda m: f"{m.group(1)}@{m.group(2)}.{m.group(3)}", text)


def _extract_emails(text: str) -> Set[str]:
    if not text:
        return set()
    text = _normalise_obfuscated(text)
    out = set()
    for raw in _EMAIL_RE.findall(text):
        e = raw.strip().strip(".").lower()
        # Drop image/asset false-positives like foo@2x.png
        if e.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")):
            continue
        domain = e.split("@")[-1]
        if domain in _PLATFORM_DOMAINS:
            continue
        out.add(e)
    return out


def _local_is_role(local: str) -> bool:
    base = re.split(r"[._\-+]", local)[0]
    return base in _ROLE_LOCALPARTS or local in _ROLE_LOCALPARTS


# ---------------------------------------------------------------------------
# Domain validation
# ---------------------------------------------------------------------------

def domain_has_mx(domain: str) -> bool:
    try:
        import dns.resolver
        return bool(dns.resolver.resolve(domain, "MX"))
    except Exception:
        return False


def is_platform_domain(domain: Optional[str]) -> bool:
    return bool(domain) and domain.lower() in _PLATFORM_DOMAINS


# ---------------------------------------------------------------------------
# Harvesting
# ---------------------------------------------------------------------------

def _fetch(url: str, client: httpx.Client) -> str:
    try:
        r = client.get(url, timeout=10, follow_redirects=True)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", "text/html"):
            return r.text
    except Exception:
        pass
    return ""


def harvest_company_emails(company: str, domain: Optional[str],
                           max_pages: int = 6) -> EmailHarvest:
    """Collect real, published emails for a company from its site + the web."""
    harvest = EmailHarvest(domain=domain)
    seen: Set[str] = set()

    def add(email: str, url: str):
        if email in seen:
            return
        seen.add(email)
        edomain = email.split("@")[-1]
        on_dom = bool(domain) and (edomain == domain or edomain.endswith("." + domain))
        # Accept company-domain emails, plus published role/contact emails even
        # on a slightly different domain only if not a free-mail provider.
        if not on_dom and edomain in _FREE_MAIL:
            return
        harvest.emails.append(FoundEmail(
            email=email, source_url=url,
            is_role=_local_is_role(email.split("@")[0]),
            on_company_domain=on_dom,
        ))

    with httpx.Client(headers=_HEADERS) as client:
        # 1) Crawl the company's own site (most authoritative source).
        if domain and domain_has_mx(domain):
            for scheme in ("https://", "http://"):
                for path in _CONTACT_PATHS[:max_pages]:
                    url = f"{scheme}{domain}/{path}".rstrip("/")
                    html = _fetch(url, client)
                    if html:
                        for e in _extract_emails(html):
                            add(e, url)
                if any(e.on_company_domain for e in harvest.emails):
                    break  # https worked; don't retry http

        # 2) Search the web for published contact emails.
        try:
            from .web_search import search_web
            queries = [
                f"{company} email contact careers",
                f"{company} HR email internship",
                f"{company} careers email address apply",
            ]
            if domain:
                queries.append(f'"@{domain}" email')
                queries.append(f'"@{domain}" careers OR hr OR contact')
            for q in queries:
                results = search_web(q, max_results=6)
                for r in results:
                    snippet = f"{r.get('title','')} {r.get('body','')}"
                    for e in _extract_emails(snippet):
                        add(e, r.get("href", ""))
                # Stop early once we have a company-domain address to save calls.
                if any(e.on_company_domain for e in harvest.emails):
                    break
        except Exception as exc:
            _logger.debug("web email search failed: %s", exc)

    return harvest


# ---------------------------------------------------------------------------
# SMTP verification (best-effort; port 25 is frequently blocked)
# ---------------------------------------------------------------------------

def _mx_host(domain: str) -> Optional[str]:
    try:
        import dns.resolver
        records = dns.resolver.resolve(domain, "MX")
        return str(sorted(records, key=lambda r: r.preference)[0].exchange).rstrip(".")
    except Exception:
        return None


def smtp_verify(email: str) -> str:
    """Return 'valid' | 'invalid' | 'catch_all' | 'unknown'.

    'unknown' means we could not reach the mail server (e.g. port 25 blocked) —
    it is NOT a confirmation. Catch-all domains accept anything, so a 250 there
    cannot confirm a specific mailbox.
    """
    domain = email.split("@")[-1]
    mx = _mx_host(domain)
    if not mx:
        return "invalid" if not domain_has_mx(domain) else "unknown"
    try:
        with smtplib.SMTP(timeout=8) as smtp:
            smtp.connect(mx, 25)
            smtp.helo("verify.local")
            smtp.mail("verify@example.com")
            # Catch-all probe: a random address that should not exist.
            import uuid
            probe = f"zz-{uuid.uuid4().hex[:12]}@{domain}"
            catch_code, _ = smtp.rcpt(probe)
            real_code, _ = smtp.rcpt(email)
            smtp.quit()
            if catch_code in (250, 251):
                return "catch_all"
            if real_code in (250, 251):
                return "valid"
            return "invalid"
    except smtplib.SMTPServerDisconnected:
        return "unknown"
    except (smtplib.SMTPConnectError, OSError):
        return "unknown"
    except Exception:
        return "unknown"

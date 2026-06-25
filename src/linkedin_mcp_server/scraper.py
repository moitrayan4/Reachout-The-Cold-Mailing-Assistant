"""LinkedIn page navigation + parsing for the MCP server.

These functions drive an already-open Patchright page (from ``LinkedInSession``)
and return plain dicts. They are intentionally selector-tolerant: LinkedIn ships
several DOM variants (logged-in app, guest pages, A/B buckets), so each lookup
tries multiple selectors and degrades gracefully rather than throwing.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser

from ..sources.stealth import safe_content, safe_scroll, human_delay

_logger = logging.getLogger("linkedin_mcp.scraper")

_JOB_ID_RE = re.compile(r"(?:jobPosting:|/jobs/view/)(\d{6,})")

# A LinkedIn page that shows the login/authwall instead of content means the
# saved session has lapsed. We surface this as text so the client can prompt a
# re-login (the existing client checks for "log in" / "no valid linkedin session").
_AUTHWALL_MARKERS = ("authwall", "/login", "sign in to linkedin", "join linkedin")


def _goto(page, url: str) -> None:
    """Navigate, waiting only for the DOM (not every sub-resource).

    LinkedIn's logged-in pages keep streaming trackers/images, so the full
    "load" event is slow and often never fires headless — waiting on it just
    times out. "domcontentloaded" is enough for our text scraping, and even if
    that is slow we swallow the timeout and read whatever rendered.
    """
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        _logger.debug("goto soft-timeout for %s: %s", url, exc)


def _looks_like_authwall(page) -> bool:
    try:
        url = (page.url or "").lower()
        return any(m in url for m in ("authwall", "/login"))
    except Exception:  # noqa: BLE001
        return False


def search_jobs(
    page,
    keywords: str,
    location: str = "India",
    job_type: str = "internship",
    experience_level: str = "internship,entry",
    date_posted: str = "past_month",
    max_pages: int = 1,
) -> dict:
    """Search LinkedIn jobs and return ``{"job_ids": [...]}``.

    Mirrors the contract the project's client expects (job_ids list), and
    returns ``{"raw": <notice>}`` when LinkedIn shows an auth wall so callers
    can prompt a re-login instead of silently reporting zero results.
    """
    f_jt = {"internship": "I", "full-time": "F", "part-time": "P",
            "contract": "C", "temporary": "T"}.get(job_type, "")
    # f_E experience levels: 1 internship, 2 entry, 3 associate, 4 mid-senior...
    exp_map = {"internship": "1", "entry": "2", "associate": "3",
               "mid-senior": "4", "director": "5", "executive": "6"}
    f_e = "%2C".join(exp_map[e.strip()] for e in experience_level.split(",")
                     if e.strip() in exp_map)
    f_tpr = {"past_24h": "r86400", "past_week": "r604800",
             "past_month": "r2592000"}.get(date_posted, "")

    url = (
        f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(keywords)}"
        f"&location={quote_plus(location)}&sortBy=DD"
    )
    if f_jt:
        url += f"&f_JT={f_jt}"
    if f_e:
        url += f"&f_E={f_e}"
    if f_tpr:
        url += f"&f_TPR={f_tpr}"

    job_ids: List[str] = []
    seen = set()
    # LinkedIn paginates job search via "&start=N" in steps of 25. We walk pages
    # until we hit max_pages or a page yields no new ids (the natural end of the
    # result set — LinkedIn keeps serving the last page rather than 404ing).
    for page_idx in range(max(1, max_pages)):
        page_url = url if page_idx == 0 else f"{url}&start={page_idx * 25}"
        _goto(page, page_url)
        human_delay(2.5, 4.5)
        if _looks_like_authwall(page):
            # First page behind the authwall ⇒ no session at all; otherwise just
            # return what we gathered before the wall appeared.
            if page_idx == 0:
                return {"raw": "No valid LinkedIn session — please log in."}
            break
        safe_scroll(page)
        human_delay(1.0, 2.0)
        html = safe_content(page)
        new_on_page = 0
        for jid in _extract_job_ids(html):
            if jid not in seen:
                seen.add(jid)
                job_ids.append(jid)
                new_on_page += 1
        if new_on_page == 0:
            break

    return {"job_ids": job_ids, "count": len(job_ids), "search_url": url}


def _extract_job_ids(html: str) -> List[str]:
    """Pull job ids from search-result cards via several DOM variants."""
    ids: List[str] = []
    tree = HTMLParser(html)
    # Variant A: guest cards carry data-entity-urn="urn:li:jobPosting:ID"
    for node in tree.css("[data-entity-urn], [data-job-id]"):
        urn = node.attributes.get("data-entity-urn") or ""
        jid = node.attributes.get("data-job-id") or ""
        m = _JOB_ID_RE.search(urn) if urn else None
        if m:
            ids.append(m.group(1))
        elif jid.isdigit():
            ids.append(jid)
    # Variant B: anchor hrefs /jobs/view/ID
    for a in tree.css("a[href*='/jobs/view/']"):
        m = _JOB_ID_RE.search(a.attributes.get("href") or "")
        if m:
            ids.append(m.group(1))
    # De-dup, preserve order.
    out, seen = [], set()
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def get_job_details(page, job_id: str) -> dict:
    """Fetch one job's detail page and return ``{url, sections:{job_posting}}``.

    The ``job_posting`` text is the human-readable block the client's
    ``_parse_job_posting_text`` already knows how to dissect (company on line 1,
    title on line 2, then a "location · posted · applicants" line, etc.).
    """
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    _goto(page, url)
    human_delay(2.0, 3.5)
    if _looks_like_authwall(page):
        return {"url": url, "sections": {"job_posting": ""},
                "raw": "No valid LinkedIn session — please log in."}

    # LinkedIn's logged-in job app ships hashed/obfuscated CSS class names that
    # change frequently, so class selectors are unreliable. Instead we read two
    # STABLE things: the document <title> ("Role | Company | LinkedIn") and the
    # rendered body text (location/posted/applicants/stipend live there).
    try:
        doc_title = page.title() or ""
    except Exception:  # noqa: BLE001
        doc_title = ""
    try:
        body_text = page.evaluate("() => document.body.innerText") or ""
    except Exception:  # noqa: BLE001
        body_text = safe_content(page)  # last-resort: raw HTML as text

    posting_text = _build_job_posting_text(doc_title, body_text)
    return {
        "url": url,
        "job_id": job_id,
        "sections": {"job_posting": posting_text},
    }


def _split_doc_title(doc_title: str) -> tuple[str, str]:
    """"Role | Company | LinkedIn" -> (role, company). Tolerant of missing parts."""
    parts = [p.strip() for p in doc_title.split("|") if p.strip()]
    # Drop a trailing "LinkedIn" / "(N) Role..." noise.
    if parts and parts[-1].lower() == "linkedin":
        parts = parts[:-1]
    role = parts[0] if parts else ""
    company = parts[1] if len(parts) > 1 else ""
    return role, company


def _build_job_posting_text(doc_title: str, body_text: str) -> str:
    """Assemble the company/title/meta block the client parser expects.

    Output shape (fed to the client's ``_parse_job_posting_text``):
        line 0 -> company
        line 1 -> title
        line 2 -> "location · posted · applicants"
        line 3 -> workplace type (Remote/On-site/Hybrid)  [if present]
        line 4 -> a stipend/pay line                       [if present]

    Built from the page <title> (stable) plus the first relevant lines of the
    rendered body, which dodges LinkedIn's hashed class names entirely.
    """
    role, company = _split_doc_title(doc_title)
    blines = [l.strip() for l in body_text.split("\n") if l.strip()]

    # The top-card meta line is the first "X · Y · Z" line that talks about
    # recency/applicants — distinguishes it from the similar-jobs sidebar.
    meta, meta_idx = "", -1
    for i, l in enumerate(blines):
        if ("·" in l and any(c.isdigit() for c in l)
                and any(w in l.lower() for w in ("ago", "applicant", "clicked apply"))):
            meta, meta_idx = l, i
            break

    # Workplace type usually sits a couple of lines under the meta line.
    workplace = ""
    if meta_idx >= 0:
        for l in blines[meta_idx + 1:meta_idx + 5]:
            if l.lower() in ("remote", "on-site", "on‑site", "hybrid"):
                workplace = l
                break

    # First money/stipend line anywhere in the posting — skipping LinkedIn's own
    # UI chrome (the "Try Premium for ₹0" upsell, free-trial nags, etc.) which
    # also contains currency symbols and would otherwise masquerade as pay.
    _stipend_noise = ("premium", "free trial", "trial", "subscription",
                      "try ", "upgrade", "/mo", "per year for")
    stipend = ""
    for l in blines:
        low = l.lower()
        if any(n in low for n in _stipend_noise):
            continue
        if (any(w in low for w in ("stipend", "/month", "per month", " lpa",
                                   "ctc", "₹", "inr", "rs.")) and len(l) < 140):
            stipend = l
            break

    lines = [company, role]
    if meta:
        lines.append(meta)
    if workplace:
        lines.append(workplace)
    if stipend and stipend not in (meta, workplace):
        lines.append(stipend)
    return "\n".join(l for l in lines if l)


def get_person_profile(page, linkedin_url: str) -> dict:
    """Scrape a public-ish LinkedIn profile into a small dict.

    Best-effort: returns whatever of name/headline/location/about it can read.
    Many profiles require login + first-degree connection for full data.
    """
    _goto(page, linkedin_url)
    human_delay(2.0, 3.5)
    if _looks_like_authwall(page):
        return {"url": linkedin_url, "raw": "No valid LinkedIn session — please log in."}
    safe_scroll(page)
    html = safe_content(page)
    tree = HTMLParser(html)
    return {
        "url": linkedin_url,
        "name": _first_text(tree, ["h1.text-heading-xlarge", "h1.top-card-layout__title", "h1"]),
        "headline": _first_text(tree, [".text-body-medium.break-words",
                                        ".top-card-layout__headline"]),
        "location": _first_text(tree, [".text-body-small.inline.t-black--light.break-words",
                                        ".top-card-layout__first-subline"]),
        "about": _first_text(tree, ["#about ~ .display-flex .inline-show-more-text",
                                    ".core-section-container__content .break-words"]),
    }

"""LinkedIn source adapter — uses the stickerdaniel/linkedin-mcp-server connector.

Falls back to Patchright scraper if the MCP connector is unavailable.
"""

from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import List

from .base import BaseSource, RawPosting
from .drift import SchemaDriftDetector
from ..narration.narrator import say, warn
from ..mcp_clients import linkedin_client

_logger = logging.getLogger("assistant.sources.linkedin")


def _parse_job_posting_text(text: str) -> dict:
    """Extract company, title, location, remote, stipend from raw job_posting text."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 2:
        return {}
    company = lines[0]
    title = lines[1]
    location = None
    remote_text = None
    posted_text = None
    stipend_text = None
    for line in lines[2:8]:
        if "·" in line and any(c.isdigit() for c in line):
            # "India · 3 hours ago · Over 100 people clicked apply"
            parts = [p.strip() for p in line.split("·")]
            location = parts[0]
            if len(parts) > 1:
                posted_text = parts[1]
        elif line.lower() in ("remote", "on-site", "hybrid"):
            remote_text = line
        elif any(w in line.lower() for w in ("month", "year", "lpa", "ctc", "stipend", "₹", "rs.", "inr")):
            stipend_text = line
    return {
        "company": company, "title": title, "location": location,
        "remote_text": remote_text, "posted_text": posted_text,
        "stipend_text": stipend_text,
    }


class LinkedInSource(BaseSource):
    site_name = "linkedin"

    def __init__(self, cookies_dir: Path, snapshots_dir: Path, proxies: List[str],
                 chrome_profile: str = "", mcp_port: int = 3000):
        self.cookies_dir = cookies_dir
        self.snapshots_dir = snapshots_dir
        self.proxies = proxies
        self.chrome_profile = chrome_profile
        self._drift = SchemaDriftDetector(
            snapshots_dir, "linkedin",
            expected_fields=["company", "role", "source_url", "location", "stipend_text"]
        )

    def method(self) -> str:
        return "connector" if linkedin_client().is_available() else "scraper"

    def is_available(self) -> bool:
        return True

    def search(self, keywords: List[str], location: str = "India") -> List[RawPosting]:
        say(f"Checking LinkedIn for: {', '.join(keywords[:3])}...")
        if linkedin_client().is_available():
            return self._search_via_connector(keywords, location)
        return self._search_via_scraper(keywords, location)

    # --- Connector path (mcp-server-linkedin via streamable-http) -----------

    def _search_via_connector(self, keywords: List[str], location: str) -> List[RawPosting]:
        async def _run() -> List[RawPosting]:
            results: List[RawPosting] = []
            async with linkedin_client() as client:
                search_data = await client.call("search_jobs", {
                    "keywords": " ".join(keywords[:5]),
                    "location": location,
                    "job_type": "internship",
                    "experience_level": "internship,entry",
                    "max_pages": 1,
                    "date_posted": "past_month",
                    "sort_by": "date",
                })
                # The MCP server returns a plain-text notice (under "raw") when the
                # saved LinkedIn session has expired — surface that instead of a
                # silent "found 0" so the owner knows to re-login.
                raw_note = (search_data.get("raw") or "") if isinstance(search_data, dict) else ""
                if "no valid linkedin session" in raw_note.lower() or "log in" in raw_note.lower():
                    warn("LinkedIn session has expired. Re-authenticate by running:  "
                         "linkedin-mcp-server --login   (sign in, then re-run harvest).")
                    return []
                job_ids: List[str] = search_data.get("job_ids", [])[:10]
                if not job_ids:
                    return []
                say(f"LinkedIn: fetching details for {len(job_ids)} internship(s)...")
                for job_id in job_ids:
                    try:
                        detail = await client.call("get_job_details", {"job_id": job_id})
                        url = detail.get("url", f"https://www.linkedin.com/jobs/view/{job_id}/")
                        text = detail.get("sections", {}).get("job_posting", "")
                        info = _parse_job_posting_text(text)
                        results.append(RawPosting(
                            company=info.get("company", ""),
                            role=info.get("title", ""),
                            source_url=url,
                            source_site="linkedin",
                            location=info.get("location"),
                            remote_text=info.get("remote_text"),
                            stipend_text=info.get("stipend_text"),
                            posted_date_text=info.get("posted_text"),
                            extra={"job_id": job_id},
                        ))
                    except Exception as exc:
                        _logger.debug("Failed to get details for job %s: %s", job_id, exc)
            return results

        try:
            results = asyncio.run(_run())
        except Exception as exc:
            _logger.warning("LinkedIn MCP error: %s; falling back to scraper", exc)
            return self._search_via_scraper(keywords, location)

        self._drift.check_results([
            {"company": r.company, "role": r.role, "source_url": r.source_url,
             "location": r.location, "stipend_text": r.stipend_text}
            for r in results
        ])
        return results

    # --- Scraper fallback --------------------------------------------------

    def _search_via_scraper(self, keywords: List[str], location: str) -> List[RawPosting]:
        from .stealth import _is_playwright_ready, stealth_browser, human_delay
        if not _is_playwright_ready():
            warn("LinkedIn scraper skipped — run 'playwright install chromium' to enable it.")
            return []

        results = []
        query = "+".join(keywords[:3]).replace(" ", "%20")
        search_url = (
            f"https://www.linkedin.com/jobs/search/?keywords={query}"
            f"&location={location.replace(' ', '%20')}"
            f"&f_JT=I&f_E=1%2C2&sortBy=DD"
        )
        try:
            with stealth_browser(self.cookies_dir, "linkedin", proxies=self.proxies) as page:
                page.goto("https://www.linkedin.com/login")
                human_delay(2, 4)
                page.goto(search_url)
                human_delay(3, 6)
                html = page.content()
                results = self._parse_jobs_html(html)
                self._drift.check_results(
                    [{"company": r.company, "role": r.role, "source_url": r.source_url,
                      "location": r.location, "stipend_text": r.stipend_text} for r in results],
                    raw_html=html,
                )
        except BaseException as exc:
            _logger.error("LinkedIn scraper error: %s", exc)
            warn("LinkedIn scraper encountered an error and was skipped.")
        return results

    def _parse_jobs_html(self, html: str) -> List[RawPosting]:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        results = []
        for card in tree.css("div.job-search-card, li.jobs-search-results__list-item"):
            company = card.css_first(".job-search-card__company-name, .artdeco-entity-lockup__subtitle")
            title = card.css_first(".job-search-card__title, .artdeco-entity-lockup__title")
            link = card.css_first("a[href*='/jobs/view/']")
            location = card.css_first(".job-search-card__location")
            if not title or not company:
                continue
            results.append(RawPosting(
                company=company.text(strip=True),
                role=title.text(strip=True),
                source_url=link.attrs.get("href", "") if link else "",
                source_site="linkedin",
                location=location.text(strip=True) if location else None,
            ))
        return results

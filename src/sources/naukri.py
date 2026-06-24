"""Naukri source adapter.

Naukri's internal ``jobapi/v3/search`` endpoint is gated by Akamai + an
invisible reCAPTCHA, so plain HTTP requests get ``403 Access Denied`` or
``406 recaptcha required``. The reliable path is to drive a real (Patchright)
browser to the search results page — the page fires its own authenticated XHR
to ``jobapi/v3/search`` which we intercept and read as clean JSON. We also parse
the rendered DOM as a backup.
"""

from __future__ import annotations
import logging
from pathlib import Path
from typing import List, Optional

from .base import BaseSource, RawPosting
from .stealth import stealth_browser, human_delay, safe_content, safe_scroll
from .drift import SchemaDriftDetector
from ..narration.narrator import say, warn

_logger = logging.getLogger("assistant.sources.naukri")


class NaukriSource(BaseSource):
    site_name = "naukri"

    def __init__(self, cookies_dir: Path, snapshots_dir: Path, proxies: List[str],
                 apify_token: str = "", headless: bool = False):
        self.cookies_dir = cookies_dir
        self.snapshots_dir = snapshots_dir
        self.proxies = proxies
        self.apify_token = apify_token
        self.headless = headless
        self._drift = SchemaDriftDetector(
            snapshots_dir, "naukri",
            expected_fields=["company", "role", "source_url", "location", "stipend_text"]
        )

    def method(self) -> str:
        return "scraper"

    def search(self, keywords: List[str], location: str = "India") -> List[RawPosting]:
        say("Checking Naukri for fresh internships...")
        return self._search_via_browser(keywords, location)

    # --- Browser path (intercept jobapi XHR, fall back to DOM) ---------------

    def _search_via_browser(self, keywords: List[str], location: str) -> List[RawPosting]:
        from .stealth import _is_playwright_ready
        if not _is_playwright_ready():
            warn("Naukri scraper skipped — run 'patchright install chromium' to enable it.")
            return []

        # Naukri's pretty search slug works best with a short phrase (1-2 terms).
        # A long slug yields a page that never fires the standard search XHR.
        slug = "-".join(k.lower().replace(" ", "-") for k in keywords[:2])
        url = f"https://www.naukri.com/{slug}-internship-jobs?experience=0"

        captured: dict = {}

        def on_response(resp):
            if "jobapi/v3/search" in resp.url and not captured.get("jobDetails"):
                try:
                    captured["jobDetails"] = (resp.json() or {}).get("jobDetails") or []
                except Exception:
                    pass

        results: List[RawPosting] = []
        html = ""
        try:
            with stealth_browser(self.cookies_dir, "naukri", proxies=self.proxies,
                                 headless=self.headless) as page:
                page.on("response", on_response)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                # Give the page time to fire its authenticated jobapi XHR.
                for _ in range(6):
                    if captured.get("jobDetails"):
                        break
                    human_delay(1.5, 2.5)
                safe_scroll(page)
                human_delay(1, 2)
                html = safe_content(page)

            jobs = captured.get("jobDetails") or []
            if jobs:
                results = self._parse_api_jobs(jobs)
            if not results and html:
                results = self._parse_naukri_html(html)
        except BaseException as exc:
            _logger.error("Naukri scraper error: %s", exc)
            warn("Naukri scraper encountered an error and was skipped.")

        if not results:
            self._save_snapshot(html)
            _logger.warning("Naukri: 0 results (saved HTML for inspection)")

        raw_dicts = [{"company": r.company, "role": r.role, "source_url": r.source_url,
                      "location": r.location, "stipend_text": r.stipend_text} for r in results]
        try:
            self._drift.check_results(raw_dicts, raw_html=html or None)
        except Exception:
            pass
        return results

    # --- Parsers ------------------------------------------------------------

    def _parse_api_jobs(self, jobs: list) -> List[RawPosting]:
        results: List[RawPosting] = []
        for job in jobs:
            placeholders = job.get("placeholders") or []
            loc = None
            exp = None
            for ph in placeholders:
                t = (ph.get("type") or "").lower()
                if t == "location":
                    loc = ph.get("label")
                elif t == "experience":
                    exp = ph.get("label")
            if loc is None and placeholders:
                loc = placeholders[0].get("label")
            salary = job.get("salary")
            stipend = salary.get("label") if isinstance(salary, dict) else salary
            url = job.get("jdURL") or ""
            if url and not url.startswith("http"):
                url = "https://www.naukri.com" + url
            results.append(RawPosting(
                company=job.get("companyName", ""),
                role=job.get("title", ""),
                source_url=url,
                source_site="naukri",
                location=loc,
                stipend_text=stipend,
                posted_date_text=job.get("footerPlaceholderLabel") or job.get("createdDate"),
                eligibility_text=job.get("tagsAndSkills"),
                duration_text=exp,
                extra=job,
            ))
        return results

    def _parse_naukri_html(self, html: str) -> List[RawPosting]:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        results = []
        cards = tree.css(
            "article.jobTuple, div.srp-jobtuple-wrapper, "
            "div[class*='srp-jobtuple'], div[class*='jobTuple']"
        )
        for card in cards:
            title_node = (card.css_first("a[class*='title']") or
                          card.css_first("a.title") or card.css_first(".jobTitle"))
            company_node = (card.css_first("a[class*='comp-name']") or
                            card.css_first("span[class*='comp-name']") or
                            card.css_first(".companyInfo .subTitle"))
            loc_node = (card.css_first("span[class*='locWdth']") or
                        card.css_first("span[class*='loc']") or
                        card.css_first(".loc-link"))
            sal_node = card.css_first("span[class*='sal']") or card.css_first(".salary")
            posted_node = (card.css_first("span[class*='job-post-day']") or
                           card.css_first("span[class*='posted']") or card.css_first("time"))
            if not title_node:
                continue
            results.append(RawPosting(
                company=company_node.text(strip=True) if company_node else "Unknown",
                role=title_node.text(strip=True),
                source_url=title_node.attrs.get("href", ""),
                source_site="naukri",
                location=loc_node.text(strip=True) if loc_node else None,
                stipend_text=sal_node.text(strip=True) if sal_node else None,
                posted_date_text=posted_node.text(strip=True) if posted_node else None,
            ))
        return results

    def _save_snapshot(self, html: Optional[str]) -> None:
        if not html:
            return
        try:
            snap = Path(self.snapshots_dir) / "naukri" / "last_page.html"
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_text(html, encoding="utf-8", errors="replace")
        except Exception:
            pass

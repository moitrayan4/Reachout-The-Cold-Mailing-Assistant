"""Internshala source adapter — Apify MCP or Patchright scraper."""

from __future__ import annotations
import logging
from pathlib import Path
from typing import List

import httpx

from .base import BaseSource, RawPosting
from .stealth import stealth_browser, human_delay, safe_content, safe_scroll
from .drift import SchemaDriftDetector
from ..narration.narrator import say, warn

_logger = logging.getLogger("assistant.sources.internshala")

_APIFY_ACTOR = "logiover/internshala-scraper"


class InternshalaSource(BaseSource):
    site_name = "internshala"

    def __init__(self, cookies_dir: Path, snapshots_dir: Path, proxies: List[str],
                 apify_token: str = "", headless: bool = False):
        self.cookies_dir = cookies_dir
        self.snapshots_dir = snapshots_dir
        self.proxies = proxies
        self.apify_token = apify_token
        self.headless = headless
        self._drift = SchemaDriftDetector(
            snapshots_dir, "internshala",
            expected_fields=["company", "role", "source_url", "location", "stipend_text"]
        )

    def method(self) -> str:
        return "apify" if self.apify_token else "scraper"

    def search(self, keywords: List[str], location: str = "India") -> List[RawPosting]:
        say("Checking Internshala for fresh internships...")
        if self.apify_token:
            return self._search_via_apify(keywords, location)
        return self._search_via_scraper(keywords, location)

    # --- Apify path --------------------------------------------------------

    def _search_via_apify(self, keywords: List[str], location: str) -> List[RawPosting]:
        results = []
        try:
            run_resp = httpx.post(
                f"https://api.apify.com/v2/acts/{_APIFY_ACTOR}/run-sync-get-dataset-items",
                params={"token": self.apify_token},
                json={
                    "category": keywords[0] if keywords else "technology",
                    "location": location,
                    "work_from_home": False,
                    "maxItems": 100,
                },
                timeout=120,
            )
            run_resp.raise_for_status()
            for item in run_resp.json():
                results.append(RawPosting(
                    company=item.get("company_name", ""),
                    role=item.get("profile", ""),
                    source_url=item.get("url", ""),
                    source_site="internshala",
                    location=item.get("location"),
                    stipend_text=item.get("stipend"),
                    posted_date_text=item.get("posted_on"),
                    eligibility_text=item.get("who_can_apply"),
                    duration_text=item.get("duration"),
                    start_date_text=item.get("start_date"),
                    ppo_text="PPO" if item.get("ppo") else None,
                    extra=item,
                ))
        except Exception as exc:
            _logger.warning("Internshala Apify error: %s; falling back to scraper", exc)
            return self._search_via_scraper(keywords, location)

        raw_dicts = [{"company": r.company, "role": r.role, "source_url": r.source_url,
                      "location": r.location, "stipend_text": r.stipend_text} for r in results]
        self._drift.check_results(raw_dicts)
        return results

    # --- Scraper fallback --------------------------------------------------

    def _search_via_scraper(self, keywords: List[str], location: str) -> List[RawPosting]:
        from .stealth import _is_playwright_ready
        if not _is_playwright_ready():
            warn("Internshala scraper skipped — run 'playwright install chromium' to enable it.")
            return []

        results = []
        kw = "-".join(k.lower().replace(" ", "-") for k in keywords[:2])
        url = f"https://internshala.com/internships/{kw}-internship"
        try:
            with stealth_browser(self.cookies_dir, "internshala", proxies=self.proxies,
                                 headless=self.headless) as page:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                human_delay(2, 5)
                # Scroll to load more
                safe_scroll(page)
                human_delay(1, 2)
                html = safe_content(page)
                results = self._parse_internshala_html(html)
                raw_dicts = [{"company": r.company, "role": r.role, "source_url": r.source_url,
                              "location": r.location, "stipend_text": r.stipend_text} for r in results]
                self._drift.check_results(raw_dicts, raw_html=html)
        except BaseException as exc:
            _logger.error("Internshala scraper error: %s", exc)
            warn("Internshala scraper encountered an error and was skipped.")
        return results

    def _parse_internshala_html(self, html: str) -> List[RawPosting]:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        results = []
        for card in tree.css(".internship_meta, div[id^='internshiplist']"):
            company = card.css_first(".company_name, .company-name")
            title = card.css_first(".profile, .job-internship-name")
            link = card.css_first("a.view_detail_button, a[href*='/internship/detail/']")
            location = card.css_first(".location_link, .locations")
            stipend = card.css_first(".stipend_container .stipend, .stipend")
            posted = card.css_first(".status-inactive, .posted-time")
            duration = card.css_first(".item_body .months, .duration")
            ppo = card.css_first(".job_ppo, .ppo-badge")
            who = card.css_first(".who_can_apply, .eligibility")

            if not title:
                continue

            results.append(RawPosting(
                company=company.text(strip=True) if company else "Unknown",
                role=title.text(strip=True),
                source_url="https://internshala.com" + (link.attrs.get("href", "") if link else ""),
                source_site="internshala",
                location=location.text(strip=True) if location else None,
                stipend_text=stipend.text(strip=True) if stipend else None,
                posted_date_text=posted.text(strip=True) if posted else None,
                duration_text=duration.text(strip=True) if duration else None,
                ppo_text=ppo.text(strip=True) if ppo else None,
                eligibility_text=who.text(strip=True) if who else None,
            ))
        return results

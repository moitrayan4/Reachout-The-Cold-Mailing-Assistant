"""Unstop source adapter — Patchright scraper over the Angular SSR listing."""

from __future__ import annotations
import logging
from pathlib import Path
from typing import List

from .base import BaseSource, RawPosting
from .stealth import stealth_browser, human_delay, safe_content, safe_scroll
from .drift import SchemaDriftDetector
from ..narration.narrator import say, warn

_logger = logging.getLogger("assistant.sources.unstop")


class UnstopSource(BaseSource):
    site_name = "unstop"

    def __init__(self, cookies_dir: Path, snapshots_dir: Path, proxies: List[str],
                 headless: bool = False):
        self.cookies_dir = cookies_dir
        self.snapshots_dir = snapshots_dir
        self.proxies = proxies
        self.headless = headless
        self._drift = SchemaDriftDetector(
            snapshots_dir, "unstop",
            expected_fields=["company", "role", "source_url", "location", "stipend_text"]
        )

    def method(self) -> str:
        return "scraper"

    def search(self, keywords: List[str], location: str = "India") -> List[RawPosting]:
        say("Checking Unstop for fresh internships...")
        return self._search_via_scraper(keywords, location)

    def _search_via_scraper(self, keywords: List[str], location: str) -> List[RawPosting]:
        from .stealth import _is_playwright_ready
        if not _is_playwright_ready():
            warn("Unstop scraper skipped — run 'patchright install chromium' to enable it.")
            return []

        results: List[RawPosting] = []
        html = ""
        kw = "%20".join(keywords[:2])
        url = f"https://unstop.com/internships?search={kw}&location=India"
        try:
            with stealth_browser(self.cookies_dir, "unstop", proxies=self.proxies,
                                 headless=self.headless) as page:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                human_delay(3, 5)
                # Scroll a few times to load more lazy cards.
                for _ in range(3):
                    safe_scroll(page)
                    human_delay(1, 2)
                html = safe_content(page)
                results = self._parse_unstop_html(html)
                if not results:
                    snap = Path(self.snapshots_dir) / "unstop" / "last_page.html"
                    snap.parent.mkdir(parents=True, exist_ok=True)
                    snap.write_text(html, encoding="utf-8", errors="replace")
                    _logger.warning("Unstop: 0 results — saved HTML to %s for inspection", snap)
                raw_dicts = [{"company": r.company, "role": r.role, "source_url": r.source_url,
                              "location": r.location, "stipend_text": r.stipend_text} for r in results]
                self._drift.check_results(raw_dicts, raw_html=html)
        except BaseException as exc:
            _logger.error("Unstop scraper error: %s", exc)
            warn("Unstop scraper encountered an error and was skipped.")
        return results

    # --- Parser (Angular SSR output) -----------------------------------------

    def _parse_unstop_html(self, html: str) -> List[RawPosting]:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        results = []
        # Unstop is Angular — cards are <app-competition-listing> custom elements.
        for card in tree.css("app-competition-listing"):
            link = card.css_first("a.item, a[href*='/internships/'], a[href*='/jobs/']")
            title = card.css_first("h3[itemprop='name'], h3, h2")
            # Company is the first <p class="single-wrap"> inside the card caption.
            company = card.css_first("div.cptn p.single-wrap, p.single-wrap")
            location = card.css_first("[class*='location'], [class*='city']")
            stipend = card.css_first("[class*='stipend'], [class*='salary'], [class*='prize']")

            if not title:
                continue

            # Prefer the canonical URL from the schema.org meta tag if present.
            href = ""
            meta_url = card.css_first("meta[itemprop='url']")
            if meta_url:
                href = meta_url.attrs.get("content", "")
            if not href and link:
                href = link.attrs.get("href", "")
            if href and not href.startswith("http"):
                href = "https://unstop.com" + href

            results.append(RawPosting(
                company=company.text(strip=True) if company else "Unknown",
                role=title.text(strip=True),
                source_url=href,
                source_site="unstop",
                location=location.text(strip=True) if location else None,
                stipend_text=stipend.text(strip=True) if stipend else None,
            ))
        return results

"""Wellfound (AngelList) source adapter — Patchright scraper.

Wellfound sits behind Cloudflare and requires a logged-in session to see job
results. The first run opens a headed window so the owner can log in and clear
the Cloudflare challenge; the persistent browser profile then keeps that state.

The previous version crashed with "page is navigating" / "execution context was
destroyed" because it read the DOM mid-navigation. We now wait for the page to
settle and read content defensively via ``safe_content``.
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import List

from .base import BaseSource, RawPosting
from .stealth import stealth_browser, human_delay, safe_content, safe_scroll
from .drift import SchemaDriftDetector
from ..narration.narrator import say, warn

_logger = logging.getLogger("assistant.sources.wellfound")


class WellfoundSource(BaseSource):
    site_name = "wellfound"

    def __init__(self, cookies_dir: Path, snapshots_dir: Path, proxies: List[str],
                 headless: bool = False):
        self.cookies_dir = cookies_dir
        self.snapshots_dir = snapshots_dir
        self.proxies = proxies
        self.headless = headless
        self._drift = SchemaDriftDetector(
            snapshots_dir, "wellfound",
            expected_fields=["company", "role", "source_url", "location", "stipend_text"]
        )

    def method(self) -> str:
        return "scraper"

    def search(self, keywords: List[str], location: str = "India") -> List[RawPosting]:
        say("Checking Wellfound for startup internships...")
        return self._search_via_scraper(keywords, location)

    def _search_via_scraper(self, keywords: List[str], location: str) -> List[RawPosting]:
        from .stealth import _is_playwright_ready
        if not _is_playwright_ready():
            warn("Wellfound scraper skipped — run 'patchright install chromium' to enable it.")
            return []

        results: List[RawPosting] = []
        html = ""
        kw = "%20".join(keywords[:3])
        url = f"https://wellfound.com/jobs?q={kw}&type=internship&l=India"
        try:
            with stealth_browser(self.cookies_dir, "wellfound", proxies=self.proxies,
                                 headless=self.headless) as page:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Wait out the Cloudflare challenge / SPA hydration. Don't crash
                # if the page is still navigating.
                self._wait_for_results(page)
                safe_scroll(page)
                human_delay(1, 2)
                html = safe_content(page)
                results = self._parse_wellfound_html(html)
                if not results:
                    self._save_snapshot(html)
                    if self._looks_blocked(html):
                        warn("Wellfound returned a Cloudflare/login wall — "
                             "run once headed and log in to establish the session.")
                    else:
                        _logger.warning("Wellfound: 0 results (saved HTML for inspection)")
                raw_dicts = [{"company": r.company, "role": r.role, "source_url": r.source_url,
                              "location": r.location, "stipend_text": r.stipend_text} for r in results]
                self._drift.check_results(raw_dicts, raw_html=html)
        except BaseException as exc:
            _logger.error("Wellfound scraper error: %s", exc)
            warn("Wellfound scraper encountered an error and was skipped.")
        return results

    def _wait_for_results(self, page) -> None:
        """Wait for real job content, tolerating Cloudflare redirects."""
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        # Wait for any job anchor to appear (up to ~20s) — survives the CF hop.
        for _ in range(10):
            try:
                if page.query_selector("a[href*='/jobs/']"):
                    return
            except Exception:
                pass
            human_delay(1.5, 2.5)

    @staticmethod
    def _looks_blocked(html: str) -> bool:
        low = (html or "").lower()
        return (len(html) < 5000) or "just a moment" in low or "cf-challenge" in low

    def _save_snapshot(self, html: str) -> None:
        try:
            snap = Path(self.snapshots_dir) / "wellfound" / "last_page.html"
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_text(html or "", encoding="utf-8", errors="replace")
        except Exception:
            pass

    # --- Parsers ------------------------------------------------------------

    def _parse_wellfound_html(self, html: str) -> List[RawPosting]:
        results = self._parse_jsonld(html)
        if results:
            return results
        return self._parse_dom(html)

    def _parse_jsonld(self, html: str) -> List[RawPosting]:
        """Parse schema.org JobPosting blocks if Wellfound emits them."""
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        results: List[RawPosting] = []
        for node in tree.css("script[type='application/ld+json']"):
            try:
                data = json.loads(node.text())
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or item.get("@type") != "JobPosting":
                    continue
                org = item.get("hiringOrganization") or {}
                loc = item.get("jobLocation") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = (loc.get("address") if isinstance(loc, dict) else {}) or {}
                results.append(RawPosting(
                    company=(org.get("name") if isinstance(org, dict) else "") or "Unknown",
                    role=item.get("title", ""),
                    source_url=item.get("url", ""),
                    source_site="wellfound",
                    location=addr.get("addressLocality") if isinstance(addr, dict) else None,
                    posted_date_text=item.get("datePosted"),
                    is_startup_hint=True,
                ))
        return results

    def _parse_dom(self, html: str) -> List[RawPosting]:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        results: List[RawPosting] = []
        seen = set()
        # Job title links are the most stable anchor on the page.
        for link in tree.css("a[href*='/jobs/']"):
            href = link.attrs.get("href", "")
            role = link.text(strip=True)
            if not href or not role or len(role) < 3:
                continue
            if href in seen:
                continue
            seen.add(href)
            if not href.startswith("http"):
                href = "https://wellfound.com" + href

            # Walk up to a card container to find the company name.
            company = "Unknown"
            location = None
            node = link.parent
            for _ in range(5):
                if node is None:
                    break
                comp = node.css_first("[class*='company'], [class*='startup'], h2 a, h3 a")
                if comp and comp.text(strip=True) and comp.text(strip=True) != role:
                    company = comp.text(strip=True)
                loc = node.css_first("[class*='location']")
                if loc and loc.text(strip=True):
                    location = loc.text(strip=True)
                if company != "Unknown":
                    break
                node = node.parent

            results.append(RawPosting(
                company=company,
                role=role,
                source_url=href,
                source_site="wellfound",
                location=location,
                is_startup_hint=True,
            ))
        return results

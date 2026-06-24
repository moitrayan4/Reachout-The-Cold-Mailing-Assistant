"""CompanyCareersSource — watches the India career sites of curated "dream"
companies for internships open to the owner's graduating batch (2028).

Why this exists
---------------
The five job boards (LinkedIn/Naukri/Internshala/Unstop/Wellfound) are great for
volume, but the companies the owner actually dreams of (Google, DRDO, Goldman
Sachs, NVIDIA, …) often post early-career internships *only* on their own ATS.
This source goes straight to those career pages and, whenever it finds an
internship that mentions the 2028 batch (or pre-final / 3rd-year / penultimate),
flags it as **priority** so the pipeline surfaces it no matter what — it bypasses
the stipend/recency filters downstream.

How it works
------------
For each target company (round-robined ``company_watch_max_per_run`` at a time so
a daily run never takes forever):

1. Resolve its India careers/listing URL — from ``target_companies.yaml`` if
   given, else a cached DuckDuckGo lookup (``state/company_careers_urls.json``).
2. Render the page with the shared Patchright stealth browser (most ATS pages are
   JS SPAs) and read the settled HTML.
3. Parse schema.org ``JobPosting`` JSON-LD (emitted by Greenhouse/Lever/Workday/
   SmartRecruiters/most ATS) with a generic DOM-anchor fallback.
4. Keep only internships that look India-based, tagging each with batch/priority
   signals in ``RawPosting.extra``.

Everything is best-effort and defensive: a company that errors or blocks is
skipped, not fatal.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from .base import BaseSource, RawPosting
from .stealth import stealth_browser, human_delay, safe_content, safe_scroll, _is_playwright_ready
from . import ats_api
from ..narration.narrator import say, warn
from ..companies import TargetCompany, load_config

_logger = logging.getLogger("assistant.sources.company_careers")

# Domains that strongly indicate a real careers/ATS listing page.
_ATS_HINTS = (
    "greenhouse.io", "boards.greenhouse", "lever.co", "jobs.lever",
    "myworkdayjobs.com", "workday", "smartrecruiters.com", "successfactors",
    "icims.com", "eightfold.ai", "avature.net", "taleo.net", "phenom",
    "/careers", "/jobs", "careers.", "jobs.", "recruiting",
)

# Hosts whose URLs are real, individual job postings (not blogs/news/aggregator
# spam). A web-watch result is only trusted as an opportunity if it lives on one
# of these — or on the company's own careers domain. This is the anti-hallucination
# gate: we never invent a posting, we only forward ones that demonstrably exist.
_JOB_HOST_ALLOW = (
    "linkedin.com/jobs", "in.linkedin.com/jobs", "naukri.com", "internshala.com",
    "unstop.com", "wellfound.com", "instahyre.com", "hirist.tech", "cutshort.io",
    "glassdoor.com/job", "glassdoor.co.in/job", "indeed.com", "in.indeed.com",
    "prosple.com", "freshersworld.com", "iimjobs.com",
)

# Years old enough to certainly indicate a stale drive (relative to "now").
# We don't reject the current or previous year (those appear in live batch text).

# --- Internship / batch detection ------------------------------------------

_INTERN_RE = re.compile(r"\b(intern(ship)?|trainee|co-?op|apprentice|summer\s+intern)\b", re.IGNORECASE)
_NOT_INTERN_RE = re.compile(r"\b(internal|international|alternative)\b", re.IGNORECASE)

_INDIA_RE = re.compile(
    r"\b(india|bangalore|bengaluru|mumbai|delhi|hyderabad|chennai|pune|kolkata|"
    r"noida|gurgaon|gurugram|ahmedabad|jaipur|chandigarh|kochi|coimbatore|"
    r"mysore|mysuru|trivandrum|thiruvananthapuram|indore|nagpur|vadodara|"
    r"remote.?india|pan.?india|\bin\b)\b",
    re.IGNORECASE,
)

# A graduating batch the owner is *not* eligible for (older grads / experience).
_SENIOR_RE = re.compile(
    r"\b(20\d{2})\s*(?:pass|passout|batch|graduat)|"
    r"\b([3-9]|1[0-9])\+?\s*(?:years?|yrs?)\s+(?:of\s+)?experience\b",
    re.IGNORECASE,
)


def grad_year_patterns(year: int) -> re.Pattern:
    """Regex that matches mentions of a specific graduating batch (e.g. 2028)."""
    y = str(year)
    y2 = y[2:]                       # "28"
    start = str(year - 4)            # entry year for a 4-yr program, "2024"
    return re.compile(
        rf"\b{y}\b|\b{start}\s*[-–]\s*(?:{y}|{y2})\b|"
        r"\b(pre[- ]?final|penultimate|3rd[- ]?year|third[- ]?year)\b|"
        rf"class\s+of\s+{y}",
        re.IGNORECASE,
    )


def classify_batch(text: str, grad_year: int) -> str:
    """Return 'match' | 'unknown' | 'mismatch' for the target grad batch.

    - 'match'    : explicitly mentions the 2028 batch / pre-final / 3rd-year.
    - 'mismatch' : explicitly targets a different/older batch or needs experience.
    - 'unknown'  : no batch signal (internships default to current students → keep).
    """
    text = text or ""
    if grad_year_patterns(grad_year).search(text):
        return "match"
    if _SENIOR_RE.search(text):
        # ...but a 2028 mention anywhere still wins (handled above).
        return "mismatch"
    return "unknown"


def is_internship(title: str, body: str = "") -> bool:
    blob = f"{title} {body}"
    if _INTERN_RE.search(blob) and not _NOT_INTERN_RE.search(title or ""):
        return True
    return False


def looks_india(*texts: Optional[str]) -> bool:
    blob = " ".join(t for t in texts if t)
    return bool(_INDIA_RE.search(blob))


def _norm_name(s: Optional[str]) -> str:
    """Normalise a company/employer name to comparable alphanumerics."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---------------------------------------------------------------------------

class CompanyCareersSource(BaseSource):
    site_name = "company_careers"

    def __init__(self, cookies_dir: Path, snapshots_dir: Path, proxies: List[str],
                 settings, headless: bool = False):
        self.cookies_dir = cookies_dir
        self.snapshots_dir = snapshots_dir
        self.proxies = proxies
        self.settings = settings
        self.headless = headless
        self.cfg = load_config(settings)
        self.grad_year = self.cfg.target_grad_year
        self.max_per_run = max(1, int(getattr(settings, "company_watch_max_per_run", 20)))
        self._url_cache_path = settings.log_dir.parent / "company_careers_urls.json"
        self._ats_cache_path = settings.log_dir.parent / "company_ats.json"
        self._cursor_path = settings.log_dir.parent / "company_watch_cursor.json"
        self._browser_ready = False
        self.ats_discovery = bool(getattr(settings, "company_watch_ats_discovery", True))
        self.web_watch = bool(getattr(settings, "company_watch_web_search", True))
        self.web_watch_max_age = int(getattr(settings, "company_watch_web_search_max_age_days", 31))
        self._ats_cache: dict = {}
        # Most companies are scanned via their ATS JSON API (pure HTTP, thread-safe),
        # so we fan them out across threads. The headed-browser fallback shares one
        # persistent profile, so a lock serialises those launches.
        self._browser_lock = threading.Lock()
        # Aggregate diagnostics so a "0 results" run is explainable (did we even
        # reach any ATS API? how many jobs/internships did we actually see?).
        self._stats_lock = threading.Lock()
        self._stats: dict = {"ats_companies": 0, "no_ats": 0, "raw_jobs": 0, "intern_jobs": 0}
        self.scan_workers = max(1, int(getattr(settings, "company_watch_parallel", 8)))
        # The headed-browser fallback resolves URLs via web search, which often
        # returns wrong/heavy pages that time out or crash the renderer. Off by
        # default: the ATS JSON-API path is fast and reliable for most companies.
        self.browser_fallback = bool(getattr(settings, "company_watch_browser_fallback", False))
        # Caches for URL/cursor files must be guarded once scans run concurrently.
        self._cache_lock = threading.Lock()

    def method(self) -> str:
        return "scraper"

    def is_available(self) -> bool:
        # ATS JSON APIs need only an HTTP client; the browser is just a fallback.
        return bool(self.cfg.companies)

    # --- main entry ---------------------------------------------------------

    def search(self, keywords: List[str], location: str = "India") -> List[RawPosting]:
        companies = self._select_batch()
        if not companies:
            return []
        say(f"Watching {len(companies)} target-company career site(s) for "
            f"{self.grad_year}-batch internships "
            f"(of {len(self.cfg.companies)} total, round-robin)...")

        # Most companies are scraped via their ATS JSON API (no browser needed).
        # The headed browser is only the (opt-in) fallback for non-ATS portals.
        self._browser_ready = _is_playwright_ready() if self.browser_fallback else False
        if self.browser_fallback and not self._browser_ready:
            _logger.info("Playwright not installed — company watcher will use ATS JSON "
                         "APIs only (run 'patchright install chromium' for HTML fallback).")

        results: List[RawPosting] = []
        workers = min(self.scan_workers, len(companies))
        self._stats = {"ats_companies": 0, "no_ats": 0, "raw_jobs": 0,
                       "intern_jobs": 0, "discovered": 0, "web_watch_hits": 0}
        self._ats_cache = self._load_ats_cache()

        def scan(company: TargetCompany) -> List[RawPosting]:
            try:
                found = self._scan_company(company)
                if found:
                    say(f"  {company.name}: {len(found)} India intern listing(s).")
                return found
            except BaseException as exc:  # noqa: BLE001
                _logger.warning("Company-careers scan failed for %s: %s", company.name, exc)
                return []

        if workers <= 1:
            for company in companies:
                results.extend(scan(company))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(scan, c) for c in companies]
                for fut in as_completed(futures):
                    results.extend(fut.result())

        self._save_ats_cache()

        priority = sum(1 for r in results if r.extra.get("priority"))
        st = self._stats
        # Make a 0-result run explainable: prove we actually reached ATS APIs and
        # show how the funnel narrowed (jobs -> internships -> India internships).
        disc = f", {st['discovered']} ATS newly auto-discovered" if st.get("discovered") else ""
        say(f"Company watcher: scanned {len(companies)} company(s) -- "
            f"{st['ats_companies']} had a public ATS API ({st['raw_jobs']} job(s) seen, "
            f"{st['intern_jobs']} internship(s) anywhere){disc}, "
            f"{st['no_ats']} had no public API"
            f"{'' if self.browser_fallback else ' (browser fallback off)'}.")
        if self.web_watch and st.get("web_watch_hits"):
            say(f"  Web-watch found {st['web_watch_hits']} fresh internship posting(s) "
                f"(last month, real job-host links) at no-ATS target companies.")
        if st["ats_companies"] == 0:
            say("  Note: none of this round's companies expose a public ATS API, so there "
                "was nothing to fetch. The watcher round-robins, so other companies are "
                "checked on the next run.")
        elif st["intern_jobs"] > 0 and not results:
            say(f"  The {st['intern_jobs']} internship(s) found were all outside India, so "
                "none matched. (Internships are seasonal — India roles appear in bursts.)")
        say(f"Company watcher done: {len(results)} India internship listing(s), "
            f"{priority} flagged PRIORITY for the {self.grad_year} batch.")
        return results

    # --- per-company --------------------------------------------------------

    def _scan_company(self, company: TargetCompany) -> List[RawPosting]:
        # 1) Preferred: the company's ATS JSON API (clean, structured, reliable).
        ats, coords = self._resolve_ats(company)
        if ats and coords is not None:
            postings = self._ats_postings(company, ats, coords)
            n_intern = sum(1 for p in postings if is_internship(p.role, ""))
            with self._stats_lock:
                self._stats["ats_companies"] += 1
                self._stats["raw_jobs"] += len(postings)
                self._stats["intern_jobs"] += n_intern
            kept = self._tag_and_filter(postings, company, resolved_url=None,
                                        location_reliable=True)
            if kept:
                return kept
            # The ATS answered; don't waste a browser on the same company.
            return []
        with self._stats_lock:
            self._stats["no_ats"] += 1

        # 2) No public ATS: watch the open web for a freshly-posted internship at
        # this company (last month only, real postings on known job hosts only).
        if self.web_watch:
            watched = self._web_watch_company(company)
            if watched:
                return watched

        # 3) Fallback: render the careers page and parse JSON-LD / DOM.
        # Disabled by default — web-search URL resolution is unreliable (blogs,
        # forums, heavy SPAs) and renders crash/stall the browser.
        if not self.browser_fallback or not self._browser_ready:
            return []
        url = self._resolve_url(company)
        if not url:
            _logger.debug("No careers URL resolved for %s", company.name)
            return []
        html = self._fetch(url, company)
        if not html:
            return []
        postings = self._parse(html, company, url)
        return self._tag_and_filter(postings, company, resolved_url=url,
                                    location_reliable=False)

    # --- web-watch path (non-ATS companies) ---------------------------------

    def _web_watch_company(self, company: TargetCompany) -> List[RawPosting]:
        """Find fresh internships at a no-ATS company via a recency-bounded search.

        Strictly extraction-only — every returned posting comes from a real search
        result whose URL is an actual job posting (a known job host or the company's
        own careers domain); nothing is invented. Search is limited to the last
        month (``timelimit='m'``), so results are never stale, and an explicit
        old-year guard drops obvious past drives.
        """
        from ..contacts.web_search import search_web_recent
        year = datetime.utcnow().year
        queries = [
            f"{company.name} internship India {year}",
            f"{company.name} intern India apply",
        ]
        domain = (company.domain or "").lower()
        seen: set = set()
        out: List[RawPosting] = []
        for q in queries:
            if len(out) >= 5:
                break
            for r in search_web_recent(q, max_results=10, timelimit="m"):
                href = (r.get("href") or "").strip()
                title = (r.get("title") or "").strip()
                body = (r.get("body") or "").strip()
                if not href or not title or href in seen:
                    continue
                if not self._web_result_is_real_posting(company, domain, title, body, href, year):
                    continue
                seen.add(href)
                out.append(RawPosting(
                    company=company.name,
                    role=self._clean_web_role(title, company.name),
                    source_url=href,
                    source_site="company_careers",
                    location="India",
                    eligibility_text=(body[:600] or None),
                    extra={"web_watch": True},
                ))
                if len(out) >= 5:
                    break
        if out:
            with self._stats_lock:
                self._stats["web_watch_hits"] = self._stats.get("web_watch_hits", 0) + len(out)
        # Tag/filter conservatively: target-company yes, but PRIORITY only on an
        # explicit 2028-batch signal (web hits are lower-confidence than ATS JSON).
        return self._tag_and_filter(out, company, resolved_url=None,
                                    location_reliable=False, web_watch=True)

    def _web_result_is_real_posting(self, company: TargetCompany, domain: str,
                                    title: str, body: str, href: str, year: int) -> bool:
        """Strict gate so web-watch only forwards genuine, on-topic, fresh postings.

        Rejects (in order): off-host/blog URLs, non-internship titles, generic
        listing/search pages, non-India, stale years, and — most importantly —
        results where the actual employer is some *other* company (so a "Meta Ads"
        job at an agency, or a Google job, is never attributed to Meta)."""
        low = href.lower()

        # 1) Must be a specific posting on a known job host or the company's own
        #    careers domain (never a blog/news page).
        on_company = bool(domain) and domain in low and \
            any(k in low for k in ("career", "/job", "intern")) and \
            not any(b in low for b in ("/blog", "/news", "/press", "/stories", "/story"))
        on_job_host = any(h in low for h in _JOB_HOST_ALLOW)
        if not (on_job_host or on_company):
            return False
        # LinkedIn listing/search pages aren't individual jobs — require /jobs/view/.
        if "linkedin.com" in low and "/jobs/view/" not in low:
            return False

        # 2) Must read as an internship and an India role, not a stale drive.
        if not is_internship(title, body):
            return False
        if not (looks_india(title, body) or ats_api.is_india_location(title, body)
                or "india" in low):
            return False
        if self._mentions_stale_year(f"{title} {body} {href}", year):
            return False

        # 3) Generic listing titles ("Internship jobs in India", "X jobs") are not
        #    a specific opening.
        tl = title.lower()
        if re.search(r"\bjobs\b.*\bin india\b", tl) or re.search(r"\binternship jobs\b", tl):
            return False

        # 4) The employer must actually be this company (kills cross-attribution).
        return self._web_employer_ok(company.name, title, href)

    @staticmethod
    def _web_employer_ok(company: str, title: str, href: str) -> bool:
        """True only if the posting's employer is plausibly ``company``.

        Search for "<Company> internship" routinely surfaces jobs *mentioning* the
        company (e.g. "Meta Ads Internship" at an agency, or a Google role). We read
        the employer from the common "<Emp> hiring …", "… at <Emp>", and LinkedIn
        "-at-<emp>" slug patterns and reject when it's clearly a different company.
        """
        target = _norm_name(company)

        def _conflict(found: str) -> bool:
            f = _norm_name(found)
            if not f:
                return False
            return not (f == target or f.startswith(target) or target.startswith(f))

        m = re.search(r"^(.*?)\s+hiring\b", title, re.IGNORECASE)
        if m and _conflict(m.group(1)):
            return False
        m = re.search(r"\bat\s+([A-Za-z0-9&.\-' ]{2,40})$", title.strip())
        if m and _conflict(m.group(1)):
            return False
        m = re.search(r"-at-([a-z0-9\-]{2,40})(?:[/?]|$)", href.lower())
        if m and _conflict(m.group(1).replace("-", "")):
            return False

        # Beyond "no conflicting employer", require the company to actually appear.
        return target in _norm_name(title) or target in _norm_name(href)

    @staticmethod
    def _mentions_stale_year(text: str, current_year: int) -> bool:
        """True if the text names a clearly-past year (<= current_year - 2)."""
        for m in re.findall(r"\b(20\d{2})\b", text or ""):
            if int(m) <= current_year - 2:
                return True
        return False

    @staticmethod
    def _clean_web_role(title: str, company: str) -> str:
        """Trim a noisy search-result title down to a role string."""
        role = re.split(r"\s[\|\-–—:]\s", title)[0].strip()
        # Drop a leading "<Company> hiring/is hiring" prefix if present.
        role = re.sub(rf"^{re.escape(company)}\b[\s:]*", "", role, flags=re.IGNORECASE).strip()
        role = re.sub(r"^(hiring|is hiring|careers?)[\s:]*", "", role, flags=re.IGNORECASE).strip()
        return role or title.strip()

    def _tag_and_filter(self, postings: List[RawPosting], company: TargetCompany,
                        resolved_url: Optional[str], location_reliable: bool,
                        web_watch: bool = False) -> List[RawPosting]:
        """Keep India internships; tag each with batch/priority signals.

        ``location_reliable`` is True for ATS-API results (every job carries a real
        location, so non-India roles are dropped). For rendered HTML, location is
        often missing, so we keep an India-looking URL's listings as a fallback.
        """
        kept: List[RawPosting] = []
        for p in postings:
            title = p.role or ""
            loc = p.location or ""
            desc = p.eligibility_text or ""

            # Internship gate. For clean ATS titles, judge by the TITLE only —
            # using the description here matches roles that merely *mention* an
            # internship program (false positives). HTML anchors can be terse, so
            # there we also allow the surrounding text.
            intern_body = "" if location_reliable else desc
            if not is_internship(title, intern_body):
                continue

            # India gate — accept ANY Indian office (Bengaluru, Pune, Gurgaon, …)
            # or India-remote. For ATS, the structured location is authoritative;
            # don't let a description that name-drops "India" smuggle in a foreign
            # role. For HTML (location often missing) fall back to URL/look.
            if location_reliable:
                india = ats_api.is_india_location(loc) or ats_api.is_remote_india(loc, title)
            else:
                india = (ats_api.is_india_location(title, loc, desc)
                         or ats_api.is_remote_india(title, loc, desc)
                         or looks_india(title, loc, desc) or _india_url(resolved_url or ""))
            if self.cfg.india_only and not india:
                continue

            batch = classify_batch(f"{title} {desc}", self.grad_year)
            if batch == "mismatch":
                continue
            # ATS/HTML postings are authoritative -> priority on match OR unknown.
            # Web-watch hits are lower-confidence -> priority only on an explicit
            # 2028-batch match, so uncertain web finds don't bypass the filters.
            is_priority = (batch == "match") if web_watch else (batch in ("match", "unknown"))
            p.extra.update({
                "target_company": True,
                "company_category": company.category,
                "batch_signal": batch,
                "batch_match_year": self.grad_year if batch == "match" else None,
                "priority": is_priority,
                "web_watch": bool(web_watch),
            })
            if batch == "match":
                p.eligibility_text = (f"{desc} [{self.grad_year} batch]").strip()
            kept.append(p)
        return kept

    # --- ATS-API path -------------------------------------------------------

    def _resolve_ats(self, company: TargetCompany):
        """Return (ats_name, coords) for the company, or (None, None).

        Order: explicit yaml ats+token → ATS detected from a known careers URL →
        ATS detected from a web-search-resolved URL.
        """
        # Explicit hint with a usable token (amazon needs no token).
        if company.ats:
            coords = ats_api.coords_from_token(company.ats, company.ats_token)
            if coords is not None:
                return company.ats.lower(), coords

        # Detect from a configured careers URL.
        if company.careers_url:
            hit = ats_api.detect_ats(company.careers_url)
            if hit:
                return hit

        # Auto-discover the ATS (browser-free), with a persistent cache so each
        # company is only probed once. This is what extends coverage past the
        # pre-configured companies to the long tail.
        if self.ats_discovery:
            hit = self._discover_ats_cached(company)
            if hit:
                return hit

        # Detect from a web-search-resolved careers URL (only useful when we can
        # then render a non-ATS page, so reserve it for the opt-in browser path).
        if self.browser_fallback:
            url = self._resolve_url(company)
            if url:
                hit = ats_api.detect_ats(url)
                if hit:
                    return hit
        return None, None

    def _discover_ats_cached(self, company: TargetCompany):
        """Return (ats, coords) for a company via cached auto-discovery, or None.

        Caches both hits and misses (misses are re-checked every 10 days, since a
        company can adopt an ATS later). Discovery first probes name-slugs against
        Greenhouse/Lever/Ashby, then falls back to a web search for the company's
        ATS board URL (Workday/etc.) — all over plain HTTP, no browser.
        """
        key = company.key()
        with self._cache_lock:
            entry = self._ats_cache.get(key)
        if entry is not None:
            ts = entry.get("ts", "")
            fresh = ts and ts >= (datetime.utcnow() - timedelta(days=10)).isoformat()
            if entry.get("ats"):
                return entry["ats"], entry.get("coords") or {}
            if fresh:
                return None  # known no-ATS, still fresh — don't re-probe

        hit = ats_api.discover_ats(company.name, company.domain)
        if not hit:
            hit = self._discover_ats_url(company)  # web search -> ATS board URL

        with self._cache_lock:
            if hit:
                self._ats_cache[key] = {
                    "ats": hit[0], "coords": hit[1],
                    "ts": datetime.utcnow().isoformat(),
                }
                self._stats["discovered"] = self._stats.get("discovered", 0) + 1
            else:
                self._ats_cache[key] = {"ats": None, "ts": datetime.utcnow().isoformat()}
        return hit

    def _discover_ats_url(self, company: TargetCompany):
        """Web-search for the company's ATS board URL and parse it (no browser).

        Distinct from ``_resolve_url`` (which hunts for a human careers *page*):
        here we bias the query toward ATS hostnames and run ``detect_ats`` on
        *every* result URL, so a Workday/Greenhouse/Lever board link anywhere in
        the results is picked up. This is what catches the likes of
        ``adobe.wd5.myworkdayjobs.com`` / ``cisco.wd5.myworkdayjobs.com``.
        """
        from ..contacts.web_search import search_web
        queries = [
            f"{company.name} careers myworkdayjobs greenhouse lever smartrecruiters",
            f"{company.name} internship apply job opening",
        ]
        if company.domain:
            queries.append(f"{company.name} careers site:{company.domain}")
        seen = set()
        for q in queries:
            for r in search_web(q, max_results=8):
                href = (r.get("href") or "").strip()
                if not href or href in seen:
                    continue
                seen.add(href)
                hit = ats_api.detect_ats(href)
                # Crucial: a search result's ATS link may belong to a *different*
                # company — only accept it if its identity matches this company.
                if hit and ats_api.ats_identity_matches(company.name, hit[0], hit[1]):
                    return hit
        return None

    def _load_ats_cache(self) -> dict:
        try:
            if self._ats_cache_path.exists():
                return json.loads(self._ats_cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_ats_cache(self) -> None:
        try:
            with self._cache_lock:
                self._ats_cache_path.parent.mkdir(parents=True, exist_ok=True)
                self._ats_cache_path.write_text(
                    json.dumps(self._ats_cache, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Could not persist ATS cache: %s", exc)

    def _ats_postings(self, company: TargetCompany, ats: str, coords: dict) -> List[RawPosting]:
        try:
            jobs = ats_api.fetch(ats, coords, search_terms=["intern", "internship"])
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ATS fetch failed for %s (%s): %s", company.name, ats, exc)
            return []
        out: List[RawPosting] = []
        for j in jobs:
            out.append(RawPosting(
                company=company.name,
                role=j.title,
                source_url=j.url,
                source_site="company_careers",
                location=j.location or None,
                remote_text="remote" if j.remote else None,
                posted_date_text=j.posted,
                eligibility_text=(j.description or None),
                extra={"ats": ats},
            ))
        return out

    def _fetch(self, url: str, company: TargetCompany) -> str:
        html = ""
        # The persistent "company_careers" profile can only back one browser at a
        # time, so serialise headed fetches even though company scans run in parallel.
        try:
            with self._browser_lock:
                with stealth_browser(self.cookies_dir, "company_careers",
                                     proxies=self.proxies, headless=self.headless) as page:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    safe_scroll(page)
                    human_delay(1.0, 2.0)
                    html = safe_content(page)
            if not html or len(html) < 2000:
                self._save_snapshot(company, html)
        except BaseException as exc:  # noqa: BLE001
            _logger.warning("Fetch error for %s (%s): %s", company.name, url, exc)
        return html or ""

    # --- URL resolution -----------------------------------------------------

    def _resolve_url(self, company: TargetCompany) -> Optional[str]:
        if company.careers_url:
            return company.careers_url

        cache = self._load_url_cache()
        hit = cache.get(company.key())
        if hit and hit.get("url"):
            return hit["url"]

        url = self._discover_url(company)
        if url:
            # Re-load under the lock and merge so concurrent scans don't clobber.
            with self._cache_lock:
                cache = self._load_url_cache()
                cache[company.key()] = {"url": url, "resolved_at": datetime.utcnow().isoformat()}
                self._save_url_cache(cache)
        return url

    def _discover_url(self, company: TargetCompany) -> Optional[str]:
        """Find a careers/ATS listing URL via web search, biased to India + intern."""
        from ..contacts.web_search import search_web
        queries = [
            f"{company.name} India careers internship {self.grad_year}",
            f"{company.name} internship India careers",
        ]
        if company.domain:
            queries.insert(0, f"{company.name} careers internship site:{company.domain}")

        for q in queries:
            for r in search_web(q, max_results=8):
                href = (r.get("href") or "").strip()
                if not href:
                    continue
                if self._looks_like_careers(href, company):
                    return href
        return None

    @staticmethod
    def _looks_like_careers(href: str, company: TargetCompany) -> bool:
        low = href.lower()
        if not low.startswith("http"):
            return False
        # Reject editorial / community pages — these are the URLs that previously
        # got resolved by web search and then timed out or crashed the renderer.
        if any(bad in low for bad in (
            "/blog/", "/blogs/", "/news/", "/press/", "/article", "/community",
            "community.", "forum", "/help/", "wikipedia.", "youtube.", "facebook.",
            "linkedin.com/pulse", "/stories/", "/insights/",
        )):
            return False
        if any(h in low for h in _ATS_HINTS):
            return True
        # Same-domain careers path.
        if company.domain and company.domain.lower() in low and (
            "career" in low or "job" in low or "intern" in low
        ):
            return True
        return False

    def _load_url_cache(self) -> dict:
        try:
            if self._url_cache_path.exists():
                return json.loads(self._url_cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_url_cache(self, cache: dict) -> None:
        try:
            self._url_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._url_cache_path.write_text(
                json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Could not persist URL cache: %s", exc)

    # --- round-robin cursor -------------------------------------------------

    def _select_batch(self) -> List[TargetCompany]:
        companies = self.cfg.companies
        if not companies:
            return []
        if len(companies) <= self.max_per_run:
            return companies
        cursor = self._load_cursor()
        n = len(companies)
        start = cursor % n
        selected = [companies[(start + i) % n] for i in range(self.max_per_run)]
        self._save_cursor((start + self.max_per_run) % n)
        return selected

    def _load_cursor(self) -> int:
        try:
            if self._cursor_path.exists():
                return int(json.loads(self._cursor_path.read_text(encoding="utf-8")).get("cursor", 0))
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _save_cursor(self, cursor: int) -> None:
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self._cursor_path.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # --- parsing ------------------------------------------------------------

    def _parse(self, html: str, company: TargetCompany, page_url: str) -> List[RawPosting]:
        out = self._parse_jsonld(html, company, page_url)
        if out:
            return out
        return self._parse_dom(html, company, page_url)

    def _parse_jsonld(self, html: str, company: TargetCompany, page_url: str) -> List[RawPosting]:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        results: List[RawPosting] = []
        for node in tree.css("script[type='application/ld+json']"):
            try:
                data = json.loads(node.text())
            except Exception:
                continue
            for item in _iter_jobpostings(data):
                title = item.get("title") or ""
                if not title:
                    continue
                loc = _jsonld_location(item)
                results.append(RawPosting(
                    company=company.name,
                    role=title.strip(),
                    source_url=item.get("url") or page_url,
                    source_site="company_careers",
                    location=loc,
                    eligibility_text=_strip_html(item.get("description", ""))[:1500] or None,
                    posted_date_text=item.get("datePosted"),
                    extra={"ats": company.ats or "jsonld"},
                ))
        return results

    def _parse_dom(self, html: str, company: TargetCompany, page_url: str) -> List[RawPosting]:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        base = _origin(page_url)
        results: List[RawPosting] = []
        seen = set()
        for link in tree.css("a"):
            role = link.text(strip=True)
            href = link.attrs.get("href", "") or ""
            if not role or len(role) < 4 or not href:
                continue
            if not _INTERN_RE.search(role):
                continue
            if href in seen:
                continue
            seen.add(href)
            if href.startswith("/"):
                href = base + href
            elif not href.startswith("http"):
                continue
            # Try to read a nearby location label.
            location = None
            node = link.parent
            for _ in range(4):
                if node is None:
                    break
                loc_el = node.css_first("[class*='location'], [data-qa*='location'], .location")
                if loc_el and loc_el.text(strip=True):
                    location = loc_el.text(strip=True)
                    break
                node = node.parent
            results.append(RawPosting(
                company=company.name,
                role=role,
                source_url=href,
                source_site="company_careers",
                location=location,
                extra={"ats": company.ats or "dom"},
            ))
        return results

    def _save_snapshot(self, company: TargetCompany, html: str) -> None:
        try:
            safe = re.sub(r"[^a-z0-9]+", "_", company.name.lower()).strip("_")
            snap = Path(self.snapshots_dir) / "company_careers" / f"{safe}.html"
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_text(html or "", encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# JSON-LD helpers
# ---------------------------------------------------------------------------

def _iter_jobpostings(data):
    """Yield every JobPosting dict from an arbitrary JSON-LD blob."""
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, dict):
            t = cur.get("@type")
            types = t if isinstance(t, list) else [t]
            if "JobPosting" in types:
                yield cur
            # Some pages nest postings under @graph / itemListElement.
            for key in ("@graph", "itemListElement", "item"):
                if key in cur:
                    stack.append(cur[key])


def _jsonld_location(item: dict) -> Optional[str]:
    loc = item.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"),
                     addr.get("addressCountry")]
            parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
            return ", ".join(p for p in parts if p) or None
    if item.get("applicantLocationRequirements"):
        alr = item["applicantLocationRequirements"]
        if isinstance(alr, dict):
            return alr.get("name")
    return None


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()


def _origin(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""


def _india_url(url: str) -> bool:
    low = (url or "").lower()
    return ("india" in low or "/in/" in low or "-in-" in low or "location=india" in low
            or ".in/" in low)


def _mentions_remote(text: Optional[str]) -> bool:
    return bool(text and re.search(r"\b(remote|work.?from.?home|wfh|anywhere)\b", text, re.IGNORECASE))


__all__ = ["CompanyCareersSource", "classify_batch", "is_internship", "grad_year_patterns"]

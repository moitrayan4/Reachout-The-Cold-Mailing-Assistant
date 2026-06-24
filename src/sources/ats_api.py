"""ATS / careers JSON-API fetchers.

Rendering every company's careers page in a browser is slow and fragile. Most
modern career sites are powered by an applicant-tracking system (ATS) that
exposes a **public JSON API** with clean, structured data (title, location,
posted date, description). This module talks to those APIs directly with a
browser-impersonating TLS client (``curl_cffi``), which is far more reliable than
scraping HTML and is what actually lets the company watcher return real jobs.

Supported ATS (auto-detected from a careers/ATS URL, or given explicitly):
  * Greenhouse        boards-api.greenhouse.io/v1/boards/{token}/jobs
  * Lever             api.lever.co/v0/postings/{slug}
  * SmartRecruiters   api.smartrecruiters.com/v1/companies/{id}/postings
  * Ashby             api.ashbyhq.com/posting-api/job-board/{org}
  * Workday           {tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
  * Amazon            amazon.jobs/en/search.json   (special-cased, no token)

Every fetcher returns a list of :class:`AtsJob`. India filtering is the caller's
job (see :func:`is_india_location`) — these just return everything.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

_logger = logging.getLogger("assistant.sources.ats_api")

_TIMEOUT = 25
_IMPERSONATE = "chrome"


def _client():
    """Return a curl_cffi requests module (browser-TLS), or None if unavailable."""
    try:
        from curl_cffi import requests as creq  # type: ignore
        return creq
    except Exception:  # noqa: BLE001
        try:
            import httpx  # type: ignore
            return httpx
        except Exception:  # noqa: BLE001
            return None


def _get(url: str, **kw):
    cl = _client()
    if cl is None:
        return None
    try:
        if cl.__name__.startswith("curl_cffi"):
            return cl.get(url, impersonate=_IMPERSONATE, timeout=_TIMEOUT, **kw)
        return cl.get(url, timeout=_TIMEOUT, **kw)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("GET %s failed: %s", url, exc)
        return None


def _post(url: str, json_body: dict, **kw):
    cl = _client()
    if cl is None:
        return None
    try:
        if cl.__name__.startswith("curl_cffi"):
            return cl.post(url, json=json_body, impersonate=_IMPERSONATE, timeout=_TIMEOUT, **kw)
        return cl.post(url, json=json_body, timeout=_TIMEOUT, **kw)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("POST %s failed: %s", url, exc)
        return None


@dataclass
class AtsJob:
    title: str
    url: str
    location: str = ""
    posted: Optional[str] = None
    description: str = ""
    remote: bool = False


# ---------------------------------------------------------------------------
# India location detection — any Indian office counts (the owner is open to all).
# ---------------------------------------------------------------------------

_INDIA_CITIES = (
    "india", "bengaluru", "bangalore", "mumbai", "navi mumbai", "new delhi", "delhi",
    "noida", "greater noida", "gurgaon", "gurugram", "manesar", "hyderabad", "chennai",
    "pune", "kolkata", "ahmedabad", "gandhinagar", "jaipur", "surat", "lucknow",
    "chandigarh", "mohali", "kochi", "cochin", "coimbatore", "mysore", "mysuru",
    "trivandrum", "thiruvananthapuram", "indore", "nagpur", "vadodara", "baroda",
    "visakhapatnam", "vizag", "bhubaneswar", "bhopal", "kanpur", "nashik", "faridabad",
    "ghaziabad", "thane", "hosur", "vellore", "warangal", "madurai", "jalandhar",
    "ludhiana", "dehradun", "goa", "panaji", "kolhapur", "raipur", "ranchi", "guwahati",
    "jamshedpur", "udaipur", "rajkot", "trichy", "tiruchirappalli", "salem", "hubli",
)
_INDIA_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in _INDIA_CITIES) + r")\b", re.IGNORECASE)
# Trailing country code, e.g. "Bengaluru, IN" / "Pune, KA, IN".
_IN_CODE_RE = re.compile(r",\s*IN\b|\bIND\b|\(india\)", re.IGNORECASE)


def is_india_location(*texts: Optional[str]) -> bool:
    """True if any provided text names an Indian office (city or country)."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return False
    return bool(_INDIA_RE.search(blob) or _IN_CODE_RE.search(blob))


def is_remote_india(*texts: Optional[str]) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    return ("remote" in blob and (("india" in blob) or ("apac" in blob) or ("anywhere" in blob)))


# ---------------------------------------------------------------------------
# URL → ATS detection
# ---------------------------------------------------------------------------

def detect_ats(url: str) -> Optional[Tuple[str, dict]]:
    """Parse a careers/ATS URL into ('greenhouse'|'lever'|..., coords) or None."""
    if not url:
        return None
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    host = (p.netloc or "").lower()
    path_parts = [seg for seg in (p.path or "").split("/") if seg]
    qs = parse_qs(p.query or "")

    # Greenhouse: boards.greenhouse.io/{token}, job-boards.greenhouse.io/{token},
    #             boards.greenhouse.io/embed/job_board?for={token}, {token}.greenhouse.io
    if "greenhouse.io" in host:
        token = None
        if "for" in qs:
            token = qs["for"][0]
        elif path_parts and path_parts[0] not in ("embed",):
            token = path_parts[0]
        elif host.endswith(".greenhouse.io") and host.split(".")[0] not in ("boards", "job-boards", "boards-api"):
            token = host.split(".")[0]
        if token:
            return "greenhouse", {"token": token}

    # Lever: jobs.lever.co/{slug}
    if "lever.co" in host and path_parts:
        return "lever", {"slug": path_parts[0]}

    # SmartRecruiters: careers.smartrecruiters.com/{Company} or jobs.smartrecruiters.com/{Company}
    if "smartrecruiters.com" in host and path_parts:
        return "smartrecruiters", {"company": path_parts[0]}

    # Ashby: jobs.ashbyhq.com/{org}
    if "ashbyhq.com" in host and path_parts:
        return "ashby", {"org": path_parts[0]}

    # Workday: {tenant}.{dc}.myworkdayjobs.com/[locale/]{site}
    if "myworkdayjobs.com" in host:
        bits = host.split(".")
        tenant = bits[0] if bits else None
        dc = bits[1] if len(bits) > 1 and bits[1].startswith("wd") else "wd1"
        site = None
        for seg in path_parts:
            if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", seg) or seg.lower() in ("en", "en-us"):
                continue
            site = seg
            break
        if tenant and site:
            return "workday", {"tenant": tenant, "dc": dc, "site": site}

    # Amazon
    if "amazon.jobs" in host:
        return "amazon", {}

    return None


_NAME_SUFFIXES = (
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "company",
    "co", "plc", "gmbh", "technologies", "technology", "tech", "labs", "lab", "software",
    "solutions", "systems", "global", "international", "india", "group", "holdings",
    "the",
)


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _candidate_slugs(name: str) -> List[str]:
    """High-confidence ATS slug guesses derived from a company name.

    e.g. "Goldman Sachs" -> ["goldmansachs", "goldman-sachs", "goldman_sachs"];
    suffixes like Inc/Ltd/Technologies are stripped so "Postman Technologies"
    also yields "postman".
    """
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    core = [w for w in words if w not in _NAME_SUFFIXES] or words
    if not core:
        return []
    joined = "".join(core)
    cands = [joined, "-".join(core), "_".join(core)]
    if len(core) > 1:                       # also the leading token (e.g. "stripe")
        cands.append(core[0])
    # de-dup, keep order, drop 1-2 char noise
    seen, out = set(), []
    for c in cands:
        if len(c) >= 3 and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _name_matches(company: str, board_name: Optional[str]) -> bool:
    """True if a board's self-reported name plausibly matches the company."""
    if not board_name:
        return False
    a, b = _norm_name(company), _norm_name(board_name)
    if not a or not b:
        return False
    return a in b or b in a or a[:6] == b[:6]


def _ats_identity(coords: dict) -> str:
    """The slug/tenant that identifies whose board this is."""
    return (coords.get("token") or coords.get("slug") or coords.get("company")
            or coords.get("org") or coords.get("tenant") or "")


def ats_identity_matches(name: str, ats: str, coords: dict) -> bool:
    """True if an ATS board plausibly belongs to ``name``.

    Web search for "<company> careers greenhouse/workday/..." routinely returns a
    board belonging to a *different* company (an aggregator link, an unrelated
    org). Accepting those would attribute the wrong company's jobs to a target,
    so every web-discovered board is identity-checked here: the board's own
    name (Greenhouse) or its slug/tenant (everyone else) must match the company.
    """
    ats = (ats or "").lower()
    ident = _norm_name(_ats_identity(coords))
    if not ident:
        return ats == "amazon"
    # Accept only when the board's slug/tenant is one of the name-derived
    # candidates (exact, normalised) — so "Meta" won't match a "Meta1" board.
    candidates = {_norm_name(s) for s in _candidate_slugs(name)} | {_norm_name(name)}
    if ats == "greenhouse":
        # Strongest signal: the board reports its own company name.
        meta = _get(f"https://boards-api.greenhouse.io/v1/boards/{_ats_identity(coords)}")
        if meta is not None and getattr(meta, "status_code", 0) == 200:
            try:
                if _name_matches(name, (meta.json() or {}).get("name")):
                    return True
            except Exception:  # noqa: BLE001
                pass
    return ident in candidates


def discover_ats(name: str, domain: Optional[str] = None) -> Optional[Tuple[str, dict]]:
    """Probe the common ATS providers for a company by name, browser-free.

    Returns ``(ats, coords)`` for the first provider that responds with real,
    name-verified postings, else ``None``. This is what lets the company watcher
    cover the long tail of companies that never had an ATS token configured —
    reliably and over plain HTTP, with no headed browser.

    Precision over recall: Greenhouse is verified against the board's own name;
    the others require a tight slug match plus a non-empty postings response, so
    we don't misattribute some unrelated board's jobs to the company.
    """
    slugs = _candidate_slugs(name)
    if not slugs:
        return None
    strict = {_norm_name(name)}                     # tightest acceptable slugs
    strict.add(_norm_name(name).replace(" ", ""))

    for slug in slugs:
        # Greenhouse — verifiable via board metadata (safe to try every candidate).
        meta = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if meta is not None and getattr(meta, "status_code", 0) == 200:
            try:
                if _name_matches(name, (meta.json() or {}).get("name")):
                    return "greenhouse", {"token": slug}
            except Exception:  # noqa: BLE001
                pass

    # The remaining providers can't cheaply self-identify, so only trust a slug
    # that is a tight normalisation of the company name (not the leading-word guess).
    for slug in slugs:
        if _norm_name(slug) not in strict and slug.replace("-", "").replace("_", "") not in strict:
            continue
        lev = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1")
        if lev is not None and getattr(lev, "status_code", 0) == 200:
            try:
                if isinstance(lev.json(), list) and lev.json():
                    return "lever", {"slug": slug}
            except Exception:  # noqa: BLE001
                pass
        ash = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if ash is not None and getattr(ash, "status_code", 0) == 200:
            try:
                if (ash.json() or {}).get("jobs"):
                    return "ashby", {"org": slug}
            except Exception:  # noqa: BLE001
                pass
    return None


def coords_from_token(ats: str, token: Optional[str]) -> Optional[dict]:
    """Build coords from an explicit yaml hint. Workday token = 'tenant:dc:site'."""
    ats = (ats or "").lower()
    if ats == "amazon":
        return {}
    if not token:
        return None
    if ats == "greenhouse":
        return {"token": token}
    if ats == "lever":
        return {"slug": token}
    if ats == "smartrecruiters":
        return {"company": token}
    if ats == "ashby":
        return {"org": token}
    if ats == "workday":
        parts = token.split(":")
        if len(parts) == 3:
            return {"tenant": parts[0], "dc": parts[1], "site": parts[2]}
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def fetch(ats: str, coords: dict, search_terms: Optional[List[str]] = None) -> List[AtsJob]:
    ats = (ats or "").lower()
    try:
        if ats == "greenhouse":
            return fetch_greenhouse(coords["token"])
        if ats == "lever":
            return fetch_lever(coords["slug"])
        if ats == "smartrecruiters":
            return fetch_smartrecruiters(coords["company"])
        if ats == "ashby":
            return fetch_ashby(coords["org"])
        if ats == "workday":
            return fetch_workday(coords["tenant"], coords["dc"], coords["site"], search_terms)
        if ats == "amazon":
            return fetch_amazon()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("ATS fetch (%s) failed: %s", ats, exc)
    return []


# ---------------------------------------------------------------------------
# Per-ATS fetchers
# ---------------------------------------------------------------------------

def fetch_greenhouse(token: str) -> List[AtsJob]:
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    if not r or getattr(r, "status_code", 0) != 200:
        return []
    out: List[AtsJob] = []
    for j in (r.json().get("jobs") or []):
        loc = (j.get("location") or {}).get("name") or ""
        out.append(AtsJob(
            title=j.get("title", ""),
            url=j.get("absolute_url", ""),
            location=loc,
            posted=j.get("updated_at"),
            description=_strip(j.get("content", "")),
        ))
    return out


def fetch_lever(slug: str) -> List[AtsJob]:
    r = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not r or getattr(r, "status_code", 0) != 200:
        return []
    out: List[AtsJob] = []
    for j in (r.json() or []):
        cats = j.get("categories") or {}
        out.append(AtsJob(
            title=j.get("text", ""),
            url=j.get("hostedUrl", ""),
            location=cats.get("location", "") or "",
            posted=str(j.get("createdAt", "")) or None,
            description=_strip(j.get("descriptionPlain") or j.get("description", "")),
            remote=("remote" in (cats.get("workplaceType", "") or "").lower()),
        ))
    return out


def fetch_smartrecruiters(company: str) -> List[AtsJob]:
    out: List[AtsJob] = []
    offset = 0
    for _ in range(10):  # up to 1000 postings
        r = _get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
                 f"?limit=100&offset={offset}")
        if not r or getattr(r, "status_code", 0) != 200:
            break
        data = r.json()
        content = data.get("content") or []
        for j in content:
            loc = j.get("location") or {}
            loc_str = ", ".join(filter(None, [
                loc.get("city"), loc.get("region"), loc.get("country")]))
            jid = j.get("id")
            out.append(AtsJob(
                title=j.get("name", ""),
                url=f"https://jobs.smartrecruiters.com/{company}/{jid}",
                location=loc_str or (loc.get("countryCode", "") or ""),
                posted=j.get("releasedDate"),
                remote=bool(loc.get("remote")),
            ))
        if offset + len(content) >= data.get("totalFound", 0) or not content:
            break
        offset += 100
    return out


def fetch_ashby(org: str) -> List[AtsJob]:
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=false")
    if not r or getattr(r, "status_code", 0) != 200:
        return []
    out: List[AtsJob] = []
    for j in (r.json().get("jobs") or []):
        secondary = " ".join(
            s.get("location", "") if isinstance(s, dict) else str(s)
            for s in (j.get("secondaryLocations") or [])
        )
        out.append(AtsJob(
            title=j.get("title", ""),
            url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            location=" ".join(filter(None, [j.get("location", ""), secondary])),
            posted=j.get("publishedAt"),
            description=_strip(j.get("descriptionPlain", "")),
            remote=bool(j.get("isRemote")),
        ))
    return out


def fetch_workday(tenant: str, dc: str, site: str,
                  search_terms: Optional[List[str]] = None) -> List[AtsJob]:
    host = f"https://{tenant}.{dc}.myworkdayjobs.com"
    endpoint = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    search_text = "intern"
    out: List[AtsJob] = []
    offset = 0
    for _ in range(6):  # up to ~120 results
        body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search_text}
        r = _post(endpoint, body, headers={"Accept": "application/json"})
        if not r or getattr(r, "status_code", 0) != 200:
            break
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            break
        postings = data.get("jobPostings") or []
        for j in postings:
            ext = j.get("externalPath", "") or ""
            out.append(AtsJob(
                title=j.get("title", ""),
                url=f"{host}/{site}{ext}" if ext else host,
                location=j.get("locationsText", "") or "",
                posted=j.get("postedOn"),
            ))
        total = data.get("total", 0)
        offset += 20
        if offset >= total or not postings:
            break
    return out


def fetch_amazon() -> List[AtsJob]:
    out: List[AtsJob] = []
    offset = 0
    for _ in range(5):  # up to 500 results
        r = _get("https://www.amazon.jobs/en/search.json"
                 f"?base_query=intern&loc_query=India&country=IND"
                 f"&result_limit=100&offset={offset}&sort=recent")
        if not r or getattr(r, "status_code", 0) != 200:
            break
        data = r.json()
        hits = data.get("jobs") or []
        for j in hits:
            loc = j.get("location") or ", ".join(filter(None, [j.get("city"), j.get("country_code")]))
            path = j.get("job_path", "")
            out.append(AtsJob(
                title=j.get("title", ""),
                url=("https://www.amazon.jobs" + path) if path.startswith("/") else path,
                location=loc or "India",
                posted=j.get("posted_date"),
                description=_strip(j.get("description_short", "")),
            ))
        if not hits or offset + len(hits) >= data.get("hits", 0):
            break
        offset += 100
        time.sleep(0.5)
    return out


# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _strip(text: str) -> str:
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


__all__ = ["AtsJob", "fetch", "detect_ats", "discover_ats", "ats_identity_matches",
           "coords_from_token", "is_india_location", "is_remote_india"]

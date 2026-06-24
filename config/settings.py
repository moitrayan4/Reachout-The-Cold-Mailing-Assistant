"""Central configuration via pydantic-settings + .env file."""

from __future__ import annotations
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Groq ---
    groq_api_key: str = Field(..., description="Groq API key")
    groq_model: str = Field("llama-3.3-70b-versatile", description="Groq model id (auto-selected if not set)")

    # --- Gmail ---
    gmail_from_address: str = Field("you@example.com")
    gmail_app_password: str = Field("", description="App password for IMAP draft creation")

    # --- Gmail API (OAuth) — used when IMAP/SMTP basic-auth is blocked ---
    gmail_use_api: bool = Field(
        True,
        description="Prefer the Gmail API (OAuth) for drafts when authorized; "
        "falls back to IMAP app password otherwise.",
    )
    gmail_credentials_path: Path = Field(
        default=PROJECT_ROOT / "gmail_credentials.json",
        description="OAuth client (Desktop) JSON downloaded from Google Cloud.",
    )
    gmail_token_path: Path = Field(
        default=PROJECT_ROOT / "state" / "gmail_token.json",
        description="Cached OAuth token (created by `python -m src gmail-auth`).",
    )

    # --- Apify (optional) ---
    apify_token: str = Field("", description="Apify API token for Naukri/Internshala MCP scrapers")

    # --- LinkedIn MCP ---
    linkedin_chrome_profile_path: str = Field("", description="Chrome profile path logged into LinkedIn")

    # --- Hunter.io MCP (optional — verified-email booster) ---
    # API key for Hunter's remote MCP server (https://mcp.hunter.io/mcp).
    # Free plan = 50 credits/month. Leave empty to disable Hunter entirely.
    hunter_api_key: str = Field("", description="Hunter.io API key (used as X-API-KEY for its MCP server)")

    # --- Owner profile (set these in .env) ---
    owner_name: str = Field("Your Name")
    owner_email: str = Field("you@example.com")
    owner_college: str = Field("Your College")
    owner_batch: str = Field("2028")
    owner_graduation_year: int = Field(2028)

    # --- Scheduling ---
    harvest_time_ist: str = Field("08:00", description="Daily harvest time in IST (HH:MM)")

    # --- Proxies ---
    proxy_list: str = Field("", description="Comma-separated list of HTTP proxies")

    # --- Scraping ---
    scraper_headless: bool = Field(
        False,
        description="Run anti-bot scrapers (Naukri/Unstop/Wellfound) headless. "
        "Default False: headed mode reliably passes Akamai/Cloudflare; "
        "set True only if your IP/profile is already trusted.",
    )
    scraper_max_parallel: int = Field(
        4,
        description="How many source adapters to run at once. Each runs in its own "
        "subprocess so a browser/driver crash in one can never cascade into the "
        "others. Higher = faster but more simultaneous browser windows / RAM.",
    )
    source_timeout_s: int = Field(
        180,
        description="Hard wall-clock budget per source adapter. A source that "
        "exceeds it is force-killed (process terminated) and skipped, so one stuck "
        "site can't stall the whole harvest.",
    )

    # --- Paths ---
    db_path: Path = Field(default=PROJECT_ROOT / "state" / "app.db")
    llm_cache_db: Path = Field(default=PROJECT_ROOT / "state" / "llm_cache.db")
    log_dir: Path = Field(default=PROJECT_ROOT / "state" / "logs")
    cookies_dir: Path = Field(default=PROJECT_ROOT / "state" / "cookies")
    snapshots_dir: Path = Field(default=PROJECT_ROOT / "state" / "snapshots")
    resume_dir: Path = Field(default=PROJECT_ROOT, description="Root folder where the resume lives")

    # --- Email ---
    dry_run_email: bool = Field(False, description="Write drafts to disk instead of Gmail")

    # --- Company-careers watcher ---
    company_watch_enabled: bool = Field(
        True,
        description="Watch the India career sites of curated target companies "
        "(config/target_companies.yaml) for internships open to the 2028 batch.",
    )
    company_watch_grad_year: int = Field(
        2028,
        description="Graduating batch the watcher MUST surface internships for. "
        "Matching internships from target companies are pinned as PRIORITY and "
        "bypass the stipend/recency filters.",
    )
    company_watch_max_per_run: int = Field(
        20,
        description="Max number of target companies to scan per harvest run. The "
        "watcher round-robins through the full list across runs (cursor in state/).",
    )
    company_watch_parallel: int = Field(
        8,
        description="How many target companies to scan concurrently within the "
        "company watcher. ATS JSON-API scans (most companies) are pure HTTP and "
        "parallelise freely; the rare headed-browser fallback is serialised.",
    )
    company_watch_ats_discovery: bool = Field(
        True,
        description="Auto-discover a company's ATS (Greenhouse/Lever/Ashby/Workday/...) "
        "when no token is configured — by probing name-slugs and, failing that, a web "
        "search for its ATS board URL. Browser-free and reliable; results are cached in "
        "state/company_ats.json so it's a one-time cost per company. This is what widens "
        "coverage past the handful of pre-configured companies.",
    )
    company_watch_web_search: bool = Field(
        True,
        description="For target companies with NO public ATS API (Google/Microsoft/"
        "Apple/etc.), watch the open web for freshly-posted internships. Searches "
        "are restricted to the last month and only real postings on known job hosts "
        "(LinkedIn/Naukri/Internshala/...) or the company's own careers domain are "
        "kept — never invented, never stale. Set False to disable.",
    )
    company_watch_web_search_max_age_days: int = Field(
        31,
        description="Hard recency cap for web-watch results (the search is already "
        "limited to the last month; this is the belt-and-braces upper bound).",
    )
    company_watch_browser_fallback: bool = Field(
        False,
        description="Render a company's careers page in a headed browser when no "
        "ATS JSON API is detected. Default OFF: web-search URL resolution often "
        "returns wrong/heavy pages (blogs, SPAs) that time out or crash the "
        "renderer, which is slow and unreliable. The ATS-API path is fast and "
        "covers most target companies; enable this only if you need the long tail.",
    )

    # --- Eligibility ---
    stipend_min_inr: int = Field(20000, description="Minimum stipend in INR/month")
    max_posting_age_days: int = Field(31, description="Maximum age of a posting in days")
    drop_unknown_date: bool = Field(
        False,
        description="If True, drop postings whose posted-date can't be parsed. "
        "Default False: scraped listings often lack a machine-readable date, so "
        "fail-closing here silently discards most results.",
    )

    @property
    def proxies(self) -> list[str]:
        if not self.proxy_list.strip():
            return []
        return [p.strip() for p in self.proxy_list.split(",") if p.strip()]

    @field_validator("db_path", "llm_cache_db", "log_dir", "cookies_dir", "snapshots_dir",
                     "gmail_credentials_path", "gmail_token_path", mode="before")
    @classmethod
    def resolve_path(cls, v):
        p = Path(v) if not isinstance(v, Path) else v
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

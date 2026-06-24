"""Source-adapter factory.

Centralises how site adapters are built from ``Settings`` + ``config/sites.yaml``
so the same construction logic can run in two places:

1. The main process (``__main__._build_sources`` -> :func:`build_all_sources`).
2. A spawned worker subprocess that rebuilds a *single* adapter by name
   (:func:`build_source`) — see :mod:`src.agents.board_discoverer`. Running each
   browser adapter in its own process is what isolates Patchright's Node driver
   so one site's crash/hang can never cascade into the others.

The functions here must stay import-cheap and free of side effects so that the
Windows ``spawn`` start-method can re-import this module quickly in children.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import yaml

from .base import BaseSource


def _sites_config() -> dict:
    cfg_path = Path(__file__).parent.parent.parent / "config" / "sites.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f) or {}


def _enabled(site_cfg: dict, name: str, default: bool = True) -> bool:
    return bool(site_cfg.get(name, {}).get("enabled", default))


def build_source(name: str, settings) -> Optional[BaseSource]:
    """Build a single adapter by name, or return ``None`` if disabled/unknown.

    Used by the per-source worker subprocess, which only needs one adapter.
    """
    cfg = _sites_config()
    site_cfg = cfg.get("sites", {})
    proxies = settings.proxies

    if name == "linkedin" and _enabled(site_cfg, "linkedin"):
        from .linkedin import LinkedInSource
        return LinkedInSource(
            cookies_dir=settings.cookies_dir,
            snapshots_dir=settings.snapshots_dir,
            proxies=proxies,
            chrome_profile=settings.linkedin_chrome_profile_path,
        )

    if name == "naukri" and _enabled(site_cfg, "naukri"):
        from .naukri import NaukriSource
        return NaukriSource(
            cookies_dir=settings.cookies_dir,
            snapshots_dir=settings.snapshots_dir,
            proxies=proxies,
            apify_token=settings.apify_token,
            headless=settings.scraper_headless,
        )

    if name == "internshala" and _enabled(site_cfg, "internshala"):
        from .internshala import InternshalaSource
        return InternshalaSource(
            cookies_dir=settings.cookies_dir,
            snapshots_dir=settings.snapshots_dir,
            proxies=proxies,
            apify_token=settings.apify_token,
            headless=settings.scraper_headless,
        )

    if name == "unstop" and _enabled(site_cfg, "unstop"):
        from .unstop import UnstopSource
        return UnstopSource(
            cookies_dir=settings.cookies_dir,
            snapshots_dir=settings.snapshots_dir,
            proxies=proxies,
            headless=settings.scraper_headless,
        )

    if name == "wellfound" and _enabled(site_cfg, "wellfound"):
        from .wellfound import WellfoundSource
        return WellfoundSource(
            cookies_dir=settings.cookies_dir,
            snapshots_dir=settings.snapshots_dir,
            proxies=proxies,
            headless=settings.scraper_headless,
        )

    if name == "company_careers" and _enabled(site_cfg, "company_careers") and \
            getattr(settings, "company_watch_enabled", True):
        from .company_careers import CompanyCareersSource
        return CompanyCareersSource(
            cookies_dir=settings.cookies_dir,
            snapshots_dir=settings.snapshots_dir,
            proxies=proxies,
            settings=settings,
            headless=settings.scraper_headless,
        )

    return None


def build_all_sources(settings) -> Dict[str, BaseSource]:
    """Instantiate every enabled adapter — used by the main process bootstrap."""
    names = ["linkedin", "naukri", "internshala", "unstop", "wellfound", "company_careers"]
    sources: Dict[str, BaseSource] = {}
    for name in names:
        src = build_source(name, settings)
        if src is not None:
            sources[name] = src
    return sources

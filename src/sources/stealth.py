"""Patchright stealth browser context pool.

Uses Patchright (a patched, undetected Playwright fork) with a persistent
per-site browser profile. The persistent profile keeps cookies, login state and
anti-bot clearance (Akamai / Cloudflare) between runs, which is what lets
Naukri / Wellfound load real results.

On first use for a site, opens a headed window so the owner can log in once.
Subsequent runs reuse the saved profile. Headless mode is available but the
anti-bot sites generally require headed mode, so callers default to headed.
"""

from __future__ import annotations
import logging
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List

_logger = logging.getLogger("assistant.stealth")

# Patchright recommends NOT spoofing the user agent / fingerprint — the patched
# Chromium already presents a consistent, real fingerprint. We only set locale.


def _import_playwright():
    """Return a sync_playwright factory, preferring Patchright over Playwright."""
    try:
        from patchright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except Exception:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright


_READY_CACHE: Optional[bool] = None


def _is_playwright_ready() -> bool:
    """Return True if a (patch)playwright browser is installed and runnable.

    The result is memoised: each adapter used to call this right before opening
    its own browser, which spun up (and tore down) an extra Node driver per
    source. That churn — multiplied across parallel sources — is exactly what
    provoked the EPIPE / "connection closed while reading from the driver"
    cascades. Checking once per process removes that whole class of failure.
    """
    global _READY_CACHE
    if _READY_CACHE is not None:
        return _READY_CACHE
    try:
        sync_playwright = _import_playwright()
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
            _READY_CACHE = Path(exe).exists()
    except Exception:
        _READY_CACHE = False
    return _READY_CACHE


def _clear_singleton_locks(profile_dir: Path) -> None:
    """Remove stale Chrome ``Singleton*`` lock files from a persistent profile.

    When a previous scraper run crashed (or was killed mid-flight), Chrome can
    leave ``SingletonLock`` / ``SingletonCookie`` / ``SingletonSocket`` behind.
    A fresh ``launch_persistent_context`` on that profile then either refuses to
    start or attaches to a phantom instance, so the page never navigates and
    every ``goto`` hangs to its timeout. Clearing them makes launches reliable.

    Safe because each site has its own dedicated profile here — these are never
    the user's day-to-day Chrome profile.
    """
    try:
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = profile_dir / name
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def _profile_dir(cookies_dir: Path, site: str) -> Path:
    d = cookies_dir / site / "profile"
    d.parent.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def stealth_browser(
    cookies_dir: Path,
    site: str,
    proxies: Optional[List[str]] = None,
    headless: bool = False,
):
    """Context manager yielding a Patchright Page in stealth mode.

    Uses a persistent context so cookies / logins / anti-bot clearance survive
    between runs. On first use per site (empty profile), forces a headed window
    so the owner can complete any required login.
    """
    sync_playwright = _import_playwright()

    profile_dir = _profile_dir(cookies_dir, site)
    # "First time" = the profile has never been populated. Force headed so the
    # owner can log in / clear any challenge once.
    first_time = not any(profile_dir.iterdir()) if profile_dir.exists() else True
    launch_headless = headless and not first_time

    # Drop any stale lock from a previously crashed run so the launch is clean.
    _clear_singleton_locks(profile_dir)

    proxy_cfg = None
    if proxies:
        proxy_url = random.choice(proxies)
        proxy_cfg = {"server": proxy_url}
        _logger.debug("Using proxy: %s", proxy_url)

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--start-maximized",
    ]

    if first_time:
        _logger.info(
            "First-time setup for %s — a browser window will open. "
            "Log in if prompted, then leave it; it will close automatically.",
            site,
        )

    with sync_playwright() as pw:
        ctx_kwargs: dict = {
            "user_data_dir": str(profile_dir),
            "headless": launch_headless,
            "args": launch_args,
            "locale": "en-IN",
            "no_viewport": True,
            "ignore_default_args": ["--enable-automation"],
        }
        if proxy_cfg:
            ctx_kwargs["proxy"] = proxy_cfg

        context = None
        # Prefer real Google Chrome (best stealth); fall back to bundled Chromium.
        for channel in ("chrome", None):
            try:
                kwargs = dict(ctx_kwargs)
                if channel:
                    kwargs["channel"] = channel
                context = pw.chromium.launch_persistent_context(**kwargs)
                break
            except Exception as exc:
                _logger.debug("Launch with channel=%s failed: %s", channel, exc)
                continue
        if context is None:
            raise RuntimeError("Could not launch a stealth browser for " + site)

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # A page that never finishes navigating should fail fast (and be retried by
        # safe_content) rather than block the whole run; the per-source process
        # timeout upstream is the hard backstop.
        try:
            context.set_default_navigation_timeout(45000)
            context.set_default_timeout(45000)
        except Exception:  # noqa: BLE001
            pass

        page = context.pages[0] if context.pages else context.new_page()

        try:
            yield page
        finally:
            _logger.info("Closing browser for %s (profile persisted)", site)
            try:
                context.close()
            except Exception as exc:
                _logger.warning("Could not close context for %s: %s", site, exc)


def safe_content(page, retries: int = 4, settle_ms: int = 1500) -> str:
    """Return page HTML, retrying through in-flight navigations.

    Patchright/Playwright raise "Unable to retrieve content because the page is
    navigating" or "Execution context was destroyed" when a SPA navigates while
    we read it. Retry until the DOM settles.
    """
    last_exc: Optional[Exception] = None
    for _ in range(max(1, retries)):
        try:
            return page.content()
        except Exception as exc:
            last_exc = exc
            try:
                page.wait_for_load_state("domcontentloaded", timeout=settle_ms)
            except Exception:
                pass
            time.sleep(settle_ms / 1000.0)
    # Last resort: pull the serialized DOM via JS (survives most nav races).
    try:
        return page.evaluate("() => document.documentElement.outerHTML")
    except Exception:
        if last_exc:
            _logger.warning("safe_content gave up: %s", last_exc)
        return ""


def safe_scroll(page) -> None:
    """Scroll to the bottom to trigger lazy loading; ignore nav races."""
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    except Exception as exc:
        _logger.debug("scroll skipped: %s", exc)


def human_delay(min_s: float = 1.0, max_s: float = 4.0) -> None:
    """Randomised human-like delay."""
    time.sleep(random.uniform(min_s, max_s))

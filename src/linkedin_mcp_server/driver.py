"""Long-lived Patchright LinkedIn session for the MCP server.

stickerdaniel's server keeps a single Selenium driver alive across tool calls.
We do the same with Patchright: one persistent browser context, opened lazily on
the first tool call and kept open until ``close()`` (the ``close_session`` tool)
or process exit. The persistent profile carries the LinkedIn login + anti-bot
clearance between server runs, exactly like the rest of this project's scrapers.

We deliberately reuse the project's existing stealth helpers
(``_import_playwright``, ``_clear_singleton_locks``) so the fingerprint and
launch behaviour match the other sources instead of drifting.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional, TypeVar

# Reuse the project's vetted stealth primitives rather than re-deriving them.
from ..sources.stealth import _import_playwright, _clear_singleton_locks  # type: ignore

_logger = logging.getLogger("linkedin_mcp.driver")

_T = TypeVar("_T")

# All Patchright work runs on ONE dedicated thread. Two reasons:
#   1. FastMCP invokes tools inside an asyncio loop, and the Playwright *sync*
#      API refuses to run on a thread with a live event loop.
#   2. Playwright sync objects are thread-affine — the page must be used on the
#      same thread that created it.
# A single-worker pool gives us both: an event-loop-free, stable thread.
_browser_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="lkbrowser"
)


def run_on_browser_thread(fn: Callable[..., _T], *args, **kwargs) -> _T:
    """Execute a browser-touching callable on the dedicated Playwright thread."""
    return _browser_pool.submit(fn, *args, **kwargs).result()


def capture_session(cdp_url: str, out_path: Optional[Path] = None) -> Path:
    """Export the LinkedIn login from a running browser into a storage_state file.

    Connects to a browser you started with --remote-debugging-port (CDP), reads
    its cookies/localStorage, keeps ONLY the linkedin.com entries (so we don't
    persist your whole browser's cookies), and writes them to disk. Later
    headless harvests load this file and are already signed in.
    """
    import json

    out = out_path or _storage_state_file()
    out.parent.mkdir(parents=True, exist_ok=True)

    def _work() -> Path:
        sync_playwright = _import_playwright()
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            state = ctx.storage_state()
            # Keep only LinkedIn cookies + origins.
            state["cookies"] = [c for c in state.get("cookies", [])
                                if "linkedin" in (c.get("domain") or "").lower()]
            state["origins"] = [o for o in state.get("origins", [])
                                if "linkedin" in (o.get("origin") or "").lower()]
            out.write_text(json.dumps(state), encoding="utf-8")
        return out

    path = run_on_browser_thread(_work)
    n = 0
    try:
        import json as _json
        n = len(_json.loads(path.read_text(encoding="utf-8")).get("cookies", []))
    except Exception:  # noqa: BLE001
        pass
    _logger.info("Saved %d LinkedIn cookie(s) to %s", n, path)
    return path


def _cdp_url() -> Optional[str]:
    """If set, attach to an already-running browser instead of launching one.

    Set LINKEDIN_MCP_CDP_URL=http://localhost:9222 to reuse a browser you
    started with --remote-debugging-port=9222 — i.e. your real, already
    logged-in Edge/Chrome session. We then scrape through that live session and
    leave the browser open when done.
    """
    return os.getenv("LINKEDIN_MCP_CDP_URL", "").strip() or None


def _storage_state_file() -> Path:
    """Path to the saved LinkedIn cookies (Playwright storage_state JSON)."""
    env = os.getenv("LINKEDIN_MCP_STORAGE_STATE")
    if env:
        return Path(env)
    return Path.home() / ".reachout-linkedin-mcp" / "storage_state.json"


def _storage_state_if_present() -> Optional[Path]:
    p = _storage_state_file()
    return p if p.exists() else None


def _default_profile_dir() -> Path:
    """Where the persistent LinkedIn browser profile lives.

    Overridable with LINKEDIN_MCP_PROFILE so the owner can point it at an
    existing Chrome profile or a project-local cookies dir.
    """
    env = os.getenv("LINKEDIN_MCP_PROFILE") or os.getenv("USER_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".reachout-linkedin-mcp" / "profile"


class LinkedInSession:
    """A singleton-ish holder for one open Patchright page.

    Thread-safe lazy open: MCP tool calls may arrive on different threads
    depending on the transport, so guard the launch with a lock.
    """

    _instance: Optional["LinkedInSession"] = None
    _lock = threading.Lock()

    def __init__(self, *, headless: Optional[bool] = None, profile_dir: Optional[Path] = None):
        if headless is None:
            headless = os.getenv("HEADLESS", "0") not in ("", "0", "false", "False")
        self.headless = headless
        self.profile_dir = profile_dir or _default_profile_dir()
        self._pw_cm = None      # sync_playwright() context manager
        self._pw = None         # entered playwright
        self._browser = None    # only set in CDP-attach mode
        self._context = None    # persistent browser context
        self._page = None       # the active page
        self._cdp_mode = False  # attached to an external browser?
        atexit.register(self.close)

    # -- singleton access --------------------------------------------------

    @classmethod
    def get(cls) -> "LinkedInSession":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # -- lifecycle ---------------------------------------------------------

    def _ensure_open(self):
        """Launch the persistent context on first use; reuse it afterwards."""
        if self._page is not None:
            return self._page
        with self._lock:
            if self._page is not None:
                return self._page

            sync_playwright = _import_playwright()
            self._pw_cm = sync_playwright()
            self._pw = self._pw_cm.__enter__()

            # --- Attach to an already-running browser (your live Edge session)?
            cdp_url = _cdp_url()
            if cdp_url:
                browser = self._pw.chromium.connect_over_cdp(cdp_url)
                self._browser = browser
                self._cdp_mode = True
                # Use the existing (logged-in) context, but open our own fresh
                # tab so we never navigate away from the user's other tabs.
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                self._context = context
                self._page = context.new_page()
                try:
                    self._page.set_default_navigation_timeout(45000)
                    self._page.set_default_timeout(45000)
                except Exception:  # noqa: BLE001
                    pass
                _logger.info("Attached to running browser via CDP at %s", cdp_url)
                return self._page

            # --- Headless run seeded with a captured LinkedIn session ---------
            # If we have saved cookies (from `--capture-session`), load them into
            # a fresh context. No persistent profile, no interactive login — the
            # harvest just reuses the session you were already logged into.
            state = _storage_state_if_present()
            if state:
                browser = None
                for opts in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
                    try:
                        browser = self._pw.chromium.launch(
                            headless=self.headless,
                            args=["--disable-blink-features=AutomationControlled"],
                            ignore_default_args=["--enable-automation"],
                            **opts,
                        )
                        break
                    except Exception as exc:  # noqa: BLE001
                        _logger.debug("launch %s failed: %s", opts, exc)
                        continue
                if browser is None:
                    raise RuntimeError("Could not launch a browser for LinkedIn")
                self._browser = browser
                context = browser.new_context(
                    storage_state=str(state), locale="en-IN",
                    viewport={"width": 1366, "height": 900},
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                try:
                    context.set_default_navigation_timeout(45000)
                    context.set_default_timeout(45000)
                except Exception:  # noqa: BLE001
                    pass
                self._context = context
                self._page = context.new_page()
                _logger.info("Loaded saved LinkedIn session from %s", state)
                return self._page

            # --- Otherwise launch our own persistent context ------------------
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            _clear_singleton_locks(self.profile_dir)

            # NOTE: deliberately no --no-sandbox / --disable-dev-shm-usage here.
            # Those are Linux/Docker flags; on a normal desktop they do nothing
            # useful and Chrome shows an "unsupported command-line flag:
            # --no-sandbox — stability and security will suffer" warning bar that
            # also makes the session look automated.
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ]
            ctx_kwargs: dict = {
                "user_data_dir": str(self.profile_dir),
                "headless": self.headless,
                "args": launch_args,
                "locale": "en-IN",
                "no_viewport": True,
                "ignore_default_args": ["--enable-automation"],
            }

            context = None
            # Launch order: Edge (the owner's browser), then Chrome, then the
            # bundled Chromium as a last resort.
            for opts in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
                try:
                    kwargs = dict(ctx_kwargs)
                    kwargs.update(opts)
                    context = self._pw.chromium.launch_persistent_context(**kwargs)
                    _logger.info("Launched browser with %s", opts or "bundled chromium")
                    break
                except Exception as exc:  # noqa: BLE001
                    _logger.debug("launch %s failed: %s", opts, exc)
                    continue
            if context is None:
                raise RuntimeError("Could not launch a stealth browser for LinkedIn")

            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            try:
                context.set_default_navigation_timeout(45000)
                context.set_default_timeout(45000)
            except Exception:  # noqa: BLE001
                pass

            self._context = context
            self._page = context.pages[0] if context.pages else context.new_page()
            return self._page

    @property
    def page(self):
        return self._ensure_open()

    def is_logged_in(self) -> bool:
        """Best-effort check: feed page redirects to /login when signed out."""
        try:
            page = self._ensure_open()
            page.goto("https://www.linkedin.com/feed/")
            page.wait_for_timeout(2500)
            return "/login" not in page.url and "/authwall" not in page.url
        except Exception as exc:  # noqa: BLE001
            _logger.debug("is_logged_in check failed: %s", exc)
            return False

    def close(self):
        with self._lock:
            # In CDP-attach mode the browser is the user's own running Edge —
            # never close its context/other tabs. Close only the tab we opened,
            # then disconnect our driver from it.
            if self._cdp_mode:
                try:
                    if self._page is not None:
                        self._page.close()
                except Exception as exc:  # noqa: BLE001
                    _logger.debug("cdp page close failed: %s", exc)
            else:
                try:
                    if self._context is not None:
                        self._context.close()
                except Exception as exc:  # noqa: BLE001
                    _logger.debug("context close failed: %s", exc)
                # storage_state mode uses launch() -> a Browser we must close.
                try:
                    if self._browser is not None:
                        self._browser.close()
                except Exception as exc:  # noqa: BLE001
                    _logger.debug("browser close failed: %s", exc)
            try:
                if self._pw_cm is not None:
                    self._pw_cm.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                _logger.debug("playwright exit failed: %s", exc)
            self._browser = self._context = self._page = None
            self._pw = self._pw_cm = None
            self._cdp_mode = False
            type(self)._instance = None

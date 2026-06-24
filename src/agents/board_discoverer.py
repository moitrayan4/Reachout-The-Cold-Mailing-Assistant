"""BoardDiscoverer — runs all job-board adapters in parallel with process
isolation and circuit-breaking.

Why processes, not threads
--------------------------
Every browser adapter drives Patchright (a Playwright fork) through its sync API,
which talks to a Node.js driver over a stdio pipe. Driving several of those from
worker *threads* in one interpreter is fragile: when one driver's pipe breaks
(``EPIPE`` / "connection closed while reading from the driver") the failure
bleeds into the others, and because a Python thread can't be force-killed, a
single hung ``page.goto`` stalls the whole harvest until its 60s timeout — over
and over, which is exactly the "everything broke" failure we saw.

Running each adapter in its own **subprocess** fixes both problems:

* Isolation — each process has its own Node driver and event loop, so a crash or
  hang in one site cannot corrupt or block any other.
* Hard timeouts — a process that blows its wall-clock budget is *terminated*
  (something you cannot do to a thread), so one stuck site never stalls the run.
* Real parallelism — independent processes scrape genuinely concurrently.

The child rebuilds its single adapter from settings via
:func:`src.sources.factory.build_source`; only the site name, keywords and the
resulting :class:`RawPosting` list cross the process boundary (all picklable).
Health/circuit-breaker bookkeeping stays in the parent so children never touch
the DB.
"""

from __future__ import annotations
import logging
import multiprocessing as mp
import queue as _queue
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..narration.narrator import say, site_status, warn
from ..storage.models import SiteHealth
from ..sources.base import BaseSource, RawPosting

_logger = logging.getLogger("assistant.agents.board_discoverer")

_MAX_CONSECUTIVE_FAILURES = 3
_DEFAULT_SOURCE_TIMEOUT_S = 180
_DEFAULT_MAX_PARALLEL = 4


def _source_worker(name: str, keywords: List[str], location: str, out_q) -> None:
    """Subprocess entrypoint: build one adapter, run it, return its postings.

    Always puts exactly one ``(name, method, postings, status)`` tuple on the
    queue (even on failure) so the parent never blocks waiting for a result.
    """
    method = "scraper"
    try:
        from config.settings import get_settings
        from src.sources.factory import build_source

        settings = get_settings()
        # Mirror the parent's file logging so child-side errors are captured.
        try:
            settings.log_dir.mkdir(parents=True, exist_ok=True)
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
                handlers=[logging.FileHandler(settings.log_dir / "debug.log", encoding="utf-8")],
            )
        except Exception:  # noqa: BLE001
            pass

        # Redirect this worker's native stderr (fd 2) into the log file. The
        # Patchright Node driver writes directly to fd 2, so a renderer/page crash
        # emits a raw "EPIPE: broken pipe" stack trace there. Without this it
        # splatters across the owner's console (the original scary screenshot);
        # captured here it's still recorded for debugging but stays off-screen.
        try:
            import os
            err_fd = os.open(str(settings.log_dir / "driver_stderr.log"),
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.dup2(err_fd, 2)
        except Exception:  # noqa: BLE001
            pass

        src = build_source(name, settings)
        if src is None:
            out_q.put((name, method, [], "unavailable"))
            return
        method = src.method()
        if not src.is_available():
            out_q.put((name, method, [], "unavailable"))
            return
        postings = src.search(keywords, location)
        out_q.put((name, method, list(postings or []), "ok"))
    except BaseException as exc:  # noqa: BLE001
        _logger.error("[%s] worker failed: %s", name, exc)
        out_q.put((name, method, [], f"error:{type(exc).__name__}: {exc}"))


class SourceManager:
    """Orchestrates all site adapters with process isolation + circuit-breaking."""

    def __init__(self, sources: Dict[str, BaseSource], db_session, db_path: Path):
        self.sources = sources
        self.session = db_session
        self.db_path = db_path
        self._settings = None  # lazily loaded for parallelism/timeout knobs

    # --- tunables (read from settings, with safe fallbacks) -----------------

    def _get_settings(self):
        if self._settings is None:
            try:
                from config.settings import get_settings
                self._settings = get_settings()
            except Exception:  # noqa: BLE001
                self._settings = False
        return self._settings or None

    @property
    def _max_parallel(self) -> int:
        s = self._get_settings()
        return max(1, int(getattr(s, "scraper_max_parallel", _DEFAULT_MAX_PARALLEL))) if s \
            else _DEFAULT_MAX_PARALLEL

    @property
    def _source_timeout(self) -> int:
        s = self._get_settings()
        return max(30, int(getattr(s, "source_timeout_s", _DEFAULT_SOURCE_TIMEOUT_S))) if s \
            else _DEFAULT_SOURCE_TIMEOUT_S

    # --- main entry ---------------------------------------------------------

    def collect_all(self, keywords: List[str], location: str = "India") -> List[RawPosting]:
        all_postings: List[RawPosting] = []

        # Decide which sources actually get to run (parent-side: health + creds).
        runnable: List[str] = []
        for name, src in self.sources.items():
            if not self._should_run(name, src):
                continue
            runnable.append(name)

        if not runnable:
            say("No sources are eligible to run right now.")
            return []

        timeout_s = self._source_timeout
        max_parallel = min(self._max_parallel, len(runnable))
        say(f"Starting collection from {len(runnable)} source(s), "
            f"up to {max_parallel} in parallel (each isolated in its own process)...")

        ctx = mp.get_context("spawn")
        pending = list(runnable)
        # proc -> (name, queue, start_time)
        active: Dict[mp.Process, Tuple[str, object, float]] = {}

        def launch(name: str) -> None:
            q = ctx.Queue()
            p = ctx.Process(target=_source_worker, args=(name, keywords, location, q),
                            name=f"src-{name}", daemon=True)
            p.start()
            active[p] = (name, q, time.time())

        while pending or active:
            while pending and len(active) < max_parallel:
                launch(pending.pop(0))

            time.sleep(0.25)

            for p in list(active.keys()):
                name, q, started = active[p]
                result = self._try_collect(p, q)

                if result is not None:
                    method, postings, status = result
                    self._finish(name, method, postings, status, all_postings)
                    p.join(timeout=5)
                    if p.is_alive():
                        p.terminate()
                    active.pop(p, None)
                    continue

                if time.time() - started > timeout_s:
                    _logger.error("[%s] timed out after %ss — terminating.", name, timeout_s)
                    p.terminate()
                    p.join(timeout=5)
                    if p.is_alive():
                        p.kill()
                    method = self.sources[name].method() if name in self.sources else "scraper"
                    site_status(name, method, -1)
                    self._record_health(name, method, ok=False)
                    active.pop(p, None)

        say(f"Collection done. Total raw postings: {len(all_postings)}.")
        return all_postings

    def _try_collect(self, p, q) -> Optional[Tuple[str, List[RawPosting], str]]:
        """Return the worker's ``(method, postings, status)`` if ready, else None.

        Drains the queue *before* the process is joined so a large result can't
        deadlock the child's feeder thread.
        """
        try:
            _name, method, postings, status = q.get_nowait()
            return method, postings, status
        except _queue.Empty:
            if not p.is_alive():
                # The process is gone. It almost certainly flushed its result to
                # the pipe before exiting, but the feeder is async — give the pipe
                # a brief moment before concluding it crashed without a result.
                try:
                    _name, method, postings, status = q.get(timeout=2)
                    return method, postings, status
                except _queue.Empty:
                    return "scraper", [], "error:process exited without a result"
                except Exception as exc:  # noqa: BLE001
                    return "scraper", [], f"error:{type(exc).__name__}: {exc}"
            return None
        except Exception as exc:  # noqa: BLE001 (e.g. unpickling failure)
            return "scraper", [], f"error:{type(exc).__name__}: {exc}"

    def _finish(self, name: str, method: str, postings: List[RawPosting],
                status: str, sink: List[RawPosting]) -> None:
        if status == "ok":
            count = len(postings)
            site_status(name, method, count)
            sink.extend(postings)
            self._record_health(name, method, ok=True)
        elif status == "unavailable":
            warn(f"Skipping {name} — not available (credentials/config).")
            # Not a failure: don't trip the circuit breaker.
        else:
            _logger.error("[%s] failed: %s", name, status)
            site_status(name, method, -1)
            self._record_health(name, method, ok=False)

    def _should_run(self, name: str, src: BaseSource) -> bool:
        health = self._load_health(name)
        if health and health.consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            warn(f"Skipping {name} today — it has failed {health.consecutive_failures} times in a row.")
            return False
        try:
            if not src.is_available():
                warn(f"Skipping {name} — required credentials are not configured.")
                return False
        except Exception:  # noqa: BLE001
            return True
        return True

    def _load_health(self, site: str) -> Optional[SiteHealth]:
        from sqlmodel import select
        try:
            stmt = select(SiteHealth).where(SiteHealth.site == site)
            return self.session.exec(stmt).first()
        except Exception:
            try:
                self.session.rollback()
            except Exception:
                pass
            return None

    def _record_health(self, site: str, method: str, ok: bool) -> None:
        from ..storage.database import get_session
        from sqlmodel import select

        try:
            with get_session(self.db_path) as s:
                stmt = select(SiteHealth).where(SiteHealth.site == site)
                health = s.exec(stmt).first()
                now = datetime.utcnow()
                if health is None:
                    health = SiteHealth(site=site, method=method)
                    s.add(health)
                health.method = method
                health.last_run = now
                if ok:
                    health.last_ok = now
                    health.consecutive_failures = 0
                    health.drift_flag = False
                else:
                    health.consecutive_failures = (health.consecutive_failures or 0) + 1
                s.commit()
        except Exception as exc:
            _logger.warning("Failed to record health for %s: %s", site, exc)

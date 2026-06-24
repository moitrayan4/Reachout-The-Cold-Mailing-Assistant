"""SchemaDriftDetector — detects when a scraped site changes its layout."""

from __future__ import annotations
import logging
from pathlib import Path
from typing import List, Optional

from ..narration.narrator import drift_alert

_logger = logging.getLogger("assistant.drift")


class SchemaDriftDetector:
    """Compares current page structure against a saved contract.

    The "contract" is a list of CSS selectors / field names expected to be
    present in each scraped result. If >50% of expected fields vanish, or
    results drop to 0 where many were normal, a drift alert fires.
    """

    def __init__(self, snapshots_dir: Path, site: str, expected_fields: List[str]):
        self.site = site
        self.expected_fields = expected_fields
        self.snapshot_dir = snapshots_dir / site
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._contract_file = self.snapshot_dir / "selector_contract.json"
        self._last_ok_count_file = self.snapshot_dir / "last_ok_count.txt"

    # --- Public API --------------------------------------------------------

    def check_results(self, results: List[dict], raw_html: Optional[str] = None) -> bool:
        """Return True if results look healthy; False (and alert) if drift detected.

        Only fires drift alert after at least 3 consecutive zero-result runs
        to avoid false positives from CAPTCHA / rate-limit on a single run.
        """
        count = len(results)
        last_ok = self._load_last_ok_count()
        consecutive_zeros = self._load_consecutive_zeros()

        if count == 0:
            consecutive_zeros += 1
            self._save_consecutive_zeros(consecutive_zeros)

        # Drift alert only after 3 consecutive zero runs (avoids CAPTCHA/rate-limit false positives)
        if count == 0 and last_ok and last_ok > 5 and consecutive_zeros >= 3:
            _logger.warning("[%s] Drift: got 0 results for 3 runs (last OK: %d)", self.site, last_ok)
            drift_alert(self.site)
            if raw_html:
                self._save_snapshot(raw_html)
            return False

        # Check field presence
        if results:
            self._save_consecutive_zeros(0)  # reset on success
            missing = self._count_missing_fields(results[0])
            if missing / max(len(self.expected_fields), 1) > 0.5:
                _logger.warning("[%s] Drift: >50%% expected fields missing", self.site)
                drift_alert(self.site)
                return False
            self._save_last_ok_count(count)
            if raw_html:
                self._save_snapshot(raw_html, ok=True)

        return True

    # --- Private helpers ---------------------------------------------------

    def _count_missing_fields(self, sample: dict) -> int:
        missing = 0
        for f in self.expected_fields:
            if f not in sample or sample[f] in (None, "", []):
                missing += 1
        return missing

    def _load_last_ok_count(self) -> Optional[int]:
        if self._last_ok_count_file.exists():
            try:
                return int(self._last_ok_count_file.read_text().strip())
            except Exception:
                return None
        return None

    def _save_last_ok_count(self, count: int) -> None:
        self._last_ok_count_file.write_text(str(count))

    def _load_consecutive_zeros(self) -> int:
        f = self.snapshot_dir / "consecutive_zeros.txt"
        if f.exists():
            try:
                return int(f.read_text().strip())
            except Exception:
                return 0
        return 0

    def _save_consecutive_zeros(self, count: int) -> None:
        (self.snapshot_dir / "consecutive_zeros.txt").write_text(str(count))

    def _save_snapshot(self, html: str, ok: bool = False) -> None:
        fname = "last_good.html" if ok else "last_bad.html"
        (self.snapshot_dir / fname).write_text(html, encoding="utf-8", errors="replace")

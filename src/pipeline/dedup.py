"""Deduplication — primary fingerprint + secondary soft-key cross-site merge."""

from __future__ import annotations
import logging
from typing import List, Dict

_logger = logging.getLogger("assistant.pipeline.dedup")


def dedup(normalised: List[dict]) -> List[dict]:
    """Merge duplicates. Same fingerprint → identical; same soft-key → merge source_urls."""
    seen_fp: Dict[str, dict] = {}       # fingerprint -> record
    seen_soft: Dict[str, dict] = {}     # soft_key -> record

    for record in normalised:
        fp = record["fingerprint"]
        sk = record["soft_key"]

        if fp in seen_fp:
            # Exact duplicate — skip
            _logger.debug("Dedup: exact duplicate fingerprint %s", fp)
            continue

        if sk in seen_soft:
            # Same role, different URL → merge source_urls
            existing = seen_soft[sk]
            urls = existing.get("source_urls", [])
            for url in record.get("source_urls", []):
                if url not in urls:
                    urls.append(url)
            existing["source_urls"] = urls
            # Preserve target-company / priority signals from either side — a
            # 2028 must-show flag should never be lost just because a job board
            # surfaced the same role first.
            for flag in ("priority", "is_target_company", "batch_2028"):
                if record.get(flag):
                    existing[flag] = True
            if record.get("company_category") and not existing.get("company_category"):
                existing["company_category"] = record["company_category"]
            _logger.debug("Dedup: merged cross-site duplicate for %s @ %s",
                          record["company"], record["role"])
            continue

        seen_fp[fp] = record
        seen_soft[sk] = record

    result = list(seen_fp.values())
    _logger.info("Dedup: %d raw → %d unique", len(normalised), len(result))
    return result

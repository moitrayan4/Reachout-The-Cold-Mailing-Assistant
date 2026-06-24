"""Manual skill overrides — lets you add skills the resume parser missed (or hide
ones it got wrong) from the dashboard, without re-editing the resume.

Edits are persisted to ``state/manual_skills.json`` as two lists::

    {"added": ["Rust", "Kubernetes"], "removed": ["jQuery"]}

They're layered on top of the LLM-parsed resume skills by
:meth:`src.profile.manager.ProfileManager._build_profile`, so the merged set
feeds everything downstream — match scoring and the cold-email draft included.
The store is keyed to the project, not the resume hash, so manual edits survive
a resume re-parse.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, List

from config.settings import Settings

_FILENAME = "manual_skills.json"


def _path(settings: Settings) -> Path:
    return settings.log_dir.parent / _FILENAME  # state/manual_skills.json


def load_manual_skills(settings: Settings) -> Dict[str, List[str]]:
    """Return ``{"added": [...], "removed": [...]}`` (always both keys present)."""
    p = _path(settings)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                added = [s for s in data.get("added", []) if isinstance(s, str) and s.strip()]
                removed = [s for s in data.get("removed", []) if isinstance(s, str) and s.strip()]
                return {"added": added, "removed": removed}
        except Exception:
            pass
    return {"added": [], "removed": []}


def _save(settings: Settings, data: Dict[str, List[str]]) -> None:
    p = _path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def add_skill(settings: Settings, skill: str) -> Dict[str, List[str]]:
    """Add a skill. Cancels any prior removal of the same skill."""
    skill = skill.strip()
    data = load_manual_skills(settings)
    if not skill:
        return data
    low = skill.lower()
    data["removed"] = [s for s in data["removed"] if s.lower() != low]
    if low not in [s.lower() for s in data["added"]]:
        data["added"].append(skill)
    _save(settings, data)
    return data


def remove_skill(settings: Settings, skill: str) -> Dict[str, List[str]]:
    """Remove a skill. Drops it from manual additions and suppresses it if it
    came from the parsed resume."""
    skill = skill.strip()
    data = load_manual_skills(settings)
    if not skill:
        return data
    low = skill.lower()
    data["added"] = [s for s in data["added"] if s.lower() != low]
    if low not in [s.lower() for s in data["removed"]]:
        data["removed"].append(skill)
    _save(settings, data)
    return data


def merge_skills(parsed: List[str], settings: Settings) -> List[str]:
    """Apply manual add/remove overrides to the parsed resume skills.

    Order: kept parsed skills first (minus removals), then manual additions.
    De-duplicated case-insensitively, first spelling wins.
    """
    data = load_manual_skills(settings)
    removed_low = {s.lower() for s in data["removed"]}

    result: List[str] = []
    seen: set[str] = set()
    for skill in list(parsed or []) + data["added"]:
        if not skill or not skill.strip():
            continue
        low = skill.lower()
        if low in removed_low or low in seen:
            continue
        seen.add(low)
        result.append(skill)
    return result

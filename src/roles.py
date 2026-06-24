"""Target roles manager — persists manually specified search roles to state/target_roles.json."""

from __future__ import annotations
import json
from pathlib import Path
from typing import List

from config.settings import Settings

_FILENAME = "target_roles.json"


def _path(settings: Settings) -> Path:
    return settings.log_dir.parent / _FILENAME  # state/target_roles.json


def load_roles(settings: Settings) -> List[str]:
    p = _path(settings)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [r for r in data if isinstance(r, str) and r.strip()]
        except Exception:
            pass
    return []


def save_roles(settings: Settings, roles: List[str]) -> None:
    p = _path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(roles, indent=2, ensure_ascii=False), encoding="utf-8")


def add_role(settings: Settings, role: str) -> List[str]:
    roles = load_roles(settings)
    role = role.strip()
    if role.lower() not in [r.lower() for r in roles]:
        roles.append(role)
        save_roles(settings, roles)
    return roles


def remove_role(settings: Settings, role: str) -> List[str]:
    roles = load_roles(settings)
    roles = [r for r in roles if r.lower() != role.strip().lower()]
    save_roles(settings, roles)
    return roles


def set_roles(settings: Settings, roles: List[str]) -> List[str]:
    roles = [r.strip() for r in roles if r.strip()]
    save_roles(settings, roles)
    return roles


def clear_roles(settings: Settings) -> None:
    save_roles(settings, [])

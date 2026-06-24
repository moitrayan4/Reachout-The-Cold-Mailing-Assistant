"""Target-companies manager — the curated "dream companies" whose India career
sites are watched for internships open to the owner's graduating batch (2028).

The base list lives in ``config/target_companies.yaml``. Owner edits (add/remove)
are persisted as a small override file ``state/target_companies.json`` so the
shipped YAML is never mutated. The effective list = YAML base merged with the
override (removals subtracted, additions appended).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from config.settings import Settings, PROJECT_ROOT

_logger = logging.getLogger("assistant.companies")

_YAML_PATH = PROJECT_ROOT / "config" / "target_companies.yaml"
_OVERRIDE_FILENAME = "target_companies.json"


@dataclass
class TargetCompany:
    """One watched company."""
    name: str
    category: str = "Other"
    careers_url: Optional[str] = None
    domain: Optional[str] = None
    ats: Optional[str] = None
    ats_token: Optional[str] = None      # board token / slug / company id;
    #                                      Workday encodes 'tenant:dc:site'.

    def key(self) -> str:
        return self.name.strip().lower()


@dataclass
class CompanyWatchConfig:
    """The full watch configuration."""
    target_grad_year: int = 2028
    india_only: bool = True
    companies: List[TargetCompany] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _override_path(settings: Settings) -> Path:
    return settings.log_dir.parent / _OVERRIDE_FILENAME  # state/target_companies.json


def _load_yaml() -> CompanyWatchConfig:
    if not _YAML_PATH.exists():
        return CompanyWatchConfig()
    try:
        data = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Could not parse target_companies.yaml: %s", exc)
        return CompanyWatchConfig()

    companies = []
    for entry in data.get("companies", []) or []:
        if isinstance(entry, str):
            companies.append(TargetCompany(name=entry))
        elif isinstance(entry, dict) and entry.get("name"):
            companies.append(TargetCompany(
                name=str(entry["name"]).strip(),
                category=str(entry.get("category", "Other")),
                careers_url=entry.get("careers_url") or None,
                domain=entry.get("domain") or None,
                ats=entry.get("ats") or None,
                ats_token=entry.get("ats_token") or None,
            ))
    return CompanyWatchConfig(
        target_grad_year=int(data.get("target_grad_year", 2028)),
        india_only=bool(data.get("india_only", True)),
        companies=companies,
    )


def _load_override(settings: Settings) -> dict:
    p = _override_path(settings)
    if not p.exists():
        return {"added": [], "removed": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("added", [])
            data.setdefault("removed", [])
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"added": [], "removed": []}


def _save_override(settings: Settings, override: dict) -> None:
    p = _override_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(override, indent=2, ensure_ascii=False), encoding="utf-8")


def load_config(settings: Settings) -> CompanyWatchConfig:
    """Return the effective watch config (YAML base + owner overrides)."""
    cfg = _load_yaml()
    # owner-configurable grad year wins if set in settings
    cfg.target_grad_year = getattr(settings, "company_watch_grad_year", None) or cfg.target_grad_year

    override = _load_override(settings)
    removed = {r.strip().lower() for r in override.get("removed", [])}

    companies = [c for c in cfg.companies if c.key() not in removed]
    existing = {c.key() for c in companies}
    for entry in override.get("added", []):
        if isinstance(entry, dict) and entry.get("name"):
            tc = TargetCompany(
                name=str(entry["name"]).strip(),
                category=str(entry.get("category", "Custom")),
                careers_url=entry.get("careers_url") or None,
                domain=entry.get("domain") or None,
                ats=entry.get("ats") or None,
                ats_token=entry.get("ats_token") or None,
            )
        elif isinstance(entry, str):
            tc = TargetCompany(name=entry.strip(), category="Custom")
        else:
            continue
        if tc.key() not in existing:
            companies.append(tc)
            existing.add(tc.key())

    cfg.companies = companies
    return cfg


def load_companies(settings: Settings) -> List[TargetCompany]:
    return load_config(settings).companies


# ---------------------------------------------------------------------------
# Mutations (owner add/remove)
# ---------------------------------------------------------------------------

def add_company(settings: Settings, name: str, category: str = "Custom",
                careers_url: Optional[str] = None, domain: Optional[str] = None) -> List[TargetCompany]:
    name = name.strip()
    if not name:
        return load_companies(settings)
    override = _load_override(settings)
    key = name.lower()
    # Un-remove if it was previously removed.
    override["removed"] = [r for r in override.get("removed", []) if r.strip().lower() != key]
    already = any(
        (isinstance(a, dict) and a.get("name", "").strip().lower() == key)
        or (isinstance(a, str) and a.strip().lower() == key)
        for a in override.get("added", [])
    )
    base_keys = {c.key() for c in _load_yaml().companies}
    if not already and key not in base_keys:
        override.setdefault("added", []).append({
            "name": name, "category": category,
            "careers_url": careers_url, "domain": domain,
        })
    _save_override(settings, override)
    return load_companies(settings)


def remove_company(settings: Settings, name: str) -> List[TargetCompany]:
    name = name.strip()
    key = name.lower()
    override = _load_override(settings)
    # Drop from added list if it was a custom addition.
    override["added"] = [
        a for a in override.get("added", [])
        if not ((isinstance(a, dict) and a.get("name", "").strip().lower() == key)
                or (isinstance(a, str) and a.strip().lower() == key))
    ]
    # Record a removal for base-list companies.
    base_keys = {c.key() for c in _load_yaml().companies}
    if key in base_keys and key not in {r.strip().lower() for r in override.get("removed", [])}:
        override.setdefault("removed", []).append(name)
    _save_override(settings, override)
    return load_companies(settings)

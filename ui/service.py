"""Service layer between the Streamlit UI and the existing app internals.

The UI never re-implements business logic — it calls the same functions the CLI
uses (ContactFinder, draft_email, save_draft_via_mcp, the roles manager, etc.).

Two execution styles:
  * Long, narrated runs (harvest / watch / push-drafts / schedule) are launched
    as `python -m src <cmd>` subprocesses so their plain-English narration can be
    streamed live into the UI.
  * Per-opportunity review decisions (approve / store / forget) run in-process,
    mirroring the REVIEW LangGraph's nodes but without the interactive interrupts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Cached shared resources (built once per Streamlit server process)
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_settings():
    """Load settings, initialise the DB schema and the LLM client once."""
    from config.settings import get_settings as _get
    settings = _get()

    settings.log_dir.mkdir(parents=True, exist_ok=True)

    from src.storage.database import init_db
    init_db(settings.db_path)

    from src.llm.groq_client import init_llm
    init_llm(settings.groq_api_key, settings.groq_model, settings.llm_cache_db)

    return settings


@st.cache_resource(show_spinner="Reading your resume profile…")
def get_profile_manager():
    """A long-lived ProfileManager with its own session (used for drafting)."""
    settings = get_settings()
    from src.storage.database import open_session
    from src.profile.manager import ProfileManager

    session = open_session(settings.db_path)
    pm = ProfileManager(settings.resume_dir, session, settings)
    try:
        pm.load_or_parse()
    except Exception:  # noqa: BLE001 — UI must stay up even if resume parse fails
        pass
    return pm


@contextmanager
def _session():
    """Short-lived session so the UI always reads fresh, uncached rows."""
    settings = get_settings()
    from src.storage.database import get_session
    with get_session(settings.db_path) as s:
        yield s


# ---------------------------------------------------------------------------
# Read helpers (dashboard / tables)
# ---------------------------------------------------------------------------

_STATUSES = [
    "pending_review", "presented", "approved",
    "drafted", "sent", "replied", "skipped_stored", "expired",
]


def opportunity_counts() -> dict:
    from sqlmodel import select
    from src.storage.models import Opportunity
    counts = {s: 0 for s in _STATUSES}
    with _session() as s:
        for opp in s.exec(select(Opportunity)).all():
            counts[opp.status] = counts.get(opp.status, 0) + 1
    return counts


def reviewable_opportunities() -> list[dict]:
    """Pending/presented opportunities, minus forgotten ones, best score first."""
    from sqlmodel import select
    from src.storage.models import Opportunity, OpportunityStatus, ForgottenFingerprint
    from src.orchestrator.review import _opp_to_dict

    with _session() as s:
        forgotten = {r.fingerprint for r in s.exec(select(ForgottenFingerprint)).all()}
        stmt = select(Opportunity).where(
            Opportunity.status.in_([
                OpportunityStatus.pending_review,
                OpportunityStatus.presented,
            ])
        )
        opps = [o for o in s.exec(stmt).all() if o.fingerprint not in forgotten]
        # Priority (2028-batch target-company internships) first, then by score.
        opps.sort(
            key=lambda o: (bool(getattr(o, "priority", False)), o.match_score or 0),
            reverse=True,
        )
        return [_opp_to_dict(o) for o in opps]


def list_contacts() -> list[dict]:
    from sqlmodel import select
    from src.storage.models import HRContact
    with _session() as s:
        rows = s.exec(select(HRContact)).all()
        return [
            {
                "company": c.company,
                "name": c.name,
                "designation": c.designation,
                "email": c.email,
                "verified": c.verified,
                "phone": c.phone,
                "profile_url": c.profile_url,
                "source": c.source,
                "city": c.office_city,
                "country": c.office_country,
            }
            for c in rows
        ]


def list_emails() -> list[dict]:
    from sqlmodel import select
    from src.storage.models import Email, Opportunity
    with _session() as s:
        rows = s.exec(select(Email)).all()
        out = []
        for e in rows:
            opp = s.get(Opportunity, e.fingerprint)
            out.append({
                "company": opp.company if opp else "—",
                "role": opp.role if opp else "—",
                "subject": e.subject,
                "to": ", ".join(e.get_to_addrs()),
                "status": e.status,
                "draft_id": e.draft_id,
                "created": e.created,
                "sent_at": e.sent_at,
                "last_reply_at": e.last_reply_at,
            })
        return out


def email_stats() -> dict:
    from sqlmodel import select
    from src.storage.models import Email
    with _session() as s:
        emails = s.exec(select(Email)).all()
        return {
            "total": len(emails),
            "draft": sum(1 for e in emails if e.status == "draft"),
            "sent": sum(1 for e in emails if e.status == "sent"),
            "replied": sum(1 for e in emails if e.status == "replied"),
        }


def site_health() -> list[dict]:
    from sqlmodel import select
    from src.storage.models import SiteHealth
    with _session() as s:
        rows = s.exec(select(SiteHealth)).all()
        return [
            {
                "site": h.site,
                "method": h.method,
                "last_ok": h.last_ok,
                "last_run": h.last_run,
                "failures": h.consecutive_failures,
                "drift": h.drift_flag,
            }
            for h in rows
        ]


def recent_actions(limit: int = 30) -> list[dict]:
    from sqlmodel import select
    from src.storage.models import Action, Opportunity
    with _session() as s:
        rows = s.exec(select(Action)).all()
        rows = sorted(rows, key=lambda a: a.timestamp or datetime.min, reverse=True)[:limit]
        out = []
        for a in rows:
            opp = s.get(Opportunity, a.fingerprint)
            out.append({
                "time": a.timestamp,
                "action": a.action_type,
                "company": opp.company if opp else a.fingerprint[:8],
                "role": opp.role if opp else "—",
                "notes": a.notes,
            })
        return out


def profile_summary() -> Optional[dict]:
    pm = get_profile_manager()
    p = pm.get_profile()
    if not p:
        return None
    return {
        "full_name": p.full_name,
        "batch": p.batch,
        "graduation_year": p.graduation_year,
        "current_year": p.current_year,
        "skills": p.skills,
        "domains": p.domains,
        "projects": p.projects,
        "preferred_roles": p.preferred_roles,
        "keywords_for_search": p.keywords_for_search,
        "resume_path": p.resume_path,
    }


# ---------------------------------------------------------------------------
# Skills management (manual overrides on top of the parsed resume)
# ---------------------------------------------------------------------------

def manual_skill_set() -> set[str]:
    """Lowercased set of skills the user added by hand (vs. parsed from resume)."""
    from src.skills import load_manual_skills
    return {s.lower() for s in load_manual_skills(get_settings())["added"]}


def add_skill(skill: str) -> None:
    """Add a skill and rebuild the cached profile so scoring/drafting pick it up."""
    from src.skills import add_skill as _add
    _add(get_settings(), skill)
    _reload_profile()


def remove_skill(skill: str) -> None:
    """Remove/suppress a skill and rebuild the cached profile."""
    from src.skills import remove_skill as _rm
    _rm(get_settings(), skill)
    _reload_profile()


def _reload_profile() -> None:
    """Re-merge skills into the long-lived ProfileManager's cached profile.

    Cheap — the resume itself is unchanged, so this re-reads the cached parse
    from the DB (no LLM call) and just re-applies the manual skill overrides.
    """
    try:
        get_profile_manager().load_or_parse()
    except Exception:  # noqa: BLE001 — UI must stay up even if the resume parse fails
        pass


# ---------------------------------------------------------------------------
# Roles management (thin wrapper over src.roles)
# ---------------------------------------------------------------------------

def gmail_status() -> tuple[bool, str]:
    """(ready, human-readable method) for the current Gmail draft delivery."""
    settings = get_settings()
    try:
        from src.email import gmail_api
        if settings.gmail_use_api and gmail_api.is_authorized(settings):
            return True, "Gmail API (OAuth)"
    except Exception:  # noqa: BLE001
        pass
    if settings.gmail_app_password:
        return True, "IMAP app password"
    from pathlib import Path
    if not Path(settings.gmail_credentials_path).exists():
        return False, "no OAuth client file"
    return False, "not connected"


def get_roles() -> list[str]:
    from src.roles import load_roles
    return load_roles(get_settings())


def add_role(role: str) -> list[str]:
    from src.roles import add_role as _add
    return _add(get_settings(), role)


def remove_role(role: str) -> list[str]:
    from src.roles import remove_role as _rm
    return _rm(get_settings(), role)


def set_roles(roles: list[str]) -> list[str]:
    from src.roles import set_roles as _set
    return _set(get_settings(), roles)


def clear_roles() -> None:
    from src.roles import clear_roles as _clear
    _clear(get_settings())


# ---------------------------------------------------------------------------
# Target companies (watched dream-company career sites)
# ---------------------------------------------------------------------------

def get_company_watch() -> dict:
    """Return {'grad_year', 'india_only', 'companies': [...]} for the UI."""
    from src.companies import load_config
    cfg = load_config(get_settings())
    return {
        "grad_year": cfg.target_grad_year,
        "india_only": cfg.india_only,
        "companies": [
            {"name": c.name, "category": c.category,
             "careers_url": c.careers_url, "domain": c.domain}
            for c in cfg.companies
        ],
    }


def add_company(name: str, category: str = "Custom") -> int:
    from src.companies import add_company as _add
    return len(_add(get_settings(), name, category=category))


def remove_company(name: str) -> int:
    from src.companies import remove_company as _rm
    return len(_rm(get_settings(), name))


def priority_opportunity_count() -> int:
    """How many reviewable opportunities are PRIORITY (2028-batch dream company)."""
    from sqlmodel import select
    from src.storage.models import Opportunity, OpportunityStatus, ForgottenFingerprint
    with _session() as s:
        forgotten = {r.fingerprint for r in s.exec(select(ForgottenFingerprint)).all()}
        stmt = select(Opportunity).where(
            Opportunity.status.in_([
                OpportunityStatus.pending_review,
                OpportunityStatus.presented,
            ])
        )
        return sum(
            1 for o in s.exec(stmt).all()
            if o.fingerprint not in forgotten and getattr(o, "priority", False)
        )


# ---------------------------------------------------------------------------
# Review decisions (in-process, mirrors review.py nodes without interrupts)
# ---------------------------------------------------------------------------

def approve_opportunity(fingerprint: str) -> dict:
    """Find HR contacts, draft the email and queue it — same as the REVIEW
    graph's `process_yes` node, minus the interactive low-trust prompt.

    Returns a result dict for the UI: subject/body/recipients/contacts/trust.
    """
    from sqlmodel import select
    from src.storage.models import (
        Opportunity, OpportunityStatus, Action, ActionType,
    )
    from src.orchestrator.review import _opp_to_dict
    from src.contacts.finder import ContactFinder
    from src.contacts.startup import verify_startup
    from src.email.drafter import draft_email
    from src.email.gmail_client import save_draft_via_mcp

    settings = get_settings()
    pm = get_profile_manager()

    with _session() as s:
        opp = s.get(Opportunity, fingerprint)
        if opp is None:
            raise ValueError("Opportunity no longer exists.")
        opp_dict = _opp_to_dict(opp)

        trust_verdict = "trusted"
        trust_reasons: list[str] = []
        if opp.is_startup:
            verdict, reasons = verify_startup(
                opp.company, opp.role or "",
                opp_dict.get("source_urls", [""])[0] if opp_dict.get("source_urls") else "",
            )
            trust_verdict, trust_reasons = verdict, reasons

        finder = ContactFinder(s, settings)
        contacts = finder.find_contacts(opp.company, opp.is_startup)

        profile = pm.get_profile() or pm.load_or_parse()
        draft = draft_email(settings, profile, opp_dict, contacts)
        save_draft_via_mcp(draft, settings, s, fingerprint, contacts=contacts)

        # Capture contact fields into plain dicts WHILE the session is still
        # open — ORM objects detach once the `with` block closes.
        contacts_data = [
            {
                "name": c.name,
                "designation": c.designation,
                "email": c.email,
                "verified": c.verified,
                "profile_url": c.profile_url,
            }
            for c in contacts
        ]

        opp.status = OpportunityStatus.drafted
        opp.trust_verdict = trust_verdict
        s.add(opp)
        s.add(Action(
            fingerprint=fingerprint,
            action_type=ActionType.drafted,
            timestamp=datetime.utcnow(),
            notes=f"contacts={len(contacts)}",
        ))
        s.commit()

        return {
            "subject": draft["subject"],
            "body": draft["body"],
            "to_addrs": draft.get("to_addrs", []),
            "needs_manual_recipient": draft.get("needs_manual_recipient", not draft.get("to_addrs")),
            "llm_fallback": draft.get("llm_fallback", False),
            "trust_verdict": trust_verdict,
            "trust_reasons": trust_reasons,
            "contacts": contacts_data,
        }


def store_opportunity(fingerprint: str) -> None:
    """Keep for later — mirrors `ask_store_or_forget` (store branch)."""
    from src.storage.models import Opportunity, OpportunityStatus, Action, ActionType
    with _session() as s:
        opp = s.get(Opportunity, fingerprint)
        if opp:
            opp.status = OpportunityStatus.skipped_stored
            s.add(opp)
        s.add(Action(
            fingerprint=fingerprint,
            action_type=ActionType.stored_for_later,
            timestamp=datetime.utcnow(),
        ))
        s.commit()


def forget_opportunity(fingerprint: str) -> None:
    """Hide permanently — mirrors `ask_store_or_forget` (forget branch)."""
    from src.storage.models import (
        Opportunity, ForgottenFingerprint, Action, ActionType,
    )
    with _session() as s:
        s.add(ForgottenFingerprint(fingerprint=fingerprint, hidden_at=datetime.utcnow()))
        s.add(Action(
            fingerprint=fingerprint,
            action_type=ActionType.forgotten,
            timestamp=datetime.utcnow(),
        ))
        opp = s.get(Opportunity, fingerprint)
        if opp:
            s.delete(opp)
        s.commit()


# ---------------------------------------------------------------------------
# Long-running narrated commands (streamed subprocesses)
# ---------------------------------------------------------------------------

def stream_command(args: list[str]) -> Iterator[str]:
    """Run `python -m src <args>` and yield its output line by line.

    Rich auto-detects the non-TTY pipe and emits plain text (no ANSI codes).
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "src", *args],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            yield line.rstrip("\n")
    finally:
        proc.stdout.close()
        proc.wait()
    yield f"\n[process exited with code {proc.returncode}]"

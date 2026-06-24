"""Gmail draft management.

Flow:
  1. Python pipeline (review) finds HR contact name/LinkedIn via web search agent.
  2. save_draft_via_mcp() writes the draft + contact info to pending_drafts.jsonl.
     If a real email is already known it also creates the IMAP draft immediately.
  3. When no email is known, run `python -m src push-drafts` — it re-runs the
     evidence-based email finder (published mailbox / verified address only) to
     fill in the recipient, then creates the IMAP draft.
"""

from __future__ import annotations
import imaplib
import json
import logging
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from ..narration.narrator import say, draft_saved, warn

_logger = logging.getLogger("assistant.email.gmail")

_IMAP_HOST = "imap.gmail.com"
_IMAP_PORT = 993
PENDING_DRAFTS_FILE = "pending_drafts.jsonl"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def save_draft_via_mcp(draft: dict, settings, db_session, fingerprint: str,
                        contacts: Optional[list] = None) -> Optional[str]:
    """
    Save a draft. If a real email is present, creates the IMAP draft immediately.
    Always writes to pending_drafts.jsonl so a later push can fill missing emails.
    """
    subject = draft["subject"]
    body = draft["body"]
    to_addrs = draft.get("to_addrs", [])
    from_addr = draft.get("from_addr", settings.gmail_from_address)
    resume_path = draft.get("resume_path")

    # Dry-run
    if settings.dry_run_email:
        dry_path = Path(settings.log_dir) / f"draft_{fingerprint[:8]}.txt"
        dry_path.parent.mkdir(parents=True, exist_ok=True)
        dry_path.write_text(
            f"Subject: {subject}\nTo: {', '.join(to_addrs)}\n\n{body}", encoding="utf-8"
        )
        say(f"[DRY RUN] Draft saved to: {dry_path}")
        return f"dry_run_{fingerprint[:8]}"

    # Attempt the draft FIRST so we can record whether it actually succeeded —
    # only then is the queue entry marked as pushed.
    draft_id = None
    if to_addrs and _can_create_draft(settings):
        draft_id = _create_draft(settings, from_addr, to_addrs, subject, body, resume_path)

    # Queue to file with the TRUE push status (so a failed push is retried,
    # and a missing email can still be enriched later by push-drafts).
    _queue(draft, fingerprint, settings, contacts, pushed=bool(draft_id))

    if draft_id:
        draft_saved(subject, draft_id)
        _save_email_record(db_session, fingerprint, draft_id, to_addrs, subject)
        return draft_id

    if to_addrs and _can_create_draft(settings):
        warn("Draft creation failed — draft queued. Run: python -m src push-drafts")
    elif not to_addrs:
        say("No email found yet — draft queued. Run: python -m src push-drafts")
    else:
        say("Gmail not connected (run `python -m src gmail-auth`) — draft queued.")

    local_id = f"pending_{fingerprint[:8]}"
    _save_email_record(db_session, fingerprint, local_id, to_addrs, subject)
    return local_id


# ---------------------------------------------------------------------------
# Queue file
# ---------------------------------------------------------------------------

def _queue(draft: dict, fingerprint: str, settings, contacts: Optional[list],
           pushed: bool = False) -> None:
    """Write draft + contact info to pending_drafts.jsonl."""
    pending_path = Path(settings.log_dir) / PENDING_DRAFTS_FILE
    pending_path.parent.mkdir(parents=True, exist_ok=True)

    contact_info = []
    if contacts:
        for c in contacts:
            contact_info.append({
                "name": getattr(c, "name", None),
                "designation": getattr(c, "designation", None),
                "email": getattr(c, "email", None),
                "profile_url": getattr(c, "profile_url", None),
                "company": getattr(c, "company", None),
            })

    entry = {
        "fingerprint": fingerprint,
        "subject": draft["subject"],
        "body": draft["body"],
        "to_addrs": draft.get("to_addrs", []),
        "from_addr": draft.get("from_addr", ""),
        "resume_path": draft.get("resume_path"),
        "contacts": contact_info,
        "pushed": pushed,  # True only when the IMAP draft was actually created
    }
    with open(pending_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# IMAP draft creation
# ---------------------------------------------------------------------------

def _build_mime(from_addr: str, to_addrs: list, subject: str, body: str,
                resume_path: Optional[str] = None) -> MIMEMultipart:
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs) if to_addrs else ""
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if resume_path and Path(resume_path).exists():
        with open(resume_path, "rb") as f:
            part = MIMEApplication(f.read(), Name=Path(resume_path).name)
        part["Content-Disposition"] = f'attachment; filename="{Path(resume_path).name}"'
        msg.attach(part)
    return msg


def _find_drafts_folder(imap: imaplib.IMAP4_SSL) -> str:
    _, folders = imap.list()
    for entry in (folders or []):
        text = entry.decode() if isinstance(entry, bytes) else str(entry)
        if "draft" in text.lower():
            parts = text.split('"')
            if len(parts) >= 2:
                name = parts[-2]
                if name and name != "/":
                    return name
    return "[Gmail]/Drafts"


def _imap_create_draft(from_addr: str, app_password: str,
                        to_addrs: list, subject: str, body: str,
                        resume_path: Optional[str] = None) -> Optional[str]:
    try:
        msg = _build_mime(from_addr, to_addrs, subject, body, resume_path)
        imap = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT)
        imap.login(from_addr, app_password)
        folder = _find_drafts_folder(imap)
        result, data = imap.append(
            f'"{folder}"', r"\Draft",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        imap.logout()
        if result == "OK":
            uid = data[0].decode() if data and data[0] else "ok"
            return f"imap_{uid}"
        return None
    except Exception as exc:
        _logger.error("IMAP draft failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Unified draft creation: Gmail API (OAuth) preferred, IMAP app password fallback
# ---------------------------------------------------------------------------

def _gmail_api_ready(settings) -> bool:
    if not getattr(settings, "gmail_use_api", True):
        return False
    try:
        from . import gmail_api
        return gmail_api.is_authorized(settings)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("Gmail API readiness check failed: %s", exc)
        return False


def _can_create_draft(settings) -> bool:
    """True if we can create a draft by any available method."""
    return _gmail_api_ready(settings) or bool(settings.gmail_app_password)


def _create_draft(settings, from_addr: str, to_addrs: list, subject: str,
                  body: str, resume_path: Optional[str] = None) -> Optional[str]:
    """Create a draft via the Gmail API if authorized, else via IMAP."""
    if _gmail_api_ready(settings):
        try:
            from . import gmail_api
            return gmail_api.create_draft(settings, from_addr, to_addrs, subject, body, resume_path)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Gmail API draft failed (%s); trying IMAP fallback.", exc)
            warn(f"Gmail API draft failed ({exc}); trying IMAP fallback.")
    if settings.gmail_app_password:
        return _imap_create_draft(from_addr, settings.gmail_app_password,
                                  to_addrs, subject, body, resume_path)
    return None


# ---------------------------------------------------------------------------
# Evidence-based email enrichment + push
# ---------------------------------------------------------------------------

def _extract_company_from_subject(subject: str) -> str:
    """Extract company name from 'Application for ... at Company Name'."""
    if " at " in subject:
        return subject.rsplit(" at ", 1)[-1].strip()
    return ""


def _find_verified_email(company: str, contacts: list) -> Optional[str]:
    """Find a *verified* email for the company using the evidence-based finder.

    Only returns an address that is published on the company's own site or
    SMTP-confirmed — never a guess. Tries each named contact first, then falls
    back to a published role mailbox (careers@/hr@/...).
    """
    from ..contacts.email_finder import find_email, find_generic_company_email

    # 1) Try each named contact's verified personal email.
    for contact in contacts:
        name = (contact.get("name") or "").strip()
        if name and " " in name:
            first, *rest = name.split()
            try:
                res = find_email(first, " ".join(rest), company)
            except Exception as exc:
                _logger.debug("find_email failed for %s: %s", name, exc)
                continue
            if res.get("status") == "verified" and res.get("email"):
                _logger.info("Verified email for %s / %s: %s", company, name, res["email"])
                return res["email"]

    # 2) Fall back to a published company role mailbox.
    if company:
        try:
            res = find_generic_company_email(company)
        except Exception as exc:
            _logger.debug("find_generic_company_email failed for %s: %s", company, exc)
            res = {}
        if res.get("status") == "verified" and res.get("email"):
            _logger.info("Verified company mailbox for %s: %s", company, res["email"])
            return res["email"]

    return None


def push_pending_drafts(settings, db_session) -> int:
    """
    Read pending_drafts.jsonl, enrich missing emails with the evidence-based
    email finder, then push IMAP drafts. Returns the number successfully pushed.
    """
    pending_path = Path(settings.log_dir) / PENDING_DRAFTS_FILE
    if not pending_path.exists():
        say("No pending drafts found.")
        return 0

    entries = []
    with open(pending_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    unpushed = [e for e in entries if not e.get("pushed", False)]
    if not unpushed:
        say("All drafts already pushed.")
        return 0

    say(f"Pushing {len(unpushed)} pending draft(s)...")
    pushed_count = 0

    for entry in entries:
        if entry.get("pushed", False):
            continue

        subject = entry.get("subject", "")
        to_addrs = entry.get("to_addrs") or []

        # Enrich missing email via the evidence-based finder
        if not to_addrs:
            company = _extract_company_from_subject(subject)
            contacts = entry.get("contacts") or []
            if company:
                say(f"  Looking up a verified email for {company}...")
                email = _find_verified_email(company, contacts)
                if email:
                    to_addrs = [email]
                    entry["to_addrs"] = to_addrs
                else:
                    warn(f"  No verified email found for {company} — skipping.")
                    continue
            else:
                warn(f"  Cannot extract company from subject: {subject!r} — skipping.")
                continue

        # Create the draft (Gmail API if authorized, else IMAP)
        from_addr = entry.get("from_addr") or settings.gmail_from_address
        if not _can_create_draft(settings):
            warn("Gmail not connected — run `python -m src gmail-auth` "
                 "or set GMAIL_APP_PASSWORD. Cannot push drafts.")
            break

        draft_id = _create_draft(
            settings,
            from_addr,
            to_addrs,
            subject,
            entry.get("body", ""),
            entry.get("resume_path"),
        )
        if draft_id:
            entry["pushed"] = True
            draft_saved(subject, draft_id)
            _save_email_record(db_session, entry["fingerprint"], draft_id, to_addrs, subject)
            pushed_count += 1
        else:
            warn(f"  IMAP failed for {subject!r} — will retry next push.")

    # Rewrite the file with updated pushed flags
    with open(pending_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    say(f"Pushed {pushed_count}/{len(unpushed)} draft(s) to Gmail.")
    return pushed_count


# ---------------------------------------------------------------------------
# DB record
# ---------------------------------------------------------------------------

def _save_email_record(db_session, fingerprint: str, draft_id: str,
                        to_addrs: list, subject: str) -> None:
    from ..storage.models import Email, EmailStatus
    from datetime import datetime
    from sqlmodel import select

    # Upsert: a draft may be re-queued (e.g. a failed IMAP push retried later),
    # so update the existing non-sent record rather than inserting a duplicate.
    existing = db_session.exec(
        select(Email).where(Email.fingerprint == fingerprint)
    ).first()
    if existing and existing.status == EmailStatus.draft:
        existing.draft_id = draft_id
        existing.to_addrs = json.dumps(to_addrs)
        existing.subject = subject
        db_session.add(existing)
    else:
        db_session.add(Email(
            fingerprint=fingerprint,
            draft_id=draft_id,
            to_addrs=json.dumps(to_addrs),
            subject=subject,
            status=EmailStatus.draft,
            created=datetime.utcnow(),
        ))
    db_session.commit()

"""Gmail API (OAuth) draft creation.

Used when the account blocks IMAP/SMTP basic-auth (common on edu Google
Workspace). Creates drafts directly in the user's mailbox via OAuth — no app
password needed.

One-time setup (per machine):
  1. In Google Cloud Console: create a project, enable the **Gmail API**, and
     create an **OAuth client ID** of type **Desktop app**.
  2. Download the client JSON and save it as `gmail_credentials.json` in the
     project root (or set GMAIL_CREDENTIALS_PATH).
  3. Run `python -m src gmail-auth` (or click "Connect Gmail" in the UI) and
     complete the browser consent. The token is cached at state/gmail_token.json.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import List, Optional

_logger = logging.getLogger("assistant.email.gmail_api")

# gmail.compose is the minimal scope that allows creating/updating drafts.
SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def _load_creds(settings):
    """Load cached credentials, refreshing them if expired. Returns None if the
    user has never authorized (no usable token)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_path = Path(settings.gmail_token_path)
    if not token_path.exists():
        return None

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds
    return None


def is_authorized(settings) -> bool:
    """True if we have a valid (or refreshable) token."""
    try:
        return _load_creds(settings) is not None
    except Exception as exc:  # noqa: BLE001
        _logger.debug("Gmail API token check failed: %s", exc)
        return False


def authorize(settings) -> str:
    """Run the one-time OAuth consent flow and cache the token.

    Returns the path to the saved token. Raises FileNotFoundError if the OAuth
    client JSON is missing.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    cred_path = Path(settings.gmail_credentials_path)
    if not cred_path.exists():
        raise FileNotFoundError(
            f"Missing OAuth client file at '{cred_path}'. In Google Cloud Console "
            "enable the Gmail API, create a Desktop OAuth client, download the JSON "
            "and save it there (or set GMAIL_CREDENTIALS_PATH)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path = Path(settings.gmail_token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    _logger.info("Gmail API token saved to %s", token_path)
    return str(token_path)


def get_service(settings):
    from googleapiclient.discovery import build

    creds = _load_creds(settings)
    if creds is None:
        raise RuntimeError("Gmail API not authorized. Run: python -m src gmail-auth")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def create_draft(settings, from_addr: str, to_addrs: List[str], subject: str,
                 body: str, resume_path: Optional[str] = None) -> Optional[str]:
    """Create a Gmail draft via the API. Returns a draft id (prefixed 'api_')."""
    # Reuse the existing MIME builder (handles the resume attachment).
    from .gmail_client import _build_mime

    service = get_service(settings)
    msg = _build_mime(from_addr, to_addrs, subject, body, resume_path)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    draft_id = draft.get("id")
    return f"api_{draft_id}" if draft_id else None

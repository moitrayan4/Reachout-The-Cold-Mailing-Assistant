"""ReplyTracker — polls Gmail for replies and updates the DB; also wires the LangGraph."""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Dict, Any, List

from langgraph.graph import StateGraph, END

from ..orchestrator.state import ReplyWatchState
from ..narration.narrator import reply_detected, say
from ..storage.models import Email, EmailStatus

_logger = logging.getLogger("assistant.agents.reply_tracker")


class ReplyTracker:

    def __init__(self, db_session):
        self.session = db_session

    def poll(self) -> int:
        """Poll Gmail for new replies. Returns count of new replies detected."""
        say("Checking for replies to your outreach emails...")
        sent_emails = self._load_sent_emails()

        if not sent_emails:
            say("No outreach emails sent yet — nothing to track.")
            return 0

        new_replies = 0
        for email in sent_emails:
            if self._check_for_reply(email):
                new_replies += 1

        if new_replies == 0:
            say("No new replies yet.")
        return new_replies

    def _load_sent_emails(self) -> List[Email]:
        from sqlmodel import select
        stmt = select(Email).where(Email.status.in_([EmailStatus.sent, EmailStatus.draft]))
        return self.session.exec(stmt).all()

    def _check_for_reply(self, email: Email) -> bool:
        thread_id = email.gmail_thread_id
        if not thread_id:
            return False

        try:
            reply_found = self._gmail_search_reply(thread_id, email.subject)
            if reply_found:
                email.status = EmailStatus.replied
                email.last_reply_at = datetime.utcnow()
                self.session.add(email)
                self.session.commit()

                company = self._get_company(email.fingerprint)
                reply_detected(company or "the company")
                return True
        except Exception as exc:
            _logger.debug("Reply check failed for thread %s: %s", thread_id, exc)
        return False

    def _gmail_search_reply(self, thread_id: str, subject: str) -> bool:
        try:
            import subprocess, sys
            result = subprocess.run(
                [sys.executable, "-c",
                 f"import mcp_gmail_helper; print(mcp_gmail_helper.has_reply('{thread_id}'))"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return result.stdout.strip().lower() == "true"
        except Exception:
            pass
        return False

    def _get_company(self, fingerprint: str) -> str:
        from ..storage.models import Opportunity
        from sqlmodel import select
        stmt = select(Opportunity).where(Opportunity.fingerprint == fingerprint)
        opp = self.session.exec(stmt).first()
        return opp.company if opp else "Unknown"


# ---------------------------------------------------------------------------
# LangGraph
# ---------------------------------------------------------------------------

def build_reply_watch_graph(db_session):
    g = StateGraph(ReplyWatchState)

    def poll_gmail_threads(state: ReplyWatchState) -> Dict[str, Any]:
        tracker = ReplyTracker(db_session)
        return {"new_replies": tracker.poll()}

    def notify_new_replies(state: ReplyWatchState) -> Dict[str, Any]:
        count = state.get("new_replies", 0)
        if count > 0:
            say(f"{count} new reply(s) detected — check your Gmail!")
        return {}

    g.add_node("poll_gmail_threads", poll_gmail_threads)
    g.add_node("notify_new_replies", notify_new_replies)
    g.set_entry_point("poll_gmail_threads")
    g.add_edge("poll_gmail_threads", "notify_new_replies")
    g.add_edge("notify_new_replies", END)

    return g.compile()

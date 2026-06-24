"""REVIEW graph — the interactive, owner-driven approval loop."""

from __future__ import annotations
import logging
import re
from datetime import datetime
from typing import Dict, Any, List

from langgraph.graph import StateGraph, END
from langgraph.types import interrupt

from .state import ReviewState
from ..narration.narrator import (
    say, heading, show_opportunity, show_opportunities_table, success
)
from ..storage.models import (
    Opportunity, OpportunityStatus, ForgottenFingerprint, Action, ActionType
)
from ..contacts.finder import ContactFinder
from ..contacts.startup import verify_startup
from ..email.drafter import draft_email
from ..email.gmail_client import save_draft_via_mcp
from sqlmodel import select

_logger = logging.getLogger("assistant.orchestrator.review")

_TERMINAL_STATUSES = {
    OpportunityStatus.approved,
    OpportunityStatus.drafted,
    OpportunityStatus.sent,
    OpportunityStatus.replied,
    OpportunityStatus.skipped_stored,
}


def build_review_graph(settings, db_session, profile_manager):
    """Build and return the compiled REVIEW LangGraph."""

    g = StateGraph(ReviewState)

    # --- Node: load_reviewable ---
    def load_reviewable(state: ReviewState) -> Dict[str, Any]:
        heading("Loading opportunities for your review...")

        forgotten_fps = {
            r.fingerprint for r in db_session.exec(select(ForgottenFingerprint)).all()
        }

        stmt = select(Opportunity).where(
            Opportunity.status.in_([
                OpportunityStatus.pending_review,
                OpportunityStatus.presented,
            ])
        )
        opps = db_session.exec(stmt).all()
        opps = [o for o in opps if o.fingerprint not in forgotten_fps]
        # Priority (2028-batch target-company internships) first, then by score.
        opps = sorted(
            opps,
            key=lambda o: (bool(getattr(o, "priority", False)), o.match_score or 0),
            reverse=True,
        )

        if not opps:
            say("You're all caught up — no new opportunities to review right now. Run harvest first.")
            return {"opportunities": [], "current_index": 0}

        say(f"Found {len(opps)} opportunity(s) to review.")
        opp_dicts = [_opp_to_dict(o) for o in opps]
        # Show the full table once here (this node does not interrupt, so it
        # won't be re-rendered on resume).
        show_opportunities_table(opp_dicts)
        return {
            "opportunities": opp_dicts,
            "current_index": 0,
        }

    # --- Router: did we load anything? ---
    def has_opportunities(state: ReviewState) -> str:
        return "select" if state.get("opportunities") else "done"

    # --- Node: select_opportunities ---
    def select_opportunities(state: ReviewState) -> Dict[str, Any]:
        """Show every opportunity in a table and let the owner pick a subset,
        so they don't have to walk through all of them one by one."""
        opps = state["opportunities"]

        answer = interrupt(
            f"Which of the {len(opps)} would you like to review? "
            "(e.g. 1,3,5-8 / top 10 / all / none)"
        )
        chosen = _parse_selection(str(answer), len(opps))

        if not chosen:
            say("No opportunities selected — nothing to review.")
            return {"opportunities": [], "current_index": 0}

        selected = [opps[i] for i in chosen]
        say(f"Selected {len(selected)} opportunity(s). Let's go through them.")
        return {"opportunities": selected, "current_index": 0}

    # --- Node: check_more (router) ---
    def check_more(state: ReviewState) -> str:
        opps = state.get("opportunities", [])
        idx = state.get("current_index", 0)
        return "present" if idx < len(opps) else "done"

    # --- Node: present_and_ask ---
    def present_and_ask(state: ReviewState) -> Dict[str, Any]:
        opps = state["opportunities"]
        idx = state["current_index"]
        opp = opps[idx]
        total = len(opps)

        # Show the opportunity
        show_opportunity(idx + 1, total, opp)

        # Mark as presented in DB
        fp = opp["fingerprint"]
        db_opp = db_session.exec(select(Opportunity).where(Opportunity.fingerprint == fp)).first()
        if db_opp and db_opp.status == OpportunityStatus.pending_review:
            db_opp.status = OpportunityStatus.presented
            db_session.add(db_opp)
            db_session.commit()

        # Interrupt: ask owner if they want to work here
        answer = interrupt(
            f"Do you want to work at {opp['company']} as {opp['role']}? (yes / no)"
        )
        return {"owner_answer": str(answer).strip().lower()}

    # --- Router: yes/no ---
    def route_answer(state: ReviewState) -> str:
        answer = (state.get("owner_answer") or "").lower()
        return "yes" if answer in ("yes", "y", "1") else "no"

    # --- Node: process_yes ---
    def process_yes(state: ReviewState) -> Dict[str, Any]:
        opp = state["opportunities"][state["current_index"]]
        fp = opp["fingerprint"]
        say(f"Great! Looking up HR contact(s) for {opp['company']}...")

        # Startup verification
        trust_verdict = "trusted"
        if opp.get("is_startup"):
            verdict, reasons = verify_startup(
                opp["company"], opp.get("role", ""),
                opp.get("source_urls", [""])[0] if opp.get("source_urls") else "",
            )
            trust_verdict = verdict
            if verdict == "low_trust":
                low_trust_answer = interrupt(
                    f"Warning: this startup has low trust signals ({'; '.join(reasons[:2])}). "
                    "Still proceed? (yes / no)"
                )
                if str(low_trust_answer).strip().lower() not in ("yes", "y"):
                    say("Skipping this one.")
                    return {
                        "current_index": state["current_index"] + 1,
                        "owner_answer": None,
                    }

        # Find HR contacts
        finder = ContactFinder(db_session, settings)
        contacts = finder.find_contacts(opp["company"], opp.get("is_startup", False))

        # Draft email
        p = profile_manager.get_profile() or profile_manager.load_or_parse()
        d = draft_email(settings, p, opp, contacts)

        # Show summary of draft
        say(f"\n--- DRAFT EMAIL PREVIEW ---")
        email_to = ', '.join(d.get('to_addrs', []))
        if email_to:
            say(f"To: {email_to}")
        else:
            # Show LinkedIn profile URLs if we have them but no email
            profile_links = [c.profile_url for c in contacts if c.profile_url]
            if profile_links:
                say(f"To: (email not found — reach out via LinkedIn: {profile_links[0]})")
            else:
                say(f"To: (no contact found — add email manually in Gmail)")
        say(f"Subject: {d['subject']}")
        say(f"\n{d['body'][:600]}{'...' if len(d['body']) > 600 else ''}")
        say("---")

        # Save to Gmail Drafts (pass contacts so a later push can enrich the email)
        save_draft_via_mcp(d, settings, db_session, fp, contacts=contacts)

        # Update DB
        db_opp = db_session.exec(select(Opportunity).where(Opportunity.fingerprint == fp)).first()
        if db_opp:
            db_opp.status = OpportunityStatus.drafted
            db_opp.trust_verdict = trust_verdict
            db_session.add(db_opp)
        action = Action(fingerprint=fp, action_type=ActionType.drafted, timestamp=datetime.utcnow(),
                        notes=f"contacts={len(contacts)}")
        db_session.add(action)
        db_session.commit()

        success(f"Done — email draft ready for {opp['company']}. Review it in Gmail before sending.")
        return {"current_index": state["current_index"] + 1, "owner_answer": None}

    # --- Node: ask_store_or_forget ---
    def ask_store_or_forget(state: ReviewState) -> Dict[str, Any]:
        opp = state["opportunities"][state["current_index"]]
        fp = opp["fingerprint"]

        answer = interrupt(
            f"Store '{opp['company']} — {opp['role']}' for later, or forget it entirely? "
            "(store / forget)"
        )
        sf_answer = str(answer).strip().lower()

        if sf_answer in ("forget", "f"):
            ff = ForgottenFingerprint(fingerprint=fp, hidden_at=datetime.utcnow())
            db_session.add(ff)
            action = Action(fingerprint=fp, action_type=ActionType.forgotten,
                            timestamp=datetime.utcnow())
            db_session.add(action)
            db_opp = db_session.exec(select(Opportunity).where(Opportunity.fingerprint == fp)).first()
            if db_opp:
                db_session.delete(db_opp)
            say("Forgotten — won't show this one again.")
        else:
            db_opp = db_session.exec(select(Opportunity).where(Opportunity.fingerprint == fp)).first()
            if db_opp:
                db_opp.status = OpportunityStatus.skipped_stored
                db_session.add(db_opp)
            action = Action(fingerprint=fp, action_type=ActionType.stored_for_later,
                            timestamp=datetime.utcnow())
            db_session.add(action)
            say("Stored — you can revisit this later.")

        db_session.commit()
        return {"current_index": state["current_index"] + 1, "owner_answer": None}

    # --- Node: end_of_list ---
    def end_of_list(state: ReviewState) -> Dict[str, Any]:
        say("That's all the opportunities for now. Good luck!")
        return {}

    # Wire the graph
    g.add_node("load_reviewable", load_reviewable)
    g.add_node("select_opportunities", select_opportunities)
    g.add_node("present_and_ask", present_and_ask)
    g.add_node("process_yes", process_yes)
    g.add_node("ask_store_or_forget", ask_store_or_forget)
    g.add_node("end_of_list", end_of_list)

    g.set_entry_point("load_reviewable")
    g.add_conditional_edges("load_reviewable", has_opportunities,
                            {"select": "select_opportunities", "done": "end_of_list"})
    g.add_conditional_edges("select_opportunities", check_more,
                            {"present": "present_and_ask", "done": "end_of_list"})
    g.add_conditional_edges("present_and_ask", route_answer,
                            {"yes": "process_yes", "no": "ask_store_or_forget"})
    g.add_conditional_edges(
        "process_yes",
        lambda s: "present" if s["current_index"] < len(s["opportunities"]) else "done",
        {"present": "present_and_ask", "done": "end_of_list"},
    )
    g.add_conditional_edges(
        "ask_store_or_forget",
        lambda s: "present" if s["current_index"] < len(s["opportunities"]) else "done",
        {"present": "present_and_ask", "done": "end_of_list"},
    )
    g.add_edge("end_of_list", END)

    from langgraph.checkpoint.memory import MemorySaver
    return g.compile(checkpointer=MemorySaver())


def _parse_selection(text: str, total: int) -> List[int]:
    """Parse an owner selection string into sorted, unique 0-based indices.

    Accepts: 'all', 'none'/'q'/'quit'/'' -> none, 'top N', and any mix of
    comma/space-separated single numbers and 'a-b' ranges (1-based input).
    Out-of-range values are ignored.
    """
    text = (text or "").strip().lower()
    if not text or text in ("none", "n", "no", "q", "quit", "exit"):
        return []
    if text in ("all", "a", "*"):
        return list(range(total))

    top_match = re.match(r"^top\s*(\d+)$", text)
    if top_match:
        n = max(0, min(int(top_match.group(1)), total))
        return list(range(n))

    chosen: set[int] = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        rng = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if lo > hi:
                lo, hi = hi, lo
            for i in range(lo, hi + 1):
                if 1 <= i <= total:
                    chosen.add(i - 1)
        elif part.isdigit():
            i = int(part)
            if 1 <= i <= total:
                chosen.add(i - 1)
    return sorted(chosen)


def _opp_to_dict(opp: Opportunity) -> dict:
    return {
        "fingerprint": opp.fingerprint,
        "company": opp.company,
        "role": opp.role,
        "location": opp.location,
        "remote": opp.remote,
        "stipend_inr": opp.stipend_inr,
        "stipend_stated": opp.stipend_stated,
        "ppo_flag": opp.ppo_flag,
        "fte_flag": opp.fte_flag,
        "duration": opp.duration,
        "start_timing": opp.start_timing,
        "is_startup": opp.is_startup,
        "trust_verdict": opp.trust_verdict,
        "match_score": opp.match_score,
        "match_explanation": opp.get_match_explanation(),
        "source_urls": opp.get_source_urls(),
        "priority": getattr(opp, "priority", False),
        "is_target_company": getattr(opp, "is_target_company", False),
        "batch_2028": getattr(opp, "batch_2028", False),
        "company_category": getattr(opp, "company_category", None),
        "stipend_label": (
            "stipend not stated" if not opp.stipend_stated
            else f"Rs.{opp.stipend_inr:,}/mo" if opp.stipend_inr
            else "stipend not stated"
        ),
    }

"""EmailDrafter — Groq-powered formal cold email agent."""

from __future__ import annotations
import logging
from typing import List

from langchain_core.messages import HumanMessage, SystemMessage

from ..llm.groq_client import get_llm
from ..storage.models import HRContact
from ..narration.narrator import say, warn

_logger = logging.getLogger("assistant.agents.email_drafter")

_SYSTEM_PROMPT = """You are an expert professional cold-email writer. Write a formal internship
application email from a 3rd-year engineering student (pre-final year) to an HR professional.

REQUIREMENTS:
- Standard formal cold-email-to-HR format
- Very formal tone, professional language
- Subject line on the first line, format: "Subject: <subject here>"
- Body: 3-4 paragraphs (introduction, why this company, skills/projects, call-to-action)
- Explicitly mention: batch (2027-28 / graduating 2028), college (Thapar Institute of Engineering & Technology)
- End with a professional email signature
- Do NOT use em-dashes (—); use commas or restructure
- Attach resume mentioned naturally in the closing paragraph
- Keep it concise — under 350 words

Return ONLY the email text (subject line first, then blank line, then body). No explanations."""


def draft_email(
    settings,
    profile,
    opportunity: dict,
    contacts: List[HRContact],
) -> dict:
    """Draft a formal cold email. Returns {subject, body, to_addrs}."""
    say(f"Drafting your email for {opportunity.get('company')}...")

    # Only address the email to VERIFIED recipients — never to a guessed or
    # unverified address (that is how mail went to wrong / non-existent people).
    verified_contacts = [c for c in contacts if c.email and c.verified]
    to_addrs = [c.email for c in verified_contacts]
    if not to_addrs:
        say("  No verified recipient email — drafting WITHOUT a To: address so "
            "you can add one manually after checking the company page.")

    # Greet a named, verified person if we have one; else a neutral salutation.
    named = next((c for c in verified_contacts if c.name), None)
    contact_name = named.name if named else "Hiring Manager"
    contact_designation = (named.designation if named and named.designation else "HR")

    profile_summary = (
        f"Name: {profile.full_name or settings.owner_name}\n"
        f"College: {settings.owner_college}\n"
        f"Batch: {profile.batch or settings.owner_batch} (graduating {profile.graduation_year or settings.owner_graduation_year})\n"
        f"Top skills: {', '.join((profile.skills or [])[:8])}\n"
        f"Domains: {', '.join(profile.domains or [])}\n"
        f"Notable projects: {'; '.join((profile.projects or [])[:3])}"
    )

    opp_summary = (
        f"Company: {opportunity.get('company')}\n"
        f"Role: {opportunity.get('role')}\n"
        f"Location: {opportunity.get('location', 'N/A')} "
        f"({'Remote' if opportunity.get('remote') else 'Onsite'})\n"
        f"Stipend: {opportunity.get('stipend_label', 'Not stated')}\n"
        f"Contact name: {contact_name}\n"
        f"Contact designation: {contact_designation}"
    )

    msgs = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Student profile:\n{profile_summary}\n\n"
                f"Opportunity details:\n{opp_summary}\n\n"
                f"Sender email: {settings.owner_email}"
            )
        ),
    ]

    # The Groq client already retries transient errors. If it still fails
    # (offline, outage, rate limit), don't throw away the work done so far —
    # fall back to a deterministic template so a usable draft is still queued.
    llm_fallback = False
    try:
        resp = get_llm().invoke(msgs)
        email_text = resp.content.strip()
        lines = email_text.split("\n", 2)
        subject_line = (
            lines[0].replace("Subject:", "").strip()
            if lines and lines[0].strip()
            else f"Internship Application - {opportunity.get('role')} at {opportunity.get('company')}"
        )
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else email_text
    except Exception as exc:  # noqa: BLE001 — any LLM failure -> template fallback
        _logger.warning("LLM drafting failed for %s (%s); using template fallback.",
                        opportunity.get("company"), exc)
        warn(f"  LLM unavailable - drafting a template email for "
             f"{opportunity.get('company')} (edit it before sending).")
        llm_fallback = True
        subject_line, body = _template_email(
            settings, profile, opportunity, contact_name
        )

    return {
        "subject": subject_line,
        "body": body,
        "to_addrs": to_addrs,
        "from_addr": settings.owner_email,
        "resume_path": profile.resume_path,
        "needs_manual_recipient": not to_addrs,
        "llm_fallback": llm_fallback,
    }


def _template_email(settings, profile, opportunity: dict, contact_name: str) -> tuple[str, str]:
    """Deterministic formal cold email used when the LLM is unavailable."""
    name = profile.full_name or settings.owner_name
    company = opportunity.get("company", "your company")
    role = opportunity.get("role", "an internship")
    batch = profile.batch or settings.owner_batch
    grad = profile.graduation_year or settings.owner_graduation_year
    skills = ", ".join((profile.skills or [])[:6]) or "software development"
    projects = (profile.projects or [])[:2]
    project_line = (
        " Notably, I have worked on " + "; ".join(projects) + "."
        if projects else ""
    )

    subject = f"Internship Application - {role} at {company}"
    body = (
        f"Dear {contact_name},\n\n"
        f"I am {name}, a pre-final year engineering student at "
        f"{settings.owner_college} (batch {batch}, graduating {grad}). "
        f"I am writing to express my strong interest in the {role} opportunity at {company}.\n\n"
        f"My background centres on {skills}, and I am keen to contribute these skills "
        f"to your team while learning from experienced professionals.{project_line}\n\n"
        f"I have attached my resume for your reference and would welcome the chance to "
        f"discuss how I can add value to {company}. Thank you for your time and consideration.\n\n"
        f"Warm regards,\n"
        f"{name}\n"
        f"{settings.owner_email}"
    )
    return subject, body

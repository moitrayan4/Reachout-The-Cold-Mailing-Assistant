"""OpportunityFinder — LangGraph harvest pipeline: collect → filter → score → persist."""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Dict, Any

from langgraph.graph import StateGraph, END

from ..orchestrator.state import HarvestState
from ..narration.narrator import say, harvest_summary, heading
from ..pipeline.normalizer import normalise
from ..pipeline.dedup import dedup
from ..pipeline.filter import EligibilityFilter
from ..pipeline.scorer import MatchScorer
from ..storage.models import Opportunity, OpportunityStatus, ForgottenFingerprint
from sqlmodel import select

_logger = logging.getLogger("assistant.agents.opportunity_finder")


def build_harvest_graph(settings, db_session, source_manager, profile_manager):
    """Build and return the compiled harvest LangGraph."""

    g = StateGraph(HarvestState)

    def load_or_refresh_profile(state: HarvestState) -> Dict[str, Any]:
        heading("Loading your profile...")
        profile = profile_manager.load_or_parse()
        return {"profile": {
            "full_name": profile.full_name,
            "skills": profile.skills,
            "domains": profile.domains,
            "keywords_for_search": profile.keywords_for_search,
            "preferred_roles": profile.preferred_roles,
            "batch": profile.batch,
            "graduation_year": profile.graduation_year,
        }}

    def acquire_all_sources(state: HarvestState) -> Dict[str, Any]:
        heading("Collecting internships from all sources...")
        profile = state["profile"]

        from src.roles import load_roles
        custom_roles = load_roles(settings)
        resume_keywords = (profile.get("keywords_for_search") or
                           profile.get("preferred_roles") or
                           ["software engineer intern", "machine learning intern"])
        seen = set()
        keywords = []
        for kw in custom_roles + resume_keywords:
            if kw.lower() not in seen:
                seen.add(kw.lower())
                keywords.append(kw)
        say(f"Searching {len(keywords)} role(s) ({len(custom_roles)} custom + {len(resume_keywords)} from resume): {', '.join(keywords[:6])}{'...' if len(keywords) > 6 else ''}")

        raw = source_manager.collect_all(keywords)
        return {"raw_postings": [
            {
                "company": r.company, "role": r.role, "source_url": r.source_url,
                "source_site": r.source_site, "location": r.location,
                "remote_text": r.remote_text, "stipend_text": r.stipend_text,
                "posted_date_text": r.posted_date_text, "eligibility_text": r.eligibility_text,
                "duration_text": r.duration_text, "start_date_text": r.start_date_text,
                "ppo_text": r.ppo_text, "fte_text": r.fte_text,
                "is_startup_hint": r.is_startup_hint,
                "extra": r.extra,
            }
            for r in raw
        ]}

    def normalize_and_fingerprint(state: HarvestState) -> Dict[str, Any]:
        say("Cleaning up and fingerprinting the listings...")
        from ..sources.base import RawPosting
        now = datetime.utcnow()
        normalised = []
        for raw in state["raw_postings"]:
            rp = RawPosting(**{k: raw.get(k) for k in RawPosting.__dataclass_fields__ if k in raw})
            normalised.append(normalise(rp, now))
        return {"normalised": normalised}

    def dedup_cross_site(state: HarvestState) -> Dict[str, Any]:
        say("Removing duplicates across sites...")
        return {"after_dedup": dedup(state["normalised"])}

    def eligibility_filter(state: HarvestState) -> Dict[str, Any]:
        say("Applying eligibility filters (batch, stipend, location, recency)...")
        elig_filter = EligibilityFilter(settings)
        passed, dropped = elig_filter.filter(state["after_dedup"])

        reasons: Dict[str, int] = {}
        for _, reason in dropped:
            cat = reason.split("_")[0] + "_" + reason.split("_")[1] if "_" in reason else reason
            reasons[cat] = reasons.get(cat, 0) + 1

        say(f"Filtered: {len(passed)} passed, {len(dropped)} dropped.")
        return {"after_filter": passed, "dropped_count": len(dropped), "drop_reasons": reasons}

    def match_and_score(state: HarvestState) -> Dict[str, Any]:
        say("Scoring each opportunity against your profile...")
        from ..profile.manager import OwnerProfile
        p = state["profile"]
        profile = OwnerProfile(**{k: p.get(k) for k in OwnerProfile.__dataclass_fields__ if k in p})
        scorer = MatchScorer(profile)
        return {"scored": scorer.score_all(state["after_filter"])}

    def persist(state: HarvestState) -> Dict[str, Any]:
        say("Saving results...")
        forgotten_fps = {
            r.fingerprint for r in db_session.exec(select(ForgottenFingerprint)).all()
        }

        saved = 0
        for rec in state["scored"]:
            fp = rec["fingerprint"]
            if fp in forgotten_fps:
                continue

            existing = db_session.exec(
                select(Opportunity).where(Opportunity.fingerprint == fp)
            ).first()

            if existing:
                changed = False
                if rec.get("match_score") and existing.match_score != rec["match_score"]:
                    existing.match_score = rec["match_score"]
                    existing.set_match_explanation(rec.get("match_explanation", []))
                    changed = True
                # Promote to priority if a target-company/2028 signal showed up.
                if rec.get("priority") and not existing.priority:
                    existing.priority = True
                    existing.is_target_company = True
                    existing.batch_2028 = existing.batch_2028 or rec.get("batch_2028", False)
                    existing.company_category = existing.company_category or rec.get("company_category")
                    changed = True
                if changed:
                    db_session.add(existing)
            else:
                opp = Opportunity(
                    fingerprint=fp,
                    company=rec["company"],
                    role=rec["role"],
                    location=rec.get("location"),
                    remote=rec.get("remote", False),
                    stipend_inr=rec.get("stipend_inr"),
                    stipend_stated=rec.get("stipend_stated", False),
                    ppo_flag=rec.get("ppo_flag", False),
                    fte_flag=rec.get("fte_flag", False),
                    duration=rec.get("duration"),
                    start_timing=rec.get("start_timing"),
                    posted_date=rec.get("posted_date"),
                    first_seen=rec.get("first_seen", datetime.utcnow()),
                    is_startup=rec.get("is_startup", False),
                    status=OpportunityStatus.pending_review,
                    match_score=rec.get("match_score"),
                    is_target_company=rec.get("is_target_company", False),
                    company_category=rec.get("company_category"),
                    batch_2028=rec.get("batch_2028", False),
                    priority=rec.get("priority", False),
                )
                opp.set_source_urls(rec.get("source_urls", []))
                opp.set_match_explanation(rec.get("match_explanation", []))
                db_session.add(opp)
                saved += 1

        db_session.commit()
        say(f"Saved {saved} new opportunity(s) to review.")
        return {}

    def narrate_summary(state: HarvestState) -> Dict[str, Any]:
        harvest_summary(
            len(state.get("raw_postings", [])),
            len(state.get("scored", [])),
            state.get("dropped_count", 0),
            state.get("drop_reasons", {}),
        )
        return {}

    g.add_node("load_or_refresh_profile", load_or_refresh_profile)
    g.add_node("acquire_all_sources", acquire_all_sources)
    g.add_node("normalize_and_fingerprint", normalize_and_fingerprint)
    g.add_node("dedup_cross_site", dedup_cross_site)
    g.add_node("eligibility_filter", eligibility_filter)
    g.add_node("match_and_score", match_and_score)
    g.add_node("persist", persist)
    g.add_node("narrate_summary", narrate_summary)

    g.set_entry_point("load_or_refresh_profile")
    g.add_edge("load_or_refresh_profile", "acquire_all_sources")
    g.add_edge("acquire_all_sources", "normalize_and_fingerprint")
    g.add_edge("normalize_and_fingerprint", "dedup_cross_site")
    g.add_edge("dedup_cross_site", "eligibility_filter")
    g.add_edge("eligibility_filter", "match_and_score")
    g.add_edge("match_and_score", "persist")
    g.add_edge("persist", "narrate_summary")
    g.add_edge("narrate_summary", END)

    return g.compile()

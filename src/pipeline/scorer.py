"""Match scoring plus human-readable explanations for each opportunity."""

from __future__ import annotations
import logging
from typing import List, Tuple

from ..profile.manager import OwnerProfile
from ..llm.groq_client import get_llm
from langchain_core.messages import HumanMessage, SystemMessage

_logger = logging.getLogger("assistant.pipeline.scorer")

# ---------------------------------------------------------------------------
# Embedding-based skill overlap
# ---------------------------------------------------------------------------

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception as exc:
            _logger.warning("sentence-transformers not available: %s", exc)
    return _embedder


def _cosine_sim(a, b) -> float:
    import numpy as np
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _skill_overlap_score(profile_skills: List[str], role_text: str) -> Tuple[int, List[str]]:
    """Return (0-50 score, matched_skills)."""
    model = _get_embedder()
    if not model or not profile_skills:
        # Fallback: keyword overlap
        role_lower = role_text.lower()
        matched = [s for s in profile_skills if s.lower() in role_lower]
        pct = min(len(matched) / max(len(profile_skills), 1), 1.0)
        return int(pct * 50), matched

    role_emb = model.encode(role_text)
    skill_text = " ".join(profile_skills)
    skill_emb = model.encode(skill_text)
    sim = _cosine_sim(role_emb, skill_emb)

    # Also check keyword matches for the explanation
    role_lower = role_text.lower()
    matched = [s for s in profile_skills if s.lower() in role_lower]
    return int(sim * 50), matched


# ---------------------------------------------------------------------------
# LLM explanation
# ---------------------------------------------------------------------------

_EXPLAIN_SYSTEM = """You are a concise internship-matching explainer.
Given a student's profile and an internship opportunity, produce a short JSON list
of 3-5 plain-English bullet points that explain WHY this role suits the student.
Include: skill match percentage, domain, eligibility, stipend, location, PPO if relevant.

Format: {"bullets": ["92% skill match (Python, PyTorch)", "Matches your AI/ML focus", ...]}
Return ONLY the JSON."""


def _llm_explain(profile: OwnerProfile, record: dict) -> List[str]:
    try:
        llm = get_llm()
        profile_summary = (
            f"Skills: {', '.join(profile.skills[:10])}\n"
            f"Domains: {', '.join(profile.domains)}\n"
            f"Batch: {profile.batch} ({profile.graduation_year})"
        )
        opp_summary = (
            f"Company: {record.get('company')}\n"
            f"Role: {record.get('role')}\n"
            f"Stipend: {record.get('stipend_label', 'not stated')}\n"
            f"Location: {record.get('location')} ({'remote' if record.get('remote') else 'onsite'})\n"
            f"PPO: {'yes' if record.get('ppo_flag') else 'no'}"
        )
        msgs = [
            SystemMessage(content=_EXPLAIN_SYSTEM),
            HumanMessage(content=f"Student profile:\n{profile_summary}\n\nOpportunity:\n{opp_summary}"),
        ]
        resp = llm.invoke(msgs)
        import json
        raw = resp.content.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        return data.get("bullets", [])
    except Exception as exc:
        _logger.warning("LLM explanation failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

class MatchScorer:
    def __init__(self, profile: OwnerProfile):
        self.profile = profile

    def score_all(self, records: List[dict]) -> List[dict]:
        """Add match_score and match_explanation to each record."""
        for rec in records:
            score, explanation = self._score_one(rec)
            rec["match_score"] = score
            rec["match_explanation"] = explanation
        # Priority (2028-batch target-company internships) always sort to the top,
        # then by match score.
        return sorted(
            records,
            key=lambda r: (bool(r.get("priority")), r.get("match_score", 0)),
            reverse=True,
        )

    def _score_one(self, rec: dict) -> Tuple[int, List[str]]:
        role_text = f"{rec.get('role', '')} {rec.get('eligibility_text', '')} {rec.get('company', '')}"

        # Skill overlap (0-50)
        skill_score, matched_skills = _skill_overlap_score(self.profile.skills, role_text)

        # Domain match (0-20)
        domain_score = 0
        role_lower = role_text.lower()
        for domain in self.profile.domains:
            if domain.lower() in role_lower:
                domain_score = 20
                break

        # Location preference (0-15)
        loc_score = 15 if rec.get("remote") else 10 if rec.get("is_india") else 0

        # Stipend attractiveness (0-15)
        stipend_inr = rec.get("stipend_inr") or 0
        if stipend_inr >= 50000:
            stip_score = 15
        elif stipend_inr >= 30000:
            stip_score = 10
        elif stipend_inr >= 20001 or not rec.get("stipend_stated"):
            stip_score = 5
        else:
            stip_score = 0

        total = min(skill_score + domain_score + loc_score + stip_score, 100)

        # Build quick explanation bullets
        bullets = []
        if rec.get("priority"):
            cat = rec.get("company_category")
            tag = f" ({cat})" if cat else ""
            if rec.get("batch_2028"):
                bullets.append(f"PRIORITY: internship at target company {rec.get('company')}{tag} "
                               "explicitly open to the 2028 batch")
            else:
                bullets.append(f"PRIORITY: internship at target company {rec.get('company')}{tag}")
        if matched_skills:
            bullets.append(f"{int(skill_score * 2)}% skill match ({', '.join(matched_skills[:4])})")
        if domain_score:
            bullets.append(f"Matches your {', '.join(self.profile.domains[:2])} focus")
        if rec.get("stipend_stated"):
            bullets.append(rec.get("stipend_label", ""))
        else:
            bullets.append("Stipend not stated (may be discussed after shortlisting)")
        if rec.get("ppo_flag"):
            bullets.append("PPO offered — potential full-time conversion")
        if rec.get("fte_flag"):
            bullets.append("Full-time opportunity")

        # Enhance with LLM if score is high enough (worth the token spend)
        if total >= 50:
            llm_bullets = _llm_explain(self.profile, rec)
            if llm_bullets:
                return total, llm_bullets

        return total, bullets

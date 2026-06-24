"""LangGraph state definitions for the harvest, review and reply-watch graphs."""

from __future__ import annotations
from typing import Any, Dict, List, Optional, TypedDict


class HarvestState(TypedDict):
    """State for the HARVEST graph (unattended daily run)."""
    run_id: str
    profile: Optional[Dict[str, Any]]
    raw_postings: List[Dict[str, Any]]
    normalised: List[Dict[str, Any]]
    after_dedup: List[Dict[str, Any]]
    after_filter: List[Dict[str, Any]]
    scored: List[Dict[str, Any]]
    dropped_count: int
    drop_reasons: Dict[str, int]
    error: Optional[str]


class ReviewState(TypedDict):
    """State for the REVIEW graph (interactive, owner-driven)."""
    opportunities: List[Dict[str, Any]]
    current_index: int
    pending_question: Optional[str]
    owner_answer: Optional[str]
    contacts_found: List[Dict[str, Any]]
    draft_saved: bool
    error: Optional[str]


class ReplyWatchState(TypedDict):
    """State for the REPLY-WATCH graph (scheduled, light)."""
    new_replies: int
    error: Optional[str]

"""SQLModel ORM models for every database table."""

from __future__ import annotations
import json
from datetime import datetime
from enum import Enum
from typing import Optional, List
from sqlmodel import Field, SQLModel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OpportunityStatus(str, Enum):
    pending_review = "pending_review"
    presented = "presented"
    approved = "approved"
    skipped_stored = "skipped_stored"
    drafted = "drafted"
    sent = "sent"
    replied = "replied"
    expired = "expired"


class ActionType(str, Enum):
    viewed = "viewed"
    approved = "approved"
    declined = "declined"
    stored_for_later = "stored_for_later"
    forgotten = "forgotten"
    drafted = "drafted"
    sent = "sent"
    reply_received = "reply_received"


class EmailStatus(str, Enum):
    draft = "draft"
    sent = "sent"
    replied = "replied"


class AcquisitionMethod(str, Enum):
    connector = "connector"
    apify = "apify"
    api = "api"
    rss = "rss"
    scraper = "scraper"
    unsupported = "unsupported"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class Opportunity(SQLModel, table=True):
    """One internship posting — post-normalisation, post-dedup."""
    __tablename__ = "opportunities"

    fingerprint: str = Field(primary_key=True)
    company: str
    role: str
    location: Optional[str] = None
    remote: bool = False
    stipend_inr: Optional[int] = None          # None = not stated
    stipend_stated: bool = False               # True if a number was found
    currency: str = Field(default="INR")
    ppo_flag: bool = False
    fte_flag: bool = False
    duration: Optional[str] = None
    start_timing: Optional[str] = None
    posted_date: Optional[datetime] = None
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    source_urls: str = Field(default="[]")     # JSON list of URLs
    is_startup: bool = False
    trust_verdict: Optional[str] = None        # trusted / low_trust / unverified
    raw_snapshot_ref: Optional[str] = None     # path to HTML snapshot
    match_score: Optional[int] = None
    match_explanation: Optional[str] = None    # JSON list of strings
    status: str = Field(default=OpportunityStatus.pending_review)

    # --- Target-company watcher flags ---
    is_target_company: bool = False            # came from a watched "dream" company
    company_category: Optional[str] = None     # e.g. "Big Tech", "Finance / Banking"
    batch_2028: bool = False                   # posting explicitly names the 2028 batch
    priority: bool = False                     # MUST-SHOW: pinned, bypasses stipend/recency

    def get_source_urls(self) -> List[str]:
        return json.loads(self.source_urls)

    def set_source_urls(self, urls: List[str]) -> None:
        self.source_urls = json.dumps(urls)

    def get_match_explanation(self) -> List[str]:
        if not self.match_explanation:
            return []
        return json.loads(self.match_explanation)

    def set_match_explanation(self, items: List[str]) -> None:
        self.match_explanation = json.dumps(items)


class ForgottenFingerprint(SQLModel, table=True):
    """Hidden fingerprints — permanently suppresses re-showing."""
    __tablename__ = "forgotten_fingerprints"

    fingerprint: str = Field(primary_key=True)
    hidden_at: datetime = Field(default_factory=datetime.utcnow)


class Action(SQLModel, table=True):
    """Audit log of every owner decision."""
    __tablename__ = "actions"

    id: Optional[int] = Field(default=None, primary_key=True)
    fingerprint: str = Field(foreign_key="opportunities.fingerprint")
    action_type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    notes: Optional[str] = None


class HRContact(SQLModel, table=True):
    """Discovered and verified HR / recruiter contacts."""
    __tablename__ = "hr_contacts"

    id: Optional[int] = Field(default=None, primary_key=True)
    company: str
    name: Optional[str] = None
    designation: Optional[str] = None
    profile_url: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None
    verified: bool = False
    verification_evidence: Optional[str] = None
    office_country: Optional[str] = None
    office_city: Optional[str] = None
    last_checked: Optional[datetime] = None


class Email(SQLModel, table=True):
    """Draft / sent email records linked to an opportunity."""
    __tablename__ = "emails"

    id: Optional[int] = Field(default=None, primary_key=True)
    fingerprint: str = Field(foreign_key="opportunities.fingerprint")
    draft_id: Optional[str] = None
    gmail_thread_id: Optional[str] = None
    to_addrs: str = Field(default="[]")        # JSON list
    subject: Optional[str] = None
    status: str = Field(default=EmailStatus.draft)
    created: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = None
    last_reply_at: Optional[datetime] = None

    def get_to_addrs(self) -> List[str]:
        return json.loads(self.to_addrs)

    def set_to_addrs(self, addrs: List[str]) -> None:
        self.to_addrs = json.dumps(addrs)


class SiteHealth(SQLModel, table=True):
    """Per-site health / circuit-breaker state."""
    __tablename__ = "site_health"

    site: str = Field(primary_key=True)
    method: str = Field(default=AcquisitionMethod.scraper)
    last_run: Optional[datetime] = None
    last_ok: Optional[datetime] = None
    drift_flag: bool = False
    consecutive_failures: int = 0


class Profile(SQLModel, table=True):
    """Parsed resume profile."""
    __tablename__ = "profile"

    id: Optional[int] = Field(default=None, primary_key=True)
    resume_hash: str
    parsed_json: str = Field(default="{}")     # JSON blob of Profile dataclass
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def get_parsed(self) -> dict:
        return json.loads(self.parsed_json)

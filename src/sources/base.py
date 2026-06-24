"""BaseSource interface and the RawPosting dataclass every adapter returns."""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class RawPosting:
    """Everything as-scraped, before normalisation."""
    company: str
    role: str
    source_url: str
    source_site: str

    location: Optional[str] = None
    remote_text: Optional[str] = None
    stipend_text: Optional[str] = None
    posted_date_text: Optional[str] = None
    eligibility_text: Optional[str] = None
    duration_text: Optional[str] = None
    start_date_text: Optional[str] = None
    ppo_text: Optional[str] = None
    fte_text: Optional[str] = None
    is_startup_hint: bool = False
    raw_html_snapshot: Optional[str] = None    # path to saved snapshot
    extra: dict = field(default_factory=dict)  # any site-specific extras


class BaseSource(ABC):
    """Every site adapter must implement this interface."""

    site_name: str = "unknown"

    @abstractmethod
    def search(self, keywords: List[str], location: str = "India") -> List[RawPosting]:
        """Search the site and return raw postings. No filtering here."""
        ...

    @abstractmethod
    def method(self) -> str:
        """Return the acquisition method: connector | apify | api | rss | scraper."""
        ...

    def is_available(self) -> bool:
        """Return True if this source can be used right now (e.g. credentials present)."""
        return True

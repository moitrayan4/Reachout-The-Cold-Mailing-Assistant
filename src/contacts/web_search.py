"""DuckDuckGo web search for contact discovery."""

from __future__ import annotations
import logging
from typing import List

_logger = logging.getLogger("assistant.contacts.web_search")
_TIMEOUT = 20


def search_web(query: str, max_results: int = 5) -> List[dict]:
    """Return list of {title, href, body} dicts from DuckDuckGo."""
    try:
        from ddgs import DDGS
        with DDGS(timeout=_TIMEOUT) as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        _logger.debug("Web search failed for '%s': %s", query, exc)
        return []


def search_web_recent(query: str, max_results: int = 8,
                      timelimit: str = "m") -> List[dict]:
    """Like :func:`search_web` but restricted to recent results.

    ``timelimit`` is DuckDuckGo's freshness window: 'd' (day), 'w' (week),
    'm' (month), 'y' (year). Used by the company web-watch so it only surfaces
    internships posted within the last month, never stale ones.
    """
    try:
        from ddgs import DDGS
        with DDGS(timeout=_TIMEOUT) as ddgs:
            return list(ddgs.text(query, max_results=max_results, timelimit=timelimit))
    except Exception as exc:
        _logger.debug("Recent web search failed for '%s': %s", query, exc)
        return []


def format_results(results: List[dict], max_items: int = 6) -> str:
    if not results:
        return "No results found."
    lines = []
    for r in results[:max_items]:
        lines.append(f"Title: {r.get('title', '')}")
        lines.append(f"URL: {r.get('href', '')}")
        lines.append(f"Snippet: {r.get('body', '')}")
        lines.append("")
    return "\n".join(lines)

"""FastMCP server exposing the LinkedIn tools.

This is the from-scratch counterpart to stickerdaniel/linkedin-mcp-server: same
idea (a stdio MCP server that drives a real browser), but built on this
project's own Patchright stealth stack and returning the exact shapes the
project's MCP client already consumes.

Tools:
    search_jobs(keywords, location, ...) -> {"job_ids": [...]}
    get_job_details(job_id)              -> {"url", "sections": {"job_posting"}}
    get_person_profile(linkedin_url)     -> {name, headline, ...}
    close_session()                      -> {"closed": true}
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from .driver import LinkedInSession, run_on_browser_thread
from . import scraper

_logger = logging.getLogger("linkedin_mcp.server")


def build_server() -> FastMCP:
    mcp = FastMCP("reachout-linkedin")

    @mcp.tool()
    def search_jobs(
        keywords: str,
        location: str = "India",
        job_type: str = "internship",
        experience_level: str = "internship,entry",
        date_posted: str = "past_month",
        max_pages: int = 1,
        sort_by: str = "date",
    ) -> dict:
        """Search LinkedIn jobs. Returns job_ids to feed into get_job_details.

        Args:
            keywords: space-separated search terms, e.g. "data science intern".
            location: city/country, e.g. "India" or "Bengaluru".
            job_type: internship | full-time | part-time | contract | temporary.
            experience_level: comma list of internship,entry,associate,mid-senior.
            date_posted: past_24h | past_week | past_month.
            max_pages: result pages to read (1 is usually enough).
        """
        def work():
            page = LinkedInSession.get().page
            return scraper.search_jobs(
                page, keywords=keywords, location=location, job_type=job_type,
                experience_level=experience_level, date_posted=date_posted,
                max_pages=max_pages,
            )
        return run_on_browser_thread(work)

    @mcp.tool()
    def get_job_details(job_id: str) -> dict:
        """Fetch one job posting's details by its numeric LinkedIn job id."""
        def work():
            page = LinkedInSession.get().page
            return scraper.get_job_details(page, job_id=job_id)
        return run_on_browser_thread(work)

    @mcp.tool()
    def get_person_profile(linkedin_url: str) -> dict:
        """Scrape a LinkedIn profile URL into name/headline/location/about."""
        def work():
            page = LinkedInSession.get().page
            return scraper.get_person_profile(page, linkedin_url=linkedin_url)
        return run_on_browser_thread(work)

    @mcp.tool()
    def close_session() -> dict:
        """Close the browser session (frees the profile lock)."""
        run_on_browser_thread(lambda: LinkedInSession.get().close())
        return {"closed": True}

    return mcp

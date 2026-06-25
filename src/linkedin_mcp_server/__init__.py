"""Reachout LinkedIn MCP server — a from-scratch equivalent of
stickerdaniel/linkedin-mcp-server, backed by this project's own Patchright
stealth browser.

It exposes the same tool contract the project's MCP client already calls
(``search_jobs`` → ``{job_ids: [...]}``, ``get_job_details`` →
``{url, sections: {job_posting}}``) plus ``get_person_profile`` and
``close_session``.

Run it:
    python -m src.linkedin_mcp_server --login          # one-time browser login
    python -m src.linkedin_mcp_server --transport stdio # how the client spawns it
"""

from .server import build_server

__all__ = ["build_server"]

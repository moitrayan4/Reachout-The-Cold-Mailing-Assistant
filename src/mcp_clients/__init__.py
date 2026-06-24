"""
MCP client factory — LinkedIn (stdio subprocess) + Hunter.io (remote HTTP).

LinkedIn:
  Your project spawns linkedin-mcp-server automatically via stdio.
  No separate terminal or port needed. Run `linkedin-mcp-server --login`
  once to save a browser session, then everything else is automatic.

Hunter.io:
  Your project connects directly to Hunter's hosted MCP server over
  streamable HTTP (no Claude in the loop), authenticating with your Hunter
  API key via the X-API-KEY header. Used as a verified-email booster.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any

_logger = logging.getLogger("assistant.mcp_clients")


class MCPClient:
    """
    Unified async MCP client supporting stdio (subprocess) and HTTP transports.

    Preferred — holds one connection for multiple calls:
        async with linkedin_client() as client:
            jobs   = await client.call("search_jobs", {...})
            detail = await client.call("get_job_details", {"job_id": "x"})

    One-shot sync:
        data = linkedin_client().call_sync("search_jobs", {...})
    """

    def __init__(
        self,
        *,
        # stdio transport: spawn a subprocess
        command: list[str] | None = None,
        # HTTP transport: connect to a URL
        url: str | None = None,
        headers: dict[str, str] | None = None,
        # extra env vars merged into subprocess environment (stdio only)
        env: dict[str, str] | None = None,
    ):
        if not command and not url:
            raise ValueError("Provide either command= (stdio) or url= (http)")
        self._command = command
        self._url = url
        self._headers = headers or {}
        self._env = {**os.environ, **(env or {})} if command else None
        self._session: Any = None
        self._stack: AsyncExitStack | None = None

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "MCPClient":
        from mcp import ClientSession

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        if self._command:
            from mcp.client.stdio import stdio_client, StdioServerParameters
            params = StdioServerParameters(
                command=self._command[0],
                args=self._command[1:],
                env=self._env,
            )
            r, w = await self._stack.enter_async_context(stdio_client(params))
        else:
            from mcp.client.streamable_http import streamablehttp_client
            r, w, _ = await self._stack.enter_async_context(
                streamablehttp_client(self._url, headers=self._headers)
            )

        self._session = await self._stack.enter_async_context(ClientSession(r, w))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._stack:
            await self._stack.__aexit__(*exc_info)

    # ------------------------------------------------------------------
    # Core call
    # ------------------------------------------------------------------

    async def call(self, tool: str, args: dict) -> dict:
        """Call a tool on the open session and return parsed JSON dict or {}."""
        if self._session is None:
            raise RuntimeError("Use MCPClient inside 'async with' before calling .call()")
        result = await self._session.call_tool(tool, args)
        for item in result.content or []:
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except Exception:
                    return {"raw": text}
        return {}

    async def list_tools(self) -> list[str]:
        """Return the names of the tools the open session exposes."""
        if self._session is None:
            raise RuntimeError("Use MCPClient inside 'async with' before calling .list_tools()")
        result = await self._session.list_tools()
        return [t.name for t in (result.tools or [])]

    # ------------------------------------------------------------------
    # Sync helpers
    # ------------------------------------------------------------------

    def call_sync(self, tool: str, args: dict) -> dict:
        """One-shot synchronous call — opens and closes a fresh session each time.

        Runs in a dedicated thread so asyncio.run() always gets a clean event loop,
        even when called from inside LangGraph's (or any other) running loop.
        BaseException (including CancelledError) is caught inside the thread so it
        can never propagate up and kill the LangGraph node.
        """
        import concurrent.futures

        command, url, headers, env = self._command, self._url, self._headers, self._env

        async def _run() -> dict:
            async with MCPClient(command=command, url=url, headers=headers, env=env) as c:
                return await c.call(tool, args)

        def _thread_target() -> dict:
            try:
                return asyncio.run(_run())
            except BaseException as exc:
                _logger.debug("MCP call '%s' in thread failed: %s", tool, exc)
                return {}

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(_thread_target).result()
        except Exception as exc:
            _logger.debug("MCP call '%s' (outer) failed: %s", tool, exc)
            return {}

    def is_available(self) -> bool:
        """
        Quick availability check without opening a connection.
        - stdio: True if the command binary is on PATH.
        - http:  True always (the server is remote; reachability checked on connect).
        """
        if self._command:
            return shutil.which(self._command[0]) is not None
        return True


# ------------------------------------------------------------------
# Factory functions
# ------------------------------------------------------------------

def linkedin_client() -> MCPClient:
    """
    MCP client for LinkedIn.

    Spawns linkedin-mcp-server as a subprocess via stdio — no port, no
    manual startup. First-time setup: run `linkedin-mcp-server --login`
    once to save a browser session to ~/.linkedin-mcp/profile.
    """
    profile_dir = os.getenv("LINKEDIN_CHROME_PROFILE_PATH") or None
    env_overrides: dict[str, str] = {"HEADLESS": "1"}
    if profile_dir:
        env_overrides["USER_DATA_DIR"] = profile_dir
    return MCPClient(
        command=["linkedin-mcp-server", "--transport", "stdio", "--log-level", "WARNING"],
        env=env_overrides,
    )


def call_linkedin_mcp(tool: str, args: dict) -> dict:
    """One-shot sync call to the LinkedIn MCP server. Spawns and kills the subprocess automatically."""
    return linkedin_client().call_sync(tool, args)


HUNTER_MCP_URL = "https://mcp.hunter.io/mcp"


def hunter_client() -> MCPClient | None:
    """
    MCP client for Hunter.io's remote server.

    Connects over streamable HTTP to https://mcp.hunter.io/mcp, authenticating
    with HUNTER_API_KEY (X-API-KEY header). Returns None when no key is set so
    callers can transparently skip Hunter.
    """
    key = os.getenv("HUNTER_API_KEY", "").strip()
    if not key:
        return None
    return MCPClient(url=HUNTER_MCP_URL, headers={"X-API-KEY": key})


def call_hunter_mcp(tool: str, args: dict) -> dict:
    """One-shot sync call to the Hunter MCP server. Returns {} when disabled or on error."""
    client = hunter_client()
    if client is None:
        return {}
    return client.call_sync(tool, args)


def hunter_list_tools() -> list[str]:
    """Return Hunter's advertised MCP tool names (for setup verification). [] when disabled."""
    client = hunter_client()
    if client is None:
        return []

    async def _run() -> list[str]:
        async with client as c:  # type: ignore[union-attr]
            return await c.list_tools()

    try:
        return asyncio.run(_run())
    except Exception as exc:
        _logger.debug("hunter_list_tools failed: %s", exc)
        return []

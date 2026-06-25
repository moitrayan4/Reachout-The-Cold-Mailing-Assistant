"""CLI entry point for the Reachout LinkedIn MCP server.

Mirrors the surface of stickerdaniel/linkedin-mcp-server so it's a drop-in:

    python -m src.linkedin_mcp_server --login              # one-time browser login
    python -m src.linkedin_mcp_server --transport stdio     # how the client spawns it
    python -m src.linkedin_mcp_server --transport sse --port 3000

--login opens a headed browser at LinkedIn's sign-in page and waits for you to
complete it (including any 2FA / captcha), then persists the session to the
profile dir so every later run is automatic and headless.
"""

from __future__ import annotations

import logging
import sys
import time

import click

from .driver import LinkedInSession
from .server import build_server


def _setup_logging(level: str) -> None:
    # MCP stdio uses stdout for the protocol — logs MUST go to stderr only.
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _do_login() -> int:
    """Open a headed window, let the user sign in, persist the session."""
    click.echo("Opening a browser for LinkedIn login...", err=True)
    session = LinkedInSession(headless=False)
    page = session.page
    page.goto("https://www.linkedin.com/login")
    click.echo(
        "Sign in to LinkedIn in the opened window (complete any 2FA/captcha).\n"
        "Waiting up to 5 minutes for you to reach your feed...",
        err=True,
    )
    deadline = time.time() + 300
    ok = False
    while time.time() < deadline:
        url = (page.url or "").lower()
        if "/feed" in url or ("linkedin.com" in url and "/login" not in url
                              and "/checkpoint" not in url and "/authwall" not in url):
            ok = True
            break
        time.sleep(2)
    if ok:
        # Touch the feed once so cookies fully settle before we close.
        try:
            page.goto("https://www.linkedin.com/feed/")
            time.sleep(3)
        except Exception:  # noqa: BLE001
            pass
        click.echo(f"Login saved to: {session.profile_dir}", err=True)
    else:
        click.echo("Timed out waiting for login. Re-run --login to retry.", err=True)
    session.close()
    return 0 if ok else 1


@click.command()
@click.option("--login", "do_login", is_flag=True,
              help="Open a browser to sign in to LinkedIn and save the session.")
@click.option("--capture-session", "capture_cdp", default="",
              help="Capture the LinkedIn login from a running browser at this "
                   "CDP url (e.g. http://localhost:9222) into a storage_state "
                   "file for headless harvests.")
@click.option("--transport", default="stdio",
              type=click.Choice(["stdio", "sse"]),
              help="MCP transport (stdio is what the project's client spawns).")
@click.option("--host", default="127.0.0.1", help="Host for sse transport.")
@click.option("--port", default=3000, type=int, help="Port for sse transport.")
@click.option("--log-level", default="WARNING",
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"],
                                case_sensitive=False))
def main(do_login: bool, capture_cdp: str, transport: str, host: str,
         port: int, log_level: str) -> None:
    _setup_logging(log_level)

    if capture_cdp:
        from .driver import capture_session
        path = capture_session(capture_cdp)
        click.echo(f"Saved LinkedIn session to: {path}", err=True)
        sys.exit(0)

    if do_login:
        sys.exit(_do_login())

    server = build_server()
    if transport == "sse":
        server.settings.host = host
        server.settings.port = port
        server.run(transport="sse")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()

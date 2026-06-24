"""Plain-English narration layer — wraps all pipeline steps.

Uses ASCII-only status markers so output works on any Windows console encoding.
"""

from __future__ import annotations
import io
import logging
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_logger = logging.getLogger("assistant.debug")

# On Windows, reconfigure stdout to UTF-8 so Rich can print any Unicode safely.
# legacy_windows=False tells Rich to use ANSI (supported on Win 10+/Win 11) instead
# of the CP1252-limited legacy console API.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, io.UnsupportedOperation):
        pass

console = Console(highlight=False, legacy_windows=False)


def _log(msg: str) -> None:
    _logger.debug(msg)


# ---------------------------------------------------------------------------
# Public API  (ASCII-only status markers — safe on all Windows encodings)
# ---------------------------------------------------------------------------

def say(message: str) -> None:
    """Plain-English status to the owner's screen."""
    console.print(f"[cyan]>[/cyan] {message}")


def heading(title: str) -> None:
    console.print(Panel(f"[bold]{title}[/bold]", expand=False, style="blue"))


def success(message: str) -> None:
    console.print(f"[green]OK[/green] {message}")


def warn(message: str) -> None:
    console.print(f"[yellow]![/yellow]  {message}")


def error(message: str) -> None:
    console.print(f"[red]ERR[/red] {message}")


def ask(question: str) -> str:
    """Ask the owner a question and return their answer."""
    console.print()
    answer = console.input(f"[bold magenta]?[/bold magenta] {question} ").strip()
    console.print()
    return answer


def show_opportunity(idx: int, total: int, opp: dict) -> None:
    """Present one opportunity to the owner with its fit explanation."""
    score = opp.get("match_score", "N/A")
    company = opp.get("company", "Unknown")
    role = opp.get("role", "Unknown")
    location = opp.get("location", "N/A")
    stipend = opp.get("stipend_label", "stipend not stated")
    explanation = opp.get("match_explanation", [])
    remote = " (Remote)" if opp.get("remote") else ""
    source_urls = opp.get("source_urls") or []

    priority = opp.get("priority")
    star = "* " if priority else ""
    title = f"[{idx}/{total}] {star}{company} -- {role}"
    body = Text()
    if priority:
        cat = opp.get("company_category")
        batch = " (2028 batch)" if opp.get("batch_2028") else ""
        label = f"PRIORITY -- target company{batch}"
        if cat:
            label += f" -- {cat}"
        body.append(f"{label}\n", style="bold yellow")
    body.append(f"Location: {location}{remote}\n", style="dim")
    body.append(f"Stipend: {stipend}\n", style="dim")
    body.append(f"Match score: {score}/100\n", style="bold")
    if source_urls:
        body.append(f"Apply: ", style="bold")
        body.append(f"{source_urls[0]}\n", style="underline cyan")
        for url in source_urls[1:]:
            body.append(f"       {url}\n", style="underline cyan")
    body.append("\n")
    for item in explanation:
        body.append(f"  * {item}\n", style="italic")

    console.print(Panel(body, title=title, border_style="cyan"))


def show_opportunities_table(opps: list) -> None:
    """Show all reviewable opportunities at a glance so the owner can pick."""
    table = Table(
        title=f"{len(opps)} opportunities ready for review",
        title_style="bold cyan",
        header_style="bold",
        border_style="cyan",
        show_lines=False,
        expand=True,
    )
    table.add_column("#", justify="right", style="bold", no_wrap=True)
    table.add_column("!", justify="center", no_wrap=True)
    table.add_column("Score", justify="right", no_wrap=True)
    table.add_column("Company", style="white", overflow="fold")
    table.add_column("Role", style="white", overflow="fold")
    table.add_column("Location", style="dim", overflow="fold")
    table.add_column("Stipend", style="dim", no_wrap=True)

    for i, opp in enumerate(opps, start=1):
        score = opp.get("match_score")
        score_val = score if isinstance(score, (int, float)) else 0
        score_style = (
            "bold green" if score_val >= 75
            else "yellow" if score_val >= 50
            else "red"
        )
        location = opp.get("location") or "N/A"
        if opp.get("remote"):
            location = f"{location} (Remote)"
        flag = "[bold yellow]*[/bold yellow]" if opp.get("priority") else ""
        table.add_row(
            str(i),
            flag,
            f"[{score_style}]{score_val}[/{score_style}]",
            opp.get("company", "Unknown"),
            opp.get("role", "Unknown"),
            location,
            opp.get("stipend_label", "not stated"),
        )

    console.print(table)
    console.print("[dim]([/dim][bold yellow]*[/bold yellow][dim] = PRIORITY: a 2028-batch "
                  "internship at one of your target companies — surfaced no matter what.)[/dim]")
    console.print(
        "[dim]Select with: numbers (e.g. [/dim][bold]1,3,5[/bold][dim]), "
        "ranges ([/dim][bold]1-8[/bold][dim]), [/dim][bold]top 10[/bold][dim], "
        "[/dim][bold]all[/bold][dim], or [/dim][bold]none[/bold][dim] to quit.[/dim]"
    )


_DROP_REASON_LABELS = {
    "date_unknown": "no posted-date found (drop-unknown-date is ON)",
    "too_old": "older than the max posting age",
    "fail_stipend": "stated stipend below your minimum",
    "fail_batch": "batch/experience not eligible",
    "fail_location": "outside India and not remote",
}


def harvest_summary(total_found: int, kept: int, dropped: int, breakdown: dict) -> None:
    say(f"Found {total_found} listing(s) across all sources.")
    say(f"Kept {kept} after filtering (dropped {dropped}).")
    if breakdown:
        say("Why the rest were dropped:")
        for reason, count in sorted(breakdown.items(), key=lambda kv: -kv[1]):
            label = _DROP_REASON_LABELS.get(reason, reason.replace("_", " "))
            console.print(f"    [dim]- {count:>3}  {label}[/dim]")
            _log(f"  Dropped -- {reason}: {count}")
    if kept == 0:
        say("Nothing new to review today. Check back tomorrow!")
    else:
        say(f"Ready to review {kept} opportunities when you're ready. Run: python -m src review")


def site_status(site: str, method: str, count: int) -> None:
    if count >= 0:
        say(f"Checked {site} ({method}): found {count} relevant listing(s).")
    else:
        warn(f"Skipped {site} -- it seems to have changed its layout or is unavailable today.")


def drift_alert(site: str) -> None:
    warn(
        f"{site}'s page layout seems to have changed or returned no results. "
        "I skipped it today and it needs a quick selector fix."
    )


def contact_found(name: str, designation: str, company: str, verified: bool) -> None:
    status = "verified" if verified else "unverified"
    say(f"Found HR contact: {name} ({designation}) at {company} -- {status}.")


def draft_saved(subject: str, draft_id: str) -> None:
    success(f'Email draft saved in your Gmail Drafts -- subject: "{subject}"')
    _log(f"Gmail draft_id={draft_id}")


def reply_detected(company: str) -> None:
    success(f"Good news -- someone from {company} replied to your email!")

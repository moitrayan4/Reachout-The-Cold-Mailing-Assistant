"""CLI entrypoint for Reachout: The Cold Mailing Assistant.

Usage:
  python -m src              # show help
  python -m src harvest      # run the daily harvest now
  python -m src review       # interactive review session
  python -m src watch        # poll Gmail for replies
  python -m src schedule     # start the background daily scheduler
  python -m src status       # show DB stats and site health
"""

from __future__ import annotations
import logging
import sys
import uuid
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "debug.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def _bootstrap():
    """Initialise all shared objects (settings, DB, LLM, sources, profile)."""
    from config.settings import get_settings
    settings = get_settings()
    _setup_logging(settings.log_dir)

    from src.storage.database import init_db, open_session
    init_db(settings.db_path)
    session = open_session(settings.db_path)

    from src.llm.groq_client import init_llm
    init_llm(settings.groq_api_key, settings.groq_model, settings.llm_cache_db)

    from src.profile.manager import ProfileManager
    profile_mgr = ProfileManager(settings.resume_dir, session, settings)
    profile_mgr.start_watcher()

    sources = _build_sources(settings)

    from src.sources.manager import SourceManager
    source_mgr = SourceManager(sources, session, settings.db_path)

    return settings, session, profile_mgr, source_mgr


def _build_sources(settings) -> dict:
    """Instantiate all enabled source adapters based on config.

    Delegates to :mod:`src.sources.factory` so the main process and the
    per-source worker subprocesses build adapters identically.
    """
    from src.sources.factory import build_all_sources
    return build_all_sources(settings)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """Reachout: The Cold Mailing Assistant — Internship Hunter & Outreach Agent."""
    pass


# ---------------------------------------------------------------------------
# roles — manage target internship roles
# ---------------------------------------------------------------------------

@cli.group()
def roles():
    """Add, remove, or list the internship roles you want to search for."""
    pass


@roles.command("list")
def roles_list():
    """Show your current target roles."""
    from config.settings import get_settings
    from src.roles import load_roles
    settings = get_settings()
    current = load_roles(settings)
    if not current:
        console.print("[yellow]No custom roles set — harvest uses your resume keywords.[/yellow]")
    else:
        console.print("[bold]Target roles:[/bold]")
        for i, r in enumerate(current, 1):
            console.print(f"  {i}. {r}")


@roles.command("add")
@click.argument("role")
def roles_add(role: str):
    """Add a role to search for (e.g. 'Data Science', 'ML Engineer')."""
    from config.settings import get_settings
    from src.roles import add_role
    settings = get_settings()
    current = add_role(settings, role)
    console.print(f"[green]Added.[/green] Current roles: {', '.join(current)}")


@roles.command("remove")
@click.argument("role")
def roles_remove(role: str):
    """Remove a role from the search list."""
    from config.settings import get_settings
    from src.roles import remove_role
    settings = get_settings()
    current = remove_role(settings, role)
    if current:
        console.print(f"[green]Removed.[/green] Remaining: {', '.join(current)}")
    else:
        console.print("[yellow]No custom roles left — harvest will use resume keywords.[/yellow]")


@roles.command("set")
@click.argument("roles_list", nargs=-1, required=True)
def roles_set(roles_list):
    """Replace ALL target roles at once.

    Example: python -m src roles set "ML Engineer" "Data Analyst" "AI Research"
    """
    from config.settings import get_settings
    from src.roles import set_roles
    settings = get_settings()
    current = set_roles(settings, list(roles_list))
    console.print(f"[green]Set {len(current)} role(s):[/green] {', '.join(current)}")


@roles.command("clear")
def roles_clear():
    """Clear all custom roles (harvest reverts to resume keywords)."""
    from config.settings import get_settings
    from src.roles import clear_roles
    settings = get_settings()
    clear_roles(settings)
    console.print("[yellow]Cleared. Harvest will now use keywords from your resume.[/yellow]")


# ---------------------------------------------------------------------------
# companies — manage the watched "dream" companies
# ---------------------------------------------------------------------------

@cli.group()
def companies():
    """List/add/remove the target companies whose India career sites are watched
    for internships open to your 2028 batch (surfaced as PRIORITY)."""
    pass


@companies.command("list")
@click.option("--category", default=None, help="Filter by category substring.")
def companies_list(category):
    """Show the watched target companies, grouped by category."""
    from config.settings import get_settings
    from src.companies import load_config
    settings = get_settings()
    cfg = load_config(settings)
    comps = cfg.companies
    if category:
        comps = [c for c in comps if category.lower() in c.category.lower()]
    if not comps:
        console.print("[yellow]No target companies configured.[/yellow]")
        return

    console.print(f"[bold]Watching {len(comps)} company(s) for {cfg.target_grad_year}-batch "
                  f"internships (India offices):[/bold]\n")
    by_cat: dict[str, list] = {}
    for c in comps:
        by_cat.setdefault(c.category, []).append(c)
    for cat in sorted(by_cat):
        console.print(f"[bold cyan]{cat}[/bold cyan]")
        for c in by_cat[cat]:
            url = f"  ->  {c.careers_url}" if c.careers_url else ""
            console.print(f"  - {c.name}{url}")
        console.print()


@companies.command("add")
@click.argument("name")
@click.option("--category", default="Custom", help="Category label.")
@click.option("--url", "careers_url", default=None, help="Known India careers/ATS URL.")
@click.option("--domain", default=None, help="Official domain (improves URL resolution).")
def companies_add(name, category, careers_url, domain):
    """Add a company to the watch list."""
    from config.settings import get_settings
    from src.companies import add_company
    settings = get_settings()
    comps = add_company(settings, name, category=category, careers_url=careers_url, domain=domain)
    console.print(f"[green]Added '{name}'.[/green] Now watching {len(comps)} company(s).")


@companies.command("remove")
@click.argument("name")
def companies_remove(name):
    """Remove a company from the watch list."""
    from config.settings import get_settings
    from src.companies import remove_company
    settings = get_settings()
    comps = remove_company(settings, name)
    console.print(f"[green]Removed '{name}'.[/green] Now watching {len(comps)} company(s).")


@cli.command("watch-companies")
def watch_companies():
    """Scan ONLY the target companies' India career sites for 2028-batch internships."""
    from src.narration.narrator import heading, say
    heading("COMPANY WATCH — scanning target-company career sites")
    settings, session, profile_mgr, source_mgr = _bootstrap()

    # Restrict the source manager to just the company-careers watcher.
    company_only = {k: v for k, v in source_mgr.sources.items() if k == "company_careers"}
    if not company_only:
        say("The company-careers watcher is disabled (company_watch_enabled / sites.yaml).")
        return
    from src.sources.manager import SourceManager
    watch_mgr = SourceManager(company_only, session, settings.db_path)

    from src.orchestrator.harvest import build_harvest_graph
    graph = build_harvest_graph(settings, session, watch_mgr, profile_mgr)
    graph.invoke({
        "run_id": str(uuid.uuid4())[:8], "profile": None,
        "raw_postings": [], "normalised": [], "after_dedup": [],
        "after_filter": [], "scored": [], "dropped_count": 0,
        "drop_reasons": {}, "error": None,
    })


@cli.command()
def harvest():
    """Run the daily harvest (collect, filter, score, save)."""
    from src.narration.narrator import heading
    heading("HARVEST MODE — collecting fresh internships")
    settings, session, profile_mgr, source_mgr = _bootstrap()

    from src.orchestrator.harvest import build_harvest_graph
    graph = build_harvest_graph(settings, session, source_mgr, profile_mgr)
    run_id = str(uuid.uuid4())[:8]
    initial_state = {
        "run_id": run_id,
        "profile": None,
        "raw_postings": [],
        "normalised": [],
        "after_dedup": [],
        "after_filter": [],
        "scored": [],
        "dropped_count": 0,
        "drop_reasons": {},
        "error": None,
    }
    graph.invoke(initial_state)


@cli.command()
def review():
    """Interactive review session — approve or skip each opportunity."""
    from src.narration.narrator import heading, say
    from langgraph.types import Command
    heading("REVIEW MODE — your internship opportunities")
    settings, session, profile_mgr, source_mgr = _bootstrap()

    from src.orchestrator.review import build_review_graph
    graph = build_review_graph(settings, session, profile_mgr)

    thread = {"configurable": {"thread_id": "review-main"}}
    initial_state = {
        "opportunities": [],
        "current_index": 0,
        "pending_question": None,
        "owner_answer": None,
        "contacts_found": [],
        "draft_saved": False,
        "error": None,
    }

    # First invocation
    graph.invoke(initial_state, thread)

    # Resume loop — handle each interrupt until graph finishes
    while True:
        snapshot = graph.get_state(thread)
        if not snapshot.next:
            break  # graph finished

        # Collect pending interrupts
        pending = []
        for task in snapshot.tasks:
            for iv in (task.interrupts or []):
                pending.append(iv)

        if not pending:
            break

        # Each interrupt is a question string
        iv = pending[0]
        question = iv.value if isinstance(iv.value, str) else str(iv.value)
        answer = console.input(f"\n[bold magenta]?[/bold magenta] {question} ").strip()

        # Resume with the answer using Command(resume=...)
        graph.invoke(Command(resume=answer), thread)


@cli.command()
def watch():
    """Poll Gmail for replies to your outreach emails."""
    from src.narration.narrator import heading
    heading("REPLY WATCH — checking for responses")
    settings, session, profile_mgr, source_mgr = _bootstrap()

    from src.orchestrator.reply_watch import build_reply_watch_graph
    graph = build_reply_watch_graph(session)
    graph.invoke({"new_replies": 0, "error": None})


@cli.command()
def schedule():
    """Start the background daily scheduler (harvest at 08:00 IST + reply poll)."""
    from src.narration.narrator import say, heading
    heading("SCHEDULER — starting background daily harvest")

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    settings, session, profile_mgr, source_mgr = _bootstrap()

    sched = BlockingScheduler(timezone=pytz.timezone("Asia/Kolkata"))

    harvest_time = settings.harvest_time_ist.split(":")
    hour, minute = int(harvest_time[0]), int(harvest_time[1])

    def run_harvest():
        from src.narration.narrator import heading as h
        h("Scheduled HARVEST starting...")
        from src.orchestrator.harvest import build_harvest_graph
        graph = build_harvest_graph(settings, session, source_mgr, profile_mgr)
        graph.invoke({
            "run_id": str(uuid.uuid4())[:8], "profile": None,
            "raw_postings": [], "normalised": [], "after_dedup": [],
            "after_filter": [], "scored": [], "dropped_count": 0,
            "drop_reasons": {}, "error": None,
        })

    def run_reply_watch():
        from src.orchestrator.reply_watch import build_reply_watch_graph
        graph = build_reply_watch_graph(session)
        graph.invoke({"new_replies": 0, "error": None})

    sched.add_job(run_harvest, CronTrigger(hour=hour, minute=minute), id="daily_harvest")
    sched.add_job(run_reply_watch, CronTrigger(hour=hour + 1, minute=0), id="reply_watch")

    say(f"Daily harvest scheduled for {settings.harvest_time_ist} IST. Press Ctrl+C to stop.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        say("Scheduler stopped.")


@cli.command("push-drafts")
def push_drafts():
    """Enrich pending drafts with verified emails and push them to Gmail as IMAP drafts."""
    from src.narration.narrator import heading
    heading("PUSH DRAFTS — enriching with verified emails")
    from config.settings import get_settings
    settings = get_settings()
    _setup_logging(settings.log_dir)

    from src.storage.database import init_db, open_session
    init_db(settings.db_path)
    session = open_session(settings.db_path)

    from src.email.gmail_client import push_pending_drafts
    push_pending_drafts(settings, session)


@cli.command("gmail-auth")
def gmail_auth():
    """Authorize Gmail draft creation via the Gmail API (one-time browser consent)."""
    from src.narration.narrator import heading, success, error, say
    heading("GMAIL AUTH — authorize draft creation via the Gmail API")
    from config.settings import get_settings
    settings = get_settings()
    _setup_logging(settings.log_dir)

    from src.email import gmail_api
    if gmail_api.is_authorized(settings):
        success("Already authorized — Gmail API token is valid.")
        return
    say("A browser window will open for Google sign-in and consent...")
    try:
        token_path = gmail_api.authorize(settings)
        success(f"Authorized. Token saved to {token_path}")
    except FileNotFoundError as exc:
        error(str(exc))
    except Exception as exc:  # noqa: BLE001
        error(f"Authorization failed: {exc}")


@cli.command()
def status():
    """Show DB stats and site health."""
    settings, session, profile_mgr, source_mgr = _bootstrap()

    from sqlmodel import select
    from src.storage.models import Opportunity, SiteHealth, Email

    # Opportunity stats
    table = Table(title="Opportunity Status")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for status in ["pending_review", "presented", "approved", "drafted", "sent", "replied", "skipped_stored"]:
        stmt = select(Opportunity).where(Opportunity.status == status)
        count = len(session.exec(stmt).all())
        table.add_row(status, str(count))
    console.print(table)

    # Site health
    table2 = Table(title="Site Health")
    table2.add_column("Site")
    table2.add_column("Method")
    table2.add_column("Last OK")
    table2.add_column("Failures", justify="right")
    table2.add_column("Drift")
    for health in session.exec(select(SiteHealth)).all():
        last_ok = health.last_ok.strftime("%Y-%m-%d %H:%M") if health.last_ok else "never"
        table2.add_row(
            health.site, health.method, last_ok,
            str(health.consecutive_failures),
            "YES" if health.drift_flag else "no",
        )
    console.print(table2)

    # Email stats
    emails = session.exec(select(Email)).all()
    console.print(f"\nEmails: {len(emails)} total | "
                  f"{sum(1 for e in emails if e.status == 'sent')} sent | "
                  f"{sum(1 for e in emails if e.status == 'replied')} replied")


def main():
    if len(sys.argv) == 1:
        cli(["--help"])
    else:
        cli()


if __name__ == "__main__":
    main()

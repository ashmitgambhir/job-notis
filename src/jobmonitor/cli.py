"""Command line interface.

    jobmon run                  # the daemon: scan forever on a schedule
    jobmon scan                 # one cycle, then exit (for cron / CI)
    jobmon discover             # detect each company's careers platform
    jobmon doctor               # check config, endpoints and channels
    jobmon test-notify          # send yourself a sample alert
    jobmon list / stats         # what has been found
    jobmon export               # csv / json / rss
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

import click
import structlog

from . import __version__
from .config.loader import load_config, read_companies_doc, save_companies
from .core.models import EmploymentType, JobPosting, MatchResult, WorkMode, utcnow
from .errors import ConfigError, JobMonitorError
from .observability.logging import configure_logging

log = structlog.get_logger(__name__)


def _load(config_dir: Optional[str]):
    try:
        config = load_config(Path(config_dir) if config_dir else None)
    except ConfigError as exc:
        click.secho(f"Configuration error:\n{exc}", fg="red", err=True)
        sys.exit(2)
    configure_logging(config.settings.logging)
    return config


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="jobmon")
@click.option("--config-dir", "-c", default=None, help="Directory holding the YAML config.")
@click.pass_context
def main(ctx: click.Context, config_dir: Optional[str]) -> None:
    """Continuous monitor for early-career engineering roles."""
    ctx.ensure_object(dict)
    ctx.obj["config_dir"] = config_dir


# --------------------------------------------------------------------------- #


@main.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Run continuously on the configured schedule."""
    from .scheduler.runner import run_daemon

    config_dir = ctx.obj["config_dir"]
    _load(config_dir)
    try:
        asyncio.run(run_daemon(config_dir))
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@main.command()
@click.option("--only", multiple=True, help="Limit to these company slugs.")
@click.option("--no-notify", is_flag=True, help="Scan and score, but send nothing.")
@click.pass_context
def scan(ctx: click.Context, only: tuple, no_notify: bool) -> None:
    """Run a single scan cycle, then exit. Use this from cron or CI."""
    from .scheduler.runner import run_once

    config = _load(ctx.obj["config_dir"])
    result = asyncio.run(run_once(config, only=list(only) or None, notify=not no_notify))

    click.echo("")
    click.secho(
        f"Scanned {len(result.companies)} companies in {result.duration_seconds:.1f}s",
        bold=True,
    )
    click.echo(
        f"  postings seen {result.jobs_seen}   new {result.jobs_new}   "
        f"matched {result.jobs_matched}   notified {result.notifications_sent}"
    )
    if result.failures:
        click.secho(f"  {len(result.failures)} companies failed:", fg="yellow")
        for failure in result.failures:
            click.echo(f"    {failure.company_slug}: {failure.error}")
    sys.exit(1 if result.failure_ratio > 0.5 else 0)


# --------------------------------------------------------------------------- #


@main.command()
@click.option("--only", multiple=True, help="Limit to these company slugs.")
@click.option("--all", "include_configured", is_flag=True,
              help="Re-probe companies that already have a connector set.")
@click.option("--write/--dry-run", default=True, help="Write results into companies.yaml.")
@click.pass_context
def discover(ctx: click.Context, only: tuple, include_configured: bool, write: bool) -> None:
    """Detect each company's careers platform and write the result to config.

    Probes the careers page for vendor fingerprints, tries board-token guesses,
    and — crucially — verifies each candidate by actually fetching postings.
    """
    config = _load(ctx.obj["config_dir"])
    config_dir = Path(ctx.obj["config_dir"] or "config")

    targets = [
        c
        for c in config.companies.companies
        if (not only or c.slug in only)
        and (include_configured or c.connector == "auto" or not c.verified)
    ]
    if not targets:
        click.echo("Nothing to discover. Use --all to re-probe configured companies.")
        return

    click.echo(f"Probing {len(targets)} companies (this makes real requests)...\n")

    async def probe():
        from .connectors.discovery import Discovery
        from .net.client import HttpClient

        results = []
        async with HttpClient(config.settings.http) as http:
            discovery = Discovery(http)
            semaphore = asyncio.Semaphore(6)

            async def one(company):
                async with semaphore:
                    return company, await discovery.discover(company)

            for coro in asyncio.as_completed([one(c) for c in targets]):
                company, result = await coro
                results.append((company, result))
                if result.ok:
                    click.secho(
                        f"  OK    {company.slug:16s} {result.connector:16s} "
                        f"{result.jobs_found:5d} postings  {result.params}",
                        fg="green",
                    )
                else:
                    click.secho(f"  FAIL  {company.slug:16s} {result.error}", fg="yellow")
        return results

    results = asyncio.run(probe())
    found = [(c, r) for c, r in results if r.ok]

    click.echo("")
    click.secho(f"{len(found)}/{len(targets)} companies resolved.", bold=True)

    if not write or not found:
        if not write:
            click.echo("Dry run — companies.yaml not modified.")
        return

    doc = read_companies_doc(config_dir)
    by_slug = {r.slug: r for _, r in found}
    for entry in doc.get("companies", []):
        result = by_slug.get(entry.get("slug"))
        if result is None:
            continue
        entry["connector"] = result.connector
        entry["params"] = result.params
        entry["verified"] = True
    save_companies(config_dir, doc)
    click.secho(f"Updated {config_dir / 'companies.yaml'}", fg="green")


# --------------------------------------------------------------------------- #


@main.command()
@click.option("--check-endpoints/--no-check-endpoints", default=True,
              help="Actually call each company's API.")
@click.pass_context
def doctor(ctx: click.Context, check_endpoints: bool) -> None:
    """Check configuration, endpoints and notification channels."""
    config = _load(ctx.obj["config_dir"])
    problems = 0

    click.secho("Configuration", bold=True)
    click.echo(f"  companies enabled     {len(config.enabled_companies())}")
    click.echo(f"  match threshold       {config.settings.matching.threshold}")
    click.echo(f"  scan interval         {config.settings.scan.interval_seconds}s")
    click.echo(f"  database              {config.settings.database.url.split('://')[0]}")

    unverified = [c.slug for c in config.enabled_companies() if not c.verified]
    if unverified:
        click.secho(
            f"  {len(unverified)} companies unverified: {', '.join(unverified[:8])}"
            + ("..." if len(unverified) > 8 else ""),
            fg="yellow",
        )
        click.echo("    run: jobmon discover")

    click.echo("")
    click.secho("Matching", bold=True)

    async def check():
        nonlocal problems
        from .matching.scorer import Scorer
        from .net.client import HttpClient
        from .notify.dispatcher import Dispatcher
        from .storage.repository import Repository

        scorer = await asyncio.to_thread(Scorer, config)
        backend = scorer.semantic.backend
        colour = "green" if backend == "sentence-transformers" else "yellow"
        click.secho(f"  semantic backend      {backend}", fg=colour)
        if backend != "sentence-transformers":
            click.echo("    install: pip install 'jobmonitor[embeddings]'")
        click.echo(
            f"  llm adjudication      "
            f"{'enabled' if scorer.llm.available else 'disabled'}"
        )

        repo = Repository(config.settings.database)
        await repo.initialize()
        async with HttpClient(config.settings.http) as http:
            click.echo("")
            click.secho("Notification channels", bold=True)
            dispatcher = Dispatcher(config.notifications, repo, http, config.settings.timezone)
            active = dispatcher.active_channels()
            if not active:
                click.secho("  none enabled — you will not be notified", fg="red")
                problems += 1
            issues = dispatcher.configuration_problems()
            for channel in active:
                issue = issues.get(channel.name)
                if issue:
                    click.secho(f"  {channel.name:10s} MISCONFIGURED — {issue}", fg="red")
                    problems += 1
                else:
                    click.secho(
                        f"  {channel.name:10s} ready (min score {channel.min_score})",
                        fg="green",
                    )

            if check_endpoints:
                click.echo("")
                click.secho("Endpoints", bold=True)
                from .connectors.registry import get_connector_class

                semaphore = asyncio.Semaphore(8)

                async def probe(company):
                    async with semaphore:
                        if company.connector == "auto":
                            return company, None, "connector not resolved (run discover)"
                        try:
                            connector = get_connector_class(company.connector)(company, http)
                            postings = await asyncio.wait_for(
                                connector.fetch_listings(), timeout=45
                            )
                            return company, len(postings), None
                        except asyncio.TimeoutError:
                            return company, None, "timed out"
                        except Exception as exc:
                            return company, None, f"{type(exc).__name__}: {exc}"[:110]

                targets = config.enabled_companies()
                for coro in asyncio.as_completed([probe(c) for c in targets]):
                    company, count, error = await coro
                    if error:
                        click.secho(f"  {company.slug:16s} FAIL  {error}", fg="yellow")
                        problems += 1
                    else:
                        click.secho(
                            f"  {company.slug:16s} ok    {count} postings", fg="green"
                        )
        await repo.close()

    asyncio.run(check())

    click.echo("")
    if problems:
        click.secho(f"{problems} problem(s) found.", fg="yellow", bold=True)
        sys.exit(1)
    click.secho("All checks passed.", fg="green", bold=True)


# --------------------------------------------------------------------------- #


@main.command("test-notify")
@click.option("--score", default=97, help="Score to put on the sample alert.")
@click.pass_context
def test_notify(ctx: click.Context, score: int) -> None:
    """Send a sample alert through every enabled channel."""
    config = _load(ctx.obj["config_dir"])

    posting = JobPosting(
        company="Caterpillar",
        company_slug="caterpillar",
        external_id="SAMPLE-1",
        title="Mechanical Engineering Intern",
        url="https://careers.caterpillar.com/en/jobs/sample",
        department="Product Development",
        location="Peoria, IL",
        description="Sample posting generated by `jobmon test-notify`.",
        posted_at=utcnow() - timedelta(minutes=3),
    )
    posting.employment_type = EmploymentType.INTERN
    posting.work_mode = WorkMode.ONSITE
    match = MatchResult(
        score=score,
        matched=True,
        reasons=["Mechanical Engineering", "Internship", "Manufacturing"],
    )

    async def send():
        from .net.client import HttpClient
        from .notify.dispatcher import Dispatcher
        from .storage.repository import Repository

        repo = Repository(config.settings.database)
        await repo.initialize()
        async with HttpClient(config.settings.http) as http:
            dispatcher = Dispatcher(
                config.notifications, repo, http, config.settings.timezone
            )
            if not dispatcher.active_channels():
                click.secho("No channels enabled. Edit config/notifications.yaml.", fg="red")
                await repo.close()
                sys.exit(1)
            # Unique key so repeated test sends are never deduped away.
            appearance = int(utcnow().timestamp())
            sent, failed = await dispatcher.dispatch(posting, match, appearance)
            click.echo("")
            if sent:
                click.secho(f"Delivered to {sent} channel(s).", fg="green")
            if failed:
                click.secho(f"{failed} channel(s) failed — see the log above.", fg="red")
        await repo.close()
        return failed

    failed = asyncio.run(send())
    sys.exit(1 if failed else 0)


# --------------------------------------------------------------------------- #


@main.command("list")
@click.option("--days", default=7, help="Look back this many days.")
@click.option("--min-score", default=0, help="Only show matches at or above this score.")
@click.option("--limit", default=40)
@click.pass_context
def list_jobs(ctx: click.Context, days: int, min_score: int, limit: int) -> None:
    """Show recently found postings."""
    config = _load(ctx.obj["config_dir"])

    async def fetch():
        from .storage.repository import Repository

        repo = Repository(config.settings.database)
        await repo.initialize()
        rows = await repo.recent_matches(
            utcnow() - timedelta(days=days), min_score=min_score, limit=limit
        )
        await repo.close()
        return rows

    rows = asyncio.run(fetch())
    if not rows:
        click.echo("Nothing found in that window.")
        return

    click.secho(f"{'score':>5}  {'company':18} {'title':46} location", bold=True)
    for row in rows:
        colour = "green" if row["matched"] else None
        click.secho(
            f"{row['score']:>5}  {row['company'][:18]:18} {row['title'][:46]:46} "
            f"{(row.get('location') or '')[:26]}",
            fg=colour,
        )


@main.command()
@click.option("--days", default=7)
@click.pass_context
def stats(ctx: click.Context, days: int) -> None:
    """Summarise activity and surface failing companies."""
    config = _load(ctx.obj["config_dir"])

    async def fetch():
        from .storage.repository import Repository

        repo = Repository(config.settings.database)
        await repo.initialize()
        data = await repo.stats(utcnow() - timedelta(days=days))
        last = await repo.last_scan()
        await repo.close()
        return data, last

    data, last = asyncio.run(fetch())

    click.secho(f"Last {days} days", bold=True)
    click.echo(f"  new postings          {data['new_jobs']}")
    click.echo(f"  matched               {data['matched']}")
    click.echo(f"  notifications sent    {data['notifications_sent']}")
    click.echo(f"  total tracked         {data['total_jobs_tracked']}")

    if last:
        click.echo("")
        click.secho("Last scan", bold=True)
        click.echo(f"  finished              {last['finished_at']}")
        click.echo(f"  duration              {last['duration_seconds']:.1f}s")
        click.echo(
            f"  companies             {last['companies_scanned']} "
            f"({last['companies_failed']} failed)"
        )

    if data["matches_by_company"]:
        click.echo("")
        click.secho("Matches by company", bold=True)
        for row in data["matches_by_company"]:
            click.echo(f"  {row['company'][:28]:28} {row['count']}")

    if data["failing_companies"]:
        click.echo("")
        click.secho("Failing companies", bold=True, fg="yellow")
        for row in data["failing_companies"]:
            click.echo(f"  {row['slug'][:20]:20} {row['failures']}x  {(row['error'] or '')[:60]}")


@main.command()
@click.option("--format", "fmt", type=click.Choice(["csv", "json", "rss"]), default="csv")
@click.option("--output", "-o", default=None, help="Output path.")
@click.option("--min-score", default=0)
@click.pass_context
def export(ctx: click.Context, fmt: str, output: Optional[str], min_score: int) -> None:
    """Export tracked postings to CSV, JSON or RSS."""
    config = _load(ctx.obj["config_dir"])
    path = Path(output or f"data/exports/jobs.{fmt}")

    async def fetch():
        from .storage.repository import Repository

        repo = Repository(config.settings.database)
        await repo.initialize()
        rows = await repo.all_jobs(min_score=min_score)
        await repo.close()
        return rows

    rows = asyncio.run(fetch())
    from .exports.exporters import to_csv, to_json, to_rss

    writer = {"csv": to_csv, "json": to_json, "rss": to_rss}[fmt]
    count = writer(rows, path)
    click.secho(f"Wrote {count} rows to {path}", fg="green")


@main.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Print a health summary (also served over HTTP by `jobmon run`)."""
    config = _load(ctx.obj["config_dir"])

    async def fetch():
        from .storage.repository import Repository

        repo = Repository(config.settings.database)
        await repo.initialize()
        last = await repo.last_scan()
        await repo.close()
        return last

    last = asyncio.run(fetch())
    if last is None:
        click.secho("No scan has completed yet.", fg="yellow")
        sys.exit(1)

    age = (utcnow() - last["finished_at"]).total_seconds()
    limit = config.settings.health.heartbeat_timeout_seconds
    healthy = age < limit
    click.secho("healthy" if healthy else "UNHEALTHY", fg="green" if healthy else "red", bold=True)
    click.echo(f"  last scan {age:.0f}s ago (limit {limit}s)")
    click.echo(f"  {last['companies_scanned']} companies, {last['companies_failed']} failed")
    click.echo(f"  {last['jobs_matched']} matched, {last['notifications_sent']} notified")
    sys.exit(0 if healthy else 1)


@main.command("init-db")
@click.pass_context
def init_db(ctx: click.Context) -> None:
    """Create the database schema."""
    config = _load(ctx.obj["config_dir"])

    async def go():
        from .storage.repository import Repository

        repo = Repository(config.settings.database)
        await repo.initialize()
        await repo.close()

    asyncio.run(go())
    click.secho("Database ready.", fg="green")


def cli() -> None:
    try:
        main(obj={})
    except JobMonitorError as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()

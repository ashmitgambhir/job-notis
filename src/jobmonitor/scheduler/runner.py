"""The long-running daemon.

Wires together config, storage, HTTP, scoring, notification and the scan loop,
and keeps them alive. Two cadences run side by side — companies tagged
``priority: high`` scan more often than the rest — plus optional digest jobs.

For cron-style deployments (GitHub Actions, Render Cron, EventBridge) use
``jobmon scan`` instead; it runs exactly one cycle and exits.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import timedelta
from typing import Any, List, Optional

import structlog

from ..config.loader import load_config
from ..config.schema import AppConfig
from ..core.health import HealthServer, HealthState
from ..core.models import utcnow
from ..core.orchestrator import Orchestrator
from ..matching.scorer import Scorer
from ..net.client import HttpClient
from ..notify.dispatcher import Dispatcher
from ..notify.formatter import render_digest
from ..storage.repository import Repository

log = structlog.get_logger(__name__)


class Runtime:
    """Owns every long-lived resource, so setup and teardown live in one place."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.repo = Repository(config.settings.database)
        self.http = HttpClient(config.settings.http)
        self.scorer: Optional[Scorer] = None
        self.dispatcher: Optional[Dispatcher] = None
        self.orchestrator: Optional[Orchestrator] = None

    async def __aenter__(self) -> Runtime:
        await self.repo.initialize()
        await self.http.start()
        # Loading the embedding model blocks for a second or two; keep it off
        # the event loop so a concurrent health probe still answers.
        self.scorer = await asyncio.to_thread(Scorer, self.config)
        self.dispatcher = Dispatcher(
            self.config.notifications, self.repo, self.http, self.config.settings.timezone
        )
        self.orchestrator = Orchestrator(
            self.config, self.repo, self.http, self.scorer, self.dispatcher
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.http.close()
        await self.repo.close()
        try:
            from ..connectors.browser import shutdown_browser

            await shutdown_browser()
        except Exception:
            pass


async def run_once(
    config: AppConfig, only: Optional[List[str]] = None, notify: bool = True
) -> Any:
    async with Runtime(config) as runtime:
        assert runtime.orchestrator is not None
        return await runtime.orchestrator.run_cycle(only=only, notify=notify)


class Daemon:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.health = HealthState(config.settings.health.heartbeat_timeout_seconds)
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        log.info("shutdown_requested")
        self._stop.set()

    async def run(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        async with Runtime(self.config) as runtime:
            orchestrator = runtime.orchestrator
            dispatcher = runtime.dispatcher
            assert orchestrator is not None and dispatcher is not None

            server: Optional[HealthServer] = None
            if self.config.settings.health.enabled and self.config.settings.health.http_port:
                server = HealthServer(
                    self.health,
                    self.config.settings.health.http_port,
                    extra=lambda: {
                        "http": runtime.http.cache_info(),
                        "notifications": dispatcher.stats(),
                        "matching": runtime.scorer.stats() if runtime.scorer else {},
                    },
                )
                await server.start()

            priority = [c.slug for c in self.config.enabled_companies() if c.priority == "high"]
            standard = [c.slug for c in self.config.enabled_companies() if c.priority != "high"]

            # One cycle at a time; overlapping cycles would double-scan and
            # fight over the detail-fetch budget.
            lock = asyncio.Lock()

            async def cycle(slugs: Optional[List[str]], label: str) -> None:
                if not slugs:
                    return
                if lock.locked():
                    log.warning("cycle_skipped_still_running", label=label)
                    return
                async with lock:
                    try:
                        result = await orchestrator.run_cycle(only=slugs)
                        self.health.record_cycle(result)
                        self.health.last_error = None
                    except Exception as exc:
                        self.health.last_error = f"{type(exc).__name__}: {exc}"
                        log.exception("cycle_failed", label=label)

            scheduler = AsyncIOScheduler(timezone=self.config.settings.timezone)
            scan = self.config.settings.scan

            if priority:
                scheduler.add_job(
                    cycle,
                    IntervalTrigger(seconds=scan.priority_interval_seconds),
                    args=[priority, "priority"],
                    id="scan_priority",
                    max_instances=1,
                    coalesce=True,
                )
            if standard:
                scheduler.add_job(
                    cycle,
                    IntervalTrigger(seconds=scan.interval_seconds),
                    args=[standard, "standard"],
                    id="scan_standard",
                    max_instances=1,
                    coalesce=True,
                )

            digest = self.config.notifications.digest
            if digest.daily_enabled:
                hour, _, minute = digest.daily_at.partition(":")
                scheduler.add_job(
                    self._send_daily_digest,
                    CronTrigger(hour=int(hour), minute=int(minute or 0)),
                    args=[runtime],
                    id="daily_digest",
                )
            if digest.weekly_enabled:
                day, _, clock = digest.weekly_at.partition(" ")
                hour, _, minute = (clock or "08:00").partition(":")
                scheduler.add_job(
                    self._send_weekly_digest,
                    CronTrigger(day_of_week=day.lower(), hour=int(hour), minute=int(minute or 0)),
                    args=[runtime],
                    id="weekly_digest",
                )

            # Housekeeping: drop closed, unmatched postings so a multi-year
            # deployment does not grow without bound.
            scheduler.add_job(
                runtime.repo.purge_stale, CronTrigger(hour=4, minute=30), id="purge"
            )

            scheduler.start()
            log.info(
                "daemon_started",
                priority_companies=len(priority),
                standard_companies=len(standard),
                priority_interval=scan.priority_interval_seconds,
                interval=scan.interval_seconds,
            )

            # Scan immediately rather than waiting out the first interval.
            await cycle(priority + standard, "startup")

            try:
                await self._stop.wait()
            finally:
                scheduler.shutdown(wait=False)
                if server is not None:
                    await server.stop()
                log.info("daemon_stopped")

    async def _send_daily_digest(self, runtime: Runtime) -> None:
        await self._digest(runtime, days=1, title="Daily job summary")

    async def _send_weekly_digest(self, runtime: Runtime) -> None:
        await self._digest(runtime, days=7, title="Weekly job summary")

    async def _digest(self, runtime: Runtime, days: int, title: str) -> None:
        since = utcnow() - timedelta(days=days)
        threshold = (
            self.config.settings.matching.review_threshold
            if self.config.notifications.digest.include_review_band
            else self.config.settings.matching.threshold
        )
        matches = await runtime.repo.recent_matches(since, min_score=threshold, limit=100)
        stats = await runtime.repo.stats(since)
        label = (
            f"{len(matches)} matches from {stats['new_jobs']} new postings "
            f"in the last {days} day{'s' if days != 1 else ''}"
        )
        html_body = render_digest(matches, title, label)
        text_body = "\n".join(
            f"{m['score']}%  {m['company']} — {m['title']} ({m.get('location') or 'n/a'})\n"
            f"      {m['url']}"
            for m in matches
        ) or "No new matches in this period."
        assert runtime.dispatcher is not None
        await runtime.dispatcher.send_digest(f"[jobmonitor] {title}", html_body, text_body)


async def run_daemon(config_dir: Optional[str] = None) -> None:
    from pathlib import Path

    config = load_config(Path(config_dir) if config_dir else None)
    daemon = Daemon(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:
            # Windows and some container runtimes; Ctrl-C still raises.
            pass

    await daemon.run()

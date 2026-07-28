"""Scan orchestration.

Runs one cycle: every enabled company is scanned concurrently, results are
deduped against the database, survivors are scored, and matches are dispatched.

The design rule throughout is **graceful degradation** — one company's failure
is recorded and moves on. A 500 from Caterpillar must never stop Anduril's
postings reaching you, so every per-company step is wrapped and bounded by its
own timeout.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import structlog

from ..config.schema import AppConfig, CompanyConfig
from ..connectors.base import Connector
from ..connectors.discovery import Discovery
from ..connectors.registry import get_connector_class
from ..errors import ConfigError, ConnectorError, JobMonitorError
from ..matching.scorer import Scorer
from ..net.client import HttpClient
from ..notify.dispatcher import Dispatcher, dispatch_many
from ..observability.logging import bind_company, clear_company
from ..storage.repository import Repository
from .models import CompanyScanResult, JobPosting, MatchResult, ScanCycleResult, utcnow

log = structlog.get_logger(__name__)


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        repo: Repository,
        http: HttpClient,
        scorer: Scorer,
        dispatcher: Dispatcher,
    ) -> None:
        self.config = config
        self.repo = repo
        self.http = http
        self.scorer = scorer
        self.dispatcher = dispatcher
        self._discovery = Discovery(http)
        # Connectors are cached across cycles so their discovered host and
        # known-id state survive; rebuilding them each cycle would lose both.
        self._connectors: Dict[str, Connector] = {}
        self._detail_budget = 0

    # ------------------------------------------------------------------ #

    async def run_cycle(
        self, only: Optional[List[str]] = None, notify: bool = True
    ) -> ScanCycleResult:
        started = utcnow()
        companies = [
            c
            for c in self.config.enabled_companies()
            if not only or c.slug in only
        ]
        if not companies:
            log.warning("no_companies_to_scan", filter=only)
            return ScanCycleResult(started_at=started, finished_at=utcnow())

        self.scorer.llm.reset_cycle()
        self._detail_budget = self.config.settings.scan.max_detail_fetches_per_cycle

        semaphore = asyncio.Semaphore(self.config.settings.scan.max_concurrent_companies)
        results = await asyncio.gather(
            *[self._scan_guarded(c, semaphore) for c in companies]
        )

        cycle = ScanCycleResult(
            started_at=started, finished_at=utcnow(), companies=list(results)
        )

        if notify:
            pending: List[Tuple[JobPosting, MatchResult, int]] = []
            for result in results:
                pending.extend(result.matches)
            if pending:
                sent, failed = await dispatch_many(self.dispatcher, pending)
                cycle.notifications_sent = sent
                cycle.notification_failures = failed

        await self._record(cycle)
        await self._check_health(cycle)

        log.info(
            "scan_cycle_complete",
            duration=round(cycle.duration_seconds, 1),
            companies=len(cycle.companies),
            failed=len(cycle.failures),
            seen=cycle.jobs_seen,
            new=cycle.jobs_new,
            matched=cycle.jobs_matched,
            notified=cycle.notifications_sent,
        )
        return cycle

    async def _scan_guarded(
        self, company: CompanyConfig, semaphore: asyncio.Semaphore
    ) -> CompanyScanResult:
        async with semaphore:
            started = utcnow()
            bind_company(company.slug)
            try:
                return await asyncio.wait_for(
                    self._scan_company(company),
                    timeout=self.config.settings.scan.per_company_timeout_seconds,
                )
            except asyncio.TimeoutError:
                error = (
                    f"timed out after "
                    f"{self.config.settings.scan.per_company_timeout_seconds}s"
                )
                log.warning("company_timeout", company=company.slug)
                await self.repo.record_company_outcome(company.slug, ok=False, error=error)
                return CompanyScanResult(
                    company_slug=company.slug,
                    company_name=company.name,
                    ok=False,
                    duration_seconds=(utcnow() - started).total_seconds(),
                    error=error,
                    error_type="timeout",
                )
            except Exception as exc:
                # Nothing a single company does may abort the cycle.
                error = f"{type(exc).__name__}: {exc}"
                log.exception("company_scan_failed", company=company.slug)
                await self.repo.record_company_outcome(company.slug, ok=False, error=error)
                return CompanyScanResult(
                    company_slug=company.slug,
                    company_name=company.name,
                    ok=False,
                    duration_seconds=(utcnow() - started).total_seconds(),
                    error=error[:500],
                    error_type=type(exc).__name__,
                )
            finally:
                clear_company()

    # ------------------------------------------------------------------ #

    async def _scan_company(self, company: CompanyConfig) -> CompanyScanResult:
        started = utcnow()
        state = await self.repo.get_company_state(company.slug)
        connector = await self._resolve_connector(company, state)

        result = CompanyScanResult(
            company_slug=company.slug,
            company_name=company.name,
            ok=True,
            duration_seconds=0.0,
            connector=connector.name,
        )

        connector.known_ids = await self.repo.known_external_ids(company.slug)
        postings = await connector.fetch_listings()
        result.jobs_seen = len(postings)

        if not postings:
            raise ConnectorError("connector returned zero postings", company=company.slug)

        new_postings, changed = await self.repo.upsert_postings(postings)
        result.jobs_new = len(new_postings)
        await self.repo.close_missing(company.slug, [p.job_key for p in postings])

        # First sight of a company: record the whole board, notify about none
        # of it. Otherwise adding a company means hundreds of alerts.
        seeding = self.config.settings.scan.seed_without_notifying and not state.get("seeded")
        candidates = new_postings + changed

        matched, detail_fetches = await self._evaluate(connector, candidates, seeding, result)
        result.jobs_matched = len(matched)
        result.jobs_ignored = len(candidates) - len(matched)
        result.detail_fetches = detail_fetches
        result.matches = matched
        result.duration_seconds = (utcnow() - started).total_seconds()

        if seeding:
            await self.repo.update_company_state(company.slug, seeded=True)
            log.info("company_seeded", company=company.slug, postings=len(postings))
        await self.repo.record_company_outcome(
            company.slug, ok=True, jobs_seen=len(postings), matches=len(matched)
        )
        log.info(
            "company_scanned",
            company=company.slug,
            connector=connector.name,
            seen=result.jobs_seen,
            new=result.jobs_new,
            matched=result.jobs_matched,
            details=detail_fetches,
            duration=round(result.duration_seconds, 1),
        )
        return result

    async def _evaluate(
        self,
        connector: Connector,
        candidates: List[JobPosting],
        seeding: bool,
        result: CompanyScanResult,
    ) -> Tuple[List[Tuple[JobPosting, MatchResult, int]], int]:
        """Prefilter, fetch detail for survivors, then score."""
        matched: List[Tuple[JobPosting, MatchResult, int]] = []
        detail_fetches = 0
        max_age = self.config.filters.max_posting_age_days

        for posting in candidates:
            age = posting.age_days()
            if age is not None and age > max_age:
                continue

            if self.config.settings.scan.prefilter_before_detail_fetch:
                if not self.scorer.worth_detail_fetch(posting):
                    continue

            # The seeding pass notifies about nothing, so a detail fetch buys
            # nothing either — skip it and leave the budget for the cycles
            # that can actually page you.
            if posting.detail_pending and not seeding and self._detail_budget > 0:
                try:
                    posting = await connector.fetch_detail(posting)
                    detail_fetches += 1
                    self._detail_budget -= 1
                except JobMonitorError as exc:
                    # Score on the title alone rather than dropping it.
                    log.debug(
                        "detail_fetch_failed", job=posting.job_key, error=str(exc)[:160]
                    )

            match = await self.scorer.score(posting)
            await self.repo.record_match(posting, match)

            if match.matched and not seeding:
                matched.append((posting, match, 1))

        return matched, detail_fetches

    # ------------------------------------------------------------------ #

    async def _resolve_connector(
        self, company: CompanyConfig, state: Dict[str, Any]
    ) -> Connector:
        """Config first, cached discovery second, live discovery last."""
        cached = self._connectors.get(company.slug)
        if cached is not None:
            return cached

        if company.connector != "auto":
            connector = get_connector_class(company.connector)(company, self.http)
            self._connectors[company.slug] = connector
            return connector

        detected = state.get("detected_connector")
        if detected:
            resolved = company.model_copy(
                update={"connector": detected, "params": state.get("detected_params") or {}}
            )
            try:
                connector = get_connector_class(detected)(resolved, self.http)
                self._connectors[company.slug] = connector
                return connector
            except (ConfigError, ConnectorError) as exc:
                # Cached detection has gone stale; fall through and re-discover.
                log.warning("cached_connector_invalid", company=company.slug, error=str(exc))

        log.info("running_discovery", company=company.slug)
        discovery = await self._discovery.discover(company)
        if not discovery.ok:
            raise ConnectorError(
                f"could not determine a working connector for '{company.slug}': "
                f"{discovery.error}. Tried: "
                f"{', '.join(f'{c}({s})' for c, s in discovery.tried) or 'nothing'}. "
                f"Set `connector:` and `params:` in companies.yaml, or run "
                f"`jobmon discover --only {company.slug}`.",
                company=company.slug,
            )

        await self.repo.update_company_state(
            company.slug,
            detected_connector=discovery.connector,
            detected_params=discovery.params,
            detected_at=utcnow(),
        )
        resolved = company.model_copy(
            update={"connector": discovery.connector, "params": discovery.params}
        )
        connector = get_connector_class(discovery.connector)(resolved, self.http)
        self._connectors[company.slug] = connector
        return connector

    # ------------------------------------------------------------------ #

    async def _record(self, cycle: ScanCycleResult) -> None:
        await self.repo.record_scan(
            {
                "started_at": cycle.started_at,
                "finished_at": cycle.finished_at,
                "duration_seconds": cycle.duration_seconds,
                "companies_scanned": len(cycle.companies),
                "companies_failed": len(cycle.failures),
                "jobs_seen": cycle.jobs_seen,
                "jobs_new": cycle.jobs_new,
                "jobs_matched": cycle.jobs_matched,
                "jobs_ignored": cycle.jobs_ignored,
                "notifications_sent": cycle.notifications_sent,
                "http_requests": self.http.stats["requests"],
                "http_retries": self.http.stats["retries"],
                "cache_hits": self.http.stats["cache_hits"],
            }
        )

    async def _check_health(self, cycle: ScanCycleResult) -> None:
        """Alert on the failure shapes that mean the system has stopped working."""
        health = self.config.settings.health
        if not health.enabled or not cycle.companies:
            return

        if cycle.failure_ratio >= health.cycle_failure_ratio_alert:
            names = ", ".join(f"{c.company_slug} ({c.error_type})" for c in cycle.failures[:12])
            await self.dispatcher.send_alert(
                "cycle_failure_ratio",
                f"{len(cycle.failures)}/{len(cycle.companies)} companies failed",
                f"A scan cycle finished with {cycle.failure_ratio:.0%} of companies "
                f"failing.\n\nFailures: {names}\n\n"
                "This usually means a network problem on the host rather than "
                "many sites breaking at once.",
            )

        for failure in cycle.failures:
            consecutive = (await self.repo.get_company_state(failure.company_slug)).get(
                "consecutive_failures", 0
            )
            if consecutive == health.company_failure_alert_threshold:
                await self.dispatcher.send_alert(
                    f"company_down:{failure.company_slug}",
                    f"{failure.company_name} has failed {consecutive} scans in a row",
                    f"Company: {failure.company_name} ({failure.company_slug})\n"
                    f"Connector: {failure.connector}\n"
                    f"Error: {failure.error}\n\n"
                    f"Run `jobmon discover --only {failure.company_slug}` to "
                    "re-detect its careers platform.",
                )

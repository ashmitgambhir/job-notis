"""Persistence layer.

SQLAlchemy is synchronous here and every public method is an ``async def`` that
hands the work to a thread. That keeps the DB code simple and debuggable while
still not blocking the event loop that the scanners run on.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import structlog
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..config.schema import DatabaseSettings
from ..core.models import JobPosting, MatchResult, utcnow
from .models import AlertState, Base, CompanyState, Job, Notification, ScanRun

log = structlog.get_logger(__name__)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; normalise so comparisons work."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Repository:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        url = settings.url
        if url.startswith("sqlite"):
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._engine = create_engine(
                url,
                future=True,
                # Sessions are created per-call and used inside one thread, but
                # the pool itself is shared across the executor's threads.
                connect_args={"check_same_thread": False, "timeout": 30},
            )
        else:
            self._engine = create_engine(
                url,
                future=True,
                pool_size=settings.pool_size,
                max_overflow=settings.max_overflow,
                pool_pre_ping=True,
            )
        self._session_factory = sessionmaker(bind=self._engine, future=True, expire_on_commit=False)

    # ------------------------------------------------------------------ #

    def _init_sync(self) -> None:
        Base.metadata.create_all(self._engine)
        if self.settings.url.startswith("sqlite"):
            with self._engine.connect() as conn:
                # WAL lets the health endpoint read while a scan writes.
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
                conn.exec_driver_sql("PRAGMA busy_timeout=30000")
                conn.commit()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._init_sync)
        log.info("database_ready", url=self._safe_url())

    def _safe_url(self) -> str:
        url = self.settings.url
        if "@" in url:  # strip credentials before logging
            scheme, _, rest = url.partition("://")
            return f"{scheme}://***@{rest.split('@', 1)[-1]}"
        return url

    async def close(self) -> None:
        await asyncio.to_thread(self._engine.dispose)

    def session(self) -> Session:
        return self._session_factory()

    # -------------------------------------------------------- job dedupe --

    def _upsert_sync(
        self, postings: Sequence[JobPosting]
    ) -> Tuple[List[JobPosting], List[JobPosting]]:
        """Return ``(new_postings, changed_postings)``.

        "New" covers both genuinely first-seen postings and ones that had been
        closed and came back — the spec calls for treating a reappearance as
        new.
        """
        new: List[JobPosting] = []
        changed: List[JobPosting] = []
        now = utcnow()

        # Connectors legitimately hand back the same posting twice: paginated
        # boards overlap when the underlying list shifts mid-walk, and HTML
        # sources emit one link per layout region. Inserting both copies
        # violates the job_key unique constraint and aborts the whole batch,
        # losing every posting for that company. De-duplicate on the way in,
        # keeping the first occurrence.
        deduped: Dict[str, JobPosting] = {}
        for posting in postings:
            deduped.setdefault(posting.job_key, posting)
        postings = list(deduped.values())

        with self.session() as s:
            keys = [p.job_key for p in postings]
            existing: Dict[str, Job] = {}
            # Chunked to stay under SQLite's variable limit at 500 companies.
            for i in range(0, len(keys), 500):
                rows = s.scalars(select(Job).where(Job.job_key.in_(keys[i : i + 500]))).all()
                existing.update({r.job_key: r for r in rows})

            for posting in postings:
                row = existing.get(posting.job_key)
                if row is None:
                    # A different requisition ID carrying an identical
                    # title+location is a re-post, not a new opening.
                    # no_autoflush: this SELECT would otherwise flush the rows
                    # queued earlier in the loop, turning any constraint issue
                    # into a failure of the whole batch mid-iteration.
                    with s.no_autoflush:
                        twin = s.scalars(
                            select(Job)
                            .where(Job.dedupe_hash == posting.dedupe_hash)
                            .where(Job.closed_at.is_(None))
                            .limit(1)
                        ).first()
                    is_repost = twin is not None
                    s.add(
                        Job(
                            job_key=posting.job_key,
                            company_slug=posting.company_slug,
                            company_name=posting.company,
                            external_id=posting.external_id,
                            title=posting.title,
                            url=posting.url,
                            department=posting.department,
                            location=posting.location,
                            content_hash=posting.content_hash,
                            dedupe_hash=posting.dedupe_hash,
                            posted_at=posting.posted_at,
                            first_seen_at=now,
                            last_seen_at=now,
                            employment_type=posting.employment_type.value,
                            work_mode=posting.work_mode.value,
                            source_platform=posting.source_platform,
                        )
                    )
                    if not is_repost:
                        new.append(posting)
                    continue

                row.last_seen_at = now
                if row.closed_at is not None:
                    # Came back from the dead: treat as a fresh opening.
                    row.closed_at = None
                    row.appearance_count += 1
                    row.notified = False
                    new.append(posting)
                elif row.content_hash != posting.content_hash:
                    row.content_hash = posting.content_hash
                    row.title = posting.title
                    row.location = posting.location
                    row.department = posting.department
                    changed.append(posting)

            s.commit()
        return new, changed

    async def upsert_postings(
        self, postings: Sequence[JobPosting]
    ) -> Tuple[List[JobPosting], List[JobPosting]]:
        if not postings:
            return [], []
        return await asyncio.to_thread(self._upsert_sync, postings)

    def _record_match_sync(self, posting: JobPosting, result: MatchResult) -> None:
        with self.session() as s:
            row = s.scalars(select(Job).where(Job.job_key == posting.job_key)).first()
            if row is None:
                return
            row.score = result.score
            row.matched = result.matched
            row.match_reasons = json.dumps(result.reasons[:8])
            row.excluded_by = result.excluded_by
            row.employment_type = posting.employment_type.value
            row.work_mode = posting.work_mode.value
            row.requires_clearance = posting.requires_clearance
            row.offers_sponsorship = posting.offers_sponsorship
            row.deadline = posting.deadline
            if posting.department:
                row.department = posting.department
            s.commit()

    async def record_match(self, posting: JobPosting, result: MatchResult) -> None:
        await asyncio.to_thread(self._record_match_sync, posting, result)

    def _known_ids_sync(self, company_slug: str) -> Set[str]:
        with self.session() as s:
            rows = s.execute(
                select(Job.external_id).where(Job.company_slug == company_slug)
            ).all()
            return {r[0] for r in rows}

    async def known_external_ids(self, company_slug: str) -> Set[str]:
        """Feeds the connectors' early-stop paging.

        Without this a recency-ordered board is walked end to end every cycle;
        with it, a typical cycle stops after the first page of known postings.
        """
        return await asyncio.to_thread(self._known_ids_sync, company_slug)

    def _close_missing_sync(self, company_slug: str, seen_keys: Iterable[str]) -> int:
        """Mark postings absent from this scan as closed."""
        seen = set(seen_keys)
        now = utcnow()
        with self.session() as s:
            rows = s.scalars(
                select(Job)
                .where(Job.company_slug == company_slug)
                .where(Job.closed_at.is_(None))
            ).all()
            closed = 0
            for row in rows:
                if row.job_key not in seen:
                    row.closed_at = now
                    closed += 1
            s.commit()
        return closed

    async def close_missing(self, company_slug: str, seen_keys: Iterable[str]) -> int:
        return await asyncio.to_thread(self._close_missing_sync, company_slug, seen_keys)

    # ---------------------------------------------------- notification log --

    def _claim_notification_sync(
        self, job_key: str, channel: str, dedupe_key: str, score: int
    ) -> bool:
        """Atomically reserve the right to send.

        The unique constraint does the real work: if a second process gets
        there first, the insert fails and we return False rather than sending a
        duplicate.
        """
        with self.session() as s:
            try:
                s.add(
                    Notification(
                        job_key=job_key,
                        channel=channel,
                        dedupe_key=dedupe_key,
                        status="pending",
                        score=score,
                    )
                )
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    async def claim_notification(
        self, job_key: str, channel: str, dedupe_key: str, score: int
    ) -> bool:
        return await asyncio.to_thread(
            self._claim_notification_sync, job_key, channel, dedupe_key, score
        )

    def _finish_notification_sync(
        self, dedupe_key: str, channel: str, ok: bool, attempts: int, error: Optional[str]
    ) -> None:
        with self.session() as s:
            s.execute(
                update(Notification)
                .where(Notification.dedupe_key == dedupe_key, Notification.channel == channel)
                .values(
                    status="sent" if ok else "failed",
                    attempts=attempts,
                    sent_at=utcnow() if ok else None,
                    error=(error or "")[:2000] or None,
                )
            )
            if ok:
                s.execute(
                    update(Job)
                    .where(
                        Job.job_key
                        == select(Notification.job_key)
                        .where(Notification.dedupe_key == dedupe_key)
                        .scalar_subquery()
                    )
                    .values(notified=True)
                )
            s.commit()

    async def finish_notification(
        self, dedupe_key: str, channel: str, ok: bool, attempts: int, error: Optional[str] = None
    ) -> None:
        await asyncio.to_thread(
            self._finish_notification_sync, dedupe_key, channel, ok, attempts, error
        )

    def _release_notification_sync(self, dedupe_key: str, channel: str) -> None:
        """Drop the claim so a later cycle can retry a permanently failed send."""
        with self.session() as s:
            s.execute(
                delete(Notification).where(
                    Notification.dedupe_key == dedupe_key, Notification.channel == channel
                )
            )
            s.commit()

    async def release_notification(self, dedupe_key: str, channel: str) -> None:
        await asyncio.to_thread(self._release_notification_sync, dedupe_key, channel)

    def _channel_sent_last_hour_sync(self, channel: str) -> int:
        cutoff = utcnow() - timedelta(hours=1)
        with self.session() as s:
            return int(
                s.scalar(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.channel == channel)
                    .where(Notification.status == "sent")
                    .where(Notification.sent_at >= cutoff)
                )
                or 0
            )

    async def channel_sent_last_hour(self, channel: str) -> int:
        return await asyncio.to_thread(self._channel_sent_last_hour_sync, channel)

    # ------------------------------------------------------ company state --

    def _get_company_state_sync(self, slug: str) -> Dict[str, Any]:
        with self.session() as s:
            row = s.get(CompanyState, slug)
            if row is None:
                return {"slug": slug, "seeded": False, "consecutive_failures": 0}
            return {
                "slug": row.slug,
                "seeded": row.seeded,
                "consecutive_failures": row.consecutive_failures,
                "last_success_at": _aware(row.last_success_at),
                "last_error": row.last_error,
                "detected_connector": row.detected_connector,
                "detected_params": json.loads(row.detected_params) if row.detected_params else None,
                # Drives the re-discovery cooldown; set on failed attempts too.
                "detected_at": _aware(row.detected_at),
            }

    async def get_company_state(self, slug: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._get_company_state_sync, slug)

    def _update_company_state_sync(self, slug: str, **fields: Any) -> None:
        with self.session() as s:
            row = s.get(CompanyState, slug)
            if row is None:
                row = CompanyState(slug=slug)
                s.add(row)
            for key, value in fields.items():
                if key == "detected_params" and value is not None:
                    value = json.dumps(value)
                setattr(row, key, value)
            s.commit()

    async def update_company_state(self, slug: str, **fields: Any) -> None:
        await asyncio.to_thread(self._update_company_state_sync, slug, **fields)

    def _record_company_outcome_sync(
        self, slug: str, ok: bool, error: Optional[str], jobs_seen: int, matches: int
    ) -> int:
        with self.session() as s:
            row = s.get(CompanyState, slug)
            if row is None:
                row = CompanyState(slug=slug)
                s.add(row)
            row.last_attempt_at = utcnow()
            if ok:
                row.last_success_at = utcnow()
                row.consecutive_failures = 0
                row.last_error = None
                row.total_jobs_seen = (row.total_jobs_seen or 0) + jobs_seen
                row.total_matches = (row.total_matches or 0) + matches
            else:
                row.consecutive_failures = (row.consecutive_failures or 0) + 1
                row.last_error = (error or "")[:2000]
            s.commit()
            return row.consecutive_failures

    async def record_company_outcome(
        self, slug: str, ok: bool, error: Optional[str] = None, jobs_seen: int = 0, matches: int = 0
    ) -> int:
        return await asyncio.to_thread(
            self._record_company_outcome_sync, slug, ok, error, jobs_seen, matches
        )

    # -------------------------------------------------------------- runs --

    def _record_scan_sync(self, values: Dict[str, Any]) -> None:
        with self.session() as s:
            s.add(ScanRun(**values))
            s.commit()

    async def record_scan(self, values: Dict[str, Any]) -> None:
        await asyncio.to_thread(self._record_scan_sync, values)

    def _last_scan_sync(self) -> Optional[Dict[str, Any]]:
        with self.session() as s:
            row = s.scalars(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1)).first()
            if row is None:
                return None
            return {
                "started_at": _aware(row.started_at),
                "finished_at": _aware(row.finished_at),
                "duration_seconds": row.duration_seconds,
                "companies_scanned": row.companies_scanned,
                "companies_failed": row.companies_failed,
                "jobs_seen": row.jobs_seen,
                "jobs_new": row.jobs_new,
                "jobs_matched": row.jobs_matched,
                "notifications_sent": row.notifications_sent,
            }

    async def last_scan(self) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._last_scan_sync)

    # ------------------------------------------------------------ alerts --

    def _should_alert_sync(self, key: str, cooldown_seconds: int) -> bool:
        now = utcnow()
        with self.session() as s:
            row = s.get(AlertState, key)
            if row is not None:
                last = _aware(row.last_sent_at)
                if last and (now - last).total_seconds() < cooldown_seconds:
                    return False
                row.last_sent_at = now
                row.count += 1
            else:
                s.add(AlertState(key=key, last_sent_at=now, count=1))
            s.commit()
            return True

    async def should_alert(self, key: str, cooldown_seconds: int) -> bool:
        return await asyncio.to_thread(self._should_alert_sync, key, cooldown_seconds)

    # ------------------------------------------------------- reporting --

    def _recent_matches_sync(self, since: datetime, min_score: int, limit: int) -> List[Dict]:
        with self.session() as s:
            rows = s.scalars(
                select(Job)
                .where(Job.first_seen_at >= since)
                .where(Job.score >= min_score)
                .order_by(Job.score.desc(), Job.first_seen_at.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "company": r.company_name,
                    "title": r.title,
                    "location": r.location,
                    "url": r.url,
                    "score": r.score,
                    "matched": r.matched,
                    "employment_type": r.employment_type,
                    "work_mode": r.work_mode,
                    "posted_at": _aware(r.posted_at),
                    "first_seen_at": _aware(r.first_seen_at),
                    "reasons": json.loads(r.match_reasons) if r.match_reasons else [],
                }
                for r in rows
            ]

    async def recent_matches(
        self, since: datetime, min_score: int = 0, limit: int = 200
    ) -> List[Dict]:
        return await asyncio.to_thread(self._recent_matches_sync, since, min_score, limit)

    def _stats_sync(self, since: datetime) -> Dict[str, Any]:
        with self.session() as s:
            total_jobs = s.scalar(select(func.count()).select_from(Job)) or 0
            new_jobs = (
                s.scalar(select(func.count()).select_from(Job).where(Job.first_seen_at >= since))
                or 0
            )
            matched = (
                s.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.first_seen_at >= since, Job.matched.is_(True))
                )
                or 0
            )
            notified = (
                s.scalar(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.sent_at >= since, Notification.status == "sent")
                )
                or 0
            )
            by_company = s.execute(
                select(Job.company_name, func.count())
                .where(Job.first_seen_at >= since, Job.matched.is_(True))
                .group_by(Job.company_name)
                .order_by(func.count().desc())
                .limit(15)
            ).all()
            failing = s.scalars(
                select(CompanyState)
                .where(CompanyState.consecutive_failures > 0)
                .order_by(CompanyState.consecutive_failures.desc())
            ).all()
            return {
                "total_jobs_tracked": total_jobs,
                "new_jobs": new_jobs,
                "matched": matched,
                "notifications_sent": notified,
                "matches_by_company": [{"company": c, "count": n} for c, n in by_company],
                "failing_companies": [
                    {"slug": f.slug, "failures": f.consecutive_failures, "error": f.last_error}
                    for f in failing
                ],
            }

    async def stats(self, since: datetime) -> Dict[str, Any]:
        return await asyncio.to_thread(self._stats_sync, since)

    def _all_jobs_sync(self, min_score: int, limit: int) -> List[Dict[str, Any]]:
        with self.session() as s:
            rows = s.scalars(
                select(Job)
                .where(Job.score >= min_score)
                .order_by(Job.first_seen_at.desc())
                .limit(limit)
            ).all()
            return [
                {
                    "company": r.company_name,
                    "company_slug": r.company_slug,
                    "title": r.title,
                    "department": r.department,
                    "location": r.location,
                    "url": r.url,
                    "score": r.score,
                    "matched": r.matched,
                    "employment_type": r.employment_type,
                    "work_mode": r.work_mode,
                    "source_platform": r.source_platform,
                    "posted_at": _aware(r.posted_at).isoformat() if r.posted_at else None,
                    "first_seen_at": _aware(r.first_seen_at).isoformat()
                    if r.first_seen_at
                    else None,
                    "closed": r.closed_at is not None,
                    "reasons": json.loads(r.match_reasons) if r.match_reasons else [],
                }
                for r in rows
            ]

    async def all_jobs(self, min_score: int = 0, limit: int = 10000) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._all_jobs_sync, min_score, limit)

    def _reset_alerts_sync(self) -> Dict[str, int]:
        """Forget which postings have been seen and alerted, keep seeded flags.

        Used after a filter change, to re-apply the new rules to postings that
        are already open. Both halves matter:

        * clearing ``jobs`` makes the next scan treat every open posting as
          new, so it is re-fetched, re-scored under the current config and can
          notify again;
        * keeping ``company_state.seeded`` is what stops that from being
          silent — an unseeded company records its whole board without
          notifying, which would be the exact opposite of the intent.
        """
        with self.session() as s:
            jobs = s.execute(delete(Job)).rowcount or 0
            notes = s.execute(delete(Notification)).rowcount or 0
            kept = (
                s.scalar(
                    select(func.count())
                    .select_from(CompanyState)
                    .where(CompanyState.seeded.is_(True))
                )
                or 0
            )
            s.commit()
        return {"jobs_cleared": int(jobs), "notifications_cleared": int(notes),
                "companies_still_seeded": int(kept)}

    async def reset_alerts(self) -> Dict[str, int]:
        return await asyncio.to_thread(self._reset_alerts_sync)

    def _purge_sync(self, older_than_days: int) -> int:
        cutoff = utcnow() - timedelta(days=older_than_days)
        with self.session() as s:
            # Only drop closed, unmatched postings; matched history is the
            # record of what the system actually found for you.
            result = s.execute(
                delete(Job)
                .where(Job.closed_at.is_not(None))
                .where(Job.closed_at < cutoff)
                .where(Job.matched.is_(False))
            )
            s.commit()
            return int(result.rowcount or 0)

    async def purge_stale(self, older_than_days: Optional[int] = None) -> int:
        days = older_than_days or self.settings.stale_after_days
        return await asyncio.to_thread(self._purge_sync, days)

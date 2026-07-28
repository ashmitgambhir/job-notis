"""Notification dispatch: dedupe, throttle, retry.

The "never notify twice" guarantee lives here and in the database. Before any
send, the dispatcher claims a ``(dedupe_key, channel)`` row protected by a
unique constraint — so even if two scanner instances race on the same posting,
exactly one wins the claim and the other silently backs off.

The dedupe key includes the posting's appearance count, which is what lets a
genuine re-post (closed, then reopened) notify again while a repeat of the same
appearance cannot.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

from ..config.schema import NotificationsConfig
from ..core.models import JobPosting, MatchResult, utcnow
from ..errors import NotificationError
from ..net.client import HttpClient
from ..storage.repository import Repository
from .channels import Channel, build_channels
from .formatter import Alert

log = structlog.get_logger(__name__)


class Dispatcher:
    def __init__(
        self,
        config: NotificationsConfig,
        repo: Repository,
        http: HttpClient,
        timezone_name: str = "America/Chicago",
    ) -> None:
        self.config = config
        self.repo = repo
        self.timezone_name = timezone_name
        self.channels: List[Channel] = build_channels(config, http)
        self.sent = 0
        self.failed = 0
        self.suppressed = 0

    # ------------------------------------------------------------------ #

    def active_channels(self) -> List[Channel]:
        return [c for c in self.channels if c.enabled]

    def configuration_problems(self) -> Dict[str, str]:
        """Enabled channels that cannot actually send. Surfaced by ``doctor``."""
        problems = {}
        for channel in self.active_channels():
            problem = channel.validate()
            if problem:
                problems[channel.name] = problem
        return problems

    def _in_quiet_hours(self, now: datetime) -> bool:
        start, end = self.config.quiet_hours_start, self.config.quiet_hours_end
        if not start or not end:
            return False
        try:
            from zoneinfo import ZoneInfo

            local = now.astimezone(ZoneInfo(self.timezone_name))
        except Exception:
            local = now
        current = local.hour * 60 + local.minute

        def to_minutes(value: str) -> int:
            hours, _, minutes = value.partition(":")
            return int(hours) * 60 + int(minutes or 0)

        start_m, end_m = to_minutes(start), to_minutes(end)
        # Windows that cross midnight ("23:00" to "07:00") need the OR form.
        if start_m <= end_m:
            return start_m <= current < end_m
        return current >= start_m or current < end_m

    @staticmethod
    def dedupe_key(posting: JobPosting, appearance: int) -> str:
        return f"{posting.job_key}#{appearance}"

    # ------------------------------------------------------------------ #

    async def dispatch(
        self, posting: JobPosting, match: MatchResult, appearance: int = 1
    ) -> Tuple[int, int]:
        """Send one match to every eligible channel. Returns ``(sent, failed)``."""
        now = utcnow()
        alert = Alert(posting=posting, match=match, detected_at=now,
                      timezone_name=self.timezone_name)
        key = self.dedupe_key(posting, appearance)
        quiet = self._in_quiet_hours(now)

        sent = failed = 0
        for channel in self.active_channels():
            if match.score < channel.min_score:
                continue
            # Quiet hours only hold back the ordinary matches; an exceptional
            # score still goes out, because being first is the whole point.
            if quiet and match.score < self.config.quiet_hours_override_score:
                self.suppressed += 1
                log.debug("notification_quiet_hours", channel=channel.name, score=match.score)
                continue
            if await self._over_rate_limit(channel):
                self.suppressed += 1
                log.warning("notification_rate_limited", channel=channel.name,
                            limit=channel.max_per_hour)
                continue

            # The claim is the dedupe guarantee: unique on (dedupe_key, channel).
            if not await self.repo.claim_notification(
                posting.job_key, channel.name, key, match.score
            ):
                log.debug("notification_already_sent", channel=channel.name, job=posting.job_key)
                continue

            ok, attempts, error = await self._deliver(channel, alert)
            await self.repo.finish_notification(key, channel.name, ok, attempts, error)
            if ok:
                sent += 1
                self.sent += 1
                log.info(
                    "notification_sent",
                    channel=channel.name,
                    company=posting.company_slug,
                    title=posting.title[:70],
                    score=match.score,
                    attempts=attempts,
                )
            else:
                failed += 1
                self.failed += 1
                log.error("notification_failed", channel=channel.name, error=error,
                          attempts=attempts)
                # Free the claim so a later cycle can try this posting again;
                # a failed send should not permanently consume the one chance.
                await self.repo.release_notification(key, channel.name)

        return sent, failed

    async def _over_rate_limit(self, channel: Channel) -> bool:
        recent = await self.repo.channel_sent_last_hour(channel.name)
        return recent >= channel.max_per_hour

    async def _deliver(self, channel: Channel, alert: Alert) -> Tuple[bool, int, Optional[str]]:
        """Send with bounded retries. Permanent failures skip the backoff."""
        last_error: Optional[str] = None
        for attempt in range(1, self.config.max_delivery_attempts + 1):
            try:
                await channel.send(alert)
                return True, attempt, None
            except NotificationError as exc:
                last_error = str(exc)
                if exc.permanent:
                    return False, attempt, f"{last_error} (not retryable)"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < self.config.max_delivery_attempts:
                delay = min(30.0, 1.5 * (2 ** (attempt - 1))) * (0.5 + random.random())
                await asyncio.sleep(delay)
        return False, self.config.max_delivery_attempts, last_error

    # ------------------------------------------------------------------ #

    async def send_alert(self, key: str, subject: str, body: str) -> bool:
        """Self-monitoring alert (dead company, stalled scans, delivery failures).

        Rate-limited by a DB-backed cooldown so a persistently broken company
        cannot turn into an hourly email.
        """
        if not self.config.alerts.enabled:
            return False
        if not await self.repo.should_alert(key, self.config.alerts.cooldown_seconds):
            return False

        html_body = f"<pre style='font-family:ui-monospace,monospace'>{body}</pre>"
        delivered = False
        for channel in self.active_channels():
            if channel.name not in self.config.alerts.channels:
                continue
            try:
                await channel.send_raw(f"[jobmonitor] {subject}", html_body, body)
                delivered = True
            except (NotificationError, NotImplementedError) as exc:
                log.warning("alert_delivery_failed", channel=channel.name, error=str(exc))
        return delivered

    async def send_digest(self, subject: str, html_body: str, text_body: str) -> bool:
        delivered = False
        for channel in self.active_channels():
            if channel.name != "email":
                continue
            try:
                await channel.send_raw(subject, html_body, text_body)
                delivered = True
            except NotificationError as exc:
                log.warning("digest_failed", error=str(exc))
        return delivered

    def stats(self) -> Dict[str, Any]:
        return {
            "sent": self.sent,
            "failed": self.failed,
            "suppressed": self.suppressed,
            "active_channels": [c.name for c in self.active_channels()],
        }


async def dispatch_many(
    dispatcher: Dispatcher, items: Sequence[Tuple[JobPosting, MatchResult, int]]
) -> Tuple[int, int]:
    """Send a batch, highest score first so the best match arrives first."""
    ordered = sorted(items, key=lambda t: -t[1].score)
    sent = failed = 0
    for posting, match, appearance in ordered:
        s, f = await dispatcher.dispatch(posting, match, appearance)
        sent += s
        failed += f
    return sent, failed

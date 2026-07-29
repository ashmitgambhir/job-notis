"""Notification formatting, throttling and dedupe."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jobmonitor.core.models import EmploymentType, MatchResult, WorkMode, utcnow
from jobmonitor.net.client import HttpClient
from jobmonitor.notify.channels import Channel
from jobmonitor.notify.dispatcher import Dispatcher
from jobmonitor.notify.formatter import Alert, humanize_age
from jobmonitor.storage.repository import Repository


@pytest.fixture
def alert(posting_factory):
    posting = posting_factory(posted_at=utcnow() - timedelta(minutes=3))
    posting.employment_type = EmploymentType.INTERN
    posting.work_mode = WorkMode.ONSITE
    return Alert(
        posting=posting,
        match=MatchResult(
            score=97,
            matched=True,
            reasons=["Mechanical Engineering", "Internship", "Manufacturing"],
        ),
        detected_at=utcnow(),
    )


class TestHumanizeAge:
    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(seconds=20), "just now"),
            (timedelta(minutes=3), "3 minutes ago"),
            (timedelta(minutes=1), "1 minute ago"),
            (timedelta(hours=5), "5 hours ago"),
            (timedelta(days=2), "2 days ago"),
        ],
    )
    def test_phrasing(self, delta, expected):
        assert humanize_age(utcnow() - delta) == expected

    def test_unknown_when_missing(self):
        assert humanize_age(None) == "unknown"


class TestFormatting:
    def test_sms_is_short_and_has_the_link(self, alert):
        text = alert.sms_text()
        assert alert.posting.url in text
        assert "97%" in text
        # SMS bills per 160-char segment; keep it to a couple.
        assert len(text) < 320

    def test_plain_text_has_every_required_field(self, alert):
        text = alert.plain_text()
        for expected in [
            "Company:", "Title:", "Department:", "Location:", "Work Mode:",
            "Type:", "Posted:", "Detected:", "Match Score:", "Apply:",
        ]:
            assert expected in text
        assert "Mechanical Engineering" in text

    def test_email_html_escapes_input(self, posting_factory):
        posting = posting_factory(title='Engineer <script>alert("x")</script>')
        html = Alert(posting, MatchResult(score=80, matched=True), utcnow()).email_html()
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_webhook_payload_shape(self, alert):
        payload = alert.webhook_payload()
        assert payload["event"] == "job.matched"
        assert payload["score"] == 97
        assert payload["url"] == alert.posting.url
        assert payload["employment_type"] == "intern"

    def test_telegram_escapes_html(self, posting_factory):
        posting = posting_factory(title="A & B <Engineer>")
        text = Alert(posting, MatchResult(score=80, matched=True), utcnow()).telegram_text()
        assert "&amp;" in text and "&lt;Engineer&gt;" in text

    def test_bad_timezone_does_not_raise(self, alert):
        alert.timezone_name = "Not/AZone"
        assert alert.local_detected


class RecordingChannel(Channel):
    """Test double that records what it was asked to send."""

    name = "recording"

    def __init__(self, min_score: int = 75, fail_times: int = 0):
        self.sent = []
        self._min_score = min_score
        self.fail_times = fail_times
        self.attempts = 0

    @property
    def enabled(self) -> bool:
        return True

    @property
    def min_score(self) -> int:
        return self._min_score

    @property
    def max_per_hour(self) -> int:
        return 100

    async def send(self, alert) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("transient")
        self.sent.append(alert)


@pytest.fixture
async def dispatcher(settings, config):
    repo = Repository(settings.database)
    await repo.initialize()
    async with HttpClient(settings.http) as http:
        d = Dispatcher(config.notifications, repo, http, "America/Chicago")
        d.config.max_delivery_attempts = 3
        yield d
    await repo.close()


class TestDispatcher:
    @pytest.mark.asyncio
    async def test_sends_once_only(self, dispatcher, posting_factory):
        channel = RecordingChannel()
        dispatcher.channels = [channel]
        posting = posting_factory()
        match = MatchResult(score=90, matched=True)

        assert await dispatcher.dispatch(posting, match) == (1, 0)
        # The same posting must never produce a second notification.
        assert await dispatcher.dispatch(posting, match) == (0, 0)
        assert len(channel.sent) == 1

    @pytest.mark.asyncio
    async def test_respects_channel_min_score(self, dispatcher, posting_factory):
        channel = RecordingChannel(min_score=95)
        dispatcher.channels = [channel]
        await dispatcher.dispatch(posting_factory(), MatchResult(score=80, matched=True))
        assert not channel.sent

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self, dispatcher, posting_factory):
        channel = RecordingChannel(fail_times=2)
        dispatcher.channels = [channel]
        sent, failed = await dispatcher.dispatch(posting_factory(), MatchResult(90, True))
        assert (sent, failed) == (1, 0)
        assert channel.attempts == 3

    @pytest.mark.asyncio
    async def test_exhausted_retries_free_the_claim(self, dispatcher, posting_factory):
        """A failed send must not consume the posting's one chance."""
        channel = RecordingChannel(fail_times=99)
        dispatcher.channels = [channel]
        posting = posting_factory()

        sent, failed = await dispatcher.dispatch(posting, MatchResult(90, True))
        assert (sent, failed) == (0, 1)

        channel.fail_times = 0
        sent, failed = await dispatcher.dispatch(posting, MatchResult(90, True))
        assert (sent, failed) == (1, 0)

    @pytest.mark.asyncio
    async def test_quiet_hours_suppress_ordinary_matches(self, dispatcher, posting_factory):
        dispatcher.config.quiet_hours_start = "00:00"
        dispatcher.config.quiet_hours_end = "23:59"
        dispatcher.config.quiet_hours_override_score = 95
        channel = RecordingChannel()
        dispatcher.channels = [channel]

        await dispatcher.dispatch(posting_factory(), MatchResult(80, True))
        assert not channel.sent

    @pytest.mark.asyncio
    async def test_quiet_hours_yield_to_an_exceptional_match(self, dispatcher, posting_factory):
        dispatcher.config.quiet_hours_start = "00:00"
        dispatcher.config.quiet_hours_end = "23:59"
        dispatcher.config.quiet_hours_override_score = 95
        channel = RecordingChannel()
        dispatcher.channels = [channel]

        await dispatcher.dispatch(posting_factory(), MatchResult(98, True))
        assert len(channel.sent) == 1

    @pytest.mark.asyncio
    async def test_hourly_cap_stops_a_flood(self, dispatcher, posting_factory):
        class Capped(RecordingChannel):
            @property
            def max_per_hour(self) -> int:
                return 2

        channel = Capped()
        dispatcher.channels = [channel]
        for i in range(5):
            await dispatcher.dispatch(
                posting_factory(external_id=str(i)), MatchResult(90, True)
            )
        assert len(channel.sent) == 2
        assert dispatcher.suppressed == 3


class TestQuietHoursWindow:
    @pytest.mark.parametrize(
        "start,end,hour,expected",
        [
            ("23:00", "07:00", 2, True),    # crosses midnight
            ("23:00", "07:00", 12, False),
            ("09:00", "17:00", 12, True),   # same-day window
            ("09:00", "17:00", 20, False),
        ],
    )
    def test_window_maths(self, dispatcher, start, end, hour, expected):
        from datetime import datetime, timezone

        dispatcher.config.quiet_hours_start = start
        dispatcher.config.quiet_hours_end = end
        dispatcher.timezone_name = "UTC"
        now = datetime(2026, 7, 28, hour, 30, tzinfo=timezone.utc)
        assert dispatcher._in_quiet_hours(now) is expected


class TestEmailFilteringHeaders:
    """Alerts are sent from an address to itself, so they land in both Inbox
    and Sent with nothing to distinguish them from real mail. These headers
    are what a mail rule can key on.
    """

    def _build(self, config):
        from jobmonitor.notify.channels import EmailChannelImpl

        channel = EmailChannelImpl.__new__(EmailChannelImpl)
        channel.config = config
        sent = {}

        import smtplib

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, **k): pass
            def login(self, *a): pass
            def send_message(self, msg): sent["msg"] = msg

        smtplib.SMTP = FakeSMTP  # type: ignore[misc]
        channel._send_smtp("[97%] Caterpillar — Intern", "<p>hi</p>", "hi")
        return sent["msg"]

    def test_headers_are_present(self, config, monkeypatch):
        import smtplib

        original = smtplib.SMTP
        try:
            cfg = config.notifications.email.model_copy(
                update={
                    "enabled": True,
                    "smtp_host": "smtp.example.com",
                    "smtp_username": "me@example.com",
                    "smtp_password": "x",
                    "from_address": "me@example.com",
                    "to_addresses": ["me+jobs@example.com"],
                }
            )
            msg = self._build(cfg)
        finally:
            smtplib.SMTP = original

        assert msg["X-JobMonitor"] == "alert"
        assert "jobmonitor" in msg["List-Id"]
        assert msg["Auto-Submitted"] == "auto-generated"
        # Plus-addressing survives into the envelope.
        assert "+jobs@" in msg["To"]

"""End-to-end tests: config loading, HTTP behaviour, and a full scan cycle.

The scan-cycle test wires real components together — repository, scorer,
dispatcher, orchestrator — against a mocked Greenhouse board, so it exercises
the same path production uses.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from jobmonitor.config.loader import _interpolate, load_config
from jobmonitor.config.schema import HttpSettings
from jobmonitor.core.orchestrator import Orchestrator
from jobmonitor.errors import ConfigError, PermanentError, TransientError
from jobmonitor.matching.scorer import Scorer
from jobmonitor.net.client import HttpClient
from jobmonitor.notify.dispatcher import Dispatcher
from jobmonitor.storage.repository import Repository
from tests.test_notify import RecordingChannel


def _recent(days_ago: int = 1) -> str:
    """An ISO timestamp near now.

    Fixtures must not hardcode absolute dates. `filters.max_posting_age_days`
    discards anything older than 30 days, so a literal date silently turns a
    passing test into a failing one once enough time passes — which is exactly
    what happened to the scan-cycle test after it sat for three months.
    """
    from datetime import timedelta

    from jobmonitor.core.models import utcnow

    return (utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestConfigInterpolation:
    def test_substitutes_env(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret-value")
        assert _interpolate("${MY_TOKEN}") == "secret-value"

    def test_uses_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        assert _interpolate("${NOPE:-fallback}") == "fallback"

    def test_unset_without_default_becomes_none(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        assert _interpolate("${NOPE}") is None

    def test_empty_string_triggers_the_default(self, monkeypatch):
        """Set-but-empty must behave like unset, as POSIX `:-` does.

        GitHub Actions materialises a missing secret as an empty string, so
        `LLM_ENABLED: ${{ secrets.LLM_ENABLED }}` exports `LLM_ENABLED=""`.
        Treating that as a real value made `${LLM_ENABLED:-false}` resolve to
        None and every boolean field failed validation in CI.
        """
        monkeypatch.setenv("LLM_ENABLED", "")
        assert _interpolate("${LLM_ENABLED:-false}") == "false"

    def test_empty_string_without_default_still_none(self, monkeypatch):
        monkeypatch.setenv("SOME_TOKEN", "")
        assert _interpolate("${SOME_TOKEN}") is None

    def test_real_value_still_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("LLM_ENABLED", "true")
        assert _interpolate("${LLM_ENABLED:-false}") == "true"

    def test_config_loads_with_all_secrets_empty(self, monkeypatch):
        """Reproduces the CI failure end-to-end.

        Every secret referenced by the workflow is exported empty, exactly as
        a fresh GitHub repository with none of them configured would.
        """
        for name in (
            "LLM_ENABLED", "SMS_ENABLED", "EMAIL_ENABLED", "SLACK_ENABLED",
            "DISCORD_ENABLED", "TELEGRAM_ENABLED", "WEBHOOK_ENABLED",
            "DAILY_DIGEST_ENABLED", "WEEKLY_DIGEST_ENABLED", "EMAIL_PROVIDER",
            "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "ALERT_EMAIL",
            "ALERT_FROM_EMAIL", "ALERT_SMS_GATEWAY", "ANTHROPIC_API_KEY",
            "DATABASE_URL", "LOG_LEVEL", "LOG_FORMAT", "HEALTH_PORT",
        ):
            monkeypatch.setenv(name, "")
        config = load_config()
        assert config.settings.matching.llm.enabled is False
        assert config.notifications.email.enabled is False
        assert config.notifications.sms.enabled is False
        # Falling back to the documented defaults, not to None.
        assert config.settings.database.url.startswith("sqlite")
        assert config.settings.logging.level == "INFO"

    def test_unset_entries_are_dropped_from_lists(self, monkeypatch):
        """An unset recipient means "channel off", not "a recipient named null"."""
        monkeypatch.delenv("NOPE", raising=False)
        assert _interpolate(["a", "${NOPE}", "b"]) == ["a", "b"]

    def test_recurses_into_nested_structures(self, monkeypatch):
        monkeypatch.setenv("HOST", "example.com")
        assert _interpolate({"a": {"b": ["${HOST}"]}}) == {"a": {"b": ["example.com"]}}


class TestShippedConfig:
    """The config that ships in the repo must actually load."""

    def test_loads_and_validates(self):
        config = load_config()
        assert config.enabled_companies()
        assert config.settings.matching.threshold > 0
        assert config.roles.families

    def test_weights_are_normalised(self):
        assert abs(sum(load_config().roles.weights.values()) - 1.0) < 1e-9

    def test_company_slugs_are_unique(self):
        slugs = [c.slug for c in load_config().companies.companies]
        assert len(slugs) == len(set(slugs))

    def test_every_connector_name_is_registered(self):
        from jobmonitor.connectors.registry import REGISTRY

        for company in load_config().companies.companies:
            assert company.connector == "auto" or company.connector in REGISTRY

    def test_exclusion_patterns_compile(self):
        import re

        config = load_config()
        for pattern in config.filters.exclude_title_patterns:
            re.compile(pattern)
        for pattern in config.filters.penalty_patterns:
            re.compile(pattern)

    def test_missing_directory_raises_config_error(self):
        with pytest.raises(ConfigError):
            load_config("does/not/exist")


class TestHttpClient:
    @pytest.fixture
    def settings(self):
        return HttpSettings(
            max_retries=2, backoff_base_seconds=0.01, cache_ttl_seconds=60,
            requests_per_second_per_host=1000, http2=False,
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_retries_5xx_then_succeeds(self, settings):
        route = respx.get("https://x.test/a").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        async with HttpClient(settings) as client:
            data, _ = await client.get_json("https://x.test/a")
        assert data == {"ok": True}
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self, settings):
        respx.get("https://x.test/b").mock(return_value=httpx.Response(500))
        async with HttpClient(settings) as client:
            with pytest.raises(TransientError):
                await client.get_json("https://x.test/b")

    @respx.mock
    @pytest.mark.asyncio
    async def test_4xx_is_permanent_and_not_retried(self, settings):
        route = respx.get("https://x.test/c").mock(return_value=httpx.Response(404))
        async with HttpClient(settings) as client:
            with pytest.raises(PermanentError):
                await client.get_json("https://x.test/c")
        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_cache_serves_repeat_within_ttl(self, settings):
        route = respx.get("https://x.test/d").mock(
            return_value=httpx.Response(200, json={"n": 1})
        )
        async with HttpClient(settings) as client:
            await client.get_json("https://x.test/d")
            _, cached = await client.get_json("https://x.test/d")
        assert cached is True
        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_query_params_are_part_of_the_cache_key(self, settings):
        """Pagination varies only the query string.

        Keying the cache on the URL alone made page 2 a cache hit for page 1,
        so every paginated connector read one page and then repeated it until
        the page limit ran out. AWS reported 67 postings instead of 402.
        """
        route = respx.get("https://x.test/list").mock(
            side_effect=lambda req: httpx.Response(
                200, json={"page": req.url.params.get("offset")}
            )
        )
        async with HttpClient(settings) as client:
            first, _ = await client.get_json("https://x.test/list", params={"offset": 0})
            second, cached = await client.get_json(
                "https://x.test/list", params={"offset": 100}
            )
        assert first != second
        assert cached is False
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_identical_params_still_cache(self, settings):
        route = respx.get("https://x.test/same").mock(
            return_value=httpx.Response(200, json={"n": 1})
        )
        async with HttpClient(settings) as client:
            await client.get_json("https://x.test/same", params={"a": 1, "b": 2})
            # Different dict order, same query — must still be one request.
            _, cached = await client.get_json("https://x.test/same", params={"b": 2, "a": 1})
        assert cached is True
        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_gzip_response_survives_the_cache(self, settings):
        """Caching decoded bytes alongside Content-Encoding broke replay.

        httpx tried to gunzip already-decompressed content and raised
        "incorrect header check" on the second read.
        """
        import gzip

        body = gzip.compress(b'{"ok": true}')
        respx.get("https://x.test/gz").mock(
            return_value=httpx.Response(
                200,
                content=body,
                headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            )
        )
        async with HttpClient(settings) as client:
            first, _ = await client.get_json("https://x.test/gz")
            second, cached = await client.get_json("https://x.test/gz")
        assert first == second == {"ok": True}
        assert cached is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_304_reuses_cached_body(self, settings):
        settings.cache_ttl_seconds = 0
        respx.get("https://x.test/e").mock(
            side_effect=[
                httpx.Response(200, json={"v": 1}, headers={"ETag": '"abc"'}),
                httpx.Response(304),
            ]
        )
        async with HttpClient(settings) as client:
            await client.get_json("https://x.test/e")
            data, cached = await client.get_json("https://x.test/e")
        assert data == {"v": 1}
        assert cached is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_html_where_json_expected_is_transient(self, settings):
        respx.get("https://x.test/f").mock(
            return_value=httpx.Response(
                200, text="<html>maintenance</html>",
                headers={"Content-Type": "text/html"},
            )
        )
        async with HttpClient(settings) as client:
            with pytest.raises(TransientError, match="expected JSON"):
                await client.get_json("https://x.test/f")


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_bucket_limits_throughput(self):
        import time

        from jobmonitor.net.ratelimit import TokenBucket

        bucket = TokenBucket(rate=20.0, burst=2)
        start = time.monotonic()
        for _ in range(5):
            await bucket.acquire()
        # 2 free from the burst, 3 more at 20/s ≈ 0.15s.
        assert time.monotonic() - start >= 0.1


class TestScanCycle:
    @respx.mock
    @pytest.mark.asyncio
    async def test_seeding_then_notifying(self, config, settings):
        """First scan records silently; the next one notifies about new postings."""
        board = "https://boards-api.greenhouse.io/v1/boards/caterpillar/jobs"
        respx.get(board).mock(
            return_value=httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 1,
                            "title": "Mechanical Engineering Intern",
                            "absolute_url": "https://x/1",
                            "location": {"name": "Peoria, IL"},
                            "updated_at": _recent(2),
                        }
                    ]
                },
            )
        )
        respx.get(f"{board}/1").mock(
            return_value=httpx.Response(
                200,
                json={"content": "BS Mechanical Engineering student. SolidWorks, FEA, CAD."},
            )
        )
        # Anduril is in the fixture config but not mocked; its failure must
        # not stop Caterpillar from being scanned.
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs"
        ).mock(return_value=httpx.Response(500))

        repo = Repository(settings.database)
        await repo.initialize()
        async with HttpClient(settings.http) as http:
            scorer = Scorer(config)
            dispatcher = Dispatcher(config.notifications, repo, http)
            channel = RecordingChannel(min_score=75)
            dispatcher.channels = [channel]
            orchestrator = Orchestrator(config, repo, http, scorer, dispatcher)

            first = await orchestrator.run_cycle()
            assert first.jobs_seen == 1
            assert first.jobs_new == 1
            # Seeding must not page you about an existing board.
            assert first.notifications_sent == 0
            assert not channel.sent
            # One company failed; the other still completed.
            assert len(first.failures) == 1
            assert any(c.ok for c in first.companies)

            respx.get(board).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "jobs": [
                            {
                                "id": 1,
                                "title": "Mechanical Engineering Intern",
                                "absolute_url": "https://x/1",
                                "location": {"name": "Peoria, IL"},
                                "updated_at": _recent(2),
                            },
                            {
                                "id": 2,
                                "title": "Mechanical Design Engineer",
                                "absolute_url": "https://x/2",
                                "location": {"name": "Peoria, IL"},
                                "updated_at": _recent(1),
                            },
                        ]
                    },
                )
            )
            respx.get(f"{board}/2").mock(
                return_value=httpx.Response(
                    200,
                    json={"content": "Design mechanical assemblies. SolidWorks, GD&T, FEA."},
                )
            )

            second = await orchestrator.run_cycle()
            assert second.jobs_new == 1
            assert second.notifications_sent == 1
            assert len(channel.sent) == 1
            assert channel.sent[0].posting.external_id == "2"

            third = await orchestrator.run_cycle()
            # Nothing new: nothing sent, and no duplicate for posting 2.
            assert third.jobs_new == 0
            assert len(channel.sent) == 1
        await repo.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_company_failure_is_isolated(self, config, settings):
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/caterpillar/jobs"
        ).mock(return_value=httpx.Response(500))
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs"
        ).mock(return_value=httpx.Response(500))

        repo = Repository(settings.database)
        await repo.initialize()
        async with HttpClient(settings.http) as http:
            orchestrator = Orchestrator(
                config, repo, http, Scorer(config),
                Dispatcher(config.notifications, repo, http),
            )
            result = await orchestrator.run_cycle()
        await repo.close()

        # Every company failed, but the cycle itself completed cleanly.
        assert len(result.failures) == 2
        assert result.failure_ratio == 1.0


class TestHealthAlerting:
    """Alerts must fire on regressions, not on unconfigured companies.

    A company with no resolved endpoint fails every cycle by construction.
    Counting those toward the failure ratio produced an alert email and text
    every hour saying 65% of companies were failing, which is both untrue as
    an incident and the fastest way to train someone to ignore alerts.
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_never_working_companies_do_not_alert(self, config, settings):
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/caterpillar/jobs"
        ).mock(return_value=httpx.Response(500))
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs"
        ).mock(return_value=httpx.Response(500))

        config.settings.health.enabled = True
        repo = Repository(settings.database)
        await repo.initialize()
        async with HttpClient(settings.http) as http:
            dispatcher = Dispatcher(config.notifications, repo, http)
            sent = []

            async def capture(key, subject, body):
                sent.append(subject)
                return True

            dispatcher.send_alert = capture
            orchestrator = Orchestrator(
                config, repo, http, Scorer(config), dispatcher
            )
            result = await orchestrator.run_cycle()

            # Everything failed, but nothing had ever worked, so it is a
            # configuration gap rather than an incident.
            assert len(result.failures) == 2
            assert sent == []
        await repo.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_regression_after_a_success_does_alert(self, config, settings):
        board = "https://boards-api.greenhouse.io/v1/boards/caterpillar/jobs"
        respx.get(board).mock(
            return_value=httpx.Response(
                200,
                json={"jobs": [{
                    "id": 1, "title": "Mechanical Engineer",
                    "absolute_url": "https://x/1",
                    "location": {"name": "Peoria, IL"},
                    "updated_at": _recent(2),
                }]},
            )
        )
        respx.get(f"{board}/1").mock(
            return_value=httpx.Response(200, json={"content": "Design parts."})
        )
        respx.get(
            "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs"
        ).mock(return_value=httpx.Response(500))

        config.settings.health.enabled = True
        repo = Repository(settings.database)
        await repo.initialize()
        async with HttpClient(settings.http) as http:
            dispatcher = Dispatcher(config.notifications, repo, http)
            sent = []

            async def capture(key, subject, body):
                sent.append(subject)
                return True

            dispatcher.send_alert = capture
            orchestrator = Orchestrator(config, repo, http, Scorer(config), dispatcher)

            await orchestrator.run_cycle()   # caterpillar succeeds, is now known-good
            sent.clear()

            respx.get(board).mock(return_value=httpx.Response(500))
            await orchestrator.run_cycle()   # it regresses

            assert any("working companies just failed" in s for s in sent)
        await repo.close()

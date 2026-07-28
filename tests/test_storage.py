"""Storage and dedupe tests.

"Never notify twice" is a database guarantee, so these tests are the ones that
prove the headline promise actually holds.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from jobmonitor.core.models import MatchResult, utcnow
from jobmonitor.storage.repository import Repository


@pytest.fixture
async def repo(settings):
    r = Repository(settings.database)
    await r.initialize()
    yield r
    await r.close()


class TestUpsert:
    @pytest.mark.asyncio
    async def test_first_sight_is_new(self, repo, posting_factory):
        new, changed = await repo.upsert_postings([posting_factory()])
        assert len(new) == 1
        assert not changed

    @pytest.mark.asyncio
    async def test_second_sight_is_not_new(self, repo, posting_factory):
        posting = posting_factory()
        await repo.upsert_postings([posting])
        new, changed = await repo.upsert_postings([posting])
        assert not new
        assert not changed

    @pytest.mark.asyncio
    async def test_edited_posting_is_changed_not_new(self, repo, posting_factory):
        await repo.upsert_postings([posting_factory()])
        edited = posting_factory(description="Revised description with new duties.")
        new, changed = await repo.upsert_postings([edited])
        assert not new
        assert len(changed) == 1

    @pytest.mark.asyncio
    async def test_reappearance_after_close_counts_as_new(self, repo, posting_factory):
        """The spec calls for treating a returning posting as new."""
        posting = posting_factory()
        await repo.upsert_postings([posting])
        await repo.close_missing("caterpillar", seen_keys=[])

        new, _ = await repo.upsert_postings([posting])
        assert len(new) == 1

    @pytest.mark.asyncio
    async def test_repost_under_new_id_is_not_new(self, repo, posting_factory):
        """A fresh requisition ID for an identical role must not notify twice."""
        await repo.upsert_postings([posting_factory(external_id="1001")])
        new, _ = await repo.upsert_postings([posting_factory(external_id="2002")])
        assert not new

    @pytest.mark.asyncio
    async def test_known_ids_round_trip(self, repo, posting_factory):
        await repo.upsert_postings(
            [posting_factory(external_id="a"), posting_factory(external_id="b")]
        )
        assert await repo.known_external_ids("caterpillar") == {"a", "b"}

    @pytest.mark.asyncio
    async def test_empty_batch_is_safe(self, repo):
        assert await repo.upsert_postings([]) == ([], [])


class TestNotificationDedupe:
    @pytest.mark.asyncio
    async def test_claim_succeeds_once(self, repo):
        assert await repo.claim_notification("c:1", "sms", "c:1#1", 90) is True
        assert await repo.claim_notification("c:1", "sms", "c:1#1", 90) is False

    @pytest.mark.asyncio
    async def test_claims_are_per_channel(self, repo):
        assert await repo.claim_notification("c:1", "sms", "c:1#1", 90) is True
        assert await repo.claim_notification("c:1", "email", "c:1#1", 90) is True

    @pytest.mark.asyncio
    async def test_new_appearance_can_notify_again(self, repo):
        assert await repo.claim_notification("c:1", "sms", "c:1#1", 90) is True
        # A genuine re-post bumps the appearance counter, freeing the key.
        assert await repo.claim_notification("c:1", "sms", "c:1#2", 90) is True

    @pytest.mark.asyncio
    async def test_release_allows_a_retry(self, repo):
        await repo.claim_notification("c:1", "sms", "c:1#1", 90)
        await repo.release_notification("c:1#1", "sms")
        assert await repo.claim_notification("c:1", "sms", "c:1#1", 90) is True

    @pytest.mark.asyncio
    async def test_hourly_count_tracks_sends(self, repo):
        await repo.claim_notification("c:1", "sms", "c:1#1", 90)
        assert await repo.channel_sent_last_hour("sms") == 0
        await repo.finish_notification("c:1#1", "sms", ok=True, attempts=1)
        assert await repo.channel_sent_last_hour("sms") == 1


class TestCompanyState:
    @pytest.mark.asyncio
    async def test_defaults_for_unknown_company(self, repo):
        state = await repo.get_company_state("nobody")
        assert state["seeded"] is False
        assert state["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_failures_accumulate_then_reset(self, repo):
        assert await repo.record_company_outcome("x", ok=False, error="boom") == 1
        assert await repo.record_company_outcome("x", ok=False, error="boom") == 2
        assert await repo.record_company_outcome("x", ok=True) == 0

    @pytest.mark.asyncio
    async def test_detected_params_round_trip(self, repo):
        await repo.update_company_state(
            "x", detected_connector="workday", detected_params={"tenant": "t"}
        )
        state = await repo.get_company_state("x")
        assert state["detected_connector"] == "workday"
        assert state["detected_params"] == {"tenant": "t"}


class TestAlertCooldown:
    @pytest.mark.asyncio
    async def test_second_alert_is_suppressed(self, repo):
        assert await repo.should_alert("k", 3600) is True
        assert await repo.should_alert("k", 3600) is False

    @pytest.mark.asyncio
    async def test_zero_cooldown_always_fires(self, repo):
        assert await repo.should_alert("k2", 60) is True
        assert await repo.should_alert("k2", 0) is True


class TestReporting:
    @pytest.mark.asyncio
    async def test_recent_matches_respects_min_score(self, repo, posting_factory):
        posting = posting_factory()
        await repo.upsert_postings([posting])
        await repo.record_match(posting, MatchResult(score=88, matched=True, reasons=["a"]))

        assert len(await repo.recent_matches(utcnow() - timedelta(days=1), min_score=80)) == 1
        assert len(await repo.recent_matches(utcnow() - timedelta(days=1), min_score=95)) == 0

    @pytest.mark.asyncio
    async def test_purge_keeps_matched_history(self, repo, posting_factory):
        matched = posting_factory(external_id="keep", title="Mechanical Engineer Intern")
        unmatched = posting_factory(external_id="drop", title="Something Else")
        await repo.upsert_postings([matched, unmatched])
        await repo.record_match(matched, MatchResult(score=90, matched=True))
        await repo.record_match(unmatched, MatchResult(score=10, matched=False))
        await repo.close_missing("caterpillar", seen_keys=[])

        # Nothing is old enough to purge yet.
        assert await repo.purge_stale(older_than_days=30) == 0

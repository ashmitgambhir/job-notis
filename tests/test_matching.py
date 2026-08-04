"""Matching engine tests — the part where a bug costs you a job."""

from __future__ import annotations

import pytest

from jobmonitor.core.models import EmploymentType, WorkMode
from jobmonitor.matching.classify import (
    classify_employment_type,
    classify_work_mode,
    detect_clearance,
    detect_deadline,
    detect_sponsorship,
    enrich,
)
from jobmonitor.matching.normalize import contains_phrase, normalize, tokenize
from jobmonitor.matching.rules import RuleEngine
from jobmonitor.matching.scorer import Scorer


class TestNormalize:
    def test_expands_abbreviations(self):
        assert "senior" in normalize("Sr. Mechanical Engineer")
        assert "coop" in normalize("Co-Op Student")

    def test_engineering_collapses_to_engineer(self):
        # Both sides normalise, so config written as "Robotics Engineer"
        # still matches a posting titled "Robotics Engineering Intern".
        assert normalize("Robotics Engineering Intern") == "robotics engineer intern"

    def test_roman_numeral_levels(self):
        assert normalize("Engineer II") == "engineer 2"

    def test_phrase_matching_is_word_bounded(self):
        text = normalize("Mechanical Design Engineer")
        assert contains_phrase(text, "Design Engineer")
        assert not contains_phrase(normalize("Redesigned systems"), "design")

    def test_tokenize_drops_stopwords(self):
        assert "the" not in tokenize("The mechanical engineer role")


class TestClassify:
    def test_intern_from_title(self, posting_factory):
        p = posting_factory(title="Mechanical Engineering Intern")
        assert classify_employment_type(p) is EmploymentType.INTERN

    def test_coop_beats_intern(self, posting_factory):
        p = posting_factory(title="Engineering Co-Op")
        assert classify_employment_type(p) is EmploymentType.COOP

    def test_platform_flag_wins(self, posting_factory):
        p = posting_factory(title="Mechanical Engineer", raw={"is_intern": True})
        assert classify_employment_type(p) is EmploymentType.INTERN

    def test_title_beats_boilerplate(self, posting_factory):
        # A full-time posting mentioning the intern programme is not an
        # internship.
        p = posting_factory(
            title="Mechanical Engineer",
            description="Full time role. We also run a summer internship programme.",
        )
        assert classify_employment_type(p) is not EmploymentType.INTERN

    @pytest.mark.parametrize(
        "location,expected",
        [
            ("Remote - US", WorkMode.REMOTE),
            ("Hybrid - Chicago, IL", WorkMode.HYBRID),
            ("Peoria, IL", WorkMode.ONSITE),
        ],
    )
    def test_work_mode(self, posting_factory, location, expected):
        p = posting_factory(location=location, description="")
        assert classify_work_mode(p) is expected

    def test_clearance_required(self):
        assert detect_clearance("Must hold an active TS/SCI clearance.") is True

    def test_clearance_obtainable_is_not_a_barrier(self):
        text = "Must have the ability to obtain a security clearance."
        assert detect_clearance(text) is False

    def test_clearance_unknown_when_unmentioned(self):
        assert detect_clearance("Design mechanical parts.") is None

    def test_sponsorship_refused(self):
        assert detect_sponsorship("We are unable to sponsor visas.") is False

    def test_sponsorship_offered(self):
        assert detect_sponsorship("Visa sponsorship is available.") is True

    def test_deadline_extraction(self):
        deadline = detect_deadline("Please apply by March 15, 2026.")
        assert deadline is not None and deadline.year == 2026 and deadline.month == 3

    def test_enrich_populates_everything(self, posting_factory):
        p = enrich(
            posting_factory(
                title="Mechanical Engineering Intern",
                location="Remote - US",
                description="No sponsorship available. Apply by June 1, 2026.",
            )
        )
        assert p.employment_type is EmploymentType.INTERN
        assert p.work_mode is WorkMode.REMOTE
        assert p.offers_sponsorship is False
        assert p.deadline is not None


class TestExclusions:
    @pytest.fixture
    def engine(self, roles, filters):
        return RuleEngine(roles, filters)

    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer",
            "Backend Engineer",
            "Data Scientist",
            "Marketing Intern",
            "Director of Engineering",
            "Registered Nurse",
            "Principal Mechanical Engineer",
        ],
    )
    def test_excluded_titles(self, engine, posting_factory, title):
        assert engine.hard_exclusion(posting_factory(title=title)) is not None

    @pytest.mark.parametrize(
        "title",
        [
            "Mechanical Engineering Intern",
            "Manufacturing Engineer",
            "Engineer I",
            # Hardware-focused software survives the software exclusion.
            "Embedded Software Engineer for hardware validation",
        ],
    )
    def test_kept_titles(self, engine, posting_factory, title):
        p = posting_factory(title=title)
        enrich(p)
        assert engine.hard_exclusion(p) is None

    def test_denied_location(self, engine, posting_factory):
        p = posting_factory(location="Bangalore, India")
        enrich(p)
        assert "deny list" in (engine.hard_exclusion(p) or "")

    def test_us_state_counts_as_us(self, engine, posting_factory):
        p = posting_factory(location="Peoria, IL")
        enrich(p)
        assert engine.hard_exclusion(p) is None


class TestSeniority:
    @pytest.fixture
    def engine(self, roles, filters):
        return RuleEngine(roles, filters)

    def test_senior_marker_dominates_junior_marker(self, engine, posting_factory):
        """The regression that let "Senior Mechanical Engineer I" score 76.

        The title matches both `senior` (-70) and `engineer i` (+95). Taking
        the highest weight read it as a new-grad role; the most senior marker
        has to win.
        """
        senior = engine.score_seniority(posting_factory(title="Senior Mechanical Engineer I"))[0]
        junior = engine.score_seniority(posting_factory(title="Mechanical Engineer I"))[0]
        assert senior < junior
        assert senior < 30

    def test_description_does_not_sink_an_internship(self, engine, posting_factory):
        p = posting_factory(
            title="Mechanical Engineering Intern",
            description="You will work alongside senior engineers with 10+ years.",
        )
        assert engine.score_seniority(p)[0] >= 90


class TestScorer:
    @pytest.fixture
    def scorer(self, config):
        return Scorer(config)

    @pytest.mark.asyncio
    async def test_strong_match(self, scorer, posting_factory):
        result = await scorer.score(posting_factory())
        assert result.matched
        assert result.score >= 75
        assert result.reasons

    @pytest.mark.asyncio
    async def test_excluded_scores_zero(self, scorer, posting_factory):
        result = await scorer.score(posting_factory(title="Senior Software Engineer"))
        assert result.score == 0
        assert not result.matched
        assert result.excluded_by

    @pytest.mark.asyncio
    async def test_senior_role_does_not_match(self, scorer, posting_factory):
        result = await scorer.score(
            posting_factory(
                title="Senior Mechanical Engineer",
                description="Design assemblies. 7+ years experience required.",
            )
        )
        assert not result.matched

    @pytest.mark.asyncio
    async def test_preferred_location_bonus(self, scorer, posting_factory):
        here = await scorer.score(posting_factory(location="Peoria, IL"))
        elsewhere = await scorer.score(posting_factory(location="Phoenix, AZ"))
        assert here.score > elsewhere.score

    @pytest.mark.asyncio
    async def test_phd_requirement_penalised(self, scorer, posting_factory):
        plain = await scorer.score(posting_factory())
        phd = await scorer.score(
            posting_factory(description=posting_factory().description + " PhD required.")
        )
        assert phd.score < plain.score

    def test_prefilter_rejects_obvious_misses(self, scorer, posting_factory):
        assert not scorer.worth_detail_fetch(posting_factory(title="Data Scientist"))

    def test_prefilter_keeps_plausible_titles(self, scorer, posting_factory):
        # The prefilter must not discard a match before its description is read.
        assert scorer.worth_detail_fetch(
            posting_factory(title="Mechanical Design Engineer", description=None)
        )

    def test_score_is_bounded(self, scorer, posting_factory):
        estimate, _ = scorer.score_title_only(posting_factory())
        assert 0.0 <= estimate <= 100.0


class TestInternsOnly:
    """The shipped filter must pass internships and co-ops and nothing else.

    Full-time and new-grad roles were reaching the alerts because
    `employment_types` included them. A new-grad posting is a full-time job,
    not an internship.
    """

    @pytest.fixture
    def shipped(self):
        from jobmonitor.config.loader import load_config

        return load_config()

    def test_shipped_config_allows_only_intern_and_coop(self, shipped):
        assert set(shipped.filters.employment_types) == {"intern", "co-op"}

    @pytest.fixture
    def engine(self, shipped):
        return RuleEngine(shipped.roles, shipped.filters)

    @pytest.mark.parametrize(
        "title",
        [
            "Mechanical Design Engineer",
            "Engineer I",
            "Entry Level Manufacturing Engineer",
            "Mechanical Engineer II",
            "Associate Mechanical Engineer",
        ],
    )
    def test_non_intern_roles_are_excluded(self, engine, posting_factory, title):
        """Whether a role reads as full-time or new-grad, neither is an internship."""
        p = posting_factory(title=title, description="Design mechanical assemblies.")
        enrich(p)
        assert p.employment_type.value not in ("intern", "co-op")
        assert "employment type not wanted" in (engine.hard_exclusion(p) or "")

    @pytest.mark.parametrize(
        "title",
        [
            "Mechanical Engineering Intern",
            "Manufacturing Engineering Co-op",
            "2027 Mechanical Engineer Intern",
        ],
    )
    def test_intern_roles_survive(self, engine, posting_factory, title):
        p = posting_factory(title=title)
        enrich(p)
        assert engine.hard_exclusion(p) is None

    def test_type_filter_is_deferred_until_the_description_is_read(
        self, engine, posting_factory
    ):
        """An internship whose title omits the word must not die in the prefilter.

        Deere titles some student roles "Part Time Student - Product
        Engineering". Judged on the title alone that classifies as full time,
        so applying the filter before the detail fetch would discard it unread.
        """
        p = posting_factory(title="Product Engineering Analyst", description=None)
        p.detail_pending = True
        enrich(p)
        assert engine.hard_exclusion(p, defer_employment_type=True) is None
        # The final pass never defers, so an unconfirmed posting is dropped.
        assert engine.hard_exclusion(p) is not None

        # Once the description arrives and confirms full time, it is dropped.
        p.detail_pending = False
        p.description = "Full time salaried position. Five years of experience."
        enrich(p)
        assert "employment type not wanted" in (engine.hard_exclusion(p) or "")

    def test_deferred_posting_that_turns_out_to_be_an_internship_is_kept(
        self, engine, posting_factory
    ):
        p = posting_factory(title="Product Engineering Analyst", description=None)
        p.detail_pending = True
        enrich(p)
        assert engine.hard_exclusion(p, defer_employment_type=True) is None

        p.detail_pending = False
        p.description = "The intern will support product engineering. Currently enrolled."
        enrich(p)
        assert p.employment_type.value == "intern"
        assert engine.hard_exclusion(p) is None


class TestUnconfirmedTypeFailsClosed:
    """The leak that kept sending full-time roles after the interns-only change.

    `detail_pending` stays True whenever the per-cycle fetch budget runs out or
    a detail request fails — routine on a 19-company scan. The employment-type
    check was skipped in exactly those cases, so it failed *open*: the busier
    the cycle, the more full-time roles leaked through.
    """

    @pytest.fixture
    def scorer(self, config):
        strict = config.model_copy(deep=True)
        strict.filters.employment_types = ["intern", "co-op"]
        return Scorer(strict)

    @pytest.mark.asyncio
    async def test_full_time_role_without_a_description_is_not_sent(
        self, scorer, posting_factory
    ):
        p = posting_factory(title="Mechanical Design Engineer", description=None)
        p.detail_pending = True          # budget ran out; never fetched
        result = await scorer.score(p)
        assert not result.matched
        assert result.excluded_by

    @pytest.mark.asyncio
    async def test_intern_title_still_sends_without_a_description(
        self, scorer, posting_factory
    ):
        """Failing closed must not cost the obvious matches."""
        p = posting_factory(title="Mechanical Engineering Intern", description=None)
        p.detail_pending = True
        result = await scorer.score(p)
        assert result.matched

    def test_prefilter_still_defers_so_odd_titles_survive_to_the_fetch(
        self, scorer, posting_factory
    ):
        p = posting_factory(title="Part Time Student - Product Engineering",
                            description=None)
        p.detail_pending = True
        assert scorer.worth_detail_fetch(p)

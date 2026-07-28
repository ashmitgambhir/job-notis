"""Shared fixtures.

Tests never touch the network. Connectors are exercised against recorded
payload shapes via `respx`, and the matching tests run the real scorer against
an in-memory config so the scoring rules are genuinely covered.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

import pytest

from jobmonitor.config.schema import (
    AppConfig,
    CompaniesConfig,
    CompanyConfig,
    FiltersConfig,
    LocationFilter,
    NotificationsConfig,
    RolesConfig,
    SeniorityRule,
    Settings,
    TitleFamily,
)
from jobmonitor.core.models import JobPosting, utcnow


@pytest.fixture
def roles() -> RolesConfig:
    return RolesConfig(
        semantic_profile=[
            "Internship for a mechanical engineering student involving CAD, "
            "design, prototyping and testing of hardware.",
            "Entry level mechanical engineer designing components and running "
            "analysis and validation.",
        ],
        families=[
            TitleFamily(
                name="core_mechanical",
                weight=100,
                titles=["Mechanical Engineer", "Mechanical Engineering Intern",
                        "Mechanical Design Engineer"],
                keywords=["mechanical", "cad", "solidworks"],
            ),
            TitleFamily(
                name="manufacturing",
                weight=88,
                titles=["Manufacturing Engineer", "Process Engineer"],
                keywords=["manufacturing", "tooling", "lean"],
            ),
            TitleFamily(
                name="hardware_generic",
                weight=70,
                titles=["Engineering Intern", "Engineer I", "Associate Engineer"],
                keywords=["hardware", "engineering intern"],
            ),
        ],
        seniority=[
            SeniorityRule(name="internship", patterns=[r"\bintern(ship)?\b", r"\bco-?op\b"],
                          weight=100),
            SeniorityRule(name="new_grad",
                          patterns=[r"\bnew grad(uate)?\b", r"\bentry[ -]level\b",
                                    r"\bengineer\s+i\b"],
                          weight=95),
            SeniorityRule(name="senior",
                          patterns=[r"\bsenior\b", r"\bsr\.?\b", r"\bstaff\b",
                                    r"\bprincipal\b", r"\b[5-9]\+? years\b"],
                          weight=-70),
        ],
        degrees=["mechanical engineering", "engineering"],
        skills=["solidworks", "creo", "fea", "gd&t", "cnc", "matlab", "prototyping"],
    )


@pytest.fixture
def filters() -> FiltersConfig:
    return FiltersConfig(
        exclude_title_patterns=[
            r"(?i)\b(software|backend|frontend)\s+(engineer|developer|intern)\b"
            r"(?!.*\b(hardware|firmware|embedded|mechanical)\b)",
            r"(?i)\b(data scientist|accountant|marketing|recruiter)\b",
            r"(?i)\b(director|vice president|principal)\b",
            r"(?i)\bnurse\b",
        ],
        penalty_patterns={r"(?i)\b(phd|ph\.d\.) (required|degree required)\b": 25},
        exclude_departments=["human resources", "finance"],
        employment_types=["intern", "co-op", "new_grad", "full_time"],
        location=LocationFilter(
            deny=["India", "China"],
            preferred=["Illinois", "Peoria"],
            preferred_bonus=5,
            countries=["United States", "Remote"],
        ),
        max_posting_age_days=30,
    )


@pytest.fixture
def companies() -> CompaniesConfig:
    return CompaniesConfig(
        companies=[
            CompanyConfig(
                name="Caterpillar", slug="caterpillar", connector="greenhouse",
                params={"board_token": "caterpillar"}, verified=True,
            ),
            CompanyConfig(
                name="Anduril", slug="anduril", connector="greenhouse",
                params={"board_token": "andurilindustries"}, verified=True,
                priority="high",
            ),
        ]
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database={"url": f"sqlite:///{tmp_path}/test.db"},
        logging={"level": "WARNING", "file": None},
        # No response cache: tests re-mock the same URL between cycles and
        # must see the new payload.
        http={"cache_ttl_seconds": 0, "max_retries": 1, "backoff_base_seconds": 0.01,
              "requests_per_second_per_host": 1000, "http2": False},
        # Embeddings are slow to load and download a model; the lexical
        # fallback keeps unit tests fast and offline.
        matching={"threshold": 75, "review_threshold": 60,
                  "embeddings": {"enabled": False}},
        health={"enabled": False, "http_port": None},
    )


@pytest.fixture
def config(settings, companies, roles, filters) -> AppConfig:
    return AppConfig(
        settings=settings,
        companies=companies,
        roles=roles,
        filters=filters,
        notifications=NotificationsConfig(),
    )


@pytest.fixture
def posting_factory():
    def make(**kwargs: Any) -> JobPosting:
        defaults: Dict[str, Any] = {
            "company": "Caterpillar",
            "company_slug": "caterpillar",
            "external_id": "1001",
            "title": "Mechanical Engineering Intern",
            "url": "https://example.com/jobs/1001",
            "department": "Product Development",
            "location": "Peoria, IL",
            "description": (
                "Seeking a BS Mechanical Engineering student. Use SolidWorks "
                "and Creo for component design. Perform FEA and prototyping."
            ),
            "posted_at": utcnow() - timedelta(hours=2),
        }
        defaults.update(kwargs)
        return JobPosting(**defaults)

    return make

"""Domain objects shared by connectors, matcher, storage and notifiers.

Connectors normalise wildly different ATS payloads into :class:`JobPosting`;
everything downstream only ever sees this shape.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class EmploymentType(str, Enum):
    INTERN = "intern"
    COOP = "co-op"
    NEW_GRAD = "new_grad"
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    UNKNOWN = "unknown"


class WorkMode(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_WS = re.compile(r"\s+")


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    return _WS.sub(" ", text).strip()


@dataclass
class JobPosting:
    """A single posting, normalised across every ATS platform."""

    company: str
    company_slug: str
    # The ATS's own identifier. Stable within a company, not across companies.
    external_id: str
    title: str
    url: str

    department: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    posted_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    employment_type: EmploymentType = EmploymentType.UNKNOWN
    work_mode: WorkMode = WorkMode.UNKNOWN

    # Populated by the classifier, not the connector.
    requires_clearance: Optional[bool] = None
    offers_sponsorship: Optional[bool] = None
    deadline: Optional[datetime] = None

    source_platform: str = "unknown"
    # True when `description` has not been fetched yet. The orchestrator uses
    # this to decide whether a detail request is worth making.
    detail_pending: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    first_seen_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        self.title = _clean(self.title)
        self.department = _clean(self.department) or None
        self.location = _clean(self.location) or None

    @property
    def job_key(self) -> str:
        """Primary dedupe key: company + the ATS's own ID.

        Scoped by company because ATS IDs collide freely across employers.
        """
        return f"{self.company_slug}:{self.external_id}"

    @property
    def content_hash(self) -> str:
        """Hash of the material content.

        Used to spot a posting that was edited rather than replaced, and as a
        secondary dedupe key when a company reissues the same role under a new
        requisition ID.
        """
        payload = "|".join(
            [
                self.company_slug,
                self.title.lower(),
                (self.location or "").lower(),
                (self.department or "").lower(),
                (self.description or "")[:4000].lower(),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def dedupe_hash(self) -> str:
        """Title+location fingerprint, ignoring description.

        Catches the common case of a company re-posting an identical role with
        a fresh requisition ID, which would otherwise notify twice.
        """
        payload = f"{self.company_slug}|{self.title.lower()}|{(self.location or '').lower()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def searchable_text(self) -> str:
        return " ".join(
            p for p in [self.title, self.department, self.location, self.description] if p
        )

    def age_days(self) -> Optional[float]:
        if not self.posted_at:
            return None
        posted = self.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (utcnow() - posted).total_seconds() / 86400.0


@dataclass
class MatchResult:
    """Outcome of scoring one posting."""

    score: int
    matched: bool
    reasons: List[str] = field(default_factory=list)
    # Per-component contributions, kept for tuning and for the digest.
    components: Dict[str, float] = field(default_factory=dict)
    excluded_by: Optional[str] = None
    llm_used: bool = False
    llm_verdict: Optional[str] = None

    @property
    def top_reasons(self) -> List[str]:
        return self.reasons[:4]


@dataclass
class CompanyScanResult:
    """What happened for one company in one cycle."""

    company_slug: str
    company_name: str
    ok: bool
    duration_seconds: float
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_matched: int = 0
    jobs_ignored: int = 0
    detail_fetches: int = 0
    error: Optional[str] = None
    error_type: Optional[str] = None
    retries: int = 0
    from_cache: bool = False
    connector: str = "unknown"
    matches: List[Any] = field(default_factory=list)


@dataclass
class ScanCycleResult:
    """Aggregate of a full cycle across every company."""

    started_at: datetime
    finished_at: datetime
    companies: List[CompanyScanResult] = field(default_factory=list)
    notifications_sent: int = 0
    notification_failures: int = 0

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def jobs_seen(self) -> int:
        return sum(c.jobs_seen for c in self.companies)

    @property
    def jobs_new(self) -> int:
        return sum(c.jobs_new for c in self.companies)

    @property
    def jobs_matched(self) -> int:
        return sum(c.jobs_matched for c in self.companies)

    @property
    def jobs_ignored(self) -> int:
        return sum(c.jobs_ignored for c in self.companies)

    @property
    def failures(self) -> List[CompanyScanResult]:
        return [c for c in self.companies if not c.ok]

    @property
    def failure_ratio(self) -> float:
        if not self.companies:
            return 0.0
        return len(self.failures) / len(self.companies)

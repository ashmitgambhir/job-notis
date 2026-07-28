"""Core domain types.

Deliberately a leaf package: `orchestrator` and `health` live here but are NOT
re-exported, because they import the matching and notify layers, which import
these models. Import them by module path instead.
"""

from .models import (
    CompanyScanResult,
    EmploymentType,
    JobPosting,
    MatchResult,
    ScanCycleResult,
    WorkMode,
    utcnow,
)

__all__ = [
    "JobPosting",
    "MatchResult",
    "CompanyScanResult",
    "ScanCycleResult",
    "EmploymentType",
    "WorkMode",
    "utcnow",
]

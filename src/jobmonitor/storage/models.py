"""Database schema.

SQLite by default (one file, zero ops); swap ``database.url`` to Postgres for a
multi-instance deployment. Nothing here is SQLite-specific.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..core.models import utcnow


class Base(DeclarativeBase):
    pass


class Job(Base):
    """Every posting ever seen, matched or not.

    Keeping the non-matches is what makes threshold tuning possible after the
    fact — you can re-score history instead of waiting for new postings.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # company_slug:external_id — the primary dedupe key.
    job_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    company_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[Optional[str]] = mapped_column(String(256))
    location: Mapped[Optional[str]] = mapped_column(String(512))

    # Full description is not stored — only its hash — so the DB stays small
    # enough to keep on a free tier at 500 companies.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Title+location fingerprint; catches re-posts under a new requisition ID.
    dedupe_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    # Set when the posting stops appearing. If it reappears later it is
    # treated as new, per spec.
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # How many times this posting has been through the new-posting path.
    appearance_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    match_reasons: Mapped[Optional[str]] = mapped_column(Text)
    excluded_by: Mapped[Optional[str]] = mapped_column(String(256))

    employment_type: Mapped[str] = mapped_column(String(32), default="unknown")
    work_mode: Mapped[str] = mapped_column(String(32), default="unknown")
    requires_clearance: Mapped[Optional[bool]] = mapped_column(Boolean)
    offers_sponsorship: Mapped[Optional[bool]] = mapped_column(Boolean)
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    source_platform: Mapped[str] = mapped_column(String(64), default="unknown")
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("ix_jobs_company_seen", "company_slug", "last_seen_at"),
        Index("ix_jobs_matched_score", "matched", "score"),
        Index("ix_jobs_first_seen", "first_seen_at"),
    )


class Notification(Base):
    """One row per (job, channel) delivery attempt outcome.

    The unique constraint is the hard guarantee behind "never notify twice":
    it holds even if two scanner instances race on the same posting.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    # Includes the appearance count, so a genuine re-post can notify again
    # while a repeat of the same appearance cannot.
    dedupe_key: Mapped[str] = mapped_column(String(600), nullable=False)

    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)
    score: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("dedupe_key", "channel", name="uq_notification_dedupe"),
        Index("ix_notifications_sent", "channel", "sent_at"),
    )


class ScanRun(Base):
    """One completed cycle. Backs the health checks and weekly stats."""

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    companies_scanned: Mapped[int] = mapped_column(Integer, default=0)
    companies_failed: Mapped[int] = mapped_column(Integer, default=0)
    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_matched: Mapped[int] = mapped_column(Integer, default=0)
    jobs_ignored: Mapped[int] = mapped_column(Integer, default=0)
    notifications_sent: Mapped[int] = mapped_column(Integer, default=0)
    http_requests: Mapped[int] = mapped_column(Integer, default=0)
    http_retries: Mapped[int] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_scan_runs_started", "started_at"),)


class CompanyState(Base):
    """Per-company operational state that must survive restarts.

    ``seeded`` is the one that matters most: it is what stops the first scan of
    a new company from sending hundreds of notifications.
    """

    __tablename__ = "company_state"

    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    seeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    # Result of platform auto-detection, so discovery runs once not every cycle.
    detected_connector: Mapped[Optional[str]] = mapped_column(String(64))
    detected_params: Mapped[Optional[str]] = mapped_column(Text)
    detected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    total_jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    total_matches: Mapped[int] = mapped_column(Integer, default=0)


class AlertState(Base):
    """Cooldown bookkeeping for self-monitoring alerts."""

    __tablename__ = "alert_state"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=1)

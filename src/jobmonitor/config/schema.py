"""Typed configuration models.

Every knob the operator is expected to turn lives here and is sourced from YAML
in ``config/``. Nothing in this module reads the environment directly; secret
interpolation happens in :mod:`jobmonitor.config.loader` before validation, so
these models only ever see resolved values.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# settings.yaml
# --------------------------------------------------------------------------- #


class ScanSettings(_Base):
    """How often and how aggressively to poll."""

    interval_seconds: int = Field(300, ge=30)
    # Companies flagged ``priority: high`` get their own faster cadence.
    priority_interval_seconds: int = Field(120, ge=30)
    # Concurrent company scans. Each company is independent, so this is the
    # main throughput lever.
    max_concurrent_companies: int = Field(12, ge=1, le=200)
    # A company that overruns this is abandoned for the cycle; the next cycle
    # retries it. Prevents one hung site from stalling the whole scan.
    per_company_timeout_seconds: int = Field(120, ge=10)
    # Skip the detail fetch for postings whose title alone cannot pass the
    # threshold. This is the single biggest latency win.
    prefilter_before_detail_fetch: bool = True
    # Cap on detail fetches per company per cycle, so a board that dumps 500
    # new postings at once cannot blow the cycle budget.
    max_detail_fetches_per_cycle: int = Field(60, ge=0)
    # First scan of a company records everything without notifying, so you do
    # not get 2,000 texts on day one.
    seed_without_notifying: bool = True
    # How long to leave a company alone after discovery fails for it. Without
    # this, every unresolved company re-probes its whole candidate list on
    # every cycle — dozens of doomed HTTP requests that dominate the cycle
    # budget and starve the companies that actually work.
    rediscover_after_hours: int = Field(6, ge=0)


class HttpSettings(_Base):
    connect_timeout_seconds: float = Field(10.0, gt=0)
    read_timeout_seconds: float = Field(25.0, gt=0)
    max_connections: int = Field(100, ge=1)
    max_keepalive_connections: int = Field(30, ge=1)
    max_retries: int = Field(3, ge=0, le=10)
    backoff_base_seconds: float = Field(0.5, gt=0)
    backoff_max_seconds: float = Field(30.0, gt=0)
    # Per-host politeness. Keeps us well under any sane WAF threshold.
    requests_per_second_per_host: float = Field(4.0, gt=0)
    burst_per_host: int = Field(8, ge=1)
    # ETag / Last-Modified revalidation. Most boards support it and a 304 is
    # near-free, which is what makes a 2-minute cadence affordable.
    enable_conditional_requests: bool = True
    cache_ttl_seconds: int = Field(60, ge=0)
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    http2: bool = True


class DatabaseSettings(_Base):
    # sqlite:///... for single-node, postgresql+psycopg://... for HA.
    url: str = "sqlite:///data/jobmonitor.db"
    pool_size: int = Field(5, ge=1)
    max_overflow: int = Field(10, ge=0)
    # Postings not seen in a scan for this long are marked closed. If one comes
    # back after that, it is treated as new (per the spec).
    stale_after_days: int = Field(45, ge=1)


class LoggingSettings(_Base):
    level: str = "INFO"
    # "json" for shipping to a log aggregator, "console" for humans.
    format: str = "console"
    file: Optional[str] = "data/logs/jobmonitor.log"
    rotate_max_bytes: int = Field(10_485_760, ge=0)
    rotate_backups: int = Field(5, ge=0)

    @field_validator("level")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {v}")
        return v

    @field_validator("format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        if v not in {"json", "console"}:
            raise ValueError("format must be 'json' or 'console'")
        return v


class EmbeddingSettings(_Base):
    enabled: bool = True
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # Below this band we trust the rules outright; inside it we may escalate.
    cache_embeddings: bool = True


class LlmSettings(_Base):
    """LLM is deliberately a tiebreaker, not a pipeline stage.

    It only runs for postings whose rule score lands in the ambiguous band, so
    cost scales with genuinely uncertain postings rather than with traffic.
    """

    enabled: bool = False
    provider: str = "anthropic"
    model: str = "claude-opus-5"
    # Only adjudicate scores inside [low, high]. Outside the band the rules are
    # confident enough that an LLM call buys nothing.
    adjudicate_band_low: int = Field(55, ge=0, le=100)
    adjudicate_band_high: int = Field(88, ge=0, le=100)
    max_calls_per_cycle: int = Field(25, ge=0)
    max_description_chars: int = Field(6000, ge=500)
    timeout_seconds: float = Field(30.0, gt=0)
    api_key: Optional[str] = None

    @model_validator(mode="after")
    def _band(self) -> LlmSettings:
        if self.adjudicate_band_low > self.adjudicate_band_high:
            raise ValueError("adjudicate_band_low must be <= adjudicate_band_high")
        return self


class MatchingSettings(_Base):
    # Notify only above this. The single most useful dial for tuning noise.
    threshold: int = Field(75, ge=0, le=100)
    # Postings scoring here or above are recorded for review but not sent.
    review_threshold: int = Field(60, ge=0, le=100)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)

    @model_validator(mode="after")
    def _order(self) -> MatchingSettings:
        if self.review_threshold > self.threshold:
            raise ValueError("review_threshold must be <= threshold")
        return self


class HealthSettings(_Base):
    enabled: bool = True
    # Bind an HTTP endpoint for container/platform liveness probes.
    http_port: Optional[int] = 8080
    # Alert if no successful scan cycle completes within this window.
    heartbeat_timeout_seconds: int = Field(1800, ge=60)
    # Alert if a company fails this many consecutive cycles.
    company_failure_alert_threshold: int = Field(5, ge=1)
    # Alert if this fraction of companies fail in one cycle.
    cycle_failure_ratio_alert: float = Field(0.4, ge=0.0, le=1.0)


class Settings(_Base):
    scan: ScanSettings = Field(default_factory=ScanSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    matching: MatchingSettings = Field(default_factory=MatchingSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    timezone: str = "America/Chicago"

    @model_validator(mode="after")
    def _cache_must_not_outlive_the_scan_interval(self) -> Settings:
        """A response cache longer than the scan interval hides new postings.

        Detection speed is the whole point, so the cache is clamped rather
        than trusted: at a 60-second cadence a 60-second cache would serve
        every other scan a stale board.
        """
        fastest = min(self.scan.interval_seconds, self.scan.priority_interval_seconds)
        ceiling = max(0, fastest // 2)
        if self.http.cache_ttl_seconds > ceiling:
            self.http.cache_ttl_seconds = ceiling
        return self


# --------------------------------------------------------------------------- #
# companies.yaml
# --------------------------------------------------------------------------- #


class CompanyConfig(_Base):
    """One monitored employer.

    ``connector`` names a registered connector; ``params`` is that connector's
    own schema. ``auto`` defers to runtime platform detection, which is what
    ``jobmon discover`` writes back into this file.
    """

    name: str
    slug: str
    enabled: bool = True
    # "high" companies scan on the faster cadence.
    priority: str = "normal"
    connector: str = "auto"
    params: Dict[str, Any] = Field(default_factory=dict)
    careers_url: Optional[str] = None
    # Bumps the final score; use for employers you actually want.
    score_bonus: int = Field(0, ge=-50, le=50)
    tags: List[str] = Field(default_factory=list)
    # False means the endpoint has not been confirmed against the live site.
    # ``jobmon doctor`` flips this.
    verified: bool = False
    notes: Optional[str] = None

    @field_validator("priority")
    @classmethod
    def _prio(cls, v: str) -> str:
        if v not in {"high", "normal", "low"}:
            raise ValueError("priority must be high, normal or low")
        return v


class CompaniesConfig(_Base):
    defaults: Dict[str, Any] = Field(default_factory=dict)
    companies: List[CompanyConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_slugs(self) -> CompaniesConfig:
        seen = set()
        for c in self.companies:
            if c.slug in seen:
                raise ValueError(f"duplicate company slug: {c.slug}")
            seen.add(c.slug)
        return self


# --------------------------------------------------------------------------- #
# roles.yaml
# --------------------------------------------------------------------------- #


class TitleFamily(_Base):
    """A cluster of related titles sharing one relevance weight.

    Grouping beats a flat keyword list because it lets "Mechanical Design
    Engineer" outrank "Systems Engineer" without maintaining per-title scores.
    """

    name: str
    weight: int = Field(..., ge=0, le=100)
    titles: List[str] = Field(default_factory=list)
    # Substrings that count as a partial hit for this family.
    keywords: List[str] = Field(default_factory=list)


class SeniorityRule(_Base):
    name: str
    # Matched against title + description.
    patterns: List[str]
    weight: int = Field(..., ge=-100, le=100)


class RolesConfig(_Base):
    # Anchor sentences describing the target role, embedded once at startup and
    # compared against each posting for the semantic component.
    semantic_profile: List[str] = Field(default_factory=list)
    families: List[TitleFamily] = Field(default_factory=list)
    seniority: List[SeniorityRule] = Field(default_factory=list)
    degrees: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    # Component weights for the final score; normalised at load time.
    weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "title": 0.34,
            "semantic": 0.22,
            "seniority": 0.16,
            "department": 0.08,
            "skills": 0.10,
            "degree": 0.06,
            "description": 0.04,
        }
    )

    @model_validator(mode="after")
    def _normalise_weights(self) -> RolesConfig:
        total = sum(self.weights.values())
        if total <= 0:
            raise ValueError("roles.weights must sum to a positive number")
        self.weights = {k: v / total for k, v in self.weights.items()}
        return self


# --------------------------------------------------------------------------- #
# filters.yaml
# --------------------------------------------------------------------------- #


class LocationFilter(_Base):
    # Empty allow-list means "anywhere".
    allow: List[str] = Field(default_factory=list)
    deny: List[str] = Field(default_factory=list)
    allow_remote: bool = True
    # Bonus for locations you actively want.
    preferred: List[str] = Field(default_factory=list)
    preferred_bonus: int = Field(5, ge=0, le=30)
    # Drop anything that is clearly not in these countries.
    countries: List[str] = Field(default_factory=list)


class FiltersConfig(_Base):
    # Hard kill: any of these in the title drops the posting before scoring.
    exclude_title_patterns: List[str] = Field(default_factory=list)
    # Softer: these in the description subtract from the score.
    penalty_patterns: Dict[str, int] = Field(default_factory=dict)
    exclude_departments: List[str] = Field(default_factory=list)
    # Keep only these employment types; empty means keep all.
    employment_types: List[str] = Field(default_factory=list)
    location: LocationFilter = Field(default_factory=LocationFilter)
    # Drop postings that demand a clearance you cannot get.
    exclude_requiring_clearance: bool = False
    # Drop postings that explicitly refuse visa sponsorship.
    exclude_no_sponsorship: bool = False
    # Ignore postings older than this at first sight; stops a newly added
    # company from paging you about a six-month-old listing.
    max_posting_age_days: int = Field(30, ge=1)


# --------------------------------------------------------------------------- #
# notifications.yaml
# --------------------------------------------------------------------------- #


class SmsChannel(_Base):
    enabled: bool = False
    provider: str = "twilio"
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None
    from_number: Optional[str] = None
    to_numbers: List[str] = Field(default_factory=list)
    # SMS is the "wake me up" channel, so hold it to a higher bar than email.
    min_score: int = Field(85, ge=0, le=100)
    max_per_hour: int = Field(15, ge=1)


class EmailChannel(_Base):
    enabled: bool = False
    # "smtp" or "sendgrid"
    provider: str = "smtp"
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    use_tls: bool = True
    sendgrid_api_key: Optional[str] = None
    from_address: Optional[str] = None
    to_addresses: List[str] = Field(default_factory=list)
    min_score: int = Field(75, ge=0, le=100)
    max_per_hour: int = Field(60, ge=1)


class WebhookChannel(_Base):
    enabled: bool = False
    url: Optional[str] = None
    min_score: int = Field(75, ge=0, le=100)
    max_per_hour: int = Field(120, ge=1)
    headers: Dict[str, str] = Field(default_factory=dict)


class TelegramChannel(_Base):
    enabled: bool = False
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    min_score: int = Field(75, ge=0, le=100)
    max_per_hour: int = Field(120, ge=1)


class DigestConfig(_Base):
    daily_enabled: bool = False
    daily_at: str = "08:00"
    weekly_enabled: bool = False
    weekly_at: str = "MON 08:00"
    # Include near-misses so you can tune the threshold from real data.
    include_review_band: bool = True


class AlertsConfig(_Base):
    """Where the system complains about itself."""

    enabled: bool = True
    # Reuse a channel by name: sms, email, slack, discord, telegram, webhook.
    channels: List[str] = Field(default_factory=lambda: ["email"])
    # Do not re-send the same alert more often than this.
    cooldown_seconds: int = Field(3600, ge=60)


class NotificationsConfig(_Base):
    sms: SmsChannel = Field(default_factory=SmsChannel)
    email: EmailChannel = Field(default_factory=EmailChannel)
    slack: WebhookChannel = Field(default_factory=WebhookChannel)
    discord: WebhookChannel = Field(default_factory=WebhookChannel)
    telegram: TelegramChannel = Field(default_factory=TelegramChannel)
    webhook: WebhookChannel = Field(default_factory=WebhookChannel)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    # Suppress non-urgent channels during these local hours. High scores still
    # go out, because being first is the entire point.
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    quiet_hours_override_score: int = Field(92, ge=0, le=100)
    # Retries per channel before the notification is parked as failed.
    max_delivery_attempts: int = Field(4, ge=1, le=10)


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #


class AppConfig(_Base):
    """Everything, loaded and validated."""

    settings: Settings
    companies: CompaniesConfig
    roles: RolesConfig
    filters: FiltersConfig
    notifications: NotificationsConfig

    def enabled_companies(self) -> List[CompanyConfig]:
        return [c for c in self.companies.companies if c.enabled]

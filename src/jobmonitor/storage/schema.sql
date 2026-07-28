-- Reference schema. The application creates these tables automatically via
-- SQLAlchemy (`jobmon init-db`); this file documents the shape for anyone
-- querying the database directly or provisioning Postgres by hand.

CREATE TABLE IF NOT EXISTS jobs (
    id                 INTEGER PRIMARY KEY,
    job_key            VARCHAR(512) NOT NULL UNIQUE,  -- company_slug:external_id
    company_slug       VARCHAR(128) NOT NULL,
    company_name       VARCHAR(256) NOT NULL,
    external_id        VARCHAR(256) NOT NULL,
    title              VARCHAR(512) NOT NULL,
    url                TEXT NOT NULL,
    department         VARCHAR(256),
    location           VARCHAR(512),
    content_hash       VARCHAR(64) NOT NULL,   -- detects edits to a posting
    dedupe_hash        VARCHAR(64) NOT NULL,   -- title+location; catches re-posts
    posted_at          TIMESTAMP,
    first_seen_at      TIMESTAMP NOT NULL,
    last_seen_at       TIMESTAMP NOT NULL,
    closed_at          TIMESTAMP,              -- set when it stops appearing
    appearance_count   INTEGER NOT NULL DEFAULT 1,
    score              INTEGER NOT NULL DEFAULT 0,
    matched            BOOLEAN NOT NULL DEFAULT 0,
    match_reasons      TEXT,                   -- JSON array
    excluded_by        VARCHAR(256),
    employment_type    VARCHAR(32) DEFAULT 'unknown',
    work_mode          VARCHAR(32) DEFAULT 'unknown',
    requires_clearance BOOLEAN,
    offers_sponsorship BOOLEAN,
    deadline           TIMESTAMP,
    source_platform    VARCHAR(64) DEFAULT 'unknown',
    notified           BOOLEAN NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_jobs_company_seen  ON jobs (company_slug, last_seen_at);
CREATE INDEX IF NOT EXISTS ix_jobs_matched_score ON jobs (matched, score);
CREATE INDEX IF NOT EXISTS ix_jobs_first_seen    ON jobs (first_seen_at);
CREATE INDEX IF NOT EXISTS ix_jobs_dedupe_hash   ON jobs (dedupe_hash);

-- The unique constraint is what guarantees "never notify twice", even with
-- two scanner instances racing on the same posting.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    job_key     VARCHAR(512) NOT NULL,
    channel     VARCHAR(64)  NOT NULL,
    dedupe_key  VARCHAR(600) NOT NULL,   -- job_key#appearance_count
    status      VARCHAR(32)  NOT NULL DEFAULT 'pending',
    attempts    INTEGER      NOT NULL DEFAULT 0,
    created_at  TIMESTAMP    NOT NULL,
    sent_at     TIMESTAMP,
    error       TEXT,
    score       INTEGER DEFAULT 0,
    CONSTRAINT uq_notification_dedupe UNIQUE (dedupe_key, channel)
);
CREATE INDEX IF NOT EXISTS ix_notifications_job  ON notifications (job_key);
CREATE INDEX IF NOT EXISTS ix_notifications_sent ON notifications (channel, sent_at);

CREATE TABLE IF NOT EXISTS scan_runs (
    id                 INTEGER PRIMARY KEY,
    started_at         TIMESTAMP NOT NULL,
    finished_at        TIMESTAMP NOT NULL,
    duration_seconds   REAL NOT NULL,
    companies_scanned  INTEGER DEFAULT 0,
    companies_failed   INTEGER DEFAULT 0,
    jobs_seen          INTEGER DEFAULT 0,
    jobs_new           INTEGER DEFAULT 0,
    jobs_matched       INTEGER DEFAULT 0,
    jobs_ignored       INTEGER DEFAULT 0,
    notifications_sent INTEGER DEFAULT 0,
    http_requests      INTEGER DEFAULT 0,
    http_retries       INTEGER DEFAULT 0,
    cache_hits         INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_scan_runs_started ON scan_runs (started_at);

CREATE TABLE IF NOT EXISTS company_state (
    slug                 VARCHAR(128) PRIMARY KEY,
    seeded               BOOLEAN NOT NULL DEFAULT 0,  -- first scan done silently
    last_success_at      TIMESTAMP,
    last_attempt_at      TIMESTAMP,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    detected_connector   VARCHAR(64),   -- cached platform auto-detection
    detected_params      TEXT,          -- JSON
    detected_at          TIMESTAMP,
    total_jobs_seen      INTEGER DEFAULT 0,
    total_matches        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alert_state (
    key          VARCHAR(256) PRIMARY KEY,
    last_sent_at TIMESTAMP NOT NULL,
    count        INTEGER DEFAULT 1
);

-- Shared job contract between Spring Boot and Python ingestion workers.
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        TEXT NOT NULL,
    book_id         UUID NULL,
    source_key      TEXT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    result          JSONB NULL,
    error_message   TEXT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    worker_id       TEXT NULL,
    locked_at       TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ NULL,
    finished_at     TIMESTAMPTZ NULL
);

ALTER TABLE ingestion_jobs
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS ingestion_job_events (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES ingestion_jobs(id) ON DELETE CASCADE,
    event TEXT NOT NULL CHECK (event IN ('enqueued', 'claimed', 'retry_scheduled', 'completed', 'failed')),
    worker_id TEXT NULL,
    attempt INTEGER NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ingestion_job_events_job_idx
    ON ingestion_job_events (job_id, created_at);

CREATE INDEX IF NOT EXISTS ingestion_jobs_pending_idx
    ON ingestion_jobs (created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ingestion_jobs_book_idx
    ON ingestion_jobs (book_id, created_at DESC);

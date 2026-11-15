-- ─────────────────────────────────────────────────────────────────────────────
-- Readsake ingestion pipeline — audiobook tables.
-- Run once against the same database the Spring Boot backend uses.
-- Global read-only catalog data (like gutenberg_catalog) — no RLS.
-- ─────────────────────────────────────────────────────────────────────────────

-- One row per LibriVox recording matched to a catalog book.
CREATE TABLE IF NOT EXISTS audiobooks (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gutenberg_id           INT NOT NULL REFERENCES gutenberg_catalog(gutenberg_id),
    source                 TEXT NOT NULL DEFAULT 'LIBRIVOX',
    librivox_id            BIGINT NOT NULL UNIQUE,
    title                  TEXT NOT NULL,
    narrator               TEXT,               -- display name, or 'Various readers'
    language               TEXT,
    total_duration_seconds INT,                -- of the CLEANED tracks
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audiobooks_gutenberg ON audiobooks(gutenberg_id);

-- One row per cleaned chapter MP3.
CREATE TABLE IF NOT EXISTS audio_tracks (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    audiobook_id     UUID NOT NULL REFERENCES audiobooks(id) ON DELETE CASCADE,
    track_number     INT NOT NULL,
    title            TEXT,
    duration_seconds NUMERIC(10,2),
    asset_path       TEXT NOT NULL,            -- storage key/URL of the cleaned MP3
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (audiobook_id, track_number)
);

-- Audit: what was cut from every track, and whether it needs a human ear.
CREATE TABLE IF NOT EXISTS audio_processing_log (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    librivox_id       BIGINT NOT NULL,
    track_number      INT NOT NULL,
    input_url         TEXT,
    output_key        TEXT,
    original_duration NUMERIC(10,2),
    cut_start         NUMERIC(10,2),
    cut_end           NUMERIC(10,2),
    new_duration      NUMERIC(10,2),
    normalized        BOOLEAN NOT NULL DEFAULT FALSE,
    validation        TEXT NOT NULL,            -- ok | failed
    detail            JSONB,                    -- full audit dict
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (librivox_id, track_number)          -- doubles as the run checkpoint
);

-- Lets Discovery mark catalog titles that have a cleaned audiobook ready.
ALTER TABLE gutenberg_catalog
    ADD COLUMN IF NOT EXISTS has_audio BOOLEAN NOT NULL DEFAULT FALSE;

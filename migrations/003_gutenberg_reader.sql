ALTER TABLE gutenberg_catalog
    ADD COLUMN IF NOT EXISTS reader_file_key TEXT,
    ADD COLUMN IF NOT EXISTS reader_cover_key TEXT,
    ADD COLUMN IF NOT EXISTS reader_metadata JSONB,
    ADD COLUMN IF NOT EXISTS reader_processor_version TEXT,
    ADD COLUMN IF NOT EXISTS reader_processed_at TIMESTAMPTZ;

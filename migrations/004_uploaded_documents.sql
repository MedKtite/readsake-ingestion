ALTER TABLE books
    ADD COLUMN IF NOT EXISTS processing_status TEXT NOT NULL DEFAULT 'ready',
    ADD COLUMN IF NOT EXISTS processing_error TEXT,
    ADD COLUMN IF NOT EXISTS reading_file_key TEXT,
    ADD COLUMN IF NOT EXISTS processing_version TEXT,
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS extracted_metadata JSONB;

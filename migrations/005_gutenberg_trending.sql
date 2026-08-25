-- Keep this migration in sync with marginalia-db/migrations/003_gutenberg_trending.sql.
-- It lives here too so the standalone ingestion deployment can be installed
-- without depending on a second checkout.

CREATE TABLE IF NOT EXISTS gutenberg_trending_books (
    gutenberg_id   INT PRIMARY KEY
        REFERENCES gutenberg_catalog(gutenberg_id) ON DELETE CASCADE,
    rank           INT NOT NULL UNIQUE CHECK (rank > 0),
    download_count INT NOT NULL DEFAULT 0 CHECK (download_count >= 0),
    synced_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gutenberg_trending_books_synced
    ON gutenberg_trending_books (synced_at DESC);

CREATE TABLE IF NOT EXISTS gutenberg_top_authors (
    author_key     TEXT PRIMARY KEY,
    author_name    TEXT NOT NULL,
    rank           INT NOT NULL UNIQUE CHECK (rank > 0),
    download_count BIGINT NOT NULL DEFAULT 0 CHECK (download_count >= 0),
    book_count     INT NOT NULL DEFAULT 0 CHECK (book_count >= 0),
    image_url      TEXT,
    tagline        TEXT,
    bio            TEXT,
    synced_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gutenberg_top_authors_synced
    ON gutenberg_top_authors (synced_at DESC);

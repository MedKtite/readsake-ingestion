"""Thin PostgreSQL layer (psycopg v3) over the backend's database.

The pipeline's "book" table is Readsake's `gutenberg_catalog` — the synced
Project Gutenberg index (gutenberg_id PK, title, authors, title_norm). The
three audiobook tables come from migrations/001_audiobooks.sql.

Config: DATABASE_URL, e.g. postgresql://user:pass@localhost:5432/readsake
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row


class Db:
    def __init__(self, dsn: Optional[str] = None):
        self._conn = psycopg.connect(
            dsn or os.environ["DATABASE_URL"],
            row_factory=dict_row,
            autocommit=True,
        )

    def close(self) -> None:
        self._conn.close()

    @property
    def connection(self):
        """Expose the connection to small transaction-scoped repositories."""
        return self._conn

    def __enter__(self) -> "Db":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── Catalog lookups (matching) ──────────────────────────────────────

    def find_book_by_gutenberg_id(self, gutenberg_id: int) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT gutenberg_id, title, authors, language, title_norm "
                "FROM gutenberg_catalog WHERE gutenberg_id = %s",
                (gutenberg_id,),
            )
            return cur.fetchone()

    def search_books_by_title(self, title_norm: str, limit: int = 25) -> list[dict]:
        """Candidates whose normalized title contains the query (trgm-indexed)."""
        if not title_norm:
            return []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT gutenberg_id, title, authors, language, title_norm "
                "FROM gutenberg_catalog WHERE title_norm LIKE %s LIMIT %s",
                (f"%{title_norm}%", limit),
            )
            return cur.fetchall()

    def known_gutenberg_ids(self, gutenberg_ids: list[int]) -> set[int]:
        """Return only ids already present in the canonical catalog."""
        if not gutenberg_ids:
            return set()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT gutenberg_id FROM gutenberg_catalog "
                "WHERE gutenberg_id = ANY(%s)",
                (gutenberg_ids,),
            )
            return {int(row["gutenberg_id"]) for row in cur.fetchall()}

    def top_author_metadata(self, author_keys: list[str]) -> dict[str, dict]:
        """Previous enrichment, used when Wikipedia is temporarily unavailable."""
        if not author_keys:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT author_key, image_url, tagline, bio
                FROM gutenberg_top_authors
                WHERE author_key = ANY(%s)
                """,
                (author_keys,),
            )
            return {str(row["author_key"]): row for row in cur.fetchall()}

    def replace_gutenberg_discovery_snapshot(
        self,
        books: list[dict[str, Any]],
        authors: list[dict[str, Any]],
        synced_at: datetime,
    ) -> None:
        """Atomically publish a complete Home discovery snapshot."""
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                # Prevent two scheduler instances from publishing concurrently.
                cur.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('readsake:gutenberg-discovery-snapshot'))"
                )
                cur.execute("DELETE FROM gutenberg_trending_books")
                cur.executemany(
                    """
                    INSERT INTO gutenberg_trending_books
                        (gutenberg_id, rank, download_count, synced_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [
                        (
                            row["gutenberg_id"],
                            row["rank"],
                            row["download_count"],
                            synced_at,
                        )
                        for row in books
                    ],
                )

                cur.execute("DELETE FROM gutenberg_top_authors")
                cur.executemany(
                    """
                    INSERT INTO gutenberg_top_authors
                        (author_key, author_name, rank, download_count,
                         book_count, image_url, tagline, bio, synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            row["author_key"],
                            row["author_name"],
                            row["rank"],
                            row["download_count"],
                            row["book_count"],
                            row.get("image_url"),
                            row.get("tagline"),
                            row.get("bio"),
                            synced_at,
                        )
                        for row in authors
                    ],
                )

    def mark_gutenberg_processed(self, gutenberg_id: int, result: dict[str, Any]) -> None:
        """Record the immutable, versioned reader artifact for a catalog book."""
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE gutenberg_catalog
                    SET reader_file_key = %s,
                        reader_cover_key = COALESCE(%s, reader_cover_key),
                        reader_metadata = %s::jsonb,
                        reader_processor_version = %s,
                        reader_processed_at = NOW()
                    WHERE gutenberg_id = %s
                    """,
                    (
                        result.get("reader_key"),
                        result.get("cover_key"),
                        json.dumps(result),
                        result.get("processor_version"),
                        gutenberg_id,
                    ),
                )

                cur.execute(
                    """UPDATE books SET reading_file_key = %s, processing_status = 'ready',
                       processing_error = NULL, processing_version = %s,
                       cover_url = CASE WHEN %s::text IS NOT NULL THEN %s ELSE cover_url END,
                       updated_at = NOW() WHERE gutenberg_id = %s""",
                    (result.get("reader_key"), result.get("processor_version"),
                     result.get("cover_key"), f"/covers/gutenberg/{gutenberg_id}", gutenberg_id),
                )

    def mark_uploaded_processed(self, book_id: str, result: dict[str, Any]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE books
                SET processing_status = 'ready', processing_error = NULL,
                    reading_file_key = %s, processing_version = %s,
                    description = COALESCE(%s, description),
                    author = COALESCE(%s, author),
                    title = COALESCE(%s, title),
                    cover_url = COALESCE(%s, cover_url),
                    extracted_metadata = %s::jsonb, updated_at = NOW()
                WHERE id = %s
                """,
                (
                    result.get("reader_key"), result.get("processor_version"),
                    result.get("description"), result.get("author"),
                    result.get("title"), result.get("cover_url"),
                    json.dumps(result), book_id,
                ),
            )

    def mark_uploaded_failed(self, book_id: str, error: str) -> None:
        """Expose a terminal processing error to the app instead of leaving a book pending."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE books
                SET processing_status = 'failed',
                    processing_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (error[:2000], book_id),
            )

    # ── Audiobooks / tracks ─────────────────────────────────────────────

    def upsert_audiobook(
        self,
        gutenberg_id: int,
        librivox_id: int,
        title: str,
        narrator: Optional[str],
        language: Optional[str],
        total_duration_seconds: Optional[int] = None,
    ) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audiobooks
                    (gutenberg_id, source, librivox_id, title, narrator,
                     language, total_duration_seconds)
                VALUES (%s, 'LIBRIVOX', %s, %s, %s, %s, %s)
                ON CONFLICT (librivox_id) DO UPDATE SET
                    gutenberg_id = EXCLUDED.gutenberg_id,
                    title        = EXCLUDED.title,
                    narrator     = EXCLUDED.narrator,
                    language     = EXCLUDED.language,
                    updated_at   = NOW()
                RETURNING id
                """,
                (gutenberg_id, librivox_id, title, narrator, language,
                 total_duration_seconds),
            )
            return str(cur.fetchone()["id"])

    def upsert_track(
        self,
        audiobook_id: str,
        track_number: int,
        title: Optional[str],
        duration_seconds: Optional[float],
        asset_path: str,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audio_tracks
                    (audiobook_id, track_number, title, duration_seconds, asset_path)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (audiobook_id, track_number) DO UPDATE SET
                    title            = EXCLUDED.title,
                    duration_seconds = EXCLUDED.duration_seconds,
                    asset_path       = EXCLUDED.asset_path
                """,
                (audiobook_id, track_number, title, duration_seconds, asset_path),
            )

    def refresh_total_duration(self, audiobook_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE audiobooks SET
                    total_duration_seconds = (
                        SELECT ROUND(SUM(duration_seconds))
                        FROM audio_tracks WHERE audiobook_id = %s
                    ),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (audiobook_id, audiobook_id),
            )

    def set_has_audio(self, gutenberg_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE gutenberg_catalog SET has_audio = TRUE "
                "WHERE gutenberg_id = %s",
                (gutenberg_id,),
            )

    # ── Audit log / checkpointing ───────────────────────────────────────

    def log_processing(
        self,
        librivox_id: int,
        track_number: int,
        input_url: Optional[str],
        output_key: Optional[str],
        audit: dict[str, Any],
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audio_processing_log
                    (librivox_id, track_number, input_url, output_key,
                     original_duration, cut_start, cut_end, new_duration,
                     normalized, validation, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (librivox_id, track_number) DO UPDATE SET
                    input_url         = EXCLUDED.input_url,
                    output_key        = EXCLUDED.output_key,
                    original_duration = EXCLUDED.original_duration,
                    cut_start         = EXCLUDED.cut_start,
                    cut_end           = EXCLUDED.cut_end,
                    new_duration      = EXCLUDED.new_duration,
                    normalized        = EXCLUDED.normalized,
                    validation        = EXCLUDED.validation,
                    detail            = EXCLUDED.detail,
                    created_at        = NOW()
                """,
                (
                    librivox_id, track_number, input_url, output_key,
                    audit.get("original_duration"), audit.get("cut_start"),
                    audit.get("cut_end"), audit.get("new_duration"),
                    audit.get("normalized", False), audit.get("validation"),
                    json.dumps(audit),
                ),
            )

    def is_track_processed(self, librivox_id: int, track_number: int) -> bool:
        """Checkpoint: a log row (ok OR failed) means the track was handled.

        Failed tracks are deliberately NOT retried on normal runs — they are
        reprocessed only via --manual-review-only.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM audio_processing_log "
                "WHERE librivox_id = %s AND track_number = %s",
                (librivox_id, track_number),
            )
            return cur.fetchone() is not None

    def failed_tracks(self) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT librivox_id, track_number, input_url "
                "FROM audio_processing_log WHERE validation = 'failed' "
                "ORDER BY librivox_id, track_number",
            )
            return cur.fetchall()

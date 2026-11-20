"""PostgreSQL-backed processing jobs shared by ingestion workers.

The table is created by migrations/002_ingestion_jobs.sql. Workers claim jobs
with row-level locking so multiple worker processes can run safely.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Optional

from .db import Db


@dataclass(frozen=True)
class IngestionJob:
    id: str
    job_type: str
    book_id: Optional[str]
    source_key: Optional[str]
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


class JobStore:
    def __init__(self, db: Db):
        self._db = db

    def enqueue(
        self,
        job_type: str,
        *,
        book_id: Optional[str] = None,
        source_key: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        max_attempts: int = 3,
    ) -> str:
        with self._db.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_jobs
                    (job_type, book_id, source_key, payload, max_attempts)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                RETURNING id::text
                """,
                (job_type, book_id, source_key, json.dumps(payload or {}), max(1, max_attempts)),
            )
            job_id = str(cur.fetchone()["id"])
            self._event(cur, job_id, "enqueued", detail={"job_type": job_type})
            return job_id

    def prepare_trending(self, limit: int) -> int:
        """Queue popular unprepared titles ahead of demand, using the durable queue."""
        with self._db.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO ingestion_jobs
                   (job_type, source_key, payload, max_attempts)
                   SELECT 'GUTENBERG_READER', c.gutenberg_id::text,
                          jsonb_build_object('gutenberg_id', c.gutenberg_id), 6
                   FROM gutenberg_trending_books t
                   JOIN gutenberg_catalog c ON c.gutenberg_id = t.gutenberg_id
                   WHERE c.reader_file_key IS NULL AND NOT EXISTS (
                       SELECT 1 FROM ingestion_jobs j
                       WHERE j.job_type = 'GUTENBERG_READER'
                         AND j.source_key = c.gutenberg_id::text
                         AND (j.status IN ('pending', 'processing') OR
                              (j.status = 'failed' AND j.finished_at > NOW() - INTERVAL '15 minutes')))
                   ORDER BY t.rank LIMIT %s""",
                (max(1, min(limit, 100)),),
            )
            return cur.rowcount

    def claim(self, worker_id: str) -> Optional[IngestionJob]:
        with self._db.connection.cursor() as cur:
            cur.execute(
                """
                WITH next_job AS (
                    SELECT id
                    FROM ingestion_jobs
                    WHERE (status = 'pending' AND next_attempt_at <= NOW())
                       OR (status = 'processing'
                           AND locked_at < NOW() - INTERVAL '15 minutes')
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE ingestion_jobs j
                SET status = 'processing',
                    attempts = j.attempts + 1,
                    worker_id = %s,
                    locked_at = NOW(),
                    started_at = COALESCE(j.started_at, NOW())
                FROM next_job
                WHERE j.id = next_job.id
                RETURNING j.id::text, j.job_type, j.book_id::text,
                          j.source_key, j.payload, j.attempts, j.max_attempts
                """,
                (worker_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            self._event(cur, row["id"], "claimed", worker_id=worker_id, attempt=row["attempts"])
            payload = row["payload"] or {}
            return IngestionJob(
                id=row["id"],
                job_type=row["job_type"],
                book_id=row["book_id"],
                source_key=row["source_key"],
                payload=payload,
                attempts=row["attempts"],
                max_attempts=row["max_attempts"],
            )

    def complete(self, job_id: str, result: Optional[dict[str, Any]] = None) -> None:
        with self._db.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE ingestion_jobs
                SET status = 'ready', result = %s::jsonb,
                    finished_at = NOW(), locked_at = NULL, worker_id = NULL
                WHERE id = %s
                """,
                (json.dumps(result or {}), job_id),
            )
            self._event(cur, job_id, "completed", detail=result or {})

    def fail(self, job_id: str, error: str, *, retry: bool = True) -> None:
        with self._db.connection.cursor() as cur:
            cur.execute("SELECT attempts, max_attempts, worker_id FROM ingestion_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
            if row is None:
                return
            should_retry = retry and row["attempts"] < row["max_attempts"]
            # Dependency outages back off for minutes, with jitter across workers.
            base = 60 if error.startswith("SOURCE_UNAVAILABLE:") else 5
            delay_seconds = min(3600, base * (2 ** max(0, row["attempts"] - 1)))
            delay_seconds += random.randint(0, max(1, delay_seconds // 4))
            cur.execute(
                """
                UPDATE ingestion_jobs
                SET status = CASE WHEN %s THEN 'pending' ELSE 'failed' END,
                    error_message = %s, locked_at = NULL, worker_id = NULL,
                    next_attempt_at = CASE WHEN %s THEN NOW() + (%s * INTERVAL '1 second') ELSE NOW() END,
                    finished_at = CASE WHEN %s THEN NULL ELSE NOW() END
                WHERE id = %s
                """,
                (should_retry, error[:2000], should_retry, delay_seconds, should_retry, job_id),
            )
            self._event(cur, job_id, "retry_scheduled" if should_retry else "failed",
                        worker_id=row["worker_id"], attempt=row["attempts"],
                        detail={"error": error[:2000], "retry": should_retry})

    def _event(self, cur, job_id: str, event: str, *, worker_id: Optional[str] = None,
               attempt: Optional[int] = None, detail: Optional[dict[str, Any]] = None) -> None:
        cur.execute(
            """INSERT INTO ingestion_job_events (job_id, event, worker_id, attempt, detail)
               VALUES (%s, %s, %s, %s, %s::jsonb)""",
            (job_id, event, worker_id, attempt, json.dumps(detail or {})),
        )

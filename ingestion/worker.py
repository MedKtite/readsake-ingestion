"""Generic durable worker for Gutenberg and uploaded-document jobs."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

from .db import Db
from .documents.pipeline import process_uploaded_document
from .gutenberg.pipeline import process_gutenberg_book
from .jobs import IngestionJob, JobStore
from .storage import make_storage

LOGGER = logging.getLogger(__name__)


def process_job(job: IngestionJob, *, db: Db, storage, work_dir: Path) -> dict:
    if job.job_type == "GUTENBERG_READER":
        gutenberg_id = int(job.payload.get("gutenberg_id") or job.source_key or job.book_id)
        return process_gutenberg_book(gutenberg_id, db=db, storage=storage, work_dir=work_dir)

    if job.job_type == "UPLOAD_DOCUMENT":
        fmt = str(job.payload.get("format") or "").lower()
        if not job.book_id or not job.source_key or fmt not in {"pdf", "epub"}:
            raise ValueError("UPLOAD_DOCUMENT requires book_id, source_key and pdf/epub format")
        return process_uploaded_document(
            job.book_id, job.source_key, fmt, db=db, storage=storage, work_dir=work_dir
        )

    raise ValueError(f"Unsupported ingestion job type: {job.job_type}")


def run_once(*, db: Db, storage, worker_id: str) -> bool:
    store = JobStore(db)
    job = store.claim(worker_id)
    if job is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="readsake-ingestion-") as directory:
            result = process_job(job, db=db, storage=storage, work_dir=Path(directory))
        store.complete(job.id, result)
    except Exception as exc:
        LOGGER.exception("Ingestion job %s failed", job.id)
        if job.book_id and job.job_type == "UPLOAD_DOCUMENT" and job.attempts >= job.max_attempts:
            db.mark_uploaded_failed(job.book_id, str(exc))
        store.fail(job.id, str(exc), retry=True)
    return True


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run Readsake ingestion jobs")
    parser.add_argument("--once", action="store_true", help="process at most one job")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    with Db() as db:
        storage = make_storage()
        while True:
            had_job = run_once(db=db, storage=storage, worker_id=worker_id)
            if args.once:
                return
            if not had_job:
                LOGGER.info("No pending ingestion jobs; checking again in %.1f seconds", args.poll_seconds)
                time.sleep(max(0.1, args.poll_seconds))


if __name__ == "__main__":
    main()

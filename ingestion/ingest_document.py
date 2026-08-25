"""Process one uploaded PDF/EPUB from object storage.

Usage:
    python -m ingestion.ingest_document BOOK_ID SOURCE_KEY pdf
"""

from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from .db import Db
from .documents.pipeline import process_uploaded_document
from .storage import make_storage


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Process an uploaded document")
    parser.add_argument("book_id")
    parser.add_argument("source_key")
    parser.add_argument("format", choices=("pdf", "epub"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with Db() as db, tempfile.TemporaryDirectory(prefix=f"document-{args.book_id}-") as tmp:
        result = process_uploaded_document(
            args.book_id, args.source_key, args.format,
            db=db, storage=make_storage(), work_dir=Path(tmp),
        )
        logging.info("Processed %s: reader=%s", args.book_id, result["reader_key"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

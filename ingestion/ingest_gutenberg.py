"""CLI for preparing one or more Project Gutenberg reader copies.

Usage:
    python -m ingestion.ingest_gutenberg 1342 1661
"""

from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from .db import Db
from .gutenberg.pipeline import process_gutenberg_book
from .storage import make_storage


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Prepare Gutenberg reader copies")
    parser.add_argument("gutenberg_ids", nargs="+", type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with Db() as db:
        storage = make_storage()
        for gutenberg_id in args.gutenberg_ids:
            with tempfile.TemporaryDirectory(prefix=f"gutenberg-{gutenberg_id}-") as tmp:
                result = process_gutenberg_book(
                    gutenberg_id, db=db, storage=storage, work_dir=Path(tmp)
                )
                logging.info(
                    "Prepared %s: removed_boilerplate=%s key=%s",
                    gutenberg_id,
                    result["removed_boilerplate"],
                    result["reader_key"],
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

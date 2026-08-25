"""Orchestrator / CLI — LibriVox → matched, trimmed, normalized, uploaded.

    python -m ingestion.ingest_librivox --limit 20 --dry-run
    python -m ingestion.ingest_librivox --limit 20 --languages en
    python -m ingestion.ingest_librivox --manual-review-only

Pipeline per audiobook:
  match → upsert audiobooks row → per track: download (resumable) →
  trim+normalize → upload → upsert audio_tracks row → audit log row →
  delete temp files → mark gutenberg_catalog.has_audio.

Idempotent: audiobooks/tracks upsert on natural keys, and a log row per
(librivox_id, track_number) is the checkpoint — a crashed run continues
where it stopped. Failed-validation tracks are only retried with
--manual-review-only.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import requests
from tqdm import tqdm

from . import audio_cleaner
from .db import Db
from .librivox_client import LibrivoxBook, LibrivoxClient
from .matcher import match
from .storage import StorageBackend, make_storage

log = logging.getLogger("ingestion")

DOWNLOAD_CHUNK = 1 << 20  # 1 MiB


def _download(session: requests.Session, url: str, dest: Path) -> Path:
    """Resumable download to a temp .part file, renamed when complete."""
    part = dest.with_suffix(dest.suffix + ".part")
    headers = {}
    mode = "wb"
    if part.exists() and part.stat().st_size > 0:
        headers["Range"] = f"bytes={part.stat().st_size}-"
        mode = "ab"

    resp = session.get(url, stream=True, headers=headers, timeout=120)
    if resp.status_code == 416:  # already fully downloaded
        part.rename(dest)
        return dest
    if resp.status_code == 200 and mode == "ab":
        mode = "wb"  # server ignored Range — start over
    resp.raise_for_status()

    with open(part, mode) as f:
        for chunk in resp.iter_content(DOWNLOAD_CHUNK):
            f.write(chunk)
    part.rename(dest)
    return dest


def _process_book(
    book: LibrivoxBook,
    gutenberg_id: int,
    db: Db,
    storage: StorageBackend,
    session: requests.Session,
    device: Optional[str],
    tmp_root: Path,
) -> None:
    audiobook_id = db.upsert_audiobook(
        gutenberg_id=gutenberg_id,
        librivox_id=book.librivox_id,
        title=book.title,
        narrator=book.narrator,
        language=book.language,
    )

    for track in tqdm(book.tracks, desc=book.title[:40], unit="track",
                      leave=False):
        if db.is_track_processed(book.librivox_id, track.number):
            continue
        raw = tmp_root / f"{book.librivox_id}_{track.number:03d}_raw.mp3"
        cleaned = tmp_root / f"{book.librivox_id}_{track.number:03d}.mp3"
        try:
            _download(session, track.url, raw)
            # No chapter openings in our schema — marker-based cuts only;
            # anything suspicious lands in the log as validation='failed'.
            audit = audio_cleaner.trim_track(
                str(raw), str(cleaned), book_opening=None, device=device
            )
            key = f"audiobooks/{book.librivox_id}/{track.number:03d}.mp3"
            url = storage.upload(str(cleaned), key)
            db.upsert_track(
                audiobook_id, track.number, track.title,
                audit["new_duration"], url,
            )
            db.log_processing(
                book.librivox_id, track.number, track.url, key, audit
            )
        except Exception as e:  # keep the run alive; this track shows as gap
            log.error("Track %s/%d failed: %s",
                      book.librivox_id, track.number, e)
        finally:
            raw.unlink(missing_ok=True)
            cleaned.unlink(missing_ok=True)

    db.refresh_total_duration(audiobook_id)
    db.set_has_audio(gutenberg_id)


def _run_feed(args: argparse.Namespace) -> None:
    languages = (
        {lang.strip().lower() for lang in args.languages.split(",") if lang.strip()}
        if args.languages
        else None
    )
    client = LibrivoxClient()
    session = requests.Session()
    session.headers["User-Agent"] = "MarginaliaIngestion/0.1"

    with Db() as db, tempfile.TemporaryDirectory(prefix="marg_ingest_") as tmp:
        storage = make_storage()
        books = client.iter_audiobooks(offset=args.offset, limit=args.limit)
        for book in tqdm(books, total=args.limit, desc="audiobooks",
                         unit="book"):
            if languages and (book.language or "").lower()[:2] not in {
                lang[:2] for lang in languages
            }:
                continue
            result = match(book, db)
            if result.gutenberg_id is None:
                log.info(
                    "needs manual match: librivox=%s '%s' by %s (best %.0f)",
                    book.librivox_id, book.title, book.authors,
                    result.confidence,
                )
                continue
            log.info(
                "matched: librivox=%s '%s' → gutenberg=%s (%s, %.0f)",
                book.librivox_id, book.title, result.gutenberg_id,
                result.method, result.confidence,
            )
            if args.dry_run:
                continue
            _process_book(
                book, result.gutenberg_id, db, storage, session,
                args.device, Path(tmp),
            )


def _run_manual_review(args: argparse.Namespace) -> None:
    """Reprocess only the tracks whose validation previously failed."""
    client = LibrivoxClient()
    session = requests.Session()
    session.headers["User-Agent"] = "MarginaliaIngestion/0.1"

    with Db() as db, tempfile.TemporaryDirectory(prefix="marg_review_") as tmp:
        storage = make_storage()
        failed = db.failed_tracks()
        log.info("%d failed tracks to reprocess", len(failed))
        for row in tqdm(failed, unit="track"):
            librivox_id = row["librivox_id"]
            number = row["track_number"]
            url = row["input_url"]
            if not url:
                log.warning("No input_url for %s/%d — skipping",
                            librivox_id, number)
                continue
            raw = Path(tmp) / f"{librivox_id}_{number:03d}_raw.mp3"
            cleaned = Path(tmp) / f"{librivox_id}_{number:03d}.mp3"
            try:
                _download(session, url, raw)
                audit = audio_cleaner.trim_track(
                    str(raw), str(cleaned), device=args.device
                )
                key = f"audiobooks/{librivox_id}/{number:03d}.mp3"
                storage.upload(str(cleaned), key)
                db.log_processing(librivox_id, number, url, key, audit)
            except Exception as e:
                log.error("Reprocess %s/%d failed: %s", librivox_id, number, e)
            finally:
                raw.unlink(missing_ok=True)
                cleaned.unlink(missing_ok=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="LibriVox ingestion & audio-cleaning pipeline")
    parser.add_argument("--limit", type=int, default=20,
                        help="max audiobooks to process (default 20)")
    parser.add_argument("--offset", type=int, default=0,
                        help="feed offset to start from")
    parser.add_argument("--languages", type=str, default="",
                        help="comma-separated language filter, e.g. en,fr,ar")
    parser.add_argument("--dry-run", action="store_true",
                        help="match + log only, no download/processing")
    parser.add_argument("--device", choices=["cpu", "cuda"], default=None,
                        help="Whisper device (default: WHISPER_DEVICE or cpu)")
    parser.add_argument("--manual-review-only", action="store_true",
                        help="reprocess only previously failed tracks")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if "DATABASE_URL" not in os.environ:
        parser.error("DATABASE_URL is not set (see .env.example)")

    if args.manual_review_only:
        _run_manual_review(args)
    else:
        _run_feed(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

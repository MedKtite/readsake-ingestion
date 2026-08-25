"""CLI and lightweight UTC scheduler for Gutenberg Home snapshots."""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from .db import Db
from .gutenberg.trending import sync_gutenberg_discovery

LOGGER = logging.getLogger(__name__)


def _parse_daily_at(raw: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = raw.split(":", maxsplit=1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("expected HH:MM in UTC") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise argparse.ArgumentTypeError("expected HH:MM in UTC")
    return hour, minute


def _next_run(hour: int, minute: int) -> datetime:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def _run(args: argparse.Namespace) -> dict:
    with Db() as db:
        return sync_gutenberg_discovery(
            db,
            book_limit=args.book_limit,
            author_limit=args.author_limit,
            enrich_authors=not args.skip_author_enrichment,
        )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Publish DB-backed Gutenberg trending books and top authors"
    )
    parser.add_argument("--book-limit", type=int, default=100)
    parser.add_argument("--author-limit", type=int, default=12)
    parser.add_argument(
        "--skip-author-enrichment",
        action="store_true",
        help="do not call Wikipedia for author portraits and biographies",
    )
    parser.add_argument(
        "--daily-at",
        type=_parse_daily_at,
        metavar="HH:MM",
        help="stay running and refresh daily at this UTC time",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.daily_at is None:
        result = _run(args)
        LOGGER.info("Published Gutenberg discovery snapshot: %s", result)
        return

    hour, minute = args.daily_at
    while True:
        target = _next_run(hour, minute)
        LOGGER.info("Next Gutenberg discovery sync at %s", target.isoformat())
        time.sleep(max(0.0, (target - datetime.now(timezone.utc)).total_seconds()))
        try:
            result = _run(args)
            LOGGER.info("Published Gutenberg discovery snapshot: %s", result)
        except Exception:
            # The atomic publisher has not touched the previous snapshot.
            LOGGER.exception(
                "Gutenberg discovery sync failed; previous snapshot retained"
            )


if __name__ == "__main__":
    main()

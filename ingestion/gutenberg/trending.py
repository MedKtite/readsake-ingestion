"""Build and atomically publish Gutenberg Home discovery snapshots."""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any, Optional
from urllib.parse import quote

import requests

from ..db import Db

LOGGER = logging.getLogger(__name__)

GUTENDEX_URL = os.getenv("GUTENDEX_URL", "https://gutendex.com/books/")
GUTENBERG_TOP_URL = os.getenv(
    "GUTENBERG_TOP_URL",
    "https://www.gutenberg.org/browse/scores/top",
)
WIKIPEDIA_SUMMARY_URL = (
    "https://en.wikipedia.org/api/rest_v1/page/summary/"
)
REQUEST_ATTEMPTS = 3
EXCLUDED_AUTHORS = {
    "anonymous",
    "unknown",
    "various",
    "various authors",
}


def _request_json(
    session: requests.Session,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    attempts: int = REQUEST_ATTEMPTS,
) -> dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(
                url,
                params=params,
                timeout=(5, 30),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object from {url}")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.5 * (2 ** (attempt - 1)))
    assert last_error is not None
    raise last_error


def fetch_popular_books(
    session: requests.Session,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch enough pages of Gutendex's English popularity ordering."""
    books: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_pages: set[str] = set()
    next_url: Optional[str] = GUTENDEX_URL
    params: Optional[dict[str, Any]] = {
        "languages": "en",
        "sort": "popular",
    }

    while next_url and len(books) < limit:
        if next_url in seen_pages:
            raise ValueError(f"Gutendex pagination loop at {next_url}")
        seen_pages.add(next_url)
        payload = _request_json(session, next_url, params=params)
        params = None  # `next` already contains its own query string.

        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("Gutendex response has no results list")
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                gutenberg_id = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if gutenberg_id in seen_ids:
                continue
            seen_ids.add(gutenberg_id)
            books.append(item)
            if len(books) >= limit:
                break

        raw_next = payload.get("next")
        next_url = raw_next if isinstance(raw_next, str) and raw_next else None

    if not books:
        raise ValueError("Gutendex returned no popular books")
    return books


def fetch_gutenberg_rankings(
    session: requests.Session,
    *,
    book_limit: int,
    author_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read Project Gutenberg's official daily book and author rankings."""
    response = session.get(GUTENBERG_TOP_URL, timeout=(5, 30))
    response.raise_for_status()
    source = response.text
    book_heading = re.search(
        r"<h2[^>]*>\s*Top 100 EBooks yesterday\s*</h2>",
        source,
        flags=re.IGNORECASE,
    )
    author_heading = re.search(
        r"<h2[^>]*>\s*Top 100 Authors yesterday\s*</h2>",
        source,
        flags=re.IGNORECASE,
    )
    if not book_heading or not author_heading:
        raise ValueError("Project Gutenberg top page has an unknown layout")

    next_heading = re.search(
        r"<h2[^>]*>\s*Top 100 EBooks last 7 days\s*</h2>",
        source[author_heading.end() :],
        flags=re.IGNORECASE,
    )
    author_end = (
        author_heading.end() + next_heading.start()
        if next_heading
        else len(source)
    )
    book_html = source[book_heading.end() : author_heading.start()]
    author_html = source[author_heading.end() : author_end]

    books: list[dict[str, Any]] = []
    for gutenberg_id, label in re.findall(
        r'<a[^>]+href=["\']/ebooks/(\d+)["\'][^>]*>(.*?)</a>',
        book_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        text = unescape(re.sub(r"<[^>]+>", "", label)).strip()
        match = re.search(r"\(([0-9,]+)\)\s*$", text)
        books.append(
            {
                "id": int(gutenberg_id),
                "download_count": int(match.group(1).replace(",", ""))
                if match
                else 0,
                "authors": [],
            }
        )
        if len(books) >= book_limit:
            break

    authors: list[dict[str, Any]] = []
    for label in re.findall(
        r'<a[^>]+href=["\']/browse/authors/[^"\']+["\'][^>]*>(.*?)</a>',
        author_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        text = unescape(re.sub(r"<[^>]+>", "", label)).strip()
        match = re.search(r"\(([0-9,]+)\)\s*$", text)
        if not match:
            continue
        raw_name = text[: match.start()].strip()
        name = _display_author(raw_name)
        key = _author_key(name)
        if not key or key in EXCLUDED_AUTHORS:
            continue
        authors.append(
            {
                "author_key": key,
                "author_name": name,
                "rank": len(authors) + 1,
                "download_count": int(match.group(1).replace(",", "")),
                "book_count": 0,
            }
        )
        if len(authors) >= author_limit:
            break

    if not books or not authors:
        raise ValueError("Project Gutenberg top page contained no rankings")
    return books, authors


def _display_author(raw_name: str) -> str:
    """Convert Project Gutenberg's `Last, First, dates` form for display."""
    without_dates = re.sub(r",?\s*\d{3,4}(?:-\d{0,4})?\s*$", "", raw_name)
    without_expansion = re.sub(r"\s+\([^)]*\)\s*$", "", without_dates)
    parts = [
        part.strip() for part in without_expansion.split(",") if part.strip()
    ]
    if len(parts) >= 2:
        return f"{' '.join(parts[1:])} {parts[0]}".strip()
    return without_expansion.strip()


def _author_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def build_snapshot(
    raw_books: list[dict[str, Any]],
    *,
    known_ids: set[int],
    author_limit: int,
    ranked_authors: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep canonical catalog rows and aggregate their authors by downloads."""
    ranked_books: list[dict[str, Any]] = []
    author_totals: dict[str, dict[str, Any]] = {}

    for item in raw_books:
        gutenberg_id = int(item["id"])
        if gutenberg_id not in known_ids:
            continue
        downloads = max(0, int(item.get("download_count") or 0))
        ranked_books.append(
            {
                "gutenberg_id": gutenberg_id,
                "rank": len(ranked_books) + 1,
                "download_count": downloads,
            }
        )

        seen_for_book: set[str] = set()
        raw_authors = item.get("authors")
        if not isinstance(raw_authors, list):
            continue
        for raw_author in raw_authors:
            if not isinstance(raw_author, dict):
                continue
            raw_name = raw_author.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            name = _display_author(raw_name)
            key = _author_key(name)
            if key in EXCLUDED_AUTHORS or key in seen_for_book:
                continue
            seen_for_book.add(key)
            aggregate = author_totals.setdefault(
                key,
                {
                    "author_key": key,
                    "author_name": name,
                    "download_count": 0,
                    "book_count": 0,
                },
            )
            aggregate["download_count"] += downloads
            aggregate["book_count"] += 1

    authors = ranked_authors or sorted(
        author_totals.values(),
        key=lambda row: (
            -int(row["download_count"]),
            -int(row["book_count"]),
            str(row["author_name"]).lower(),
        ),
    )[:author_limit]
    for rank, author in enumerate(authors, start=1):
        author["rank"] = rank
    return ranked_books, authors


def enrich_author_portraits(
    session: requests.Session,
    authors: list[dict[str, Any]],
    previous: dict[str, dict],
) -> None:
    """Populate portraits in the batch; retain yesterday's data on a miss."""
    for author in authors:
        key = str(author["author_key"])
        old = previous.get(key, {})
        author["image_url"] = old.get("image_url")
        author["tagline"] = old.get("tagline")
        author["bio"] = old.get("bio")
        try:
            payload = _request_json(
                session,
                WIKIPEDIA_SUMMARY_URL
                + quote(str(author["author_name"]), safe=""),
                attempts=2,
            )
            if payload.get("type") == "disambiguation":
                continue
            original = payload.get("originalimage")
            thumbnail = payload.get("thumbnail")
            image_url = None
            if isinstance(original, dict):
                image_url = original.get("source")
            if not image_url and isinstance(thumbnail, dict):
                image_url = thumbnail.get("source")
            if isinstance(image_url, str):
                author["image_url"] = image_url
            if isinstance(payload.get("description"), str):
                author["tagline"] = payload["description"]
            if isinstance(payload.get("extract"), str):
                author["bio"] = payload["extract"]
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning(
                "Wikipedia enrichment failed for %s: %s",
                author["author_name"],
                exc,
            )


def sync_gutenberg_discovery(
    db: Db,
    *,
    book_limit: int = 100,
    author_limit: int = 12,
    enrich_authors: bool = True,
) -> dict[str, Any]:
    """Fetch, validate, enrich, then atomically publish one snapshot."""
    if book_limit < 1 or author_limit < 1:
        raise ValueError("book_limit and author_limit must be positive")

    with requests.Session() as session:
        session.headers["User-Agent"] = "Marginalia-Ingestion/1.0"
        try:
            raw_books, official_authors = fetch_gutenberg_rankings(
                session,
                book_limit=book_limit,
                author_limit=author_limit,
            )
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning(
                "Project Gutenberg ranking fetch failed; trying Gutendex: %s",
                exc,
            )
            raw_books = fetch_popular_books(session, limit=book_limit)
            official_authors = None
        ids = [int(item["id"]) for item in raw_books]
        known_ids = db.known_gutenberg_ids(ids)
        books, authors = build_snapshot(
            raw_books,
            known_ids=known_ids,
            author_limit=author_limit,
            ranked_authors=official_authors,
        )

        minimum_books = min(12, book_limit)
        if len(books) < minimum_books:
            raise ValueError(
                "Refusing to replace the previous snapshot: "
                f"only {len(books)} of {minimum_books} required books exist "
                "in gutenberg_catalog"
            )
        if not authors:
            raise ValueError(
                "Refusing to replace the previous snapshot: no authors found"
            )

        previous = db.top_author_metadata(
            [str(author["author_key"]) for author in authors]
        )
        if enrich_authors:
            enrich_author_portraits(session, authors, previous)
        else:
            for author in authors:
                old = previous.get(str(author["author_key"]), {})
                author["image_url"] = old.get("image_url")
                author["tagline"] = old.get("tagline")
                author["bio"] = old.get("bio")

    synced_at = datetime.now(timezone.utc)
    db.replace_gutenberg_discovery_snapshot(books, authors, synced_at)
    return {
        "books": len(books),
        "authors": len(authors),
        "portraits": sum(bool(row.get("image_url")) for row in authors),
        "synced_at": synced_at.isoformat(),
    }

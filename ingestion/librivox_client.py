"""Polite LibriVox API client (≤1 request/second, retry with backoff).

API: https://librivox.org/api/feed/audiobooks?format=json&extended=1
Section MP3s are hosted on archive.org. `url_text_source` usually points at
the Project Gutenberg text — that link is the reliable match key.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

import requests

log = logging.getLogger(__name__)

API_URL = "https://librivox.org/api/feed/audiobooks"
USER_AGENT = "ReadsakeIngestion/0.1 (offline catalog pipeline)"
PAGE_SIZE = 50
MIN_REQUEST_INTERVAL_S = 1.0
MAX_RETRIES = 4

GUTENBERG_URL_RE = re.compile(
    r"gutenberg\.org/(?:etext|ebooks|files|cache/epub)/(\d+)", re.IGNORECASE
)


def gutenberg_id_from_url(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    m = GUTENBERG_URL_RE.search(url)
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class LibrivoxTrack:
    number: int
    title: Optional[str]
    url: str  # archive.org MP3 (https)
    playtime_seconds: Optional[int]


@dataclass(frozen=True)
class LibrivoxBook:
    librivox_id: int
    title: str
    authors: str  # "First Last, First Last"
    narrator: Optional[str]  # single reader's name or 'Various readers'
    language: Optional[str]
    total_seconds: Optional[int]
    gutenberg_id: Optional[int]  # from url_text_source when present
    tracks: list[LibrivoxTrack] = field(default_factory=list)


class LibrivoxClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._last_request = 0.0

    def _get(self, params: dict) -> Optional[dict]:
        for attempt in range(MAX_RETRIES):
            wait = self._last_request + MIN_REQUEST_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self._session.get(API_URL, params=params, timeout=60)
                if resp.status_code == 200:
                    return resp.json()
                log.warning("LibriVox HTTP %s (attempt %d)",
                            resp.status_code, attempt + 1)
            except requests.RequestException as e:
                log.warning("LibriVox request failed: %s (attempt %d)",
                            e, attempt + 1)
            time.sleep(2 ** attempt)
        return None

    def iter_audiobooks(
        self,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> Iterator[LibrivoxBook]:
        """Paginate the whole feed, yielding parsed books."""
        yielded = 0
        while limit is None or yielded < limit:
            data = self._get({
                "format": "json",
                "extended": "1",
                "limit": PAGE_SIZE,
                "offset": offset,
            })
            books = (data or {}).get("books")
            if not books:
                return  # end of feed (or {"error": …})
            for raw in books:
                book = _parse_book(raw)
                if book is None:
                    continue
                yield book
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            offset += PAGE_SIZE


def _parse_book(raw: dict) -> Optional[LibrivoxBook]:
    try:
        librivox_id = int(raw["id"])
        title = (raw.get("title") or "").strip()
        if not title:
            return None
    except (KeyError, ValueError, TypeError):
        return None

    tracks: list[LibrivoxTrack] = []
    readers: set[str] = set()
    for i, sec in enumerate(raw.get("sections") or [], start=1):
        url = (sec.get("listen_url") or "").strip()
        if not url:
            continue
        for r in sec.get("readers") or []:
            name = (r.get("display_name") or "").strip()
            if name:
                readers.add(name)
        tracks.append(LibrivoxTrack(
            number=i,
            title=(sec.get("title") or "").strip() or None,
            url=url.replace("http://", "https://", 1),
            playtime_seconds=_parse_playtime(sec.get("playtime")),
        ))
    if not tracks:
        return None

    authors = ", ".join(
        name
        for a in (raw.get("authors") or [])
        if (name := f"{a.get('first_name', '')} {a.get('last_name', '')}".strip())
    )

    narrator = None
    if len(readers) == 1:
        narrator = next(iter(readers))
    elif readers:
        narrator = "Various readers"

    total = raw.get("totaltimesecs")
    return LibrivoxBook(
        librivox_id=librivox_id,
        title=title,
        authors=authors,
        narrator=narrator,
        language=(raw.get("language") or "").strip() or None,
        total_seconds=int(total) if isinstance(total, int) and total > 0 else None,
        gutenberg_id=gutenberg_id_from_url(raw.get("url_text_source")),
        tracks=tracks,
    )


def _parse_playtime(raw: Optional[str]) -> Optional[int]:
    """LibriVox playtime is usually plain seconds ("1030"), rarely "H:MM:SS"."""
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        if ":" not in raw:
            return int(raw)
        total = 0
        for part in raw.split(":"):
            total = total * 60 + int(part)
        return total
    except ValueError:
        return None

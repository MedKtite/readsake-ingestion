"""Match a LibriVox audiobook to a row in gutenberg_catalog.

Primary: the Gutenberg id embedded in LibriVox's `url_text_source` — when
present and known to our catalog, that's a certain match.

Fallback: normalized-title fuzzy match (rapidfuzz) restricted to candidates
that share an author surname. Below the confidence threshold we NEVER guess —
the book is skipped and logged as "needs manual match".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Protocol

from rapidfuzz import fuzz

from .librivox_client import LibrivoxBook

FUZZY_THRESHOLD = 88  # 0–100; below this a fuzzy match is rejected


class CatalogDb(Protocol):
    """The slice of Db the matcher needs (duck-typed for tests)."""

    def find_book_by_gutenberg_id(self, gutenberg_id: int) -> Optional[dict]: ...

    def search_books_by_title(self, title_norm: str, limit: int = 25) -> list[dict]: ...


@dataclass(frozen=True)
class MatchResult:
    gutenberg_id: Optional[int]  # None → no confident match
    confidence: float  # 0–100
    method: str  # 'gutenberg_id' | 'fuzzy' | 'none'


def normalize(s: Optional[str]) -> str:
    """lower / unaccent / alnum-only — mirrors the backend's title_norm."""
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_only.lower())


def _surname_tokens(authors: Optional[str]) -> set[str]:
    """Author name tokens worth comparing (≥3 chars, no initials/dates)."""
    if not authors:
        return set()
    return {
        t
        for t in re.split(r"[^a-zA-Z]+", unicodedata.normalize("NFKD", authors)
                          .encode("ascii", "ignore").decode("ascii").lower())
        if len(t) >= 3
    }


def authors_overlap(a: Optional[str], b: Optional[str]) -> bool:
    ta, tb = _surname_tokens(a), _surname_tokens(b)
    if not ta or not tb:
        return False
    return bool(ta & tb)


def match(book: LibrivoxBook, db: CatalogDb) -> MatchResult:
    # Primary — LibriVox told us the Gutenberg text it was read from.
    if book.gutenberg_id is not None:
        row = db.find_book_by_gutenberg_id(book.gutenberg_id)
        if row is not None:
            return MatchResult(book.gutenberg_id, 100.0, "gutenberg_id")

    # Fallback — fuzzy title + author-surname overlap. Never guess below
    # the threshold.
    title_norm = normalize(book.title)
    if not title_norm:
        return MatchResult(None, 0.0, "none")

    best_id: Optional[int] = None
    best_score = 0.0
    for candidate in db.search_books_by_title(title_norm):
        if not authors_overlap(book.authors, candidate.get("authors")):
            continue
        score = fuzz.token_sort_ratio(
            book.title.lower(), (candidate.get("title") or "").lower()
        )
        if score > best_score:
            best_score = score
            best_id = candidate["gutenberg_id"]

    if best_id is not None and best_score >= FUZZY_THRESHOLD:
        return MatchResult(best_id, float(best_score), "fuzzy")
    return MatchResult(None, float(best_score), "none")

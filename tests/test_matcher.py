"""Matcher — Gutenberg-URL extraction, normalization, fuzzy thresholds."""

from ingestion.librivox_client import LibrivoxBook, gutenberg_id_from_url
from ingestion.matcher import MatchResult, authors_overlap, match, normalize


class FakeDb:
    """Duck-typed CatalogDb backed by an in-memory list of rows."""

    def __init__(self, rows):
        self._rows = rows

    def find_book_by_gutenberg_id(self, gutenberg_id):
        return next(
            (r for r in self._rows if r["gutenberg_id"] == gutenberg_id), None)

    def search_books_by_title(self, title_norm, limit=25):
        return [r for r in self._rows if title_norm in r["title_norm"]][:limit]


def book(title, authors, gutenberg_id=None):
    return LibrivoxBook(
        librivox_id=1, title=title, authors=authors, narrator=None,
        language="English", total_seconds=None, gutenberg_id=gutenberg_id,
        tracks=[],
    )


PRIDE = {
    "gutenberg_id": 1342,
    "title": "Pride and Prejudice",
    "authors": "Austen, Jane, 1775-1817",
    "title_norm": "prideandprejudice",
}


# ── gutenberg_id_from_url ───────────────────────────────────────────────────

def test_gutenberg_id_from_etext_url():
    assert gutenberg_id_from_url("https://www.gutenberg.org/etext/1342") == 1342


def test_gutenberg_id_from_ebooks_url():
    assert gutenberg_id_from_url("http://gutenberg.org/ebooks/84") == 84


def test_gutenberg_id_from_unrelated_url():
    assert gutenberg_id_from_url("https://example.com/books/12") is None
    assert gutenberg_id_from_url(None) is None


# ── normalize / authors_overlap ─────────────────────────────────────────────

def test_normalize_strips_accents_case_and_punctuation():
    assert normalize("Les Misérables, Vol. I!") == "lesmiserablesvoli"


def test_authors_overlap_across_name_orders():
    assert authors_overlap("Jane Austen", "Austen, Jane, 1775-1817")
    assert not authors_overlap("Jane Austen", "Melville, Herman")
    assert not authors_overlap("", "Austen, Jane")


# ── match ───────────────────────────────────────────────────────────────────

def test_primary_match_by_gutenberg_id():
    result = match(book("Pride and Prejudice", "Jane Austen",
                        gutenberg_id=1342), FakeDb([PRIDE]))
    assert result == MatchResult(1342, 100.0, "gutenberg_id")


def test_fuzzy_match_close_title_same_author():
    result = match(book("Pride and Prejudice", "Jane Austen"), FakeDb([PRIDE]))
    assert result.gutenberg_id == 1342
    assert result.method == "fuzzy"
    assert result.confidence >= 88


def test_fuzzy_rejects_different_author():
    result = match(book("Pride and Prejudice", "Herman Melville"),
                   FakeDb([PRIDE]))
    assert result.gutenberg_id is None
    assert result.method == "none"


def test_fuzzy_never_guesses_below_threshold():
    other = {
        "gutenberg_id": 999,
        "title": "Pride: A Comprehensive History of Prejudice in Europe",
        "authors": "Austen, Jane",
        "title_norm": "prideacomprehensivehistoryofprejudiceineurope",
    }
    # Candidate found (title_norm contains nothing of the query) — craft a
    # query whose normalized form IS contained, but whose score stays low.
    result = match(book("Pride", "Jane Austen"), FakeDb([other]))
    assert result.gutenberg_id is None
    assert result.method == "none"


def test_unknown_gutenberg_id_falls_back_to_fuzzy():
    # LibriVox pointed at a Gutenberg text our catalog doesn't have.
    result = match(book("Pride and Prejudice", "Jane Austen",
                        gutenberg_id=555555), FakeDb([PRIDE]))
    assert result.gutenberg_id == 1342
    assert result.method == "fuzzy"

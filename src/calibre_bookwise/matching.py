"""Match Calibre books to books in the user's Readwise Books library.

A session can only be recorded against a Bookwise book id, so each Calibre
book is either matched to a book already in the Bookwise library or added
with books_create_books. Matching order:

1. a mapping saved earlier (Calibre id -> Bookwise id)
2. ISBN (normalised to ISBN-13) against the Bookwise book's catalog product
3. ASIN
4. main title + an author surname. Bookwise often shortens titles
   ("JavaScript" for "JavaScript: The Good Parts", "Code Complete" for
   "Code Complete, 2nd Edition"), so exact titles would miss them.

Anything unmatched is planned as a create: by ISBN when Calibre has one (the
API matches it to the catalog), else by title + author, which the API only
matches when both are exact.
Stdlib only: this has to run inside Calibre's bundled Python 3.11.
"""

import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

CREATE_STATUS = "currently_reading"  # books are created when a session is read
BATCH_SIZE = 10  # books_create_books takes at most 10 items
UNKNOWN_AUTHORS = {"", "unknown"}


@dataclass(frozen=True)
class CalibreBook:
    calibre_id: int
    title: str
    authors: tuple[str, ...]
    isbn: str | None = None
    asin: str | None = None


@dataclass(frozen=True)
class Match:
    book: CalibreBook
    bookwise_id: int | None  # None: needs creating (or can't be created)
    matched_by: str | None  # saved | isbn | asin | title_author
    bookwise: dict = field(default_factory=dict)  # the matched Bookwise book
    create_item: dict | None = None  # books_create_books item, when creating
    problem: str | None = None  # why it can't be matched or created

    @property
    def action(self) -> str:
        if self.bookwise_id is not None:
            return "use existing"
        return "create" if self.create_item else "cannot match"


def normalize_isbn(value: str | None) -> str | None:
    """ISBN-13 digits, converting ISBN-10; None if it isn't a valid ISBN."""
    digits = re.sub(r"[^0-9Xx]", "", value or "").upper()
    if len(digits) == 10 and _isbn10_valid(digits):
        core = "978" + digits[:9]
        return core + _isbn13_check_digit(core)
    if len(digits) == 13 and digits.isdigit():
        return digits if _isbn13_check_digit(digits[:12]) == digits[12] else None
    return None


def _isbn10_valid(digits: str) -> bool:
    if not digits[:9].isdigit() or not (digits[9].isdigit() or digits[9] == "X"):
        return False
    values = [int(d) for d in digits[:9]] + [10 if digits[9] == "X" else int(digits[9])]
    return sum(v * w for v, w in zip(values, range(10, 0, -1))) % 11 == 0


def _isbn13_check_digit(first12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return str((10 - total % 10) % 10)


def main_title(title: str) -> str:
    """Lowercase title without subtitle, edition, punctuation or accents."""
    title = re.split(
        r"\s*[:(]|,\s*\d+(?:st|nd|rd|th)\s+ed", title, maxsplit=1, flags=re.IGNORECASE
    )[0]
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^\w\s]", " ", title.casefold()).split())


def surnames(authors: Iterable[str]) -> set[str]:
    names = set()
    for author in authors:
        for person in re.split(r"\s*(?:&|,| and )\s*", author):
            words = re.sub(r"[^\w\s-]", "", person).split()
            if words and person.strip().casefold() not in UNKNOWN_AUTHORS:
                names.add(words[-1].casefold())
    return names


def plan_matches(
    books: Iterable[CalibreBook],
    bookwise_books: Iterable[dict],
    saved: Mapping[int, int] | None = None,
) -> list[Match]:
    """Decide, per Calibre book, which Bookwise book to use or how to create one.

    bookwise_books are books from books_list_books (id, title, author, status,
    product with isbn/asin). Nothing here calls the API.
    """
    saved = saved or {}
    # Readwise can hold the same book twice, e.g. a finished copy and a deleted
    # one; put deleted books last so a live copy wins the match.
    remote = sorted(bookwise_books, key=lambda b: b.get("status") == "deleted")
    by_id = {b["id"]: b for b in remote}
    by_isbn, by_asin = {}, {}
    for b in remote:
        product = b.get("product") or {}
        if isbn := normalize_isbn(product.get("isbn")):
            by_isbn.setdefault(isbn, b)
        if asin := (product.get("asin") or "").upper():
            by_asin.setdefault(asin, b)

    matches = []
    for book in books:
        if book.calibre_id in saved:
            bookwise_id = saved[book.calibre_id]
            matches.append(
                Match(book, bookwise_id, "saved", by_id.get(bookwise_id, {}))
            )
            continue
        isbn = normalize_isbn(book.isbn)
        if isbn and isbn in by_isbn:
            matches.append(Match(book, by_isbn[isbn]["id"], "isbn", by_isbn[isbn]))
            continue
        if book.asin and book.asin.upper() in by_asin:
            found = by_asin[book.asin.upper()]
            matches.append(Match(book, found["id"], "asin", found))
            continue
        found = _title_author_match(book, remote)
        if found:
            matches.append(Match(book, found["id"], "title_author", found))
            continue
        matches.append(_create(book, isbn))
    return matches


def _title_author_match(book: CalibreBook, remote: list[dict]) -> dict | None:
    title, authors = main_title(book.title), surnames(book.authors)
    if not title or not authors:
        return None
    for candidate in remote:
        candidate_titles = {
            main_title(candidate.get("title") or ""),
            main_title((candidate.get("product") or {}).get("title") or ""),
        }
        if title in candidate_titles and authors & surnames(
            [candidate.get("author") or ""]
        ):
            return candidate
    return None


def _create(book: CalibreBook, isbn: str | None) -> Match:
    if isbn:
        product = {"isbn": isbn}
    elif book.asin:
        product = {"asin": book.asin}
    elif surnames(book.authors):
        product = {"title": book.title, "author": " & ".join(book.authors)}
    else:
        return Match(
            book, None, None, problem="no ISBN, ASIN or known author to create it from"
        )
    return Match(
        book, None, None, create_item={"product": product, "status": CREATE_STATUS}
    )


def create_requests(matches: Iterable[Match]) -> list[dict]:
    """books_create_books request bodies (batches of 10) for the planned creates."""
    items = [m.create_item for m in matches if m.create_item]
    return [
        {"items": items[start : start + BATCH_SIZE]}
        for start in range(0, len(items), BATCH_SIZE)
    ]


def read_calibre_books(
    library: Path, calibre_ids: Iterable[int] | None = None
) -> list[CalibreBook]:
    """Books from a Calibre library's metadata.db, opened read-only.

    Inside a Calibre plugin, use the library API (db.field_for) instead.
    """
    uri = f"file:{library / 'metadata.db'}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute(
            """
            SELECT b.id, b.title,
                (SELECT group_concat(a.name, '\x1f') FROM books_authors_link l
                   JOIN authors a ON a.id = l.author WHERE l.book = b.id),
                (SELECT val FROM identifiers WHERE book = b.id AND type = 'isbn'),
                (SELECT val FROM identifiers WHERE book = b.id
                   AND type IN ('amazon', 'mobi-asin', 'asin') LIMIT 1)
            FROM books b ORDER BY b.id
            """
        ).fetchall()
    wanted = set(calibre_ids) if calibre_ids is not None else None
    return [
        CalibreBook(
            calibre_id=book_id,
            title=title,
            authors=tuple((authors or "").split("\x1f")) if authors else (),
            isbn=isbn or None,
            asin=asin or None,
        )
        for book_id, title, authors, isbn, asin in rows
        if wanted is None or book_id in wanted
    ]

"""Helpers for the scripts/ diagnostics; not packaged into the plugin.

Inside Calibre the plugin reads books through the library API and sends one
item per request, so it needs neither the SQLite reader nor batching.
"""

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .matching import CalibreBook, Match

BATCH_SIZE = 10  # the Books API takes at most 10 books or sessions per request


def batches(items: list[dict]) -> list[dict]:
    """Request bodies of at most BATCH_SIZE items each."""
    return [
        {"items": items[start : start + BATCH_SIZE]}
        for start in range(0, len(items), BATCH_SIZE)
    ]


def create_requests(matches: Iterable[Match]) -> list[dict]:
    """books_create_books request bodies for the planned creates."""
    return batches([m.create_item for m in matches if m.create_item])


def read_calibre_books(
    library: Path, calibre_ids: Iterable[int] | None = None
) -> list[CalibreBook]:
    """Books from a Calibre library's metadata.db, opened read-only."""
    uri = f"file:{library / 'metadata.db'}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute(
            """
            SELECT b.id, b.title,
                (SELECT group_concat(a.name, '\x1f') FROM books_authors_link l
                   JOIN authors a ON a.id = l.author WHERE l.book = b.id)
            FROM books b ORDER BY b.id
            """
        ).fetchall()
        identifiers: dict[int, dict[str, str]] = {}
        for book_id, kind, value in db.execute(
            "SELECT book, type, val FROM identifiers"
        ):
            identifiers.setdefault(book_id, {})[kind] = value
    wanted = set(calibre_ids) if calibre_ids is not None else None
    return [
        CalibreBook.from_identifiers(
            book_id,
            title,
            authors.split("\x1f") if authors else (),
            identifiers.get(book_id, {}),
        )
        for book_id, title, authors in rows
        if wanted is None or book_id in wanted
    ]

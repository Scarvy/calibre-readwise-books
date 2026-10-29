"""Readwise Books data as Calibre custom column values.

COLUMNS defines the custom columns the plugin creates; column_updates turns
matched books and their Readwise data into {column: {calibre_id: value}},
ready for Calibre's set_field. Books not in Readwise get status "Not in
Readwise" and empty values elsewhere, so stale values are cleared.
Stdlib only: this has to run inside Calibre's bundled Python 3.11.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .matching import Match

STATUS_NAMES = {
    "unsorted": "Unsorted",
    "want_to_read": "Want to read",
    "currently_reading": "Currently reading",
    "finished": "Finished",
    "did_not_finish": "Did not finish",
    "deleted": "Deleted",
}
NOT_IN_READWISE = "Not in Readwise"


@dataclass(frozen=True)
class Column:
    label: str  # Calibre lookup name without "#"
    name: str  # heading shown in Calibre
    datatype: str
    display: dict = field(default_factory=dict)


COLUMNS = (
    Column(
        "rw_status",
        "Readwise status",
        "enumeration",
        {"enum_values": [*STATUS_NAMES.values(), NOT_IN_READWISE]},
    ),
    Column("rw_rating", "Readwise rating", "rating", {"allow_half_stars": True}),
    Column("rw_review", "Readwise review", "comments", {"interpret_as": "long-text"}),
    Column("rw_started", "Readwise started", "datetime"),
    Column("rw_finished", "Readwise finished", "datetime"),
    Column("rw_progress", "Readwise progress", "float", {"number_format": "{0:.1f}%"}),
    Column("rw_minutes", "Readwise minutes read", "int"),
    Column("rw_sessions", "Readwise sessions", "int"),
    Column("rw_last_read", "Readwise last read", "datetime"),
    Column("rw_pages", "Readwise pages", "int"),
    Column("rw_words", "Readwise words", "int"),
)


def column_updates(
    matches: Iterable[Match], sessions: Iterable[dict]
) -> dict[str, dict[int, object]]:
    """{column label: {calibre book id: value}} for every matched-or-not book."""
    by_book: dict[int, list[dict]] = {}
    for session in sessions:
        if not session.get("is_deleted"):
            by_book.setdefault(session["book_id"], []).append(session)
    updates: dict[str, dict[int, object]] = {column.label: {} for column in COLUMNS}
    for match in matches:
        values = book_values(match, by_book.get(match.bookwise_id or -1, []))
        for label, value in values.items():
            updates[label][match.book.calibre_id] = value
    return updates


def book_values(match: Match, sessions: list[dict]) -> dict[str, object]:
    """Column values for one Calibre book."""
    book = match.bookwise
    if match.bookwise_id is None or not book:
        return {column.label: None for column in COLUMNS} | {
            "rw_status": NOT_IN_READWISE
        }
    product = book.get("product") or {}
    rating = book.get("rating")
    progress = book.get("progress")
    seconds = sum(s.get("total_time_seconds") or 0 for s in sessions)
    last_read = max(
        (s["ended_at"] for s in sessions if s.get("ended_at")), default=None
    )
    return {
        "rw_status": STATUS_NAMES.get(book.get("status") or "", book.get("status")),
        # Readwise rates 0.25-5 in quarter steps; Calibre stores 0-10 (half stars).
        "rw_rating": int(rating * 2 + 0.5) if rating else None,
        "rw_review": book.get("review") or None,
        "rw_started": _date(book.get("started_reading_at")),
        "rw_finished": _date(book.get("finished_reading_at")),
        "rw_progress": round(progress * 100, 1) if progress is not None else None,
        "rw_minutes": round(seconds / 60) if sessions else None,
        "rw_sessions": len(sessions) or None,
        "rw_last_read": _date(last_read),
        "rw_pages": product.get("page_count") or None,
        "rw_words": product.get("word_count") or None,
    }


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None

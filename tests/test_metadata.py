from datetime import UTC, datetime

from calibre_bookwise.matching import CalibreBook, plan_matches
from calibre_bookwise.metadata import COLUMNS, NOT_IN_READWISE, column_updates

# Shaped like real GET /books and GET /sessions results (2026-10-29).
DESIGN = {
    "id": 888611,
    "title": "Design for Developers",
    "author": "Stephanie Stimac",
    "status": "currently_reading",
    "progress": 0.009531448660733332,
    "rating": 4.25,
    "review": "Practical.",
    "started_reading_at": "2026-10-16T17:39:11Z",
    "finished_reading_at": None,
    "product": {"isbn": "9781617299476", "page_count": 280, "word_count": 79811},
}
SESSIONS = [
    {"book_id": 888611, "total_time_seconds": 404, "ended_at": "2026-10-28T13:23:49Z"},
    {"book_id": 888611, "total_time_seconds": 304, "ended_at": "2026-10-29T04:24:29Z"},
    {
        "book_id": 888611,
        "total_time_seconds": 999,
        "ended_at": "2026-10-30T00:00:00Z",
        "is_deleted": True,
    },
    {"book_id": 2291, "total_time_seconds": 60, "ended_at": "2025-05-16T13:23:49Z"},
]


def updates_for(*books):
    return column_updates(plan_matches(books, [DESIGN]), SESSIONS)


def test_matched_book_gets_every_column():
    updates = updates_for(
        CalibreBook(11, "Design for Developers", ("Stephanie Stimac",), "9781617299476")
    )

    assert {label: values[11] for label, values in updates.items()} == {
        "rw_status": "Currently reading",
        "rw_rating": 9,  # 4.25 stars -> 8.5 half-stars, rounded up
        "rw_review": "Practical.",
        "rw_started": datetime(2026, 10, 16, 17, 39, 11, tzinfo=UTC),
        "rw_finished": None,
        "rw_progress": 1.0,
        "rw_minutes": 12,  # 404 + 304 seconds; the deleted session is ignored
        "rw_sessions": 2,
        "rw_last_read": datetime(2026, 10, 29, 4, 24, 29, tzinfo=UTC),
        "rw_pages": 280,
        "rw_words": 79811,
    }


def test_book_not_in_readwise_is_marked_and_other_columns_cleared():
    updates = updates_for(CalibreBook(1, "Quick Start Guide", ("John Schember",)))

    assert updates["rw_status"][1] == NOT_IN_READWISE
    assert all(updates[c.label][1] is None for c in COLUMNS if c.label != "rw_status")


def test_status_values_are_all_allowed_by_the_column():
    [status] = [c for c in COLUMNS if c.label == "rw_status"]
    updates = updates_for(
        CalibreBook(
            11, "Design for Developers", ("Stephanie Stimac",), "9781617299476"
        ),
        CalibreBook(1, "Quick Start Guide", ("John Schember",)),
    )

    assert set(updates["rw_status"].values()) <= set(status.display["enum_values"])

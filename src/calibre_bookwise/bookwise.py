"""Turn reading sessions into Readwise Books API session items.

Each item needs the Bookwise book id (not the Calibre id), start and end times,
format "ebook", and start/end positions as fractions of the book (0-1).
"""

import os
from datetime import UTC, timedelta

from .sessions import Session

# Opening a book and closing it again isn't a reading session.
MIN_DURATION = timedelta(minutes=1)
# The API rejects sessions where ended_at is more than 24h after started_at.
MAX_DURATION = timedelta(hours=24)
BATCH_SIZE = 10  # the API takes at most 10 sessions per request


def skip_reason(session: Session) -> str | None:
    if session.duration < MIN_DURATION:
        return f"shorter than {MIN_DURATION}"
    if session.duration > MAX_DURATION:
        return f"longer than {MAX_DURATION}"
    if session.start_progress is None or session.end_progress is None:
        return "no reading position"
    return None


def session_item(
    session: Session, bookwise_book_id: int, timezone: str | None = None
) -> dict:
    """The API item for one session; check skip_reason first."""
    item = {
        "book_id": bookwise_book_id,
        "format": "ebook",
        "started_at": _utc(session.start),
        "ended_at": _utc(session.end),
        "percent_at_start": _fraction(session.start_progress),
        "percent_at_end": _fraction(session.end_progress),
    }
    if timezone:
        item["timezone"] = timezone
    return item


def local_timezone() -> str | None:
    """IANA name of this Mac's time zone, e.g. America/New_York."""
    target = os.path.realpath("/etc/localtime")
    _, found, name = target.partition("/zoneinfo/")
    return name if found else None


def _utc(moment) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fraction(progress: float | None) -> float:
    return round(min(max(progress or 0.0, 0.0), 1.0), 4)

from datetime import UTC, datetime, timedelta, timezone

from calibre_bookwise.bookwise import session_item, skip_reason
from calibre_bookwise.sessions import Session

EASTERN = timezone(timedelta(hours=-4))
START = datetime(2026, 10, 28, 23, 40, 0, tzinfo=EASTERN)


def session(
    minutes: float = 3.5,
    start_progress: float | None = 0.4167,
    end_progress: float | None = 0.4583,
) -> Session:
    return Session(
        book_key="d1aafe7b9f",
        book_id=11,
        title="Design for Developers",
        start=START,
        end=START + timedelta(minutes=minutes),
        start_progress=start_progress,
        end_progress=end_progress,
        saves=12,
        longest_gap=timedelta(seconds=33),
    )


def test_session_item_has_api_fields_in_utc():
    item = session_item(session(), 888611, "America/New_York")

    assert item == {
        "book_id": 888611,
        "format": "ebook",
        "started_at": "2026-10-29T03:40:00Z",
        "ended_at": "2026-10-29T03:43:30Z",
        "percent_at_start": 0.4167,
        "percent_at_end": 0.4583,
        "timezone": "America/New_York",
    }


def test_session_item_omits_unknown_timezone_and_clamps_progress():
    item = session_item(session(start_progress=-0.1, end_progress=1.2), 1)

    assert "timezone" not in item
    assert (item["percent_at_start"], item["percent_at_end"]) == (0.0, 1.0)


def test_skip_reasons():
    assert skip_reason(session()) is None
    assert skip_reason(session(minutes=0.5)) == "shorter than 0:01:00"
    assert skip_reason(session(minutes=25 * 60)) == "longer than 1 day, 0:00:00"
    assert skip_reason(session(end_progress=None)) == "no reading position"


def test_utc_input_stays_utc():
    item = session_item(
        Session(
            book_key="k",
            book_id=1,
            title=None,
            start=datetime(2026, 10, 29, 3, 0, tzinfo=UTC),
            end=datetime(2026, 10, 29, 3, 5, tzinfo=UTC),
            start_progress=0.1,
            end_progress=0.2,
            saves=2,
            longest_gap=timedelta(minutes=5),
        ),
        1,
    )

    assert (item["started_at"], item["ended_at"]) == (
        "2026-10-29T03:00:00Z",
        "2026-10-29T03:05:00Z",
    )

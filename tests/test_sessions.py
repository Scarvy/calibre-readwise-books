import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from calibre_bookwise.sessions import (
    FocusChange,
    Save,
    build_sessions,
    focus_from_events,
    saves_from_events,
)

T0 = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)


FIXTURES = Path(__file__).parent / "fixtures"


def save_at(seconds: float, book: str = "a", progress: float | None = None) -> Save:
    return Save(book_key=book, at=T0 + timedelta(seconds=seconds), progress=progress)


def focus_at(seconds: float, viewer_focused: bool) -> FocusChange:
    return FocusChange(
        at=T0 + timedelta(seconds=seconds), viewer_focused=viewer_focused
    )


def sessions_from_log(name: str, **limits) -> list:
    with (FIXTURES / name).open() as log:
        events = [json.loads(line) for line in log]
    return build_sessions(
        saves_from_events(events), focus=focus_from_events(events), **limits
    )


def test_steady_page_turns_form_one_session():
    saves = [save_at(s, progress=p) for s, p in [(0, 0.1), (15, 0.2), (30, 0.3)]]

    [session] = build_sessions(saves)

    assert session.duration == timedelta(seconds=30)
    assert (session.start_progress, session.end_progress) == (0.1, 0.3)
    assert session.saves == 3
    assert session.longest_gap == timedelta(seconds=15)


def test_gap_over_idle_limit_splits_session():
    saves = [save_at(0), save_at(60), save_at(60 + 301), save_at(60 + 320)]

    first, second = build_sessions(saves, idle_limit=timedelta(minutes=5))

    assert (first.saves, second.saves) == (2, 2)
    assert first.end == T0 + timedelta(seconds=60)
    assert second.start == T0 + timedelta(seconds=361)


def test_gap_equal_to_idle_limit_does_not_split():
    saves = [save_at(0), save_at(300)]

    assert len(build_sessions(saves, idle_limit=timedelta(minutes=5))) == 1


def test_interleaved_books_get_separate_sessions():
    saves = [save_at(0, "a"), save_at(10, "b"), save_at(20, "a"), save_at(30, "b")]

    sessions = build_sessions(saves)

    assert [(s.book_key, s.saves) for s in sessions] == [("a", 2), ("b", 2)]


def test_unsorted_input_is_ordered_by_time():
    saves = [save_at(30), save_at(0), save_at(15)]

    [session] = build_sessions(saves)

    assert session.start == T0
    assert session.end == T0 + timedelta(seconds=30)


def test_missing_progress_uses_nearest_known_values():
    saves = [
        save_at(0),
        save_at(10, progress=0.2),
        save_at(20, progress=0.4),
        save_at(30),
    ]

    [session] = build_sessions(saves)

    assert (session.start_progress, session.end_progress) == (0.2, 0.4)


def test_single_save_is_a_zero_length_session():
    [session] = build_sessions([save_at(0)])

    assert session.duration == timedelta(0)
    assert session.longest_gap == timedelta(0)


def test_saves_from_events_skips_non_saves_and_duplicates():
    events = [
        {"event": "started"},
        {"event": "position_saved", "key": "a", "viewer_timestamp": None},
        {
            "event": "position_saved",
            "key": "a",
            "viewer_timestamp": "2026-10-29T02:00:00+00:00",
            "approx_progress": 0.1,
            "book_id": 1,
            "title": "Quick Start Guide",
        },
        {
            "event": "position_saved",
            "key": "a",
            "viewer_timestamp": "2026-10-29T02:00:00+00:00",
        },
        {"event": "annotations_changed", "key": "a"},
    ]

    [save] = saves_from_events(events)

    assert save == Save(
        book_key="a", at=T0, progress=0.1, book_id=1, title="Quick Start Guide"
    )


def test_saves_from_events_skips_positions_carried_over_from_old_reading():
    events = [
        {
            "event": "position_saved",
            "at": "2026-10-29T02:32:02.763+00:00",
            "key": "a",
            "viewer_timestamp": "2025-04-23T13:19:48.421134+00:00",
        },
        {
            "event": "position_saved",
            "at": "2026-10-29T02:32:06.076+00:00",
            "key": "a",
            "viewer_timestamp": "2026-10-29T02:32:02.557526+00:00",
        },
    ]

    [save] = saves_from_events(events)

    assert save.at == datetime(2026, 10, 29, 2, 32, 2, 557526, tzinfo=UTC)


DWELL = [save_at(0), save_at(20), save_at(20 + 420), save_at(20 + 440)]


def test_dwell_with_viewer_focused_stays_one_session():
    [session] = build_sessions(DWELL, focus=[focus_at(-5, True)])

    assert session.saves == 4
    assert session.longest_gap == timedelta(minutes=7)


def test_dwell_without_focus_data_splits():
    assert len(build_sessions(DWELL)) == 2


def test_gap_with_viewer_unfocused_splits():
    focus = [focus_at(-5, True), focus_at(30, False), focus_at(400, True)]

    assert len(build_sessions(DWELL, focus=focus)) == 2


def test_brief_focus_blip_within_grace_keeps_dwell():
    focus = [focus_at(-5, True), focus_at(100, False), focus_at(104, True)]

    assert len(build_sessions(DWELL, focus=focus)) == 1


def test_focused_gap_over_dwell_limit_splits():
    saves = [save_at(0), save_at(11 * 60)]

    sessions = build_sessions(
        saves, focus=[focus_at(-5, True)], dwell_limit=timedelta(minutes=10)
    )

    assert len(sessions) == 2


def test_focus_from_events_ignores_events_without_viewer_focused():
    events = [
        {
            "event": "focus_changed",
            "at": "2026-10-29T02:00:00+00:00",
            "frontmost": "calibre",
        },
        {
            "event": "focus_changed",
            "at": "2026-10-29T02:00:05+00:00",
            "frontmost": "E-book Viewer",
            "viewer_focused": True,
        },
    ]

    assert focus_from_events(events) == [focus_at(5, True)]


def test_recorded_long_dwell_is_one_session():
    [session] = sessions_from_log("long-dwell.jsonl")

    assert session.saves == 8
    assert timedelta(minutes=7) < session.longest_gap < timedelta(minutes=8)


def test_recorded_away_and_back_still_splits():
    assert [s.saves for s in sessions_from_log("away-and-back.jsonl")] == [4, 4]


def test_manual_end_splits_even_a_steady_session():
    saves = [save_at(0), save_at(15), save_at(30), save_at(45)]

    first, second = build_sessions(saves, ends=[T0 + timedelta(seconds=20)])

    assert (first.saves, second.saves) == (2, 2)
    assert second.start == T0 + timedelta(seconds=30)


def test_manual_end_after_last_save_changes_nothing():
    saves = [save_at(0), save_at(15)]

    assert len(build_sessions(saves, ends=[T0 + timedelta(seconds=60)])) == 1

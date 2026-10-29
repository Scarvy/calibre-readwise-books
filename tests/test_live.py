import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from calibre_bookwise.live import Tracker, ViewerState

T0 = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
KEY = "d1aafe7b9f"
OTHER = "05c6d07563"
BOOK = {"book_id": 11, "title": "Design for Developers", "path": "/library/dfd.epub"}


class Viewer:
    """Stands in for the e-book viewer writing annots/<key>.json."""

    def __init__(self, annots_dir):
        self.annots_dir = annots_dir
        self.writes = 0

    def save(
        self,
        at: datetime,
        pos: str = "epubcfi(/22/2/4/2)",
        highlights: int = 0,
        key: str = KEY,
    ):
        annots = [{"type": "highlight"}] * highlights + [
            {"type": "last-read", "pos": pos, "timestamp": at.isoformat()}
        ]
        path = self.annots_dir / f"{key}.json"
        path.write_text(json.dumps(annots))
        self.writes += 1
        os.utime(path, (1_000_000 + self.writes, 1_000_000 + self.writes))


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    annots = tmp_path / "annots"
    annots.mkdir()
    return tmp_path / "data", annots, Viewer(annots), Clock()


def tracker(env, viewer=None, **kwargs):
    data, annots, _, clock = env
    return Tracker(
        data,
        annots,
        book_lookup=lambda key: (
            BOOK if key == KEY else {"book_id": 4, "title": "Other"}
        ),
        progress_of=lambda path, cfi: 0.25 if cfi == "start" else 0.3,
        viewer_probe=viewer,
        clock=clock,
        **kwargs,
    )


def read(env, at_seconds: float, pos: str = "page"):
    """The viewer saves a page turn at T0 + at_seconds, then the plugin polls."""
    _, _, viewer, clock = env
    clock.now = T0 + timedelta(seconds=at_seconds + 2)
    viewer.save(T0 + timedelta(seconds=at_seconds), pos)


def test_positions_saved_before_tracking_are_not_reading(env):
    _, _, viewer, _ = env
    viewer.save(T0 - timedelta(days=1))

    t = tracker(env)

    assert t.poll() == []
    assert t.open_sessions() == []


def test_session_stays_open_while_it_can_grow_then_is_queued(env):
    t = tracker(env)
    read(env, 0, "start")
    t.poll()
    for second in (30, 60, 90):
        read(env, second)
        assert t.poll() == []
    [session] = t.open_sessions()
    assert session.saves == 4

    env[3].now = T0 + timedelta(seconds=90) + timedelta(minutes=10, seconds=1)
    [record] = t.poll()

    assert record["status"] == "pending"
    assert (record["start_progress"], record["end_progress"]) == (0.25, 0.3)
    assert record["duration_seconds"] == 90
    assert t.finished_sessions() == [record]
    assert t.open_sessions() == []


def test_manual_end_finishes_now_and_next_page_starts_a_new_session(env):
    t = tracker(env)
    for second in (0, 30, 70):
        read(env, second)
        t.poll()

    [record] = t.end_session()
    assert record["saves"] == 3

    read(env, 100)
    t.poll()
    [session] = t.open_sessions()
    assert session.start == T0 + timedelta(seconds=100)


def test_highlight_without_page_turn_is_not_a_save(env):
    t = tracker(env)
    read(env, 0)
    t.poll()
    env[2].save(T0, highlights=1)  # same last-read timestamp, file rewritten

    t.poll()

    assert t.open_sessions()[0].saves == 1


def test_restart_keeps_open_saves_and_does_not_requeue_finished(env):
    t = tracker(env)
    for second in (0, 30, 70):
        read(env, second)
        t.poll()
    t.end_session()
    read(env, 100)
    t.poll()
    read(env, 130)
    t.poll()

    restarted = tracker(env)  # Calibre quits and starts again

    assert len(restarted.finished_sessions()) == 1
    [session] = restarted.open_sessions()
    assert session.saves == 2
    env[3].now = T0 + timedelta(minutes=20)
    assert [r["saves"] for r in restarted.poll()] == [2]
    assert len(restarted.finished_sessions()) == 2


def test_dwell_with_viewer_focused_stays_one_session(env):
    t = tracker(env, viewer=lambda: ViewerState(True, frozenset({1}), frozenset({1})))
    for second in (0, 20, 20 + 7 * 60, 20 + 7 * 60 + 20):
        read(env, second)
        t.poll()

    [session] = t.open_sessions()
    assert session.saves == 4


def test_dwell_without_focus_splits(env):
    t = tracker(env, viewer=lambda: ViewerState(False, frozenset({1}), frozenset({1})))
    for second in (0, 20, 20 + 7 * 60, 20 + 7 * 60 + 20):
        read(env, second)
        t.poll()

    assert [r["saves"] for r in t.finished_sessions()] == [2]
    assert t.open_sessions()[0].saves == 2


def test_open_and_close_is_recorded_as_skipped(env):
    t = tracker(env)
    read(env, 0)
    t.poll()

    [record] = t.end_session()

    assert (record["status"], record["skip_reason"]) == (
        "skipped",
        "shorter than 0:01:00",
    )


def test_record_round_trips_to_session(env):
    from calibre_bookwise.live import session_from_record

    t = tracker(env)
    for second in (0, 30, 70):
        read(env, second)
        t.poll()
    [session] = t.open_sessions()
    [record] = t.end_session()

    assert session_from_record(record) == session


class FakeViewer:
    """Viewer windows that tests open, minimize and close."""

    def __init__(self):
        self.state = ViewerState(True, frozenset({1}), frozenset({1}))

    def __call__(self):
        return self.state

    def set(self, onscreen=(), existing=None):
        existing = onscreen if existing is None else existing
        self.state = ViewerState(
            bool(onscreen), frozenset(onscreen), frozenset(existing)
        )


def turn(env, t, at_seconds: float, key: str = KEY):
    _, _, viewer, clock = env
    clock.now = T0 + timedelta(seconds=at_seconds + 2)
    viewer.save(T0 + timedelta(seconds=at_seconds), pos=f"p{at_seconds}", key=key)
    return t.poll()


def test_closing_the_viewer_ends_the_session_straight_away(env):
    viewer = FakeViewer()
    t = tracker(env, viewer=viewer)
    for second in (0, 30, 70):
        turn(env, t, second)

    viewer.set(onscreen=())  # closing saves the position, then the window goes
    [record] = turn(env, t, 75)

    assert (record["saves"], record["duration_seconds"]) == (4, 75)
    assert t.open_sessions() == []


def test_minimizing_is_not_closing(env):
    viewer = FakeViewer()
    t = tracker(env, viewer=viewer)
    for second in (0, 30):
        turn(env, t, second)

    viewer.set(onscreen=(), existing={1})  # minimized: off screen, still exists
    env[3].now = T0 + timedelta(seconds=40)

    assert t.poll() == []
    assert len(t.open_sessions()) == 1


def test_closing_one_of_two_books_ends_only_that_book(env):
    viewer = FakeViewer()
    viewer.set(onscreen={1, 2})
    t = tracker(env, viewer=viewer)
    turn(env, t, 0, KEY)
    turn(env, t, 10, OTHER)
    turn(env, t, 30, KEY)
    turn(env, t, 60, KEY)

    viewer.set(onscreen={1})  # the other book's window closes...
    [record] = turn(env, t, 62, OTHER)  # ...right after its close save

    assert record["book_key"] == OTHER
    assert [s.book_key for s in t.open_sessions()] == [KEY]


def test_last_window_closing_ends_everything_without_a_recent_save(env):
    viewer = FakeViewer()
    t = tracker(env, viewer=viewer)
    for second in (0, 30):
        turn(env, t, second)

    viewer.set(onscreen=())
    env[3].now = T0 + timedelta(seconds=90)  # no save in the last 8 seconds

    assert [r["saves"] for r in t.poll()] == [2]

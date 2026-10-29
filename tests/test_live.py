import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from calibre_bookwise.live import Tracker

T0 = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
KEY = "d1aafe7b9f"
BOOK = {"book_id": 11, "title": "Design for Developers", "path": "/library/dfd.epub"}


class Viewer:
    """Stands in for the e-book viewer writing annots/<key>.json."""

    def __init__(self, annots_dir):
        self.annots_dir = annots_dir
        self.writes = 0

    def save(self, at: datetime, pos: str = "epubcfi(/22/2/4/2)", highlights: int = 0):
        annots = [{"type": "highlight"}] * highlights + [
            {"type": "last-read", "pos": pos, "timestamp": at.isoformat()}
        ]
        path = self.annots_dir / f"{KEY}.json"
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


def tracker(env, focus=None, **kwargs):
    data, annots, _, clock = env
    return Tracker(
        data,
        annots,
        book_lookup=lambda key: BOOK if key == KEY else None,
        progress_of=lambda path, cfi: 0.25 if cfi == "start" else 0.3,
        focus_probe=focus,
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
    t = tracker(env, focus=lambda: True)
    for second in (0, 20, 20 + 7 * 60, 20 + 7 * 60 + 20):
        read(env, second)
        t.poll()

    [session] = t.open_sessions()
    assert session.saves == 4


def test_dwell_without_focus_splits(env):
    t = tracker(env, focus=lambda: False)
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

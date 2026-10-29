"""Turn the viewer's saved reading positions into reading sessions.

Observed on Calibre 8.14: the viewer saves the reading position on every page
turn and once when the book closes, and saves nothing while you sit idle. So a
session is a run of saves for one book with no gap longer than the idle limit.

A long gap alone can't tell a slow page from stepping away, but focus can: a
gap up to the dwell limit stays in the session if the viewer kept focus for
the whole gap (allowing brief blips).
Stdlib only: this has to run inside Calibre's bundled Python 3.11.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from operator import attrgetter

DEFAULT_IDLE_LIMIT = timedelta(minutes=5)
DEFAULT_DWELL_LIMIT = timedelta(minutes=10)
# Unfocused time a dwell may contain, e.g. a glance at a notification.
FOCUS_GRACE = timedelta(seconds=30)
# The logger sees a save 1-4s after the viewer timestamps it; much older means
# the position was carried over from earlier reading.
STALE_LIMIT = timedelta(minutes=2)


@dataclass(frozen=True)
class Save:
    """One saved reading position, timestamped by the viewer."""

    book_key: str
    at: datetime
    progress: float | None = None
    book_id: int | None = None
    title: str | None = None


@dataclass(frozen=True)
class FocusChange:
    """The moment the viewer gained or lost focus."""

    at: datetime
    viewer_focused: bool


@dataclass(frozen=True)
class Session:
    book_key: str
    book_id: int | None
    title: str | None
    start: datetime
    end: datetime
    start_progress: float | None
    end_progress: float | None
    saves: int
    longest_gap: timedelta

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def build_sessions(
    saves: Iterable[Save],
    idle_limit: timedelta = DEFAULT_IDLE_LIMIT,
    focus: Sequence[FocusChange] = (),
    dwell_limit: timedelta = DEFAULT_DWELL_LIMIT,
) -> list[Session]:
    """Group saves per book, splitting at gaps over idle_limit unless dwelling."""
    focus = sorted(focus, key=attrgetter("at"))
    by_book: dict[str, list[Save]] = {}
    for save in saves:
        by_book.setdefault(save.book_key, []).append(save)

    sessions = []
    for book_saves in by_book.values():
        book_saves.sort(key=attrgetter("at"))
        run = [book_saves[0]]
        for previous, save in pairwise(book_saves):
            gap = save.at - previous.at
            dwelling = gap <= dwell_limit and _kept_focus(focus, previous.at, save.at)
            if gap > idle_limit and not dwelling:
                sessions.append(_session_from(run))
                run = []
            run.append(save)
        sessions.append(_session_from(run))
    return sorted(sessions, key=attrgetter("start"))


def _kept_focus(focus: Sequence[FocusChange], start: datetime, end: datetime) -> bool:
    """Whether the viewer had focus from start to end, give or take FOCUS_GRACE.

    False when focus is unknown at start (no focus data recorded before it).
    """
    known = [change for change in focus if change.at <= start]
    if not known:
        return False
    focused, since = known[-1].viewer_focused, start
    unfocused = timedelta(0)
    for change in focus:
        if not start < change.at < end:
            continue
        if not focused:
            unfocused += change.at - since
        focused, since = change.viewer_focused, change.at
    if not focused:
        unfocused += end - since
    return unfocused <= FOCUS_GRACE


def _session_from(run: list[Save]) -> Session:
    progress = [save.progress for save in run if save.progress is not None]
    gaps = [b.at - a.at for a, b in pairwise(run)]
    first, last = run[0], run[-1]
    return Session(
        book_key=first.book_key,
        book_id=first.book_id,
        title=first.title,
        start=first.at,
        end=last.at,
        start_progress=progress[0] if progress else None,
        end_progress=progress[-1] if progress else None,
        saves=len(run),
        longest_gap=max(gaps, default=timedelta(0)),
    )


def saves_from_events(
    events: Iterable[dict],
    stale_limit: timedelta = STALE_LIMIT,
    progress_of: Callable[[str | None, str | None], float | None] | None = None,
) -> list[Save]:
    """Extract saves from logger events, one per distinct viewer timestamp.

    Skips stale saves: on first open the viewer can import an old position
    (e.g. from the EPUB's META-INF/calibre_bookmarks.txt) carrying its original
    timestamp, which is not reading done now.

    progress_of(book_path, cfi) computes precise progress (see progress.py);
    without it, or when it returns None, the logger's chapter-level
    approx_progress is used.
    """
    saves = []
    seen = set()
    for event in events:
        timestamp = event.get("viewer_timestamp")
        if event.get("event") != "position_saved" or not timestamp:
            continue
        identity = (event["key"], timestamp)
        if identity in seen:
            continue
        seen.add(identity)
        saved_at = datetime.fromisoformat(timestamp)
        if (
            "at" in event
            and datetime.fromisoformat(event["at"]) - saved_at > stale_limit
        ):
            continue
        progress = None
        if progress_of:
            progress = progress_of(event.get("path"), event.get("pos"))
        if progress is None:
            progress = event.get("approx_progress")
        saves.append(
            Save(
                book_key=event["key"],
                at=saved_at,
                progress=progress,
                book_id=event.get("book_id"),
                title=event.get("title"),
            )
        )
    return saves


def focus_from_events(events: Iterable[dict]) -> list[FocusChange]:
    """Extract viewer focus changes from logger events.

    Logs from before the logger recorded viewer_focused yield nothing, which
    leaves build_sessions with the plain idle rule.
    """
    return [
        FocusChange(
            at=datetime.fromisoformat(event["at"]),
            viewer_focused=event["viewer_focused"],
        )
        for event in events
        if event.get("event") == "focus_changed" and "viewer_focused" in event
    ]

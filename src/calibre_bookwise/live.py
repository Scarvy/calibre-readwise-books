"""Live session tracking: what the Calibre plugin runs every few seconds.

Each poll:
1. notices viewer position saves (annots/<sha256(book path)>.json changing),
2. while a session is open, checks the viewer (macOS): whether it has focus,
   and whether a viewer window closed, which ends that book's session,
3. finishes sessions that can no longer continue: idle past the dwell limit,
   viewer window closed, or ended manually, into the sessions queue.

Everything is kept in the data directory, so nothing is lost if Calibre quits:
  events.jsonl    saves, focus changes and manual ends (the logger's format)
  sessions.jsonl  finished sessions, oldest first: the upload queue
  state.json      per book, where its last finished session ended
Stdlib only: this has to run inside Calibre's bundled Python 3.11.
"""

import json
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .bookwise import skip_reason
from .sessions import (
    DEFAULT_DWELL_LIMIT,
    DEFAULT_IDLE_LIMIT,
    Session,
    build_sessions,
    focus_from_events,
    saves_from_events,
)

# Viewer opened from the Calibre app, and from the command line (macOS).
VIEWER_EXECUTABLE_SUFFIXES = (
    "/ebook-viewer.app/Contents/MacOS/calibre-parallel",
    "/MacOS/ebook-viewer",
)
# Closing a book saves its position just before the window goes; a save this
# recent when a window disappears identifies the book that was closed.
CLOSE_SAVE_WINDOW = timedelta(seconds=8)


@dataclass(frozen=True)
class ViewerState:
    """What the viewer looks like right now (see macos_viewer_state)."""

    focused: bool  # a viewer window is the frontmost app
    onscreen: frozenset[int] = field(default_factory=frozenset)  # window ids
    # Every viewer window id, including minimized and other-Space windows.
    existing: frozenset[int] = field(default_factory=frozenset)


BookLookup = Callable[[str], dict | None]  # annots key -> {book_id, title, path}
ProgressOf = Callable[[str | None, str | None], float | None]  # (path, cfi) -> 0-1
ViewerProbe = Callable[[], ViewerState | None]  # None = unknown (e.g. not macOS)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Tracker:
    def __init__(
        self,
        data_dir: Path,
        annots_dir: Path,
        book_lookup: BookLookup,
        progress_of: ProgressOf | None = None,
        viewer_probe: ViewerProbe | None = None,
        clock: Callable[[], datetime] = utc_now,
        idle_limit: timedelta = DEFAULT_IDLE_LIMIT,
        dwell_limit: timedelta = DEFAULT_DWELL_LIMIT,
    ):
        self.data_dir = data_dir
        self.annots_dir = annots_dir
        self.book_lookup = book_lookup
        self.progress_of = _remembered(progress_of) if progress_of else None
        self.viewer_probe = viewer_probe
        self.clock = clock
        self.idle_limit = idle_limit
        self.dwell_limit = dwell_limit
        # A session can still grow until its last save is this old.
        self.finish_after = max(idle_limit, dwell_limit)

        data_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = data_dir / "events.jsonl"
        self.sessions_path = data_dir / "sessions.jsonl"
        self.state_path = data_dir / "state.json"
        self.events = _read_jsonl(self.events_path)
        self.finished_until: dict[str, str] = self._read_state().get(
            "finished_until", {}
        )
        # poll() runs on a background thread in the plugin; the UI reads too.
        self.lock = threading.RLock()
        self.focused: bool | None = None
        # Ids of viewer windows seen on screen while a session was open.
        self.windows: set[int] = set()
        # Baseline: positions already saved before tracking started aren't reading.
        self.mtimes = {f: f.stat().st_mtime for f in self._annots_files()}
        self.positions = {f.stem: _last_read(f).get("timestamp") for f in self.mtimes}

    def poll(self) -> list[dict]:
        """Run one tracking step; returns sessions finished by this step."""
        with self.lock:
            return self._poll()

    def _poll(self) -> list[dict]:
        now = self.clock()
        for annots_file in self._annots_files():
            mtime = annots_file.stat().st_mtime
            if self.mtimes.get(annots_file) == mtime:
                continue
            self.mtimes[annots_file] = mtime
            self._record_save(annots_file, now)
        open_sessions = self.open_sessions()
        if not open_sessions:
            self.focused = None  # unknown until the next session starts
            self.windows = set()
        elif self.viewer_probe is not None and (state := self.viewer_probe()):
            # A new session's focus is dated to its start, so a dwell straight
            # after opening the book is recognised.
            start = min(session.start for session in open_sessions)
            self._record_focus(
                now if self.focused is not None else min(now, start), state.focused
            )
            self._check_windows(now, state, open_sessions)
        return self._finish(now)

    def end_session(self) -> list[dict]:
        """Manually end every open session now; returns the finished sessions."""
        with self.lock:
            now = self.clock()
            self._append_event({"at": now.isoformat(), "event": "session_ended"})
            return self._finish(now)

    def open_sessions(self) -> list[Session]:
        """Sessions that are still in progress (not yet finished)."""
        with self.lock:
            return self._sessions()

    def finished_sessions(self) -> list[dict]:
        with self.lock:
            return _read_jsonl(self.sessions_path)

    def _sessions(self) -> list[Session]:
        by_book: dict[str, list] = {}
        for save in saves_from_events(self.events, progress_of=self.progress_of):
            done = self.finished_until.get(save.book_key)
            if done is None or save.at > datetime.fromisoformat(done):
                by_book.setdefault(save.book_key, []).append(save)
        focus = focus_from_events(self.events)
        sessions = []
        for key, saves in by_book.items():
            sessions += build_sessions(
                saves,
                self.idle_limit,
                focus=focus,
                dwell_limit=self.dwell_limit,
                ends=self._ends_for(key),
            )
        return sorted(sessions, key=lambda session: session.start)

    def _ends_for(self, key: str) -> list[datetime]:
        """Times sessions of this book were ended: manually, or by closing it."""
        return [
            datetime.fromisoformat(event["at"])
            for event in self.events
            if event.get("event") == "session_ended"
            and ("keys" not in event or key in event["keys"])
        ]

    def _check_windows(
        self, now: datetime, state: ViewerState, open_sessions: list[Session]
    ):
        """End the session of a book whose viewer window has closed."""
        closed = self.windows - state.existing
        self.windows = (self.windows & state.existing) | state.onscreen
        if not closed:
            return
        # Each closed window is the book with the most recent (close) save.
        recent = sorted(
            (s for s in open_sessions if now - s.end <= CLOSE_SAVE_WINDOW),
            key=lambda s: s.end,
            reverse=True,
        )
        keys = {s.book_key for s in recent[: len(closed)]}
        if not keys and not self.windows:
            keys = {s.book_key for s in open_sessions}  # last window gone
        if keys:
            self._append_event(
                {
                    "at": now.isoformat(),
                    "event": "session_ended",
                    "reason": "viewer_closed",
                    "keys": sorted(keys),
                }
            )

    def _finish(self, now: datetime) -> list[dict]:
        finished = []
        sessions = self._sessions()
        for session in sessions:
            ended_manually = any(
                end >= session.end for end in self._ends_for(session.book_key)
            )
            superseded = any(
                other.book_key == session.book_key and other.start > session.end
                for other in sessions
            )
            idle_too_long = now - session.end > self.finish_after
            if not (ended_manually or superseded or idle_too_long):
                continue
            record = _session_record(session, now)
            with self.sessions_path.open("a") as queue:
                queue.write(json.dumps(record) + "\n")
            self.finished_until[session.book_key] = session.end.isoformat()
            finished.append(record)
        if finished:
            self._write_state()
        return finished

    def _record_save(self, annots_file: Path, now: datetime):
        key = annots_file.stem
        position = _last_read(annots_file)
        timestamp = position.get("timestamp")
        if not timestamp or timestamp == self.positions.get(key):
            return  # a highlight change, not a new position
        self.positions[key] = timestamp
        book = self.book_lookup(key) or {}
        self._append_event(
            {
                "at": now.isoformat(),
                "event": "position_saved",
                "key": key,
                "book_id": book.get("book_id"),
                "title": book.get("title"),
                "pos": position.get("pos"),
                "viewer_timestamp": timestamp,
                "path": book.get("path"),
            }
        )

    def _record_focus(self, at: datetime, focused: bool):
        if focused == self.focused:
            return
        self.focused = focused
        self._append_event(
            {"at": at.isoformat(), "event": "focus_changed", "viewer_focused": focused}
        )

    def _append_event(self, event: dict):
        self.events.append(event)
        with self.events_path.open("a") as log:
            log.write(json.dumps(event) + "\n")

    def _annots_files(self) -> list[Path]:
        return sorted(self.annots_dir.glob("*.json"))

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _write_state(self):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"finished_until": self.finished_until}))
        temporary.replace(self.state_path)


def _remembered(progress_of: ProgressOf) -> ProgressOf:
    """Each position's progress only needs working out once."""
    known: dict[tuple[str | None, str | None], float | None] = {}

    def progress(path: str | None, cfi: str | None) -> float | None:
        if (path, cfi) not in known:
            known[path, cfi] = progress_of(path, cfi)
        return known[path, cfi]

    return progress


def session_from_record(record: dict) -> Session:
    """Rebuild a Session from a sessions.jsonl record."""
    return Session(
        book_key=record["book_key"],
        book_id=record.get("book_id"),
        title=record.get("title"),
        start=datetime.fromisoformat(record["start"]),
        end=datetime.fromisoformat(record["end"]),
        start_progress=record.get("start_progress"),
        end_progress=record.get("end_progress"),
        saves=record["saves"],
        longest_gap=timedelta(seconds=record["longest_gap"]),
    )


def _session_record(session: Session, now: datetime) -> dict:
    record = asdict(session)
    record.update(
        start=session.start.isoformat(),
        end=session.end.isoformat(),
        longest_gap=session.longest_gap.total_seconds(),
        duration_seconds=session.duration.total_seconds(),
        finished_at=now.isoformat(),
    )
    reason = skip_reason(session)
    record["status"] = "skipped" if reason else "pending"
    record["skip_reason"] = reason
    return record


def _last_read(annots_file: Path) -> dict:
    try:
        annots = json.loads(annots_file.read_text())
    except (OSError, ValueError):
        return {}
    entries = [
        a for a in annots if isinstance(a, dict) and a.get("type") == "last-read"
    ]
    return max(entries, key=lambda a: a.get("timestamp", ""), default={})


def _read_jsonl(path: Path) -> list[dict]:
    try:
        with path.open() as lines:
            return [json.loads(line) for line in lines if line.strip()]
    except OSError:
        return []


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=2
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


VIEWER_STATE_JXA = """
ObjC.import("AppKit");
ObjC.import("CoreGraphics");
function run(argv) {
  const pids = argv.map(Number);
  const front = $.NSWorkspace.sharedWorkspace.frontmostApplication;
  const windows = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(
    $.kCGWindowListOptionAll | $.kCGWindowListExcludeDesktopElements, 0)));
  const mine = windows.filter(w => w.kCGWindowLayer === 0 && pids.includes(w.kCGWindowOwnerPID));
  return JSON.stringify({
    front: front.isNil() ? null : front.processIdentifier,
    onscreen: mine.filter(w => w.kCGWindowIsOnscreen).map(w => w.kCGWindowNumber),
    existing: mine.map(w => w.kCGWindowNumber),
  });
}
"""


def macos_viewer_state() -> ViewerState | None:
    """Viewer focus and window ids; None off macOS or if the probe fails.

    Window ids rather than counts: a minimized window (or one on another
    Space) is off screen but keeps its id; only a closed window loses it.
    """
    if sys.platform != "darwin":
        return None
    pids = [
        pid
        for pid, _, command in (
            line.strip().partition(" ")
            for line in _run(["ps", "-axo", "pid=,comm="]).splitlines()
        )
        if command.strip().endswith(VIEWER_EXECUTABLE_SUFFIXES)
    ]
    try:
        found = json.loads(
            _run(["osascript", "-l", "JavaScript", "-e", VIEWER_STATE_JXA, *pids])
        )
    except ValueError:
        return None
    return ViewerState(
        focused=str(found["front"]) in pids,
        onscreen=frozenset(found["onscreen"]),
        existing=frozenset(found["existing"]),
    )

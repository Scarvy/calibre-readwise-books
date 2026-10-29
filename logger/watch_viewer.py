# /// script
# requires-python = ">=3.11"
# ///
"""Read-only logger for Calibre e-book viewer activity (macOS).

Records what the viewer does while you read, so session rules can be tested
against real data. Logs to JSONL; never writes to Calibre's files.

Events:
  position_saved       a book's last-read position changed (page turn, open, close)
  annotations_changed  a book's annots file changed but the position didn't
  window_opened / window_closed   a viewer window appeared or disappeared
  focus_changed        the frontmost app changed (viewer_focused says if it's
                       the viewer rather than the main Calibre window)

Run:  uv run logger/watch_viewer.py --label "what you're testing"
Stop with Ctrl-C (or SIGTERM); a summary is printed.
"""

import argparse
import hashlib
import itertools
import json
import re
import signal
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

CALIBRE_PREFS = Path.home() / "Library/Preferences/calibre"
ANNOTS_DIR = CALIBRE_PREFS / "viewer/annots"
# Viewer opened from the command line, and viewer opened from the Calibre app.
VIEWER_EXECUTABLES = (
    "/Applications/calibre.app/Contents/MacOS/ebook-viewer",
    "/Applications/calibre.app/Contents/ebook-viewer.app/Contents/MacOS/calibre-parallel",
)
BOOK_EXTENSIONS = {".epub", ".azw3", ".mobi", ".pdf", ".kepub", ".fb2", ".docx", ".txt"}
CFI_SPINE_STEP = re.compile(r"epubcfi\(/(\d+)")
BOOK_DIR_ID = re.compile(r"^(.*) \((\d+)\)$")
# Counts normal (layer 0) on-screen windows per PID. Window titles would need
# Screen Recording permission; counts don't.
WINDOW_COUNT_JXA = """
ObjC.import("CoreGraphics");
function run(argv) {
  const pids = argv.map(Number);
  const windows = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(
    $.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements, 0)));
  const counts = {};
  for (const w of windows) {
    if (w.kCGWindowLayer === 0 && pids.includes(w.kCGWindowOwnerPID)) {
      counts[w.kCGWindowOwnerPID] = (counts[w.kCGWindowOwnerPID] || 0) + 1;
    }
  }
  return JSON.stringify(counts);
}
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def run_quiet(args: list[str]) -> str:
    return subprocess.run(args, check=False, capture_output=True, text=True).stdout


def library_path() -> Path:
    prefs = json.loads((CALIBRE_PREFS / "global.py.json").read_text())
    return Path(prefs["library_path"])


def index_library(library: Path) -> dict[str, dict]:
    """Map the viewer's annotation key (sha256 of the book file path) to the book."""
    books = {}
    for path in library.glob("*/*/*"):
        if path.suffix.lower() not in BOOK_EXTENSIONS:
            continue
        match = BOOK_DIR_ID.match(path.parent.name)
        key = hashlib.sha256(str(path).encode()).hexdigest()
        books[key] = {
            "book_id": int(match.group(2)) if match else None,
            "title": match.group(1) if match else path.parent.name,
            "author": path.parent.parent.name,
            "path": str(path),
        }
    return books


def spine_length(epub_path: str) -> int | None:
    """Number of spine items in an EPUB, for turning a CFI into a rough fraction."""
    try:
        with zipfile.ZipFile(epub_path) as epub:
            container = ElementTree.fromstring(epub.read("META-INF/container.xml"))
            rootfile_el = container.find(".//{*}rootfile")
            rootfile = rootfile_el.get("full-path") if rootfile_el is not None else None
            if rootfile is None:
                return None
            opf = ElementTree.fromstring(epub.read(rootfile))
            return len(opf.findall(".//{*}spine/{*}itemref"))
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return None


def approx_progress(pos: str | None, spine_count: int | None) -> float | None:
    """CFI '/N' first step is (spine_index + 1) * 2; return spine_index / spine_count."""
    match = CFI_SPINE_STEP.match(pos or "")
    if not match or not spine_count:
        return None
    return round((int(match.group(1)) // 2 - 1) / spine_count, 3)


def read_annots(annots_file: Path) -> list[dict]:
    try:
        annots = json.loads(annots_file.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return annots if isinstance(annots, list) else []


def last_read(annots: list[dict]) -> dict:
    entries = [a for a in annots if a.get("type") == "last-read"]
    return max(entries, key=lambda a: a.get("timestamp", ""), default={})


def viewer_pids() -> set[int]:
    pids = set()
    for line in run_quiet(["ps", "-axo", "pid=,comm="]).splitlines():
        pid, _, command = line.strip().partition(" ")
        if command.strip() in VIEWER_EXECUTABLES:
            pids.add(int(pid))
    return pids


def viewer_window_counts(pids: set[int]) -> dict[int, int]:
    if not pids:
        return {}
    output = run_quiet(
        ["osascript", "-l", "JavaScript", "-e", WINDOW_COUNT_JXA, *map(str, pids)]
    )
    try:
        return {int(pid): count for pid, count in json.loads(output).items()}
    except (json.JSONDecodeError, ValueError):
        return {}


def frontmost_app() -> tuple[str | None, int | None]:
    front = run_quiet(["lsappinfo", "front"]).strip()
    if not front:
        return None, None
    info = run_quiet(["lsappinfo", "info", "-only", "name", "-only", "pid", front])
    name = re.search(r'"LSDisplayName"="(.*?)"', info)
    pid = re.search(r'"pid"=(\d+)', info)
    return (name.group(1) if name else None, int(pid.group(1)) if pid else None)


class Logger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.events: list[dict] = []

    def emit(self, event: str, **fields):
        record = {"at": now(), "event": event, **fields}
        self.events.append(record)
        with self.log_path.open("a") as log:
            log.write(json.dumps(record) + "\n")
        detail = ", ".join(
            f"{k}={v}" for k, v in fields.items() if k not in ("path", "key")
        )
        print(f"{record['at'][11:23]}  {event:<19} {detail}", flush=True)


def summarize(events: list[dict]):
    saves = [
        e
        for e in events
        if e["event"] == "position_saved" and e.get("viewer_timestamp")
    ]
    print(f"\n{len(saves)} position saves")
    by_book: dict[str, list[datetime]] = {}
    for save in saves:
        by_book.setdefault(save.get("title") or save["key"][:10], []).append(
            datetime.fromisoformat(save["viewer_timestamp"])
        )
    for title, times in by_book.items():
        gaps = [round((b - a).total_seconds()) for a, b in itertools.pairwise(times)]
        print(f"  {title}: {len(times)} saves, gaps (s): {gaps or '-'}")
    print("\nSessions: uv run calibre-bookwise-report logger/logs/*.jsonl")


def stop_on_signal(signum, frame):
    raise KeyboardInterrupt


def watch(interval: float, log_path: Path, label: str | None):
    logger = Logger(log_path)
    books = index_library(library_path())
    spine_cache: dict[str, int | None] = {}
    mtimes = {f: f.stat().st_mtime for f in ANNOTS_DIR.glob("*.json")}
    positions = {f.stem: last_read(read_annots(f)).get("timestamp") for f in mtimes}
    windows = viewer_window_counts(viewer_pids())
    front = frontmost_app()

    logger.emit(
        "started",
        label=label,
        library_books=len(books),
        annots_files=len(mtimes),
        viewer_windows=sum(windows.values()),
        frontmost=front[0],
    )
    try:
        while True:
            time.sleep(interval)

            pids = viewer_pids()
            current_windows = viewer_window_counts(pids)
            for pid in set(windows) | set(current_windows):
                before, after = windows.get(pid, 0), current_windows.get(pid, 0)
                if after != before:
                    logger.emit(
                        "window_opened" if after > before else "window_closed",
                        pid=pid,
                        viewer_windows=sum(current_windows.values()),
                    )
            windows = current_windows

            current_front = frontmost_app()
            if current_front != front:
                logger.emit(
                    "focus_changed",
                    frontmost=current_front[0],
                    viewer_focused=current_front[1] in pids,
                    previous=front[0],
                )
                front = current_front

            for annots_file in ANNOTS_DIR.glob("*.json"):
                mtime = annots_file.stat().st_mtime
                if mtimes.get(annots_file) == mtime:
                    continue
                mtimes[annots_file] = mtime
                key = annots_file.stem
                if key not in books:
                    books = index_library(library_path())
                book = books.get(key, {})
                annots = read_annots(annots_file)
                position = last_read(annots)
                timestamp = position.get("timestamp")
                if timestamp == positions.get(key):
                    logger.emit(
                        "annotations_changed",
                        key=key,
                        title=book.get("title"),
                        annotations=len(annots),
                    )
                    continue
                positions[key] = timestamp
                if book.get("path") and book["path"] not in spine_cache:
                    spine_cache[book["path"]] = spine_length(book["path"])
                logger.emit(
                    "position_saved",
                    key=key,
                    book_id=book.get("book_id"),
                    title=book.get("title"),
                    pos=position.get("pos"),
                    viewer_timestamp=timestamp,
                    approx_progress=approx_progress(
                        position.get("pos"), spine_cache.get(book.get("path"))
                    ),
                    path=book.get("path"),
                )
    except KeyboardInterrupt:
        logger.emit("stopped")
        summarize(logger.events)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", help="what this run tests, e.g. 'long dwell'")
    parser.add_argument(
        "--interval", type=float, default=1.0, help="poll seconds (default 1)"
    )
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).parent / "logs")
    args = parser.parse_args()

    if not ANNOTS_DIR.is_dir():
        sys.exit(f"Viewer annotations dir not found: {ANNOTS_DIR}")
    # Background jobs start with SIGINT ignored; restore it and treat SIGTERM alike.
    signal.signal(signal.SIGINT, stop_on_signal)
    signal.signal(signal.SIGTERM, stop_on_signal)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", (args.label or "").lower()).strip("-")
    log_name = f"viewer-events-{datetime.now():%Y%m%d-%H%M%S}"
    log_path = args.log_dir / f"{log_name}{'-' + slug if slug else ''}.jsonl"
    print(f"Logging to {log_path}\n")
    watch(args.interval, log_path, args.label)


if __name__ == "__main__":
    main()

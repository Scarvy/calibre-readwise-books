"""Print the reading sessions found in viewer logs.

Usage:  uv run calibre-bookwise-report logger/logs/*.jsonl
            [--idle-minutes 5] [--dwell-minutes 10]
"""

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from .bookwise import (
    BATCH_SIZE,
    local_timezone,
    session_item,
    skip_reason,
)
from .progress import book_progress
from .sessions import (
    Session,
    build_sessions,
    focus_from_events,
    saves_from_events,
)


def load_events(log_path: Path) -> list[dict]:
    with log_path.open() as log:
        return [json.loads(line) for line in log if line.strip()]


def format_duration(span: timedelta) -> str:
    minutes, seconds = divmod(round(span.total_seconds()), 60)
    return f"{minutes}:{seconds:02d}"


def format_progress(session: Session) -> str:
    if session.start_progress is None or session.end_progress is None:
        return "?"
    return f"{session.start_progress:.1%} → {session.end_progress:.1%}"


def print_report(log_path: Path, idle_limit: timedelta, dwell_limit: timedelta):
    events = load_events(log_path)
    started = next((e for e in events if e.get("event") == "started"), {})
    label = started.get("label")
    print(f"\n{log_path.name}" + (f"  [{label}]" if label else ""))

    sessions = build_sessions(
        saves_from_events(events, progress_of=book_progress),
        idle_limit,
        focus=focus_from_events(events),
        dwell_limit=dwell_limit,
    )
    if not sessions:
        print("  no position saves")
        return
    print(
        f"  {'start':<8} {'length':>7} {'saves':>5} {'max gap':>8}  progress     book"
    )
    for session in sessions:
        print(
            f"  {session.start.astimezone():%H:%M:%S}"
            f" {format_duration(session.duration):>7}"
            f" {session.saves:>5}"
            f" {format_duration(session.longest_gap):>8}"
            f"  {format_progress(session):<11}"
            f"  {session.title or session.book_key[:10]}"
        )


def print_payload(log_paths: list[Path], book_map: dict[int, int], **limits):
    """Print the Books API request bodies these logs would produce (not sent)."""
    timezone = local_timezone()
    items = []
    for log_path in sorted(log_paths):
        events = load_events(log_path)
        sessions = build_sessions(
            saves_from_events(events, progress_of=book_progress),
            focus=focus_from_events(events),
            **limits,
        )
        for session in sessions:
            name = session.title or session.book_key[:10]
            if reason := skip_reason(session):
                print(
                    f"skip {name} at {session.start:%H:%M}: {reason}", file=sys.stderr
                )
            elif session.book_id not in book_map:
                print(
                    f"skip {name}: no Bookwise id for Calibre book {session.book_id}"
                    " (pass --book CALIBRE_ID=BOOKWISE_ID)",
                    file=sys.stderr,
                )
            else:
                items.append(session_item(session, book_map[session.book_id], timezone))
    for start in range(0, len(items), BATCH_SIZE):
        print(json.dumps({"items": items[start : start + BATCH_SIZE]}, indent=2))


def parse_book_map(pairs: list[str]) -> dict[int, int]:
    book_map = {}
    for pair in pairs:
        calibre_id, _, bookwise_id = pair.partition("=")
        book_map[int(calibre_id)] = int(bookwise_id)
    return book_map


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "logs", nargs="+", type=Path, help="viewer-events-*.jsonl files"
    )
    parser.add_argument(
        "--idle-minutes",
        type=float,
        default=5,
        help="split sessions at gaps longer than this (default 5)",
    )
    parser.add_argument(
        "--dwell-minutes",
        type=float,
        default=10,
        help="keep gaps up to this long if the viewer kept focus (default 10, 0 = off)",
    )
    parser.add_argument(
        "--payload",
        action="store_true",
        help="print Books API session request bodies instead of the table",
    )
    parser.add_argument(
        "--book",
        action="append",
        default=[],
        metavar="CALIBRE_ID=BOOKWISE_ID",
        help="Bookwise book id for a Calibre book (repeatable; for --payload)",
    )
    args = parser.parse_args(argv)
    if args.payload:
        print_payload(
            args.logs,
            parse_book_map(args.book),
            idle_limit=timedelta(minutes=args.idle_minutes),
            dwell_limit=timedelta(minutes=args.dwell_minutes),
        )
        return
    for log_path in sorted(args.logs):
        print_report(
            log_path,
            timedelta(minutes=args.idle_minutes),
            timedelta(minutes=args.dwell_minutes),
        )

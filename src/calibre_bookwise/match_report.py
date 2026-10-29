"""Dry run: show how Calibre books would match the Readwise Books library.

Nothing is sent. The Bookwise side comes from a snapshot of books_list_books
results saved as JSON (a list of books).

Usage:  uv run calibre-bookwise-match --snapshot snapshots/bookwise-library.json
            [--library ~/Calibre\\ Library] [CALIBRE_ID ...]
"""

import argparse
import json
from pathlib import Path

from .matching import create_requests, plan_matches, read_calibre_books

DEFAULT_LIBRARY = Path.home() / "Calibre Library"


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "ids", nargs="*", type=int, help="Calibre book ids (default all)"
    )
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument(
        "--snapshot", type=Path, required=True, help="books_list_books results as JSON"
    )
    args = parser.parse_args(argv)

    books = read_calibre_books(args.library, args.ids or None)
    matches = plan_matches(books, json.loads(args.snapshot.read_text()))

    print(f"{'id':>3}  {'calibre title':<40} {'action':<13} {'by':<12} bookwise")
    for match in matches:
        bookwise = match.bookwise
        if match.bookwise_id is not None:
            detail = f"#{match.bookwise_id} {bookwise.get('title', '?')!r}"
            if bookwise.get("status"):
                detail += f" ({bookwise['status']})"
        elif match.create_item:
            detail = "product " + json.dumps(match.create_item["product"])
        else:
            detail = match.problem or ""
        print(
            f"{match.book.calibre_id:>3}  {match.book.title[:40]:<40}"
            f" {match.action:<13} {match.matched_by or '-':<12} {detail}"
        )

    requests = create_requests(matches)
    if requests:
        print("\nWould send to books_create_books (dry run, not sent):")
        for body in requests:
            print(json.dumps(body, indent=2))

"""Dry run of Sync Readwise metadata against the real Readwise API.

Reads READWISE_TOKEN from the environment or ./.env, fetches books and
sessions with the GET-only client, matches the Calibre library (opened
read-only) and prints the value each Readwise column would get.
Writes nothing to Readwise or to Calibre.

Usage:  uv run python scripts/sync_dry_run.py [--library ~/Calibre\\ Library]
"""

import argparse
import os
from pathlib import Path

from calibre_bookwise.matching import plan_matches, read_calibre_books
from calibre_bookwise.metadata import COLUMNS, column_updates
from calibre_bookwise.readwise_api import ReadOnlyClient

ROOT = Path(__file__).resolve().parent.parent


def token_from_env() -> str:
    token = os.environ.get("READWISE_TOKEN")
    if not token and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "READWISE_TOKEN":
                token = value.strip().strip("'\"")
    if not token:
        raise SystemExit("Set READWISE_TOKEN in the environment or .env")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--library", type=Path, default=Path.home() / "Calibre Library")
    args = parser.parse_args()

    client = ReadOnlyClient(token_from_env())
    readwise_books = client.list_books()
    sessions = client.list_sessions()
    print(
        f"Readwise: {len(readwise_books)} books, {len(sessions)} sessions (read only)\n"
    )

    matches = plan_matches(read_calibre_books(args.library), readwise_books)
    updates = column_updates(matches, sessions)
    for match in matches:
        book_id = match.book.calibre_id
        how = (
            f"#{match.bookwise_id} by {match.matched_by}"
            if match.matched_by
            else "no match"
        )
        print(f"{book_id:>3} {match.book.title[:42]:<42} {how}")
        for column in COLUMNS:
            value = updates[column.label][book_id]
            if value is not None:
                text = " ".join(str(value).split())
                print(f"      {column.name:<24} {text[:70]}")


if __name__ == "__main__":
    main()

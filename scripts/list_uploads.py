"""List every upload returned by the Readwise Books uploads API.

Usage: uv run python scripts/list_uploads.py [--json] [--api-only]

Reads READWISE_TOKEN from the environment or the repository's .env file.
Makes GET requests only. Also lists uploaded ebook books with no /uploads record,
which can include older web uploads. Use --api-only for just GET /uploads.
Use --json to print complete records.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from calibre_bookwise.readwise_api import ReadOnlyClient, ReadwiseError

ROOT = Path(__file__).resolve().parent.parent


def token_from_env() -> str:
    token = os.environ.get("READWISE_TOKEN")
    if not token and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "READWISE_TOKEN":
                token = value.strip().strip("'\"")
                break
    if not token:
        raise SystemExit("Set READWISE_TOKEN in the environment or .env")
    return token


def cell(value: object) -> str:
    return " ".join(str(value or "").split())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", action="store_true", help="print complete upload records as JSON"
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="show only records from GET /uploads",
    )
    args = parser.parse_args(argv)

    try:
        client = ReadOnlyClient(token_from_env())
        uploads = client.list_uploads()
        legacy_books = []
        if not args.api_only:
            upload_book_ids = {
                upload["book"]["id"] for upload in uploads if upload.get("book")
            }
            legacy_books = [
                book
                for book in client.list_books()
                if book.get("creation_origin") == "uploaded_ebook"
                and book.get("id") not in upload_book_ids
            ]
    except ReadwiseError as error:
        print(error, file=sys.stderr)
        return 1

    if args.json:
        result = (
            {
                "uploads": uploads,
                "uploaded_books_without_upload_record": legacy_books,
            }
            if not args.api_only
            else uploads
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("created\tstatus\tfile_name\tupload_id\tbook_id\tbook_title")
        for upload in uploads:
            book = upload.get("book") or {}
            print(
                "\t".join(
                    cell(value)
                    for value in (
                        upload.get("created"),
                        upload.get("upload_status"),
                        upload.get("file_name"),
                        upload.get("id"),
                        book.get("id"),
                        book.get("title"),
                    )
                )
            )
        if not args.api_only:
            print("\nUploaded ebook books absent from /uploads:")
            print("created\tshelf\ttitle\tbook_id")
            for book in legacy_books:
                print(
                    "\t".join(
                        cell(value)
                        for value in (
                            book.get("created"),
                            book.get("status"),
                            book.get("title"),
                            book.get("id"),
                        )
                    )
                )
    print(f"{len(uploads)} uploads", file=sys.stderr)
    if not args.api_only:
        print(
            f"{len(legacy_books)} uploaded books without an upload record",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

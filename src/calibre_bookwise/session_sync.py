"""Opt-in upload of finished Calibre reading sessions to Readwise Books."""

import hashlib
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .bookwise import local_timezone, session_item, skip_reason
from .live import session_from_record
from .matching import CalibreBook, plan_matches
from .readwise_api import (
    BASE_URL,
    Fetch,
    ReadOnlyClient,
    ReadwiseError,
    _error_message,
    _urlopen,
)


class SessionSender(Protocol):
    def list_books(self) -> list[dict]: ...
    def create_book(self, item: dict) -> int: ...
    def create_session(self, item: dict) -> int: ...


class BookCreationUncertain(ReadwiseError):
    """A book POST may have succeeded even though its result was not received."""


class BooksClient:
    """Write client kept separate from the GET-only metadata client."""

    def __init__(self, token: str, fetch: Fetch = _urlopen):
        self.token = token.strip()
        self.fetch = fetch
        self.reader = ReadOnlyClient(token, fetch=fetch)

    def list_books(self) -> list[dict]:
        return self.reader.list_books()

    def create_book(self, item: dict) -> int:
        return self._create("books", item, "book_id")

    def create_session(self, item: dict) -> int:
        return self._create("sessions", item, "session_id")

    def _create(self, path: str, item: dict, id_field: str) -> int:
        request = urllib.request.Request(
            BASE_URL + path,
            data=json.dumps({"items": [item]}).encode(),
            method="POST",
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            status, _headers, body = self.fetch(request)
        except OSError as error:
            raise ReadwiseError(f"Couldn't reach Readwise: {error}") from error
        if status != 200:
            raise ReadwiseError(_error_message(status, body))
        try:
            [result] = json.loads(body)["results"]
            if result["status"] in ("created", "existing") and isinstance(
                result.get(id_field), int
            ):
                return result[id_field]
            error = result.get("error") or {}
            raise ReadwiseError(f"Readwise {path} error: {error}")
        except (ValueError, KeyError, TypeError) as error:
            raise ReadwiseError(f"Unexpected Readwise {path} response.") from error


def record_key(record: dict) -> str:
    """Stable identity for one immutable entry in sessions.jsonl."""
    fields = (record["book_key"], record["start"], record["end"], record["finished_at"])
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


class SessionSync:
    def __init__(self, data_dir: Path, records: list[dict], clock=time.time):
        self.path = data_dir / "session_uploads.json"
        self.clock = clock
        self.lock = threading.RLock()
        if self.path.exists():
            self.state: dict[str, Any] = json.loads(self.path.read_text())
        else:
            self.state = {"mode": "dry_run", "items": {}, "book_ids": {}, "retry_at": 0}
            self._register(records)
            self._save()
        if (
            not isinstance(self.state, dict)
            or self.state.get("mode") not in ("dry_run", "send")
            or not isinstance(self.state.get("items"), dict)
            or not isinstance(self.state.get("book_ids"), dict)
        ):
            raise ValueError("Invalid session upload state; restore from backup.")
        for item in self.state["items"].values():
            if item.get("status") == "sending":
                item.update(
                    status="needs review",
                    error="Upload outcome unknown after interruption; check Readwise before retrying.",
                )
                self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2))
        temporary.replace(self.path)

    def _register(self, records: list[dict]):
        added = False
        for record in records:
            key = record_key(record)
            if key not in self.state["items"]:
                added = True
                self.state["items"][key] = {
                    "status": "skipped"
                    if record["status"] == "skipped"
                    else ("pending" if self.state["mode"] == "send" else "local"),
                    "error": "",
                }
        return added

    def set_mode(self, mode: str, records: list[dict]):
        if mode not in ("dry_run", "send"):
            raise ValueError("Invalid session mode")
        with self.lock:
            # Anything already recorded before the mode change stays local.
            self.state["mode"] = "dry_run"
            self._register(records)
            if mode == "dry_run":
                for item in self.state["items"].values():
                    if item["status"] == "pending":
                        item["status"] = "local"
            self.state["mode"] = mode
            self._save()

    def status(self, record: dict) -> dict:
        with self.lock:
            return self.state["items"].get(
                record_key(record), {"status": record["status"]}
            )

    def queue_local(self, record: dict):
        """Send one selected local session even while automatic uploads are off."""
        self.queue_locals([record])

    def queue_locals(self, records: list[dict]):
        """Queue an explicit selection atomically, without changing the mode."""
        with self.lock:
            self._register(records)
            items = [self.state["items"][record_key(record)] for record in records]
            if not records or any(
                item["status"] != "local" or skip_reason(session_from_record(record))
                for item, record in zip(items, records)
            ):
                raise ValueError("Select eligible local sessions to send.")
            for item in items:
                item.update(status="pending", manual=True, error="")
            self._save()

    def _can_send(self, item: dict) -> bool:
        return item["status"] == "pending" and (
            self.state["mode"] == "send" or item.get("manual", False)
        )

    def retry_after_review(self, record: dict):
        with self.lock:
            item = self.state["items"][record_key(record)]
            if item["status"] != "needs review":
                raise ValueError("Only sessions needing review can be retried.")
            item.update(status="pending", manual=True, error="")
            self.state["retry_at"] = 0
            self._save()

    def poll(
        self, records: list[dict], books: dict[str, dict], client: SessionSender | None
    ):
        with self.lock:
            if self._register(records):
                self._save()
            if client is None or self.clock() < self.state.get("retry_at", 0):
                return
            for record in records:
                key = record_key(record)
                item = self.state["items"][key]
                if not self._can_send(item):
                    continue
                book = books.get(record["book_key"])
                if book is None:
                    continue  # wait for the originating library to be active
                session = session_from_record(record)
                if reason := skip_reason(session):
                    item.update(status="skipped", error=reason)
                    self._save()
                    break
                try:
                    book_id = self._book_id(record["book_key"], book, item, client)
                except BookCreationUncertain as error:
                    item.update(status="needs review", error=str(error))
                    self._save()
                    break
                except ReadwiseError as error:
                    item["error"] = str(error)
                    self.state["retry_at"] = self.clock() + 60
                    self._save()
                    break
                if not self._can_send(item):
                    break  # mode changed while the book lookup was in flight
                if book_id is None:
                    item.update(
                        status="needs review", error="Cannot match or create this book."
                    )
                    self._save()
                    break
                payload = session_item(session, book_id, local_timezone())
                payload["publish_to_feed"] = False
                item.update(status="sending", error="")
                self._save()  # a crash or ambiguous response must not resend
                self.lock.release()
                try:
                    session_id = client.create_session(payload)
                except Exception as error:  # noqa: BLE001 - any lost POST result is ambiguous
                    upload_error = error
                else:
                    upload_error = None
                finally:
                    self.lock.acquire()
                if upload_error is not None:
                    item.update(status="needs review", error=str(upload_error))
                else:
                    item.update(status="uploaded", session_id=session_id)
                self._save()
                break

    def _book_id(
        self, key: str, book: dict, item: dict, client: SessionSender
    ) -> int | None:
        if key in self.state["book_ids"]:
            return self.state["book_ids"][key]
        calibre_book = CalibreBook(
            book["book_id"],
            book["title"],
            tuple(book["authors"]),
            book.get("isbn"),
            book.get("asin"),
        )
        self.lock.release()
        try:
            remote_books = client.list_books()
        finally:
            self.lock.acquire()
        if not self._can_send(item):
            return None
        [match] = plan_matches([calibre_book], remote_books)
        if match.bookwise_id is not None:
            book_id = match.bookwise_id
        elif match.create_item is not None:
            self.lock.release()
            try:
                book_id = client.create_book(match.create_item)
            except ReadwiseError as error:
                raise BookCreationUncertain(
                    f"Book creation outcome unknown: {error}. Check Readwise before retrying."
                ) from error
            finally:
                self.lock.acquire()
        else:
            return None
        self.state["book_ids"][key] = book_id
        self._save()
        return book_id

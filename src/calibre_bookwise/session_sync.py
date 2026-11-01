"""Opt-in upload of finished Calibre reading sessions to Readwise Books."""

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .book_map import definite_links, read_book_map, remember
from .bookwise import SessionStatus, local_timezone, session_item, skip_reason
from .live import session_from_record
from .locking import unlocked
from .matching import CalibreBook, plan_matches
from .readwise_api import (
    BASE_URL,
    ApiClient,
    Fetch,
    ReadOnlyClient,
    ReadwiseError,
    urlopen,
)
from .storage import atomic_write_json


class SessionMode(StrEnum):
    DRY_RUN = "dry_run"  # finished sessions stay local unless sent by hand
    SEND = "send"  # newly finished sessions are queued to send


class SessionSender(Protocol):
    def list_books(self) -> list[dict]: ...
    def create_book(self, item: dict) -> int: ...
    def create_session(self, item: dict) -> int: ...


class BookCreationUncertain(ReadwiseError):
    """A book POST may have succeeded even though its result was not received."""


class BooksClient(ApiClient):
    """Write client kept separate from the GET-only metadata client."""

    def __init__(self, token: str, fetch: Fetch = urlopen):
        super().__init__(token, fetch)
        self.reader = ReadOnlyClient(token, fetch=fetch)

    def list_books(self) -> list[dict]:
        return self.reader.list_books()

    def create_book(self, item: dict) -> int:
        return self._create("books", item, "book_id")

    def create_session(self, item: dict) -> int:
        return self._create("sessions", item, "session_id")

    def _create(self, path: str, item: dict, id_field: str) -> int:
        body = self._request("POST", BASE_URL + path, {"items": [item]})
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
        self.data_dir = data_dir
        self.path = data_dir / "session_uploads.json"
        self.clock = clock
        self.lock = threading.RLock()
        if self.path.exists():
            self.state: dict[str, Any] = json.loads(self.path.read_text())
        else:
            self.state = {
                "mode": SessionMode.DRY_RUN,
                "publish_to_feed": False,
                "items": {},
                "book_ids": {},
                "retry_at": 0,
            }
            self._register(records)
            self._save()
        if (
            not isinstance(self.state, dict)
            or self.state.get("mode") not in tuple(SessionMode)
            or not isinstance(self.state.get("items"), dict)
            or not isinstance(self.state.get("book_ids"), dict)
        ):
            raise ValueError("Invalid session upload state; restore from backup.")
        for item in self.state["items"].values():
            if item.get("status") == SessionStatus.SENDING:
                item.update(
                    status=SessionStatus.NEEDS_REVIEW,
                    error="Upload outcome unknown after interruption; check Readwise before retrying.",
                )
                self._save()

    def _save(self):
        atomic_write_json(self.path, self.state, indent=2)

    def _register(self, records: list[dict]):
        added = False
        for record in records:
            key = record_key(record)
            if key not in self.state["items"]:
                added = True
                self.state["items"][key] = {
                    "status": SessionStatus.SKIPPED
                    if record["status"] == SessionStatus.SKIPPED
                    else (
                        SessionStatus.PENDING
                        if self.state["mode"] == SessionMode.SEND
                        else SessionStatus.LOCAL
                    ),
                    "error": "",
                    "publish_to_feed": self.state.get("publish_to_feed", False),
                }
        return added

    @property
    def sending(self) -> bool:
        """Whether newly finished sessions are sent automatically."""
        with self.lock:
            return self.state["mode"] == SessionMode.SEND

    @property
    def feed_default(self) -> bool:
        """Whether sessions queued from now on post to the Readwise feed."""
        with self.lock:
            return self.state.get("publish_to_feed", False)

    def book_id_for(self, book_key: str) -> int | None:
        """The Readwise book id this book file's sessions go to, once known."""
        with self.lock:
            return self.state["book_ids"].get(book_key)

    def set_publish_to_feed(self, enabled: bool):
        """Set the feed preference for sessions queued in the future."""
        with self.lock:
            previous = self.state.get("publish_to_feed", False)
            self.state["publish_to_feed"] = bool(enabled)
            try:
                self._save()
            except OSError:
                self.state["publish_to_feed"] = previous
                raise

    def publish_to_feed(self, record: dict) -> bool:
        """The feed setting a preview or pending upload will use."""
        with self.lock:
            item = self.state["items"].get(record_key(record), {})
            if item.get("status") == SessionStatus.LOCAL:
                return self.state.get("publish_to_feed", False)
            return item.get("publish_to_feed", False)

    def set_mode(self, mode: str, records: list[dict]):
        if mode not in tuple(SessionMode):
            raise ValueError("Invalid session mode")
        with self.lock:
            # Anything already recorded before the mode change stays local.
            self.state["mode"] = SessionMode.DRY_RUN
            self._register(records)
            if mode == SessionMode.DRY_RUN:
                for item in self.state["items"].values():
                    if item["status"] == SessionStatus.PENDING:
                        item["status"] = SessionStatus.LOCAL
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
                item["status"] != SessionStatus.LOCAL
                or skip_reason(session_from_record(record))
                for item, record in zip(items, records)
            ):
                raise ValueError("Select eligible local sessions to send.")
            for item in items:
                item.update(
                    status=SessionStatus.PENDING,
                    manual=True,
                    error="",
                    publish_to_feed=self.state.get("publish_to_feed", False),
                )
            self._save()

    def _can_send(self, item: dict) -> bool:
        return item["status"] == SessionStatus.PENDING and (
            self.state["mode"] == SessionMode.SEND or item.get("manual", False)
        )

    def retry_after_review(self, record: dict):
        with self.lock:
            item = self.state["items"][record_key(record)]
            if item["status"] != SessionStatus.NEEDS_REVIEW:
                raise ValueError("Only sessions needing review can be retried.")
            item.update(status=SessionStatus.PENDING, manual=True, error="")
            self.state["retry_at"] = 0
            self._save()

    def poll(
        self,
        records: list[dict],
        books: Mapping[str, CalibreBook],
        client: SessionSender | None,
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
                    item.update(status=SessionStatus.SKIPPED, error=reason)
                    self._save()
                    break
                try:
                    book_id = self._book_id(record["book_key"], book, item, client)
                except BookCreationUncertain as error:
                    item.update(status=SessionStatus.NEEDS_REVIEW, error=str(error))
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
                        status=SessionStatus.NEEDS_REVIEW,
                        error="Cannot match or create this book.",
                    )
                    self._save()
                    break
                payload = session_item(session, book_id, local_timezone())
                payload["publish_to_feed"] = item.get("publish_to_feed", False)
                item.update(status=SessionStatus.SENDING, error="")
                self._save()  # a crash or ambiguous response must not resend
                with unlocked(self.lock):
                    try:
                        session_id = client.create_session(payload)
                    except Exception as error:  # noqa: BLE001 - any lost POST result is ambiguous
                        upload_error = error
                    else:
                        upload_error = None
                if upload_error is not None:
                    item.update(
                        status=SessionStatus.NEEDS_REVIEW, error=str(upload_error)
                    )
                else:
                    item.update(status=SessionStatus.UPLOADED, session_id=session_id)
                self._save()
                break

    def _book_id(
        self, key: str, book: CalibreBook, item: dict, client: SessionSender
    ) -> int | None:
        if key in self.state["book_ids"]:
            return self.state["book_ids"][key]
        saved = read_book_map(self.data_dir)  # links metadata sync has made
        if book.calibre_id in saved:
            book_id = saved[book.calibre_id]
        else:
            with unlocked(self.lock):
                remote_books = client.list_books()
            if not self._can_send(item):
                return None
            [match] = plan_matches([book], remote_books)
            if match.bookwise_id is not None:
                book_id = match.bookwise_id
                remember(self.data_dir, definite_links([match]))
            elif match.create_item is not None:
                try:
                    with unlocked(self.lock):
                        book_id = client.create_book(match.create_item)
                except ReadwiseError as error:
                    raise BookCreationUncertain(
                        f"Book creation outcome unknown: {error}. Check Readwise before retrying."
                    ) from error
                remember(self.data_dir, {book.calibre_id: book_id})
            else:
                return None
        self.state["book_ids"][key] = book_id
        self._save()
        return book_id

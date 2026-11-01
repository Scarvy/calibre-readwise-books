"""Opt-in, one-way sync of Calibre highlights and their notes.

Baseline on automatic enable; older highlights require explicit selection.
Persist intent before any network IO.
Only this module's uploaded highlights can have their notes updated; no deletes.
"""

import json
import threading
import time
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple, Protocol

from .locking import unlocked
from .matching import CalibreBook
from .readwise_api import ApiClient, RateLimited, ReadwiseError
from .storage import atomic_write_json

HIGHLIGHTS_URL = "https://readwise.io/api/v2/highlights/"
SETTLE_SECONDS = 5


class HighlightStatus(StrEnum):
    """Where one Calibre highlight is on its way to Readwise."""

    UNAVAILABLE = "unavailable"  # Calibre saved no annotation id
    LOCAL = "local"  # not queued: existed before sync was enabled
    PENDING = "pending"  # queued to send (or to update its note)
    UPLOADED = "uploaded"
    REMOVED = "removed locally"  # deleted in Calibre; kept in Readwise
    INVALID = "invalid"  # over Readwise's limits; waits for an edit
    REJECTED = "rejected"  # Readwise refused it; retry only by hand


class Waiting(StrEnum):
    """Why a pending highlight has not been sent yet, for the status column."""

    RETRY = "retry"  # the last attempt failed and will be repeated
    TOKEN = "token needed"
    PAUSED = "paused"  # automatic upload is off and it wasn't sent by hand
    SETTLING = "settling"  # the viewer may still be saving it


class Explanation(NamedTuple):
    message: str  # for the user
    waiting: Waiting | None = None


class HighlightSender(Protocol):
    def send(self, payload: dict, remote_id: int | None = None) -> int: ...


class HighlightClient(ApiClient):
    """Separate from the deliberately GET-only metadata client. No DELETE method."""

    _retry_default = 60.0

    def send(self, payload: dict, remote_id: int | None = None) -> int:
        method, url, data = highlight_request(payload, remote_id)
        try:
            body = self._request(method, url, data)
        except ReadwiseError as error:
            if error.status in (400, 422):
                raise HighlightRejected(str(error), error.status) from error
            raise
        if remote_id is not None:
            return remote_id
        books = json.loads(body)
        ids = [i for book in books for i in book.get("modified_highlights", [])]
        if len(ids) != 1 or not isinstance(ids[0], int):
            raise ReadwiseError(
                "Readwise returned no unique highlight ID; retry is safe, but "
                "this highlight may already exist in Readwise."
            )
        return ids[0]


class HighlightRejected(ReadwiseError):
    """Readwise rejected this payload; repeating it unchanged will not help."""


def highlight_key(book_key: str, uuid: str) -> str:
    """Sync state key for one annotation: annots file key, then Calibre's uuid."""
    return f"{book_key}:{uuid}"


def book_key_of(key: str) -> str:
    """The annots file key a highlight_key belongs to."""
    return key.split(":", 1)[0]


def highlight_request(
    payload: dict, remote_id: int | None = None
) -> tuple[str, str, dict]:
    """Return the method, URL, and JSON body used for a highlight request."""
    if remote_id is None:
        # The note is optional; an empty annotation is better omitted on create.
        item = {key: value for key, value in payload.items() if key != "note" or value}
        return "POST", HIGHLIGHTS_URL, {"highlights": [item]}
    return "PATCH", f"{HIGHLIGHTS_URL}{remote_id}/", payload


def read_annotations(path: Path) -> list[dict]:
    """Fail rather than baseline an unreadable/partially written file as empty."""
    value = json.loads(path.read_text())
    if not isinstance(value, list) or any(not isinstance(a, dict) for a in value):
        raise ValueError("Invalid Calibre annotation file")
    return value


def highlight_payload(annotation: dict, book: CalibreBook) -> dict:
    text = annotation.get("highlighted_text") or ""
    note = annotation.get("notes") or ""
    title = book.title or ""
    author = ", ".join(book.authors)
    for name, value, limit in (
        ("text", text, 8191),
        ("note", note, 8191),
        ("title", title, 511),
        ("author", author, 1024),
    ):
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"Highlight {name} exceeds Readwise's limit ({limit}).")
    if not text.strip() or not title.strip():
        raise ValueError("Highlight text and book title are required.")
    payload = {
        "text": text,
        "note": note,
        "title": title,
        "author": author,
        "category": "books",
        "source_type": "calibre",
        "location_type": "order",
    }
    if annotation.get("timestamp"):
        payload["highlighted_at"] = annotation["timestamp"]
    return payload


def preview_highlights(
    annots_dir: Path, books: Mapping[str, CalibreBook]
) -> tuple[list[dict], list[str]]:
    """Build create requests from saved annotations without changing sync state."""
    previews = []
    errors = []
    for path in annots_dir.glob("*.json"):
        book = books.get(path.stem)
        if book is None:
            continue
        try:
            annotations = read_annotations(path)
        except (OSError, ValueError) as error:
            errors.append(f"{book.title or path.name}: {error}")
            continue
        for annotation in annotations:
            if annotation.get("type") != "highlight" or annotation.get("removed"):
                continue
            try:
                payload = highlight_payload(annotation, book)
            except ValueError as error:
                errors.append(f"{book.title or path.name}: {error}")
                continue
            method, url, body = highlight_request(payload)
            previews.append(
                {
                    "key": highlight_key(path.stem, annotation["uuid"])
                    if annotation.get("uuid")
                    else None,
                    "book": book.title or "",
                    "timestamp": annotation.get("timestamp") or "",
                    "text": payload["text"],
                    "note": payload["note"],
                    "payload": payload,
                    "method": method,
                    "url": url,
                    "body": body,
                }
            )
    previews.sort(key=lambda item: str(item["timestamp"]), reverse=True)
    return previews, errors


class PreviewCache:
    """preview_highlights for a window that refreshes every half second.

    Re-reads the annotation files only when one changes or the library does.
    Callers may add fields to the returned previews but not change payloads.
    """

    def __init__(self, annots_dir: Path):
        self.annots_dir = annots_dir
        self._stamp: object = None
        self._books: Mapping[str, CalibreBook] | None = None
        self._result: tuple[list[dict], list[str]] = ([], [])

    def get(self, books: Mapping[str, CalibreBook]) -> tuple[list[dict], list[str]]:
        stamp = self._files_stamp()
        if stamp is None or stamp != self._stamp or books is not self._books:
            self._result = preview_highlights(self.annots_dir, books)
            self._stamp, self._books = stamp, books
        return self._result

    def _files_stamp(self) -> tuple | None:
        files = []
        try:
            for path in self.annots_dir.glob("*.json"):
                stat = path.stat()
                files.append((path.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            return None  # a file vanished mid-scan: just read them again
        return tuple(sorted(files))


class HighlightSync:
    def __init__(self, data_dir: Path, annots_dir: Path, clock=time.time):
        self.path = data_dir / "highlights.json"
        self.annots_dir = annots_dir
        self.clock = clock
        self.lock = threading.RLock()
        # A corrupt queue must never silently reset deduplication state.
        self.state: dict = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {
                "enabled": False,
                "seen": [],
                "items": {},
                "retry_at": 0,
            }
        )

        if (
            not isinstance(self.state, dict)
            or not isinstance(self.state.get("enabled"), bool)
            or not isinstance(self.state.get("seen"), list)
            or not isinstance(self.state.get("items"), dict)
            or not all(isinstance(v, dict) for v in self.state["items"].values())
        ):
            raise ValueError("Invalid highlight sync state; restore from backup.")

    def _save(self):
        atomic_write_json(self.path, self.state, indent=2)

    @property
    def enabled(self) -> bool:
        """Whether new highlights are uploaded automatically."""
        with self.lock:
            return self.state["enabled"]

    def summary(self) -> tuple[dict[str, int], list[str]]:
        """How many highlights are in each status, and the distinct errors."""
        with self.lock:
            items = list(self.state["items"].values())
        counts: dict[str, int] = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return counts, sorted({item["error"] for item in items if item.get("error")})

    def set_enabled(self, enabled: bool):
        with self.lock:
            if enabled:
                # Read ALL files before changing state: a failed baseline aborts enable.
                seen = set(self.state["seen"])
                for path in self.annots_dir.glob("*.json"):
                    for a in read_annotations(path):
                        if a.get("type") == "highlight" and a.get("uuid"):
                            seen.add(highlight_key(path.stem, a["uuid"]))
                self.state["seen"] = sorted(seen)
            self.state["enabled"] = enabled
            self._save()

    def status(self, key: str | None) -> str:
        if key is None:
            return HighlightStatus.UNAVAILABLE
        with self.lock:
            return self.state["items"].get(key, {}).get("status", HighlightStatus.LOCAL)

    def explanation(self, key: str | None, token_available: bool) -> Explanation:
        """Why this highlight is waiting, or why Readwise rejected it."""
        if key is None:
            return Explanation(
                "Calibre did not save an annotation ID for this highlight."
            )
        with self.lock:
            item = self.state["items"].get(key)
            if item is None:
                return Explanation("Saved locally. Select it to upload.")
            pending = item.get("status") == HighlightStatus.PENDING
            if item.get("error"):
                return Explanation(item["error"], Waiting.RETRY if pending else None)
            if not pending:
                return Explanation("")
            if not token_available:
                return Explanation("Waiting for a Readwise token.", Waiting.TOKEN)
            if not self.state["enabled"] and not item.get("manual"):
                return Explanation("Automatic upload is paused.", Waiting.PAUSED)
            if self.clock() < item.get("ready_at", 0):
                return Explanation(
                    "Waiting for the highlight to finish saving.", Waiting.SETTLING
                )
            if self.clock() < self.state.get("retry_at", 0):
                return Explanation("Waiting for the next retry.", Waiting.RETRY)
            return Explanation("Queued for the next upload attempt.")

    def queue_saved(self, keys: list[str], books: Mapping[str, CalibreBook]):
        """Queue chosen older highlights, including while automatic upload is off."""
        self._queue_saved(keys, books, HighlightStatus.LOCAL)

    def retry_rejected(self, keys: list[str], books: Mapping[str, CalibreBook]):
        """Explicitly retry rejected highlights after inspecting the error."""
        self._queue_saved(keys, books, HighlightStatus.REJECTED)

    def _queue_saved(
        self, keys: list[str], books: Mapping[str, CalibreBook], expected: str
    ):
        with self.lock:
            previews, _errors = preview_highlights(self.annots_dir, books)
            available = {p["key"]: p for p in previews if p["key"] is not None}
            if not keys or any(
                key not in available or self.status(key) != expected for key in keys
            ):
                raise ValueError(
                    f"Select saved {expected} highlights with annotation IDs."
                )
            now = self.clock()
            seen = set(self.state["seen"])
            for key in keys:
                self.state["items"][key] = {
                    "payload": available[key]["payload"],
                    "status": HighlightStatus.PENDING,
                    "ready_at": now,
                    "manual": True,
                    "error": "",
                }
                seen.add(key)
            self.state["seen"] = sorted(seen)
            self._save()

    def poll(self, books: Mapping[str, CalibreBook], client: HighlightSender | None):
        with self.lock:
            now = self.clock()
            seen = set(self.state["seen"])
            items = self.state["items"]
            paths = self.annots_dir.glob("*.json") if self.state["enabled"] else ()
            for path in paths:
                book = books.get(path.stem)
                if book is None:
                    continue  # another library; do not attribute it to this one
                try:
                    annotations = read_annotations(path)
                except (OSError, ValueError):
                    continue  # viewer may be mid-write; try again next poll
                for a in annotations:
                    if a.get("type") != "highlight" or not a.get("uuid"):
                        continue
                    key = highlight_key(path.stem, a["uuid"])
                    item = items.get(key)
                    if key in seen and item is None:
                        continue  # pre-existing highlight, including edits to it
                    seen.add(key)
                    if a.get("removed"):
                        if item:
                            item["status"] = HighlightStatus.REMOVED
                        continue
                    try:
                        payload = highlight_payload(a, book)
                    except ValueError as error:
                        items[key] = {
                            **(item or {}),
                            "status": HighlightStatus.INVALID,
                            "error": str(error),
                        }
                        continue
                    if (
                        item is None
                        or item.get("payload") != payload
                        or item.get("status") == HighlightStatus.INVALID
                    ):
                        items[key] = {
                            **(item or {}),
                            "payload": payload,
                            "status": HighlightStatus.PENDING,
                            "ready_at": now + SETTLE_SECONDS,
                            "error": "",
                        }
            if self.state["enabled"]:
                self.state["seen"] = sorted(seen)
                self._save()  # durable queue before the first request
            if client is None or now < self.state.get("retry_at", 0):
                return
            # One request per poll: no sleeps or unbounded background request loops.
            for key, item in items.items():
                if (
                    item["status"] != HighlightStatus.PENDING
                    or now < item["ready_at"]
                    or (not self.state["enabled"] and not item.get("manual"))
                ):
                    continue
                # Don't upload queued books from a different active library.
                if book_key_of(key) not in books:
                    continue
                remote_id = item.get("remote_id")
                if remote_id is not None:
                    if item["payload"]["note"] == item.get("sent_note"):
                        item["status"] = HighlightStatus.UPLOADED
                        continue
                    payload = {"note": item["payload"]["note"]}
                else:
                    payload = item["payload"]
                try:
                    with unlocked(self.lock):
                        uploaded_id = client.send(payload, remote_id)
                except (OSError, ValueError, ReadwiseError) as error:
                    item["error"] = str(error)
                    if isinstance(error, HighlightRejected):
                        item["status"] = HighlightStatus.REJECTED
                    else:
                        delay = error.delay if isinstance(error, RateLimited) else 60
                        self.state["retry_at"] = now + delay
                else:
                    # Another poll may have replaced the item (a note edited
                    # mid-request). Keep the id on whatever is there now, or
                    # the next poll would create the highlight a second time.
                    current = items.setdefault(key, item)
                    current["remote_id"] = uploaded_id
                    current["sent_note"] = payload.get("note", "")
                    if current is item:
                        item.update(status=HighlightStatus.UPLOADED, error="")
                    # else it stays pending; the next poll sends any new note.
                self._save()
                break

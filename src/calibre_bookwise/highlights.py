"""Opt-in, one-way sync of Calibre highlights and their notes.

Baseline on automatic enable; older highlights require explicit selection.
Persist intent before any network IO.
Only this module's uploaded highlights can have their notes updated; no deletes.
"""

import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Protocol

from .readwise_api import Fetch, ReadwiseError, _urlopen

HIGHLIGHTS_URL = "https://readwise.io/api/v2/highlights/"
SETTLE_SECONDS = 5


class HighlightSender(Protocol):
    def send(self, payload: dict, remote_id: int | None = None) -> int: ...


class HighlightClient:
    """Separate from the deliberately GET-only metadata client. No DELETE method."""

    def __init__(self, token: str, fetch: Fetch = _urlopen):
        self.token = token.strip()
        self.fetch = fetch

    def send(self, payload: dict, remote_id: int | None = None) -> int:
        method, url, data = highlight_request(payload, remote_id)
        request = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            method=method,
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "application/json",
            },
        )
        status, headers, body = self.fetch(request)
        if status == 429:
            try:
                delay = max(1, float(headers.get("Retry-After", 60)))
            except ValueError:
                delay = 60
            raise RateLimited(delay)
        if status != 200:
            message = _highlight_error(status, body)
            if status in (400, 422):
                raise HighlightRejected(message)
            raise ReadwiseError(message)
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


class RateLimited(ReadwiseError):
    def __init__(self, delay: float):
        super().__init__("Readwise rate limit; upload will retry later.")
        self.delay = delay


class HighlightRejected(ReadwiseError):
    """Readwise rejected this payload; repeating it unchanged will not help."""


def _highlight_error(status: int, body: bytes) -> str:
    try:
        details = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        details = body.decode("utf-8", "replace").strip()
    if isinstance(details, dict):
        details = details.get("detail") or details.get("error") or details
    if isinstance(details, (dict, list)):
        details = json.dumps(details, ensure_ascii=False)
    detail = str(details).strip()[:500]
    return f"Readwise highlight upload failed (HTTP {status})" + (
        f": {detail}" if detail else "."
    )


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


def highlight_payload(annotation: dict, book: dict) -> dict:
    text = annotation.get("highlighted_text") or ""
    note = annotation.get("notes") or ""
    title = book.get("title") or ""
    author = ", ".join(book.get("authors") or ())
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
    annots_dir: Path, books: dict[str, dict]
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
            errors.append(f"{book.get('title') or path.name}: {error}")
            continue
        for annotation in annotations:
            if annotation.get("type") != "highlight" or annotation.get("removed"):
                continue
            try:
                payload = highlight_payload(annotation, book)
            except ValueError as error:
                errors.append(f"{book.get('title') or path.name}: {error}")
                continue
            method, url, body = highlight_request(payload)
            previews.append(
                {
                    "key": f"{path.stem}:{annotation['uuid']}"
                    if annotation.get("uuid")
                    else None,
                    "book": book.get("title") or "",
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2))
        temporary.replace(self.path)

    def set_enabled(self, enabled: bool):
        with self.lock:
            if enabled:
                # Read ALL files before changing state: a failed baseline aborts enable.
                seen = set(self.state["seen"])
                for path in self.annots_dir.glob("*.json"):
                    for a in read_annotations(path):
                        if a.get("type") == "highlight" and a.get("uuid"):
                            seen.add(f"{path.stem}:{a['uuid']}")
                self.state["seen"] = sorted(seen)
            self.state["enabled"] = enabled
            self._save()

    def status(self, key: str | None) -> str:
        if key is None:
            return "unavailable"
        with self.lock:
            return self.state["items"].get(key, {}).get("status", "local")

    def entry(self, key: str | None) -> dict:
        if key is None:
            return {}
        with self.lock:
            return self.state["items"].get(key, {}).copy()

    def explanation(self, key: str | None, token_available: bool) -> str:
        """Why this highlight is waiting, or why Readwise rejected it."""
        if key is None:
            return "Calibre did not save an annotation ID for this highlight."
        with self.lock:
            item = self.state["items"].get(key)
            if item is None:
                return "Saved locally. Select it to upload."
            if item.get("error"):
                return item["error"]
            if item.get("status") != "pending":
                return ""
            if not token_available:
                return "Waiting for a Readwise token."
            if not self.state["enabled"] and not item.get("manual"):
                return "Automatic upload is paused."
            if self.clock() < item.get("ready_at", 0):
                return "Waiting for the highlight to finish saving."
            if self.clock() < self.state.get("retry_at", 0):
                return "Waiting for the next retry."
            return "Queued for the next upload attempt."

    def queue_saved(self, keys: list[str], books: dict[str, dict]):
        """Queue chosen older highlights, including while automatic upload is off."""
        self._queue_saved(keys, books, "local")

    def retry_rejected(self, keys: list[str], books: dict[str, dict]):
        """Explicitly retry rejected highlights after inspecting the error."""
        self._queue_saved(keys, books, "rejected")

    def _queue_saved(self, keys: list[str], books: dict[str, dict], expected: str):
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
                    "status": "pending",
                    "ready_at": now,
                    "manual": True,
                    "error": "",
                }
                seen.add(key)
            self.state["seen"] = sorted(seen)
            self._save()

    def poll(self, books: dict[str, dict], client: HighlightSender | None):
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
                    key = f"{path.stem}:{a['uuid']}"
                    item = items.get(key)
                    if key in seen and item is None:
                        continue  # pre-existing highlight, including edits to it
                    seen.add(key)
                    if a.get("removed"):
                        if item:
                            item["status"] = "removed locally"
                        continue
                    try:
                        payload = highlight_payload(a, book)
                    except ValueError as error:
                        items[key] = {
                            **(item or {}),
                            "status": "invalid",
                            "error": str(error),
                        }
                        continue
                    if (
                        item is None
                        or item.get("payload") != payload
                        or item.get("status") == "invalid"
                    ):
                        items[key] = {
                            **(item or {}),
                            "payload": payload,
                            "status": "pending",
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
                    item["status"] != "pending"
                    or now < item["ready_at"]
                    or (not self.state["enabled"] and not item.get("manual"))
                ):
                    continue
                # Don't upload queued books from a different active library.
                if key.split(":", 1)[0] not in books:
                    continue
                try:
                    remote_id = item.get("remote_id")
                    if remote_id is not None:
                        if item["payload"]["note"] == item.get("sent_note"):
                            item["status"] = "uploaded"
                            continue
                        payload = {"note": item["payload"]["note"]}
                    else:
                        payload = item["payload"]
                    self.lock.release()
                    try:
                        uploaded_id = client.send(payload, remote_id)
                    finally:
                        self.lock.acquire()
                    item["remote_id"] = uploaded_id
                    item["sent_note"] = payload.get("note", "")
                    item["status"] = "uploaded"
                    item["error"] = ""
                except (OSError, ValueError, ReadwiseError) as error:
                    item["error"] = str(error)
                    if isinstance(error, HighlightRejected):
                        item["status"] = "rejected"
                    else:
                        delay = error.delay if isinstance(error, RateLimited) else 60
                        self.state["retry_at"] = now + delay
                self._save()
                break

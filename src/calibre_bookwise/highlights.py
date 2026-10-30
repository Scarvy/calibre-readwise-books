"""Opt-in, one-way sync of newly observed Calibre highlights and their notes.

Baseline on enable; no historical import. Persist intent before any network IO.
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
        url = HIGHLIGHTS_URL if remote_id is None else f"{HIGHLIGHTS_URL}{remote_id}/"
        data = {"highlights": [payload]} if remote_id is None else payload
        request = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            method="POST" if remote_id is None else "PATCH",
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
            raise ReadwiseError(f"Readwise highlight upload failed (HTTP {status}).")
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

    def poll(self, books: dict[str, dict], client: HighlightSender | None):
        with self.lock:
            if not self.state["enabled"]:
                return
            now = self.clock()
            seen = set(self.state["seen"])
            items = self.state["items"]
            for path in self.annots_dir.glob("*.json"):
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
            self.state["seen"] = sorted(seen)
            self._save()  # durable queue before the first request
            if client is None or now < self.state.get("retry_at", 0):
                return
            # One request per poll: no sleeps or unbounded background request loops.
            for key, item in items.items():
                if item["status"] != "pending" or now < item["ready_at"]:
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
                    delay = error.delay if isinstance(error, RateLimited) else 60
                    self.state["retry_at"] = now + delay
                self._save()
                break

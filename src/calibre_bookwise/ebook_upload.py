"""Upload one Calibre EPUB with the Readwise Books uploads API."""

import functools
import json
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from .matching import Match
from .readwise_api import BASE_URL, ApiClient, Fetch, ReadwiseError, urlopen

MAX_SIZE = 500_000_000
slow_urlopen = functools.partial(urlopen, timeout=600)


class LibraryStatus(StrEnum):
    """Where a Calibre book stands in the user's Readwise Books library."""

    UPLOADED = "uploaded ebook"
    IN_READWISE = "in Readwise"
    DELETED = "deleted in Readwise"
    NOT_IN_READWISE = "not in Readwise"


def library_status(match: Match) -> LibraryStatus:
    remote = match.bookwise
    if match.bookwise_id is None or not remote:
        return LibraryStatus.NOT_IN_READWISE
    if remote.get("status") == "deleted":
        return LibraryStatus.DELETED
    if remote.get("creation_origin") == "uploaded_ebook":
        return LibraryStatus.UPLOADED
    return LibraryStatus.IN_READWISE


class UploadUncertain(ReadwiseError):
    """The completion request may have succeeded; do not start another upload."""


def _result(body: bytes, expected: set[str]) -> dict:
    try:
        result = json.loads(body)["results"][0]
        if result["status"] not in expected:
            error = result.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ReadwiseError(message or "Readwise rejected the upload.")
        return result
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise ReadwiseError("Unexpected Readwise upload response.") from error


class UploadClient(ApiClient):
    """Start, transfer, complete, and poll one documented EPUB upload."""

    def __init__(
        self,
        token: str,
        fetch: Fetch = slow_urlopen,
        sleep: Callable[[float], None] = time.sleep,
        polls: int = 12,
    ):
        super().__init__(token, fetch)
        self.sleep = sleep
        self.polls = polls

    def upload(self, path: Path) -> dict:
        """Return the upload record, ready or still processing after polling."""
        file_name = path.name
        if not file_name.lower().endswith(".epub") or len(file_name) > 255:
            raise ReadwiseError("Select an EPUB with a file name under 256 characters.")
        size = path.stat().st_size
        if not 0 < size <= MAX_SIZE:
            raise ReadwiseError("EPUBs must be nonempty and no larger than 500 MB.")

        started = _result(
            self._request(
                "POST",
                BASE_URL + "uploads",
                {"items": [{"file_name": file_name, "size": size}]},
            ),
            {"created"},
        )
        try:
            upload_id = started["upload"]["id"]
            url = started["upload_url"]
            headers = started["upload_headers"]
            host = urllib.parse.urlsplit(url).hostname or ""
            if (
                not url.startswith("https://")
                or not host.endswith(".amazonaws.com")
                or not isinstance(headers, dict)
                or headers.get("Content-Length") != str(size)
            ):
                raise ReadwiseError("Readwise returned an unsafe upload destination.")
        except (KeyError, TypeError) as error:
            raise ReadwiseError("Unexpected Readwise upload response.") from error

        # The signed URL gets only the supplied headers, never the API token.
        try:
            with path.open("rb") as stream:
                if os.fstat(stream.fileno()).st_size != size:
                    raise ReadwiseError(
                        "The EPUB changed before the transfer. Try again."
                    )
                request = urllib.request.Request(
                    url, data=stream, method="PUT", headers=headers
                )
                status, _response_headers, _body = self.fetch(request)
        except OSError as error:
            raise ReadwiseError(f"Couldn't send the EPUB: {error}") from error
        if status not in (200, 201, 204):
            raise ReadwiseError(f"EPUB transfer failed (HTTP {status}).")

        try:
            completed = _result(
                self._request(
                    "POST",
                    BASE_URL + "uploads/complete",
                    {"items": [{"id": upload_id, "status": "currently_reading"}]},
                ),
                {"processing", "existing"},
            )
        except ReadwiseError as error:
            raise UploadUncertain(
                f"Upload {upload_id} may be processing: {error} "
                "Check Readwise before starting another upload."
            ) from error

        upload = completed.get("upload")
        if not isinstance(upload, dict):
            raise UploadUncertain(
                f"Upload {upload_id} may be processing. Check Readwise before retrying."
            )
        for _ in range(self.polls):
            if upload.get("upload_status") in ("ready", "failed"):
                break
            self.sleep(5)
            try:
                page = json.loads(
                    self._request("GET", BASE_URL + f"uploads?ids={upload_id}")
                )
                upload = page["results"][0]
            except (ReadwiseError, ValueError, KeyError, IndexError, TypeError):
                # Completion succeeded. Keep that known outcome if polling fails.
                break
        if upload.get("upload_status") == "failed":
            raise ReadwiseError(
                f"Readwise could not process upload {upload_id}. Check it in Readwise."
            )
        return upload

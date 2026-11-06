"""Readwise API requests, and the read-only client for the Readwise Books API.

ApiClient sends every request: auth, errors and rate limits in one place.
ReadOnlyClient is deliberately GET-only: it has no method that can create,
change or delete anything in the user's Readwise account. The write clients
live with the code that needs them (session_sync.py, highlights.py).

Docs: https://readwise.io/books_api. Base URL /books/api/v1/, header
"Authorization: Token <token>", pages of up to 1000 results with a "next"
URL, 20 requests a minute per endpoint (429 + Retry-After when exceeded).
Stdlib only: this has to run inside Calibre's bundled Python 3.11.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

BASE_URL = "https://readwise.io/books/api/v1/"
PAGE_SIZE = 1000
MAX_RETRIES = 3


class ReadwiseError(Exception):
    """A Readwise API request failed; str() is a message for the user."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # the HTTP status, when Readwise answered


class RateLimited(ReadwiseError):
    def __init__(self, delay: float):
        super().__init__("Readwise rate limit; will retry later.", 429)
        self.delay = delay


Fetch = Callable[[urllib.request.Request], tuple[int, dict, bytes]]


def urlopen(
    request: urllib.request.Request, timeout: float = 30
) -> tuple[int, dict, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


class ApiClient:
    """Sends authenticated Readwise requests; subclasses choose which ones."""

    _retry_default = 30.0  # seconds to wait when a 429 gives no usable Retry-After

    def __init__(self, token: str, fetch: Fetch = urlopen):
        self.token = token.strip()
        self.fetch = fetch

    def _request(self, method: str, url: str, payload: object = None) -> bytes:
        """The 200 response body; raises ReadwiseError (or RateLimited) else."""
        headers = {"Authorization": f"Token {self.token}"}
        data = None
        if payload is None:
            headers["Accept"] = "application/json"
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            status, response_headers, body = self.fetch(request)
        except OSError as error:
            raise ReadwiseError(f"Couldn't reach Readwise: {error}") from error
        if status == 429:
            raise RateLimited(retry_after(response_headers, self._retry_default))
        if status != 200:
            raise ReadwiseError(error_message(status, body), status)
        return body


class ReadOnlyClient(ApiClient):
    def __init__(
        self,
        token: str,
        fetch: Fetch = urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(token, fetch)
        self.sleep = sleep

    def list_books(self) -> list[dict]:
        """Every book in the user's Readwise Books library."""
        return self._get_all("books")

    def list_sessions(self) -> list[dict]:
        """Every reading session (not deleted)."""
        return self._get_all("sessions")

    def _get_all(self, path: str) -> list[dict]:
        url = BASE_URL + path + "?" + urllib.parse.urlencode({"limit": PAGE_SIZE})
        results = []
        while url:
            page = self._get(url)
            results += page.get("results", [])
            url = page.get("next")
        return results

    def _get(self, url: str) -> dict:
        if not url.startswith(BASE_URL):
            raise ReadwiseError(f"Refusing to follow a link outside Readwise: {url}")
        for _ in range(MAX_RETRIES + 1):
            try:
                return json.loads(self._request("GET", url))
            except RateLimited as limited:
                self.sleep(limited.delay)
        raise ReadwiseError("Readwise kept rate-limiting the request; try again later.")


def retry_after(headers: dict, default: float) -> float:
    """Seconds to wait from a 429's Retry-After; default if absent or a date."""
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                return max(1.0, float(value))
            except (TypeError, ValueError):
                break
    return default


def error_message(status: int, body: bytes) -> str:
    """A user-facing message for a failed request, with Readwise's detail."""
    if status in (401, 403):
        return "Readwise rejected the API token. Check it under Set Readwise token."
    try:
        details = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        details = body.decode("utf-8", "replace").strip()
    if isinstance(details, dict):
        error = details.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("code")
        details = details.get("detail") or error or details
    if isinstance(details, (dict, list)):
        details = json.dumps(details, ensure_ascii=False)
    detail = str(details).strip()[:500]
    return f"Readwise error {status}" + (f": {detail}" if detail else ".")

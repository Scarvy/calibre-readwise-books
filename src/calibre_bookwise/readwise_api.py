"""Read-only client for the Readwise Books API.

Deliberately GET-only: there is no method here that can create, change or
delete anything in the user's Readwise account.

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


Fetch = Callable[[urllib.request.Request], tuple[int, dict, bytes]]


def _urlopen(request: urllib.request.Request) -> tuple[int, dict, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


class ReadOnlyClient:
    def __init__(
        self,
        token: str,
        fetch: Fetch = _urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.token = token.strip()
        self.fetch = fetch
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
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Token {self.token}",
                "Accept": "application/json",
            },
        )
        for _ in range(MAX_RETRIES + 1):
            try:
                status, headers, body = self.fetch(request)
            except OSError as error:
                raise ReadwiseError(f"Couldn't reach Readwise: {error}") from error
            if status == 429:
                self.sleep(float(headers.get("Retry-After", 30)))
                continue
            if status == 200:
                return json.loads(body)
            raise ReadwiseError(_error_message(status, body))
        raise ReadwiseError("Readwise kept rate-limiting the request; try again later.")


def _error_message(status: int, body: bytes) -> str:
    if status in (401, 403):
        return "Readwise rejected the API token. Check it under Set Readwise token."
    try:
        error = json.loads(body).get("error", {})
        return f"Readwise error {status}: {error.get('message') or error.get('code')}"
    except (ValueError, AttributeError):
        return f"Readwise error {status}"

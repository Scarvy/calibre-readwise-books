"""Definite Calibre-to-Readwise links, isolated by library and API token.

The former unscoped book_map.json is left untouched. Its numeric Calibre IDs
cannot safely be assigned to a library or Readwise account after the fact.
"""

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path

from .matching import Match
from .storage import atomic_write_json

FILE_NAME = "book_map_scoped.json"
DEFINITE = ("isbn", "asin")
_lock = threading.Lock()  # metadata sync and session sync can both write


def book_map_scope(library_id: str, token: str) -> str:
    """Opaque key for one Calibre library and one Readwise credential."""
    identity = json.dumps([str(library_id), token.strip()])
    return hashlib.sha256(identity.encode()).hexdigest()


def _read_all(data_dir: Path) -> dict[str, dict[str, int]]:
    path = data_dir / FILE_NAME
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read {FILE_NAME}; restore it from backup.") from error
    if not isinstance(saved, dict) or any(
        not isinstance(scope, str)
        or not isinstance(links, dict)
        or any(
            not str(book_id).isdigit()
            or not isinstance(remote_id, int)
            or isinstance(remote_id, bool)
            for book_id, remote_id in links.items()
        )
        for scope, links in saved.items()
    ):
        raise ValueError(f"Invalid {FILE_NAME}; restore it from backup.")
    return saved


def read_book_map(data_dir: Path, scope: str) -> dict[int, int]:
    return {
        int(book_id): remote_id
        for book_id, remote_id in _read_all(data_dir).get(scope, {}).items()
    }


def definite_links(matches: Iterable[Match]) -> dict[int, int]:
    return {
        match.book.calibre_id: match.bookwise_id
        for match in matches
        if match.matched_by in DEFINITE and match.bookwise_id is not None
    }


def remember(data_dir: Path, scope: str, links: Mapping[int, int]):
    """Add definite links for this library and Readwise credential."""
    if not links:
        return
    with _lock:
        saved = _read_all(data_dir)
        current = saved.get(scope, {}) | {
            str(book_id): remote_id for book_id, remote_id in links.items()
        }
        saved[scope] = current
        atomic_write_json(data_dir / FILE_NAME, saved, indent=2, sort_keys=True)

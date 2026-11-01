"""Which Readwise Books book each Calibre book is, shared by every feature.

book_map.json maps Calibre book id -> Readwise Books id. Only definite links
are saved: ISBN and ASIN matches, and books this plugin created. Title
matches are checked again each time, since a better match may appear.
Stdlib only: this has to run inside Calibre's bundled Python 3.11.
"""

import json
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path

from .matching import Match
from .storage import atomic_write_json

FILE_NAME = "book_map.json"
DEFINITE = ("isbn", "asin")  # Match.matched_by values worth remembering
# Metadata sync (GUI thread) and session sync (poll thread) both write.
_lock = threading.Lock()


def read_book_map(data_dir: Path) -> dict[int, int]:
    try:
        saved = json.loads((data_dir / FILE_NAME).read_text())
        return {
            int(calibre_id): bookwise_id for calibre_id, bookwise_id in saved.items()
        }
    except (OSError, ValueError, AttributeError):
        return {}


def definite_links(matches: Iterable[Match]) -> dict[int, int]:
    return {
        match.book.calibre_id: match.bookwise_id
        for match in matches
        if match.matched_by in DEFINITE and match.bookwise_id is not None
    }


def remember(data_dir: Path, links: Mapping[int, int]):
    """Add these Calibre id -> Readwise id links to the saved map."""
    if not links:
        return
    with _lock:
        saved = read_book_map(data_dir) | dict(links)
        atomic_write_json(data_dir / FILE_NAME, saved, indent=2, sort_keys=True)

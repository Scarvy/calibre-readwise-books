"""Sync Readwise metadata: fill Readwise custom columns in the Calibre library.

Read-only towards Readwise (ReadOnlyClient has only GET methods); the only
writes are to this Calibre library's custom columns. The first run offers to
create the columns, which Calibre needs a restart to show.
"""

import json
from pathlib import Path

from calibre.gui2 import error_dialog, info_dialog, question_dialog
from calibre.utils.config import JSONConfig
from calibre_plugins.readwise_books.matching import CalibreBook, plan_matches
from calibre_plugins.readwise_books.metadata import (
    COLUMNS,
    NOT_IN_READWISE,
    column_updates,
)
from calibre_plugins.readwise_books.progress import viewer_position_at_progress
from calibre_plugins.readwise_books.readwise_api import ReadOnlyClient, ReadwiseError
from qt.core import QInputDialog, QLineEdit, QThread, pyqtSignal

prefs = JSONConfig("plugins/readwise_books")  # holds the Readwise API token


class SyncWorker(QThread):
    """Fetches Readwise data and works out column values off the GUI thread."""

    finished_sync = pyqtSignal(object, object)  # (matches, updates) or (None, error)

    def __init__(
        self, parent, token: str, books: list[CalibreBook], saved: dict[int, int]
    ):
        super().__init__(parent)
        self.token, self.books, self.saved = token, books, saved

    def run(self):
        try:
            client = ReadOnlyClient(self.token)
            readwise_books = client.list_books()
            sessions = client.list_sessions()
            matches = plan_matches(self.books, readwise_books, self.saved)
            self.finished_sync.emit(matches, column_updates(matches, sessions))
        except ReadwiseError as error:
            self.finished_sync.emit(None, str(error))


class MetadataSync:
    def __init__(self, gui, data_dir: Path):
        self.gui = gui
        self.book_map_path = data_dir / "book_map.json"
        self.worker = None

    def set_token(self) -> bool:
        token, ok = QInputDialog.getText(
            self.gui,
            "Readwise API token",
            "Paste your Readwise access token (readwise.io/access_token).\n"
            "Used for metadata reads and optional session, highlight, and note uploads.",
            QLineEdit.EchoMode.Password,
            prefs.get("token", ""),
        )
        if ok and token.strip():
            prefs["token"] = token.strip()
            return True
        return False

    def sync(self):
        if self.worker is not None and self.worker.isRunning():
            return
        if not prefs.get("token") and not self.set_token():
            return
        if not self.ensure_columns():
            return
        db = self.gui.current_db.new_api
        books = [_calibre_book(db, book_id) for book_id in db.all_book_ids()]
        self.worker = SyncWorker(self.gui, prefs["token"], books, self._saved())
        self.worker.finished_sync.connect(self.apply)
        self.gui.status_bar.show_message("Readwise Books: syncing metadata…", 0)
        self.worker.start()

    def ensure_columns(self) -> bool:
        """True when every column exists; otherwise offer to create them."""
        db = self.gui.current_db
        existing = set(db.custom_column_label_map)
        missing = [c for c in COLUMNS if c.label not in existing]
        if not missing:
            return True
        names = "\n".join(f"  • {c.name}  (#{c.label})" for c in missing)
        if not question_dialog(
            self.gui,
            "Create Readwise columns?",
            f"Readwise Books will add {len(missing)} custom columns to this library:"
            f"\n\n{names}\n\nCalibre needs a restart before they appear. "
            "After restarting, run Sync library metadata from Readwise again.",
        ):
            return False
        for column in missing:
            db.create_custom_column(
                column.label,
                column.name,
                column.datatype,
                False,
                display=column.display,
            )
        if question_dialog(
            self.gui,
            "Restart Calibre?",
            "The columns are created. Restart Calibre now?",
        ):
            self.gui.quit(restart=True)
        return False

    def apply(self, matches, updates):
        self.gui.status_bar.clear_message()
        if matches is None:
            error_dialog(self.gui, "Readwise sync failed", updates, show=True)
            return
        db = self.gui.current_db.new_api
        selected = self.gui.current_view().get_selected_ids()
        selected_id = next(iter(selected)) if len(selected) == 1 else None
        previous_progress = (
            db.field_for("#rw_progress", selected_id)
            if selected_id is not None
            else None
        )
        for label, values in updates.items():
            db.set_field(f"#{label}", values)
        self.gui.library_view.model().refresh_ids(list(updates["rw_status"]))
        self._save(matches)
        in_readwise = sum(
            1 for v in updates["rw_status"].values() if v != NOT_IN_READWISE
        )
        summary = (
            f"Updated {len(updates['rw_status'])} books: {in_readwise} in Readwise "
            f"Books, {len(updates['rw_status']) - in_readwise} not. "
            "Nothing was changed in Readwise."
        )
        current_progress = updates["rw_progress"].get(selected_id)
        if (
            selected_id is not None
            and current_progress is not None
            and (
                previous_progress is None
                or abs(current_progress - previous_progress) >= 1
            )
            and self.offer_readwise_position(selected_id, current_progress, summary)
        ):
            return
        info_dialog(self.gui, "Readwise metadata synced", summary, show=True)

    def open_selected_at_readwise_progress(self):
        """Offer the saved Readwise location for one selected Calibre book."""
        selected = self.gui.current_view().get_selected_ids()
        if len(selected) != 1:
            info_dialog(
                self.gui,
                "Select one book",
                "Select one book in the library first.",
                show=True,
            )
            return
        book_id = next(iter(selected))
        progress = self.gui.current_db.new_api.field_for("#rw_progress", book_id)
        if progress is None:
            info_dialog(
                self.gui,
                "No Readwise progress",
                "Sync library metadata from Readwise to get this book's progress.",
                show=True,
            )
            return
        self.offer_readwise_position(book_id, progress)

    def offer_readwise_position(
        self, book_id: int, progress: float, summary: str = ""
    ) -> bool:
        """Return whether the offer was shown; never change position on decline."""
        db = self.gui.current_db.new_api
        path = db.format_abspath(book_id, "EPUB")
        position = viewer_position_at_progress(path, progress) if path else None
        if position is None:
            if not summary:
                info_dialog(
                    self.gui,
                    "Position unavailable",
                    "An EPUB copy is needed to open this Readwise progress position.",
                    show=True,
                )
            return False
        title = db.field_for("title", book_id)
        message = (
            f"{summary}\n\n" if summary else ""
        ) + f"Readwise shows “{title}” at {progress:g}%. Open the EPUB near that point?"
        if question_dialog(self.gui, "Open at Readwise progress?", message):
            self.gui.iactions["View"].view_format_by_id(
                book_id, "EPUB", open_at=position
            )
        return True

    def _saved(self) -> dict[int, int]:
        try:
            return {
                int(k): v for k, v in json.loads(self.book_map_path.read_text()).items()
            }
        except (OSError, ValueError):
            return {}

    def _save(self, matches):
        """Remember ISBN/ASIN matches; title matches are re-checked each sync."""
        saved = self._saved()
        for match in matches:
            if match.matched_by in ("isbn", "asin"):
                saved[match.book.calibre_id] = match.bookwise_id
        self.book_map_path.parent.mkdir(parents=True, exist_ok=True)
        self.book_map_path.write_text(json.dumps(saved, indent=2, sort_keys=True))


def _calibre_book(db, book_id: int) -> CalibreBook:
    identifiers = db.field_for("identifiers", book_id) or {}
    return CalibreBook(
        calibre_id=book_id,
        title=db.field_for("title", book_id),
        authors=tuple(db.field_for("authors", book_id) or ()),
        isbn=identifiers.get("isbn"),
        asin=identifiers.get("amazon") or identifiers.get("mobi-asin"),
    )

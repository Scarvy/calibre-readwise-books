"""Readwise Books toolbar action: runs the session tracker inside Calibre.

Polls the e-book viewer's saved positions every few seconds (see live.py)
and shows recorded sessions with the Bookwise payload each would send.
Reading sessions stay local. Highlight uploads are separately opt-in.
"""

import hashlib
import json
import threading
import traceback
from pathlib import Path

from calibre.constants import cache_dir, config_dir
from calibre.gui2 import error_dialog, info_dialog, open_local_file
from calibre.gui2.actions import InterfaceAction
from calibre_plugins.readwise_books.bookwise import (
    local_timezone,
    session_item,
    skip_reason,
)
from calibre_plugins.readwise_books.highlights import HighlightClient, HighlightSync
from calibre_plugins.readwise_books.live import (
    Tracker,
    macos_viewer_state,
    session_from_record,
)
from calibre_plugins.readwise_books.metadata_sync import MetadataSync, prefs
from calibre_plugins.readwise_books.progress import book_progress
from qt.core import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTimer,
    QVBoxLayout,
)

POLL_MS = 2000
ICON = "images/readwise-books.png"
DATA_DIR = Path(config_dir) / "plugins" / "readwise_books"
# Where sessions were kept while the plugin was called Bookwise Sessions.
OLD_DATA_DIR = Path(config_dir) / "plugins" / "bookwise_sessions"
ANNOTS_DIR = Path(config_dir) / "viewer" / "annots"
VIEWER_CACHE = Path(cache_dir()) / "ev2"


class ReadwiseBooksAction(InterfaceAction):
    name = "Readwise Books"
    action_spec = (
        "Readwise Books",
        None,  # icon set in genesis from the plugin's own images/
        "Reading sessions for Readwise Books",
        None,
    )
    action_type = "current"
    popup_type = 1  # QToolButton.ToolButtonPopupMode.MenuButtonPopup

    def genesis(self):
        self.highlights = None
        self.tracker = None
        self.books: dict[str, dict] = {}
        self.dialog = None
        self.poll_failed = False
        self.polling = False
        # get_icons is provided by Calibre to plugin code at runtime.
        self.qaction.setIcon(get_icons(ICON, "Readwise Books"))  # noqa: F821
        menu = QMenu(self.gui)
        menu.addAction("Show sessions", self.show_sessions)
        menu.addAction("End session now", self.end_session)
        menu.addSeparator()
        self.metadata_sync = MetadataSync(self.gui, DATA_DIR)
        menu.addAction("Sync Readwise metadata", self.metadata_sync.sync)
        menu.addAction("Set Readwise token…", self.metadata_sync.set_token)
        self.highlight_action = menu.addAction("Upload new highlights and notes")
        self.highlight_action.setCheckable(True)
        self.highlight_action.triggered.connect(self.toggle_highlights)
        menu.addAction("Show highlight sync status", self.show_highlight_status)
        menu.addSeparator()
        menu.addAction("Open data folder", lambda: open_local_file(str(DATA_DIR)))
        self.qaction.setMenu(menu)
        self.qaction.triggered.connect(self.show_sessions)
        self.timer = QTimer(self.gui)
        self.timer.timeout.connect(self.poll)

    def initialization_complete(self):
        if OLD_DATA_DIR.is_dir() and not DATA_DIR.exists():
            OLD_DATA_DIR.rename(DATA_DIR)  # keep sessions recorded before the rename
        self.index_library()
        try:
            self.highlights = HighlightSync(DATA_DIR, ANNOTS_DIR)
            self.highlight_action.setChecked(self.highlights.state["enabled"])
        except (OSError, ValueError):
            error_dialog(
                self.gui,
                "Highlight sync unavailable",
                "Could not read highlights.json. Restore the file from backup "
                "before enabling sync to avoid duplicate uploads.",
                show=True,
            )
            self.highlight_action.setEnabled(False)
        self.tracker = Tracker(
            DATA_DIR,
            ANNOTS_DIR,
            book_lookup=self.lookup_book,
            progress_of=lambda path, cfi: book_progress(path, cfi, VIEWER_CACHE),
            viewer_probe=macos_viewer_state,
        )
        self.timer.start(POLL_MS)
        self.highlight_token = prefs.get("token", "")

    def library_changed(self, db):
        self.index_library()

    def shutting_down(self):
        self.timer.stop()
        return True

    def index_library(self):
        """Map the viewer's annots key (sha256 of a book file's path) to the book."""
        db = self.gui.current_db.new_api
        books = {}
        for book_id in db.all_book_ids():
            title = db.field_for("title", book_id)
            for fmt in db.formats(book_id):
                path = db.format_abspath(book_id, fmt)
                if path:
                    key = hashlib.sha256(path.encode()).hexdigest()
                    books[key] = {
                        "book_id": book_id,
                        "title": title,
                        "path": path,
                        "authors": db.field_for("authors", book_id) or (),
                    }
        self.books = books

    def lookup_book(self, key: str) -> dict | None:
        if key not in self.books:
            self.index_library()  # a book added since the last index
        return self.books.get(key)

    def poll(self):
        """Every 2 seconds: run a tracker step off the GUI thread.

        The viewer check shells out to macOS (~150 ms), which would make the
        Calibre window stutter if it ran here.
        """
        if self.tracker is None:
            return
        self.highlight_token = prefs.get("token", "")
        if not self.polling:
            self.polling = True
            threading.Thread(target=self._poll_in_background, daemon=True).start()
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.refresh()

    def _poll_in_background(self):
        try:
            self.tracker.poll()
            if self.highlights is not None:
                client = (
                    HighlightClient(self.highlight_token)
                    if self.highlight_token
                    else None
                )
                self.highlights.poll(self.books.copy(), client)
            self.poll_failed = False
        # A tracker bug must never break the plugin: log it once and keep going.
        except Exception:  # noqa: BLE001
            if not self.poll_failed:
                traceback.print_exc()
            self.poll_failed = True
        finally:
            self.polling = False

    def toggle_highlights(self, enabled):
        if self.highlights is None:
            self.highlight_action.setChecked(False)
            return
        if enabled and not prefs.get("token") and not self.metadata_sync.set_token():
            self.highlight_action.setChecked(False)
            return
        try:
            self.highlights.set_enabled(enabled)
        except (OSError, ValueError):
            self.highlight_action.setChecked(self.highlights.state["enabled"])
            error_dialog(
                self.gui,
                "Highlight sync unavailable",
                "Could not save or baseline annotations. Try again after the viewer "
                "has finished saving.",
                show=True,
            )

    def show_highlight_status(self):
        if self.highlights is None:
            return
        with self.highlights.lock:
            items = list(self.highlights.state["items"].values())
            counts = {}
            for item in items:
                counts[item["status"]] = counts.get(item["status"], 0) + 1
            errors = sorted({item["error"] for item in items if item.get("error")})
        message = "\n".join(f"{status}: {count}" for status, count in counts.items())
        message = message or "No new highlights recorded."
        if not prefs.get("token"):
            message += "\nSet a Readwise token to upload queued highlights."
        if errors:
            message += "\n\n" + "\n".join(errors)
        info_dialog(self.gui, "Highlight sync status", message, show=True)

    def end_session(self):
        if self.tracker is not None:
            self.tracker.end_session()
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.refresh()

    def show_sessions(self):
        if self.tracker is None:
            return
        if self.dialog is None:
            self.dialog = SessionsDialog(self.gui, self)
        self.dialog.refresh()
        self.dialog.show()
        self.dialog.raise_()


class SessionsDialog(QDialog):
    COLUMNS = ("Book", "Started", "Length", "Progress", "Pages", "Status")

    def __init__(self, parent, action: ReadwiseBooksAction):
        super().__init__(parent)
        self.action = action
        self.rows: list[dict] = []
        self.setWindowTitle("Readwise Books: reading sessions")
        self.resize(820, 520)

        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.show_payload)

        self.payload = QPlainTextEdit(self)
        self.payload.setReadOnly(True)
        self.payload.setMaximumHeight(170)

        end_button = QPushButton("End session now", self)
        end_button.clicked.connect(self.action.end_session)
        folder_button = QPushButton("Open data folder", self)
        folder_button.clicked.connect(lambda: open_local_file(str(DATA_DIR)))
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close_box.rejected.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(end_button)
        buttons.addWidget(folder_button)
        buttons.addStretch()
        buttons.addWidget(close_box)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Recorded from the e-book viewer. Reading sessions stay local: "
                "the payload below is what each session would send.",
                self,
            )
        )
        layout.addWidget(self.table)
        layout.addWidget(QLabel("Bookwise payload (dry run):", self))
        layout.addWidget(self.payload)
        layout.addLayout(buttons)

    def refresh(self):
        tracker = self.action.tracker
        if tracker is None:
            return
        open_rows = [
            {"session": session, "status": "reading…"}
            for session in tracker.open_sessions()
        ]
        finished_rows = [
            {"session": session_from_record(record), "status": record["status"]}
            for record in reversed(tracker.finished_sessions())
        ]
        rows = open_rows + finished_rows
        selected = self.table.currentRow()
        self.rows = rows
        self.table.setRowCount(len(rows))
        for row, entry in enumerate(rows):
            session = entry["session"]
            status = entry["status"]
            if status == "skipped":
                status = f"skipped: {skip_reason(session)}"
            values = (
                session.title or session.book_key[:10],
                session.start.astimezone().strftime("%a %d %b %H:%M"),
                _duration(session.duration.total_seconds()),
                _progress(session.start_progress, session.end_progress),
                str(session.saves),
                status,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        if 0 <= selected < len(rows):
            self.table.selectRow(selected)
        self.show_payload()

    def show_payload(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self.rows):
            self.payload.setPlainText("Select a session to see its payload.")
            return
        session = self.rows[row]["session"]
        if reason := skip_reason(session):
            self.payload.setPlainText(f"Not sent: {reason}.")
            return
        # Book matching isn't wired in yet, so the Bookwise id is a placeholder.
        item = session_item(session, 0, local_timezone())
        item["book_id"] = f"<Bookwise id for Calibre book {session.book_id}>"
        self.payload.setPlainText(json.dumps({"items": [item]}, indent=2))


def _duration(seconds: float) -> str:
    minutes, seconds = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    )


def _progress(start: float | None, end: float | None) -> str:
    if start is None or end is None:
        return "?"
    return f"{start:.1%} → {end:.1%}"

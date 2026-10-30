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
from typing import Any

from calibre.constants import cache_dir, config_dir
from calibre.gui2 import error_dialog, info_dialog, open_local_file, question_dialog
from calibre.gui2.actions import InterfaceAction
from calibre_plugins.readwise_books.bookwise import (
    local_timezone,
    session_item,
    skip_reason,
)
from calibre_plugins.readwise_books.highlights import (
    HighlightClient,
    HighlightSync,
    preview_highlights,
)
from calibre_plugins.readwise_books.live import (
    Tracker,
    macos_viewer_state,
    session_from_record,
)
from calibre_plugins.readwise_books.metadata_sync import MetadataSync, prefs
from calibre_plugins.readwise_books.progress import book_progress
from calibre_plugins.readwise_books.session_sync import (
    BooksClient,
    SessionSync,
    record_key,
)
from qt.core import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QIcon,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTableWidgetSelectionRange,
    QTabWidget,
    QTimer,
    QVBoxLayout,
    QWidget,
)

POLL_MS = 2000
PREVIEW_POLL_MS = 500
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
    popup_type = 2  # QToolButton.ToolButtonPopupMode.InstantPopup

    def genesis(self):
        self.highlights = None
        self.session_sync = None
        self.tracker = None
        self.books: dict[str, dict] = {}
        self.dialog = None
        self.poll_failed = False
        self.polling = False
        # get_icons is provided by Calibre to plugin code at runtime.
        self.qaction.setIcon(get_icons(ICON, "Readwise Books"))  # noqa: F821
        menu = QMenu(self.gui)
        end_action = menu.addAction("End session now", self.end_session)
        end_action.setIcon(QIcon.ic("window-close.png"))
        sync_menu = menu.addMenu("Sync settings")
        sync_menu.setIcon(QIcon.ic("sync.png"))
        self.session_send_action = sync_menu.addAction("Sync reading sessions")
        self.session_send_action.setIcon(QIcon.ic("book.png"))
        self.session_send_action.setCheckable(True)
        self.session_send_action.triggered.connect(self.toggle_session_sending)
        self.feed_action = sync_menu.addAction("Post to feed")
        self.feed_action.setIcon(QIcon.ic("send.png"))
        self.feed_action.setCheckable(True)
        self.feed_action.triggered.connect(self.toggle_feed_posting)
        self.highlight_action = sync_menu.addAction("Sync highlights and notes")
        self.highlight_action.setIcon(QIcon.ic("highlight.png"))
        self.highlight_action.setCheckable(True)
        self.highlight_action.triggered.connect(self.toggle_highlights)
        self.metadata_sync = MetadataSync(self.gui, DATA_DIR)
        metadata_action = menu.addAction(
            "Sync library metadata from Readwise", self.metadata_sync.sync
        )
        metadata_action.setIcon(QIcon.ic("download-metadata.png"))
        position_action = menu.addAction(
            "Open selected book at Readwise progress…",
            self.metadata_sync.open_selected_at_readwise_progress,
        )
        position_action.setIcon(QIcon.ic("view.png"))
        menu.addSeparator()
        settings_menu = menu.addMenu("Settings")
        settings_menu.setIcon(QIcon.ic("gear.png"))
        token_action = settings_menu.addAction(
            "Set Readwise token…", self.metadata_sync.set_token
        )
        token_action.setIcon(QIcon.ic("drm-locked.png"))
        developer_menu = settings_menu.addMenu("Developer")
        developer_menu.setIcon(QIcon.ic("debug.png"))
        open_action = developer_menu.addAction("Open sync window", self.show_sessions)
        open_action.setIcon(QIcon.ic("sync.png"))
        status_action = developer_menu.addAction(
            "Show highlight upload status", self.show_highlight_status
        )
        status_action.setIcon(QIcon.ic("dialog_information.png"))
        folder_action = developer_menu.addAction(
            "Open data folder", lambda: open_local_file(str(DATA_DIR))
        )
        folder_action.setIcon(QIcon.ic("tb_folder.png"))
        self.qaction.setMenu(menu)
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
        try:
            self.session_sync = SessionSync(DATA_DIR, self.tracker.finished_sessions())
            self.session_send_action.setChecked(
                self.session_sync.state["mode"] == "send"
            )
            self.feed_action.setChecked(
                self.session_sync.state.get("publish_to_feed", False)
            )
        except (OSError, ValueError):
            self.session_send_action.setEnabled(False)
            self.feed_action.setEnabled(False)
            error_dialog(
                self.gui,
                "Session upload unavailable",
                "Could not read session_uploads.json. Restore it from backup before sending sessions.",
                show=True,
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
                        "isbn": (db.field_for("identifiers", book_id) or {}).get(
                            "isbn"
                        ),
                        "asin": (db.field_for("identifiers", book_id) or {}).get(
                            "amazon"
                        )
                        or (db.field_for("identifiers", book_id) or {}).get(
                            "mobi-asin"
                        ),
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
            self.dialog.refresh_current()

    def _poll_in_background(self):
        try:
            self.tracker.poll()
            if self.session_sync is not None:
                client = (
                    BooksClient(self.highlight_token) if self.highlight_token else None
                )
                self.session_sync.poll(
                    self.tracker.finished_sessions(), self.books.copy(), client
                )
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

    def toggle_session_sending(self, enabled):
        if self.session_sync is None or self.tracker is None:
            self.session_send_action.setChecked(False)
            return
        if enabled and not prefs.get("token") and not self.metadata_sync.set_token():
            self.session_send_action.setChecked(False)
            return
        try:
            self.session_sync.set_mode(
                "send" if enabled else "dry_run", self.tracker.finished_sessions()
            )
        except (OSError, ValueError):
            self.session_send_action.setChecked(
                self.session_sync.state["mode"] == "send"
            )
            error_dialog(
                self.gui,
                "Session mode unavailable",
                "Could not save the session mode.",
                show=True,
            )
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.sessions.refresh()

    def toggle_feed_posting(self, enabled):
        if self.session_sync is None:
            self.feed_action.setChecked(False)
            return
        try:
            self.session_sync.set_publish_to_feed(enabled)
        except (OSError, ValueError):
            error_dialog(
                self.gui,
                "Feed preference unavailable",
                "Could not save the feed posting preference.",
                show=True,
            )
        self.feed_action.setChecked(
            self.session_sync.state.get("publish_to_feed", False)
        )
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.sessions.refresh()

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
            self.dialog.sessions.refresh()

    def show_sessions(self):
        if self.tracker is None:
            return
        if self.dialog is None:
            self.dialog = SyncDialog(self.gui, self)
        self.dialog.refresh_current()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.start_refresh()


class SyncDialog(QDialog):
    """One window for reading sessions and highlight delivery."""

    def __init__(self, parent, action: ReadwiseBooksAction):
        super().__init__(parent)
        self.setWindowTitle("Readwise Books: sync")
        self.resize(960, 680)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.sessions = SessionsPanel(self.tabs, action)
        self.highlights = HighlightsPanel(self.tabs, action)
        self.tabs.addTab(self.sessions, "Sessions")
        self.tabs.addTab(self.highlights, "Highlights")
        self.tabs.currentChanged.connect(lambda _index: self.refresh_current())
        layout.addWidget(self.tabs)
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close_box.rejected.connect(self.close)
        layout.addWidget(close_box)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_highlights_if_visible)

    def start_refresh(self):
        self.refresh_timer.start(PREVIEW_POLL_MS)

    def refresh_current(self):
        if self.tabs.currentIndex() == 0:
            self.sessions.refresh()
        else:
            self.highlights.refresh()

    def refresh_highlights_if_visible(self):
        if not self.isVisible():
            self.refresh_timer.stop()
        elif self.tabs.currentIndex() == 1:
            self.highlights.refresh()


class HighlightsPanel(QWidget):
    COLUMNS = ("Book", "Highlight", "Note", "Saved", "Status")

    def __init__(self, parent, action: ReadwiseBooksAction):
        super().__init__(parent)
        self.action = action
        self.rows: list[dict] = []
        self.signature = None
        layout = QVBoxLayout(self)
        self.mode_label = QLabel(self)
        layout.addWidget(self.mode_label)
        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.show_payload)
        layout.addWidget(self.table)
        layout.addWidget(QLabel("Readwise request payload:", self))
        self.payload = QPlainTextEdit(self)
        self.payload.setReadOnly(True)
        self.payload.setMaximumHeight(180)
        layout.addWidget(self.payload)
        self.errors_label = QLabel(self)
        layout.addWidget(self.errors_label)
        self.send_button = QPushButton("Send selected local highlights", self)
        self.send_button.clicked.connect(self.send_selected)
        self.send_button.setEnabled(False)
        self.send_all_button = QPushButton("Send all local highlights", self)
        self.send_all_button.clicked.connect(self.send_all)
        self.send_all_button.setEnabled(False)
        self.retry_button = QPushButton("Retry rejected", self)
        self.retry_button.clicked.connect(self.retry_selected)
        self.retry_button.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.send_all_button)
        buttons.addWidget(self.retry_button)
        buttons.addStretch()
        layout.addLayout(buttons)

    def refresh(self):
        sync = self.action.highlights
        previews, errors = preview_highlights(ANNOTS_DIR, self.action.books.copy())
        for preview in previews:
            preview["status"] = sync.status(preview["key"]) if sync else "unavailable"
            preview["explanation"] = (
                sync.explanation(preview["key"], bool(prefs.get("token")))
                if sync
                else "Highlight sync state is unavailable."
            )
            preview["display_status"] = preview["status"]
            if preview["status"] == "pending":
                if sync and sync.entry(preview["key"]).get("error"):
                    preview["display_status"] = "pending · retry"
                elif "paused" in preview["explanation"]:
                    preview["display_status"] = "pending · paused"
                elif "token" in preview["explanation"]:
                    preview["display_status"] = "pending · token needed"
                elif "finish saving" in preview["explanation"]:
                    preview["display_status"] = "pending · settling"
        signature = [
            (
                p["key"],
                p["text"],
                p["note"],
                p["timestamp"],
                p["status"],
                p["display_status"],
                p["explanation"],
            )
            for p in previews
        ]
        self.mode_label.setText(
            "Automatic upload: on. Older local highlights can be sent below."
            if sync is not None and sync.state["enabled"]
            else "Automatic upload: off. Selected highlights can still be sent."
        )
        self.errors_label.setText("; ".join(errors[:3]))
        if signature == self.signature:
            return
        selected_keys = {
            self.rows[index.row()]["key"]
            for index in self.table.selectionModel().selectedRows()
            if 0 <= index.row() < len(self.rows)
        }
        self.signature = signature
        self.rows = previews
        self.table.blockSignals(True)
        self.table.setRowCount(len(previews))
        for row, preview in enumerate(previews):
            values = (
                preview["book"],
                preview["text"][:180],
                preview["note"][:100],
                preview["timestamp"] or "unknown",
                preview["display_status"],
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.clearSelection()
        selected_rows = [
            row
            for row, preview in enumerate(previews)
            if preview["key"] is not None and preview["key"] in selected_keys
        ]
        if not selected_rows and previews:
            selected_rows = [0]
        if selected_rows:
            self.table.setCurrentCell(selected_rows[0], 0)
            for row in selected_rows:
                self.table.setRangeSelected(
                    QTableWidgetSelectionRange(row, 0, row, len(self.COLUMNS) - 1), True
                )
        self.table.blockSignals(False)
        self.show_payload()

    def selected_rows(self) -> list[dict]:
        return [
            self.rows[index.row()]
            for index in self.table.selectionModel().selectedRows()
            if 0 <= index.row() < len(self.rows)
        ]

    def local_keys(self, rows: list[dict]) -> list[str]:
        return [p["key"] for p in rows if p["key"] and p["status"] == "local"]

    def show_payload(self):
        selected = self.selected_rows()
        selected_keys = self.local_keys(selected)
        rejected_keys = [
            p["key"] for p in selected if p["key"] and p["status"] == "rejected"
        ]
        all_keys = self.local_keys(self.rows)
        self.send_button.setEnabled(bool(selected_keys))
        self.send_all_button.setEnabled(bool(all_keys))
        self.retry_button.setEnabled(bool(rejected_keys))
        self.send_button.setText(
            f"Send {len(selected_keys)} selected local highlight{'s' if len(selected_keys) != 1 else ''}"
            if selected_keys
            else "Send selected local highlights"
        )
        self.send_all_button.setText(
            f"Send all {len(all_keys)} local highlight{'s' if len(all_keys) != 1 else ''}"
            if all_keys
            else "Send all local highlights"
        )
        row = self.table.currentRow()
        selected_numbers = {
            index.row() for index in self.table.selectionModel().selectedRows()
        }
        if row not in selected_numbers and selected_numbers:
            row = min(selected_numbers)
        if not selected or not 0 <= row < len(self.rows):
            self.payload.setPlainText("Select a highlight to see its payload.")
            return
        preview = self.rows[row]
        content = (
            f"Status: {preview['status']}\n{preview['explanation']}\n\n"
            f"{preview['method']} {preview['url']}\n\n"
        )
        content += json.dumps(preview["body"], indent=2, ensure_ascii=False)
        if content != self.payload.toPlainText():
            self.payload.setPlainText(content)

    def send_selected(self):
        self.send_keys(self.local_keys(self.selected_rows()))

    def retry_selected(self):
        keys = [
            p["key"]
            for p in self.selected_rows()
            if p["key"] and p["status"] == "rejected"
        ]
        self.send_keys(keys, retry=True)

    def send_all(self):
        keys = self.local_keys(self.rows)
        if not keys:
            return
        if not question_dialog(
            self,
            "Send saved highlights?",
            f"Send {len(keys)} local highlights and their notes to Readwise?",
        ):
            return
        self.send_keys(keys)

    def send_keys(self, keys: list[str], retry: bool = False):
        sync = self.action.highlights
        if sync is None or not keys:
            return
        if not prefs.get("token") and not self.action.metadata_sync.set_token():
            return
        try:
            if retry:
                sync.retry_rejected(keys, self.action.books.copy())
            else:
                sync.queue_saved(keys, self.action.books.copy())
        except (OSError, ValueError) as error:
            error_dialog(self, "Highlight upload unavailable", str(error), show=True)
            return
        self.refresh()
        self.action.poll()


class SessionsPanel(QWidget):
    COLUMNS = ("Book", "Started", "Length", "Progress", "Pages", "Status")

    def __init__(self, parent, action: ReadwiseBooksAction):
        super().__init__(parent)
        self.action = action
        self.rows: list[dict] = []

        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        self.send_button = QPushButton("Send selected local sessions", self)
        self.send_button.clicked.connect(self.send_selected)
        self.send_button.setEnabled(False)
        self.send_all_button = QPushButton("Send all local sessions", self)
        self.send_all_button.clicked.connect(self.send_all)
        self.send_all_button.setEnabled(False)
        retry_button = QPushButton("Retry selected after review", self)
        retry_button.clicked.connect(self.retry_selected)
        folder_button = QPushButton("Open data folder", self)
        folder_button.clicked.connect(lambda: open_local_file(str(DATA_DIR)))
        send_buttons = QHBoxLayout()
        send_buttons.addWidget(self.send_button)
        send_buttons.addWidget(self.send_all_button)
        send_buttons.addStretch()
        buttons = QHBoxLayout()
        buttons.addWidget(end_button)
        buttons.addWidget(retry_button)
        buttons.addWidget(folder_button)
        buttons.addStretch()

        layout = QVBoxLayout(self)
        self.mode_label = QLabel(self)
        layout.addWidget(self.mode_label)
        self.feed_checkbox = QCheckBox("Post to feed", self)
        self.feed_checkbox.toggled.connect(self.action.toggle_feed_posting)
        layout.addWidget(self.feed_checkbox)
        layout.addWidget(self.table)
        layout.addWidget(QLabel("Bookwise session payload:", self))
        layout.addWidget(self.payload)
        layout.addLayout(send_buttons)
        layout.addLayout(buttons)

    def refresh(self):
        tracker = self.action.tracker
        if tracker is None:
            return
        sync = self.action.session_sync
        sending = sync is not None and sync.state["mode"] == "send"
        self.feed_checkbox.blockSignals(True)
        self.feed_checkbox.setChecked(
            sync is not None and sync.state.get("publish_to_feed", False)
        )
        self.feed_checkbox.setEnabled(sync is not None)
        self.feed_checkbox.blockSignals(False)
        self.mode_label.setText(
            "Mode: Send newly finished sessions to Readwise."
            if sending
            else "Mode: Dry run. Sessions stay local unless sent manually."
        )
        open_rows = [
            {"session": session, "status": "reading…"}
            for session in tracker.open_sessions()
        ]
        finished_rows = [
            {
                "session": session_from_record(record),
                "record": record,
                "status": sync.status(record)["status"] if sync else record["status"],
                "error": sync.status(record).get("error", "") if sync else "",
            }
            for record in reversed(tracker.finished_sessions())
        ]
        rows: list[dict[str, Any]] = open_rows + finished_rows
        selected_keys = {
            record_key(self.rows[index.row()]["record"])
            for index in self.table.selectionModel().selectedRows()
            if 0 <= index.row() < len(self.rows)
            and self.rows[index.row()].get("record") is not None
        }
        self.rows = rows
        self.table.blockSignals(True)
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
        self.table.clearSelection()
        selected_rows = [
            row
            for row, entry in enumerate(rows)
            if entry.get("record") is not None
            and record_key(entry["record"]) in selected_keys
        ]
        if selected_rows:
            self.table.setCurrentCell(selected_rows[0], 0)
            for row in selected_rows:
                self.table.setRangeSelected(
                    QTableWidgetSelectionRange(row, 0, row, len(self.COLUMNS) - 1),
                    True,
                )
        self.table.blockSignals(False)
        self.show_payload()

    def local_records(self, rows: list[dict]) -> list[dict]:
        sync = self.action.session_sync
        if sync is None:
            return []
        return [
            record
            for entry in rows
            if (record := entry.get("record")) is not None
            and sync.status(record)["status"] == "local"
            and entry["session"].book_key in self.action.books
            and skip_reason(entry["session"]) is None
        ]

    def selected_entries(self) -> list[dict]:
        return [
            self.rows[index.row()]
            for index in self.table.selectionModel().selectedRows()
            if 0 <= index.row() < len(self.rows)
        ]

    def show_payload(self):
        selected = self.selected_entries()
        local_selected = self.local_records(selected)
        local_all = self.local_records(self.rows)
        self.send_button.setEnabled(bool(local_selected))
        self.send_all_button.setEnabled(bool(local_all))
        self.send_button.setText(
            f"Send {len(local_selected)} selected local session{'s' if len(local_selected) != 1 else ''}"
            if local_selected
            else "Send selected local sessions"
        )
        self.send_all_button.setText(
            f"Send all {len(local_all)} local session{'s' if len(local_all) != 1 else ''}"
            if local_all
            else "Send all local sessions"
        )
        row = self.table.currentRow()
        selected_row_numbers = {
            index.row() for index in self.table.selectionModel().selectedRows()
        }
        if row not in selected_row_numbers and selected_row_numbers:
            row = min(selected_row_numbers)
        if not selected or not 0 <= row < len(self.rows):
            self.payload.setPlainText("Select a session to see its payload.")
            return
        session = self.rows[row]["session"]
        sync = self.action.session_sync
        error = self.rows[row].get("error")
        if reason := skip_reason(session):
            self.payload.setPlainText(f"Not sent: {reason}.")
            return
        # A local session has no resolved Readwise book id yet.
        item = session_item(session, 0, local_timezone())
        bookwise_id = sync.state["book_ids"].get(session.book_key) if sync else None
        item["book_id"] = (
            bookwise_id or f"<Bookwise id for Calibre book {session.book_id}>"
        )
        record = self.rows[row].get("record")
        item["publish_to_feed"] = (
            sync.publish_to_feed(record)
            if sync is not None and record is not None
            else (sync.state.get("publish_to_feed", False) if sync else False)
        )
        body = json.dumps({"items": [item]}, indent=2)
        self.payload.setPlainText(f"{error}\n\n{body}" if error else body)

    def send_selected(self):
        self.send_records(self.local_records(self.selected_entries()))

    def send_all(self):
        records = self.local_records(self.rows)
        if not records:
            return
        if not question_dialog(
            self,
            "Send local sessions?",
            f"Send {len(records)} eligible local session(s) from this Calibre library to Readwise Books? "
            + (
                "They will post to your feed."
                if self.action.session_sync.state.get("publish_to_feed", False)
                else "They will stay off your feed."
            ),
        ):
            return
        self.send_records(records)

    def send_records(self, records: list[dict]):
        sync = self.action.session_sync
        if sync is None or not records:
            return
        if not prefs.get("token") and not self.action.metadata_sync.set_token():
            return
        try:
            sync.queue_locals(records)
        except (OSError, ValueError) as error:
            error_dialog(self, "Session upload unavailable", str(error), show=True)
            return
        self.refresh()
        self.action.poll()

    def retry_selected(self):
        row = self.table.currentRow()
        sync = self.action.session_sync
        if sync is None or not 0 <= row < len(self.rows):
            return
        record = self.rows[row].get("record")
        if record is None or sync.status(record)["status"] != "needs review":
            return
        if not question_dialog(
            self,
            "Retry session upload?",
            "Check Readwise Books first. Its API does not detect duplicate sessions. "
            "Retry only if this session is absent there.",
        ):
            return
        try:
            sync.retry_after_review(record)
        except (OSError, ValueError) as error:
            error_dialog(self, "Retry unavailable", str(error), show=True)
        self.refresh()


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

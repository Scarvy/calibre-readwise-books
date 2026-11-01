"""Readwise Books toolbar action: runs the session tracker inside Calibre.

Polls the e-book viewer's saved positions every few seconds (see live.py)
and shows recorded sessions with the Bookwise payload each would send.
Reading sessions stay local. Highlight uploads are separately opt-in.
"""

import hashlib
import json
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from calibre.constants import cache_dir, config_dir
from calibre.gui2 import error_dialog, info_dialog, open_local_file, question_dialog
from calibre.gui2.actions import InterfaceAction
from calibre_plugins.readwise_books.bookwise import (
    SessionStatus,
    local_timezone,
    session_item,
    skip_reason,
)
from calibre_plugins.readwise_books.build_info import IS_DEV
from calibre_plugins.readwise_books.highlights import (
    Explanation,
    HighlightClient,
    HighlightStatus,
    HighlightSync,
    PreviewCache,
)
from calibre_plugins.readwise_books.live import (
    Tracker,
    macos_viewer_state,
    session_from_record,
)
from calibre_plugins.readwise_books.matching import LibraryBook, calibre_book_from_db
from calibre_plugins.readwise_books.metadata_sync import MetadataSync, prefs
from calibre_plugins.readwise_books.progress import book_progress
from calibre_plugins.readwise_books.session_sync import (
    BooksClient,
    SessionMode,
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
        "Readwise Books [DEV]" if IS_DEV else "Readwise Books",
        None,  # icon set in genesis from the plugin's own images/
        "Development build of Readwise Books"
        if IS_DEV
        else "Reading sessions for Readwise Books",
        None,
    )
    action_type = "current"
    popup_type = 2  # QToolButton.ToolButtonPopupMode.InstantPopup

    def genesis(self):
        self.highlights = None
        self.session_sync = None
        self.tracker = None
        self.books: dict[str, LibraryBook] = {}
        self.unindexed: set[str] = set()  # annots keys not in this library
        self.dialog = None
        self.poll_failed = False
        self.poll_failure_shown = False
        self.polling = False
        # get_icons is provided by Calibre to plugin code at runtime.
        self.qaction.setIcon(get_icons(ICON, "Readwise Books"))  # noqa: F821
        menu = QMenu(self.gui)
        if IS_DEV:
            build_action = menu.addAction("DEV BUILD · installed from a local build")
            build_action.setIcon(QIcon.ic("debug.png"))
            build_action.setEnabled(False)
            menu.addSeparator()
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
        channel_action = developer_menu.addAction(
            "Build: Development" if IS_DEV else "Build: Release"
        )
        channel_action.setEnabled(False)
        developer_menu.addSeparator()
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
            self.highlight_action.setChecked(self.highlights.enabled)
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
            self.session_send_action.setChecked(self.session_sync.sending)
            self.feed_action.setChecked(self.session_sync.feed_default)
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
        self.token = prefs.get("token", "")

    def library_changed(self, db):
        self.unindexed = set()
        self.index_library()

    def shutting_down(self):
        self.timer.stop()
        return True

    def index_library(self):
        """Map the viewer's annots key (sha256 of a book file's path) to the book."""
        db = self.gui.current_db.new_api
        books = {}
        for book_id in db.all_book_ids():
            book = calibre_book_from_db(db, book_id)
            for fmt in db.formats(book_id):
                path = db.format_abspath(book_id, fmt)
                if path:
                    key = hashlib.sha256(path.encode()).hexdigest()
                    books[key] = LibraryBook(**vars(book), path=path)
        self.books = books

    def lookup_book(self, key: str) -> LibraryBook | None:
        return self.books.get(key)  # runs on the poll thread: no db access

    def index_new_books(self):
        """Re-index when the viewer has a book file this index doesn't know.

        Runs on the GUI thread, which owns the library database. A key that
        is still unknown afterwards (another library's book) is not retried.
        """
        unknown = {path.stem for path in ANNOTS_DIR.glob("*.json")}
        unknown -= self.books.keys() | self.unindexed
        if unknown:
            self.index_library()
            self.unindexed |= unknown - self.books.keys()

    def poll(self):
        """Every 2 seconds: run a tracker step off the GUI thread.

        The viewer check shells out to macOS (~150 ms), which would make the
        Calibre window stutter if it ran here.
        """
        if self.tracker is None:
            return
        self.report_poll_failure()
        self.token = prefs.get("token", "")
        if not self.polling:
            self.index_new_books()  # before the tracker looks the books up
            self.polling = True
            threading.Thread(target=self._poll_in_background, daemon=True).start()
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.refresh_current()

    def report_poll_failure(self):
        """Tell the user once when tracking starts failing, not on every poll."""
        if not self.poll_failed:
            self.poll_failure_shown = False
        elif not self.poll_failure_shown:
            self.poll_failure_shown = True
            self.gui.status_bar.show_message(
                "Readwise Books: reading tracking hit an error and will keep "
                "retrying. Details are in Calibre's debug log.",
                15000,
            )

    def _poll_in_background(self):
        try:
            self.tracker.poll()
            if self.session_sync is not None:
                client = BooksClient(self.token) if self.token else None
                self.session_sync.poll(
                    self.tracker.finished_sessions(), self.books.copy(), client
                )
            if self.highlights is not None:
                client = HighlightClient(self.token) if self.token else None
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
        sync, tracker = self.session_sync, self.tracker
        self._apply_toggle(
            self.session_send_action,
            available=sync is not None and tracker is not None,
            needs_token=enabled,
            apply=lambda: sync.set_mode(
                SessionMode.SEND if enabled else SessionMode.DRY_RUN,
                tracker.finished_sessions(),
            ),
            saved=lambda: sync.sending,
            failure_title="Session mode unavailable",
            failure_message="Could not save the session mode.",
        )

    def toggle_feed_posting(self, enabled):
        sync = self.session_sync
        self._apply_toggle(
            self.feed_action,
            available=sync is not None,
            needs_token=False,
            apply=lambda: sync.set_publish_to_feed(enabled),
            saved=lambda: sync.feed_default,
            failure_title="Feed preference unavailable",
            failure_message="Could not save the feed posting preference.",
        )

    def toggle_highlights(self, enabled):
        highlights = self.highlights
        self._apply_toggle(
            self.highlight_action,
            available=highlights is not None,
            needs_token=enabled,
            apply=lambda: highlights.set_enabled(enabled),
            saved=lambda: highlights.enabled,
            failure_title="Highlight sync unavailable",
            failure_message="Could not save or baseline annotations. Try again "
            "after the viewer has finished saving.",
        )

    def _apply_toggle(
        self,
        menu_action,
        available: bool,
        needs_token: bool,
        apply: Callable[[], object],
        saved: Callable[[], bool],
        failure_title: str,
        failure_message: str,
    ):
        """Save a sync setting; afterwards the checkmark shows what was saved."""
        if not available:
            menu_action.setChecked(False)
            return
        if needs_token and not self.have_token():
            menu_action.setChecked(False)
            return
        try:
            apply()
        except (OSError, ValueError):
            error_dialog(self.gui, failure_title, failure_message, show=True)
        menu_action.setChecked(saved())
        self.refresh_sessions_window()

    def have_token(self) -> bool:
        """Whether a Readwise token is set, asking for one if not."""
        return bool(prefs.get("token")) or self.metadata_sync.set_token()

    def show_highlight_status(self):
        if self.highlights is None:
            return
        counts, errors = self.highlights.summary()
        message = "\n".join(f"{status}: {count}" for status, count in counts.items())
        message = message or "No new highlights recorded."
        if not prefs.get("token"):
            message += "\nSet a Readwise token to upload queued highlights."
        if errors:
            message += "\n\n" + "\n".join(errors)
        info_dialog(self.gui, "Highlight sync status", message, show=True)

    def refresh_sessions_window(self):
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.sessions.refresh()

    def end_session(self):
        if self.tracker is not None:
            self.tracker.end_session()
        self.refresh_sessions_window()

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


class SyncPanel(QWidget):
    """A table of rows, a preview of the selected row's request, send buttons.

    Subclasses set COLUMNS and NOUN, lay the widgets out, and say how a row
    is identified (row_key) so a refresh keeps the selection.
    """

    COLUMNS: tuple[str, ...] = ()
    STRETCH_COLUMN = 0
    NOUN = ""  # what a row is, e.g. "highlight"

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
        for column in range(len(self.COLUMNS)):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Stretch
                if column == self.STRETCH_COLUMN
                else QHeaderView.ResizeMode.ResizeToContents,
            )
        self.table.itemSelectionChanged.connect(self.show_payload)
        self.payload = QPlainTextEdit(self)
        self.payload.setReadOnly(True)
        self.payload.setMaximumHeight(180)
        self.send_button = QPushButton(self)
        self.send_button.clicked.connect(self.send_selected)
        self.send_all_button = QPushButton(self)
        self.send_all_button.clicked.connect(self.send_all)
        self.update_send_buttons(0, 0)

    def row_key(self, row: dict) -> str | None:
        raise NotImplementedError

    def show_payload(self):
        raise NotImplementedError

    def send_selected(self):
        raise NotImplementedError

    def send_all(self):
        raise NotImplementedError

    def selected_rows(self) -> list[dict]:
        return [
            self.rows[index.row()]
            for index in self.table.selectionModel().selectedRows()
            if 0 <= index.row() < len(self.rows)
        ]

    def fill_table(self, rows: list[dict], values: list[tuple], select_first: bool):
        """Show rows, keeping the selection; values are each row's cells."""
        selected_keys = {self.row_key(row) for row in self.selected_rows()} - {None}
        self.rows = rows
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for row, cells in enumerate(values):
            for column, value in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.clearSelection()
        selected = [
            number
            for number, row in enumerate(rows)
            if self.row_key(row) in selected_keys
        ]
        if not selected and select_first and rows:
            selected = [0]
        if selected:
            self.table.setCurrentCell(selected[0], 0)
            for number in selected:
                self.table.setRangeSelected(
                    QTableWidgetSelectionRange(
                        number, 0, number, len(self.COLUMNS) - 1
                    ),
                    True,
                )
        self.table.blockSignals(False)
        self.show_payload()

    def payload_row(self) -> dict | None:
        """The selected row to preview: the current one, else the first."""
        numbers = {index.row() for index in self.table.selectionModel().selectedRows()}
        row = self.table.currentRow()
        if row not in numbers and numbers:
            row = min(numbers)
        if not numbers or not 0 <= row < len(self.rows):
            return None
        return self.rows[row]

    def update_send_buttons(self, selected: int, everything: int):
        noun = self.NOUN
        self.send_button.setEnabled(bool(selected))
        self.send_all_button.setEnabled(bool(everything))
        self.send_button.setText(
            f"Send {selected} selected local {_plural(selected, noun)}"
            if selected
            else f"Send selected local {noun}s"
        )
        self.send_all_button.setText(
            f"Send all {everything} local {_plural(everything, noun)}"
            if everything
            else f"Send all local {noun}s"
        )


class HighlightsPanel(SyncPanel):
    COLUMNS = ("Book", "Highlight", "Note", "Saved", "Status")
    STRETCH_COLUMN = 1
    NOUN = "highlight"

    def __init__(self, parent, action: ReadwiseBooksAction):
        super().__init__(parent, action)
        self.previews = PreviewCache(ANNOTS_DIR)
        self.signature = None
        self.mode_label = QLabel(self)
        self.errors_label = QLabel(self)
        self.retry_button = QPushButton("Retry rejected", self)
        self.retry_button.clicked.connect(self.retry_selected)
        self.retry_button.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.send_all_button)
        buttons.addWidget(self.retry_button)
        buttons.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.mode_label)
        layout.addWidget(self.table)
        layout.addWidget(QLabel("Readwise request payload:", self))
        layout.addWidget(self.payload)
        layout.addWidget(self.errors_label)
        layout.addLayout(buttons)

    def row_key(self, row: dict) -> str | None:
        return row["key"]

    def refresh(self):
        sync = self.action.highlights
        previews, errors = self.previews.get(self.action.books)
        token = bool(prefs.get("token"))
        for preview in previews:
            if sync is None:
                preview["status"] = HighlightStatus.UNAVAILABLE
                explanation = Explanation("Highlight sync state is unavailable.")
            else:
                preview["status"] = sync.status(preview["key"])
                explanation = sync.explanation(preview["key"], token)
            preview["explanation"] = explanation.message
            preview["display_status"] = (
                f"{preview['status']} · {explanation.waiting}"
                if explanation.waiting
                else preview["status"]
            )
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
            if sync is not None and sync.enabled
            else "Automatic upload: off. Selected highlights can still be sent."
        )
        self.errors_label.setText("; ".join(errors[:3]))
        if signature == self.signature:
            return
        self.signature = signature
        values = [
            (
                p["book"],
                p["text"][:180],
                p["note"][:100],
                p["timestamp"] or "unknown",
                p["display_status"],
            )
            for p in previews
        ]
        self.fill_table(previews, values, select_first=True)

    @staticmethod
    def keys_with(rows: list[dict], status: HighlightStatus) -> list[str]:
        return [p["key"] for p in rows if p["key"] and p["status"] == status]

    def show_payload(self):
        selected = self.selected_rows()
        self.update_send_buttons(
            len(self.keys_with(selected, HighlightStatus.LOCAL)),
            len(self.keys_with(self.rows, HighlightStatus.LOCAL)),
        )
        self.retry_button.setEnabled(
            bool(self.keys_with(selected, HighlightStatus.REJECTED))
        )
        preview = self.payload_row()
        if preview is None:
            self.payload.setPlainText("Select a highlight to see its payload.")
            return
        content = (
            f"Status: {preview['status']}\n{preview['explanation']}\n\n"
            f"{preview['method']} {preview['url']}\n\n"
        )
        content += json.dumps(preview["body"], indent=2, ensure_ascii=False)
        if content != self.payload.toPlainText():
            self.payload.setPlainText(content)

    def send_selected(self):
        self.send_keys(self.keys_with(self.selected_rows(), HighlightStatus.LOCAL))

    def retry_selected(self):
        keys = self.keys_with(self.selected_rows(), HighlightStatus.REJECTED)
        self.send_keys(keys, retry=True)

    def send_all(self):
        keys = self.keys_with(self.rows, HighlightStatus.LOCAL)
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
        if not self.action.have_token():
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


class SessionsPanel(SyncPanel):
    COLUMNS = ("Book", "Started", "Length", "Progress", "Pages", "Status")
    NOUN = "session"

    def __init__(self, parent, action: ReadwiseBooksAction):
        super().__init__(parent, action)
        end_button = QPushButton("End session now", self)
        end_button.clicked.connect(self.action.end_session)
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

    def row_key(self, row: dict) -> str | None:
        record = row.get("record")
        return record_key(record) if record is not None else None

    def refresh(self):
        tracker = self.action.tracker
        if tracker is None:
            return
        sync = self.action.session_sync
        sending = sync is not None and sync.sending
        self.feed_checkbox.blockSignals(True)
        self.feed_checkbox.setChecked(sync is not None and sync.feed_default)
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
        finished_rows = []
        for record in reversed(tracker.finished_sessions()):
            status = sync.status(record) if sync else {"status": record["status"]}
            finished_rows.append(
                {
                    "session": session_from_record(record),
                    "record": record,
                    "status": status["status"],
                    "error": status.get("error", ""),
                }
            )
        rows: list[dict[str, Any]] = open_rows + finished_rows
        values = []
        for entry in rows:
            session = entry["session"]
            status = entry["status"]
            if status == SessionStatus.SKIPPED:
                status = f"skipped: {skip_reason(session)}"
            values.append(
                (
                    session.title or session.book_key[:10],
                    session.start.astimezone().strftime("%a %d %b %H:%M"),
                    _duration(session.duration.total_seconds()),
                    _progress(session.start_progress, session.end_progress),
                    session.saves,
                    status,
                )
            )
        self.fill_table(rows, values, select_first=False)

    def local_records(self, rows: list[dict]) -> list[dict]:
        sync = self.action.session_sync
        if sync is None:
            return []
        return [
            record
            for entry in rows
            if (record := entry.get("record")) is not None
            and sync.status(record)["status"] == SessionStatus.LOCAL
            and entry["session"].book_key in self.action.books
            and skip_reason(entry["session"]) is None
        ]

    def show_payload(self):
        local_selected = self.local_records(self.selected_rows())
        local_all = self.local_records(self.rows)
        self.update_send_buttons(len(local_selected), len(local_all))
        entry = self.payload_row()
        if entry is None:
            self.payload.setPlainText("Select a session to see its payload.")
            return
        session = entry["session"]
        sync = self.action.session_sync
        error = entry.get("error")
        if reason := skip_reason(session):
            self.payload.setPlainText(f"Not sent: {reason}.")
            return
        # A local session has no resolved Readwise book id yet.
        item = session_item(session, 0, local_timezone())
        bookwise_id = sync.book_id_for(session.book_key) if sync else None
        item["book_id"] = (
            bookwise_id or f"<Bookwise id for Calibre book {session.book_id}>"
        )
        record = entry.get("record")
        if sync is None:
            item["publish_to_feed"] = False
        elif record is None:
            item["publish_to_feed"] = sync.feed_default
        else:
            item["publish_to_feed"] = sync.publish_to_feed(record)
        body = json.dumps({"items": [item]}, indent=2)
        self.payload.setPlainText(f"{error}\n\n{body}" if error else body)

    def send_selected(self):
        self.send_records(self.local_records(self.selected_rows()))

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
                if self.action.session_sync.feed_default
                else "They will stay off your feed."
            ),
        ):
            return
        self.send_records(records)

    def send_records(self, records: list[dict]):
        sync = self.action.session_sync
        if sync is None or not records:
            return
        if not self.action.have_token():
            return
        try:
            sync.queue_locals(records)
        except (OSError, ValueError) as error:
            error_dialog(self, "Session upload unavailable", str(error), show=True)
            return
        self.refresh()
        self.action.poll()

    def retry_selected(self):
        sync = self.action.session_sync
        if sync is None:
            return
        records = [
            record
            for entry in self.selected_rows()
            if (record := entry.get("record")) is not None
            and sync.status(record)["status"] == SessionStatus.NEEDS_REVIEW
        ]
        if not records:
            return
        sessions = "this session is" if len(records) == 1 else "these sessions are"
        if not question_dialog(
            self,
            f"Retry {len(records)} session {_plural(len(records), 'upload')}?",
            "Check Readwise Books first. Its API does not detect duplicate sessions. "
            f"Retry only if {sessions} absent there.",
        ):
            return
        try:
            for record in records:
                sync.retry_after_review(record)
        except (OSError, ValueError) as error:
            error_dialog(self, "Retry unavailable", str(error), show=True)
        self.refresh()


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


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

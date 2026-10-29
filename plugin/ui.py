"""Bookwise Sessions toolbar action: runs the session tracker inside Calibre.

Polls the e-book viewer's saved positions every few seconds (see live.py)
and shows recorded sessions with the Bookwise payload each would send.
Record only: nothing is sent anywhere.
"""

import hashlib
import json
import traceback
from pathlib import Path

from calibre.constants import cache_dir, config_dir
from calibre.gui2 import open_local_file
from calibre.gui2.actions import InterfaceAction
from calibre_plugins.bookwise_sessions.bookwise import (
    local_timezone,
    session_item,
    skip_reason,
)
from calibre_plugins.bookwise_sessions.live import (
    Tracker,
    macos_viewer_focused,
    session_from_record,
)
from calibre_plugins.bookwise_sessions.progress import book_progress
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
DATA_DIR = Path(config_dir) / "plugins" / "bookwise_sessions"
ANNOTS_DIR = Path(config_dir) / "viewer" / "annots"
VIEWER_CACHE = Path(cache_dir()) / "ev2"


class BookwiseSessionsAction(InterfaceAction):
    name = "Bookwise Sessions"
    action_spec = (
        "Bookwise",
        "view.png",
        "Reading sessions for Readwise Bookwise",
        None,
    )
    action_type = "current"
    popup_type = 1  # QToolButton.ToolButtonPopupMode.MenuButtonPopup

    def genesis(self):
        self.tracker = None
        self.books: dict[str, dict] = {}
        self.dialog = None
        self.poll_failed = False
        menu = QMenu(self.gui)
        menu.addAction("Show sessions", self.show_sessions)
        menu.addAction("End session now", self.end_session)
        menu.addSeparator()
        menu.addAction("Open data folder", lambda: open_local_file(str(DATA_DIR)))
        self.qaction.setMenu(menu)
        self.qaction.triggered.connect(self.show_sessions)
        self.timer = QTimer(self.gui)
        self.timer.timeout.connect(self.poll)

    def initialization_complete(self):
        self.index_library()
        self.tracker = Tracker(
            DATA_DIR,
            ANNOTS_DIR,
            book_lookup=self.lookup_book,
            progress_of=lambda path, cfi: book_progress(path, cfi, VIEWER_CACHE),
            focus_probe=macos_viewer_focused,
        )
        self.timer.start(POLL_MS)

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
                    books[key] = {"book_id": book_id, "title": title, "path": path}
        self.books = books

    def lookup_book(self, key: str) -> dict | None:
        if key not in self.books:
            self.index_library()  # a book added since the last index
        return self.books.get(key)

    def poll(self):
        if self.tracker is None:
            return
        try:
            self.tracker.poll()
            self.poll_failed = False
        # A tracker bug must never break Calibre's timer: log it and keep going.
        except Exception:  # noqa: BLE001
            if not self.poll_failed:  # report once, not every two seconds
                traceback.print_exc()
            self.poll_failed = True
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.refresh()

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

    def __init__(self, parent, action: BookwiseSessionsAction):
        super().__init__(parent)
        self.action = action
        self.rows: list[dict] = []
        self.setWindowTitle("Bookwise reading sessions")
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
                "Recorded from the e-book viewer. Nothing is sent to Readwise yet: "
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

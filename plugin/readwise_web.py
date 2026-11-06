"""Worker threads and Calibre UI for Readwise Books EPUB uploads."""

import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

from calibre.gui2 import error_dialog, info_dialog
from calibre_plugins.readwise_books.ebook_upload import UploadClient, UploadUncertain
from calibre_plugins.readwise_books.metadata_sync import prefs
from calibre_plugins.readwise_books.readwise_api import ReadOnlyClient, ReadwiseError
from qt.core import QThread, pyqtSignal


class UploadWorker(QThread):
    finished_upload = pyqtSignal(object, object)  # upload record or None, error or None

    def __init__(self, parent, token: str, book_id: int, path: str):
        super().__init__(parent)
        self.token, self.book_id, self.path = token, book_id, path

    def run(self):
        try:
            path = Path(self.path)
            upload = UploadClient(self.token).upload(path)
            self.finished_upload.emit(upload, None)
        except Exception as error:  # noqa: BLE001 - always report back
            if not isinstance(error, (ReadwiseError, OSError)):
                traceback.print_exc()
            self.finished_upload.emit(None, error)


class RemoteBooksWorker(QThread):
    """Fetches the Readwise Books library with the API token (GET only)."""

    finished_fetch = pyqtSignal(object, object)  # (books, None) or (None, error)

    def __init__(self, parent, token: str):
        super().__init__(parent)
        self.token = token

    def run(self):
        try:
            self.finished_fetch.emit(ReadOnlyClient(self.token).list_books(), None)
        except ReadwiseError as error:
            self.finished_fetch.emit(None, str(error))
        except Exception as error:  # noqa: BLE001 - always report back
            traceback.print_exc()
            self.finished_fetch.emit(None, f"Unexpected error: {error}")


class EpubUploader:
    def __init__(self, gui):
        self.gui = gui
        self._remove_old_sign_in()
        self.worker: UploadWorker | None = None
        self.listeners: list[Callable[[int, Exception | None], None]] = []

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    @staticmethod
    def _remove_old_sign_in():
        """Discard credentials saved by the retired web upload implementation."""
        if "web_sign_in" in prefs:
            del prefs["web_sign_in"]
        if sys.platform == "darwin":
            subprocess.run(
                [
                    "/usr/bin/security",
                    "delete-generic-password",
                    "-a",
                    "readwise.io web sign-in",
                    "-s",
                    "Readwise Books Calibre plugin",
                ],
                capture_output=True,
                check=False,
            )

    def upload_selected(self):
        ids = list(self.gui.library_view.get_selected_ids())
        if len(ids) != 1:
            error_dialog(
                self.gui,
                "Select one EPUB",
                "Select one book that has an EPUB. Readwise accepts EPUB uploads only.",
                show=True,
            )
            return
        self.upload_book(ids[0], self.gui)

    def upload_book(self, book_id: int, parent):
        if self.busy:
            info_dialog(
                parent, "Upload running", "An upload is already running.", show=True
            )
            return
        db = self.gui.current_db.new_api
        if not db.has_format(book_id, "EPUB"):
            error_dialog(
                parent,
                "Select one EPUB",
                "Select one book that has an EPUB. Readwise accepts EPUB uploads only.",
                show=True,
            )
            return
        token = prefs.get("token", "").strip()
        if not token:
            error_dialog(
                parent,
                "Readwise token needed",
                "Set a Readwise API token under Settings to upload EPUBs.",
                show=True,
            )
            return
        path = db.format_abspath(book_id, "EPUB")
        self.worker = UploadWorker(self.gui, token, book_id, path)
        self.worker.finished_upload.connect(self._finished)
        self.worker.start()
        self.gui.status_bar.show_message(f"Uploading {Path(path).name} to Readwise…")

    def _finished(self, upload, error):
        name = Path(self.worker.path).name if self.worker else "The book"
        if self.worker is not None:
            for listener in self.listeners:
                listener(self.worker.book_id, error)
        if error is None:
            state = upload.get("upload_status") if isinstance(upload, dict) else None
            message = (
                f"{name} is ready in Readwise Books."
                if state == "ready"
                else f"{name} was sent. Readwise is still processing it."
            )
            info_dialog(self.gui, "EPUB uploaded", message, show=True)
        elif isinstance(error, UploadUncertain):
            error_dialog(self.gui, "Upload outcome unknown", str(error), show=True)
        else:
            error_dialog(self.gui, "Upload failed", str(error), show=True)

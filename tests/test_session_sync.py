import json

import pytest

from calibre_bookwise.readwise_api import ReadwiseError
from calibre_bookwise.session_sync import BooksClient, SessionSync


def record(start="2026-10-30T10:00:00+00:00", finished="2026-10-30T10:05:00+00:00"):
    return {
        "book_key": "book",
        "book_id": 7,
        "title": "A Book",
        "start": start,
        "end": "2026-10-30T10:04:00+00:00",
        "finished_at": finished,
        "start_progress": 0.1,
        "end_progress": 0.2,
        "saves": 3,
        "longest_gap": 120,
        "status": "pending",
    }


BOOKS = {
    "book": {
        "book_id": 7,
        "title": "A Book",
        "authors": ("An Author",),
        "isbn": None,
        "asin": None,
    }
}


class FakeClient:
    def __init__(self, remote_books=None):
        self.books = (
            remote_books
            if remote_books is not None
            else [{"id": 42, "title": "A Book", "author": "An Author"}]
        )
        self.created_books = []
        self.sessions = []

    def list_books(self):
        return self.books

    def create_book(self, item):
        self.created_books.append(item)
        return 42

    def create_session(self, item):
        self.sessions.append(item)
        return 99


def test_default_dry_run_and_mode_switch_only_sends_future_sessions(tmp_path):
    old = record()
    sync = SessionSync(tmp_path, [old])
    client = FakeClient()
    sync.poll([old], BOOKS, client)
    assert not client.sessions
    assert sync.status(old)["status"] == "local"

    sync.set_mode("send", [old])
    new = record("2026-10-30T11:00:00+00:00", "2026-10-30T11:05:00+00:00")
    new["end"] = "2026-10-30T11:04:00+00:00"
    sync.poll([old, new], BOOKS, client)
    assert len(client.sessions) == 1
    assert client.sessions[0]["book_id"] == 42
    assert client.sessions[0]["publish_to_feed"] is False
    assert sync.status(new)["status"] == "uploaded"
    assert sync.status(old)["status"] == "local"

    restarted = SessionSync(tmp_path, [old, new])
    restarted.poll([old, new], BOOKS, client)
    assert len(client.sessions) == 1


def test_selected_local_session_sends_while_dry_run_remains_on(tmp_path):
    first = record()
    second = record("2026-10-30T11:00:00+00:00", "2026-10-30T11:05:00+00:00")
    second["end"] = "2026-10-30T11:04:00+00:00"
    sync = SessionSync(tmp_path, [first, second])
    sync.queue_local(second)
    client = FakeClient()
    sync.poll([first, second], BOOKS, client)
    assert sync.state["mode"] == "dry_run"
    assert sync.status(first)["status"] == "local"
    assert sync.status(second)["status"] == "uploaded"
    assert len(client.sessions) == 1
    assert client.sessions[0]["publish_to_feed"] is False


def test_multiple_selected_local_sessions_queue_without_changing_mode(tmp_path):
    first = record()
    second = record("2026-10-30T11:00:00+00:00", "2026-10-30T11:05:00+00:00")
    second["end"] = "2026-10-30T11:04:00+00:00"
    sync = SessionSync(tmp_path, [first, second])
    sync.queue_locals([first, second])
    client = FakeClient()
    sync.poll([first, second], BOOKS, client)
    assert len(client.sessions) == 1  # one request per poll
    sync.poll([first, second], BOOKS, client)
    assert len(client.sessions) == 2
    assert sync.state["mode"] == "dry_run"
    assert [sync.status(r)["status"] for r in (first, second)] == [
        "uploaded",
        "uploaded",
    ]


def test_batch_selection_rejects_ineligible_rows_without_queueing_any(tmp_path):
    eligible = record()
    skipped = record("2026-10-30T11:00:00+00:00", "2026-10-30T11:05:00+00:00")
    skipped["end"] = "2026-10-30T11:00:30+00:00"
    skipped["status"] = "skipped"
    sync = SessionSync(tmp_path, [eligible, skipped])
    with pytest.raises(ValueError):
        sync.queue_locals([eligible, skipped])
    assert sync.status(eligible)["status"] == "local"


def test_unmatched_book_is_created_once_before_session(tmp_path):
    sync = SessionSync(tmp_path, [])
    sync.set_mode("send", [])
    client = FakeClient(remote_books=[])
    entry = record()
    sync.poll([entry], BOOKS, client)
    assert client.created_books == [
        {
            "product": {"title": "A Book", "author": "An Author"},
            "status": "currently_reading",
        }
    ]
    assert len(client.sessions) == 1
    assert SessionSync(tmp_path, [entry]).state["book_ids"]["book"] == 42


def test_uncertain_session_post_is_never_retried_automatically(tmp_path):
    sync = SessionSync(tmp_path, [])
    sync.set_mode("send", [])

    class FailingClient(FakeClient):
        def create_session(self, item):
            raise ReadwiseError("Connection lost")

    client = FailingClient()
    entry = record()
    sync.poll([entry], BOOKS, client)
    assert sync.status(entry)["status"] == "needs review"
    restarted = SessionSync(tmp_path, [entry])
    restarted.poll([entry], BOOKS, FakeClient())
    assert restarted.status(entry)["status"] == "needs review"
    client = FakeClient()
    restarted.set_mode("dry_run", [entry])
    restarted.retry_after_review(entry)
    restarted.poll([entry], BOOKS, client)
    assert restarted.status(entry)["status"] == "uploaded"
    assert len(client.sessions) == 1


def test_switching_back_to_dry_run_keeps_queued_sessions_local(tmp_path):
    sync = SessionSync(tmp_path, [])
    sync.set_mode("send", [])
    entry = record()
    sync.poll([entry], BOOKS, None)
    assert sync.status(entry)["status"] == "pending"
    sync.set_mode("dry_run", [entry])
    sync.set_mode("send", [entry])
    client = FakeClient()
    sync.poll([entry], BOOKS, client)
    assert sync.status(entry)["status"] == "local"
    assert not client.sessions


def test_interrupted_sending_requires_review(tmp_path):
    entry = record()
    sync = SessionSync(tmp_path, [])
    sync.set_mode("send", [])
    sync.poll([entry], BOOKS, None)
    key = next(iter(sync.state["items"]))
    sync.state["items"][key]["status"] = "sending"
    sync._save()
    restarted = SessionSync(tmp_path, [entry])
    assert restarted.status(entry)["status"] == "needs review"


def test_uncertain_book_creation_is_not_retried(tmp_path):
    class FailingBookClient(FakeClient):
        def create_book(self, item):
            raise ReadwiseError("Connection lost")

    sync = SessionSync(tmp_path, [])
    sync.set_mode("send", [])
    entry = record()
    sync.poll([entry], BOOKS, FailingBookClient(remote_books=[]))
    assert sync.status(entry)["status"] == "needs review"


def test_books_client_posts_off_feed_session_and_checks_result():
    requests = []

    def fetch(request):
        requests.append(request)
        return 200, {}, b'{"results":[{"status":"created","session_id":99}]}'

    client = BooksClient(" token ", fetch=fetch)
    assert client.create_session({"book_id": 42, "publish_to_feed": False}) == 99
    assert requests[0].full_url == "https://readwise.io/books/api/v1/sessions"
    assert requests[0].get_method() == "POST"
    assert requests[0].get_header("Authorization") == "Token token"
    assert json.loads(requests[0].data) == {
        "items": [{"book_id": 42, "publish_to_feed": False}]
    }

import json

import pytest

from calibre_bookwise.highlights import HighlightClient, HighlightSync, RateLimited
from calibre_bookwise.readwise_api import ReadwiseError


@pytest.fixture
def setup(tmp_path):
    annots = tmp_path / "annots"
    annots.mkdir()
    now = [100.0]
    sync = HighlightSync(tmp_path / "data", annots, clock=lambda: now[0])
    books = {"book": {"title": "A book", "authors": ["An Author"]}}
    return sync, annots / "book.json", now, books


def write(path, *annotations):
    path.write_text(json.dumps(annotations))


def highlight(uuid="one", **kwargs):
    return dict(type="highlight", uuid=uuid, highlighted_text="A passage", **kwargs)


class FakeClient:
    def __init__(self):
        self.calls = []

    def send(self, payload, remote_id=None):
        self.calls.append((payload, remote_id))
        return remote_id or 123


def test_baseline_disabled_and_restart(setup):
    sync, path, now, books = setup
    client = FakeClient()
    write(path, highlight())
    sync.poll(books, client)
    assert not sync.state["items"]
    sync.set_enabled(True)
    write(path, highlight(notes="old"), highlight("two", notes="My note"))
    sync.poll(books, client)
    assert not client.calls
    now[0] += 6
    sync.poll(books, client)
    payload, remote_id = client.calls[0]
    assert payload["note"] == "My note"
    assert payload["author"] == "An Author"
    assert remote_id is None
    restarted = HighlightSync(sync.path.parent, path.parent, clock=lambda: now[0])
    restarted.poll(books, client)
    assert len(client.calls) == 1


def test_later_notes_patch_only_owned_highlight(setup):
    sync, path, now, books = setup
    client = FakeClient()
    sync.set_enabled(True)
    write(path, highlight())
    sync.poll(books, client)
    now[0] += 6
    sync.poll(books, client)
    write(path, highlight(notes="Later note"))
    sync.poll(books, client)
    now[0] += 6
    sync.poll(books, client)
    assert client.calls[-1] == ({"note": "Later note"}, 123)
    write(path, {**highlight(notes="Later note"), "highlighted_text": "Edited"})
    sync.poll(books, client)
    now[0] += 6
    sync.poll(books, client)
    assert len(client.calls) == 2


def test_rate_limit_survives_restart(setup):
    sync, path, now, books = setup
    sync.set_enabled(True)
    write(path, highlight())
    sync.poll(books, None)
    now[0] += 6

    class Limited:
        def send(self, payload, remote_id=None):
            raise RateLimited(120)

    sync.poll(books, Limited())
    restarted = HighlightSync(sync.path.parent, path.parent, clock=lambda: now[0])
    client = FakeClient()
    restarted.poll(books, client)
    assert not client.calls
    now[0] += 120
    restarted.poll(books, client)
    assert len(client.calls) == 1


def test_removed_pending_and_other_library_do_not_upload(setup):
    sync, path, now, books = setup
    sync.set_enabled(True)
    write(path, highlight(), highlight("two"))
    sync.poll(books, None)
    write(path, highlight(removed=True), highlight("two"))
    now[0] += 6
    client = FakeClient()
    sync.poll({}, client)
    assert not client.calls
    sync.poll(books, client)
    assert len(client.calls) == 1
    assert sync.state["items"]["book:one"]["status"] == "removed locally"


def test_partial_file_baseline_aborts_then_retries(setup):
    sync, path, _now, books = setup
    path.write_text("[")
    with pytest.raises(ValueError):
        sync.set_enabled(True)
    assert not sync.state["enabled"]
    path.unlink()
    sync.set_enabled(True)
    path.write_text("[")
    sync.poll(books, FakeClient())
    write(path, highlight())
    sync.poll(books, None)
    assert sync.state["items"]["book:one"]["status"] == "pending"


def test_limits_and_corrupt_queue(setup):
    sync, path, _now, books = setup
    sync.set_enabled(True)
    write(path, highlight(notes="x" * 8192))
    sync.poll(books, FakeClient())
    assert sync.state["items"]["book:one"]["status"] == "invalid"
    write(path, highlight(notes="corrected"))
    sync.poll(books, None)
    assert sync.state["items"]["book:one"]["status"] == "pending"
    sync.path.write_text("{")
    with pytest.raises(ValueError):
        HighlightSync(sync.path.parent, path.parent)


def test_http_contract():
    requests = []

    def fetch(request):
        requests.append(request)
        return 200, {}, b'[{"modified_highlights": [42]}]'

    client = HighlightClient(" token ", fetch=fetch)
    assert client.send({"text": "Passage"}) == 42
    assert client.send({"note": "Note"}, 42) == 42
    assert [r.get_method() for r in requests] == ["POST", "PATCH"]
    assert requests[0].full_url == "https://readwise.io/api/v2/highlights/"
    assert requests[1].full_url.endswith("/42/")
    assert requests[0].get_header("Authorization") == "Token token"
    assert json.loads(requests[0].data) == {"highlights": [{"text": "Passage"}]}


@pytest.mark.parametrize("status", [401, 429, 500])
def test_failed_requests_are_not_acknowledged(status):
    client = HighlightClient("t", fetch=lambda r: (status, {"Retry-After": "7"}, b"{}"))
    with pytest.raises(ReadwiseError):
        client.send({"text": "Passage"})


def test_missing_remote_id_is_not_acknowledged():
    client = HighlightClient(
        "t", fetch=lambda r: (200, {}, b'[{"modified_highlights": []}]')
    )
    with pytest.raises(ReadwiseError, match="no unique highlight ID"):
        client.send({"text": "Passage"})

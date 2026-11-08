import json

import pytest

from calibre_bookwise.highlights import HighlightClient
from calibre_bookwise.readwise_api import (
    BASE_URL,
    RateLimited,
    ReadOnlyClient,
    ReadwiseError,
)
from calibre_bookwise.session_sync import BooksClient


class FakeReadwise:
    """Answers requests from a list of (status, headers, body) replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, headers, body = self.replies.pop(0)
        return status, headers, json.dumps(body).encode()


def test_follows_next_pages_with_token_and_only_gets():
    fake = FakeReadwise(
        (200, {}, {"results": [{"id": 1}], "next": BASE_URL + "books?cursor=abc"}),
        (200, {}, {"results": [{"id": 2}], "next": None}),
    )

    books = ReadOnlyClient(" tok123 ", fetch=fake).list_books()

    assert [b["id"] for b in books] == [1, 2]
    assert [r.full_url for r in fake.requests] == [
        BASE_URL + "books?limit=1000",
        BASE_URL + "books?cursor=abc",
    ]
    assert {r.get_method() for r in fake.requests} == {"GET"}
    assert {r.get_header("Authorization") for r in fake.requests} == {"Token tok123"}


def test_lists_all_upload_pages():
    fake = FakeReadwise(
        (200, {}, {"results": [{"id": 9}], "next": BASE_URL + "uploads?cursor=abc"}),
        (200, {}, {"results": [{"id": 8}], "next": None}),
    )

    uploads = ReadOnlyClient("token", fetch=fake).list_uploads()

    assert [item["id"] for item in uploads] == [9, 8]
    assert [request.full_url for request in fake.requests] == [
        BASE_URL + "uploads?limit=1000",
        BASE_URL + "uploads?cursor=abc",
    ]
    assert {request.get_method() for request in fake.requests} == {"GET"}


def test_waits_out_rate_limit_then_retries():
    waits = []
    fake = FakeReadwise(
        (429, {"Retry-After": "7"}, {"error": {"code": "rate_limited"}}),
        (200, {}, {"results": [], "next": None}),
    )

    assert ReadOnlyClient("t", fetch=fake, sleep=waits.append).list_sessions() == []
    assert waits == [7.0]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "7"}, 7.0),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 30.0),
        ({}, 30.0),
    ],
)
def test_rate_limit_wait_tolerates_any_retry_after(headers, expected):
    waits = []
    fake = FakeReadwise(
        (429, headers, {}),
        (200, {}, {"results": [], "next": None}),
    )

    ReadOnlyClient("t", fetch=fake, sleep=waits.append).list_books()

    assert waits == [expected]


def test_bad_token_gives_a_clear_error():
    fake = FakeReadwise((401, {}, {"error": {"code": "unauthenticated"}}))

    with pytest.raises(ReadwiseError, match="rejected the API token"):
        ReadOnlyClient("bad", fetch=fake).list_books()


def test_refuses_to_follow_links_off_readwise():
    fake = FakeReadwise((200, {}, {"results": [], "next": "https://example.com/x"}))

    with pytest.raises(ReadwiseError, match="outside Readwise"):
        ReadOnlyClient("t", fetch=fake).list_books()


def test_client_has_no_way_to_write():
    public = {name for name in dir(ReadOnlyClient) if not name.startswith("_")}

    assert public == {"list_books", "list_sessions", "list_uploads"}


def test_every_client_reports_network_failures_the_same_way():
    def offline(request):
        raise OSError("no route to host")

    for send in (
        lambda: ReadOnlyClient("t", fetch=offline).list_books(),
        lambda: BooksClient("t", fetch=offline).create_session({}),
        lambda: HighlightClient("t", fetch=offline).send({"text": "Passage"}),
    ):
        with pytest.raises(ReadwiseError, match="Couldn't reach Readwise"):
            send()


def test_write_client_rate_limit_carries_the_delay():
    client = BooksClient("t", fetch=lambda r: (429, {"Retry-After": "12"}, b""))
    with pytest.raises(RateLimited) as limited:
        client.create_session({})
    assert limited.value.delay == 12.0

import json

import pytest

from calibre_bookwise import ebook_upload
from calibre_bookwise.ebook_upload import (
    LibraryStatus,
    ReadwiseError,
    UploadClient,
    UploadUncertain,
    library_status,
)
from calibre_bookwise.matching import CalibreBook, plan_matches

S3 = "https://readwise-assets.s3.amazonaws.com/uploads/abc?sig=1"


class FakeReadwise:
    def __init__(
        self,
        *,
        put_status=200,
        complete_error=None,
        upload_status="ready",
        upload_url=S3,
    ):
        self.requests = []
        self.put_bytes = None
        self.put_status = put_status
        self.complete_error = complete_error
        self.upload_status = upload_status
        self.upload_url = upload_url

    def __call__(self, request):
        self.requests.append(request)
        if request.full_url == "https://readwise.io/books/api/v1/uploads":
            return (
                200,
                {},
                json.dumps(
                    {
                        "results": [
                            {
                                "status": "created",
                                "upload": {"id": 9312, "upload_status": "pending"},
                                "upload_url": self.upload_url,
                                "upload_headers": {
                                    "Content-Type": "application/epub+zip",
                                    "Content-Length": "13",
                                },
                            }
                        ]
                    }
                ).encode(),
            )
        if request.get_method() == "PUT":
            self.put_bytes = request.data.read()
            return self.put_status, {}, b""
        if request.full_url.endswith("uploads/complete"):
            if self.complete_error:
                raise self.complete_error
            return (
                200,
                {},
                json.dumps(
                    {
                        "results": [
                            {
                                "status": "processing",
                                "upload": {"id": 9312, "upload_status": "processing"},
                            }
                        ]
                    }
                ).encode(),
            )
        assert request.full_url.endswith("uploads?ids=9312")
        return (
            200,
            {},
            json.dumps(
                {"results": [{"id": 9312, "upload_status": self.upload_status}]}
            ).encode(),
        )


def epub(tmp_path, name="A Book.epub", data=b"PK epub bytes"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_upload_uses_documented_api_and_only_signed_headers_for_put(tmp_path):
    fake = FakeReadwise()
    upload = UploadClient("secret", fetch=fake, sleep=lambda _: None).upload(
        epub(tmp_path)
    )
    start, put, complete, poll = fake.requests
    assert upload == {"id": 9312, "upload_status": "ready"}
    assert json.loads(start.data) == {
        "items": [{"file_name": "A Book.epub", "size": 13}]
    }
    assert start.get_header("Authorization") == "Token secret"
    assert put.full_url == S3 and fake.put_bytes == b"PK epub bytes"
    assert put.get_header("Authorization") is None
    assert put.get_header("Content-type") == "application/epub+zip"
    assert put.get_header("Content-length") == "13"
    assert json.loads(complete.data) == {
        "items": [{"id": 9312, "status": "currently_reading"}]
    }
    assert complete.get_header("Authorization") == "Token secret"
    assert poll.get_header("Authorization") == "Token secret"


def test_failed_put_does_not_complete_upload(tmp_path):
    fake = FakeReadwise(put_status=403)
    with pytest.raises(ReadwiseError, match="transfer failed"):
        UploadClient("secret", fetch=fake).upload(epub(tmp_path))
    assert len(fake.requests) == 2


def test_foreign_signed_url_is_refused_before_transfer(tmp_path):
    fake = FakeReadwise(upload_url="https://example.com/steal")
    with pytest.raises(ReadwiseError, match="unsafe upload destination"):
        UploadClient("secret", fetch=fake).upload(epub(tmp_path))
    assert len(fake.requests) == 1


def test_completion_timeout_is_uncertain_and_not_retried(tmp_path):
    fake = FakeReadwise(complete_error=TimeoutError("timed out"))
    with pytest.raises(UploadUncertain, match="Check Readwise"):
        UploadClient("secret", fetch=fake).upload(epub(tmp_path))
    assert len(fake.requests) == 3


def test_processing_is_returned_after_bounded_polling(tmp_path):
    fake = FakeReadwise(upload_status="processing")
    result = UploadClient("secret", fetch=fake, sleep=lambda _: None, polls=2).upload(
        epub(tmp_path)
    )
    assert result["upload_status"] == "processing"
    assert len(fake.requests) == 5


def test_failed_processing_reports_error(tmp_path):
    fake = FakeReadwise(upload_status="failed")
    with pytest.raises(ReadwiseError, match="could not process"):
        UploadClient("secret", fetch=fake, sleep=lambda _: None).upload(epub(tmp_path))


def test_invalid_epub_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(ebook_upload, "MAX_SIZE", 3)
    fake = FakeReadwise()
    client = UploadClient("secret", fetch=fake)
    with pytest.raises(ReadwiseError, match="500 MB"):
        client.upload(epub(tmp_path, "a.epub", b"PK.."))
    with pytest.raises(ReadwiseError, match="Select an EPUB"):
        client.upload(epub(tmp_path, "a.pdf", b"PK"))
    assert fake.requests == []


def test_library_status_from_readwise_books():
    books = [
        CalibreBook(1, "Uploaded Book", ("Ann Author",)),
        CalibreBook(2, "Catalog Book", ("Bo Writer",)),
        CalibreBook(3, "Gone Book", ("Cy Poet",)),
        CalibreBook(4, "Missing Book", ("Di Scribe",)),
        CalibreBook(5, "Linked Elsewhere", ("Ed Hand",)),
    ]
    remote = [
        {
            "id": 10,
            "title": "Uploaded Book",
            "author": "Ann Author",
            "creation_origin": "uploaded_ebook",
            "status": "currently_reading",
        },
        {
            "id": 20,
            "title": "Catalog Book",
            "author": "Bo Writer",
            "creation_origin": "api",
            "status": "want_to_read",
        },
        {"id": 30, "title": "Gone Book", "author": "Cy Poet", "status": "deleted"},
    ]
    matches = plan_matches(books, remote, saved={5: 99})
    assert [library_status(m) for m in matches] == [
        LibraryStatus.UPLOADED,
        LibraryStatus.IN_READWISE,
        LibraryStatus.DELETED,
        LibraryStatus.NOT_IN_READWISE,
        LibraryStatus.NOT_IN_READWISE,
    ]

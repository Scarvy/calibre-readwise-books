import sqlite3

import pytest

from calibre_bookwise.matching import (
    CalibreBook,
    create_requests,
    main_title,
    normalize_isbn,
    plan_matches,
    read_calibre_books,
)

JAVASCRIPT = {
    "id": 2291,
    "title": "JavaScript",
    "author": "Douglas Crockford",
    "status": "did_not_finish",
    "product": {"title": "JavaScript", "isbn": "9780596517748", "asin": "0596517742"},
}
CODE_COMPLETE = {
    "id": 8341,
    "title": "Code Complete",
    "author": "Steve McConnell",
    "product": {"title": "Code Complete", "isbn": "9780735619678"},
}


def book(
    calibre_id=4,
    title="JavaScript: The Good Parts",
    authors=("Douglas Crockford",),
    **ids,
):
    return CalibreBook(calibre_id, title, tuple(authors), **ids)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("9780596517748", "9780596517748"),
        ("978-0-596-51774-8", "9780596517748"),
        ("0596517742", "9780596517748"),  # ISBN-10 converted
        ("059651774X", None),  # bad ISBN-10 check digit
        ("9780596517749", None),  # bad ISBN-13 check digit
        ("9798341662698", "9798341662698"),  # 979 prefix
        ("", None),
        (None, None),
    ],
)
def test_normalize_isbn(value, expected):
    assert normalize_isbn(value) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("JavaScript: The Good Parts", "javascript"),
        ("Code Complete, 2nd Edition", "code complete"),
        ("Interaction Design, 6th Edition", "interaction design"),
        (
            "Designing Data-Intensive Applications",
            "designing data intensive applications",
        ),
        ("Café (Revised)", "cafe"),
    ],
)
def test_main_title(title, expected):
    assert main_title(title) == expected


def test_isbn_match_even_when_titles_differ():
    [match] = plan_matches([book(isbn="978-0-596-51774-8")], [JAVASCRIPT])

    assert (match.action, match.bookwise_id, match.matched_by) == (
        "use existing",
        2291,
        "isbn",
    )


def test_isbn10_in_calibre_matches_isbn13_in_bookwise():
    [match] = plan_matches([book(isbn="0596517742")], [JAVASCRIPT])

    assert match.matched_by == "isbn"


def test_asin_match():
    [match] = plan_matches([book(asin="0596517742")], [JAVASCRIPT])

    assert (match.bookwise_id, match.matched_by) == (2291, "asin")


def test_title_and_author_match_handles_shortened_titles():
    matches = plan_matches(
        [
            book(),
            book(5, "Code Complete, 2nd Edition", ["Steve McConnell"]),
        ],
        [JAVASCRIPT, CODE_COMPLETE],
    )

    assert [(m.bookwise_id, m.matched_by) for m in matches] == [
        (2291, "title_author"),
        (8341, "title_author"),
    ]


def test_same_title_by_another_author_is_not_a_match():
    [match] = plan_matches([book(authors=["David Flanagan"])], [JAVASCRIPT])

    assert match.action == "create"


def test_saved_mapping_wins():
    [match] = plan_matches([book(isbn="9780596517748")], [JAVASCRIPT], saved={4: 999})

    assert (match.bookwise_id, match.matched_by) == (999, "saved")


def test_create_prefers_isbn_then_asin_then_title_and_author():
    matches = plan_matches(
        [
            book(1, "A", ["Ann Author"], isbn="0596517742"),
            book(2, "B", ["Ann Author"], asin="B00TEST"),
            book(3, "C", ["Ann Author"]),
        ],
        [],
    )

    assert [m.create_item for m in matches] == [
        {"product": {"isbn": "9780596517748"}, "status": "currently_reading"},
        {"product": {"asin": "B00TEST"}, "status": "currently_reading"},
        {
            "product": {"title": "C", "author": "Ann Author"},
            "status": "currently_reading",
        },
    ]


def test_no_identifiers_and_unknown_author_cannot_be_matched():
    [match] = plan_matches([book(title="Notes", authors=["Unknown"])], [])

    assert match.action == "cannot match"
    assert match.problem


def test_create_requests_batch_by_ten():
    matches = plan_matches(
        [book(i, f"Book {i}", ["Ann Author"]) for i in range(12)], []
    )

    assert [len(body["items"]) for body in create_requests(matches)] == [10, 2]


@pytest.fixture
def library(tmp_path):
    with sqlite3.connect(tmp_path / "metadata.db") as db:
        db.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);
            INSERT INTO books VALUES (4, 'JavaScript: The Good Parts'), (13, 'Interaction Design');
            INSERT INTO authors VALUES (1, 'Douglas Crockford'), (2, 'Yvonne Rogers'), (3, 'Helen Sharp');
            INSERT INTO books_authors_link (book, author) VALUES (4, 1), (13, 2), (13, 3);
            INSERT INTO identifiers (book, type, val) VALUES
                (4, 'isbn', '9780596517748'), (4, 'amazon', '0596517742');
            """
        )
    return tmp_path


def test_read_calibre_books(library):
    assert read_calibre_books(library) == [
        CalibreBook(
            4,
            "JavaScript: The Good Parts",
            ("Douglas Crockford",),
            "9780596517748",
            "0596517742",
        ),
        CalibreBook(13, "Interaction Design", ("Yvonne Rogers", "Helen Sharp")),
    ]
    assert [b.calibre_id for b in read_calibre_books(library, [13])] == [13]

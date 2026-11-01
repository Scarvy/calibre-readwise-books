from calibre_bookwise.book_map import definite_links, read_book_map, remember
from calibre_bookwise.matching import CalibreBook, Match


def test_only_definite_links_are_remembered_and_merged(tmp_path):
    book = CalibreBook(1, "T", ("A",))
    matches = [
        Match(book, 10, "isbn"),
        Match(CalibreBook(2, "T", ("A",)), 20, "title_author"),
        Match(CalibreBook(3, "T", ("A",)), 30, "asin"),
    ]
    remember(tmp_path, definite_links(matches))
    remember(tmp_path, {4: 40})

    assert read_book_map(tmp_path) == {1: 10, 3: 30, 4: 40}


def test_unreadable_map_reads_as_empty(tmp_path):
    (tmp_path / "book_map.json").write_text("[")
    assert read_book_map(tmp_path) == {}

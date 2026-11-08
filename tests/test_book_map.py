import pytest

from calibre_bookwise.book_map import (
    book_map_scope,
    definite_links,
    read_book_map,
    remember,
)
from calibre_bookwise.matching import CalibreBook, Match

SCOPE = book_map_scope("library-a", "token-a")


def test_only_definite_links_are_remembered_and_merged(tmp_path):
    book = CalibreBook(1, "T", ("A",))
    matches = [
        Match(book, 10, "isbn"),
        Match(CalibreBook(2, "T", ("A",)), 20, "title_author"),
        Match(CalibreBook(3, "T", ("A",)), 30, "asin"),
    ]
    remember(tmp_path, SCOPE, definite_links(matches))
    remember(tmp_path, SCOPE, {4: 40})

    assert read_book_map(tmp_path, SCOPE) == {1: 10, 3: 30, 4: 40}


def test_links_are_isolated_by_library_and_readwise_credential(tmp_path):
    other_library = book_map_scope("library-b", "token-a")
    other_account = book_map_scope("library-a", "token-b")
    remember(tmp_path, SCOPE, {1: 10})
    remember(tmp_path, other_library, {1: 20})
    remember(tmp_path, other_account, {1: 30})

    assert read_book_map(tmp_path, SCOPE) == {1: 10}
    assert read_book_map(tmp_path, other_library) == {1: 20}
    assert read_book_map(tmp_path, other_account) == {1: 30}


def test_unreadable_map_is_not_overwritten(tmp_path):
    path = tmp_path / "book_map_scoped.json"
    path.write_text("[")
    with pytest.raises(ValueError, match="restore it from backup"):
        read_book_map(tmp_path, SCOPE)
    with pytest.raises(ValueError, match="restore it from backup"):
        remember(tmp_path, SCOPE, {1: 10})
    assert path.read_text() == "["


def test_old_unscoped_links_are_left_untouched(tmp_path):
    old = tmp_path / "book_map.json"
    old.write_text('{"1": 10}')
    assert read_book_map(tmp_path, SCOPE) == {}
    remember(tmp_path, SCOPE, {1: 20})
    assert old.read_text() == '{"1": 10}'
    assert read_book_map(tmp_path, SCOPE) == {1: 20}

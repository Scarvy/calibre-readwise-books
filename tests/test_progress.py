import json
import zipfile

import pytest

from calibre_bookwise.progress import (
    Position,
    book_progress,
    parse_cfi,
    viewer_position_at_progress,
)
from calibre_bookwise.sessions import saves_from_events

CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""


def chapter(first: str, second: str) -> str:
    # The <head> title must not count towards the text length.
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'><head><title>Ignored</title></head>"
        f"<body><p>{first}</p><p>{second}&amp;</p></body></html>"
    )


@pytest.fixture
def epub(tmp_path):
    """Two chapters of 100 counted characters each: 60 + 40 per chapter."""
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("META-INF/container.xml", CONTAINER)
        book.writestr("OEBPS/content.opf", OPF)
        book.writestr("OEBPS/text/ch1.xhtml", chapter("a" * 60, "b" * 39))
        book.writestr("OEBPS/text/ch2.xhtml", chapter("c" * 60, "d" * 39))
    return str(path)


def test_parse_calibre_cfi_with_ids_and_offset():
    assert parse_cfi(
        "epubcfi(/22/2/4/2[sbo-rt-content]/10/2[pgfId-998551]:296)"
    ) == Position(spine_index=10, steps=(2, 4, 2, 10, 2), offset=296)


def test_viewer_position_at_progress_uses_epub_length(epub, tmp_path):
    # Two chapters, 100 counted characters each. Calibre locations use 1/1000 units.
    assert viewer_position_at_progress(epub, 8, tmp_path / "no-cache") == "0.016"
    assert viewer_position_at_progress(epub, 100, tmp_path / "no-cache") == "0.2"
    assert viewer_position_at_progress(epub, 101, tmp_path / "no-cache") is None


def test_parse_cfi_ignores_image_position():
    assert parse_cfi("epubcfi(/22/2/4/2[sbo-rt-content]/22/2/2@89.32:56.48)") == (
        Position(spine_index=10, steps=(2, 4, 2, 22, 2, 2), offset=0)
    )


def test_parse_standard_cfi_with_indirection():
    assert parse_cfi("epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/3:10)") == (
        Position(spine_index=1, steps=(2, 4, 10, 3), offset=10)
    )


@pytest.mark.parametrize("cfi", ["", "not a cfi", "epubcfi()"])
def test_parse_rejects_malformed(cfi):
    assert parse_cfi(cfi) is None


@pytest.mark.parametrize(
    ("cfi", "expected"),
    [
        ("epubcfi(/2/2/4/2/1:30)", 0.15),  # 30 chars into chapter 1's first <p>
        ("epubcfi(/2/2/4/4:10)", 0.35),  # second <p> (after 60 chars) + 10
        ("epubcfi(/4/2/4/2)", 0.5),  # start of chapter 2
        ("epubcfi(/4/2/4/4@50:50)", 0.8),  # image-style position, second <p>
        ("epubcfi(/4/2/4/4:9999)", 1.0),  # offset clamped to the element's text
    ],
)
def test_book_progress_counts_text_before_position(epub, cfi, expected):
    assert book_progress(epub, cfi) == pytest.approx(expected)


def test_steps_past_the_document_stop_at_deepest_node(epub):
    assert book_progress(epub, "epubcfi(/2/2/4/40/2:5)") == pytest.approx(0.0)


def test_book_progress_without_a_usable_book(epub):
    assert book_progress(None, "epubcfi(/2/2/4/2/1:30)") is None
    assert book_progress("/missing/book.epub", "epubcfi(/2/2/4/2/1:30)") is None
    assert book_progress(epub, "epubcfi(/90/2/4/2)") is None  # no such chapter


def test_saves_from_events_prefers_precise_progress_and_falls_back():
    def event(second: int, pos: str) -> dict:
        return {
            "event": "position_saved",
            "key": "a",
            "path": "/book.epub",
            "pos": pos,
            "viewer_timestamp": f"2026-10-29T03:40:0{second}+00:00",
            "approx_progress": 0.417,
        }

    def progress_of(path, pos):
        return 0.24 if pos == "known" else None

    saves = saves_from_events(
        [event(1, "known"), event(2, "unknown")], progress_of=progress_of
    )

    assert [save.progress for save in saves] == [0.24, 0.417]


def fake_viewer_cache(tmp_path, epub, spine, lengths):
    cache = tmp_path / "ev2"
    (cache / "f" / "c1-test").mkdir(parents=True)
    entry = {"path": "c1-test", "status": "finished", "mtime": 1, "book_path": epub}
    (cache / "metadata.json").write_text(json.dumps({"entries": {"x": [entry]}}))
    manifest = {
        "spine": spine,
        "files": {name: {"length": n} for name, n in zip(spine, lengths)},
    }
    (cache / "f" / "c1-test" / "calibre-book-manifest.json").write_text(
        json.dumps(manifest)
    )
    return cache


SPINE = ["OEBPS/text/ch1.xhtml", "OEBPS/text/ch2.xhtml"]


def test_viewer_cache_lengths_weight_chapters(tmp_path, epub):
    cache = fake_viewer_cache(tmp_path, epub, SPINE, [300, 100])

    # Start of chapter 2: 300 / 400. Halfway through chapter 2: (300 + 50) / 400.
    assert book_progress(epub, "epubcfi(/4/2/4/2)", cache) == pytest.approx(0.75)
    assert book_progress(epub, "epubcfi(/4/2/4/2/1:50)", cache) == pytest.approx(0.875)


def test_viewer_cache_with_other_chapters_is_ignored(tmp_path, epub):
    cache = fake_viewer_cache(tmp_path, epub, ["OEBPS/other.xhtml"], [300])

    assert book_progress(epub, "epubcfi(/4/2/4/2)", cache) == pytest.approx(0.5)


def test_missing_viewer_cache_falls_back_to_text(tmp_path, epub):
    assert book_progress(epub, "epubcfi(/4/2/4/2)", tmp_path / "none") == (
        pytest.approx(0.5)
    )

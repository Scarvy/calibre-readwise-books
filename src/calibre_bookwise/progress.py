"""Book progress (0-1) from the viewer's saved position (an EPUB CFI).

Calibre's viewer saves positions like
    epubcfi(/22/2/4/2[sbo-rt-content]/10/2[pgfId-998551]:296)
The first step picks the spine item, (step / 2) - 1, i.e. the chapter file.
The remaining steps walk that file from the document: even steps are element
children (/2 = first element), odd steps are text between elements, and
":296" is a character offset. "@x:y" marks an image position and is ignored.

Progress = chapters before, plus the fraction of this chapter's text before
the position, over the whole book. That gives per-page precision rather than
per-chapter.

Chapters are weighted by the lengths Calibre's viewer computed when it opened
the book (its cache), so the result matches the % the viewer shows; Calibre
counts each image as roughly 1000 characters, which plain text counting misses.
Without that cache, chapters are weighted by their text length.
Stdlib only: this has to run inside Calibre's bundled Python 3.11.
"""

import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

CFI = re.compile(r"^epubcfi\((?P<path>[^@)]*)(?:@[^)]*)?\)$")
STEP = re.compile(r"/(\d+)(?:\[[^\]]*\])?")
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "wbr",
}
UNCOUNTED_TAGS = {"head", "script", "style"}
# The viewer's cache of converted books (macOS location). Inside a Calibre
# plugin, use calibre.constants.cache_dir() / "ev2" instead.
VIEWER_CACHE = Path.home() / "Library/Caches/calibre/ev2"


@dataclass(frozen=True)
class Position:
    spine_index: int
    steps: tuple[int, ...]  # steps inside the chapter, starting from the document
    offset: int  # characters into the target text


def parse_cfi(cfi: str) -> Position | None:
    match = CFI.match(cfi.strip())
    if not match:
        return None
    path = match.group("path")
    path, _, offset = path.partition(":")
    if "!" in path:
        # Standard EPUB CFI: /6/14!/4/2 - spine step before "!", then steps from
        # the <html> element; prepend /2 to start from the document like Calibre.
        spine_part, _, inner = path.partition("!")
        spine_steps = [int(s) for s in STEP.findall(spine_part)]
        steps = [2, *(int(s) for s in STEP.findall(inner))]
        if not spine_steps:
            return None
        spine_step = spine_steps[-1]
    else:
        all_steps = [int(s) for s in STEP.findall(path)]
        if not all_steps:
            return None
        spine_step, steps = all_steps[0], all_steps[1:]
    return Position(
        spine_index=spine_step // 2 - 1,
        steps=tuple(steps),
        offset=int(offset) if offset.isdigit() else 0,
    )


@dataclass
class Node:
    tag: str
    children: list["Node | str"] = field(default_factory=list)


class TreeBuilder(HTMLParser):
    """Forgiving (X)HTML parser: real-world EPUB chapters aren't always valid XML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.document = Node("#document")
        self.stack = [self.document]

    def handle_starttag(self, tag, attrs):
        node = Node(tag)
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag))

    def handle_endtag(self, tag):
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_document(markup: str) -> Node:
    builder = TreeBuilder()
    builder.feed(markup)
    builder.close()
    return builder.document


def text_length(node: Node) -> int:
    if node.tag in UNCOUNTED_TAGS:
        return 0
    return sum(
        len(child) if isinstance(child, str) else text_length(child)
        for child in node.children
    )


def chars_before(document: Node, steps: tuple[int, ...], offset: int) -> int:
    """Characters of counted text before the position the steps point to.

    Walks as far as the steps resolve; a step past the end of the document
    stops at the deepest node reached.
    """
    before = 0
    node = document
    for step in steps:
        elements = [i for i, c in enumerate(node.children) if isinstance(c, Node)]
        # Even step: the element itself. Odd step: the text after element k.
        k = step // 2
        if step % 2 == 0:
            if not 1 <= k <= len(elements):
                return before
            stop = elements[k - 1]
        else:
            stop = elements[k - 1] + 1 if k >= 1 and k <= len(elements) else 0
        for child in node.children[:stop]:
            before += len(child) if isinstance(child, str) else text_length(child)
        if step % 2 == 1:
            return before + offset
        child = node.children[stop]
        assert isinstance(child, Node)
        if child.tag in UNCOUNTED_TAGS:
            return before
        node = child
    return before + min(offset, text_length(node))


class Book:
    """An EPUB's spine with the text length of each chapter."""

    def __init__(self, epub_path: str):
        self.chapters: list[Node] = []
        self.names: list[str | None] = []  # chapter file paths inside the EPUB
        with zipfile.ZipFile(epub_path) as epub:
            container = ElementTree.fromstring(epub.read("META-INF/container.xml"))
            rootfile_el = container.find(".//{*}rootfile")
            opf_path = rootfile_el.get("full-path") if rootfile_el is not None else None
            if opf_path is None:
                raise ValueError(f"no OPF in {epub_path}")
            opf = ElementTree.fromstring(epub.read(opf_path))
            hrefs = {
                item.get("id"): item.get("href")
                for item in opf.findall(".//{*}manifest/{*}item")
            }
            opf_dir = posixpath.dirname(opf_path)
            for itemref in opf.findall(".//{*}spine/{*}itemref"):
                href = hrefs.get(itemref.get("idref"))
                if href is None:
                    self.chapters.append(Node("#missing"))
                    self.names.append(None)
                    continue
                name = posixpath.normpath(posixpath.join(opf_dir, href))
                markup = epub.read(name).decode("utf-8", errors="replace")
                self.chapters.append(parse_document(markup))
                self.names.append(name)
        self.lengths = [text_length(chapter) for chapter in self.chapters]

    def progress(
        self, position: Position, weights: list[int] | None = None
    ) -> float | None:
        """weights: per-chapter lengths to use instead of text lengths."""
        index = position.spine_index
        weights = weights or self.lengths
        if not 0 <= index < len(self.chapters) or not sum(weights):
            return None
        within = 0.0
        if self.lengths[index]:
            chars = chars_before(self.chapters[index], position.steps, position.offset)
            within = chars / self.lengths[index]
        before = sum(weights[:index])
        return min((before + within * weights[index]) / sum(weights), 1.0)


def viewer_lengths(
    epub_path: str, chapter_names: list[str | None], cache_dir: Path = VIEWER_CACHE
) -> list[int] | None:
    """Per-chapter lengths from the viewer's cache, if it has this book.

    Only used when its chapter list matches the EPUB's, so a stale or
    differently converted cache entry can't skew progress.
    """
    try:
        index = json.loads((cache_dir / "metadata.json").read_text())
        entries = [
            entry
            for versions in index.get("entries", {}).values()
            for entry in versions
            if entry.get("book_path") == epub_path and entry.get("status") == "finished"
        ]
        if not entries:
            return None
        newest = max(entries, key=lambda entry: entry.get("mtime", 0))
        manifest_path = cache_dir / "f" / newest["path"] / "calibre-book-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if manifest["spine"] != chapter_names:
            return None
        return [int(manifest["files"][name]["length"]) for name in manifest["spine"]]
    except (OSError, KeyError, TypeError, ValueError):
        return None


@lru_cache(maxsize=32)
def _load_book(epub_path: str, mtime: float) -> Book | None:
    try:
        return Book(epub_path)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return None


def book_progress(
    epub_path: str | None, cfi: str | None, cache_dir: Path = VIEWER_CACHE
) -> float | None:
    """Fraction of the book before the saved position, or None."""
    if not epub_path or not cfi or not epub_path.lower().endswith(".epub"):
        return None
    position = parse_cfi(cfi)
    if position is None:
        return None
    try:
        mtime = Path(epub_path).stat().st_mtime
    except OSError:
        return None
    book = _load_book(epub_path, mtime)
    if book is None:
        return None
    return book.progress(position, viewer_lengths(epub_path, book.names, cache_dir))


def viewer_position_at_progress(
    epub_path: str | None, progress: float, cache_dir: Path = VIEWER_CACHE
) -> str | None:
    """Calibre --open-at position for a 0-100 progress value in an EPUB.

    The viewer measures positions in thousands of characters. Its cached
    chapter lengths give the closest match to its own percentage display.
    """
    if (
        not epub_path
        or not epub_path.lower().endswith(".epub")
        or not 0 <= progress <= 100
    ):
        return None
    try:
        mtime = Path(epub_path).stat().st_mtime
    except OSError:
        return None
    book = _load_book(epub_path, mtime)
    if book is None:
        return None
    lengths = viewer_lengths(epub_path, book.names, cache_dir) or book.lengths
    total = sum(lengths)
    return str(round(progress * total / 100_000, 3)) if total else None

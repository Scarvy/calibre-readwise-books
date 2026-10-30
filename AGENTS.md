# Project guidance

This repository builds a Calibre GUI plugin that records viewer sessions and optionally sends sessions, highlights, and notes to Readwise Books. The installed plugin uses Calibre's bundled Python 3.11. Keep plugin runtime code compatible with it and avoid third party runtime dependencies.

## Code map

- `plugin/ui.py`: Calibre action, menus, sync window, and background polling.
- `plugin/metadata_sync.py`: Readwise metadata fetch and Calibre custom columns.
- `src/calibre_bookwise/live.py`: viewer position and window tracking; session records.
- `src/calibre_bookwise/sessions.py`: rules for grouping reading events into sessions.
- `src/calibre_bookwise/progress.py`: EPUB CFI parsing and progress estimates.
- `src/calibre_bookwise/matching.py`: Calibre to Readwise Books matching and book creation plans.
- `src/calibre_bookwise/session_sync.py`: session upload state and Readwise Books POSTs.
- `src/calibre_bookwise/highlights.py`: highlight upload state and Readwise API calls.
- `src/calibre_bookwise/metadata.py`: custom column values.
- `scripts/build_plugin.py`: packages `plugin/` and its listed shared modules into `dist/readwise-books.zip`.
- `logger/`, `src/calibre_bookwise/report.py`, `src/calibre_bookwise/match_report.py`, and `scripts/sync_dry_run.py`: diagnostics, outside the installed plugin.

## Behavior to preserve

- Calibre's viewer writes `last-read` positions and highlights in `viewer/annots/<sha256(book path)>.json`. A position is an EPUB CFI, and a saved file can contain a stale imported bookmark. Track timestamps, not file changes alone, to distinguish reading from highlight edits.
- On macOS, viewer processes can outlive their windows. Session close detection uses window IDs. The viewer check runs off the GUI thread.
- A session ends on viewer close, a long idle gap, a later session for the same book, or manual end. The default idle limit is five minutes; a focused slow page can extend to the ten minute dwell limit. Sessions shorter than one minute or without usable progress are skipped.
- Progress uses the viewer cache's chapter lengths when available, with EPUB text lengths as a fallback. Readwise progress is a percentage; opening at that value is approximate.
- Session uploads are off by default. Enabling automatic uploads affects newly finished sessions; older local sessions need explicit selection. Feed posting is a separate, off by default choice captured when each session is queued. Already queued sessions keep their choice.
- Readwise Books session POSTs are not deduplicated. Persist `sending` before the request and mark uncertain outcomes `needs review`; never retry those automatically. Preserve `session_uploads.json` across restarts.
- Highlight uploads are one way from Calibre to Readwise. Baseline existing annotations on first enable, retain upload state in `highlights.json`, and do not reset unreadable state: losing IDs can duplicate uploads. Validation errors require an explicit retry.
- Metadata sync uses a GET only client and writes Calibre custom columns. It must not modify Readwise Books.

## Checks

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check .
uv run python scripts/build_plugin.py
```

Calibre and Qt imports in `plugin/` are available only inside Calibre; `pyproject.toml` configures the type checker accordingly. Test pure logic in `src/calibre_bookwise/` where possible.

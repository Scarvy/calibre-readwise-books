# calibre-bookwise

Goal: record reading done in Calibre's e-book viewer as Readwise Bookwise reading sessions.

Current stage: local session recording, optional Readwise Books session upload, read-only metadata sync, and optional one-way upload of new highlights and notes.

## What we know (Calibre 8.14, macOS)

- The viewer writes one file per book: `~/Library/Preferences/calibre/viewer/annots/<sha256(book file path)>.json`. It holds highlights plus a single `last-read` entry (`pos` as an EPUB CFI, `timestamp`), overwritten on each save.
- **The position is saved on every page turn (1–4s after it), when a book opens, and when it closes. Idle time produces no saves.** First test, 2026-10-28: 14 saves with gaps matching the page turns exactly, and none during 75s idle or 30s in Finder.
- Some EPUBs carry an old position in `META-INF/calibre_bookmarks.txt`. The viewer imports it on first open with its **original** timestamp (seen: April 2025), so saves timestamped minutes or more before the logger saw them are dropped as stale.
- The library's `metadata.db` has no `last-read` rows. The book ID comes from the `(N)` suffix on the book's folder.
- The viewer's process doesn't reliably mean a book is open. A viewer launched from the Calibre app runs as `ebook-viewer.app/.../calibre-parallel` and can outlive its window, so the logger counts viewer windows instead.
- Calibre 8.14 has no `reading_progress()` template function and no reading-progress column.
- Calibre bundles Python 3.11, so the code here targets 3.11 and uses only the standard library.

## Scenario results (2026-10-28, scripted with `logger/scenarios.sh`, books opened via the Calibre app)

| Scenario | Result with 5-min idle limit |
|---|---|
| two books | Each book in its own viewer process and window; saves stay per book, so two correct sessions. Paging back gives falling progress (93% → 71%). |
| normal read | Page turns 30–73s apart: one 8:40 session, as expected. |
| long dwell | 7 min on one page: one 8:55 session with the dwell rule (without it, two sessions and the dwell time lost). |
| away and back | 15 min in Finder correctly split it into two sessions. |

Open issues:
- Progress can go down (paging back, jumps). Check how Readwise treats an end progress lower than the start.
- Progress moves by chapter, so 10 page turns can show as 13% → 15%.

## Session rule (`src/calibre_bookwise/sessions.py`)

A session is a run of saves for one book, with no gap longer than the idle limit (default 5 minutes). A longer gap still counts as reading (a slow page) if it's within the dwell limit (default 10 minutes) and the viewer kept focus throughout, allowing up to 30s unfocused for blips. Logs without focus data use the idle limit alone. Start and end progress come from the first and last saves that have one. The open questions below decide whether this rule holds up.

## Progress (`src/calibre_bookwise/progress.py`)

The saved position is an EPUB CFI such as `epubcfi(/22/2/4/2[id]/130/2[id]:58)`. The first step is the chapter file; the rest locate the paragraph and character. Progress is the chapters before it, plus the fraction of this chapter's text before the position.

Chapters are weighted by the lengths Calibre's viewer stores in its cache (`~/Library/Caches/calibre/ev2`, found via `metadata.json` by book path), so the % matches what the viewer shows. Calibre counts each image as roughly 1000 characters, so plain text weighting reads low on figure-heavy books. Checked 2026-10-29 on *Design for Developers*: the viewer showed 33%, cache-weighted gave 32.3%, text-only gave 29.0%. Without a matching cache entry, chapters are weighted by text length.

## Book matching (`src/calibre_bookwise/matching.py`)

Sessions need a Bookwise book id. Each Calibre book (title, authors, ISBN/ASIN from `metadata.db`) is matched in order: a saved mapping, ISBN (normalised to ISBN-13), ASIN, then main title plus an author surname. Bookwise often shortens titles ("JavaScript" for *JavaScript: The Good Parts*, "Code Complete" for *Code Complete, 2nd Edition*), so exact titles would miss. Unmatched books are planned as `books_create_books` items with status `currently_reading`: by ISBN, else ASIN, else title + author. Books with none of those can't be created.

Dry run, nothing sent (the Bookwise side is a saved `books_list_books` result in the git-ignored `snapshots/`):

```sh
uv run calibre-bookwise-match --snapshot snapshots/bookwise-library.json [CALIBRE_ID ...]
```

## Collecting data

```sh
uv run logger/watch_viewer.py --label "long dwell"   # Ctrl-C to stop
uv run calibre-bookwise-report logger/logs/*.jsonl   # sessions per log
uv run calibre-bookwise-report logger/logs/*.jsonl --idle-minutes 2 --dwell-minutes 0   # compare limits
```

Open books from the Calibre library so they map to library IDs. Use one label per scenario:

| Label           | What to do                                              | What it tells us                                 |
| --------------- | ------------------------------------------------------- | ------------------------------------------------ |
| `normal read`   | Read 15–20 min as usual                                 | Real gaps between page turns                     |
| `long dwell`    | Stay on one dense page or figure for 6+ min             | Whether a 5-min idle limit splits real reading   |
| `away and back` | Read, leave the viewer open 20+ min, come back and read | Whether the split lands in the right place       |
| `two books`     | Alternate between two books (two windows)               | Per-book separation, window events               |
| `scroll mode`   | Switch the viewer to flow/scroll mode and scroll        | Whether scrolling saves like page turns          |
| `jumps`         | Use the TOC, search, or go back                         | Whether big jumps distort progress               |
| `highlights`    | Add and delete highlights while reading                 | `annotations_changed` events, no fake page turns |
| `pdf`           | Read a PDF from the library                             | Whether PDFs save positions the same way         |
| `sleep`         | Close the laptop lid or lock the screen mid-session     | Wake behaviour and timestamp gaps                |

## Calibre plugin

The plugin runs the live tracker (`src/calibre_bookwise/live.py`) inside Calibre, polling the viewer's saved positions every 2 seconds. It adds a **Readwise Books** toolbar button (icon: the Bookwise app icon, `plugin/images/readwise-books.png`). Its menu opens the sync window, ends the current session, controls automatic uploads, syncs library metadata, and holds token and diagnostic settings. The sync window has **Sessions** and **Highlights** tabs with their payloads and send controls.

```sh
uv run python scripts/build_plugin.py         # -> dist/readwise-books.zip
calibre-customize -a dist/readwise-books.zip  # install, then restart Calibre
calibre-customize -r "Readwise Books"         # uninstall
```

Data lives in Calibre's settings folder, not this repo: `~/Library/Preferences/calibre/plugins/readwise_books/` holds `events.jsonl` (saves, focus changes, manual ends), `sessions.jsonl` (finished sessions), `state.json`, and `session_uploads.json` (session mode and delivery state).

A session finishes once it can't grow any more: its viewer window closed, idle past the dwell limit (10 min), a later session of the same book started, or **End session now**. Sessions under a minute are kept but marked `skipped`.

Close detection (macOS) tracks viewer **window ids**, not counts: a minimized window or one on another Space is off screen but keeps its id; only closing removes it (checked 2026-10-29). Closing saves the position just before the window goes, so a closed window ends the session of the book with the most recent save, which handles two books open at once. The check takes ~150 ms, so the plugin polls on a background thread.

### Session mode

**Readwise Books → Automatic uploads → New reading sessions** is off by default. With it
off, sessions are recorded locally and the **Sessions** tab shows a dry-run payload.
Turn it on to send **newly finished** sessions to the [Readwise Books API](https://readwise.io/books_api#sessions-create).
Previously recorded dry-run sessions stay local. Turning the switch off keeps
sessions that have not yet been sent local as well. The mode survives restarts.
To upload older local sessions without changing modes, open **Open sync window → Sessions**.
Select one or more `local` rows (Command-click or Shift-click) and choose
**Send selected local sessions**, or choose **Send all local sessions** to queue
every eligible local session in the active Calibre library. Sessions from other
libraries and sessions that fail the duration or progress rules are excluded.

The plugin matches a book in your Readwise Books library, or creates it when
its first session is sent. Sessions are sent with `publish_to_feed: false`, so
they do not appear in the feed; they still count toward Readwise reading time,
XP, and stats. Sessions shorter than one minute or missing progress stay skipped.
The **Sessions** tab shows `local`, `pending`, `uploaded`, or `needs review`.
Readwise does not detect duplicate session requests. If a POST result is
uncertain, the plugin marks the session `needs review` and stops automatic
retrying. Check Readwise before using **Retry selected after review** in the
**Sessions** tab; retrying an existing session creates a duplicate. Keep
`session_uploads.json` to preserve the delivery history.

## Readwise metadata sync (read-only)

**Readwise Books → Sync library metadata from Readwise** fills 11 custom columns in the Calibre library from your Readwise Books library: Readwise status ("Not in Readwise" when it isn't there), rating, review, started, finished, progress, minutes read, sessions, last read, pages, words. The first run asks before creating the columns; Calibre then needs a restart. Set the token under **Settings → Set Readwise token…** (stored in Calibre's `plugins/readwise_books.json`).

If the selected book's Readwise progress changes during a metadata sync, the plugin offers to open its EPUB near that percentage. You can decline and keep your current place. **Readwise Books → Open selected book at Readwise progress…** offers the same choice later. This requires an EPUB copy; the location is approximate because Readwise supplies a percentage, not an EPUB position.

`readwise_api.ReadOnlyClient` only has GET methods (`GET /books`, `GET /sessions`), so the sync can't change anything in Readwise. Books are matched as in `matching.py`; ISBN/ASIN matches are remembered in `book_map.json`. Column values come from `metadata.column_updates`.

## Highlight and note upload v0 (opt-in)

Choose **Readwise Books → Open sync window → Highlights** to see saved highlights
and notes in the active library, newest first. The table refreshes within about
half a second of a viewer save. Select a row to inspect its exact Readwise
request body. Viewing the table needs no token and sends nothing.

To import older `local` highlights, Command-click or Shift-click rows and press
**Send selected local highlights**, or press **Send all local highlights** for
every eligible saved highlight in the active library. These buttons work even
when automatic upload is off. They queue one request per poll and keep the
automatic upload setting unchanged. Highlights without a Calibre annotation ID
cannot be queued.

Set your token with **Settings → Set Readwise token…**, then check **Automatic uploads → New highlights and notes** in the Readwise Books menu. This writes to Readwise using its [highlights
API](https://readwise.io/api_deets); the metadata client remains GET-only.

- Enabling takes a baseline of existing viewer annotation IDs: old highlights and
  edits to those highlights are not automatically imported. They can be selected
  in the table for manual upload. New IDs observed after enabling are queued.
  Imported annotations appearing later also count as new IDs.
- A new highlight uploads its text, title, authors, timestamp (when available), and
  attached note after five seconds without changes. Notes added, edited, or cleared
  later on highlights uploaded by this plugin are sent with PATCH. Text edits after
  upload, colors, tags, and exact EPUB CFI locations are outside v0.
- This is Calibre → Readwise only. Readwise edits are not pulled back; a later
  Calibre note edit replaces the Readwise note. Removing a queued highlight cancels
  its upload; removing an uploaded highlight does not delete it from Readwise.
- Unchecking pauses automatic uploads; explicitly selected highlights still send.
  Re-enabling baselines highlights created while disabled; previously queued
  automatic items resume. The enabled setting and queue survive restarts.
- The active library's books are identified by their viewer annotation path hashes;
  queued books from other libraries wait until that library is selected again.

**Settings → Show highlight upload status** displays queue counts and errors. Queue, annotation
IDs, returned Readwise IDs, and retry deadlines live in `highlights.json` in the
plugin data folder (including highlight text and notes). Keep this file to preserve
sync history. Unreadable queue JSON disables highlight sync rather than resetting it.
Readwise 400/422 validation responses show as `rejected` with the returned error
in the Highlights tab. They do not retry until you select the row and click
**Retry rejected**. Other failed uploads retry after 60 seconds; rate limits honor `Retry-After`, without
sleeping on the GUI thread. Requests use a 30-second timeout and one item per poll.
Oversize text/notes (over 8,191 characters) are shown as invalid, never truncated.
Empty notes are omitted from create requests.

The API deduplicates by title/author/text/source URL. This protects ordinary retries,
but identical passages in the same book can collapse into one highlight. If a retry
returns no unique ID, the item stays pending with an error; notes cannot be updated
until a remote ID is known. This is not an exactly-once delivery guarantee.

Book grouping uses title and author with `category: books` and `source_type: calibre`.
The create endpoint does not accept a Readwise Books/Bookwise ID; association with
existing Books entries needs live verification. Locations use Readwise's default
order, not page numbers. Historical highlights require explicit selection; no
Readwise deletions are performed.

Manual validation before release (requires Calibre and a test Readwise account):

1. Install the built plugin and enable upload with an existing highlight present;
   confirm it is skipped.
2. Add a new highlight and note in the ebook viewer; confirm one Readwise highlight
   appears after the viewer saves. Edit/clear its note and confirm the update.
3. Restart Calibre and confirm no duplicate; check the status menu for errors.
4. Pause uploads and create another highlight; re-enable and confirm it is skipped.
5. Remove an uploaded highlight in Calibre and confirm Readwise retains it.
6. With automatic upload off, select one old local highlight in the table and
   send it; confirm other local highlights remain local. Then send all remaining
   local highlights and confirm no duplicate requests after restarting Calibre.

## Plan

1. ~~Logger, session rules, precise progress, book matching (dry run)~~
2. ~~Plugin v0: record sessions in Calibre, end on viewer close, manual end~~
3. ~~**Readwise metadata sync (read-only)**~~: a GET-only Readwise client fills custom columns in Calibre from your Readwise Books library: status (or "Not in Readwise"), rating, review, started/finished dates, Bookwise progress, total reading time, sessions, last read, pages, words. Nothing is sent to Readwise.
4. ~~Session upload: send new finished sessions with `POST /sessions`, dry run on by default; create books on their first session.~~
5. Later, maybe: two-way sync (rating/status edited in Calibre pushed to Readwise).

## Layout

- `logger/watch_viewer.py`: standalone, read-only logger (PEP 723 script, stdlib only)
- `src/calibre_bookwise/sessions.py`: turns saves into sessions; pure and tested
- `src/calibre_bookwise/report.py`: `calibre-bookwise-report` CLI
- `tests/`: `uv run pytest`

Checks: `uv run ruff format . && uv run ruff check . && uv run ty check && uv run pytest`

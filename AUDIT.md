# Repository audit — 2026-11-09

Reviewed main at df03efd39fd65615c2b8e3b33f39efe671421430: runtime modules, plugin UI/workers, build/configuration, documentation and test coverage. This is a static review, not certification that every defect has been found.

## Changes in this branch

- Keep remote highlight IDs and sent-note state when explicitly retrying rejected note PATCHes. Previously the retry became a POST, risking a duplicate or an endless deduplication error.
- Keep edited rejected highlights rejected until explicit retry, as required by AGENTS.md.
- Roll back highlight enable/baseline state and session mode/queue changes in memory when saving a setting fails. A reported failed enable must not silently enable uploads.
- Choose last-read metadata by parsed datetime, not lexicographic ISO strings (offsets can reverse their apparent order).
- Reject nonfinite Retry-After values and do not sleep after the final failed GET attempt.
- Correct the UI module's stale claim that reading sessions cannot be uploaded.
- Add focused regression tests for these behaviors.

## Remaining findings for review

### High priority

1. **Account ownership of upload queues** — highlights.json stores remote highlight IDs without a Readwise account scope. Changing the token can PATCH an old account's ID using the new credential; pending creates/sessions also follow whichever account is currently configured. Scoped book links do not scope delivery state. Decide whether changing accounts pauses old queues or creates separate account queues; migration must preserve IDs and avoid resending uploaded records. See highlights.py, session_sync.py, ui.py.

2. **Tracking waits for network work** — ui.py runs tracker.poll, session sync, then highlight sync in one background pass. poll refuses another pass while that thread is busy. A 30-second request or repeated GET rate-limit sleeps can miss focus/window transitions; delays over the two-minute stale limit can discard real position saves. Split frequent local capture from serialized network delivery, with safe shutdown and library/account snapshots.

3. **Uncertain EPUB uploads are not persisted** — UploadClient returns processing after bounded polling; the UI retains only a book ID in an in-memory processing set. Completion errors show the upload ID but do not persist a recovery record. Restarting loses this context and the exact upload-to-book association may never be saved. Add a persistent pending-upload journal and GET-only recovery before allowing another upload.

4. **Book creation crash window** — session sync persists sending before session POST, but not before create_book. A crash after book creation and before saving its ID bypasses the needs-review mechanism. The Books endpoint's matching may prevent some duplicates, but this needs explicit recovery semantics and a crash test.

### Behavior and correctness

5. **Queued highlights can become stale with automatic sync off** — poll skips annotation reads entirely when disabled, including manual items. A queued manual highlight edited or removed during an offline/retry interval can later send its old text/note. Re-read existing queued IDs before sending without discovering or auto-queuing new IDs.

6. **EPUB upload eligibility conflates metadata and files** — EbooksPanel.uploadable only allows absent/deleted library entries. A catalog/API-created book can already be in Readwise but still lack an uploaded EPUB, so the table blocks the very upload needed. The separate toolbar action still permits it. Verify how the upload API associates files with existing entries before broadening the table action.

7. **Request preview differs from delivery** — HighlightsPanel displays the create request from preview_highlights even for an existing remote highlight whose pending operation is a note PATCH. Generate previews from the queued operation and known remote ID.

8. **Book index freshness** — index_new_books only rebuilds when it sees an unknown annotation path. Existing Calibre title/author/identifier edits may leave self.books stale indefinitely; newly added books with no annotations can also be omitted until indexing is triggered. Subscribe to relevant database changes or invalidate deliberately.

9. **Corrupt and unreadable state recovery** — state validation is shallow (e.g. SessionSync item values are not checked before .get), while live._read_jsonl treats any OSError as an empty log and skips malformed lines throughout the file, not just a crash-truncated tail. Unreadable finished-session logs can undermine deduplication. Define recovery/quarantine rules; fail closed on permission/I/O errors without deleting history.

### Efficiency and maintenance

10. **Polling scales with history/library size** — automatic highlights rereads every annotation and rewrites the entire queue every pass even when unchanged. SessionSync registers/scans all finished sessions every pass; SessionsPanel rebuilds all table cells every refresh. Profile a large library and use file stamps, dirty writes, incremental registration and UI signatures.

11. **Progress caching and EPUB edge cases** — live._remembered retains every (path, CFI) without a bound or file-version key; values can outlive an EPUB replacement. OPF hrefs are not URL-decoded, so chapters with percent-encoded filenames can fail to load. Add representative real-EPUB fixtures before changing CFI/progress behavior.

12. **Platform and lifecycle contract** — registration advertises Windows/Linux while README requires macOS and only macOS supplies window/focus detection. Worker shutdown also stops only the timer, leaving in-flight Python/QThread work. Decide supported platforms and worker shutdown/cancellation behavior.

No runtime module was removed: each currently has a caller or build role. The legacy sign-in cleanup is a migration; removing it requires deciding how long old installations remain supported.

## Validation limits

The selected execution environment failed before review. No pytest, Ruff, ty, build or Calibre GUI checks could be run. Regression tests are added but unexecuted. No API writes to Readwise were made.

Before merge, run:

~~~sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check .
uv run python scripts/build_plugin.py
~~~

Then manually exercise rejected-note retry, toggles on a failing/unwritable data directory, and timezone-offset metadata in Calibre. Account changes, asynchronous upload completion and worker shutdown need dedicated integration testing.

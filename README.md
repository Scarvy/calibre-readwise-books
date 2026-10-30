# Readwise Books for Calibre - Track reading sessions with Readwise Books

> [!NOTE]
> A very vibe-coded piece of software... but it works! In that case, use at your own risk and be aware it may have bugs. 
> 
> If more people use it I'll consider giving it more love and care (e.g. fix bugs, upstream features etc.).
>
> If you try it, let me know how it goes. Bug reports and feature ideas are welcome in the Issues tracker.

A Calibre plugin that records reading sessions in the e-book viewer and syncs sessions, highlights, and notes to Readwise Books. It can also pull Readwise Books metadata into Calibre and offer to open an EPUB near your Readwise reading progress. Uploads are optional and off by default.

**Requirements:** Calibre 8 or later on macOS. Session tracking currently uses macOS viewer window detection.

## Install

Download [readwise-books.zip](https://github.com/Scarvy/calibre-readwise-books/releases/latest/download/readwise-books.zip). In Calibre, choose **Preferences → Plugins → Load plugin from file**, select the zip, and restart Calibre.

To build the zip from source instead:

```sh
uv run python scripts/build_plugin.py
```

Add **Readwise Books** to a Calibre toolbar if it is not visible. To sync with Readwise, set your [access token](https://readwise.io/access_token) under **Readwise Books → Settings → Set Readwise token…**.

## Use

Open a book from your Calibre library in the e-book viewer. The plugin records finished reading sessions locally.

- **Sync settings → Sync reading sessions** sends newly finished sessions to Readwise Books, where they appear in the book's timeline. Earlier local sessions can be sent from the Sessions tab.
- **Sync settings → Post to feed** adds newly uploaded sessions to your Readwise Books feed. It is off by default and does not send sessions by itself.
- **Sync settings → Sync highlights and notes** uploads new highlights from the e-book viewer. Earlier highlights can be sent from the Highlights tab. Notes on highlights uploaded by this plugin update when edited in Calibre.
- **Sync library metadata from Readwise** fills Calibre custom columns. On the first run, Calibre must restart after creating them. If the selected book's Readwise progress changed, you can choose to open its EPUB near that percentage. The position is approximate.

Sync is one way for highlights and notes; deleting a highlight in Calibre does not delete its Readwise copy. TODO: see if it can be a two-way sync.

## Debugging

Curious how sync works, or investigating an issue? Choose **Readwise Books → Settings → Developer → Open sync window** to review sessions and highlights in separate tabs, inspect the payload each upload would send, and send selected or all eligible local items. The Developer menu also has highlight upload status and the plugin data folder.

If a session shows **needs review**, check Readwise before retrying to avoid a duplicate.

## Contribute

Issues and pull requests are welcome. The code map and implementation rules are in [AGENTS.md](AGENTS.md). Run the checks before opening a pull request:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check .
```

For local diagnostics, use the scripts directly:

```sh
uv run python scripts/report.py logger/logs/*.jsonl
uv run python scripts/match_report.py --snapshot snapshots/bookwise-library.json
```

The match report requires a local JSON snapshot of your Readwise Books library; neither command uploads data.

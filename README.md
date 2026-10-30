# Readwise Books for Calibre

A Calibre plugin that records reading sessions in the e-book viewer and syncs sessions, highlights, and notes to Readwise Books. It can also pull Readwise Books metadata into Calibre and offer to open an EPUB near your Readwise reading progress. Uploads are optional and off by default.

**Requirements:** Calibre 8 or later on macOS. Session tracking currently uses macOS viewer window detection.

## Install

Build and install the plugin, then restart Calibre:

```sh
uv run python scripts/build_plugin.py
calibre-customize -a dist/readwise-books.zip
```

Add **Readwise Books** to a Calibre toolbar if it is not visible. To sync with Readwise, set your [access token](https://readwise.io/access_token) under **Readwise Books → Settings → Set Readwise token…**.

## Use

Open a book from your Calibre library in the e-book viewer. The plugin records finished reading sessions locally. Choose **Readwise Books → Open sync window** to review sessions and highlights in separate tabs, inspect their request payloads, and send selected or all eligible local items.

- **Automatic uploads → New reading sessions** sends newly finished sessions. Earlier local sessions stay local until you send them from the Sessions tab.
- **Post uploaded sessions to feed** controls whether newly queued session uploads appear in your Readwise Books feed. It is off by default and available in the menu and Sessions tab. Turning it on does not send anything by itself.
- **Automatic uploads → New highlights and notes** sends new highlights. Earlier highlights can be sent from the Highlights tab. Notes on highlights uploaded by this plugin update when edited in Calibre.
- **Sync library metadata from Readwise** fills Calibre custom columns. On the first run, Calibre must restart after creating them. If the selected book's Readwise progress changed, you can choose to open its EPUB near that percentage.

Sync is one way for highlights and notes; deleting a highlight in Calibre does not delete its Readwise copy. If a session shows **needs review**, check Readwise before retrying to avoid a duplicate.

## Contribute

Issues and pull requests are welcome. The code map and implementation rules are in [AGENTS.md](AGENTS.md). Run the checks before opening a pull request:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check .
```

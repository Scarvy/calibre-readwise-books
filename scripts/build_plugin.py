"""Build the Calibre plugin zip: plugin/ plus the shared runtime modules.

Usage:  uv run python scripts/build_plugin.py   -> dist/readwise-books.zip
Install: calibre-customize -a dist/readwise-books.zip  (then restart Calibre)
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORT_NAME = "readwise_books"  # plugins import as calibre_plugins.<this>
PLUGIN_FILES = ["__init__.py", "ui.py", "metadata_sync.py", "images/readwise-books.png"]
SOURCE_DIR = ROOT / "src" / "calibre_bookwise"


def build(output: Path = ROOT / "dist" / "readwise-books.zip") -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as plugin:
        plugin.writestr(f"plugin-import-name-{IMPORT_NAME}.txt", "")
        for name in PLUGIN_FILES:
            plugin.write(ROOT / "plugin" / name, name)
        for module in sorted(SOURCE_DIR.glob("*.py")):
            if module.name != "__init__.py":
                plugin.write(module, module.name)
    return output


if __name__ == "__main__":
    print(build())

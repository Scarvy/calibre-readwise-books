"""Build the Calibre plugin zip: plugin/ plus the shared runtime modules.

Usage:  uv run python scripts/build_plugin.py             -> dist/readwise-books-dev.zip
        uv run python scripts/build_plugin.py --release   -> dist/readwise-books.zip
"""

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORT_NAME = "readwise_books"  # plugins import as calibre_plugins.<this>
PLUGIN_FILES = ["ui.py", "metadata_sync.py", "images/readwise-books.png"]
DEV_LINE = "IS_DEV = True"  # in plugin/__init__.py; release builds flip it
SOURCE_DIR = ROOT / "src" / "calibre_bookwise"
NOT_PACKAGED = {"__init__.py", "diagnostics.py"}  # diagnostics serve scripts/ only


def build(output: Path | None = None, release: bool = False) -> Path:
    output = output or ROOT / "dist" / (
        "readwise-books.zip" if release else "readwise-books-dev.zip"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as plugin:
        plugin.writestr(f"plugin-import-name-{IMPORT_NAME}.txt", "")
        plugin.writestr("__init__.py", _registration(release))
        for name in PLUGIN_FILES:
            plugin.write(ROOT / "plugin" / name, name)
        for module in sorted(SOURCE_DIR.glob("*.py")):
            if module.name not in NOT_PACKAGED:
                plugin.write(module, module.name)
    return output


def _registration(release: bool) -> str:
    """plugin/__init__.py with this build's IS_DEV."""
    source = (ROOT / "plugin" / "__init__.py").read_text()
    if source.count(DEV_LINE) != 1:
        raise SystemExit(f"plugin/__init__.py must contain one '{DEV_LINE}' line")
    return source.replace(DEV_LINE, f"IS_DEV = {not release}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, help="write the plugin zip here")
    parser.add_argument(
        "--release", action="store_true", help="build the unmarked release plugin"
    )
    args = parser.parse_args()
    print(build(args.output, args.release))

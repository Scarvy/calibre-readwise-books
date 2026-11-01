"""Build the Calibre plugin zip: plugin/ plus the shared runtime modules.

Usage:  uv run python scripts/build_plugin.py             -> dist/readwise-books-dev.zip
        uv run python scripts/build_plugin.py --release   -> dist/readwise-books.zip
"""

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORT_NAME = "readwise_books"  # plugins import as calibre_plugins.<this>
PLUGIN_FILES = ["__init__.py", "ui.py", "metadata_sync.py", "images/readwise-books.png"]
SOURCE_DIR = ROOT / "src" / "calibre_bookwise"
NOT_PACKAGED = {"__init__.py", "diagnostics.py"}  # diagnostics serve scripts/ only


def build(output: Path | None = None, release: bool = False) -> Path:
    channel = "release" if release else "development"
    output = output or ROOT / "dist" / (
        "readwise-books.zip" if release else "readwise-books-dev.zip"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as plugin:
        plugin.writestr(f"plugin-import-name-{IMPORT_NAME}.txt", "")
        for name in PLUGIN_FILES:
            plugin.write(ROOT / "plugin" / name, name)
        plugin.writestr(
            "build_info.py",
            f'BUILD_CHANNEL = "{channel}"\nIS_DEV = BUILD_CHANNEL == "development"\n',
        )
        for module in sorted(SOURCE_DIR.glob("*.py")):
            if module.name not in NOT_PACKAGED:
                plugin.write(module, module.name)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, help="write the plugin zip here")
    parser.add_argument(
        "--release", action="store_true", help="build the unmarked release plugin"
    )
    args = parser.parse_args()
    print(build(args.output, args.release))

"""Build the Calibre plugin zip: plugin/ plus the calibre_bookwise modules it uses.

Usage:  uv run python scripts/build_plugin.py   -> dist/bookwise-sessions.zip
Install: calibre-customize -a dist/bookwise-sessions.zip  (then restart Calibre)
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORT_NAME = "bookwise_sessions"  # plugins import as calibre_plugins.<this>
PLUGIN_FILES = ["__init__.py", "ui.py"]
# Library modules the plugin needs (not the CLI-only report/match_report).
MODULES = ["bookwise.py", "live.py", "matching.py", "progress.py", "sessions.py"]


def build(output: Path = ROOT / "dist" / "bookwise-sessions.zip") -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as plugin:
        plugin.writestr(f"plugin-import-name-{IMPORT_NAME}.txt", "")
        for name in PLUGIN_FILES:
            plugin.write(ROOT / "plugin" / name, name)
        for name in MODULES:
            plugin.write(ROOT / "src" / "calibre_bookwise" / name, name)
    return output


if __name__ == "__main__":
    print(build())

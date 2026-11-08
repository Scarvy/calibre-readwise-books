import subprocess
import sys
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_plugin.py"


def test_build_embeds_distinct_development_and_release_channels(tmp_path):
    for release, expected in ((False, "development"), (True, "release")):
        path = tmp_path / f"{expected}.zip"
        subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(path)]
            + (["--release"] if release else []),
            check=True,
            capture_output=True,
        )
        with zipfile.ZipFile(path) as plugin:
            registration = plugin.read("__init__.py").decode()
            assert f"\nIS_DEV = {not release}  #" in registration
            assert plugin.namelist().count("__init__.py") == 1
            # Installing over an older copy re-runs only __init__.py, so it
            # must not import plugin modules that could be the old copy's.
            assert "calibre_plugins" not in registration.replace(
                "calibre_plugins.readwise_books.ui:", ""
            )
            assert plugin.read("plugin-import-name-readwise_books.txt") == b""
            assert "matching.py" in plugin.namelist()

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
            assert plugin.read("build_info.py") == (
                f'BUILD_CHANNEL = "{expected}"\n'
                'IS_DEV = BUILD_CHANNEL == "development"\n'.encode()
            )
            assert plugin.read("plugin-import-name-readwise_books.txt") == b""
            assert plugin.namelist().count("build_info.py") == 1
            assert "matching.py" in plugin.namelist()
            assert "diagnostics.py" not in plugin.namelist()

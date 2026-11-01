"""Files the plugin keeps in its data directory.

Stdlib only: this has to run inside Calibre's bundled Python 3.11.
"""

import json
from pathlib import Path


def atomic_write_json(path: Path, value: object, **dumps_options):
    """Replace path with value as JSON; a crash leaves the old file or the new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, **dumps_options))
    temporary.replace(path)

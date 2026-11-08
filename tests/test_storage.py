import json

from calibre_bookwise.storage import atomic_write_json


def test_atomic_write_replaces_file_and_leaves_no_temporary(tmp_path):
    path = tmp_path / "data" / "state.json"
    atomic_write_json(path, {"old": True})
    atomic_write_json(path, {"new": True}, indent=2)

    assert json.loads(path.read_text()) == {"new": True}
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]

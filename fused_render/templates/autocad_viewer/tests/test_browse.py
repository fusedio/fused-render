"""Unit tests for the folder lister behind the viewer's Open panel
(`autocad_viewer/browse.py`)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.dirname(HERE))  # import the sibling browse.py

import browse  # noqa: E402


def test_lists_only_cad_files():
    out = browse.main(FIX)
    names = {f["name"] for f in out["files"]}
    assert {"floorplan.dxf", "crafted.dxf", "binary.dxf", "fake.dwg"} <= names
    assert not any(n.endswith(".json") for n in names)  # sidecars excluded
    assert "error" not in out


def test_files_carry_forward_slash_path_and_size():
    out = browse.main(FIX)
    f = next(f for f in out["files"] if f["name"] == "floorplan.dxf")
    assert "\\" not in f["path"] and f["path"].endswith("/floorplan.dxf")
    assert f["size"] > 0


def test_result_is_sorted_case_insensitively():
    names = [f["name"] for f in browse.main(FIX)["files"]]
    assert names == sorted(names, key=str.lower)


def test_missing_folder_is_error_not_crash():
    out = browse.main(os.path.join(FIX, "nope-does-not-exist"))
    assert out["files"] == []
    assert out["error"]

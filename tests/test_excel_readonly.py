"""Read-only-file contract for the excel template (SPEC §13.5).

reader.py is a runPython target, not a package module, so — like
test_annotate_comments.py — these load it via importlib and drive its
functions directly against tmp_path files.

Under test:
* RO-3 writer gate: `_save` refuses a chmod -w viewed file with
  PermissionError BEFORE any tmp-write (the tmp + os.replace path goes
  through the parent dir and would silently bypass the file's read-only bit).
  A NONEXISTENT dest (the save_as flow) is NOT gated — os.access(missing,
  W_OK) is False, so the gate must be existence-qualified.
* RO-4 reader verdict: `_load` folds fs writability into
  editable/readonly_message/readonly_tooltip.

Test-env note: no openpyxl/fpdf2 here, so everything runs through .csv
(stdlib csv for small saves; duckdb — which IS installed — for _load's cache).
"""

import importlib.util
import os
import stat

import pytest

# os.access always says yes for root, so the chmod-based gates can't trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _load_reader():
    path = os.path.join("fused_render", "templates", "excel", "reader.py")
    spec = importlib.util.spec_from_file_location("excel_reader", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CSV_BODY = "a,b\n1,2\n3,4\n"


def _csv(tmp_path, name="data.csv"):
    f = tmp_path / name
    f.write_text(CSV_BODY)
    return f


def _small_payload():
    return [{"name": "data", "big": False, "rows": [["x", "y"], ["9", "8"]]}]


@skip_root
def test_save_readonly_csv_raises_and_leaves_bytes_untouched(tmp_path):
    rd = _load_reader()
    f = _csv(tmp_path)
    os.chmod(f, 0o444)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            rd._save(str(f), _small_payload(), "")
        assert f.read_text() == CSV_BODY
        # no tmp turd left behind either
        assert not os.path.exists(str(f) + ".tmp")
    finally:
        os.chmod(f, stat.S_IWUSR | stat.S_IRUSR)


def test_save_to_nonexistent_dest_is_not_gated(tmp_path):
    # save_as calls _save on a fresh user-chosen dest; os.access on a missing
    # file is False, so an unqualified gate would break save_as entirely.
    rd = _load_reader()
    dest = tmp_path / "copy.csv"
    out = rd._save(str(dest), _small_payload(), "")
    assert out.get("ok") is True
    assert dest.exists()
    assert dest.read_text().splitlines() == ["x,y", "9,8"]


def test_load_writable_csv_is_editable(tmp_path):
    rd = _load_reader()
    f = _csv(tmp_path)
    res = rd._load(str(f))
    assert res["editable"] is True
    assert res["readonly_message"] == ""
    assert res["readonly_tooltip"] == ""


@skip_root
def test_load_readonly_csv_reports_readonly_verdict(tmp_path):
    rd = _load_reader()
    f = _csv(tmp_path)
    os.chmod(f, 0o444)
    try:
        res = rd._load(str(f))
        assert res["editable"] is False
        assert res["readonly_message"] == "Read-only"
        assert "read-only" in res["readonly_tooltip"]
        assert "permissions" in res["readonly_tooltip"]
        # read-only never blocks viewing: rows still come back
        assert res["sheets"] and res["sheets"][0]["rows"]
    finally:
        os.chmod(f, stat.S_IWUSR | stat.S_IRUSR)

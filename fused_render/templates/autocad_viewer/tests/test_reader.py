"""Unit tests for the stdlib DXF metadata reader (`autocad_viewer/reader.py`).

Ground truth lives in `fixtures/*.expected.json` (produced by `_gen_fixtures.py`
via ezdxf, plus a hand-written crafted fixture). Each test runs `reader.main`
against a fixture and asserts every key present in the sidecar matches — so the
stdlib scanner must independently reproduce what a real CAD library sees.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.dirname(HERE))  # import the sibling reader.py

import reader  # noqa: E402


def _data_path(sidecar_name):
    base = sidecar_name[: -len(".expected.json")]
    if not os.path.splitext(base)[1]:
        base += ".dxf"
    return os.path.join(FIX, base)


SIDECARS = sorted(f for f in os.listdir(FIX) if f.endswith(".expected.json"))


def _approx(a, b):
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return all(_approx(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == pytest.approx(b, abs=1e-3)
    return a == b


@pytest.mark.parametrize("sidecar", SIDECARS)
def test_reader_matches_ground_truth(sidecar):
    with open(os.path.join(FIX, sidecar), encoding="utf-8") as f:
        expected = json.load(f)
    got = reader.main(file=_data_path(sidecar))
    for key, want in expected.items():
        assert key in got, f"reader output missing key {key!r}"
        if key in ("extmin", "extmax"):
            assert _approx(got[key], want), f"{key}: {got[key]} != {want}"
        else:
            assert got[key] == want, f"{key}: {got[key]!r} != {want!r}"


def test_result_is_json_serializable():
    out = reader.main(file=os.path.join(FIX, "floorplan.dxf"))
    json.dumps(out)  # must not raise


def test_floorplan_details():
    out = reader.main(file=os.path.join(FIX, "floorplan.dxf"))
    assert out["format"] == "dxf" and out["supported"] is True
    assert out["entity_counts"]["DIMENSION"] == 1
    walls = next(l for l in out["layers"] if l["name"] == "WALLS")
    assert walls["color"] == 4  # cyan (ACI)
    # ezdxf clears header extents on save -> renderer computes real bounds
    assert out["extmin"] is None and out["extmax"] is None
    assert out["size"] == os.path.getsize(os.path.join(FIX, "floorplan.dxf"))


def test_crafted_has_real_extents_and_off_layer():
    out = reader.main(file=os.path.join(FIX, "crafted.dxf"))
    assert out["extmax"] == pytest.approx([250.5, 120.0, 0.0])
    hidden = next(l for l in out["layers"] if l["name"] == "HIDDEN")
    assert hidden["color"] == 3  # abs(-3): off-layer color preserved


def test_dwg_detected_but_unsupported():
    out = reader.main(file=os.path.join(FIX, "fake.dwg"))
    assert out["format"] == "dwg"
    assert out["supported"] is False
    assert out["version_name"] == "AutoCAD 2018"


def test_binary_dxf_detected_but_unsupported():
    out = reader.main(file=os.path.join(FIX, "binary.dxf"))
    assert out["format"] == "binary-dxf"
    assert out["supported"] is False


def test_missing_file_reports_error_without_raising():
    out = reader.main(file=os.path.join(FIX, "does_not_exist.dxf"))
    assert out["format"] == "unknown"
    assert out["supported"] is False
    assert "error" in out

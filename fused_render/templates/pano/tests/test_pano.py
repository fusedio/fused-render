"""Regression tests for the pano preview backend (`pano.py`).

The projection conversions (cube/little-planet/fisheye/perspective) once broke
whenever the numeric params reached `main()` as strings — an engine that
forwards URL params without coercing by annotation, where a truthy "0" string
slips straight into py360convert. These call `main()` with every numeric param
as a string and assert each mode still produces output.
"""
import os
import sys

import pytest

pytest.importorskip("numpy")
pytest.importorskip("py360convert")
Image = pytest.importorskip("PIL.Image")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pano  # noqa: E402


@pytest.fixture
def sample(tmp_path, monkeypatch):
    """A tiny 2:1 image (classifies as equirect) plus an isolated cache dir."""
    monkeypatch.setattr(pano, "CACHE_ROOT", str(tmp_path / "cache"))
    import numpy as np

    h, w = 128, 256
    lon = np.linspace(0, 255, w, dtype="uint8")[None, :].repeat(h, 0)
    lat = np.linspace(0, 255, h, dtype="uint8")[:, None].repeat(w, 1)
    arr = np.stack([lon, lat, np.full((h, w), 90, "uint8")], -1)
    path = str(tmp_path / "equirect.jpg")
    Image.fromarray(arr).save(path, quality=90)
    return path


# Every numeric param as a string, as a non-coercing engine forwards them.
STR_PARAMS = dict(fov="90", yaw="0", pitch="0", roll="0", zoom="1",
                  out_w="0", out_h="0", face_w="0")

CONVERT_MODES = ["equirect", "cube_dice", "cube_horizon", "cube_faces",
                 "little_planet", "fisheye180", "perspective"]


def test_open_classifies_equirect(sample):
    asset = pano.main(action="open", file=sample)["asset"]
    assert asset["kind"] == "equirect"
    assert asset["valid"] is True


@pytest.mark.parametrize("mode", CONVERT_MODES)
def test_convert_accepts_string_params(sample, mode):
    r = pano.main(action="convert", file=sample, mode=mode, **STR_PARAMS)
    if mode == "cube_faces":
        assert len(r["faces"]) == 6
    else:
        assert r["path"] and r["w"] > 0 and r["h"] > 0


def test_convert_respects_typed_face_size(sample):
    r = pano.main(action="convert", file=sample, mode="cube_faces", face_w=32)
    assert r["w"] == 32 and r["h"] == 32

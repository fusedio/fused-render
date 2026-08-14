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


def _open_asset(tmp_path, monkeypatch, name, w=256, h=128, fmt="JPEG", exif=None):
    """Write a synthetic image to `name` (isolated cache) and return its asset."""
    import numpy as np

    monkeypatch.setattr(pano, "CACHE_ROOT", str(tmp_path / "cache"))
    arr = np.zeros((h, w, 3), "uint8")
    arr[..., 0] = np.linspace(0, 255, w, dtype="uint8")[None, :]
    path = str(tmp_path / name)
    kwargs = {"exif": exif} if exif is not None else {}
    Image.fromarray(arr).save(path, format=fmt, **kwargs)
    return path, pano.main(action="open", file=path)["asset"]


def test_passthrough_serves_original(tmp_path, monkeypatch):
    path, a = _open_asset(tmp_path, monkeypatch, "pano.jpg")
    assert a["display"] is None                   # no re-encoded copy
    assert a["display_path"] == a["source"]       # serves the original, not a copy
    assert os.path.samefile(a["source"], path)


def test_oversized_image_is_downscaled(tmp_path, monkeypatch):
    monkeypatch.setattr(pano, "DISPLAY_MAX_W", 64)
    _, a = _open_asset(tmp_path, monkeypatch, "wide.jpg", w=256, h=128)
    assert a["display"] is not None               # re-encoded, downscaled copy
    assert a["display_w"] <= 64


def test_rotated_image_is_not_passthrough(tmp_path, monkeypatch):
    exif = Image.Exif()
    exif[0x0112] = 6                              # EXIF orientation = 90° rotate
    _, a = _open_asset(tmp_path, monkeypatch, "rot.jpg", exif=exif)
    assert a["display"] is not None               # must re-encode upright, no passthrough


def test_non_image_extension_is_not_passthrough(tmp_path, monkeypatch):
    # JPEG bytes at a path mimetypes doesn't type as image/*: /api/fs/raw would
    # serve octet-stream + nosniff, so a properly typed copy must be written.
    _, a = _open_asset(tmp_path, monkeypatch, "pano.bin", fmt="JPEG")
    assert a["display"] is not None

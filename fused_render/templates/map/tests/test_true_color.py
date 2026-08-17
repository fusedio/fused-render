"""A display-ready RGB raster must render as its own colours.

An 8-bit 3-band image (a Maxar "visual" product, a drone orthophoto, a scanned
map) already carries final pixel values. Stretching each band independently to
its own 2-98 percentile shifts the white balance and blows out saturation — the
image comes back visibly discoloured. Byte RGB therefore gets no enhancement
unless one is asked for, which is what GDAL and QGIS do.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_true_color.py -o addopts=""
"""
import importlib.util
import os
import sys

import pytest

_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_MAP), "shared")


def _load(name, filename):
    for path in (_SHARED, _MAP):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location(name, os.path.join(_MAP, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def engine():
    return _load("raster_engine", "raster_engine.py")


class FakeDataset:
    """Only the attributes the band-selection helpers read."""

    def __init__(self, colorinterp, dtypes):
        from rasterio.enums import ColorInterp

        self.colorinterp = [getattr(ColorInterp, name) for name in colorinterp]
        self.dtypes = tuple(dtypes)
        self.count = len(dtypes)


def test_rgb_bands_come_from_colour_interpretation(engine):
    # A BGRA file stores blue first; rendering bands 1,2,3 would swap red/blue.
    dataset = FakeDataset(
        ["blue", "green", "red", "alpha"], ["uint8"] * 4
    )
    assert engine._render_indexes(dataset) == [3, 2, 1]


def test_plain_rgb_uses_the_first_three_bands(engine):
    dataset = FakeDataset(["red", "green", "blue"], ["uint8"] * 3)
    assert engine._render_indexes(dataset) == [1, 2, 3]


def test_undeclared_bands_fall_back_to_the_first_three(engine):
    dataset = FakeDataset(["undefined"] * 3, ["uint16"] * 3)
    assert engine._render_indexes(dataset) == [1, 2, 3]


def test_single_band_renders_one_band(engine):
    dataset = FakeDataset(["gray"], ["float32"])
    assert engine._render_indexes(dataset) == [1]


def test_byte_rgb_is_true_colour(engine):
    dataset = FakeDataset(["red", "green", "blue"], ["uint8"] * 3)
    assert engine._is_true_color(dataset, [1, 2, 3]) is True


def test_uint16_rgb_is_not_true_colour(engine):
    # 16-bit multispectral genuinely needs a stretch to be visible at all.
    dataset = FakeDataset(["red", "green", "blue"], ["uint16"] * 3)
    assert engine._is_true_color(dataset, [1, 2, 3]) is False


def test_single_band_is_not_true_colour(engine):
    dataset = FakeDataset(["gray"], ["uint8"])
    assert engine._is_true_color(dataset, [1]) is False


def test_native_range_is_the_identity_for_byte_data(engine):
    assert engine._native_ranges(3) == [[0.0, 255.0]] * 3
    assert engine._is_native_ranges([[0.0, 255.0]] * 3) is True
    # The percentile windows measured on the real Maxar visual COG.
    assert engine._is_native_ranges([[16.0, 93.0], [20.0, 88.0], [12.0, 71.0]]) is False

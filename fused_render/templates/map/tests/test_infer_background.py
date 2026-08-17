"""Tests for _infer_background's collar detection (raster_engine.py).

Uses a fake dataset so no geo stack or network is needed: the contract under
test is that the collar verdict comes from ONE decimated whole-image read, not
from per-point full-resolution sampling — the latter cost ~20s of a remote
COG's describe and blew the 60s render budget.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_infer_background.py -o addopts=""
"""
import importlib.util
import os
import sys

import numpy as np
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


class FakeDataset:
    """Records how it was read so the test can assert the access pattern."""

    def __init__(self, array):
        self.array = array
        self.height, self.width = array.shape[1:]
        self.reads = 0
        self.samples = 0

    def read(self, indexes, out_shape=None):
        self.reads += 1
        bands, height, width = out_shape
        rows = np.linspace(0, self.height - 1, height).astype(int)
        cols = np.linspace(0, self.width - 1, width).astype(int)
        return self.array[:bands][:, rows][:, :, cols]

    def sample(self, coordinates, indexes=None):
        self.samples += 1
        raise AssertionError("must not sample points at full resolution")

    def xy(self, row, column):
        return float(column), float(row)


@pytest.fixture
def eng():
    return _load("raster_engine", "raster_engine.py")


def _scene(collar):
    """A 3-band image with real content and a `collar`-pixel zero border."""
    data = np.full((3, 2048, 2048), 200, dtype="uint8")
    if collar:
        data[:, :collar, :] = 0
        data[:, -collar:, :] = 0
        data[:, :, :collar] = 0
        data[:, :, -collar:] = 0
    return FakeDataset(data)


def test_zero_collar_is_detected_in_a_single_decimated_read(eng):
    dataset = _scene(collar=256)
    assert eng._infer_background(dataset, [1, 2, 3]) == 0.0
    assert dataset.reads == 1  # one decimated read, not 64 point samples
    assert dataset.samples == 0


def test_full_bleed_image_keeps_zero_as_valid_data(eng):
    # No collar: zero must stay meaningful data, never be masked as nodata.
    dataset = _scene(collar=0)
    assert eng._infer_background(dataset, [1, 2, 3]) is None
    assert dataset.reads == 1


def test_all_zero_image_is_not_called_a_collar(eng):
    # Border is zero, but so is the interior — there is no data to protect,
    # so the interior-nonzero half of the test must reject it.
    dataset = FakeDataset(np.zeros((3, 1024, 1024), dtype="uint8"))
    assert eng._infer_background(dataset, [1, 2, 3]) is None


def test_read_is_bounded_regardless_of_source_size(eng):
    # A 40k-pixel raster must still be probed at preview scale — the whole point
    # is that this costs a constant handful of blocks from the overview pyramid.
    dataset = _scene(collar=256)
    dataset.height = dataset.width = 40000  # claim a huge source, keep a small array
    captured = {}

    def spy(indexes, out_shape=None):
        captured["out_shape"] = out_shape
        return np.zeros((out_shape[0], out_shape[1], out_shape[2]), dtype="uint8")

    dataset.read = spy
    eng._infer_background(dataset, [1, 2, 3])
    assert max(captured["out_shape"][1:]) <= 256

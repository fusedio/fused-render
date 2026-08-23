"""Tests for the pre-download confirmation gate (raster_engine.py).

A remote raster with no overview pyramid cannot be read cheaply at any zoom, so
displaying it means downloading the whole file. Past a size threshold the engine
stops before touching a pixel and returns a ``confirm_download`` state instead of
silently pulling tens or hundreds of MB; the user accepts it through the normal
optimize endpoint. Local files and cloud-optimized sources are never gated.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_download_confirm.py -o addopts=""
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


@pytest.fixture
def re():
    return _load("raster_engine", "raster_engine.py")


@pytest.fixture
def eng(re, tmp_path):
    return re.RasterEngine(
        cache_dir=str(tmp_path), base_url="http://127.0.0.1:9999", token="tok"
    )


def _write_raster(path, *, overviews=False, tiled=False):
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds

    data = (np.random.default_rng(0).integers(1, 200, (64, 64))).astype("uint8")
    profile = dict(
        driver="GTiff", width=64, height=64, count=1, dtype="uint8",
        crs="EPSG:4326", transform=from_bounds(10, 10, 20, 20, 64, 64),
    )
    if tiled:
        profile.update(tiled=True, blockxsize=16, blockysize=16)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
        if overviews:
            dst.build_overviews([2, 4], Resampling.average)
    return str(path)


class _Inline:
    """A stand-in executor that runs the submitted work synchronously."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, *args, **kwargs):
        pass


def _as_remote(re, monkeypatch, size):
    monkeypatch.setattr(re, "is_remote_path", lambda source: True)
    monkeypatch.setattr(re, "_source_size", lambda source: size)


def test_oversized_remote_noncog_asks_before_downloading(re, eng, tmp_path, monkeypatch):
    path = _write_raster(tmp_path / "plain.tif")
    _as_remote(re, monkeypatch, 200 << 20)
    infer_calls = []
    monkeypatch.setattr(
        re, "_infer_background",
        lambda *a, **k: infer_calls.append(True) or None,
    )

    d = eng._describe(target=path, source=path, artifact_id="a", opts={})

    assert d["status"] == "ok"
    assert d["optimization"]["status"] == "confirm_download"
    assert d["optimization"]["download_bytes"] == 200 << 20
    # Nothing was read: the collar probe never ran, no derivative was started.
    assert infer_calls == []
    source_id = d["data"]["source_id"]
    assert eng.sources[source_id].optimization["status"] == "confirm_download"
    assert eng.sources[source_id].preview_path is None


def test_unconfirmed_source_serves_no_tiles(re, eng, tmp_path, monkeypatch):
    path = _write_raster(tmp_path / "plain.tif")
    _as_remote(re, monkeypatch, 200 << 20)
    monkeypatch.setattr(re, "_infer_background", lambda *a, **k: None)

    d = eng._describe(target=path, source=path, artifact_id="a", opts={})
    source_id = d["data"]["source_id"]

    assert eng.tile(source_id, 12, 1, 1) == eng.transparent_tile()


def test_small_remote_noncog_is_not_gated(re, eng, tmp_path, monkeypatch):
    path = _write_raster(tmp_path / "plain.tif")
    _as_remote(re, monkeypatch, 1 << 20)  # under the threshold
    monkeypatch.setattr(re, "_infer_background", lambda *a, **k: None)
    started = []
    monkeypatch.setattr(
        eng, "_start_preparation",
        lambda source_id, full_optimize: started.append((source_id, full_optimize)),
    )

    d = eng._describe(target=path, source=path, artifact_id="a", opts={})

    assert d["optimization"]["status"] != "confirm_download"
    assert started, "a small non-COG should still prepare a local pyramid"


def test_confirming_the_download_builds_a_local_cog(re, eng, tmp_path, monkeypatch):
    path = _write_raster(tmp_path / "plain.tif")
    _as_remote(re, monkeypatch, 200 << 20)
    monkeypatch.setattr(re, "_infer_background", lambda *a, **k: None)
    monkeypatch.setattr(eng, "prepare_pool", _Inline())

    d = eng._describe(target=path, source=path, artifact_id="a", opts={})
    source_id = d["data"]["source_id"]

    eng.start_optimization(source_id)  # runs _prepare inline

    record = eng.sources[source_id]
    assert record.optimization["status"] == "ready"
    assert record.optimized_path is not None
    assert os.path.exists(record.optimized_path)
    # With a local derivative in hand, tiles now render.
    tile = eng.tile(source_id, record.minzoom, 0, 0)
    assert tile is not None


def test_cloud_native_remote_is_not_gated(re, eng, tmp_path, monkeypatch):
    path = _write_raster(tmp_path / "cog.tif", overviews=True, tiled=True)
    _as_remote(re, monkeypatch, 500 << 20)
    monkeypatch.setattr(re, "_infer_background", lambda *a, **k: None)

    d = eng._describe(target=path, source=path, artifact_id="a", opts={})

    assert d["optimization"]["status"] != "confirm_download"


def test_local_oversized_noncog_is_not_gated(re, eng, tmp_path, monkeypatch):
    path = _write_raster(tmp_path / "plain.tif")
    # Real local file: is_remote_path stays honest, size is irrelevant.
    monkeypatch.setattr(re, "_source_size", lambda source: 999 << 20)
    monkeypatch.setattr(re, "_infer_background", lambda *a, **k: None)
    monkeypatch.setattr(eng, "_start_preparation", lambda *a, **k: None)

    d = eng._describe(target=path, source=path, artifact_id="a", opts={})

    assert d["optimization"]["status"] != "confirm_download"

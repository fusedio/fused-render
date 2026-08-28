"""LRU eviction of the on-disk raster derivative cache (raster_engine.py).

The optimized/ folder holds a full local COG (and a small preview) per
non-cloud-optimized raster ever opened, so it grows without bound. Eviction
keeps it under a byte cap, oldest-opened first, and never deletes a derivative
that backs a live layer.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_optimized_cache.py -o addopts=""
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


@pytest.fixture
def re():
    return _load("raster_engine", "raster_engine.py")


@pytest.fixture
def eng(re, tmp_path):
    return re.RasterEngine(
        cache_dir=str(tmp_path), base_url="http://x", token="t"
    )


def _write(eng, name, size, mtime):
    path = eng.optimized_dir / name
    path.write_bytes(b"\0" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_evicts_oldest_until_under_cap(re, eng, monkeypatch):
    monkeypatch.setattr(re, "OPTIMIZED_CACHE_MAX_BYTES", 250)
    old = _write(eng, "a.tif", 100, 1000)
    mid = _write(eng, "b.tif", 100, 2000)
    new = _write(eng, "c.tif", 100, 3000)

    eng._evict_optimized()

    # 300 bytes > 250 cap: drop the single oldest, leaving 200 bytes.
    assert not old.exists()
    assert mid.exists() and new.exists()


def test_never_evicts_a_derivative_backing_a_live_layer(re, eng, monkeypatch):
    monkeypatch.setattr(re, "OPTIMIZED_CACHE_MAX_BYTES", 150)
    old = _write(eng, "a.tif", 100, 1000)
    new = _write(eng, "b.tif", 100, 2000)
    # The oldest is in use by a live source, so the next-oldest must go instead.
    eng.sources["s"] = re.RasterSource(
        source_id="s", target="a", source="a", locator="a", driver="GTiff",
        width=1, height=1, count=1, dtypes=("uint8",), crs="EPSG:4326",
        bounds=[0, 0, 1, 1], minzoom=0, maxzoom=1, block_shapes=[[1, 1]],
        overviews=[], source_size=None, layout=None, nodata=None,
        inferred_nodata=None, colormap="viridis", rescale=[[0.0, 1.0]],
        optimized_path=str(old),
    )

    eng._evict_optimized()

    assert old.exists()       # protected despite being oldest
    assert not new.exists()   # evicted instead


def test_ignores_temp_files_and_respects_disabled_cap(re, eng, monkeypatch):
    monkeypatch.setattr(re, "OPTIMIZED_CACHE_MAX_BYTES", 50)
    tmp = _write(eng, "x.12345.tmp.tif", 100, 1000)
    eng._evict_optimized()
    assert tmp.exists()  # in-progress builds are never touched

    monkeypatch.setattr(re, "OPTIMIZED_CACHE_MAX_BYTES", 0)
    keep = _write(eng, "y.tif", 100, 1000)
    eng._evict_optimized()
    assert keep.exists()  # a cap of 0 disables eviction

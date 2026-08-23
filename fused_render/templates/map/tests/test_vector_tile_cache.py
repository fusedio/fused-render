"""LRU eviction of the on-disk vector-tile cache (vector_engine.py).

Rendered .pbf tiles are cached under vector-tiles/<version>/<source>/z/x/y.pbf
and accumulate without bound. Eviction keeps the tree under a byte cap,
least-recently-served first, triggered on startup and whenever the running
byte count crosses the cap.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_vector_tile_cache.py -o addopts=""
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
def ve():
    return _load("map_vector_engine", "vector_engine.py")


@pytest.fixture
def eng(ve, tmp_path):
    return ve.VectorEngine(
        base_url="http://x", token="t",
        locator=lambda source, target: source, cache_dir=str(tmp_path),
    )


def _write_tile(eng, key, size, mtime):
    z, x, y = key
    path = eng.cache_dir / "src" / str(z) / str(x) / f"{y}.pbf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_evicts_oldest_disk_tiles_until_under_cap(ve, eng, monkeypatch):
    monkeypatch.setattr(ve, "VECTOR_TILE_CACHE_MAX_BYTES", 250)
    old = _write_tile(eng, (5, 1, 1), 100, 1000)
    mid = _write_tile(eng, (5, 1, 2), 100, 2000)
    new = _write_tile(eng, (5, 1, 3), 100, 3000)

    eng._evict_disk_tiles()

    assert not old.exists()
    assert mid.exists() and new.exists()


def test_ignores_tmp_and_disabled_cap(ve, eng, monkeypatch):
    monkeypatch.setattr(ve, "VECTOR_TILE_CACHE_MAX_BYTES", 50)
    tmp = eng.cache_dir / "src" / "5" / "1" / "1.pbf.123.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(b"\0" * 100)
    eng._evict_disk_tiles()
    assert tmp.exists()

    monkeypatch.setattr(ve, "VECTOR_TILE_CACHE_MAX_BYTES", 0)
    keep = _write_tile(eng, (5, 2, 2), 100, 1000)
    eng._evict_disk_tiles()
    assert keep.exists()


def test_write_cached_bounds_the_cache(ve, eng, monkeypatch):
    monkeypatch.setattr(ve, "VECTOR_TILE_CACHE_MAX_BYTES", 500)
    tile = b"\0" * 200
    for i in range(10):
        eng._write_cached(("src", 5, i, 0), tile)

    total = sum(
        os.path.getsize(os.path.join(root, name))
        for root, _dirs, files in os.walk(eng.cache_dir)
        for name in files if name.endswith(".pbf")
    )
    assert total <= 500

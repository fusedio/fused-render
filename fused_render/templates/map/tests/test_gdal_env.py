"""The GDAL environment tile reads run under (raster_engine.py).

These options are measured, not guessed, and two of them are counter-intuitive
enough to be worth pinning down so a plausible-looking "optimization" does not
quietly undo them.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_gdal_env.py -o addopts=""
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


def _options(engine, locator, monkeypatch):
    captured = {}

    class Env:
        def __init__(self, **options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    import rasterio

    monkeypatch.setattr(rasterio, "Env", Env)
    with engine._gdal_env(locator):
        pass
    return captured


def test_the_curl_chunk_size_is_left_at_gdal_default(engine, monkeypatch):
    # GDAL rounds each read down to a chunk boundary, so a bigger chunk means a
    # bigger minimum transfer for the single block a tile needs. Raising it to
    # 64KB measured 26% slower than the 16KB default on a remote COG.
    options = _options(engine, "/vsicurl/https://example.com/a.tif", monkeypatch)
    assert "CPL_VSIL_CURL_CHUNK_SIZE" not in options


def test_remote_sources_skip_directory_listings(engine, monkeypatch):
    options = _options(engine, "/vsicurl/https://example.com/a.tif", monkeypatch)
    assert options["GDAL_DISABLE_READDIR_ON_OPEN"] == "EMPTY_DIR"


def test_local_sources_keep_listings_for_sidecar_discovery(engine, monkeypatch):
    # A local raster's colours can live in a PAM .aux.xml sidecar, which GDAL
    # only finds by listing the directory.
    options = _options(engine, r"C:\data\landcover.tif", monkeypatch)
    assert "GDAL_DISABLE_READDIR_ON_OPEN" not in options


def test_retries_do_not_stall_a_tile(engine, monkeypatch):
    # GDAL's own default retry delay is 30 seconds, which reads as a hang.
    options = _options(engine, "/vsicurl/https://example.com/a.tif", monkeypatch)
    assert float(options["GDAL_HTTP_RETRY_DELAY"]) <= 1.0

"""Tests for RasterEngine.locator's transport selection (raster_engine.py).

locator is pure string routing (the heavy geo deps are imported lazily inside
the tile/describe methods, not at import or in locator), so this loads the
module with the map dir on sys.path and needs no numpy/rasterio.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_raster_locator.py -o addopts=""
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
    sys.modules[name] = mod  # dataclass field resolution looks the module up here
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def eng(tmp_path):
    re = _load("raster_engine", "raster_engine.py")
    return re.RasterEngine(
        cache_dir=str(tmp_path), base_url="http://127.0.0.1:9999", token="tok"
    )


def test_clean_public_url_goes_straight_to_gdal(eng):
    url = (
        "https://maxar-opendata.s3.amazonaws.com/events/Belize/ard/16/"
        "033131012100/2024-01-09/10400100905FFC00-visual.tif"
    )
    # A real file URL is handed to GDAL directly so curl keeps one connection
    # alive across range reads — not funneled through the per-range relay.
    assert eng.locator(url, "10400100905FFC00-visual.tif") == f"/vsicurl/{url}"
    assert not eng.upstreams  # no relay source registered for a direct URL


def test_mount_proxy_url_uses_the_loopback_relay(eng):
    proxy = "http://127.0.0.1:1777/api/fs/raw?path=%2Fmnt%2Fscene.tif&pooled=1"
    loc = eng.locator(proxy, "scene.tif")
    assert loc.startswith(f"/vsicurl/{eng.base_url}/upstream/")
    assert "/api/fs/raw" not in loc  # GDAL never sees the recursive proxy URL
    assert eng.upstreams  # relay source registered for the opaque proxy


def test_any_query_url_takes_the_relay_not_just_the_mount_proxy(eng):
    # GDAL probes sidecars by appending suffixes to the whole locator, query
    # string included. Recognising only `/api/fs/raw` sent every other query URL
    # straight to GDAL, and one that keys off `?path=` answers `...&pooled=1.rv2`
    # with the same raster — GDAL reopens it, probes `.rv2.rv1`, and recurses
    # (measured: 868 requests, 15GB resident, on a 2MB NITF).
    served_by_query = "http://127.0.0.1:8123/raw?path=scene.ntf&pooled=1"
    assert eng.locator(served_by_query, "scene.ntf").startswith(
        f"/vsicurl/{eng.base_url}/upstream/"
    )


def test_a_signed_url_keeps_the_direct_path(eng):
    # A suffix probe cannot resolve back to the object: it invalidates the
    # signature (Azure answers 403, a path suffix 404s), so the query string here
    # is safe and the fast keep-alive read is kept.
    signed = HLS + "?st=2026-08-16&se=2026-08-17&sig=abc%3D"
    assert eng.locator(signed, "B10.tif") == f"/vsicurl/{signed}"
    assert not eng.upstreams


HLS = (
    "https://hls2euwest.blob.core.windows.net/hls2/L30/43/P/GS/2026/08/14/"
    "HLS.L30.T43PGS.2026226T051556.v2.0/HLS.L30.T43PGS.2026226T051556.v2.0.B10.tif"
)


def test_an_azure_url_is_unsigned_until_the_source_refuses_us(eng):
    assert eng.locator(HLS, "B10.tif") == f"/vsicurl/{HLS}"


def test_every_read_after_a_refusal_carries_the_token(eng, monkeypatch):
    # Tiles go through locator too, so the lesson learned while describing the
    # raster has to reach them; otherwise the overview opens and no tile draws.
    import blob_tokens

    monkeypatch.setattr(
        blob_tokens.TOKENS, "_fetch", lambda account, container: ("sig=tok", "")
    )
    monkeypatch.setattr(blob_tokens.TOKENS, "_needed", set())
    monkeypatch.setattr(blob_tokens.TOKENS, "_tokens", {})
    assert blob_tokens.TOKENS.learn(HLS, "RasterioIOError: HTTP response code: 409")
    assert eng.locator(HLS, "B10.tif") == f"/vsicurl/{HLS}?sig=tok"


def test_a_stale_token_is_replaced_before_the_source_is_opened(eng, monkeypatch):
    # Tokens last under an hour. A locator recorded while describing the raster
    # would still be in the record long after that, so every open re-signs.
    import blob_tokens

    issued = []

    def fetch(account, container):
        issued.append(container)
        return f"sig=tok{len(issued)}", ""

    monkeypatch.setattr(blob_tokens.TOKENS, "_fetch", fetch)
    monkeypatch.setattr(blob_tokens.TOKENS, "_needed", {("hls2euwest", "hls2")})
    monkeypatch.setattr(blob_tokens.TOKENS, "_tokens", {})

    recorded = eng.locator(HLS, "B10.tif")
    assert recorded.endswith("sig=tok1")
    record = _record(eng, recorded)
    blob_tokens.TOKENS._tokens.clear()  # the token has aged out
    assert eng._open_path(record, record.locator).endswith("sig=tok2")


def test_a_local_derivative_is_opened_as_it_stands(eng, monkeypatch):
    import blob_tokens

    monkeypatch.setattr(blob_tokens.TOKENS, "_needed", {("hls2euwest", "hls2")})
    record = _record(eng, f"/vsicurl/{HLS}?sig=old")
    assert eng._open_path(record, "C:/cache/derived.tif") == "C:/cache/derived.tif"


def _record(eng, locator):
    import raster_engine

    return raster_engine.RasterSource(
        source_id="s", target="B10.tif", source=HLS, locator=locator,
        driver="GTiff", width=1, height=1, count=1, dtypes=("int16",),
        crs="EPSG:32643", bounds=[0, 0, 1, 1], minzoom=0, maxzoom=12,
        block_shapes=[[512, 512]], overviews=[2], source_size=None, layout=None,
        nodata=None, inferred_nodata=None, colormap="viridis", rescale=[[0.0, 1.0]],
    )


def test_a_refusal_that_survives_signing_says_so_in_plain_terms(eng):
    import raster_engine

    message = raster_engine._read_failure_message(HLS, OSError("HTTP response code: 409"))
    assert "range transport" not in message, message
    assert "credentials" in message, message


def test_an_unreadable_file_is_still_reported_as_one(eng):
    import raster_engine

    message = raster_engine._read_failure_message(HLS, OSError("not recognized as a TIFF"))
    assert "range transport" in message, message


def test_vsi_native_and_local_pass_through_unchanged(eng):
    assert eng.locator("/vsicurl/https://h/y.tif", "y.tif") == "/vsicurl/https://h/y.tif"
    assert eng.locator("s3://bucket/key.tif", "key.tif") == "s3://bucket/key.tif"
    assert eng.locator("C:/data/x.tif", "x.tif") == "C:/data/x.tif"
    assert not eng.upstreams

"""Vector tile performance and cancellation regressions (vector_engine.py,
daemon.py).

A 3.58 GB / 13.8M-hexagon GeoPackage measured 16s per z6 tile: the dense
overview re-scanned the whole RTree per tile and GDAL's MVT driver added ~0.9s
of fixed overhead per tile. The rtree node summary plus the direct protobuf
writer brought that to ~0.3s. Abandoned tiles (pan/zoom away) used to keep
computing and, with VTILE_POOL=1, head-of-line-blocked every other request.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_vector_tiles.py -o addopts=""
"""
import importlib.util
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

pytest.importorskip("pyogrio")
pytest.importorskip("shapely")

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
def vector_engine():
    return _load("map_vector_engine", "vector_engine.py")


@pytest.fixture(scope="module")
def dense_gpkg(tmp_path_factory):
    """~62k small polygons with an RTree — enough that the pre-summary dense
    path was visibly slow, small enough to build in seconds."""
    import geopandas as gpd
    from shapely.geometry import box

    side = 250
    cell = 20.0 / side
    geometries = [
        box(
            -10.0 + column * cell,
            -10.0 + row * cell,
            -10.0 + (column + 0.8) * cell,
            -10.0 + (row + 0.8) * cell,
        )
        for row in range(side)
        for column in range(side)
    ]
    path = tmp_path_factory.mktemp("vectors") / "dense.gpkg"
    gpd.GeoDataFrame(
        {"class": [index % 7 for index in range(len(geometries))]},
        geometry=geometries,
        crs="EPSG:4326",
    ).to_file(path, layer="cells", driver="GPKG", engine="pyogrio")
    return path


def _describe(vector_engine, path, monkeypatch):
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 10)
    engine = vector_engine.VectorEngine(
        base_url="http://127.0.0.1:9999",
        token="test-token",
        locator=lambda source, _target: source,
    )
    descriptor = engine.try_describe({"target": str(path), "artifact_id": "perf"})
    assert descriptor["status"] == "ok"
    return engine, descriptor["data"]["source_id"]


def test_low_zoom_tiles_on_a_large_layer_are_fast(
    vector_engine, dense_gpkg, monkeypatch
):
    engine, source_id = _describe(vector_engine, dense_gpkg, monkeypatch)
    started = time.monotonic()
    tiles = [engine.tile(source_id, 2, x, 1) for x in (1, 2)]
    elapsed = time.monotonic() - started
    assert any(tiles), "the dense layer produced no low-zoom tiles"
    # 16s-class per-tile behavior must never come back; generous CI headroom.
    assert elapsed < 4.0, f"two low-zoom tiles took {elapsed:.1f}s"


def test_node_summary_overview_covers_the_layer(
    vector_engine, dense_gpkg, monkeypatch
):
    # Force the rtree-node-summary path (normally reserved for bboxes holding
    # hundreds of thousands of features) and check it draws real coverage.
    monkeypatch.setattr(vector_engine, "OVERVIEW_EXACT_MAX", 10)
    engine, source_id = _describe(vector_engine, dense_gpkg, monkeypatch)
    source = engine.sources[source_id]
    assert engine._node_summary(source) is not None, "rtree node parse failed"

    mvt = pytest.importorskip("mapbox_vector_tile")
    tile = engine.tile(source_id, 2, 1, 1)
    features = mvt.decode(tile)["layer"]["features"]
    assert len(features) > 20
    assert all(f["properties"]["feature_count"] > 0 for f in features)


def test_a_cancelled_tile_raises_instead_of_rendering(
    vector_engine, dense_gpkg, monkeypatch
):
    engine, source_id = _describe(vector_engine, dense_gpkg, monkeypatch)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(vector_engine.TileCancelled):
        engine.tile(source_id, 2, 1, 1, cancel)
    assert (source_id, 2, 1, 1) not in engine.tile_cache


def test_an_abandoned_tile_does_not_block_the_next_request():
    """End-to-end over real sockets: client A asks for a slow tile and hangs
    up; the daemon must cancel it and serve client B promptly even though
    VTILE_POOL renders one tile at a time."""
    daemon = _load("map_daemon", "daemon.py")

    first = threading.Event()

    class SlowVectors:
        def tile(self, _source_id, _z, _x, _y, cancel=None):
            if not first.is_set():
                first.set()
                if not cancel.wait(timeout=30):
                    raise TimeoutError("the abandoned tile was never cancelled")
                raise RuntimeError("cancelled")
            return b"\x1a\x02ok"

    server = daemon.MapServer(("127.0.0.1", 0), daemon.Handler)
    port = int(server.server_address[1])
    server.token = "test-token"
    server.vectors = SlowVectors()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        abandoned = socket.create_connection(("127.0.0.1", port), timeout=5)
        abandoned.sendall(
            b"GET /vtiles/src/0/0/0.pbf?t=test-token HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n\r\n"
        )
        assert first.wait(timeout=5), "the slow tile never started"
        abandoned.close()

        started = time.monotonic()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/vtiles/src/1/0/0.pbf?t=test-token",
            timeout=10,
        ) as response:
            assert response.status == 200
        assert time.monotonic() - started < 5.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

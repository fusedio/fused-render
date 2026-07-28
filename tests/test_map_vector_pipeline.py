"""Large-vector registration and bounded MVT tile regressions."""
from __future__ import annotations

import math
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest


pytest.importorskip("geopandas")
pytest.importorskip("mapbox_vector_tile")
pytest.importorskip("pyogrio")
pytest.importorskip("rasterio")
pytest.importorskip("shapely")

import geopandas as gpd
import mapbox_vector_tile
from shapely.geometry import box


MAP_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "fused_render" / "templates" / "map"
)
sys.path.insert(0, str(MAP_TEMPLATE))

import vector_engine
from daemon import Handler, MapServer
from vector_engine import VectorEngine


def _make_grid(path: Path, width: int = 40, height: int = 40) -> None:
    cell_width = 40.0 / width
    cell_height = 40.0 / height
    geometries = []
    classes = []
    for row in range(height):
        for column in range(width):
            west = -20.0 + column * cell_width
            south = -20.0 + row * cell_height
            geometries.append(
                box(west, south, west + cell_width * 0.8, south + cell_height * 0.8)
            )
            classes.append((row + column) % 7)
    frame = gpd.GeoDataFrame(
        {"class": classes},
        geometry=geometries,
        crs="EPSG:4326",
    )
    frame.to_file(path, layer="segments", driver="GPKG", engine="pyogrio")


def _request(path: Path) -> dict:
    return {
        "target": str(path),
        "source_url": "",
        "source_origin": "",
        "artifact_id": "large-vector",
        "opts": {},
    }


def _engine() -> VectorEngine:
    return VectorEngine(
        base_url="http://127.0.0.1:9999",
        token="test-token",
        locator=lambda source, _target: source,
    )


def _tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    scale = 1 << zoom
    x = int((lon + 180.0) / 360.0 * scale)
    y = int(
        (
            1.0
            - math.asinh(math.tan(math.radians(lat))) / math.pi
        )
        / 2.0
        * scale
    )
    return x, y


def test_large_geopackage_registers_without_whole_file_serialization(
    tmp_path, monkeypatch
):
    source = tmp_path / "segments.gpkg"
    _make_grid(source, width=20, height=20)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 10)
    engine = _engine()

    descriptor = engine.try_describe(_request(source))

    assert descriptor["status"] == "ok"
    assert descriptor["kind"] == "vector_tiles_mvt"
    assert descriptor["stats"]["feature_count"] == 400
    assert descriptor["stats"]["indexed"] is True
    assert descriptor["stats"]["source_layer"] == "segments"
    assert descriptor["bounds"] == pytest.approx([-20.0, -20.0, 19.6, 19.6])
    assert len(engine.sources) == 1


def test_geopackage_tile_is_valid_and_feature_bounded(tmp_path, monkeypatch):
    source = tmp_path / "segments.gpkg"
    _make_grid(source)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 10)
    monkeypatch.setattr(vector_engine, "MAX_TILE_FEATURES", 50)
    engine = _engine()
    descriptor = engine.try_describe(_request(source))
    source_id = descriptor["data"]["source_id"]

    tile = engine.tile(source_id, 0, 0, 0)
    decoded = mapbox_vector_tile.decode(tile)
    features = decoded["layer"]["features"]

    assert 0 < len(features) <= 50
    assert all("class" in feature["properties"] for feature in features)
    assert engine.tile(source_id, 0, 0, 0) is tile


def test_tile_outside_source_is_empty(tmp_path, monkeypatch):
    source = tmp_path / "segments.gpkg"
    _make_grid(source, width=10, height=10)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 10)
    engine = _engine()
    descriptor = engine.try_describe(_request(source))
    x, y = _tile(120.0, 50.0, 8)

    assert engine.tile(descriptor["data"]["source_id"], 8, x, y) == b""


def test_vector_tile_is_served_over_the_daemon_endpoint(tmp_path, monkeypatch):
    source = tmp_path / "segments.gpkg"
    _make_grid(source, width=10, height=10)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 10)
    token = "test-token"
    server = MapServer(("127.0.0.1", 0), Handler)
    port = int(server.server_address[1])
    server.token = token
    server.last_hit = time.time()
    server.vectors = VectorEngine(
        base_url=f"http://127.0.0.1:{port}",
        token=token,
        locator=lambda candidate, _target: candidate,
    )
    descriptor = server.vectors.try_describe(_request(source))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tile_url = (
            descriptor["data"]["tile_url"]
            .replace("{z}", "0")
            .replace("{x}", "0")
            .replace("{y}", "0")
        )
        with urllib.request.urlopen(tile_url, timeout=30) as response:
            tile = response.read()
            assert response.status == 200
            assert response.headers.get_content_type() == (
                "application/vnd.mapbox-vector-tile"
            )
        assert mapbox_vector_tile.decode(tile)["layer"]["features"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_small_vector_stays_on_the_existing_geojson_path(tmp_path, monkeypatch):
    source = tmp_path / "small.gpkg"
    _make_grid(source, width=2, height=2)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 50)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_BYTES", 1 << 30)

    assert _engine().try_describe(_request(source)) is None

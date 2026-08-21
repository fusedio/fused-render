"""Large-vector registration and bounded MVT tile regressions."""
from __future__ import annotations

import json
import math
import sys
import threading
import time
import urllib.error
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

import geo_paths
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


def _make_utm_grid(path: Path, width: int = 20, height: int = 20) -> None:
    cell = 2000.0
    geometries = []
    classes = []
    for row in range(height):
        for column in range(width):
            west = 500000.0 + column * cell
            south = 2200000.0 + row * cell
            geometries.append(
                box(west, south, west + cell * 0.8, south + cell * 0.8)
            )
            classes.append((row + column) % 7)
    frame = gpd.GeoDataFrame(
        {"class": classes},
        geometry=geometries,
        crs="EPSG:32639",
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


def _engine(cache_dir=None) -> VectorEngine:
    return VectorEngine(
        base_url="http://127.0.0.1:9999",
        token="test-token",
        locator=lambda source, _target: source,
        cache_dir=cache_dir,
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


def test_dense_geopackage_tile_is_a_complete_occupancy_overview(
    tmp_path, monkeypatch
):
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

    assert features
    assert all(
        feature["properties"]["feature_count"] > 0
        for feature in features
    )
    assert sum(
        feature["properties"]["feature_count"]
        for feature in features
    ) >= 1600
    assert engine.tile(source_id, 0, 0, 0) is tile


def test_tile_outside_source_is_empty(tmp_path, monkeypatch):
    source = tmp_path / "segments.gpkg"
    _make_grid(source, width=10, height=10)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 10)
    engine = _engine()
    descriptor = engine.try_describe(_request(source))
    x, y = _tile(120.0, 50.0, 8)

    assert engine.tile(descriptor["data"]["source_id"], 8, x, y) == b""


def _decoded_points(value):
    if isinstance(value[0], (int, float)):
        return [value]
    return [point for item in value for point in _decoded_points(item)]


def test_projected_crs_geopackage_serves_correctly_placed_tiles(
    tmp_path, monkeypatch
):
    source = tmp_path / "utm.gpkg"
    _make_utm_grid(source)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 10)
    engine = _engine()

    descriptor = engine.try_describe(_request(source))

    assert descriptor["status"] == "ok"
    assert descriptor["crs_original"] == "EPSG:32639"
    west, south, east, north = descriptor["bounds"]
    assert 50.9 < west < east < 51.5
    assert 19.5 < south < north < 20.5

    zoom = 10
    x, y = _tile((west + east) / 2.0, (south + north) / 2.0, zoom)
    tile = engine.tile(descriptor["data"]["source_id"], zoom, x, y)

    assert tile
    features = mapbox_vector_tile.decode(tile)["layer"]["features"]
    assert features
    for feature in features:
        for px, py in _decoded_points(feature["geometry"]["coordinates"]):
            assert -1024 <= px <= 5120
            assert -1024 <= py <= 5120


def test_projected_crs_dense_overview_is_reprojected(tmp_path, monkeypatch):
    source = tmp_path / "utm.gpkg"
    _make_utm_grid(source)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 10)
    monkeypatch.setattr(vector_engine, "MAX_TILE_FEATURES", 50)
    engine = _engine()
    descriptor = engine.try_describe(_request(source))
    west, south, east, north = descriptor["bounds"]

    zoom = 8
    x, y = _tile((west + east) / 2.0, (south + north) / 2.0, zoom)
    tile = engine.tile(descriptor["data"]["source_id"], zoom, x, y)

    assert tile
    features = mapbox_vector_tile.decode(tile)["layer"]["features"]
    assert features
    assert all(
        feature["properties"]["feature_count"] > 0 for feature in features
    )
    for feature in features:
        for px, py in _decoded_points(feature["geometry"]["coordinates"]):
            assert -1024 <= px <= 5120
            assert -1024 <= py <= 5120


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


def test_native_tile_cache_survives_engine_restart(tmp_path, monkeypatch):
    source = tmp_path / "segments.gpkg"
    cache = tmp_path / "cache"
    _make_grid(source, width=10, height=10)
    monkeypatch.setattr(vector_engine, "VECTOR_TILE_MIN_FEATURES", 10)

    first = _engine(cache)
    descriptor = first.try_describe(_request(source))
    tile = first.tile(descriptor["data"]["source_id"], 0, 0, 0)

    second = _engine(cache)
    descriptor = second.try_describe(_request(source))
    monkeypatch.setattr(
        second,
        "_encode_tile",
        lambda *_args: pytest.fail("disk-cached tile must not be re-encoded"),
    )

    assert second.tile(descriptor["data"]["source_id"], 0, 0, 0) == tile


def test_vector_tile_endpoint_returns_json_when_encoding_fails():
    class BrokenVectors:
        def tile(self, *_args):
            raise RuntimeError("encoder unavailable")

    token = "test-token"
    server = MapServer(("127.0.0.1", 0), Handler)
    port = int(server.server_address[1])
    server.token = token
    server.last_hit = time.time()
    server.vectors = BrokenVectors()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/vtiles/source/0/0/0.pbf?t={token}"
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url, timeout=5)
        assert caught.value.code == 500
        payload = json.loads(caught.value.read())
        assert payload["message"] == "RuntimeError: encoder unavailable"
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


def test_branch_mount_vector_uses_the_range_proxy(tmp_path, monkeypatch):
    home = tmp_path / "custom-home"
    source = (
        home
        / "branches"
        / "feat-map"
        / "mounts"
        / "bucket"
        / "segments.gpkg"
    )
    source.parent.mkdir(parents=True)
    source.write_bytes(b"mounted")
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "feat/map")
    source_url = "http://127.0.0.1:1777/api/fs/raw?path=mounted-vector"

    resolved = geo_paths.resolve_source(
        {
            "target": str(source),
            "source_url": source_url,
        },
        str(source),
    )

    assert resolved == source_url


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (
            "HTTPS://Example.test/Data/Segments.gpkg",
            "https://Example.test/Data/Segments.gpkg",
        ),
        (
            "S3://Bucket/Data/Segments.gpkg",
            "s3://Bucket/Data/Segments.gpkg",
        ),
        (
            "/VSIS3/Bucket/Data/Segments.gpkg",
            "/vsis3/Bucket/Data/Segments.gpkg",
        ),
    ],
)
def test_uppercase_remote_vectors_are_not_resolved_as_local_paths(
    target, expected
):
    assert geo_paths.resolve_source({"target": target}, target) == expected

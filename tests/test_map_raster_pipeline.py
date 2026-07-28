from __future__ import annotations

import contextlib
import io
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# The map runtime is intentionally isolated from fused-render's lean development
# environment. These integration tests run wherever the optional geospatial stack
# is installed and skip cleanly in jobs that only install the core dependencies.
pytest.importorskip("numpy")
pytest.importorskip("pandas")
pytest.importorskip("PIL")
pytest.importorskip("rasterio")
pytest.importorskip("rio_tiler")

MAP_TEMPLATE = Path(
    os.environ.get(
        "MAP_TEMPLATE_DIR",
        Path(__file__).resolve().parents[1] / "fused_render" / "templates" / "map",
    )
)
sys.path.insert(0, str(MAP_TEMPLATE))

import numpy as np
import pandas as pd
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.shutil import copy as rio_copy
from rasterio.transform import from_bounds

import map_render
import geo_classify
import raster_engine
from daemon import Handler, MapServer
from raster_engine import RasterEngine


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class RangeSourceHandler(BaseHTTPRequestHandler):
    source: Path
    requests: list[tuple[str, str, str | None]] = []
    bytes_sent = 0
    full_gets = 0

    def log_message(self, _format, *_args):
        return

    def do_HEAD(self):
        self._serve()

    def do_GET(self):
        self._serve()

    def _serve(self):
        type(self).requests.append(
            (self.command, self.path, self.headers.get("Range"))
        )
        size = self.source.stat().st_size
        if self.command == "GET" and not self.headers.get("Range"):
            type(self).full_gets += 1
            self.send_error(412, "test source requires a byte range")
            return

        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            unit, value = range_header.split("=", 1)
            assert unit == "bytes"
            first, last = value.split("-", 1)
            start = int(first)
            end = min(int(last) if last else size - 1, size - 1)
            status = 206

        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with self.source.open("rb") as handle:
            handle.seek(start)
            body = handle.read(length)
        type(self).bytes_sent += len(body)
        self.wfile.write(body)


@contextlib.contextmanager
def range_source(path: Path):
    handler = type(
        "BoundRangeSourceHandler",
        (RangeSourceHandler,),
        {"source": path, "requests": [], "bytes_sent": 0, "full_gets": 0},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield handler, f"http://127.0.0.1:{port}/raw?path={path.name}&pooled=1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def map_service(cache_dir: Path):
    token = "test-token"
    server = MapServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    server.token = token
    server.version = "test"
    server.last_hit = time.time()
    server.engine = RasterEngine(
        cache_dir=str(cache_dir),
        base_url=f"http://127.0.0.1:{port}",
        token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_cog(path: Path):
    source = path.with_name("source.tif")
    rng = np.random.default_rng(5)
    data = rng.integers(0, 65535, size=(2048, 2048), dtype=np.uint16)
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype=data.dtype,
        crs="EPSG:4326",
        transform=from_bounds(100, 0, 102, 2, data.shape[1], data.shape[0]),
        tiled=True,
        blockxsize=256,
        blockysize=256,
        compress="deflate",
    ) as dataset:
        dataset.write(data, 1)
        dataset.build_overviews([2, 4, 8], Resampling.average)
    rio_copy(
        source,
        path,
        driver="COG",
        BLOCKSIZE=512,
        COMPRESS="DEFLATE",
        OVERVIEWS="FORCE_USE_EXISTING",
    )


def make_nitf(path: Path):
    width = height = 1024
    data = np.arange(width * height, dtype=np.uint16).reshape(height, width)
    with rasterio.open(
        path,
        "w",
        driver="NITF",
        width=width,
        height=height,
        count=1,
        dtype=data.dtype,
        crs="EPSG:4326",
        transform=from_bounds(-1, -1, 1, 1, width, height),
        ICORDS="G",
    ) as dataset:
        dataset.write(data, 1)


def make_zero_collar_tiff(path: Path):
    width = height = 1024
    data = np.zeros((height, width), dtype=np.float32)
    rng = np.random.default_rng(9)
    for row in range(128, 896):
        left = 128 + (row - 128) // 4
        right = min(896, left + 480)
        data[row, left:right] = rng.uniform(10, 400, size=right - left)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype=data.dtype,
        crs="EPSG:4326",
        transform=from_bounds(76, 12, 78, 14, width, height),
        tiled=True,
        blockxsize=256,
        blockysize=256,
        compress="deflate",
    ) as dataset:
        dataset.write(data, 1)


def request_for(target: str, source_url: str):
    return {
        "target": target,
        "source_url": source_url,
        "source_origin": "http://127.0.0.1:1777",
        "artifact_id": "test-layer",
        "artifact_dir": "",
        "opts": {"colormap": "viridis"},
    }


def center_tile_url(descriptor: dict, zoom: int | None = None) -> str:
    west, south, east, north = descriptor["bounds"]
    lon, lat = (west + east) / 2, (south + north) / 2
    z = descriptor["minzoom"] if zoom is None else zoom
    scale = 2**z
    x = int((lon + 180) / 360 * scale)
    y = int(
        (1 - math.asinh(math.tan(math.radians(lat))) / math.pi)
        / 2
        * scale
    )
    return (
        descriptor["data"]["tile_url"]
        .replace("{z}", str(z))
        .replace("{x}", str(x))
        .replace("{y}", str(y))
    )


def tile_has_visible_pixels(tile: bytes) -> bool:
    with Image.open(io.BytesIO(tile)) as image:
        alpha = np.asarray(image.convert("RGBA"))[:, :, 3]
    return bool(np.any(alpha))


def test_cog_uses_ranges_and_never_downloads_the_whole_source(tmp_path):
    cog = tmp_path / "large-source.tif"
    make_cog(cog)

    with range_source(cog) as (upstream, source_url):
        with map_service(tmp_path / "cache") as engine:
            descriptor = engine.try_describe(
                request_for(r"C:\mount\large-source.tif", source_url)
            )
            low_zoom = max(0, descriptor["stats"]["native_minzoom"] - 3)
            tile = urllib.request.urlopen(
                center_tile_url(descriptor, low_zoom), timeout=30
            ).read()

    assert descriptor["status"] == "ok"
    assert descriptor["kind"] == "raster_tiles"
    assert descriptor["minzoom"] == 0
    assert descriptor["stats"]["native_minzoom"] > descriptor["minzoom"]
    assert descriptor["stats"]["overviews"] == [2, 4, 8]
    assert tile.startswith(PNG_SIGNATURE)
    assert tile_has_visible_pixels(tile)
    assert upstream.full_gets == 0
    assert any(item[2] for item in upstream.requests if item[0] == "GET")
    assert upstream.bytes_sent < cog.stat().st_size


def test_query_url_nitf_does_not_recurse_and_builds_overviews(
    tmp_path, monkeypatch
):
    nitf = tmp_path / "no-overviews.ntf"
    make_nitf(nitf)
    monkeypatch.setattr(raster_engine, "AUTO_OPTIMIZE_MAX_BYTES", 0)

    with range_source(nitf) as (upstream, source_url):
        with map_service(tmp_path / "cache") as engine:
            started = time.monotonic()
            descriptor = engine.try_describe(
                request_for(r"C:\mount\no-overviews.ntf", source_url)
            )
            assert time.monotonic() - started < 15
            assert descriptor["stats"]["driver"] == "NITF"
            assert descriptor["stats"]["overviews"] == []
            assert descriptor["minzoom"] == 0

            source_id = descriptor["data"]["source_id"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                preview_job = engine.job(source_id)
                if preview_job["status"] in {"available", "error"}:
                    break
                time.sleep(0.1)

            assert preview_job["status"] == "available", preview_job
            assert preview_job["preview_ready"] is True
            assert Path(preview_job["preview_path"]).is_file()
            assert "path" not in preview_job
            low_zoom = max(0, preview_job["native_minzoom"] - 3)
            preview_tile = urllib.request.urlopen(
                center_tile_url(descriptor, low_zoom), timeout=30
            ).read()
            assert preview_tile.startswith(PNG_SIGNATURE)
            assert tile_has_visible_pixels(preview_tile)

            engine.start_optimization(source_id)
            reloaded = engine.try_describe(
                request_for(r"C:\mount\no-overviews.ntf", source_url)
            )
            assert reloaded["data"]["source_id"] == source_id
            assert reloaded["optimization"]["status"] in {
                "queued",
                "running",
                "ready",
            }
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                job = engine.job(source_id)
                if job["status"] in {"ready", "error"}:
                    break
                time.sleep(0.1)

            assert job["status"] == "ready", job
            with rasterio.open(job["path"]) as optimized:
                assert optimized.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
                assert optimized.overviews(1)
            tile = urllib.request.urlopen(
                center_tile_url(descriptor), timeout=30
            ).read()

    assert tile.startswith(PNG_SIGNATURE)
    assert tile_has_visible_pixels(tile)
    assert upstream.full_gets == 0
    assert not any(".rv" in path for _, path, _ in upstream.requests)


@pytest.mark.filterwarnings(
    "ignore:Setting the shape on a NumPy array has been deprecated:DeprecationWarning"
)
def test_real_preview_masks_an_inferred_zero_collar(tmp_path, monkeypatch):
    source = tmp_path / "zero-collar.tif"
    make_zero_collar_tiff(source)
    monkeypatch.setattr(raster_engine, "AUTO_OPTIMIZE_MAX_BYTES", 0)

    with range_source(source) as (upstream, source_url):
        with map_service(tmp_path / "cache") as engine:
            descriptor = engine.try_describe(
                request_for(r"C:\mount\zero-collar.tif", source_url)
            )
            assert descriptor["stats"]["inferred_nodata"] == 0.0
            source_id = descriptor["data"]["source_id"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                job = engine.job(source_id)
                if job["status"] in {"available", "error"}:
                    break
                time.sleep(0.1)

            assert job["status"] == "available", job
            with rasterio.open(job["preview_path"]) as preview:
                assert preview.width == raster_engine.PREVIEW_MAX_SIZE
                assert preview.nodata == 0.0
                rows = preview.read(1).tolist()
                assert any(value != 0 for row in rows for value in row)

            zoom = max(0, descriptor["stats"]["native_minzoom"] - 3)
            tile = urllib.request.urlopen(
                center_tile_url(descriptor, zoom), timeout=30
            ).read()
            with Image.open(io.BytesIO(tile)) as image:
                alpha = np.asarray(image.convert("RGBA"))[:, :, 3]
            assert np.count_nonzero(alpha) > 0
            assert np.count_nonzero(alpha) < alpha.size

    assert upstream.full_gets == 0


def test_cached_preview_is_reused_without_restarting_preparation(
    tmp_path, monkeypatch
):
    source = tmp_path / "cached-preview.tif"
    make_zero_collar_tiff(source)
    monkeypatch.setattr(raster_engine, "AUTO_OPTIMIZE_MAX_BYTES", 0)
    cache_dir = tmp_path / "cache"
    request = request_for(str(source), source_url="")
    request["source_origin"] = ""

    first = RasterEngine(
        cache_dir=str(cache_dir),
        base_url="http://127.0.0.1:1",
        token="first",
    )
    descriptor = first.try_describe(request)
    source_id = descriptor["data"]["source_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = first.job(source_id)
        if job["status"] in {"available", "error"}:
            break
        time.sleep(0.1)
    assert job["status"] == "available", job
    assert Path(job["preview_path"]).is_file()

    fresh = RasterEngine(
        cache_dir=str(cache_dir),
        base_url="http://127.0.0.1:1",
        token="fresh",
    )
    starts = []
    monkeypatch.setattr(
        fresh,
        "_start_preparation",
        lambda *args, **kwargs: starts.append((args, kwargs)),
    )

    reloaded = fresh.try_describe(request)

    assert starts == []
    assert reloaded["optimization"]["status"] == "available"
    assert reloaded["optimization"]["preview_ready"] is True
    assert not any(
        "being prepared" in notice for notice in reloaded["warnings"]
    )


def test_local_file_bypasses_the_loopback_range_proxy(tmp_path, monkeypatch):
    source = tmp_path / "local.tif"
    source.write_bytes(b"local")
    engine = RasterEngine(
        cache_dir=str(tmp_path / "cache"),
        base_url="http://127.0.0.1:1",
        token="test",
    )
    observed = {}

    def capture(**kwargs):
        observed.update(kwargs)
        return {"status": "captured"}

    monkeypatch.setattr(engine, "_describe", capture)
    result = engine.try_describe(
        request_for(str(source), "http://127.0.0.1:1777/api/fs/raw")
    )

    assert result["status"] == "captured"
    assert observed["source"] == str(source.resolve())


@pytest.mark.parametrize(
    "target",
    [
        "s3://example-bucket/scene.tif",
        "/vsis3/example-bucket/scene.tif",
    ],
)
def test_native_remote_rasters_bypass_the_shell_raw_proxy(
    tmp_path, monkeypatch, target
):
    engine = RasterEngine(
        cache_dir=str(tmp_path / "cache"),
        base_url="http://127.0.0.1:1",
        token="test",
    )
    observed = {}

    def capture(**kwargs):
        observed.update(kwargs)
        return {"status": "captured"}

    monkeypatch.setattr(engine, "_describe", capture)
    request = request_for(
        target,
        "http://127.0.0.1:1777/api/fs/raw?path=remote-raster",
    )

    result = engine.try_describe(request)

    assert result["status"] == "captured"
    assert observed["source"] == target


def test_concurrent_describes_register_one_source_and_one_preparation(
    tmp_path, monkeypatch
):
    source = tmp_path / "concurrent.tif"
    make_zero_collar_tiff(source)
    engine = RasterEngine(
        cache_dir=str(tmp_path / "cache"),
        base_url="http://127.0.0.1:1",
        token="test",
    )
    request = request_for(str(source), source_url="")
    request["source_origin"] = ""
    barrier = threading.Barrier(2)
    source_size = raster_engine._source_size

    def synchronized_source_size(value):
        result = source_size(value)
        barrier.wait(timeout=10)
        return result

    starts = []

    def capture_preparation(source_id, full_optimize):
        starts.append((source_id, full_optimize))
        engine.sources[source_id].optimization = {
            "status": "queued",
            "progress": 0,
        }
        return dict(engine.sources[source_id].optimization)

    monkeypatch.setattr(raster_engine, "_source_size", synchronized_source_size)
    monkeypatch.setattr(engine, "_start_preparation", capture_preparation)

    with ThreadPoolExecutor(max_workers=2) as pool:
        descriptors = list(pool.map(engine.try_describe, [request, request]))

    assert len(engine.sources) == 1
    assert len(starts) == 1
    assert len({item["data"]["source_id"] for item in descriptors}) == 1


def test_service_launch_is_hidden_on_windows_and_detached_elsewhere():
    options = map_render._process_options()
    assert options["stdin"] is not None
    assert options["close_fds"] is True
    if os.name == "nt":
        import subprocess

        assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert "start_new_session" not in options
    else:
        assert options["start_new_session"] is True
        assert "creationflags" not in options


def test_excel_lat_lon_table_uses_the_cross_platform_reader(tmp_path):
    workbook = tmp_path / "places.xlsx"
    pd.DataFrame(
        {
            "name": ["west", "east"],
            "latitude": [12.9, 13.1],
            "longitude": [77.1, 77.8],
        }
    ).to_excel(workbook, index=False)

    descriptor = geo_classify.classify(
        str(workbook), str(tmp_path / "artifacts"), "excel-points", {}
    )

    assert descriptor["status"] == "ok"
    assert descriptor["kind"] == "vector_geojson"
    assert descriptor["bounds"] == pytest.approx([77.1, 12.9, 77.8, 13.1])


def test_known_raster_open_error_is_not_misclassified_as_vector(tmp_path):
    broken = tmp_path / "broken.tif"
    broken.write_bytes(b"this is not a raster")
    engine = RasterEngine(
        cache_dir=str(tmp_path / "cache"),
        base_url="http://127.0.0.1:1",
        token="test",
    )

    request = request_for(str(broken), source_url="")
    request["source_origin"] = ""
    descriptor = engine.try_describe(request)

    assert descriptor["status"] == "error"
    assert descriptor["detected_type"] == "raster"
    assert "range transport" in descriptor["message"]

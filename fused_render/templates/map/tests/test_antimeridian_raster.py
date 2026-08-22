"""Antimeridian-crossing rasters must serve tiles on BOTH sides of 180.

A sinusoidal grid whose extent runs east across the seam reports 4326 bounds
with west > east (the eastern bulk wraps into negative longitudes). The daemon
used to serve tiles only for the portion at lon <= 180 and hand back a fully
transparent PNG for the wrapped hemisphere, so the map showed almost nothing.

Synthetic COGs built in tmp_path reproduce the crossing without the network or
MODIS' full sinusoidal complexity.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_antimeridian_raster.py -o addopts=""
"""
import importlib.util
import io
import math
import os
import sys

import pytest

np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
pytest.importorskip("rio_tiler")
from PIL import Image  # noqa: E402
from rasterio.transform import from_bounds  # noqa: E402

_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_MAP), "shared")

SINU = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"


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
def eng(tmp_path):
    re = _load("raster_engine", "raster_engine.py")
    return re.RasterEngine(
        cache_dir=str(tmp_path), base_url="http://127.0.0.1:9999", token="tok"
    )


def _write_cog(path, crs, west, south, east, north, w=256, h=128):
    """A gradient raster (dark west -> bright east) with an overview pyramid."""
    transform = from_bounds(west, south, east, north, w, h)
    gradient = np.tile(np.linspace(1, 255, w).astype("uint8"), (h, 1))
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=1, dtype="uint8",
        crs=crs, transform=transform, nodata=0, tiled=True,
        blockxsize=128, blockysize=128,
    ) as ds:
        ds.write(gradient[None, :, :])
        ds.build_overviews([2, 4], rasterio.enums.Resampling.nearest)


def _sinu_x(lon_deg, lat_deg):
    return 6371007.181 * math.radians(lon_deg) * math.cos(math.radians(lat_deg))


def _crossing_cog(path):
    # Sinusoidal grid spanning lon 172..191 (crosses 180) at lat -20..-10.
    lat_mid = -15.0
    _write_cog(
        path, SINU,
        _sinu_x(172, lat_mid), 6371007.181 * math.radians(-20),
        _sinu_x(191, lat_mid), 6371007.181 * math.radians(-10),
    )


def _describe(eng, path, **opts):
    return eng.try_describe(
        {"target": str(path), "artifact_id": "art", "opts": opts}
    )


def _opaque_count(png):
    assert png[:4] == b"\x89PNG"
    pixels = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
    return int((pixels[..., 3] > 0).sum())


def test_crossing_is_detected_and_bounds_wrap(eng, tmp_path):
    path = tmp_path / "crossing.tif"
    _crossing_cog(path)
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "ok"
    assert descriptor["stats"]["crosses_antimeridian"] is True
    west, _, east, _ = descriptor["bounds"]
    assert west > east  # the eastern bulk wrapped past 180 into negatives
    assert west > 160 and east < -160


def test_both_sides_of_the_seam_return_data(eng, tmp_path):
    path = tmp_path / "crossing.tif"
    _crossing_cog(path)
    descriptor = _describe(eng, path)
    source_id = descriptor["data"]["source_id"]

    # z3 y4 covers lat 0..-21.9. x7 is lon 135..180 (the <=180 sliver); x0 is
    # lon -180..-135, the wrapped hemisphere that used to come back transparent.
    west_tile = eng.tile(source_id, 3, 7, 4)
    wrapped_tile = eng.tile(source_id, 3, 0, 4)

    assert _opaque_count(west_tile) > 500
    # Before the fix this side was a fully transparent PNG.
    assert _opaque_count(wrapped_tile) > 500


def test_tiles_off_the_data_are_still_transparent(eng, tmp_path):
    path = tmp_path / "crossing.tif"
    _crossing_cog(path)
    source_id = _describe(eng, path)["data"]["source_id"]
    # x3 at z3 is lon -45..0, nowhere near the raster on either wrap.
    assert eng.tile(source_id, 3, 3, 4) == eng.transparent_tile()


def test_non_crossing_raster_is_unchanged(eng, tmp_path):
    from morecantile import Tile, tms

    path = tmp_path / "normal.tif"
    _write_cog(path, "EPSG:4326", 10.0, -20.0, 20.0, -10.0)
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "ok"
    assert descriptor["stats"]["crosses_antimeridian"] is False
    west, _, east, _ = descriptor["bounds"]
    assert west < east

    source_id = descriptor["data"]["source_id"]
    tile = tms.get("WebMercatorQuad").tile(15.0, -15.0, 3)
    png = eng.tile(source_id, tile.z, tile.x, tile.y)
    assert _opaque_count(png) > 500

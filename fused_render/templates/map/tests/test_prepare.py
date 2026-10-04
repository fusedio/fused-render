"""prepare.py — the Map Viewer's only Python on the rendering path.

The page draws COGs, vectors, Zarr and PMTiles itself; prepare.py turns what it
cannot read into something it can. These tests build small real datasets and
check each conversion produces a file the browser loader actually accepts
(a tiled COG, a consolidated Zarr v2 store, GeoJSON in EPSG:4326, GeoParquet),
plus the refusal payloads a user sees.

Needs the geo stack, i.e. this folder's own environment:
  UV_PROJECT_ENVIRONMENT=/tmp/mapvenv uv sync --project fused_render/templates/map
  /tmp/mapvenv/bin/python -m pytest fused_render/templates/map/tests/test_prepare.py -o addopts=""
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
np = pytest.importorskip("numpy")
xr = pytest.importorskip("xarray")
gpd = pytest.importorskip("geopandas")
pytest.importorskip("h5netcdf")
from rasterio.transform import from_bounds  # noqa: E402
from shapely.geometry import box  # noqa: E402

MAP = Path(__file__).resolve().parents[1]


@pytest.fixture()
def prepare(tmp_path, monkeypatch):
    if str(MAP) not in sys.path:
        sys.path.insert(0, str(MAP))
    spec = importlib.util.spec_from_file_location("prepare_under_test", MAP / "prepare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CACHE", tmp_path / "cache")
    return module


def _tiff(path, *, tiled, crs="EPSG:4326", count=1, dtype="uint8", size=256):
    data = (np.arange(size * size * count) % 251).astype(dtype).reshape(count, size, size)
    profile = dict(driver="GTiff", width=size, height=size, count=count, dtype=dtype,
                   transform=from_bounds(10, 45, 11, 46, size, size))
    if crs:
        profile["crs"] = crs
    if tiled:
        profile.update(tiled=True, blockxsize=128, blockysize=128)
    with rasterio.open(path, "w", **profile) as out:
        out.write(data)
    return str(path)


def _is_tiled(path):
    with rasterio.open(path) as dataset:
        return bool(dataset.profile.get("tiled"))


# ---- rasters ---------------------------------------------------------------------

def test_a_tiled_geotiff_is_read_by_the_browser_as_is(prepare, tmp_path):
    source = _tiff(tmp_path / "tiled.tif", tiled=True)
    result = prepare.main(source)
    assert result == {"status": "ok", "load": "raster", "path": source}


def test_a_striped_geotiff_becomes_a_cached_cog(prepare, tmp_path):
    source = _tiff(tmp_path / "striped.tif", tiled=False, size=1200, count=3)
    first = prepare.main(source)
    assert first["status"] == "ok" and first["converted"] and first["load"] == "raster"
    assert first["path"] != source and _is_tiled(first["path"])
    with rasterio.open(first["path"]) as cog, rasterio.open(source) as original:
        assert cog.count == 3 and cog.crs == original.crs
        assert np.array_equal(cog.read(), original.read())
    mtime = os.path.getmtime(first["path"])
    assert prepare.main(source)["path"] == first["path"]
    assert os.path.getmtime(first["path"]) == mtime, "the second open must reuse the cache"


def test_convert_reencodes_even_a_tiled_file(prepare, tmp_path):
    source = _tiff(tmp_path / "tiled.tif", tiled=True)
    result = prepare.main(source, action="convert")
    assert result["converted"] and result["path"] != source and _is_tiled(result["path"])


def test_other_gdal_rasters_are_converted(prepare, tmp_path):
    img = tmp_path / "scene.img"
    with rasterio.open(img, "w", driver="HFA", width=64, height=64, count=1, dtype="uint8",
                       crs="EPSG:3857", transform=from_bounds(0, 0, 1000, 1000, 64, 64)) as out:
        out.write(np.ones((1, 64, 64), "uint8"))
    result = prepare.main(str(img))
    assert result["status"] == "ok" and result["path"].endswith(".tif") and _is_tiled(result["path"])


def test_a_raster_with_no_crs_is_refused_with_a_reason(prepare, tmp_path):
    source = _tiff(tmp_path / "plain.tif", tiled=False, crs=None)
    result = prepare.main(source)
    assert result["status"] == "error"
    assert "coordinate reference system" in result["message"]


# ---- multidimensional ---------------------------------------------------------------

def _netcdf(path):
    ds = xr.Dataset(
        {"temp": (("time", "lat", "lon"), np.random.default_rng(0).random((3, 20, 40), dtype="float32")),
         "label": (("time",), np.array([1, 2, 3]))},
        coords={"time": np.array(["2024-01-01", "2024-02-01", "2024-03-01"], dtype="datetime64[ns]"),
                "lat": np.linspace(-45, 45, 20), "lon": np.linspace(-90, 90, 40)})
    ds.to_netcdf(path, engine="h5netcdf")
    return str(path)


def test_netcdf_becomes_a_consolidated_zarr_store_chunked_per_step(prepare, tmp_path):
    result = prepare.main(_netcdf(tmp_path / "t.nc"))
    assert result["status"] == "ok" and result["load"] == "zarr" and result["converted"]
    store = Path(result["path"])
    assert (store / ".zmetadata").exists(), "the browser lists variables from consolidated metadata"
    meta = json.loads((store / ".zmetadata").read_text())["metadata"]
    assert meta["temp/.zarray"]["chunks"][0] == 1, "one chunk per time step, so a slider reads one slice"
    assert "label/.zarray" not in meta, "non-gridded variables are left out"
    round_trip = xr.open_zarr(store)
    assert round_trip["temp"].shape == (3, 20, 40)
    assert str(round_trip.time.values[1])[:10] == "2024-02-01"


def test_a_file_with_nothing_gridded_is_refused(prepare, tmp_path):
    path = tmp_path / "series.nc"
    xr.Dataset({"v": (("t",), np.arange(5.0))}).to_netcdf(path, engine="h5netcdf")
    result = prepare.main(str(path))
    assert result["status"] == "error" and "gridded" in result["message"]


def test_a_zarr_store_is_handed_to_the_browser_untouched(prepare, tmp_path):
    store = tmp_path / "cube.zarr"
    xr.Dataset({"v": (("y", "x"), np.zeros((4, 4)))}, coords={"y": range(4), "x": range(4)}).to_zarr(store, zarr_format=2)
    assert prepare.main(str(store)) == {"status": "ok", "load": "zarr", "path": str(store)}
    assert prepare.main(str(store / ".zmetadata"))["load"] == "zarr"


# ---- vectors ------------------------------------------------------------------------

def _frame(crs=3857):
    return gpd.GeoDataFrame({"height": [3.0, 9.5], "use": ["res", "com"]},
                            geometry=[box(0, 0, 1000, 1000), box(2000, 2000, 3000, 3500)], crs=crs)


def test_browser_vector_formats_pass_through_and_a_shapefile_brings_its_sidecars(prepare, tmp_path):
    shp = tmp_path / "b.shp"
    _frame().to_file(shp)
    result = prepare.main(str(shp))
    assert result["load"] == "vector" and result["path"] == str(shp)
    assert sorted(Path(p).suffix for p in result["companions"]) == [".cpg", ".dbf", ".prj", ".shx"]
    gpkg = tmp_path / "b.gpkg"
    _frame().to_file(gpkg, driver="GPKG")
    assert prepare.main(str(gpkg)) == {"status": "ok", "load": "vector", "path": str(gpkg), "format": "gpkg"}


def test_the_geojson_fallback_reprojects_to_wgs84(prepare, tmp_path):
    gpkg = tmp_path / "b.gpkg"
    _frame().to_file(gpkg, driver="GPKG")
    result = prepare.main(str(gpkg), action="geojson")
    assert result["status"] == "ok" and result["format"] == "geojson"
    data = json.loads(Path(result["path"]).read_text())
    assert len(data["features"]) == 2
    x, y = data["features"][1]["geometry"]["coordinates"][0][0]
    assert -1 < x < 1 and -1 < y < 1, "coordinates must be degrees, not metres"


# ---- a user's .py -------------------------------------------------------------------

@pytest.mark.parametrize("body, load", [
    ("import geopandas as gpd\nfrom shapely.geometry import Point\n"
     "def main():\n    return gpd.GeoDataFrame({'a': [1]}, geometry=[Point(1000, 2000)], crs=3857)\n", "vector"),
    ("import pandas as pd\nresult = pd.DataFrame({'Lat': [1.0, 2.0], 'Lon': [3.0, 4.0]})\n", "vector"),
    ("def run():\n    return {'type': 'Point', 'coordinates': [1, 2]}\n", "vector"),
    ("import numpy as np\ndef main():\n    return np.ones((8, 8), 'float32'), (0, 0, 1, 1)\n", "raster"),
    ("import numpy as np, xarray as xr\ndef main():\n"
     "    return xr.DataArray(np.zeros((2, 3, 4)), dims=('time', 'lat', 'lon'), name='v',\n"
     "                        coords={'lat': [0, 1, 2], 'lon': [0, 1, 2, 3]})\n", "zarr"),
])
def test_a_script_result_is_written_as_a_browser_format(prepare, tmp_path, body, load):
    script = tmp_path / "layer.py"
    script.write_text(body)
    result = prepare.main(str(script))
    assert result["status"] == "ok", result
    assert result["load"] == load and result["from_script"]
    assert Path(result["path"]).exists()
    if result.get("format") == "geoparquet":
        frame = gpd.read_parquet(result["path"])
        assert frame.crs.to_epsg() == 4326


def test_a_script_returning_a_path_is_planned_like_that_file(prepare, tmp_path):
    tiled = _tiff(tmp_path / "t.tif", tiled=True)
    script = tmp_path / "layer.py"
    script.write_text(f"def main():\n    return {tiled!r}\n")
    result = prepare.main(str(script))
    assert result["path"] == tiled and result["from_script"]


@pytest.mark.parametrize("body, needle", [
    ("x = 1\n", "defines no main()"),
    ("def main():\n    return object()\n", "not something this map can draw"),
    ("import pandas as pd\ndef main():\n    return pd.DataFrame({'a': [1]})\n", "longitude/latitude"),
    ("def main():\n    raise ValueError('boom')\n", "ValueError: boom"),
])
def test_script_refusals_say_why(prepare, tmp_path, body, needle):
    script = tmp_path / "layer.py"
    script.write_text(body)
    result = prepare.main(str(script))
    assert result["status"] == "error" and needle in result["message"]


# ---- inspect -------------------------------------------------------------------------

def test_inspect_describes_rasters_vectors_and_cubes(prepare, tmp_path):
    raster = prepare.main(_tiff(tmp_path / "r.tif", tiled=True, count=3), action="inspect")
    assert raster["kind"] == "raster" and raster["bands"] == 3 and raster["crs"] == "EPSG:4326"
    assert raster["bounds"] == [10.0, 45.0, 11.0, 46.0]
    gpkg = tmp_path / "v.gpkg"
    _frame().to_file(gpkg, driver="GPKG")
    vector = prepare.main(str(gpkg), action="inspect")
    assert vector["kind"] == "vector" and vector["features"] == 2 and set(vector["fields"]) == {"height", "use"}
    cube = prepare.main(_netcdf(tmp_path / "c.nc"), action="inspect")
    assert cube["kind"] == "zarr" and cube["variables"]["temp"]["dims"] == {"time": 3, "lat": 20, "lon": 40}


# ---- review follow-ups ----------------------------------------------------------------

@pytest.mark.parametrize("lon, lat", [("longitude", "lat"), ("lon", "latitude"), ("Long", "Lat"), ("x", "y")])
def test_any_longitude_name_pairs_with_any_latitude_name(prepare, tmp_path, lon, lat):
    script = tmp_path / "layer.py"
    script.write_text("import pandas as pd\n"
                      f"result = pd.DataFrame({{{lon!r}: [3.0, 4.0], {lat!r}: [1.0, 2.0]}})\n")
    result = prepare.main(str(script))
    assert result["status"] == "ok" and result["load"] == "vector", result
    frame = gpd.read_parquet(result["path"])
    assert frame.crs.to_epsg() == 4326 and list(frame.geometry.x) == [3.0, 4.0]


def test_projected_numbers_without_a_crs_are_refused_not_misplaced(prepare, tmp_path):
    script = tmp_path / "layer.py"
    script.write_text("import pandas as pd\nresult = pd.DataFrame({'x': [500000.0], 'y': [4649776.0]})\n")
    result = prepare.main(str(script))
    assert result["status"] == "error" and "no coordinate reference system" in result["message"]
    gpkg = tmp_path / "nocrs.gpkg"
    _frame(crs=None).to_file(gpkg, driver="GPKG")  # metre-sized boxes, no CRS
    refused = prepare.main(str(gpkg), action="geojson")
    assert refused["status"] == "error" and "no coordinate reference system" in refused["message"]


def test_degrees_without_a_crs_are_taken_as_wgs84(prepare, tmp_path):
    gpkg = tmp_path / "deg.gpkg"
    gpd.GeoDataFrame({"a": [1]}, geometry=[box(10, 45, 11, 46)]).to_file(gpkg, driver="GPKG")
    result = prepare.main(str(gpkg), action="geojson")
    assert result["status"] == "ok"
    x, y = json.loads(Path(result["path"]).read_text())["features"][0]["geometry"]["coordinates"][0][0]
    assert 10 <= x <= 11 and 45 <= y <= 46

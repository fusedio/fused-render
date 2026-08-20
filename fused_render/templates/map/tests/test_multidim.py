"""Tests for the multidim (NetCDF/Zarr/HDF5) engine (multidim_engine.py).

Synthetic datasets built in tmp_path exercise the titiler-xarray slice pipeline
end to end; the real samples (air_temperature.nc, era5_sample.zarr, ...) are
used only when their folder is present.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_multidim.py -o addopts=""
"""
import importlib.util
import io
import os
import sys

import pytest

np = pytest.importorskip("numpy")
xr = pytest.importorskip("xarray")
pytest.importorskip("rioxarray")
pytest.importorskip("rio_tiler")

_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_MAP), "shared")
_SAMPLES = os.environ.get("MULTIDIM_SAMPLES", r"C:\work\fused\testdata\multidim")

needs_samples = pytest.mark.skipif(
    not os.path.isdir(_SAMPLES), reason="sample datasets not present"
)


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
def eng():
    me = _load("multidim_engine", "multidim_engine.py")
    return me.MultidimEngine(base_url="http://127.0.0.1:9999", token="tok")


def _describe(eng, target, **opts):
    return eng.try_describe(
        {"target": str(target), "artifact_id": "art", "opts": opts}
    )


def _synthetic():
    times = np.array(
        ["2020-01-01T00:00", "2020-01-01T06:00", "2020-01-01T12:00"],
        dtype="datetime64[ns]",
    )
    levels = np.array([1000.0, 850.0, 500.0])
    lat = np.linspace(60.0, 20.0, 21)
    lon = np.linspace(-30.0, 30.0, 25)
    base = lat[:, None] + np.zeros((21, 25))
    data = np.stack(
        [
            np.stack([base + step * 10 + level / 100.0 for level in levels])
            for step in range(3)
        ]
    )
    return xr.Dataset(
        {
            "t2m": (
                ("time", "level", "lat", "lon"),
                data,
                {"units": "K", "long_name": "temperature"},
            ),
            "elevation": (("lat", "lon"), base * 2.0, {"units": "m"}),
            "station": (("time",), np.arange(3.0)),
        },
        coords={
            "time": times,
            "level": ("level", levels, {"units": "hPa"}),
            "lat": lat,
            "lon": lon,
        },
    )


def _mean_brightness(png):
    from PIL import Image

    pixels = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"), dtype=float)
    opaque = pixels[..., 3] > 0
    assert opaque.any()
    return pixels[..., :3][opaque].mean()


def test_variables_are_enumerated_and_non_spatial_ones_skipped(eng, tmp_path):
    path = tmp_path / "synth.nc"
    _synthetic().to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "ok"
    multidim = descriptor["stats"]["multidim"]
    names = [entry["name"] for entry in multidim["variables"]]
    assert names == ["t2m", "elevation"]  # "station" has no spatial dims
    assert multidim["variable"] == "t2m"  # first spatial var is the default
    assert [dim["name"] for dim in multidim["dims"]] == ["time", "level"]
    assert all(dim["index"] == 0 for dim in multidim["dims"])

    elevation = _describe(eng, path, var="elevation")
    assert elevation["stats"]["multidim"]["variable"] == "elevation"
    assert elevation["stats"]["multidim"]["dims"] == []


def test_0_360_longitude_rolls_and_descending_latitude_is_not_flipped(eng, tmp_path):
    lat = np.linspace(80.0, -80.0, 41)  # descending, like air.mon.mean.nc
    lon = np.arange(0.0, 360.0, 2.5)
    data = np.broadcast_to(lat[:, None], (41, lon.size)).copy()
    ds = xr.Dataset(
        {"value": (("lat", "lon"), data)}, coords={"lat": lat, "lon": lon}
    )
    path = tmp_path / "globe.nc"
    ds.to_netcdf(path, engine="h5netcdf")

    descriptor = _describe(eng, path)
    assert descriptor["status"] == "ok"
    west, _, east, _ = descriptor["bounds"]
    assert west < 0 < east  # rolled out of 0-360
    assert east <= 181  # within a half-cell of the antimeridian

    source_id = descriptor["data"]["source_id"]
    north = eng.tile(source_id, 1, 0, 0)
    south = eng.tile(source_id, 1, 0, 1)
    # Values equal latitude, so with viridis the northern tile must be the
    # brighter one; a flipped render inverts that.
    assert _mean_brightness(north) > _mean_brightness(south)


def test_cf_time_decodes_to_iso_labels_and_level_labels_carry_units(eng, tmp_path):
    path = tmp_path / "synth.nc"
    _synthetic().to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    dims = {dim["name"]: dim for dim in descriptor["stats"]["multidim"]["dims"]}
    assert dims["time"]["labels"] == [
        "2020-01-01T00:00", "2020-01-01T06:00", "2020-01-01T12:00",
    ]
    assert dims["level"]["labels"] == ["1000 hPa", "850 hPa", "500 hPa"]
    assert dims["level"]["units"] == "hPa"
    assert not dims["time"]["sparse"]


def test_sel_changes_the_tile_and_the_source_id(eng, tmp_path):
    path = tmp_path / "synth.nc"
    _synthetic().to_netcdf(path, engine="h5netcdf")
    first = _describe(eng, path, sel={"time": 0}, rescale=[0, 100])
    second = _describe(eng, path, sel={"time": 2, "level": 1}, rescale=[0, 100])
    assert first["data"]["source_id"] != second["data"]["source_id"]
    dims = {dim["name"]: dim for dim in second["stats"]["multidim"]["dims"]}
    assert dims["time"]["index"] == 2
    assert dims["level"]["index"] == 1
    tile_first = eng.tile(first["data"]["source_id"], 1, 0, 0)
    tile_second = eng.tile(second["data"]["source_id"], 1, 0, 0)
    assert tile_first != tile_second


def test_netcdf3_falls_back_to_the_scipy_engine(eng, tmp_path):
    path = tmp_path / "classic.nc"
    _synthetic().to_netcdf(path, format="NETCDF3_CLASSIC")
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "ok"
    assert descriptor["stats"]["multidim"]["engine"] == "scipy"


def test_hdf5_opens_via_h5netcdf(eng, tmp_path):
    path = tmp_path / "grid.h5"
    _synthetic()[["elevation"]].to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "ok"
    assert descriptor["stats"]["multidim"]["engine"] == "h5netcdf"
    assert descriptor["stats"]["multidim"]["variable"] == "elevation"


def test_local_zarr_directory_store(eng, tmp_path):
    store = tmp_path / "synth.zarr"
    _synthetic().to_zarr(store, mode="w")
    descriptor = _describe(eng, store)
    assert descriptor["status"] == "ok"
    assert descriptor["stats"]["multidim"]["engine"] == "zarr"
    assert eng.tile(descriptor["data"]["source_id"], 1, 0, 0)[:4] == b"\x89PNG"


def test_hdf4_gets_an_actionable_error(eng, tmp_path):
    path = tmp_path / "legacy.hdf"
    path.write_bytes(b"\x0e\x03\x13\x01" + bytes(512))
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "error"
    assert "HDF4" in descriptor["message"]
    assert "convert" in descriptor["message"].lower()


def test_curvilinear_grids_fail_gracefully(eng, tmp_path):
    ny, nx = 5, 6
    yc = np.linspace(40.0, 50.0, ny)[:, None] + np.zeros((ny, nx))
    xc = np.linspace(-10.0, 10.0, nx)[None, :] + np.zeros((ny, nx))
    ds = xr.Dataset(
        {"Tair": (("y", "x"), np.random.rand(ny, nx))},
        coords={"yc": (("y", "x"), yc), "xc": (("y", "x"), xc)},
    )
    path = tmp_path / "curvi.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "error"
    assert "curvilinear" in descriptor["message"]


def test_tiles_are_png_and_outside_bounds_is_transparent(eng, tmp_path):
    path = tmp_path / "synth.nc"
    _synthetic().to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    source_id = descriptor["data"]["source_id"]
    assert eng.tile(source_id, 1, 0, 0)[:4] == b"\x89PNG"
    # Data covers lon -30..30; z2 x0 is lon -180..-90, entirely outside.
    assert eng.tile(source_id, 2, 0, 1) == eng.transparent_tile()
    assert eng.tile("unknown", 0, 0, 0) is None


def test_backend_files_cover_the_multidim_engine():
    mr = _load("map_render", "map_render.py")
    raster = _load("raster_engine", "raster_engine.py")
    stems = {os.path.basename(str(path)) for path in mr.BACKEND_FILES}
    assert "multidim_engine.py" in stems
    assert {".nc4", ".hdf5", ".he5"} <= raster.RASTER_SUFFIXES
    for target in ("air.nc4", "scene.hdf5", "swath.he5", r"C:\data\era5.zarr"):
        assert mr._looks_like_raster(target), target


@needs_samples
def test_real_netcdf3_0_360_sample(eng):
    descriptor = _describe(eng, os.path.join(_SAMPLES, "air_temperature.nc"))
    assert descriptor["status"] == "ok"
    multidim = descriptor["stats"]["multidim"]
    assert multidim["engine"] == "scipy"
    assert multidim["variable"] == "air"
    assert descriptor["bounds"][2] <= 181
    assert multidim["dims"][0]["labels"][0] == "2013-01-01T00:00"


@needs_samples
def test_real_era5_zarr_sample(eng):
    descriptor = _describe(eng, os.path.join(_SAMPLES, "era5_sample.zarr"))
    assert descriptor["status"] == "ok"
    multidim = descriptor["stats"]["multidim"]
    assert multidim["engine"] == "zarr"
    assert len(multidim["variables"]) == 4
    assert eng.tile(descriptor["data"]["source_id"], 1, 0, 0)[:4] == b"\x89PNG"


@needs_samples
def test_real_hdf5_sample(eng):
    descriptor = _describe(eng, os.path.join(_SAMPLES, "modis_like.h5"))
    assert descriptor["status"] == "ok"
    names = [v["name"] for v in descriptor["stats"]["multidim"]["variables"]]
    assert names == ["LST_Day_1km", "NDVI"]
    assert descriptor["stats"]["multidim"]["dims"] == []


@needs_samples
def test_real_curvilinear_sample(eng):
    descriptor = _describe(eng, os.path.join(_SAMPLES, "rasm.nc"))
    assert descriptor["status"] == "error"
    assert "curvilinear" in descriptor["message"]


def test_rewritten_file_serves_fresh_pixels_after_eviction(eng, tmp_path):
    # Regression: the dataset and slice caches were keyed by path alone, so a
    # file rewritten while its handle was out of the cache kept serving the
    # old pixels under a new source_id.
    path = tmp_path / "mutable.nc"

    def write(seed):
        rng = np.random.default_rng(seed)
        xr.Dataset(
            {"t": (("lat", "lon"), rng.random((10, 10)).astype("float32"))},
            coords={"lat": np.linspace(50, 40, 10), "lon": np.linspace(0, 10, 10)},
        ).to_netcdf(path, engine="h5netcdf")

    write(1)
    first = _describe(eng, path)
    first_tile = eng.tile(first["data"]["source_id"], 0, 0, 0)
    with eng.data_lock:
        for key in list(eng.datasets):
            eng._evict_dataset(key, eng.datasets.pop(key)[1])
    write(2)
    second = _describe(eng, path)
    assert second["data"]["source_id"] != first["data"]["source_id"]
    assert eng.tile(second["data"]["source_id"], 0, 0, 0) != first_tile


def test_plain_json_named_zarr_json_is_not_claimed(eng, tmp_path):
    # Regression: any file literally named zarr.json was treated as a zarr
    # store, and the error descriptor blocked the vector/JSON fallback.
    me = _load("geo_paths", "geo_paths.py")
    plain = tmp_path / "zarr.json"
    plain.write_text('{"type": "FeatureCollection", "features": []}', encoding="utf-8")
    assert me.multidim_suffix(str(plain)) == ""
    assert eng.try_describe({"target": str(plain), "artifact_id": "a", "opts": {}}) is None
    store = tmp_path / "store"
    store.mkdir()
    metadata = store / "zarr.json"
    metadata.write_text('{"zarr_format": 3, "node_type": "group"}', encoding="utf-8")
    assert me.multidim_suffix(str(metadata)) == ".zarr"
    assert me.multidim_suffix("https://example.com/store/zarr.json") == ".zarr"


def test_sources_registry_is_bounded(eng, tmp_path):
    path = tmp_path / "synth.nc"
    _synthetic().to_netcdf(path, engine="h5netcdf")
    me = sys.modules["multidim_engine"]
    for step in range(3):
        for level in range(3):
            _describe(eng, path, sel={"time": step, "level": level})
    assert len(eng.sources) <= me.MAX_SOURCES


def test_regional_grid_crossing_the_antimeridian_errors(eng, tmp_path):
    lat = np.linspace(60.0, 40.0, 11)
    lon = np.arange(150.0, 210.0, 2.5)  # Fiji-style, straddles the 180 seam
    ds = xr.Dataset(
        {"value": (("lat", "lon"), np.random.rand(11, lon.size))},
        coords={"lat": lat, "lon": lon},
    )
    path = tmp_path / "fiji.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "error"
    assert "antimeridian" in descriptor["message"]


def test_bare_xy_meters_grid_without_crs_is_not_assumed_geographic(eng, tmp_path):
    ds = xr.Dataset(
        {"depth": (("y", "x"), np.random.rand(5, 6))},
        coords={
            "y": np.arange(0.0, 500000.0, 100000.0),
            "x": np.arange(0.0, 600000.0, 100000.0),
        },
    )
    path = tmp_path / "projected.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "error"
    assert "CRS" in descriptor["message"]


def test_worker_adopts_a_not_georeferenced_raster_fallback(tmp_path):
    worker = _load("map_worker", "worker.py")

    class MultidimStub:
        def try_describe(self, request, obj=None):
            return {"status": "error", "message": "no CRS"}

    class RasterStub:
        def try_describe(self, request, obj=None):
            return {
                "status": "not_georeferenced",
                "kind": None,
                "bounds": None,
                "data": {},
                "message": "Raster has no directly usable CRS.",
                "warnings": [],
            }

    descriptor = worker.build(
        {
            "target": str(tmp_path / "bare.nc"),
            "artifact_dir": str(tmp_path),
            "artifact_id": "art",
            "opts": {},
        },
        raster_engine=RasterStub(),
        multidim_engine=MultidimStub(),
    )
    assert descriptor["status"] == "not_georeferenced"


def test_empty_dim_gives_a_readable_error(eng, tmp_path):
    ds = xr.Dataset(
        {"t": (("time", "lat", "lon"), np.zeros((0, 4, 5)))},
        coords={
            "time": np.array([], dtype="datetime64[ns]"),
            "lat": np.linspace(50, 40, 4),
            "lon": np.linspace(0, 10, 5),
        },
    )
    path = tmp_path / "empty.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "error"
    assert "empty" in descriptor["message"]


def test_source_eviction_prefers_the_scrubbed_layer(eng, tmp_path, monkeypatch):
    me = sys.modules["multidim_engine"]
    monkeypatch.setattr(me, "MAX_SOURCES", 4)
    path = tmp_path / "synth.nc"
    _synthetic().to_netcdf(path, engine="h5netcdf")
    elevation = _describe(eng, path, var="elevation")
    for step in range(3):
        for level in range(2):
            _describe(eng, path, var="t2m", sel={"time": step, "level": level})
    # The t2m scrub overflowed the registry; it must have replaced its own
    # history rather than the quiet elevation layer's source.
    assert elevation["data"]["source_id"] in eng.sources
    assert len(eng.sources) <= 4


def test_failed_builds_are_cached_without_frames_and_swept(eng, tmp_path):
    ny, nx = 5, 6
    yc = np.linspace(40.0, 50.0, ny)[:, None] + np.zeros((ny, nx))
    xc = np.linspace(-10.0, 10.0, nx)[None, :] + np.zeros((ny, nx))
    ds = xr.Dataset(
        {"Tair": (("y", "x"), np.random.rand(ny, nx))},
        coords={"yc": (("y", "x"), yc), "xc": (("y", "x"), xc)},
    )
    path = tmp_path / "curvi.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    assert _describe(eng, path)["status"] == "error"
    with eng.data_lock:
        failed = [
            value for state, value in eng.slices.values() if state == "failed"
        ]
        assert failed and failed[0][0].__traceback__ is None
        # Age the failure past its 5s TTL; the next ready insert sweeps it.
        for key, (state, value) in list(eng.slices.items()):
            if state == "failed":
                eng.slices[key] = ("failed", (value[0], value[1] - 6))
    good = tmp_path / "good.nc"
    _synthetic().to_netcdf(good, engine="h5netcdf")
    assert _describe(eng, good)["status"] == "ok"
    with eng.data_lock:
        assert not any(state == "failed" for state, _ in eng.slices.values())


def test_global_0_360_inclusive_grid_rolls_without_seam_error(eng, tmp_path):
    # A grid shipping the seam column twice (lon 0 AND 360) must roll into a
    # clean global grid, not be misread as crossing the antimeridian.
    path = tmp_path / "inclusive.nc"
    lon = np.linspace(0.0, 360.0, 73)
    lat = np.linspace(80.0, -80.0, 33)
    data = np.tile(lon, (33, 1)).astype("float32")
    xr.Dataset(
        {"t": (("lat", "lon"), data)},
        coords={"lat": lat, "lon": lon},
    ).to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "ok"
    assert descriptor["bounds"][0] >= -183 and descriptor["bounds"][2] <= 183
    assert eng.tile(descriptor["data"]["source_id"], 1, 0, 0)[:4] == b"\x89PNG"


def test_degree_unit_spellings_pass_the_geographic_gate(eng, tmp_path):
    path = tmp_path / "spellings.nc"
    xr.Dataset(
        {"t": (("y", "x"), np.random.default_rng(3).random((8, 8)).astype("float32"))},
        coords={
            "y": ("y", np.linspace(50, 40, 8), {"units": "degree_north"}),
            "x": ("x", np.linspace(0, 10, 8), {"units": "degrees_E"}),
        },
    ).to_netcdf(path, engine="h5netcdf")
    descriptor = _describe(eng, path)
    assert descriptor["status"] == "ok"


def test_zarr_metadata_locator_keeps_its_query_string(eng):
    # Regression: trimming the object name off the whole locator ate into a
    # signed URL's query, leaving a store path that could never open.
    me = _load("geo_paths", "geo_paths.py")
    assert me.zarr_store("https://h/s.zarr/zarr.json?sig=a&se=b") == "https://h/s.zarr?sig=a&se=b"
    assert me.zarr_store("https://h/s.zarr/.zmetadata?sig=a") == "https://h/s.zarr?sig=a"
    assert me.zarr_store("https://h/s.zarr/zarr.json") == "https://h/s.zarr"
    assert me.zarr_store("https://h/s.zarr") == "https://h/s.zarr"
    assert me.zarr_store(os.path.join("C:", "d", "s.zarr", "zarr.json")).endswith("s.zarr")

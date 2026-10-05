"""The Map Viewer's only Python on the rendering path: turn a source the browser
cannot read into one it can, and say how to draw it.

The page draws everything itself — a COG straight off disk or the network over
HTTP range requests, vectors through DuckDB-WASM, Zarr through a MapLibre
custom layer, PMTiles through its protocol — so for the common formats Python
is never called at all. This module is asked only about the rest:

  plan       striped / non-TIFF GDAL rasters -> a local COG; NetCDF/HDF5 -> a
             Zarr store; a user's `.py` -> whatever it returned, written out as
             one of the browser formats. Returns `{load, path, ...}`: which
             browser loader to use, on which file.
  geojson    any OGR vector -> GeoJSON in EPSG:4326, for when DuckDB-WASM
             cannot load (offline: its wasm and spatial extension come from a
             CDN on first use).
  localize   a remote raster the browser was refused (no CORS, or an Azure
             container that needs a Planetary Computer token) -> a local COG,
             read by GDAL over /vsicurl/.
  convert    a TIFF the browser reader failed on even though it is tiled (a
             codec the reader lacks) -> re-encoded as a DEFLATE COG.
  pointcloud a LAS/LAZ the browser reader refused (LAS 1.4, a GeoTIFF-key or
             missing CRS) -> LAS 1.2 in longitude/latitude, thinned to a
             point budget; `crs` names the CRS of a file that has none.
  crs        `crs` (WKT or EPSG code) -> proj4 and `bounds` in lon/lat, for a
             Zarr grid in projected units.
  inspect    what a source holds (bands, CRS, fields, variables...), for the
             MCP `describe_source` tool and the code panel.

Conversions are cached under <home>/cache/map-v3, keyed by path, size, mtime
and this module's own hash, so reopening a map is instant.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import warnings
from pathlib import Path
from typing import Any

if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "prepare.py")
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from geo_paths import base_home, is_http_url, is_remote_path, multidim_suffix  # noqa: E402

CACHE = Path(os.environ.get("FUSED_RENDER_MAP_CACHE") or base_home() / "cache" / "map-v3")

# What the browser reads natively, by extension. Everything else needs `plan`.
BROWSER_RASTER = (".tif", ".tiff", ".cog")
BROWSER_VECTOR = (".geojson", ".json", ".parquet", ".geoparquet", ".fgb", ".csv",
                  ".tsv", ".gpkg", ".shp", ".zip", ".kml", ".kmz", ".gml")
GDAL_RASTER = (".vrt", ".jp2", ".j2k", ".img", ".ntf", ".nitf", ".dem", ".dt0",
               ".dt1", ".dt2", ".hgt", ".grd", ".asc", ".png", ".jpg", ".jpeg")
MULTIDIM_FILES = (".nc", ".nc4", ".h5", ".hdf5", ".he5", ".hdf", ".cdf")
SHAPEFILE_SIDECARS = (".shx", ".dbf", ".prj", ".cpg")

ENTRYPOINTS = ("main", "run", "udf", "fn")
RESULT_VARS = ("result", "output", "layer", "gdf", "df")


def _module_version() -> str:
    try:
        with open(os.path.abspath(__file__), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:10]
    except OSError:
        return "0"


VERSION = _module_version()


class Refusal(Exception):
    """A source this map cannot show, with the reason the user should read."""


def _suffix(path: str) -> str:
    name = path.split("?", 1)[0].rstrip("/\\").lower()
    return os.path.splitext(name)[1]


def _cache_key(path: str, action: str, extra: str = "") -> str:
    stamp = ""
    if not is_remote_path(path):
        try:
            stat = os.stat(path)
            stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            pass
    raw = f"{VERSION}|{action}|{os.path.abspath(path) if not is_remote_path(path) else path}|{stamp}|{extra}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cache_path(key: str, suffix: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / f"{key}{suffix}"


def _publish(tmp: Path, final: Path) -> None:
    """Move a finished artifact into place in one step, so a crash mid-write
    never leaves a half file that the next open would take as cached."""
    if final.is_dir():
        shutil.rmtree(final, ignore_errors=True)
    elif final.exists():
        final.unlink()
    os.replace(tmp, final)


# ---- rasters ------------------------------------------------------------------

def _is_streamable_tiff(path: str) -> bool:
    """Whether the browser reader can stream this TIFF: tiled, not striped.
    Overviews are not required (the reader falls back to full resolution)."""
    import rasterio

    with rasterio.open(path) as dataset:
        if dataset.driver != "GTiff" or dataset.crs is None:
            return False
        if dataset.profile.get("tiled"):
            return True
        # One strip holding the whole image reads the same as one tile.
        block_h, block_w = dataset.block_shapes[0]
        return block_h >= dataset.height and block_w >= dataset.width


def _to_cog(source: str, final: Path) -> Path:
    import rasterio
    from rasterio.shutil import copy as rio_copy

    with rasterio.open(source) as dataset:
        if dataset.crs is None and not dataset.gcps[0]:
            raise Refusal(
                "This raster has no coordinate reference system, so it cannot be "
                "placed on a map.")
        needs_warp = dataset.crs is None  # GCPs only
    tmp = final.with_name(final.name + f".{os.getpid()}.tmp")
    if needs_warp:
        from rasterio.vrt import WarpedVRT
        with rasterio.open(source) as dataset, WarpedVRT(dataset, crs="EPSG:4326") as vrt:
            rio_copy(vrt, str(tmp), driver="COG", compress="DEFLATE",
                     blocksize=512, BIGTIFF="IF_SAFER")
    else:
        rio_copy(source, str(tmp), driver="COG", compress="DEFLATE",
                 blocksize=512, BIGTIFF="IF_SAFER", RESAMPLING="AVERAGE")
    _publish(tmp, final)
    return final


def _raster_plan(path: str) -> dict:
    final = _cache_path(_cache_key(path, "cog"), ".tif")
    if not final.exists():
        _to_cog(path, final)
    return {"load": "raster", "path": str(final), "converted": True,
            "note": "converted to a Cloud-Optimized GeoTIFF for streaming"}


def _fetchable(url: str) -> str:
    from blob_tokens import TOKENS, container_of

    if not container_of(url):
        return url
    try:
        return TOKENS.sign(url)
    except (OSError, ValueError, KeyError):
        return url


def _localize(url: str) -> dict:
    """A remote raster the browser could not read, fetched by GDAL instead."""
    from blob_tokens import unsigned

    final = _cache_path(_cache_key(unsigned(url), "localize"), ".tif")
    if not final.exists():
        _to_cog("/vsicurl/" + _fetchable(url), final)
    return {"load": "raster", "path": str(final), "converted": True,
            "note": "copied locally (the server does not allow browser reads)"}


# ---- multidimensional -----------------------------------------------------------

def _open_dataset(path: str):
    import xarray as xr

    errors = []
    for engine in ("h5netcdf", "scipy", None):
        try:
            return xr.open_dataset(path, engine=engine, decode_timedelta=False)
        except Exception as error:  # every engine gets its turn
            errors.append(f"{engine or 'default'}: {error}")
    raise Refusal("Could not open this file as NetCDF/HDF5 — " + "; ".join(errors))


_SPATIAL = {"lat", "latitude", "y", "lon", "longitude", "x", "rlat", "rlon", "nav_lat", "nav_lon"}


def _gridded_vars(dataset) -> list[str]:
    names = []
    for name, variable in dataset.data_vars.items():
        dims = {d.lower() for d in variable.dims}
        if variable.ndim >= 2 and len(dims & _SPATIAL) >= 2 and variable.dtype.kind in "iuf":
            names.append(name)
    return names


def _dataset_to_zarr(dataset, final: Path) -> Path:
    """Write the gridded variables as a consolidated Zarr v2 store the browser
    layer reads chunk by chunk: one step per non-spatial index, 512px tiles."""
    variables = _gridded_vars(dataset)
    if not variables:
        raise Refusal("No gridded (lat/lon or y/x) numeric variable to draw in this file.")
    keep = set(variables)
    # Keep grid_mapping variables: the layer reads CF crs_wkt from them.
    for name in variables:
        mapping = dataset[name].attrs.get("grid_mapping")
        if mapping and mapping in dataset.variables:
            keep.add(mapping)
    subset = dataset[sorted(keep)]
    for name in subset.variables:
        subset[name].encoding = {}
        chunks = tuple(
            min(size, 512) if dim.lower() in _SPATIAL else 1
            for dim, size in zip(subset[name].dims, subset[name].shape)
        )
        if name in variables:
            subset[name].encoding["chunks"] = chunks
    tmp = final.with_name(final.name + f".{os.getpid()}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    subset.to_zarr(str(tmp), mode="w", zarr_format=2, consolidated=True)
    _publish(tmp, final)
    return final


def _multidim_plan(path: str) -> dict:
    local = path
    if is_http_url(path):
        local = str(_download(path))
    final = _cache_path(_cache_key(local, "zarr"), ".zarr")
    if not final.exists():
        with _open_dataset(local) as dataset:
            _dataset_to_zarr(dataset, final)
    return {"load": "zarr", "path": str(final), "converted": True,
            "note": "converted to Zarr for streaming"}


def _download(url: str) -> Path:
    import requests
    from blob_tokens import unsigned

    final = _cache_path(_cache_key(unsigned(url), "download"), _suffix(url) or ".bin")
    if final.exists():
        return final
    tmp = final.with_name(final.name + f".{os.getpid()}.tmp")
    with requests.get(_fetchable(url), stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(tmp, "wb") as handle:
            for block in response.iter_content(1 << 20):
                handle.write(block)
    _publish(tmp, final)
    return final


# ---- point clouds -------------------------------------------------------------------

POINTCLOUD_FILES = (".las", ".laz")
POINTCLOUD_BUDGET = 5_000_000


def _pointcloud_plan(path: str, crs: str = "") -> dict:
    """A LAS/LAZ the browser refused (it reads LAS <= 1.3 with a WKT CRS) as
    LAS 1.2 in longitude/latitude, thinned to POINTCLOUD_BUDGET points. `crs`
    names the CRS of a file whose header has none."""
    import laspy
    import numpy as np
    from pyproj import CRS, Transformer

    local = str(_download(path)) if is_remote_path(path) else path
    final = _cache_path(_cache_key(local, "pointcloud", crs), ".las")
    if final.exists():
        with laspy.open(local) as source, laspy.open(str(final)) as cached:
            total, kept = source.header.point_count, cached.header.point_count
        return _pointcloud_result(final, total, kept)
    with laspy.open(local) as reader:
        header = reader.header
        source_crs = CRS.from_user_input(crs) if crs else header.parse_crs()
        mins, maxs = header.mins, header.maxs
        if source_crs is None:
            if -180 <= mins[0] <= maxs[0] <= 180 and -90 <= mins[1] <= maxs[1] <= 90:
                source_crs = CRS.from_epsg(4326)
            else:
                raise Refusal(
                    "This point cloud has no coordinate system in its header and its "
                    f"coordinates are not longitude/latitude (x {mins[0]:g}…{maxs[0]:g}, "
                    f"y {mins[1]:g}…{maxs[1]:g}). Set its coordinate system (an EPSG "
                    "code such as EPSG:2992) in the layer's style panel.")
        horizontal = source_crs.sub_crs_list[0] if source_crs.is_compound else source_crs
        vertical = source_crs.sub_crs_list[1] if source_crs.is_compound else horizontal
        # Elevations in metres: US surveys often state both axes in feet.
        z_factor = 1.0
        if vertical.axis_info and not vertical.is_geographic:
            z_factor = vertical.axis_info[-1].unit_conversion_factor or 1.0
        to_wgs84 = Transformer.from_crs(horizontal, 4326, always_xy=True)
        names = set(header.point_format.dimension_names)
        rgb = {"red", "green", "blue"} <= names
        step = max(1, -(-header.point_count // POINTCLOUD_BUDGET))
        out_header = laspy.LasHeader(point_format=3 if rgb else 1, version="1.2")
        out_header.scales = np.array([1e-7, 1e-7, 0.001])
        lon0, lat0 = to_wgs84.transform((mins[0] + maxs[0]) / 2, (mins[1] + maxs[1]) / 2)
        out_header.offsets = np.array([round(lon0, 2), round(lat0, 2), 0.0])
        tmp = final.with_name(final.name + f".{os.getpid()}.tmp")
        with laspy.open(str(tmp), mode="w", header=out_header) as writer:
            seen = kept = 0
            for chunk in reader.chunk_iterator(2_000_000):
                # Every step-th point of the whole file, across chunk edges.
                pick = slice((-seen) % step, None, step)
                seen += len(chunk)
                x, y = np.asarray(chunk.x[pick]), np.asarray(chunk.y[pick])
                if not len(x):
                    continue
                kept += len(x)
                lon, lat = to_wgs84.transform(x, y)
                points = laspy.ScaleAwarePointRecord.zeros(len(x), header=out_header)
                points.x, points.y = lon, lat
                points.z = np.asarray(chunk.z[pick]) * z_factor
                for name in ("intensity", "gps_time") + (("red", "green", "blue") if rgb else ()):
                    if name in names:
                        points[name] = np.asarray(chunk[name][pick])
                # LAS 1.4 counts up to 15 returns; LAS 1.2 holds 7.
                for name in ("return_number", "number_of_returns"):
                    if name in names:
                        points[name] = np.minimum(np.asarray(chunk[name][pick]), 7)
                if "classification" in names:
                    # LAS 1.2 holds codes 0-31; higher user codes read as unclassified.
                    codes = np.asarray(chunk.classification[pick])
                    points.classification = np.where(codes > 31, 1, codes)
                writer.write_points(points)
    _publish(tmp, final)
    return _pointcloud_result(final, header.point_count, kept)


def _pointcloud_result(final: Path, total: int, kept: int) -> dict:
    thinned = f": every {-(-total // kept)}th point ({kept:,} of {total:,})" if kept < total else ""
    return {"load": "pointcloud", "path": str(final), "converted": True,
            "note": "converted by Python" + thinned}


def _pointcloud_info(path: str) -> dict:
    import laspy

    local = str(_download(path)) if is_remote_path(path) else path
    with laspy.open(local) as reader:
        header = reader.header
        crs = header.parse_crs()
        return {
            "kind": "pointcloud",
            "points": int(header.point_count),
            "version": str(header.version),
            "point_format": int(header.point_format.id),
            "dimensions": list(header.point_format.dimension_names),
            "crs": crs.to_string() if crs else None,
            "native_bounds": [float(v) for v in (*header.mins, *header.maxs)],
        }


# ---- vectors ----------------------------------------------------------------------

def shapefile_companions(path: str) -> list[str]:
    stem = os.path.splitext(path)[0]
    found = []
    for suffix in SHAPEFILE_SIDECARS:
        for candidate in (stem + suffix, stem + suffix.upper()):
            if os.path.exists(candidate):
                found.append(candidate)
                break
    return found


def _to_geojson(path: str, layer: str = "") -> dict:
    import pyogrio

    final = _cache_path(_cache_key(path, "geojson", layer), ".geojson")
    if not final.exists():
        tmp = final.with_name(final.name + f".{os.getpid()}.tmp")
        frame = _to_wgs84(pyogrio.read_dataframe(path, layer=layer or None))
        pyogrio.write_dataframe(frame, str(tmp), driver="GeoJSON")
        _publish(tmp, final)
    return {"load": "vector", "path": str(final), "format": "geojson",
            "converted": True}


def _to_wgs84(frame):
    """`frame` in EPSG:4326, which is what the page draws. A frame with no CRS
    is taken as longitude/latitude only when its coordinates could be; anything
    else is refused rather than drawn somewhere wrong."""
    import math

    if frame.crs is not None:
        return frame if frame.crs.to_epsg() == 4326 else frame.to_crs(4326)
    west, south, east, north = frame.total_bounds
    if not all(map(math.isfinite, (west, south, east, north))):  # empty or all-null geometry
        return frame.set_crs(4326)
    if -180 <= west <= east <= 180 and -90 <= south <= north <= 90:
        return frame.set_crs(4326)
    raise Refusal(
        "This data has no coordinate reference system and its coordinates are "
        f"not longitude/latitude (bounds {west:g}, {south:g}, {east:g}, {north:g}), "
        "so it cannot be placed on a map. Set its CRS first.")


# ---- a user's .py -------------------------------------------------------------------

def _load_module(path: str):
    directory = os.path.dirname(os.path.abspath(path))
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location("map_viewer_target", path)
    if spec is None or spec.loader is None:
        raise Refusal(f"Could not import {path}")
    module = importlib.util.module_from_spec(spec)
    module.__file__ = os.path.abspath(path)
    spec.loader.exec_module(module)
    return module


def _run_target(path: str, entrypoint: str = "") -> Any:
    module = _load_module(path)
    for name in ([entrypoint] if entrypoint else []) + list(ENTRYPOINTS):
        function = getattr(module, name, None)
        if callable(function):
            return function()
    for name in RESULT_VARS:
        value = getattr(module, name, None)
        if value is not None and not callable(value):
            return value
    raise Refusal("This script defines no main()/run()/udf()/fn() and no "
                  "result/output/layer/gdf/df value to put on the map.")


def _write_result(value: Any, key: str) -> dict:
    """Write what a script returned as a file one of the browser loaders reads."""
    if isinstance(value, (str, os.PathLike)):
        return plan(str(value))
    module = type(value).__module__.split(".")[0]
    if module == "rasterio" and hasattr(value, "name"):
        return plan(value.name)
    if module == "geopandas":
        frame = _to_wgs84(value if hasattr(value, "to_parquet") else value.to_frame("geometry"))
        final = _cache_path(key, ".parquet")
        frame.to_parquet(final)
        return {"load": "vector", "path": str(final), "format": "geoparquet"}
    if module == "pandas":
        return _write_result(_points_from_table(value), key)
    if module == "xarray":
        dataset = value.to_dataset(name=value.name or "value") if hasattr(value, "to_dataset") and not hasattr(value, "data_vars") else value
        final = _cache_path(key, ".zarr")
        _dataset_to_zarr(dataset, final)
        return {"load": "zarr", "path": str(final)}
    if isinstance(value, tuple) and len(value) == 2 and type(value[0]).__module__ == "numpy":
        return _array_to_cog(value[0], value[1], key)
    geo = value if isinstance(value, dict) else getattr(value, "__geo_interface__", None)
    if isinstance(geo, dict) and "type" in geo:
        if geo["type"] not in ("FeatureCollection", "Feature"):
            geo = {"type": "Feature", "geometry": geo, "properties": {}}
        final = _cache_path(key, ".geojson")
        final.write_text(json.dumps(geo), encoding="utf-8")
        return {"load": "vector", "path": str(final), "format": "geojson"}
    raise Refusal(
        f"The script returned a {type(value).__name__}, which is not something "
        "this map can draw. Return a GeoDataFrame, a DataFrame with lat/lon "
        "columns, a GeoJSON dict, an xarray DataArray/Dataset, a (array, bounds) "
        "tuple, or a path to a data file.")


def _points_from_table(frame):
    import geopandas as gpd

    lowered = {str(c).lower(): c for c in frame.columns}
    # Any longitude name with any latitude name (longitude/lat, lon/latitude,
    # long/lat...); x/y only as a pair, and only when no geographic names exist.
    lon = next((lowered[n] for n in ("longitude", "lon", "lng", "long") if n in lowered), None)
    lat = next((lowered[n] for n in ("latitude", "lat") if n in lowered), None)
    if (lon is None or lat is None) and "x" in lowered and "y" in lowered:
        lon, lat = lowered["x"], lowered["y"]
    if lon is not None and lat is not None:
        return _to_wgs84(gpd.GeoDataFrame(
            frame, geometry=gpd.points_from_xy(frame[lon], frame[lat])))
    raise Refusal("The table has no longitude/latitude columns to place on a map.")


def _array_to_cog(array, bounds, key: str) -> dict:
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    data = np.asarray(array)
    if data.ndim == 2:
        data = data[None]
    if data.ndim != 3 or len(bounds) != 4:
        raise Refusal("An (array, bounds) result needs a 2-D or (bands, h, w) "
                      "array and bounds (west, south, east, north) in degrees.")
    final = _cache_path(key, ".tif")
    tmp = final.with_name(final.name + ".tmp.tif")
    count, height, width = data.shape
    with rasterio.open(tmp, "w", driver="GTiff", count=count, height=height,
                       width=width, dtype=data.dtype, crs="EPSG:4326",
                       transform=from_bounds(*bounds, width, height)) as out:
        out.write(data)
    _to_cog(str(tmp), final)
    tmp.unlink(missing_ok=True)
    return {"load": "raster", "path": str(final)}


# ---- inspect ----------------------------------------------------------------------------

def inspect(path: str) -> dict:
    """A compact description of what a source holds."""
    suffix = _suffix(path)
    if multidim_suffix(path) or suffix in MULTIDIM_FILES:
        import xarray as xr
        opener = (lambda: xr.open_zarr(path, consolidated=None)) \
            if multidim_suffix(path) == ".zarr" else (lambda: _open_dataset(path))
        with opener() as dataset:
            return {
                "kind": "zarr",
                "variables": {
                    name: {"dims": dict(zip(dataset[name].dims, map(int, dataset[name].shape))),
                           "dtype": str(dataset[name].dtype),
                           "units": str(dataset[name].attrs.get("units", "")),
                           # The CF array that names the CRS, for the browser to open.
                           "grid_mapping": str(dataset[name].attrs.get("grid_mapping")
                                               or dataset[name].encoding.get("grid_mapping") or "")}
                    for name in _gridded_vars(dataset)
                },
            }
    if suffix in BROWSER_VECTOR:
        try:
            import pyogrio
            info = pyogrio.read_info(path)
            return {
                "kind": "vector",
                "features": int(info.get("features", -1)),
                "geometry_type": info.get("geometry_type"),
                "crs": info.get("crs"),
                "fields": {str(n): str(t) for n, t in zip(info["fields"], info["dtypes"])},
                "bounds": [float(v) for v in info["total_bounds"]] if info.get("total_bounds") is not None else None,
            }
        except Exception:
            if suffix != ".json":
                raise
    if suffix == ".pmtiles":
        return {"kind": "pmtiles"}
    if suffix in POINTCLOUD_FILES:
        return _pointcloud_info(path)
    if suffix == ".py":
        return {"kind": "python", "note": "a script; its result is drawn"}
    import rasterio
    from rasterio.warp import transform_bounds
    target = ("/vsicurl/" + path) if is_http_url(path) else path
    with rasterio.open(target) as dataset:
        bounds = None
        if dataset.crs is not None:
            bounds = [round(v, 6) for v in transform_bounds(dataset.crs, "EPSG:4326", *dataset.bounds)]
        return {
            "kind": "raster",
            "width": dataset.width, "height": dataset.height,
            "bands": dataset.count, "dtype": dataset.dtypes[0],
            "crs": dataset.crs.to_string() if dataset.crs else None,
            "nodata": dataset.nodata, "bounds": bounds,
            "descriptions": [d for d in dataset.descriptions if d],
            "has_colormap": _has_colormap(dataset),
        }


def _has_colormap(dataset) -> bool:
    try:
        dataset.colormap(1)
        return True
    except ValueError:
        return False


# ---- entry points -----------------------------------------------------------------------

def plan(target: str, entrypoint: str = "") -> dict:
    suffix = _suffix(target)
    remote = is_remote_path(target)
    if not remote and not os.path.exists(target):
        raise Refusal(f"Not found: {target}")
    if suffix == ".py":
        if remote:
            raise Refusal("Scripts run from a local file only.")
        key = _cache_key(target, "py", entrypoint)
        with contextlib.redirect_stdout(io.StringIO()):
            value = _run_target(target, entrypoint)
        result = _write_result(value, key)
        result["from_script"] = True
        return result
    if multidim_suffix(target) == ".zarr":
        return {"load": "zarr", "path": target}
    if suffix in MULTIDIM_FILES:
        return _multidim_plan(target)
    if suffix == ".pmtiles":
        return {"load": "pmtiles", "path": target}
    if suffix in POINTCLOUD_FILES or re.search(r"(^|[/\\])ept\.json$", target.split("?", 1)[0]):
        return {"load": "pointcloud", "path": target}
    if suffix in BROWSER_VECTOR:
        out = {"load": "vector", "path": target, "format": suffix.lstrip(".")}
        if suffix == ".shp" and not remote:
            out["companions"] = shapefile_companions(target)
        return out
    if suffix in BROWSER_RASTER and not remote:
        if _is_streamable_tiff(target):
            return {"load": "raster", "path": target}
        return _raster_plan(target)
    if suffix in BROWSER_RASTER:
        return {"load": "raster", "path": target}
    if suffix in GDAL_RASTER or not suffix:
        if remote:
            return _localize(target)
        return _raster_plan(target)
    raise Refusal(f"The map viewer does not know how to read {suffix} files.")


def _crs_info(crs: str, bounds: list | None = None) -> dict:
    """A CRS as a Zarr store wrote it (WKT, or an EPSG code) as the proj4
    string the browser reprojects with, plus a grid extent in lon/lat."""
    from pyproj import CRS, Transformer

    parsed = CRS.from_user_input(crs)
    # Geographic or not is the horizontal part's call: unwrap datum shifts and heights.
    horizontal = parsed.source_crs if parsed.is_bound else parsed
    horizontal = horizontal.sub_crs_list[0] if horizontal.is_compound else horizontal
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # "lose projection information"
        out = {"proj4": parsed.to_proj4(), "epsg": parsed.to_epsg(), "geographic": horizontal.is_geographic}
    if bounds and len(bounds) == 4:
        west, south, east, north = Transformer.from_crs(parsed, 4326, always_xy=True) \
            .transform_bounds(*map(float, bounds))
        out["bounds"] = [max(west, -180.0), max(south, -90.0), min(east, 180.0), min(north, 90.0)]
    return out


def main(target: str = "", action: str = "plan", entrypoint: str = "", layer: str = "",
         crs: str = "", bounds: list | None = None):
    try:
        target = str(target or "").strip()
        if not target:
            raise Refusal("No source given.")
        if not is_remote_path(target):
            target = os.path.abspath(os.path.expanduser(target))
        if action == "plan":
            result = plan(target, entrypoint)
        elif action == "geojson":
            result = _to_geojson(target, layer)
        elif action == "localize":
            result = _localize(target)
        elif action == "convert":
            # A tiled TIFF the browser still could not decode (a codec its
            # reader lacks): re-encode it rather than trusting the layout.
            result = _localize(target) if is_remote_path(target) else _raster_plan(target)
        elif action == "pointcloud":
            result = _pointcloud_plan(target, crs)
        elif action == "crs":
            result = _crs_info(crs, bounds)
        elif action == "inspect":
            result = inspect(target)
        else:
            raise Refusal(f"unknown action {action!r}")
        return {"status": "ok", **result}
    except Refusal as refusal:
        return {"status": "error", "message": str(refusal)}
    except ModuleNotFoundError as error:
        return {"status": "error", "message":
                f"{error}. A map script runs in the Map Viewer's own environment "
                "(fused_render/templates/map/pyproject.toml); only packages "
                "declared there are importable."}
    except Exception as error:
        return {"status": "error", "message": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc()[-4000:]}

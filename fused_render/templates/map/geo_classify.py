"""Classify an arbitrary Python object (or data-file path) into a normalized
map layer descriptor, writing render-ready artifacts.

Runs inside FusedRender's bundled geo runtime (geopandas, rasterio, rio-tiler,
numpy, pandas, pyproj, shapely, pillow, matplotlib). The Map Viewer consumes the returned
descriptor generically — one `descriptorToLayer()` switch on `kind`.

The single entry point is `classify(obj, artifact_dir, artifact_id, opts)`.
It never raises for a "shape we don't understand" — it returns a descriptor
with status "not_georeferenced" (data exists but can't be placed on a map) or
"error" (something genuinely went wrong), so the UI always has something to say.

Descriptor shape:
  {
    "id": str,
    "status": "ok" | "not_georeferenced" | "error",
    "kind":   "vector_geojson" | "raster_image" | "vector_tiles_pmtiles" | None,
    "crs_original": str | None,          # e.g. "EPSG:32610"
    "bounds": [w, s, e, n] | None,       # ALWAYS EPSG:4326
    "data": { "geojson_path"|"image_path"|"pmtiles_path": abs_path, ... },
    "stats": { ... },                     # drives legend + styling UI
    "style": { ... },                     # default style; UI round-trips this
    "warnings": [str, ...],
    "message": str | None,                # human text for not_georeferenced/error
    "detected_type": str,                 # what we saw, for the UI
  }
"""
from __future__ import annotations

import contextlib
import json
import math
import os

from geo_paths import is_remote_path, normalize_remote_path
from optional_runtime import require
from raster_categories import classify_categories, resolve_render_mode

# ---- output caps (keep artifacts screen-sized / network-friendly) -----------
MAX_RASTER_DIM = 1400        # longest edge of the reprojected raster PNG
MAX_VECTOR_FEATURES = 400_000    # cap for the GeoJSON (polygon/line) path
BINARY_POINT_THRESHOLD = 50_000  # points beyond this go through the fast binary path
LONLAT_PAD = 1e-6


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _finite(x):
    """numpy/py scalar -> json-native float/int/None (NaN/inf -> None)."""
    import numpy as np
    if x is None:
        return None
    if isinstance(x, (np.floating, float)):
        f = float(x)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, int):
        return x
    return str(x)


def _crs_short(crs):
    """Compact CRS label: EPSG code if known, else a short name."""
    try:
        epsg = crs.to_epsg()
        if epsg:
            return f"EPSG:{epsg}"
    except Exception:
        pass
    try:
        name = getattr(crs, "name", None)
        if name:
            return str(name)
    except Exception:
        pass
    return str(crs)[:80]


def _looks_lonlat(bounds):
    """bounds = [w,s,e,n]; True if plausibly already in lon/lat degrees."""
    if not bounds or any(b is None for b in bounds):
        return False
    w, s, e, n = bounds
    return (-180.5 <= w <= 180.5 and -180.5 <= e <= 180.5
            and -90.5 <= s <= 90.5 and -90.5 <= n <= 90.5)


def _base(artifact_id, detected_type):
    return {
        "id": artifact_id,
        "status": "ok",
        "kind": None,
        "crs_original": None,
        "bounds": None,
        "data": {},
        "stats": {},
        "style": {},
        "warnings": [],
        "message": None,
        "detected_type": detected_type,
    }


def _not_geo(artifact_id, detected_type, message):
    d = _base(artifact_id, detected_type)
    d["status"] = "not_georeferenced"
    d["message"] = message
    return d


# ---------------------------------------------------------------------------
# top-level dispatch
# ---------------------------------------------------------------------------
def classify(obj, artifact_dir, artifact_id, opts=None):
    opts = opts or {}
    os.makedirs(artifact_dir, exist_ok=True)
    tname = type(obj).__name__

    # Lazy imports so a missing optional dep only breaks its own branch.
    import numpy as np
    import pandas as pd

    try:
        import geopandas as gpd
    except Exception:
        gpd = None

    # 1) string / path-like -> dispatch on suffix / sniff
    if isinstance(obj, (str, os.PathLike)):
        return _from_path(str(obj), artifact_dir, artifact_id, opts)

    # 2) GeoDataFrame / GeoSeries  (checked before DataFrame: GDF *is a* DF)
    if gpd is not None and isinstance(obj, gpd.GeoDataFrame):
        return _from_gdf(obj, artifact_dir, artifact_id, opts, detected="GeoDataFrame")
    if gpd is not None and isinstance(obj, gpd.GeoSeries):
        gdf = gpd.GeoDataFrame(geometry=obj)
        return _from_gdf(gdf, artifact_dir, artifact_id, opts, detected="GeoSeries")

    # 3) shapely geometry
    try:
        from shapely.geometry.base import BaseGeometry
        if isinstance(obj, BaseGeometry):
            gdf = gpd.GeoDataFrame(geometry=[obj], crs=4326)
            d = _from_gdf(gdf, artifact_dir, artifact_id, opts, detected="shapely.geometry")
            d["warnings"].append("Bare geometry had no CRS; assumed EPSG:4326.")
            return d
    except Exception:
        pass

    # 4) plain pandas DataFrame -> look for geometry / lat-lon
    if isinstance(obj, pd.DataFrame):
        return _from_dataframe(obj, artifact_dir, artifact_id, opts)

    # 5) GeoJSON-ish dict
    if isinstance(obj, dict) and obj.get("type") in (
        "FeatureCollection", "Feature", "Point", "MultiPoint", "LineString",
        "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection",
    ):
        return _from_geojson(obj, artifact_dir, artifact_id, opts)

    # 6) rasterio dataset
    try:
        import rasterio
        if isinstance(obj, rasterio.io.DatasetReader):
            return _from_rasterio(obj, artifact_dir, artifact_id, opts)
    except Exception:
        pass

    # 7) xarray DataArray / Dataset (needs rioxarray for CRS)
    try:
        import xarray as xr
        if isinstance(obj, (xr.DataArray, xr.Dataset)):
            return _from_xarray(obj, artifact_dir, artifact_id, opts)
    except Exception:
        pass

    # 8) numpy array or (array, georef) tuple
    if isinstance(obj, np.ndarray):
        return _not_geo(
            artifact_id, "numpy.ndarray",
            "Got a bare NumPy array with no spatial reference. Return it as "
            "(array, bounds_4326) or (array, transform, crs), or write a GeoTIFF.",
        )
    if isinstance(obj, (tuple, list)) and obj and isinstance(obj[0], np.ndarray):
        return _from_array_tuple(obj, artifact_dir, artifact_id, opts)

    return _not_geo(
        artifact_id, tname,
        f"Don't know how to place a {tname} on the map. Return a GeoDataFrame, "
        "a DataFrame with lat/lon, GeoJSON, a raster/GeoTIFF, or a file path.",
    )


# ---------------------------------------------------------------------------
# file paths
# ---------------------------------------------------------------------------
RASTER_EXT = (
    ".tif", ".tiff", ".cog", ".vrt", ".jp2", ".j2k", ".img", ".ntf",
    ".nitf", ".dem", ".dt0", ".dt1", ".dt2", ".hgt", ".grd", ".nc",
    ".hdf", ".h5",
)
VECTOR_EXT = (".geojson", ".json", ".shp", ".gpkg", ".fgb", ".kml", ".gml")
TABLE_EXT = (".csv", ".tsv", ".xlsx", ".xls")


def _from_path(path, artifact_dir, artifact_id, opts):
    path = path.strip()
    if is_remote_path(path):
        path = normalize_remote_path(path)
    is_url = is_remote_path(path)
    reader_path = (
        path
        if is_url
        else os.path.abspath(os.path.expanduser(path))
    )
    low = path.lower().split("?")[0]

    if low.endswith(".pmtiles"):
        return _from_pmtiles(reader_path, artifact_id)
    if low.endswith(".parquet") or low.endswith(".geoparquet"):
        return _from_parquet(reader_path, artifact_dir, artifact_id, opts)
    if low.endswith(TABLE_EXT):
        return _from_table(reader_path, artifact_dir, artifact_id, opts)
    if low.endswith(RASTER_EXT):
        return _from_rasterio_path(
            reader_path, artifact_dir, artifact_id, opts
        )
    if low.endswith(VECTOR_EXT):
        return _from_vector_file(
            reader_path, artifact_dir, artifact_id, opts
        )

    if not is_url and not os.path.exists(reader_path):
        return _not_geo(
            artifact_id, "path", f"File not found: {reader_path}"
        )

    # Unknown suffix: try raster, then vector.
    try:
        return _from_rasterio_path(
            reader_path, artifact_dir, artifact_id, opts
        )
    except Exception:
        pass
    try:
        return _from_vector_file(
            reader_path, artifact_dir, artifact_id, opts
        )
    except Exception as e:
        return _not_geo(artifact_id, "path",
                        f"Couldn't read {path} as raster or vector ({e}).")


def _from_parquet(path, artifact_dir, artifact_id, opts):
    import geopandas as gpd
    import pandas as pd
    try:
        gdf = gpd.read_parquet(path)
        return _from_gdf(gdf, artifact_dir, artifact_id, opts, detected="GeoParquet")
    except Exception:
        df = pd.read_parquet(path)
        return _from_dataframe(df, artifact_dir, artifact_id, opts, detected="Parquet(table)")


def _from_table(path, artifact_dir, artifact_id, opts):
    suffix = os.path.splitext(path.split("?", 1)[0])[1].lower()
    requirements = {"pandas": "pandas"}
    feature = "tabular layers"
    if suffix == ".xls":
        requirements["xlrd"] = "xlrd"
        feature = "legacy Excel layers"
    elif suffix in {".xlsx", ".xlsm"}:
        requirements["openpyxl"] = "openpyxl"
        feature = "Excel layers"
    dependency_error = require(feature, requirements)
    if dependency_error:
        raise RuntimeError(dependency_error)

    import pandas as pd

    if suffix == ".csv":
        frame = pd.read_csv(path)
        detected = "CSV(table)"
    elif suffix == ".tsv":
        frame = pd.read_csv(path, sep="\t")
        detected = "TSV(table)"
    else:
        frame = pd.read_excel(path)
        detected = "Excel(table)"
    return _from_dataframe(
        frame, artifact_dir, artifact_id, opts, detected=detected
    )


def _from_vector_file(path, artifact_dir, artifact_id, opts):
    import geopandas as gpd
    gdf = gpd.read_file(path)
    return _from_gdf(gdf, artifact_dir, artifact_id, opts, detected="vector file")


def _from_geojson(obj, artifact_dir, artifact_id, opts):
    import geopandas as gpd
    if obj.get("type") == "FeatureCollection":
        feats = obj.get("features", [])
    elif obj.get("type") == "Feature":
        feats = [obj]
    else:  # bare geometry
        feats = [{"type": "Feature", "geometry": obj, "properties": {}}]
    if not feats:
        return _not_geo(artifact_id, "GeoJSON", "GeoJSON has no features.")
    gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
    return _from_gdf(gdf, artifact_dir, artifact_id, opts, detected="GeoJSON")


def _from_pmtiles(path, artifact_id):
    d = _base(artifact_id, "PMTiles")
    d["kind"] = "vector_tiles_pmtiles"
    d["data"] = {"pmtiles_path": path}
    d["style"] = {"line_color": [0, 200, 255, 200], "fill_color": [0, 150, 255, 60],
                  "opacity": 1.0}
    return d


# ---------------------------------------------------------------------------
# vector: DataFrame / GeoDataFrame
# ---------------------------------------------------------------------------
LAT_NAMES = ("latitude", "lat", "y", "ycoord", "lat_dd")
LON_NAMES = ("longitude", "lon", "lng", "long", "x", "xcoord", "lon_dd")
GEOM_NAMES = ("geometry", "geom", "the_geom", "wkt", "wkb")


def _find_col(cols, candidates):
    lower = {str(c).lower(): c for c in cols}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    return None


def _from_dataframe(df, artifact_dir, artifact_id, opts, detected="DataFrame"):
    import geopandas as gpd
    import pandas as pd
    from shapely import wkt as shp_wkt
    from shapely.geometry.base import BaseGeometry

    if len(df) == 0:
        return _not_geo(artifact_id, detected, "Table is empty.")

    geom_col = _find_col(df.columns, GEOM_NAMES)
    # a) column already holding shapely geometries
    if geom_col is not None and df[geom_col].map(lambda v: isinstance(v, BaseGeometry)).any():
        gdf = gpd.GeoDataFrame(df, geometry=geom_col, crs="EPSG:4326")
        d = _from_gdf(gdf, artifact_dir, artifact_id, opts, detected=detected + " (geometry col)")
        d["warnings"].append("Geometry column had no CRS; assumed EPSG:4326.")
        return d
    # b) WKT text column
    if geom_col is not None and df[geom_col].dtype == object:
        try:
            geoms = df[geom_col].map(lambda v: shp_wkt.loads(v) if isinstance(v, str) else None)
            if geoms.notna().any():
                gdf = gpd.GeoDataFrame(df.drop(columns=[geom_col]), geometry=geoms, crs="EPSG:4326")
                d = _from_gdf(gdf, artifact_dir, artifact_id, opts, detected=detected + " (WKT col)")
                d["warnings"].append("WKT geometry had no CRS; assumed EPSG:4326.")
                return d
        except Exception:
            pass
    # c) lat / lon columns
    lat = _find_col(df.columns, LAT_NAMES)
    lon = _find_col(df.columns, LON_NAMES)
    if lat and lon and lat != lon:
        sub = df[pd.to_numeric(df[lat], errors="coerce").notna()
                 & pd.to_numeric(df[lon], errors="coerce").notna()].copy()
        if len(sub) == 0:
            return _not_geo(artifact_id, detected,
                            f"Columns '{lat}'/'{lon}' hold no numeric coordinates.")
        sub[lat] = pd.to_numeric(sub[lat])
        sub[lon] = pd.to_numeric(sub[lon])
        gdf = gpd.GeoDataFrame(
            sub, geometry=gpd.points_from_xy(sub[lon], sub[lat]), crs="EPSG:4326")
        d = _from_gdf(gdf, artifact_dir, artifact_id, opts, detected=detected + f" (lat/lon: {lat},{lon})")
        return d

    cols = ", ".join(str(c) for c in list(df.columns)[:12])
    return _not_geo(
        artifact_id, detected,
        "Table has no geometry, WKT, or lat/lon columns, so it can't be placed "
        f"on the map. Columns seen: {cols}.",
    )


def _json_safe_columns(gdf):
    """Coerce non-JSON dtypes (datetime, category, object-non-str) to str so
    GeoDataFrame.to_json() doesn't choke."""
    import pandas as pd
    geom_name = gdf.geometry.name
    for col in gdf.columns:
        if col == geom_name:
            continue
        s = gdf[col]
        if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
            continue
        gdf[col] = s.astype(str)
    return gdf


def _from_gdf(gdf, artifact_dir, artifact_id, opts, detected):
    import numpy as np

    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if len(gdf) == 0:
        return _not_geo(artifact_id, detected, "No non-empty geometries to draw.")

    d = _base(artifact_id, detected)
    warnings = []

    # CRS handling
    crs = gdf.crs
    if crs is None:
        b = list(gdf.total_bounds)
        if _looks_lonlat(b):
            gdf = gdf.set_crs("EPSG:4326")
            crs = gdf.crs
            warnings.append("No CRS on the data; assumed EPSG:4326 (coords look like lon/lat).")
        else:
            return _not_geo(
                artifact_id, detected,
                "Vector data has no CRS and coordinates aren't lon/lat, so it "
                "can't be located on Earth. Set a CRS (e.g. gdf.set_crs(...)).")
    try:
        epsg = crs.to_epsg()
    except Exception:
        epsg = None
    d["crs_original"] = f"EPSG:{epsg}" if epsg else _crs_short(crs)

    if epsg != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    geom_types = sorted(set(gdf.geom_type.dropna().unique().tolist()))
    n = len(gdf)

    # Fast path: lots of points -> compact binary buffers for a deck
    # ScatterplotLayer. No GeoJSON string (which is huge + slow for big N).
    if geom_types == ["Point"] and n >= BINARY_POINT_THRESHOLD:
        return _serialize_points_binary(gdf, geom_types, artifact_dir, artifact_id, d, warnings)

    if n > MAX_VECTOR_FEATURES:
        warnings.append(f"{n:,} features — showing first {MAX_VECTOR_FEATURES:,} for performance.")
        gdf = gdf.iloc[:MAX_VECTOR_FEATURES]

    gdf = _json_safe_columns(gdf)

    w, s, e, nth = [float(v) for v in gdf.total_bounds]
    if not all(math.isfinite(v) for v in (w, s, e, nth)):
        return _not_geo(artifact_id, detected, "Geometry bounds are not finite after reprojection.")
    d["bounds"] = [w, s, e, nth]

    geojson_path = os.path.join(artifact_dir, f"{artifact_id}.geojson")
    with open(geojson_path, "w", encoding="utf-8") as f:
        f.write(gdf.to_json(drop_id=False))

    prop_cols = [c for c in gdf.columns if c != gdf.geometry.name]
    numeric_cols = [c for c in prop_cols
                    if str(gdf[c].dtype).startswith(("int", "float", "uint"))]

    d["kind"] = "vector_geojson"
    d["data"] = {"geojson_path": geojson_path}
    d["stats"] = {
        "feature_count": int(n),
        "geometry_types": geom_types,
        "columns": {str(c): str(gdf[c].dtype) for c in prop_cols[:60]},
        "numeric_columns": [str(c) for c in numeric_cols],
    }
    d["style"] = _default_vector_style(geom_types)
    d["warnings"] = warnings
    return d


def _sanitize(name):
    return "".join(c if c.isalnum() else "_" for c in str(name))[:40]


def _serialize_points_binary(gdf, geom_types, artifact_dir, artifact_id, d, warnings):
    """Write points as raw little-endian binary: positions (interleaved lon/lat
    f32) + per-column sidecars (numeric -> f32, low-card categorical -> u8 codes).
    The frontend feeds these straight into a deck ScatterplotLayer."""
    import numpy as np
    import pandas as pd
    import shapely

    coords = shapely.get_coordinates(gdf.geometry.values)  # (N, 2) lon, lat
    lon, lat = coords[:, 0], coords[:, 1]
    finite = np.isfinite(lon) & np.isfinite(lat)
    if not finite.all():
        idx = np.where(finite)[0]
        gdf = gdf.iloc[idx]
        coords = coords[finite]
        lon, lat = coords[:, 0], coords[:, 1]
    n = int(coords.shape[0])
    if n == 0:
        return _not_geo(artifact_id, "points", "No finite point coordinates to draw.")

    d["bounds"] = [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())]
    points_path = os.path.join(artifact_dir, f"{artifact_id}.points.f32")
    coords.astype("<f4").tofile(points_path)   # lon,lat,lon,lat,...

    geom_name = gdf.geometry.name
    columns_meta, numeric_cols, all_cols = {}, [], {}
    written = 0
    for col in gdf.columns:
        if col == geom_name:
            continue
        s = gdf[col]
        dt = str(s.dtype)
        all_cols[str(col)] = dt
        if written >= 12:
            continue
        safe = _sanitize(col)
        if dt.startswith(("int", "float", "uint")):
            arr = pd.to_numeric(s, errors="coerce").to_numpy().astype("<f4")
            p = os.path.join(artifact_dir, f"{artifact_id}.col_{safe}.f32")
            arr.tofile(p)
            fin = arr[np.isfinite(arr)]
            columns_meta[str(col)] = {"path": p, "dtype": "f32",
                                      "min": _finite(fin.min()) if fin.size else None,
                                      "max": _finite(fin.max()) if fin.size else None}
            numeric_cols.append(str(col))
            written += 1
        elif dt == "object" or dt.startswith("category"):
            vals = s.astype(str)
            cats = pd.unique(vals)
            if len(cats) <= 64:
                cat_list = [str(c) for c in cats.tolist()]
                code_map = {c: i for i, c in enumerate(cat_list)}
                codes = vals.map(code_map).to_numpy().astype("<u1")
                p = os.path.join(artifact_dir, f"{artifact_id}.col_{safe}.u8")
                codes.tofile(p)
                columns_meta[str(col)] = {"path": p, "dtype": "u8", "categories": cat_list}
                written += 1

    d["kind"] = "vector_points_binary"
    d["data"] = {"points_path": points_path, "count": n, "columns": columns_meta}
    d["stats"] = {"feature_count": n, "geometry_types": geom_types,
                  "columns": all_cols, "numeric_columns": numeric_cols}
    d["style"] = _default_vector_style(geom_types)
    warnings.append(f"{n:,} points via fast binary path.")
    d["warnings"] = warnings
    return d


def _default_vector_style(geom_types):
    is_poly = any("Polygon" in g for g in geom_types)
    is_line = any("Line" in g for g in geom_types)
    is_point = any("Point" in g for g in geom_types)
    style = {
        "opacity": 1.0,
        "fill_color": [56, 135, 255, 90],
        "line_color": [90, 200, 255, 220],
        "line_width": 1.5,
        "point_radius": 5,
        "color_by": None,      # column name to color by (numeric -> colormap)
        "colormap": "viridis",
    }
    if is_point and not (is_poly or is_line):
        style["fill_color"] = [0, 200, 255, 220]
    return style


# ---------------------------------------------------------------------------
# raster
# ---------------------------------------------------------------------------
def _from_rasterio(ds, artifact_dir, artifact_id, opts):
    return _render_raster(ds, artifact_dir, artifact_id, opts, detected="rasterio dataset")


def _from_rasterio_path(path, artifact_dir, artifact_id, opts):
    import rasterio
    with rasterio.open(path) as ds:
        return _render_raster(ds, artifact_dir, artifact_id, opts, detected="GeoTIFF/raster")


def _from_xarray(obj, artifact_dir, artifact_id, opts):
    import xarray as xr
    try:
        import rioxarray  # noqa: F401  (registers .rio accessor)
    except Exception:
        return _not_geo(artifact_id, "xarray",
                        "xarray data needs rioxarray to locate it; install rioxarray.")
    da = obj
    if isinstance(obj, xr.Dataset):
        names = list(obj.data_vars)
        if not names:
            return _not_geo(artifact_id, "xarray.Dataset", "Dataset has no data variables.")
        da = obj[opts.get("var") or names[0]]
    try:
        crs = da.rio.crs
    except Exception:
        crs = None
    if crs is None:
        return _not_geo(artifact_id, "xarray", "xarray data has no CRS (da.rio.crs is None).")
    tmp = os.path.join(artifact_dir, f"{artifact_id}_src.tif")
    da.rio.to_raster(tmp)
    import rasterio
    with rasterio.open(tmp) as ds:
        return _render_raster(ds, artifact_dir, artifact_id, opts, detected="xarray")


def _from_array_tuple(obj, artifact_dir, artifact_id, opts):
    """Support (array, bounds_4326) or (array, transform, crs)."""
    import numpy as np
    arr = np.asarray(obj[0])
    rest = obj[1:]
    if len(rest) == 1 and isinstance(rest[0], (tuple, list)) and len(rest[0]) == 4:
        bounds = [float(v) for v in rest[0]]
        return _render_array(arr, bounds, artifact_dir, artifact_id, opts,
                             detected="(array, bounds)")
    if len(rest) >= 2:
        try:
            from rasterio.transform import array_bounds
            from rasterio.warp import transform_bounds
            transform, crs = rest[0], rest[1]
            h, w = arr.shape[-2], arr.shape[-1]
            b = array_bounds(h, w, transform)
            bounds = list(transform_bounds(crs, "EPSG:4326", *b))
            return _render_array(arr, bounds, artifact_dir, artifact_id, opts,
                                 detected="(array, transform, crs)")
        except Exception as e:
            return _not_geo(artifact_id, "array-tuple", f"Couldn't georeference array tuple: {e}")
    return _not_geo(artifact_id, "array-tuple",
                    "Return (array, bounds_4326) or (array, transform, crs).")


def _stretch(band, lo, hi):
    import numpy as np
    if hi <= lo:
        hi = lo + 1.0
    v = (band - lo) / (hi - lo)
    return np.clip(v, 0.0, 1.0)


def _apply_colormap(norm, name, mask):
    """norm: 2D float in [0,1] (invalid -> any). mask: True where VALID.
    Returns HxWx4 uint8 RGBA."""
    import numpy as np
    try:
        import matplotlib
        cmap = matplotlib.colormaps[name] if name in matplotlib.colormaps else matplotlib.colormaps["viridis"]
        rgba = (cmap(np.nan_to_num(norm, nan=0.0)) * 255).astype("uint8")
    except Exception:
        # fallback grayscale
        g = (np.nan_to_num(norm, nan=0.0) * 255).astype("uint8")
        rgba = np.dstack([g, g, g, np.full_like(g, 255)])
    rgba[..., 3] = np.where(mask, 255, 0).astype("uint8")
    return rgba


def _render_raster(ds, artifact_dir, artifact_id, opts):  # pragma: no cover - overload guard
    raise NotImplementedError


def _render_raster(ds, artifact_dir, artifact_id, opts, detected):
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from PIL import Image

    if ds.crs is None:
        return _not_geo(artifact_id, detected,
                        "Raster has no CRS, so it can't be placed on the map "
                        "(not georeferenced).")

    d = _base(artifact_id, detected)
    d["crs_original"] = ds.crs.to_string()
    warnings = []

    # Categorical eligibility is decided from the source dataset, before
    # reprojection: a bilinear warp would blend adjacent integer class codes
    # into meaningless values, so a categorical raster must be resampled
    # with "nearest" for the warp too, not just for the final render.
    dtype0 = str(ds.dtypes[0])
    embedded_colormap = None
    if ds.count < 3:
        with contextlib.suppress(ValueError):
            embedded_colormap = ds.colormap(1)
    categories = None
    if ds.count < 3:
        try:
            sample = ds.read(
                1,
                out_shape=(min(ds.height, 256), min(ds.width, 256)),
                resampling=Resampling.nearest,
                masked=True,
            )
            categories = classify_categories(
                sample, dtype0, embedded_colormap=embedded_colormap,
                overrides=opts.get("category_colors"),
            )
        except Exception:
            categories = None
    requested_mode = str(opts.get("render_mode") or "")
    categorical = resolve_render_mode(ds.count, categories, embedded_colormap, requested_mode) == "categorical"
    warp_resampling = Resampling.nearest if categorical else Resampling.bilinear

    with WarpedVRT(ds, crs="EPSG:4326", resampling=warp_resampling) as vrt:
        scale = min(1.0, MAX_RASTER_DIM / max(vrt.width, vrt.height))
        out_h = max(1, int(round(vrt.height * scale)))
        out_w = max(1, int(round(vrt.width * scale)))
        count = vrt.count
        data = vrt.read(out_shape=(count, out_h, out_w),
                        resampling=warp_resampling, masked=True)
        b = vrt.bounds  # (left, bottom, right, top) in EPSG:4326

    bounds = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
    if not _looks_lonlat(bounds):
        warnings.append("Reprojected bounds look unusual; placement may be off.")
    d["bounds"] = bounds

    data = data.astype("float64")
    valid_all = ~np.ma.getmaskarray(data).any(axis=0) if np.ma.isMaskedArray(data) else \
        np.ones((out_h, out_w), dtype=bool)
    arr = np.ma.filled(data, np.nan)

    colormap = (opts.get("colormap") or "viridis")
    rescale = opts.get("rescale")  # optional [lo, hi]

    band_stats = []
    category_colors = {}
    if count >= 3:
        chans = []
        for i in range(3):
            band = arr[i]
            finite = band[np.isfinite(band)]
            lo = float(np.percentile(finite, 2)) if finite.size else 0.0
            hi = float(np.percentile(finite, 98)) if finite.size else 1.0
            if rescale and len(rescale) == 2:
                lo, hi = float(rescale[0]), float(rescale[1])
            chans.append((_stretch(band, lo, hi) * 255).astype("uint8"))
            band_stats.append({"index": i + 1, "p2": _finite(lo), "p98": _finite(hi)})
        alpha = np.where(valid_all, 255, 0).astype("uint8")
        rgba = np.dstack([chans[0], chans[1], chans[2], alpha])
        mode = "rgb"
    elif categorical:
        band = arr[0]
        category_colors = {c["value"]: tuple(c["color"]) for c in categories}
        rgba = np.zeros((out_h, out_w, 4), dtype="uint8")
        for value, color in category_colors.items():
            rgba[band == value] = color
        band_stats.append({"index": 1, "p2": None, "p98": None})
        mode = "categorical"
    else:
        band = arr[0]
        finite = band[np.isfinite(band)]
        lo = float(np.percentile(finite, 2)) if finite.size else 0.0
        hi = float(np.percentile(finite, 98)) if finite.size else 1.0
        if rescale and len(rescale) == 2:
            lo, hi = float(rescale[0]), float(rescale[1])
        norm = _stretch(band, lo, hi)
        rgba = _apply_colormap(norm, colormap, np.isfinite(band) & valid_all)
        band_stats.append({
            "index": 1, "min": _finite(finite.min()) if finite.size else None,
            "max": _finite(finite.max()) if finite.size else None,
            "p2": _finite(lo), "p98": _finite(hi),
        })
        mode = "single"

    image_path = os.path.join(artifact_dir, f"{artifact_id}.png")
    Image.fromarray(rgba, "RGBA").save(image_path)

    d["kind"] = "raster_image"
    d["data"] = {"image_path": image_path}
    d["stats"] = {
        "bands": int(count), "width": int(out_w), "height": int(out_h),
        "dtype": dtype0, "nodata": _finite(ds.nodata),
        "band_stats": band_stats, "render_mode": mode,
        "categories": categories,
    }
    d["style"] = {
        "opacity": 0.9, "colormap": colormap,
        "rescale": [band_stats[0].get("p2"), band_stats[0].get("p98")] if mode == "single" else None,
        "render_mode": mode,
        "category_colors": {str(value): list(color) for value, color in category_colors.items()},
    }
    d["warnings"] = warnings
    return d


def _render_array(arr, bounds, artifact_dir, artifact_id, opts, detected):
    """Render an in-memory numpy array (already-known 4326 bounds) as a PNG."""
    import numpy as np
    from PIL import Image

    dtype0 = str(np.asarray(arr).dtype)
    arr = np.asarray(arr).astype("float64")
    if arr.ndim == 2:
        arr = arr[None, :, :]
    if arr.ndim == 3 and arr.shape[0] > arr.shape[-1] and arr.shape[-1] in (3, 4):
        arr = np.moveaxis(arr, -1, 0)  # HWC -> CHW
    count = arr.shape[0]

    colormap = opts.get("colormap") or "viridis"
    rescale = opts.get("rescale")
    d = _base(artifact_id, detected)
    d["bounds"] = [float(v) for v in bounds]

    categories = None
    if count < 3:
        categories = classify_categories(arr[0], dtype0, overrides=opts.get("category_colors"))
    requested_mode = str(opts.get("render_mode") or "")
    categorical = resolve_render_mode(count, categories, None, requested_mode) == "categorical"

    category_colors = {}
    if count >= 3:
        chans = []
        for i in range(3):
            band = arr[i]
            finite = band[np.isfinite(band)]
            lo = float(np.percentile(finite, 2)) if finite.size else 0.0
            hi = float(np.percentile(finite, 98)) if finite.size else 1.0
            chans.append((_stretch(band, lo, hi) * 255).astype("uint8"))
        alpha = np.where(np.isfinite(arr[:3]).all(axis=0), 255, 0).astype("uint8")
        rgba = np.dstack([chans[0], chans[1], chans[2], alpha])
        mode = "rgb"
    elif categorical:
        band = arr[0]
        category_colors = {c["value"]: tuple(c["color"]) for c in categories}
        rgba = np.zeros((band.shape[0], band.shape[1], 4), dtype="uint8")
        for value, color in category_colors.items():
            rgba[band == value] = color
        mode = "categorical"
    else:
        band = arr[0]
        finite = band[np.isfinite(band)]
        lo = float(np.percentile(finite, 2)) if finite.size else 0.0
        hi = float(np.percentile(finite, 98)) if finite.size else 1.0
        if rescale and len(rescale) == 2:
            lo, hi = float(rescale[0]), float(rescale[1])
        rgba = _apply_colormap(_stretch(band, lo, hi), colormap, np.isfinite(band))
        mode = "single"

    image_path = os.path.join(artifact_dir, f"{artifact_id}.png")
    Image.fromarray(rgba, "RGBA").save(image_path)
    d["kind"] = "raster_image"
    d["data"] = {"image_path": image_path}
    d["stats"] = {"bands": int(count), "render_mode": mode,
                  "width": int(arr.shape[-1]), "height": int(arr.shape[-2]),
                  "categories": categories}
    d["style"] = {
        "opacity": 0.9, "colormap": colormap, "render_mode": mode,
        "category_colors": {str(value): list(color) for value, color in category_colors.items()},
    }
    return d

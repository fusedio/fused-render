"""Python-side helpers for the map template.

Vector and raster targets are served as on-the-fly tiles by the DuckDB daemon
(vector_tile_server.py); this module holds what map_render.py and the daemon
need around that:
  - route_target(): pick vector / raster / pmtiles for a path.
  - vector_meta(): fast 4326 bounds + geometry type + columns for the tile
    descriptor, without materializing geometry.
  - classify(): the PMTiles descriptor (the only target still rendered from a
    cached artifact rather than the daemon).
  - _stretch()/_apply_colormap(): raster tile colouring, reused by the daemon.
  - _find_col()/LAT_NAMES/LON_NAMES: CSV lat/lon detection, reused by the daemon.
"""
from __future__ import annotations

import os

RASTER_EXT = (".tif", ".tiff", ".vrt", ".jp2", ".img")
VECTOR_EXT = (".geojson", ".json", ".shp", ".gpkg", ".fgb", ".kml", ".gml")
LAT_NAMES = ("latitude", "lat", "y", "ycoord", "lat_dd")
LON_NAMES = ("longitude", "lon", "lng", "long", "x", "xcoord", "lon_dd")


def _crs_short(crs):
    try:
        epsg = crs.to_epsg()
        if epsg:
            return f"EPSG:{epsg}"
    except Exception:
        pass
    name = getattr(crs, "name", None)
    return str(name) if name else str(crs)[:80]


def _find_col(cols, candidates):
    lower = {str(c).lower(): c for c in cols}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    return None


def route_target(target):
    """vector -> MVT daemon, raster -> raster-tile daemon, pmtiles -> classify."""
    low = target.lower().split("?")[0]
    if low.endswith(".pmtiles"):
        return "pmtiles"
    if low.endswith(RASTER_EXT):
        return "raster"
    return "vector"


def list_layers(target):
    """Layer names for a multi-layer vector source (GPKG/KML/GML); [] for a
    single-layer source, which is rendered as one target."""
    low = target.lower().split("?")[0]
    if not low.endswith(VECTOR_EXT):
        return []
    import pyogrio
    try:
        rows = pyogrio.list_layers(target)
    except Exception:
        return []
    if rows is None:
        return []
    names = [str(row[0]) for row in rows]
    return names if len(names) > 1 else []


def vector_meta(target, layer=None):
    """Fast metadata for the MVT descriptor: 4326 bounds, geometry type,
    attribute columns and source CRS — read without materializing geometry."""
    low = target.lower().split("?")[0]
    if low.endswith((".parquet", ".geoparquet")):
        import geopandas as gpd
        gdf = gpd.read_parquet(target)
        gname = gdf.geometry.name
        w, s, e, n = (float(v) for v in gdf.to_crs(4326).total_bounds)
        gtypes = sorted(gdf.geom_type.dropna().unique().tolist())
        cols = [str(c) for c in gdf.columns if c != gname][:5]
        return {"bounds": [w, s, e, n], "geometry_type": gtypes[0] if gtypes else "Unknown",
                "columns": cols, "crs_original": _crs_short(gdf.crs) if gdf.crs else None}
    if low.endswith(".csv"):
        import pandas as pd
        df = pd.read_csv(target)
        lat = _find_col(df.columns, LAT_NAMES)
        lon = _find_col(df.columns, LON_NAMES)
        if not (lat and lon and lat != lon):
            raise ValueError("CSV has no lat/lon columns")
        la = pd.to_numeric(df[lat], errors="coerce")
        lo = pd.to_numeric(df[lon], errors="coerce")
        cols = [str(c) for c in df.columns if c not in (lat, lon)][:5]
        return {"bounds": [float(lo.min()), float(la.min()), float(lo.max()), float(la.max())],
                "geometry_type": "Point", "columns": cols, "crs_original": "EPSG:4326"}

    import pyogrio
    if layer is None:
        layers = pyogrio.list_layers(target)
        if layers is not None and len(layers) > 1:
            best, bestn = None, -1
            for lname in [row[0] for row in layers]:
                ni = pyogrio.read_info(target, layer=lname).get("features", 0)
                if ni > bestn:
                    best, bestn = lname, ni
            layer = best
    info = pyogrio.read_info(target, layer=layer) if layer else pyogrio.read_info(target)
    crs = info.get("crs")
    w, s, e, n = (float(v) for v in info["total_bounds"])
    if crs and str(crs).upper() != "EPSG:4326":
        from pyproj import Transformer
        tr = Transformer.from_crs(crs, 4326, always_xy=True)
        xs, ys = tr.transform([w, e, w, e], [s, s, n, n])
        w, e = min(xs), max(xs); s, n = min(ys), max(ys)
    gname = info.get("geometry_name") or "geometry"
    cols = [str(f) for f in list(info.get("fields", [])) if str(f) != gname][:5]
    return {"bounds": [w, s, e, n], "geometry_type": info.get("geometry_type") or "Unknown",
            "columns": cols, "crs_original": str(crs) if crs else None}


def classify(target, artifact_dir, artifact_id, opts=None):
    """PMTiles descriptor — the only route that isn't served by the daemon."""
    return {
        "id": artifact_id, "status": "ok", "kind": "vector_tiles_pmtiles",
        "crs_original": None, "bounds": None,
        "data": {"pmtiles_path": os.path.abspath(os.path.expanduser(target))},
        "stats": {},
        "style": {"line_color": [0, 200, 255, 200],
                  "fill_color": [0, 150, 255, 60], "opacity": 1.0},
        "warnings": [], "message": None, "detected_type": "PMTiles",
    }


def _stretch(band, lo, hi):
    import numpy as np
    if hi <= lo:
        hi = lo + 1.0
    return np.clip((band - lo) / (hi - lo), 0.0, 1.0)


def _apply_colormap(norm, name, mask):
    """norm: 2D float in [0,1]; mask: True where valid. Returns HxWx4 uint8."""
    import numpy as np
    try:
        import matplotlib
        cmap = matplotlib.colormaps[name] if name in matplotlib.colormaps else matplotlib.colormaps["viridis"]
        rgba = (cmap(np.nan_to_num(norm, nan=0.0)) * 255).astype("uint8")
    except Exception:
        g = (np.nan_to_num(norm, nan=0.0) * 255).astype("uint8")
        rgba = np.dstack([g, g, g, np.full_like(g, 255)])
    rgba[..., 3] = np.where(mask, 255, 0).astype("uint8")
    return rgba

"""Geometry editor reader/writer for fused-render.

Actions
-------
load   path=<abs>       -> file -> GeoJSON FeatureCollection in EPSG:4326
                           + metadata (original CRS, counts, bounds)
export out=<abs>        -> edited FeatureCollection (EPSG:4326, passed as the
       geojson=<str>       `geojson` param) written as GeoParquet; reprojected
       orig_crs=<str>      back to the original CRS unless crs_mode=wgs84.
       crs_mode=original|wgs84
       overwrite=0|1       Never overwrites silently: an existing target gets
                           a numbered suffix unless overwrite=1.

Uses only bundled libs (geopandas / pyogrio / pyarrow / shapely).
"""

import json
import math
import os

MAX_FEATURES = 15000
MAX_VERTICES = 400000


# ---------------------------------------------------------------- helpers


def _json_safe(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, bytes):
        return v.hex()
    return str(v)  # timestamps, Decimal, numpy scalars via str fallback


def _crs_str(crs):
    if crs is None:
        return "EPSG:4326"
    epsg = crs.to_epsg()
    if epsg:
        return f"EPSG:{epsg}"
    return crs.to_wkt()


def _read_any(path):
    import geopandas as gpd

    ext = os.path.splitext(path)[1].lower()
    if ext in (".parquet", ".geoparquet"):
        return gpd.read_parquet(path)
    if ext == ".zip":
        try:
            return gpd.read_file(path)
        except Exception:
            return gpd.read_file(f"zip://{path}")
    return gpd.read_file(path)


def _n_vertices(gdf):
    from shapely import get_coordinates

    try:
        return int(sum(len(get_coordinates(g)) for g in gdf.geometry if g is not None))
    except Exception:
        return -1


# ---------------------------------------------------------------- load


def _load(path):
    if not path:
        raise ValueError("load requires path=")
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    gdf = _read_any(path)
    gdf = gdf[~gdf.geometry.isna()].reset_index(drop=True)

    if len(gdf) > MAX_FEATURES:
        raise ValueError(
            f"{len(gdf):,} features — this editor caps at {MAX_FEATURES:,} "
            "so exports stay lossless. Filter the file first."
        )
    n_vert = _n_vertices(gdf)
    if n_vert > MAX_VERTICES:
        raise ValueError(
            f"{n_vert:,} vertices — this editor caps at {MAX_VERTICES:,}. "
            "Simplify or filter the file first."
        )

    orig_crs = _crs_str(gdf.crs)
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    elif not gdf.crs:
        gdf = gdf.set_crs(4326)

    feats = []
    prop_cols = [c for c in gdf.columns if c != gdf.geometry.name]
    for i, row in enumerate(gdf.itertuples(index=False)):
        geom = getattr(row, gdf.geometry.name, None) or gdf.geometry.iloc[i]
        props = {c: _json_safe(gdf.iloc[i][c]) for c in prop_cols}
        feats.append(
            {
                "type": "Feature",
                "id": i,
                "properties": props,
                "geometry": json.loads(json.dumps(geom.__geo_interface__)),
            }
        )

    b = gdf.total_bounds
    return {
        "meta": {
            "path": path,
            "name": os.path.basename(path),
            "crs": orig_crs,
            "count": len(feats),
            "n_vertices": n_vert,
            "geom_types": sorted(gdf.geometry.geom_type.unique().tolist()),
            "columns": prop_cols,
            "bounds": [float(x) for x in b],
        },
        "fc": {"type": "FeatureCollection", "features": feats},
    }


# ---------------------------------------------------------------- export


def _export(geojson, out, orig_crs, crs_mode, overwrite="0"):
    import geopandas as gpd

    if not geojson:
        raise ValueError("export requires geojson=")
    fc = json.loads(geojson)
    feats = fc.get("features", [])
    if not feats:
        raise ValueError("nothing to export — the collection is empty")

    if not out:
        raise ValueError("export requires out=")
    out = os.path.abspath(os.path.expanduser(out))
    if not out.endswith(".parquet"):
        out += ".parquet"
    os.makedirs(os.path.dirname(out), exist_ok=True)

    # never overwrite silently: an existing target gets a numbered suffix
    # (same convention as the vector template's GeoParquet converter)
    if os.path.exists(out) and str(overwrite) != "1":
        stem = out[: -len(".parquet")]
        n = 1
        while os.path.exists(f"{stem}_{n}.parquet"):
            n += 1
        out = f"{stem}_{n}.parquet"

    gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
    target = orig_crs if (crs_mode != "wgs84" and orig_crs) else "EPSG:4326"
    try:
        gdf = gdf.to_crs(target)
    except Exception as err:
        raise ValueError(f"could not reproject to {target!r}: {err}")

    gdf.to_parquet(out, index=False)
    return {
        "out": out,
        "size": os.path.getsize(out),
        "count": len(gdf),
        "crs": _crs_str(gdf.crs),
        "geom_types": sorted(gdf.geometry.geom_type.unique().tolist()),
    }


# ---------------------------------------------------------------- entrypoint


def main(
    action: str = "load",
    path: str = "",
    geojson: str = "",
    out: str = "",
    orig_crs: str = "",
    crs_mode: str = "original",
    overwrite: str = "0",
):
    if action == "load":
        return _load(path)
    if action == "export":
        return _export(geojson, out, orig_crs, crs_mode, overwrite)
    raise ValueError(f"unknown action {action!r}")


try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass

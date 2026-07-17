"""Vector preview reader for fused-render (.shp, .kml, .kmz, .gpx, ...).

Reads any pyogrio/GDAL-readable vector file (geopandas is in the app's
bundled runner; LIBKML handles .kml/.kmz), reprojects to EPSG:4326 and
returns GeoJSON for the MapLibre template plus schema/CRS/bounds metadata
and an attribute-table preview. Multi-layer sources (KML folders, GPX
tracks/waypoints) are concatenated with a `_layer` column.

Large files are protected two ways: a feature cap (`max_features`) and a
vertex budget — when the raw geometry would exceed it, geometries are
simplified with a tolerance derived from the data extent so the payload
stays renderable.
"""

MAX_VERTICES = 300_000


def _vertex_count(geom):
    from shapely import get_coordinates

    return len(get_coordinates(geom))


def main(file: str = "", max_features: int = 20000, table_rows: int = 50):
    import json
    import os

    import numpy as np

    max_features, table_rows = int(max_features), int(table_rows)
    if not file:
        return {"error": "no file selected"}
    file = os.path.abspath(os.path.expanduser(file))
    if not os.path.isfile(file):
        return {"error": f"not a file: {file}"}

    import geopandas as gpd
    from shapely import get_coordinates

    try:
        import pandas as pd
        import pyogrio

        layers = [str(l[0]) for l in pyogrio.list_layers(file)]
        if len(layers) <= 1:
            gdf = gpd.read_file(file)
        else:
            parts = []
            for name in layers:
                try:
                    part = gpd.read_file(file, layer=name)
                except Exception:  # noqa: BLE001 — skip unreadable sublayers
                    continue
                if len(part):
                    part = part[part.geometry.notna()]
                if len(part):
                    part.insert(0, "_layer", name)
                    parts.append(part)
            if not parts:
                return {"error": "no readable features in any layer", "layers": layers}
            gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
        total = len(gdf)
        truncated = total > max_features
        if truncated:
            gdf = gdf.iloc[:max_features]
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not read vector file: {type(e).__name__}: {e}"}

    crs = gdf.crs
    crs_info = {
        "epsg": (crs.to_epsg() if crs else None),
        "name": (crs.name if crs else None),
        "wkt_head": (crs.to_wkt()[:200] if crs else None),
    }
    native_bounds = [float(v) for v in gdf.total_bounds] if len(gdf) else None

    if crs and (crs.to_epsg() or 0) != 4326:
        try:
            gdf = gdf.to_crs(4326)
        except Exception as e:  # noqa: BLE001
            return {"error": f"reprojection to EPSG:4326 failed: {e}", "crs": crs_info}

    # vertex budget -> simplify (degrees tolerance scaled to extent)
    simplified = None
    if len(gdf):
        nvert = int(len(get_coordinates(gdf.geometry.values)))
        if nvert > MAX_VERTICES:
            minx, miny, maxx, maxy = gdf.total_bounds
            span = max(maxx - minx, maxy - miny, 1e-6)
            tol = span / 2000.0
            for _ in range(6):
                g2 = gdf.geometry.simplify(tol, preserve_topology=True)
                if int(len(get_coordinates(g2.values))) <= MAX_VERTICES:
                    break
                tol *= 3
            gdf = gdf.set_geometry(g2)
            simplified = tol

    # geometry kinds present
    kinds = sorted(set(gdf.geom_type.dropna())) if len(gdf) else []

    # schema + attribute preview (JSON-safe)
    def safe(v):
        if v is None or (isinstance(v, float) and (v != v)):
            return None
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            f = float(v)
            return None if f != f else f
        if isinstance(v, (int, float, bool, str)):
            return v
        return str(v)

    cols = [c for c in gdf.columns if c != gdf.geometry.name]
    schema = [{"name": c, "dtype": str(gdf[c].dtype)} for c in cols]
    table = [{c: safe(r[c]) for c in cols} for _, r in gdf.head(table_rows).iterrows()]

    gj = json.loads(gdf.to_json())
    # keep a stable feature id for hover state
    for i, feat in enumerate(gj.get("features", [])):
        feat["id"] = i

    b = [float(v) for v in gdf.total_bounds] if len(gdf) else None
    if file.lower().endswith(".shp"):  # count the sidecar set
        stem = file.rsplit(".", 1)[0]
        file_size = sum(
            os.path.getsize(stem + "." + ext)
            for ext in ("shp", "shx", "dbf", "prj", "cpg")
            if os.path.isfile(stem + "." + ext)
        )
    else:
        file_size = os.path.getsize(file)
    return {
        "file": file,
        "file_size": file_size,
        "layers": layers,
        "count": int(total if total is not None else len(gdf)),
        "shown": int(len(gdf)),
        "truncated": bool(truncated),
        "simplified_tolerance": simplified,
        "geometry_types": kinds,
        "crs": crs_info,
        "native_bounds": native_bounds,
        "bounds4326": b,
        "schema": schema,
        "table": table,
        "geojson": gj,
    }


try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass

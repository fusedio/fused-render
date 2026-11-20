"""Vector preview reader for fused-render (.shp, .geojson, .gpkg, .kml, .parquet, ...).

OGR formats are read with pyogrio/GDAL, GeoParquet with pyarrow (pyogrio's GDAL
has no Parquet driver), in this folder's environment (`pyproject.toml`, SPEC
PY-16). A signed https URL is read with range requests: /vsicurl/, or the
parquet footer and the row groups the feature cap needs. Output is EPSG:4326
GeoJSON with schema/CRS/bounds metadata and an attribute-table preview;
multi-layer sources get a `_layer` column. A feature cap and a vertex budget
(simplifying with an extent-derived tolerance) keep the payload renderable.
"""
import io
import os
import urllib.request

import truststore

os.environ["GDAL_SKIP"] = "OGR_VRT"
truststore.inject_into_ssl()
MAX_VERTICES = 300_000
H3_NAMES = ("h3", "hex", "h3_index", "h3index", "h3_cell", "cell_id", "hex_id", "h3_id")
GDAL_HTTP = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
             "CPL_VSIL_CURL_CHUNK_SIZE": str(1 << 20),
             "CPL_VSIL_CURL_CACHE_SIZE": str(256 << 20),
             "GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "0.5"}


class NotVector(ValueError):
    pass


def _get(url, rng):
    req = urllib.request.Request(url, headers={"Range": "bytes=" + rng})
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status != 206:
            raise OSError(f"the server ignored a byte range (HTTP {r.status})")
        return r.read(), int(r.headers["Content-Range"].rsplit("/", 1)[1])


def _size(path):
    try:
        return _get(path, "0-0")[1] if "://" in path else os.path.getsize(path)
    except Exception:  # noqa: BLE001
        return 0


class _Ranged(io.RawIOBase):
    def __init__(self, url):
        self.url, self.pos = url, 0
        self.tail, self.size = _get(url, "-65536")

    def readable(self):
        return True

    def seek(self, offset, whence=0):
        self.pos = (0, self.pos, self.size)[whence] + offset
        return self.pos

    def readinto(self, b):
        a, z = self.pos, min(self.pos + len(b), self.size)
        t = self.size - len(self.tail)
        data = (b"" if a >= z else self.tail[a - t:z - t] if a >= t
                else _get(self.url, f"{a}-{z - 1}")[0])
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)


def _read_ogr(src, cap):
    import geopandas as gpd
    import pandas as pd
    import pyogrio

    remote = "://" in src
    if remote:
        pyogrio.set_gdal_config_options(GDAL_HTTP)
        src = "/vsicurl/" + src
    layers = [str(name) for name, gtype in pyogrio.list_layers(src) if gtype]
    parts, read, used, capped = [], [], 0, False
    for name in layers:
        if used >= cap:
            capped = True
            break
        try:
            part = pyogrio.read_dataframe(src, layer=name, max_features=cap - used + 1, use_arrow=True)
        except Exception:  # noqa: BLE001
            if len(layers) == 1:
                raise
            continue
        read.append(name)
        capped = capped or len(part) > cap - used
        part = part.iloc[:cap - used]
        used += len(part)
        if len(layers) > 1:
            part = part[part.geometry.notna()]
            part.insert(0, "_layer", name)
        if len(part):
            parts.append(part)
    if not parts:
        raise NotVector("no features with geometry in this file")
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    if not capped:
        return gdf, len(gdf), False, layers
    counts = [pyogrio.read_info(src, layer=n, force_feature_count=not remote)["features"]
              for n in read + layers[layers.index(read[-1]) + 1:]]
    total = None if any(c is None or c < 0 for c in counts) else sum(counts)
    return gdf, total, total is None or total > used, layers


def _read_parquet(file, cap):
    import json
    import geopandas as gpd
    import pyarrow as pa
    import pyarrow.parquet as pq
    import shapely

    pf = pq.ParquetFile(_Ranged(file) if "://" in file else file, pre_buffer=True)
    schema = pf.schema_arrow
    geo = json.loads((schema.metadata or {}).get(b"geo") or "{}")
    col = geo.get("primary_column")
    if col not in schema.names:
        h3 = next((c for c in schema.names if c.lower() in H3_NAMES), None)
        raise NotVector("This parquet has no GeoParquet geometry column"
                        + (f" ({h3!r} looks like H3 cells: open it in the H3 mode)." if h3 else "."))
    meta = geo["columns"][col]
    skip = {v[0] for v in meta.get("covering", {}).get("bbox", {}).values()}
    groups = rows = 0
    while rows < cap and groups < pf.num_row_groups:
        rows += pf.metadata.row_group(groups).num_rows
        groups += 1
    columns = [f.name for f in schema if f.name == col
               or not (f.name in skip or pa.types.is_binary(f.type))]
    table = pf.read_row_groups(range(groups), columns=columns).slice(0, cap)
    df = table.to_pandas()
    df[col] = shapely.from_wkb(df[col].to_numpy())
    gdf = gpd.GeoDataFrame(df, geometry=col, crs=meta.get("crs", "OGC:CRS84"))
    return gdf, pf.metadata.num_rows, pf.metadata.num_rows > cap, []


def main(file: str = "", max_features: int = 20000, table_rows: int = 50):
    import json
    from concurrent.futures import ThreadPoolExecutor
    import numpy as np
    from shapely import get_coordinates

    max_features, table_rows = int(max_features), int(table_rows)
    if not file:
        return {"error": "no file selected"}
    if not file.lower().startswith(("http://", "https://")):
        if "://" in file:
            return {"error": "only http(s) URLs are read: sign an s3:// address first"}
        file = os.path.abspath(os.path.expanduser(file))
        if not os.path.isfile(file):
            return {"error": f"not a file: {file}"}
    stem, _, ext = (file.split("?")[0] if "://" in file else file).rpartition(".")
    ext = ext.lower()
    parts = [file] + ([f"{stem}.{e}" for e in ("shx", "dbf", "prj", "cpg")] if ext == "shp" else [])
    sizes = ThreadPoolExecutor(len(parts)).map(_size, parts)

    try:
        gdf, total, truncated, layers = (_read_parquet if ext == "parquet" else _read_ogr)(file, max_features)
    except NotVector as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not read vector file: {type(e).__name__}: {e}"}

    crs = gdf.crs
    crs_info = {"epsg": (crs.to_epsg() if crs else None),
                "name": (crs.name if crs else None),
                "wkt_head": (crs.to_wkt()[:200] if crs else None)}
    native_bounds = [float(v) for v in gdf.total_bounds] if len(gdf) else None

    if crs and (crs.to_epsg() or 0) != 4326:
        try:
            gdf = gdf.to_crs(4326)
        except Exception as e:  # noqa: BLE001
            return {"error": f"reprojection to EPSG:4326 failed: {e}",
                    "crs": crs_info}

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
    table = [{c: safe(r[c]) for c in cols}
             for _, r in gdf.head(table_rows).iterrows()]

    gj = json.loads(gdf.to_json(default=str))
    # keep a stable feature id for hover state
    for i, feat in enumerate(gj.get("features", [])):
        feat["id"] = i

    b = [float(v) for v in gdf.total_bounds] if len(gdf) else None
    return {
        "file_size": sum(sizes),
        "layers": layers,
        "count": total,
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

"""Vector preview reader for fused-render (.shp, .geojson, .gpkg, .kml, .parquet, ...).

OGR formats are read with pyogrio/GDAL, GeoParquet with pyarrow (pyogrio's GDAL
has no Parquet driver), in this folder's environment (`pyproject.toml`, SPEC
PY-16). A signed https URL is read with range requests: /vsicurl/, or the
parquet footer and the row groups each chunk needs. Output is EPSG:4326
with schema/CRS/bounds metadata and an attribute-table preview;
multi-layer sources get a `_layer` column.
"""
import base64
import functools
import io
import json
import math
import os
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import truststore

os.environ["GDAL_SKIP"] = "OGR_VRT"
truststore.inject_into_ssl()

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pyogrio
import shapely
from geopandas.io.arrow import _arrow_to_geopandas

CHUNK_COORDS = 1_000_000
FIRST_CHUNK = 20_000
BLOCK = 10_000
H3_NAMES = ("h3", "hex", "h3_index", "h3index", "h3_cell", "cell_id", "hex_id", "h3_id")
GDAL_HTTP = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
             "CPL_VSIL_CURL_CHUNK_SIZE": str(1 << 20),
             "CPL_VSIL_CURL_CACHE_SIZE": str(256 << 20),
             "GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "0.5"}
KINDS = {k.upper(): k for k in ("Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon")}
_lock = threading.Lock()


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



def _safe(v):
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if v is None or isinstance(v, (bool, int, str)):
        return v
    return json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)


def _records(table, tag=None, layer=None):
    return [{**({tag: layer} if tag else {}), **{k: _safe(v) for k, v in r.items()}}
            for r in table.to_pylist()]


def _is_lonlat(crs):
    return not crs or crs.to_epsg() == 4326 or crs.equals("OGC:CRS84")


def _bounds4326(bounds, crs):
    if bounds is None or not np.isfinite(bounds).all():
        return None
    box = gpd.GeoSeries([shapely.box(*bounds)], crs=crs or 4326)
    return box.segmentize(max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / 64 or 1).to_crs(4326).total_bounds.tolist()


def _union(boxes):
    boxes = [b for b in boxes if b is not None and np.isfinite(b).all()]
    return [float(f(b[i] for b in boxes)) for i, f in enumerate((min, min, max, max))] if boxes else None


def _b64(values, dtype):
    return base64.b64encode(np.ascontiguousarray(values, dtype=dtype).tobytes()).decode("ascii")


def _encode(geoms, ids):
    kind = shapely.get_type_id(geoms)
    drawn = (kind >= 0) & ~shapely.is_empty(geoms)
    parts = []
    for family in ((3, 6), (1, 5), (0, 4)):
        sel = drawn & np.isin(kind, family)
        if sel.any():
            gtype, coords, offsets = shapely.to_ragged_array(geoms[sel], include_z=False)
            parts.append({"kind": KINDS[gtype.name], "ids": _b64(ids[sel], "<u4"),
                          "coords": _b64(coords, "<f8"), "offsets": [_b64(o, "<i4") for o in offsets]})
    rest = drawn & ~np.isin(kind, (0, 1, 3, 4, 5, 6))
    if rest.any():
        parts.append({"kind": "geojson", "ids": _b64(ids[rest], "<u4"),
                      "geoms": shapely.to_geojson(geoms[rest]).tolist()})
    return parts


class _Source:
    cursor = (-1, None, None)

    def take(self, start, n):
        (pos, held, blocks), self.cursor = self.cursor, _Source.cursor
        if pos != start:
            held, blocks = np.empty(0, dtype=object), self.blocks(start)
        parts, rows, coords = [held], len(held), shapely.get_num_coordinates(held).sum()
        while rows < n and coords <= CHUNK_COORDS and (block := next(blocks, None)) is not None:
            parts.append(block)
            rows, coords = rows + len(block), coords + shapely.get_num_coordinates(block).sum()
        geoms = np.concatenate(parts)
        over = np.flatnonzero(np.cumsum(shapely.get_num_coordinates(geoms[:n])) > CHUNK_COORDS)
        k = max(1, int(over[0])) if len(over) else min(n, len(geoms))
        self.cursor = (start + k, geoms[k:], blocks)
        return geoms[:k]


class _Parquet(_Source):
    def __init__(self, file):
        self.pf = pq.ParquetFile(_Ranged(file) if "://" in file else file, pre_buffer=True)
        schema, md = self.pf.schema_arrow, self.pf.metadata
        self.geo = json.loads((schema.metadata or {}).get(b"geo") or (md.metadata or {}).get(b"geo") or "{}")
        self.col = self.geo.get("primary_column")
        if self.col not in schema.names:
            h3 = next((c for c in schema.names if c.lower() in H3_NAMES), None)
            raise NotVector("This parquet has no GeoParquet geometry column"
                            + (f" ({h3!r} looks like H3 cells: open it in the H3 mode)." if h3 else "."))
        meta = self.geo["columns"][self.col]
        skip = {v[0] for v in meta.get("covering", {}).get("bbox", {}).values()}
        self.attrs = [f.name for f in schema
                      if f.name != self.col and not (f.name in skip or pa.types.is_binary(f.type))]
        self.ends = np.cumsum([md.row_group(i).num_rows for i in range(md.num_row_groups)])
        self.group = functools.lru_cache(4)(lambda i, cols: self.pf.read_row_group(i, columns=list(cols)))
        self.count, self.crs = md.num_rows, _arrow_to_geopandas(schema.empty_table().select([self.col]), self.geo).crs
        self.lonlat = _is_lonlat(self.crs)
        bbox = meta.get("bbox") or []
        native = [bbox[i] for i in (0, 1, 3, 4)] if len(bbox) == 6 else bbox[:4] or None
        self.info = {"layers": [], "geometry_types": [t for t in meta.get("geometry_types") or [] if t],
                     "native_bounds": native, "bounds4326": _bounds4326(native, self.crs),
                     "schema": [{"name": f.name, "dtype": str(f.type)} for f in schema if f.name in self.attrs]}

    def _rows(self, start, stop, columns):
        if stop <= start:
            return self.pf.schema_arrow.empty_table().select(columns)
        a, b = np.searchsorted(self.ends, [start, stop - 1], side="right")
        base = int(self.ends[a - 1]) if a else 0
        groups = [self.group(i, tuple(columns)) for i in range(a, b + 1)]
        return pa.concat_tables(groups).slice(start - base, stop - start)

    def blocks(self, start):
        for i in range(int(np.searchsorted(self.ends, start, side="right")), len(self.ends)):
            table = self.group(i, (self.col,))
            for k in range(max(0, start - int(self.ends[i]) + table.num_rows), table.num_rows, BLOCK):
                gs = _arrow_to_geopandas(table.slice(k, BLOCK), self.geo).geometry
                yield (gs if self.lonlat else gs.to_crs(4326)).to_numpy()

    def records(self, start, stop):
        return _records(self._rows(start, stop, self.attrs))

    def column(self, name):
        if name not in self.attrs:
            return [None] * self.count
        return [_safe(v) for v in self.pf.read(columns=[name])[name].to_pylist()]


class _Ogr(_Source):
    def __init__(self, file):
        self.src = file
        if "://" in file:
            pyogrio.set_gdal_config_options(GDAL_HTTP)
            self.src = "/vsicurl/" + file
        names = [str(n) for n, kind in pyogrio.list_layers(self.src) if kind]
        if not names:
            raise NotVector("no features with geometry in this file")
        infos = [pyogrio.read_info(self.src, layer=n, force_feature_count=True) for n in names]
        crs = [gpd.GeoSeries([], crs=i["crs"]).crs for i in infos]
        counts = [max(0, int(i["features"])) for i in infos]
        self.spans = [(n, b, c, None if _is_lonlat(r) else r)
                      for n, b, c, r in zip(names, np.cumsum([0, *counts[:-1]]).tolist(), counts, crs)]
        self.count, self.crs, self.multi = sum(counts), crs[0], len(names) > 1
        self.block = functools.lru_cache(8)(lambda n, b: pyogrio.read_arrow(
            self.src, layer=n, skip_features=b * BLOCK, max_features=BLOCK, read_geometry=False)[1])
        fields = {}
        for info in infos:
            for name, dtype in zip(info["fields"], info["dtypes"]):
                fields.setdefault(str(name), str(dtype))
        self.tag = "_layer" if self.multi else None
        while self.tag in fields:
            self.tag = "_" + self.tag
        self.info = {"layers": names,
                     "geometry_types": sorted({str(i["geometry_type"]) for i in infos} - {"Unknown", "None"}),
                     "native_bounds": _union(i["total_bounds"] for i in infos),
                     "bounds4326": _union(_bounds4326(i["total_bounds"], c) for i, c in zip(infos, crs)),
                     "schema": [{"name": n, "dtype": t} for n, t in {self.tag: "object", **fields}.items() if n]}

    def _parts(self, start, stop):
        for name, base, n, crs in self.spans:
            lo, hi = max(start, base), min(stop, base + n)
            if lo < hi:
                yield name, lo - base, hi - lo, crs

    def blocks(self, start):
        for name, skip, _, crs in self._parts(start, self.count):
            with pyogrio.open_arrow(self.src, layer=name, skip_features=skip, columns=[], batch_size=BLOCK,
                                    use_pyarrow=True) as (meta, reader):
                for batch in reader:
                    g = shapely.from_wkb(batch[meta["geometry_name"] or "wkb_geometry"])
                    yield g if crs is None else gpd.GeoSeries(g, crs=crs).to_crs(4326).to_numpy()

    def records(self, start, stop):
        out = []
        for name, skip, n, _ in self._parts(start, stop):
            first, last = skip // BLOCK, (skip + n - 1) // BLOCK
            table = pa.concat_tables([self.block(name, b) for b in range(first, last + 1)])
            out += _records(table.slice(skip - first * BLOCK, n), self.tag, name)
        return out

    def column(self, name):
        out = []
        for layer, _, n, _ in self.spans:
            if name == self.tag:
                out += [layer] * n
                continue
            table = pyogrio.read_arrow(self.src, layer=layer, read_geometry=False, columns=[name])[1]
            out += [_safe(v) for v in table[name].to_pylist()] if name in table.column_names else [None] * n
        return out


@functools.lru_cache(maxsize=2)
def _source(file, stamp):
    return (_Parquet if file.split("?")[0].lower().endswith(".parquet") else _Ogr)(file)


def main(file: str = "", op: str = "meta", start: int = 0, column: str = "", chunk: int = 100_000,
         table_rows: int = 50):
    start, chunk, table_rows = int(start), int(chunk), int(table_rows)
    if not file:
        return {"error": "no file selected"}
    remote = file.lower().startswith(("http://", "https://"))
    if not remote:
        if "://" in file:
            return {"error": "only http(s) URLs are read: sign an s3:// address first"}
        file = os.path.abspath(os.path.expanduser(file))
        if not os.path.isfile(file):
            return {"error": f"not a file: {file}"}
    stem, _, ext = (file.split("?")[0] if remote else file).rpartition(".")
    files = [file] + ([f"{stem}.{e}" for e in ("shx", "dbf", "prj", "cpg")] if ext.lower() == "shp" else [])
    sizes = ThreadPoolExecutor(len(files)).map(_size, files) if op == "meta" else ()
    try:
        with _lock:
            st = None if remote else os.stat(file)
            source = _source(file, st and (st.st_mtime_ns, st.st_size))
            if op == "row":
                return {"props": (source.records(start, start + 1) or [{}])[0]}
            if op == "column":
                return {"values": source.column(column)}
            geoms = source.take(start, chunk if op == "chunk" else min(chunk, FIRST_CHUNK))
            table = source.records(0, min(source.count, table_rows)) if op != "chunk" else None
        end, box = start + len(geoms), shapely.total_bounds(geoms)
        first = {"parts": _encode(geoms, np.arange(start, end)), "next": end if start < end < source.count else None,
                 "bounds": box.tolist() if np.isfinite(box).all() else None}
    except NotVector as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not read vector file: {type(e).__name__}: {e}"}
    if op == "chunk":
        return first
    info, crs = source.info, source.crs
    return {**info, "count": source.count, "table": table, "first": first, "file_size": sum(sizes),
            "crs": {"epsg": crs.to_epsg() if crs else None, "name": crs.name if crs else None},
            "geometry_types": info["geometry_types"] or sorted(
                {p["kind"].replace("geojson", "GeometryCollection") for p in first["parts"]})}


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass

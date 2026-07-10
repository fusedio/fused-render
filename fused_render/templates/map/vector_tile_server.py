"""Warm DuckDB-backed vector tile daemon for the map template.

Each fused-render runPython call is a fresh subprocess (~700ms), too slow for
per-tile serving. This module is both:

  1. a runPython entrypoint `main(action="ensure")` — starts (or reuses) a
     long-lived localhost daemon and returns its port; and
  2. the daemon (run as `python vector_tile_server.py --serve`) — holds any
     opened vector file reprojected into an in-memory DuckDB table plus a
     transient overview pyramid, and serves Mapbox Vector Tiles.

Endpoints (all GET, CORS *; /open also accepts POST):
  /ping                         -> {"ok", "version"}
  /quit
  /open?file=                   -> starts async warm-up, {"opening": true}
  /status?file=                 -> {phase, pct, ready, detail_zoom, error}
  /meta?file=                   -> {bounds_4326, count, geometry_type, columns,
                                    minzoom, maxzoom, detail_zoom}
  /tile/{z}/{x}/{y}.mvt?file=   -> MVT bytes (application/vnd.mapbox-vector-tile)

The overview pyramid lives only in DuckDB memory — never written as a tile
file. Idle shutdown after 30 min. The state file embeds this module's mtime, so
editing it auto-respawns a fresh daemon on the next ensure().
"""
import hashlib
import json
import math
import os
import sys
import threading
import time

STATE = os.path.expanduser("~/.cache/fused-render-map-v1/daemon.json")
IDLE_EXIT_S = 30 * 60
MERC_R = 6378137.0
MERC_MAX = math.pi * MERC_R
CAP_AREA = 4000        # per tile-cell feature cap (polygons / lines)
CAP_POINT = 12000      # generous cap for points


def _me():
    if "__file__" in globals():
        return os.path.abspath(__file__)
    return os.path.join(os.path.abspath(sys.path[0]), "vector_tile_server.py")


def _version():
    try:
        return str(os.path.getmtime(_me())) + "|" + sys.executable
    except OSError:
        return "0"


# ================================================================ ensure()
def _alive(port, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version
    except Exception:
        return False


def main(action: str = "ensure"):
    """runPython entrypoint: make sure the daemon is running, return {port}."""
    import subprocess
    version = _version()
    try:
        with open(STATE) as f:
            st = json.load(f)
        if _alive(st.get("port"), version):
            return {"port": st["port"], "reused": True, "version": version}
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit", timeout=1).read()
        except Exception:
            pass
    except (OSError, ValueError):
        pass

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    log = os.path.join(os.path.dirname(STATE), "daemon.log")
    with open(log, "ab") as lf:
        subprocess.Popen([sys.executable, _me(), "--serve"],
                         stdout=lf, stderr=lf,
                         start_new_session=True, cwd=os.path.dirname(_me()))
    for _ in range(200):
        time.sleep(0.05)
        try:
            with open(STATE) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), version):
                return {"port": st["port"], "reused": False, "version": version}
        except (OSError, ValueError):
            continue
    return {"error": f"daemon did not start — see {log}"}


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass


# ================================================================ daemon
def _serve():
    import duckdb
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    sys.path.insert(0, os.path.dirname(_me()))
    VERSION = _version()
    last_hit = [time.time()]

    con = duckdb.connect()
    con.execute("PRAGMA threads=8")
    con.execute("LOAD spatial")

    files = {}                       # path -> file-state dict
    files_lock = threading.Lock()

    tile_cache = {}                  # (path,z,x,y) -> bytes
    tile_order = []
    tile_lock = threading.Lock()
    MAX_TILES = 500

    def _tid(path):
        return "f" + hashlib.sha1(path.encode()).hexdigest()[:12]

    def _merc_to_lonlat(x, y):
        lon = x / MERC_MAX * 180.0
        lat = math.degrees(2 * math.atan(math.exp(y / MERC_R)) - math.pi / 2)
        return lon, lat

    # ---------------- load an arbitrary vector file -> registered relation ----
    GDAL_EXT = (".gpkg", ".shp", ".geojson", ".json", ".fgb", ".kml", ".gml")

    def _load_source(cur, path, tid):
        """Register a source relation for `path`; return
        (relname, geom_sql, attrs, src_crs, geometry_type, count)."""
        low = path.lower()
        rel = tid + "_src"
        if low.endswith(GDAL_EXT):
            import pyogrio
            from pyogrio.raw import read_arrow
            layer = None
            try:
                layers = pyogrio.list_layers(path)
                if layers is not None and len(layers) > 1:
                    best, bestn = None, -1
                    for lname in [row[0] for row in layers]:
                        ni = pyogrio.read_info(path, layer=lname).get("features", 0)
                        if ni > bestn:
                            best, bestn = lname, ni
                    layer = best
            except Exception:
                layer = None
            meta, tbl = read_arrow(path, layer=layer) if layer else read_arrow(path)
            fields = [str(f) for f in list(meta.get("fields", []))]
            gname = meta.get("geometry_name") or ""
            if not gname or gname not in tbl.column_names:
                non_fields = [c for c in tbl.column_names if c not in fields]
                gname = non_fields[-1] if non_fields else tbl.column_names[-1]
            attrs = [c for c in fields if c != gname][:5]
            cur.register(rel, tbl)
            gtype = cur.execute(f'SELECT typeof("{gname}") FROM {rel} LIMIT 1').fetchone()
            gtype = gtype[0] if gtype else ""
            geom_sql = (f'"{gname}"::GEOMETRY' if "GEOMETRY" in gtype
                        else f'ST_GeomFromWKB("{gname}")')
            return (rel, geom_sql, attrs, meta.get("crs"),
                    meta.get("geometry_type"), tbl.num_rows)

        if low.endswith((".parquet", ".geoparquet")):
            import geopandas as gpd
            gdf = gpd.read_parquet(path)
            gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
            gname = gdf.geometry.name
            crs = gdf.crs.to_string() if gdf.crs else None
            gtype = sorted(gdf.geom_type.dropna().unique().tolist())
            gtype = gtype[0] if gtype else "Unknown"
            attrs = [c for c in gdf.columns if c != gname][:5]
            import pandas as pd
            df = pd.DataFrame(gdf.drop(columns=[gname]))
            df["__wkb__"] = gdf.geometry.to_wkb()
            cur.register(rel, df)
            return (rel, 'ST_GeomFromWKB("__wkb__")', attrs, crs, gtype, len(df))

        if low.endswith(".csv"):
            import geo_classify as gc
            import pandas as pd
            df = pd.read_csv(path)
            lat = gc._find_col(df.columns, gc.LAT_NAMES)
            lon = gc._find_col(df.columns, gc.LON_NAMES)
            if not (lat and lon and lat != lon):
                raise ValueError("CSV has no lat/lon columns")
            df = df[pd.to_numeric(df[lat], errors="coerce").notna()
                    & pd.to_numeric(df[lon], errors="coerce").notna()].copy()
            df[lat] = pd.to_numeric(df[lat]); df[lon] = pd.to_numeric(df[lon])
            attrs = [c for c in df.columns if c not in (lat, lon)][:5]
            cur.register(rel, df)
            return (rel, f'ST_Point("{lon}", "{lat}")', attrs, "EPSG:4326",
                    "Point", len(df))

        raise ValueError(f"unsupported vector file: {path}")

    def _src_srs(crs):
        if not crs:
            return "EPSG:4326"
        return crs

    def _fam(gtype):
        g = (gtype or "").lower()
        if "polygon" in g:
            return "polygon"
        if "line" in g or "curve" in g:
            return "line"
        if "point" in g:
            return "point"
        return "polygon"

    def _qattrs(attrs):
        return [f'"{a}"' for a in attrs]

    # ---------------- warm-up (background thread) ----------------
    def _warm(path):
        f = files[path]
        cur = con.cursor()
        tid = f["tid"]
        try:
            f["phase"] = "reading"
            rel, geom_sql, attrs, crs, gtype, count = _load_source(cur, path, tid)
            f["columns"] = attrs
            f["geometry_type"] = gtype
            f["count"] = count
            fam = _fam(gtype)
            qa = _qattrs(attrs)
            sel_a = (", " + ", ".join(qa)) if qa else ""
            base = tid + "_base"

            f["phase"] = "materializing"; f["pct"] = 5
            srs = _src_srs(crs)
            if str(srs).replace(":", "").upper().endswith("3857"):
                gt = geom_sql
            else:
                gt = f"ST_Transform({geom_sql}, '{srs}', 'EPSG:3857', always_xy:=true)"
            cur.execute(f"""
                CREATE OR REPLACE TABLE {base} AS
                SELECT ST_MakeValid({gt}) AS geom{sel_a}
                FROM {rel}
            """)
            cur.unregister(rel)
            cur.execute(f"DELETE FROM {base} WHERE geom IS NULL OR ST_IsEmpty(geom)")
            cur.execute(f"ALTER TABLE {base} ADD COLUMN imp DOUBLE")
            if fam == "polygon":
                cur.execute(f"UPDATE {base} SET imp = ST_Area(geom)")
            elif fam == "line":
                cur.execute(f"UPDATE {base} SET imp = ST_Length(geom)")
            else:
                cur.execute(f"UPDATE {base} SET imp = random()")

            f["phase"] = "indexing"; f["pct"] = 15
            cur.execute(f"CREATE INDEX {base}_rtree ON {base} USING RTREE (geom)")
            row = cur.execute(f"""SELECT count(*), min(ST_XMin(geom)), min(ST_YMin(geom)),
                                  max(ST_XMax(geom)), max(ST_YMax(geom)) FROM {base}""").fetchone()
            n = row[0] or 0
            f["count"] = n
            f["base"] = base
            if n == 0:
                f["bounds_4326"] = None
            else:
                w, s, e, nn = row[1], row[2], row[3], row[4]
                f["merc_bbox"] = [w, s, e, nn]
                lw, ls = _merc_to_lonlat(w, s)
                le, ln = _merc_to_lonlat(e, nn)
                f["bounds_4326"] = [lw, ls, le, ln]

            cap = CAP_POINT if fam == "point" else CAP_AREA
            zd = _detail_zoom(f, n, cap)
            f["detail_zoom"] = zd
            # base table already serves z >= zd and coarse (empty) views
            f["ready"] = True
            f["phase"] = "overviews"

            for i, z in enumerate(range(0, zd)):          # coarsest first
                _build_overview(cur, f, z, fam, cap, qa)
                f["ov_built"].append(z)
                _drop_file_tiles(path)
                f["pct"] = 20 + int(78 * (i + 1) / max(zd, 1))
            f["pct"] = 100
            f["phase"] = "ready"
        except Exception as e:
            import traceback
            traceback.print_exc()
            f["error"] = f"{type(e).__name__}: {e}"
            f["phase"] = "error"

    def _detail_zoom(f, n, cap):
        bb = f.get("merc_bbox")
        if not bb or n == 0:
            return 0
        w, s, e, nn = bb

        def tiles(z):
            span = 2 * MERC_MAX / (1 << z)
            tx0 = int((w + MERC_MAX) // span); tx1 = int((e + MERC_MAX) // span)
            ty0 = int((MERC_MAX - nn) // span); ty1 = int((MERC_MAX - s) // span)
            return (tx1 - tx0 + 1) * (ty1 - ty0 + 1)

        z = 0
        while z < 18 and n / tiles(z) > cap:
            z += 1
        return z

    def _build_overview(cur, f, z, fam, cap, qa):
        base = f["base"]
        ov = f["tid"] + f"_ov{z}"
        span = 2 * MERC_MAX / (1 << z)
        px = span / 256.0
        tol = span / 4096.0
        sel = (", " + ", ".join(qa)) if qa else ""
        prefilter = f"WHERE imp >= {px * px}" if fam != "point" else ""
        cur.execute(f"""
            CREATE OR REPLACE TABLE {ov} AS
            WITH c AS (
              SELECT geom, imp{sel},
                     CAST(floor((ST_XMin(geom)+{MERC_MAX})/{span}) AS INT) AS __tx,
                     CAST(floor(({MERC_MAX}-ST_YMax(geom))/{span}) AS INT) AS __ty
              FROM {base} {prefilter}
            ), r AS (
              SELECT *, row_number() OVER (PARTITION BY __tx, __ty ORDER BY imp DESC) AS __rn
              FROM c
            )
            SELECT ST_SimplifyPreserveTopology(geom, {tol}) AS geom{sel}
            FROM r WHERE __rn <= {cap}
        """)
        cur.execute(f"CREATE INDEX {ov}_rtree ON {ov} USING RTREE (geom)")

    # ---------------- routing + tile render ----------------
    def _route_table(f, z):
        zd = f.get("detail_zoom", 0)
        if z >= zd:
            return f["base"]
        built = f["ov_built"]
        if not built:
            return f["base"]
        le = [b for b in built if b <= z]
        z2 = max(le) if le else min(built)
        return f["tid"] + f"_ov{z2}"

    def _render_tile(f, z, x, y):
        table = _route_table(f, z)
        qa = _qattrs(f["columns"])
        struct = ["geom: ST_AsMVTGeom(geom, "
                  f"ST_Extent(ST_TileEnvelope({z},{x},{y}))::BOX_2D, 4096, 256, true)"]
        for a, qn in zip(f["columns"], qa):
            struct.append(f'"{a}": {qn}')
        sel = (", " + ", ".join(qa)) if qa else ""
        cur = con.cursor()
        row = cur.execute(f"""
            WITH src AS (SELECT geom{sel} FROM {table}
                         WHERE ST_Intersects(geom, ST_TileEnvelope({z},{x},{y})))
            SELECT ST_AsMVT({{{', '.join(struct)}}}, 'layer', 4096, 'geom') FROM src
        """).fetchone()
        return row[0] if row else None

    def _drop_file_tiles(path):
        with tile_lock:
            keys = [k for k in tile_cache if k[0] == path]
            for k in keys:
                tile_cache.pop(k, None)
            tile_order[:] = [k for k in tile_order if k[0] != path]

    # ---------------- request handlers ----------------
    def _ensure_open(path):
        with files_lock:
            f = files.get(path)
            if f is not None:
                return f
            f = {"tid": _tid(path), "phase": "queued", "pct": 0, "ready": False,
                 "error": None, "detail_zoom": None, "bounds_4326": None,
                 "count": None, "geometry_type": None, "columns": [],
                 "base": None, "ov_built": []}
            files[path] = f
        threading.Thread(target=_warm, args=(path,), daemon=True).start()
        return f

    def do_open(q):
        path = _abspath(q.get("file", [None])[0])
        if not path:
            return 400, b'{"error":"missing file"}', "application/json"
        f = _ensure_open(path)
        return 200, json.dumps({"opening": not f["ready"], "ready": f["ready"]}).encode(), "application/json"

    def do_status(q):
        path = _abspath(q.get("file", [None])[0])
        f = files.get(path) if path else None
        if f is None:
            return 200, json.dumps({"phase": "unopened", "pct": 0, "ready": False,
                                    "detail_zoom": None, "error": None}).encode(), "application/json"
        return 200, json.dumps({"phase": f["phase"], "pct": f["pct"], "ready": f["ready"],
                                "detail_zoom": f["detail_zoom"], "error": f["error"]}).encode(), "application/json"

    def do_meta(q):
        path = _abspath(q.get("file", [None])[0])
        f = _ensure_open(path) if path else None
        if f is None:
            return 400, b'{"error":"missing file"}', "application/json"
        m = {"bounds_4326": f["bounds_4326"], "count": f["count"],
             "geometry_type": f["geometry_type"], "columns": f["columns"],
             "minzoom": 0, "maxzoom": 18, "detail_zoom": f["detail_zoom"],
             "ready": f["ready"], "phase": f["phase"], "error": f["error"]}
        return 200, json.dumps(m).encode(), "application/json"

    MVT_CT = "application/vnd.mapbox-vector-tile"

    def do_tile(q, z, x, y):
        path = _abspath(q.get("file", [None])[0])
        f = files.get(path) if path else None
        if f is None or f["base"] is None:
            return 204, b"", MVT_CT
        key = (path, z, x, y)
        with tile_lock:
            b = tile_cache.get(key)
        if b is not None:
            return (200, b, MVT_CT) if b else (204, b"", MVT_CT)
        b = _render_tile(f, z, x, y)
        b = bytes(b) if b else b""
        with tile_lock:
            tile_cache[key] = b
            tile_order.append(key)
            while len(tile_order) > MAX_TILES:
                old = tile_order.pop(0)
                tile_cache.pop(old, None)
        return (200, b, MVT_CT) if b else (204, b"", MVT_CT)

    def _abspath(p):
        if not p:
            return None
        return os.path.abspath(os.path.expanduser(p))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _handle(self, q):
            u = urlparse(self.path)
            if u.path == "/ping":
                return 200, json.dumps({"ok": True, "version": VERSION}).encode(), "application/json"
            if u.path == "/quit":
                threading.Thread(target=srv.shutdown, daemon=True).start()
                return 200, b"bye", "text/plain"
            if u.path == "/open":
                return do_open(q)
            if u.path == "/status":
                return do_status(q)
            if u.path == "/meta":
                return do_meta(q)
            if u.path.startswith("/tile/"):
                parts = u.path.split("/")
                z, x = int(parts[2]), int(parts[3])
                y = int(parts[4].split(".")[0])
                return do_tile(q, z, x, y)
            return 404, b"not found", "text/plain"

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            try:
                code, body, ct = self._handle(parse_qs(u.query))
            except Exception as e:
                import traceback
                traceback.print_exc()
                code, body, ct = 500, str(e).encode(), "text/plain"
            self._send(code, body, ct)

        def do_POST(self):
            last_hit[0] = time.time()
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode() if length else ""
                u = urlparse(self.path)
                q = parse_qs(u.query) or parse_qs(raw)
                code, body, ct = self._handle(q)
            except Exception as e:
                code, body, ct = 500, str(e).encode(), "text/plain"
            self._send(code, body, ct)

        def _send(self, code, body, ct):
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                if body:
                    self.wfile.write(body)
            except BrokenPipeError:
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump({"port": port, "pid": os.getpid(), "version": VERSION}, fh)

    def reaper():
        while True:
            time.sleep(60)
            if time.time() - last_hit[0] > IDLE_EXIT_S:
                srv.shutdown()
                return
    threading.Thread(target=reaper, daemon=True).start()
    print(f"vector tile daemon on 127.0.0.1:{port} (v{VERSION})", flush=True)
    srv.serve_forever()


if __name__ == "__main__" and "--serve" in sys.argv:
    import duckdb  # noqa: F401  (fail fast if interpreter lacks duckdb)
    _serve()

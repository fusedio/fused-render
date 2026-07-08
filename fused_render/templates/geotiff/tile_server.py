"""Persistent GeoTIFF tile daemon for the geotiff preview.

The fused-render runner spawns a fresh subprocess per runPython call
(~700ms fixed overhead), which caps pan/zoom responsiveness. This module is
both:

  1. a runPython entrypoint `main(action="ensure")` — starts (or reuses) a
     long-lived localhost HTTP daemon and returns its port; and
  2. the daemon itself (run as `python tile_server.py --serve`) — holds the
     mmap'd file + parsed IFD index + a decoded-tile LRU warm, and serves
     256px Web-Mercator XYZ tiles straight to MapLibre.

Endpoints (all GET, CORS *):
  /ping                        -> {"ok": true, "version": ...}
  /meta?file=                  -> header metadata + supported flag + stretch
  /tile/{z}/{x}/{y}.png?file=&mode=&band=&r=&g=&b=&cmap=&robust=&div=
        &stretch=&vlo=&vhi=    -> PNG tile (transparent where no data)
  /hist?file=&bbox=w,s,e,n(3857)&bins=&band=&mode=  -> viewport stats+hist
  /value?file=&lon=&lat=&band= -> raw value under cursor

Only pure-decodable TIFFs (uncompressed/deflate) are served; /meta returns
supported=false otherwise and the page falls back to tiff_reader.py.
Idle shutdown after 30 min. The state file embeds this file's mtime, so
editing the module auto-respawns a fresh daemon on the next ensure().
"""
# /// script
# dependencies = ["numpy", "pyproj", "imagecodecs"]
# ///

import json
import math
import os
import sys
import threading
import time

STATE = os.path.expanduser("~/.cache/fused-render-geotiff-v2/daemon.json")
IDLE_EXIT_S = 30 * 60
TILE = 256
MERC_R = 6378137.0
MERC_MAX = math.pi * MERC_R


def _me():
    if "__file__" in globals():
        return os.path.abspath(__file__)
    return os.path.join(os.path.abspath(sys.path[0]), "tile_server.py")


def _version():
    try:
        return str(os.path.getmtime(_me())) + "|" + _daemon_python()
    except OSError:
        return "0"


DAEMON_VENV = os.path.expanduser("~/.cache/fused-render-geotiff-v2/venv")
DAEMON_DEPS = ["numpy", "pyproj", "imagecodecs"]


def _daemon_python():
    """Interpreter for the daemon: a self-managed uv venv (so imagecodecs is
    available even though the app runner falls back to its bundled python),
    else whatever interpreter is running now."""
    vp = os.path.join(DAEMON_VENV, "bin", "python")
    if os.path.exists(vp):
        return vp
    import shutil
    import subprocess
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    if os.path.exists(uv):
        try:
            os.makedirs(os.path.dirname(DAEMON_VENV), exist_ok=True)
            subprocess.run([uv, "venv", "--python", "3.12", DAEMON_VENV],
                           check=True, capture_output=True, timeout=120)
            subprocess.run([uv, "pip", "install", "-p", vp] + DAEMON_DEPS,
                           check=True, capture_output=True, timeout=300)
            return vp
        except Exception:
            import shutil as _sh
            _sh.rmtree(DAEMON_VENV, ignore_errors=True)
    return sys.executable


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
        # stale daemon (old version or dead) — ask it to quit, then respawn
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
        subprocess.Popen([_daemon_python(), _me(), "--serve"],
                         stdout=lf, stderr=lf,
                         start_new_session=True, cwd=os.path.dirname(_me()))
    # wait for the state file to appear with a live port
    for _ in range(100):
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
    import numpy as np
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    sys.path.insert(0, os.path.dirname(_me()))
    import _tiff_core as T
    import tiff_reader as R          # reuse encode_png / _lut / stretch utils

    try:
        import imagecodecs as IC
    except ImportError:
        IC = None
    # compression codes the daemon can decode (1/8/32946 via stdlib zlib;
    # the rest via imagecodecs when present)
    SUPPORTED_COMPS = {1, 8, 32946} | (
        {5, 7, 32773, 50000, 50001, 34887} if IC else set())

    VERSION = _version()
    last_hit = [time.time()]

    # ---------------- file cache: parsed pyramid per path ----------------
    files = {}          # path -> dict(levels, meta, lock, stretch cache)
    files_lock = threading.Lock()

    def _parse_level(buf, en, t):
        jt = T._v(t, 347)      # JPEGTables (shared quant/huffman tables)
        lv = {"tags": t,
              "W": T._v1(t, 256), "H": T._v1(t, 257),
              "spp": T._v1(t, 277, 1),
              "comp": T._v1(t, 259, 1),
              "predictor": T._v1(t, 317, 1),
              "planar": T._v1(t, 284, 1),
              "photometric": T._v1(t, 262, 1),
              "jpegtables": bytes(jt) if isinstance(jt, list) else None}
        bits = T._v(t, 258, [8]); bits = bits[0] if isinstance(bits, list) else bits
        sf = T._v(t, 339, [1]); sf = sf[0] if isinstance(sf, list) else sf
        lv["dtype"] = T._numpy_dtype(sf, bits, en)
        if 322 in t:
            lv["tiled"] = True
            lv["tw"], lv["th"] = T._v1(t, 322), T._v1(t, 323)
            lv["offs"], lv["counts"] = T._v(t, 324), T._v(t, 325)
        else:
            lv["tiled"] = False
            lv["rps"] = T._v1(t, 278, lv["H"])
            lv["offs"], lv["counts"] = T._v(t, 273), T._v(t, 279)
        # decode 1 chunk up-front to verify decodability
        return lv

    def open_file(path):
        path = os.path.abspath(os.path.expanduser(path))
        with files_lock:
            f = files.get(path)
        if f:
            return f
        buf, en, t0, next_off = T.parse_header(path)
        meta = T.header_meta(path, buf, en, t0, next_off)
        levels = [_parse_level(buf, en, t0)]
        off, hops = next_off, 0
        while off and hops < 32:
            try:
                t, off = T._read_ifd(buf, en, off)
            except Exception:
                break
            w = T._v1(t, 256)
            subtype = T._v1(t, 254, 0) or 0
            # keep reduced-resolution images (bit 0), skip masks (bit 2)
            if w and T._v1(t, 277, 1) == levels[0]["spp"] and not (subtype & 4):
                levels.append(_parse_level(buf, en, t))
            hops += 1
        levels.sort(key=lambda l: -l["W"])
        supported = all(l["comp"] in SUPPORTED_COMPS for l in levels)
        epsg = (meta.get("crs") or {}).get("epsg")
        f = {"path": path, "buf": buf, "en": en, "levels": levels,
             "meta": meta, "epsg": epsg,
             "transform": meta.get("transform"), "bounds": meta.get("bounds"),
             "supported": bool(supported and epsg and meta.get("transform")),
             "lock": threading.Lock(), "chunks": {}, "chunk_order": [],
             "stretch": {}}
        with files_lock:
            files[path] = f
        return f

    # ---------------- chunk-granular decode with LRU ----------------
    MAX_CACHE_CHUNKS = 4096

    def get_chunk(f, li, ci):
        """Decode one TIFF tile/strip of level li -> (h, w, spp) ndarray."""
        key = (li, ci)
        lv = f["levels"][li]
        with f["lock"]:
            a = f["chunks"].get(key)
            if a is not None:
                return a
        raw = f["buf"][lv["offs"][ci]:lv["offs"][ci] + lv["counts"][ci]]
        spp = lv["spp"] if lv["planar"] == 1 else 1
        if lv["tiled"]:
            h, w = lv["th"], lv["tw"]
        else:
            h = min(lv["rps"], lv["H"] - (ci % _nchunks_y(lv)) * lv["rps"])
            w = lv["W"]
        comp = lv["comp"]
        if comp == 7 and IC:
            # JPEG-in-TIFF: merge the shared JPEGTables; imagecodecs converts
            # YCbCr -> RGB itself, returning (h, w, spp) uint8
            a = IC.jpeg_decode(bytes(raw), tables=lv["jpegtables"])
            if a.ndim == 2:
                a = a[:, :, None]
            a = a[:h, :w, :spp]
        else:
            if comp in (1, 8, 32946):
                data = T._decompress(raw, comp)
            elif comp == 5 and IC:
                data = IC.lzw_decode(bytes(raw))
            elif comp == 32773 and IC:
                data = IC.packbits_decode(bytes(raw))
            elif comp == 50000 and IC:
                data = IC.zstd_decode(bytes(raw))
            elif comp == 50001 and IC:
                a0 = IC.webp_decode(bytes(raw))
                data = np.ascontiguousarray(a0[:h, :w, :spp]).tobytes()
            elif comp == 34887 and IC:
                data = IC.lerc_decode(bytes(raw)).tobytes()
            else:
                raise T.Unsupported(f"TIFF compression {comp}")
            a = np.frombuffer(data, dtype=lv["dtype"])[: h * w * spp].reshape(h, w, spp)
            a = T._unpredict(a, lv["predictor"])
        with f["lock"]:
            f["chunks"][key] = a
            f["chunk_order"].append(key)
            while len(f["chunk_order"]) > MAX_CACHE_CHUNKS:
                old = f["chunk_order"].pop(0)
                f["chunks"].pop(old, None)
        return a

    def _nchunks_y(lv):
        if lv["tiled"]:
            return (lv["H"] + lv["th"] - 1) // lv["th"]
        return (lv["H"] + lv["rps"] - 1) // lv["rps"]

    def read_window(f, li, x0, y0, x1, y1, band_idx):
        """(len(band_idx), y1-y0, x1-x0) float32 from level li, NaN nodata."""
        lv = f["levels"][li]
        W, H, spp = lv["W"], lv["H"], lv["spp"]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(W, x1), min(H, y1)
        out = np.full((len(band_idx), y1 - y0, x1 - x0), np.nan, dtype="float32")
        if x1 <= x0 or y1 <= y0:
            return out
        planar = lv["planar"]
        if lv["tiled"]:
            tw, th = lv["tw"], lv["th"]
            across = (W + tw - 1) // tw
            down = (H + th - 1) // th
            per_plane = across * down
            for ty in range(y0 // th, (y1 - 1) // th + 1):
                for tx in range(x0 // tw, (x1 - 1) // tw + 1):
                    for oi, bk in enumerate(band_idx):
                        ci = (bk * per_plane if planar == 2 else 0) + ty * across + tx
                        a = get_chunk(f, li, ci)
                        s = 0 if planar == 2 else bk
                        gy0, gx0 = ty * th, tx * tw
                        sy0, sx0 = max(y0, gy0), max(x0, gx0)
                        sy1, sx1 = min(y1, gy0 + th, H), min(x1, gx0 + tw, W)
                        out[oi, sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = \
                            a[sy0 - gy0:sy1 - gy0, sx0 - gx0:sx1 - gx0, s]
        else:
            rps = lv["rps"]
            nsy = _nchunks_y(lv)
            for si in range(y0 // rps, (y1 - 1) // rps + 1):
                for oi, bk in enumerate(band_idx):
                    ci = (bk * nsy if planar == 2 else 0) + si
                    a = get_chunk(f, li, ci)
                    s = 0 if planar == 2 else bk
                    gy0 = si * rps
                    sy0, sy1 = max(y0, gy0), min(y1, gy0 + a.shape[0])
                    out[oi, sy0 - y0:sy1 - y0, :] = a[sy0 - gy0:sy1 - gy0, x0:x1, s]
        nod = f["meta"].get("nodata")
        if nod is not None:
            out[out == nod] = np.nan
        return out

    # ---------------- mercator plumbing ----------------
    from pyproj import Transformer
    _tr_cache = {}

    def merc_to_native(epsg):
        if int(epsg) == 3857:
            return None
        tr = _tr_cache.get(epsg)
        if tr is None:
            tr = Transformer.from_crs(3857, int(epsg), always_xy=True)
            _tr_cache[epsg] = tr
        return tr

    def tile_bbox(z, x, y):
        n = 2 ** z
        s = 2 * MERC_MAX / n
        return (-MERC_MAX + x * s, MERC_MAX - (y + 1) * s,
                -MERC_MAX + (x + 1) * s, MERC_MAX - y * s)

    def native_res_in_merc(f, li):
        """Approx native pixel size of level li expressed in merc m/px."""
        b = f["bounds"]
        try:
            x0, y0, x1, y1 = R._merc_bbox(b, f["epsg"])
        except Exception:
            return None
        return (x1 - x0) / f["levels"][li]["W"]

    def pick_level(f, merc_res):
        best = 0
        for li in range(len(f["levels"])):
            r = native_res_in_merc(f, li)
            if r is not None and r <= merc_res * 1.4:
                best = li
        return best

    def sample_merc_grid(f, band_idx, mx0, my0, mx1, my1, ow, oh):
        """Sample bands onto a regular merc grid -> (n, oh, ow) float32."""
        mx = np.linspace(mx0, mx1, ow + 1)[:-1] + (mx1 - mx0) / (2 * ow)
        my = np.linspace(my1, my0, oh + 1)[:-1] - (my1 - my0) / (2 * oh)
        MXg, MYg = np.meshgrid(mx, my)
        tr = merc_to_native(f["epsg"])
        if tr is None:
            NX, NY = MXg, MYg
        else:
            NX, NY = tr.transform(MXg, MYg)
        a, b_, c, d, e, fof = f["transform"]
        # invert affine (supports rotation)
        det = a * e - b_ * d
        if abs(det) < 1e-18:
            return np.full((len(band_idx), oh, ow), np.nan, "float32")
        px = (e * (NX - c) - b_ * (NY - fof)) / det
        py = (-d * (NX - c) + a * (NY - fof)) / det
        fin = np.isfinite(px) & np.isfinite(py)
        W0, H0 = f["levels"][0]["W"], f["levels"][0]["H"]
        inside = fin & (px >= 0) & (px < W0) & (py >= 0) & (py < H0)
        if not inside.any():
            return np.full((len(band_idx), oh, ow), np.nan, "float32")
        # choose level: full-res pixels per output pixel
        merc_res = (mx1 - mx0) / ow
        li = pick_level(f, merc_res)
        lv = f["levels"][li]
        sx, sy = lv["W"] / float(W0), lv["H"] / float(H0)
        lpx = np.clip(px * sx, 0, lv["W"] - 1)
        lpy = np.clip(py * sy, 0, lv["H"] - 1)
        x0 = int(np.floor(lpx[inside].min())); x1 = int(np.ceil(lpx[inside].max())) + 1
        y0 = int(np.floor(lpy[inside].min())); y1 = int(np.ceil(lpy[inside].max())) + 1
        win = read_window(f, li, x0, y0, x1, y1, band_idx)
        ix = np.clip(lpx.astype(np.int32) - x0, 0, win.shape[2] - 1)
        iy = np.clip(lpy.astype(np.int32) - y0, 0, win.shape[1] - 1)
        out = win[:, iy, ix]
        out[:, ~inside] = np.nan
        return out

    # ---------------- stretch (cached per file+channels) ----------------
    def get_stretch(f, band_idx, robust):
        key = (tuple(band_idx), robust)
        st = f["stretch"].get(key)
        if st:
            return st
        li = len(f["levels"]) - 1
        lv = f["levels"][li]
        # smallest overview capped at ~1M cells for the estimate
        step = 1
        while (lv["W"] // step) * (lv["H"] // step) > 1_000_000 and step < 64:
            step += 1
        arr = read_window(f, li, 0, 0, lv["W"], lv["H"], band_idx)[:, ::step, ::step]
        st = []
        for k in range(arr.shape[0]):
            fin = arr[k][np.isfinite(arr[k])]
            if not fin.size:
                st.append([0.0, 1.0]); continue
            if robust:
                lo, hi = float(np.percentile(fin, 2)), float(np.percentile(fin, 98))
            else:
                lo, hi = float(fin.min()), float(fin.max())
            if hi <= lo:
                hi = lo + 1.0
            st.append([lo, hi])
        f["stretch"][key] = st
        return st

    # ---------------- render ----------------
    def q1(q, k, dflt=None):
        v = q.get(k)
        return v[0] if v else dflt

    def render_params(q, f):
        count = f["levels"][0]["spp"]
        mode = q1(q, "mode", "auto")
        want_rgb = (mode == "rgb") or (mode == "auto" and count >= 3)
        if want_rgb and count >= 3:
            idx = [min(max(int(q1(q, k, str(i + 1))), 1), count) - 1
                   for i, k in enumerate(("r", "g", "b"))]
        else:
            want_rgb = False
            idx = [min(max(int(q1(q, "band", "1")), 1), count) - 1]
        return want_rgb, idx

    def parse_stretch(q, f, idx, want_rgb):
        robust = q1(q, "robust", "1") == "1"
        s = q1(q, "stretch", "")
        try:
            v = [float(x) for x in s.split(",")]
            n = 3 if want_rgb else 1
            if len(v) == 2 * n:
                return [[v[2 * i], v[2 * i + 1]] for i in range(n)]
        except (ValueError, AttributeError):
            pass
        return get_stretch(f, idx, robust)

    def do_tile(q, z, x, y):
        f = open_file(q1(q, "file"))
        if not f["supported"]:
            return 404, b"unsupported", "text/plain"
        want_rgb, idx = render_params(q, f)
        st = parse_stretch(q, f, idx, want_rgb)
        mx0, my0, mx1, my1 = tile_bbox(z, x, y)
        arr = sample_merc_grid(f, idx, mx0, my0, mx1, my1, TILE, TILE)
        if want_rgb:
            rgba = np.zeros((TILE, TILE, 4), "uint8")
            valid = np.isfinite(arr).all(axis=0)
            for k in range(3):
                lo, hi = st[k]
                v = np.clip((arr[k] - lo) / max(hi - lo, 1e-12), 0, 1)
                rgba[:, :, k] = np.where(np.isfinite(v), v * 255, 0).astype("uint8")
            rgba[:, :, 3] = np.where(valid, 255, 0)
        else:
            vals = arr[0].astype("float64")
            lo, hi = st[0]
            if q1(q, "div", "0") == "1":
                m = max(abs(lo), abs(hi)) or 1.0
                lo, hi = -m, m
            lut = R._lut(q1(q, "cmap", "viridis"))
            t = np.clip((vals - lo) / max(hi - lo, 1e-12), 0, 1)
            ix = np.where(np.isfinite(t), t * 255, 0).astype("uint8")
            rgba = np.zeros((TILE, TILE, 4), "uint8")
            rgba[:, :, :3] = lut[ix]
            alpha = np.isfinite(vals)
            vlo, vhi = q1(q, "vlo", ""), q1(q, "vhi", "")
            if vlo not in ("", None) and vhi not in ("", None):
                alpha = alpha & (vals >= float(vlo)) & (vals <= float(vhi))
            rgba[:, :, 3] = np.where(alpha, 255, 0)
        return 200, R.encode_png(np.ascontiguousarray(rgba)), "image/png"

    def _deep_meta(f, m):
        """gdalinfo-parity extras: WKT, geotransform, corner coords, files,
        per-band info. Cheap — pyproj + tag data only, plus a min/max pass
        on the smallest overview (cached)."""
        from pyproj import CRS, Transformer
        epsg, tr = f["epsg"], f["transform"]
        # full CRS description
        if epsg:
            try:
                crs = CRS.from_epsg(int(epsg))
                m["crs_wkt"] = crs.to_wkt(pretty=True)
                m["crs_units"] = (crs.axis_info[0].unit_name
                                  if crs.axis_info else None)
            except Exception:
                pass
        # geotransform -> origin + pixel size
        if tr:
            a, b_, c, d, e, ff = tr
            m["geotransform"] = tr
            m["origin"] = [c, ff]
            m["pixel_size"] = [a, e]
            m["rotation"] = None if (b_ == 0 and d == 0) else [b_, d]
        # corner coordinates (native + lon/lat), gdalinfo order
        if tr and f["bounds"]:
            W, H = f["levels"][0]["W"], f["levels"][0]["H"]
            a, b_, c, d, e, ff = tr
            px = {"Upper Left": (0, 0), "Lower Left": (0, H),
                  "Upper Right": (W, 0), "Lower Right": (W, H),
                  "Center": (W / 2.0, H / 2.0)}
            t2ll = None
            if epsg and int(epsg) != 4326:
                try:
                    t2ll = Transformer.from_crs(int(epsg), 4326, always_xy=True)
                except Exception:
                    pass
            corners = []
            for name, (x, y) in px.items():
                nx, ny = c + a * x + b_ * y, ff + d * x + e * y
                lon, lat = (nx, ny) if not t2ll else t2ll.transform(nx, ny)
                corners.append({"name": name, "native": [nx, ny],
                                "lonlat": ([lon, lat]
                                           if lon == lon and abs(lon) != float("inf")
                                           else None)})
            m["corners"] = corners
        # sidecar files
        base = f["path"].rsplit(".", 1)[0]
        files = [f["path"]]
        for ext in (".aux.xml", ".ovr", ".msk", ".tfw", ".wld", ".prj"):
            for cand in (f["path"] + ext, base + ext):
                if os.path.isfile(cand) and cand not in files:
                    files.append(cand)
        m["files"] = files
        # per-band info: dtype, colorinterp, approx min/max, block, overviews
        lv0 = f["levels"][0]
        spp = lv0["spp"]
        photometric = lv0.get("photometric", 1)
        gm = m.get("gdal_metadata") or {}
        interp = (["Red", "Green", "Blue", "Alpha"] if photometric == 2
                  else ["Palette"] if photometric == 3 else ["Gray"])
        stats = None
        if f["supported"]:
            try:
                stats = get_stretch(f, list(range(spp)), False)
            except Exception:
                stats = None
        block = ([lv0["tw"], lv0["th"]] if lv0["tiled"]
                 else [lv0["W"], lv0["rps"]])
        bands = []
        for i in range(spp):
            smin = gm.get(f"band {i + 1}: STATISTICS_MINIMUM")
            smax = gm.get(f"band {i + 1}: STATISTICS_MAXIMUM")
            bands.append({
                "band": i + 1,
                "description": (m.get("descriptions") or [None] * spp)[i],
                "dtype": str(np.dtype(lv0["dtype"])).lstrip("<>|"),
                "colorinterp": interp[i] if i < len(interp) else "Undefined",
                "min": (float(smin) if smin else
                        (stats[i][0] if stats else None)),
                "max": (float(smax) if smax else
                        (stats[i][1] if stats else None)),
                "stats_source": ("GDAL metadata" if smin else
                                 "overview scan" if stats else None),
                "nodata": m.get("nodata"),
                "block": block,
                "overviews": len(f["levels"]) - 1,
            })
        m["band_info"] = bands
        return m

    def do_meta(q):
        f = open_file(q1(q, "file"))
        m = dict(f["meta"])
        m["supported"] = f["supported"]
        m["crs_name"] = T._crs_name(m.get("crs"))
        import _raster_common as C
        m["lonlat_bounds"] = C.lonlat_bounds(m.get("bounds"), f["epsg"])
        try:
            m["merc_bbox"] = list(R._merc_bbox(f["bounds"], f["epsg"])) \
                if f["supported"] else None
        except Exception:
            m["merc_bbox"] = None
        if f["supported"]:
            want_rgb = f["levels"][0]["spp"] >= 3
            idx = [0, 1, 2] if want_rgb else [0]
            m["stretch"] = get_stretch(f, idx, True)
        m["levels"] = [[l["W"], l["H"]] for l in f["levels"]]
        try:
            m = _deep_meta(f, m)
        except Exception:
            pass
        return 200, json.dumps(m, default=str).encode(), "application/json"

    def do_hist(q):
        f = open_file(q1(q, "file"))
        if not f["supported"]:
            return 404, b"{}", "application/json"
        want_rgb, idx = render_params(q, f)
        bins = min(max(int(q1(q, "bins", "60")), 4), 200)
        try:
            w, s, e, n = [float(v) for v in q1(q, "bbox", "").split(",")]
        except (ValueError, AttributeError):
            b = R._merc_bbox(f["bounds"], f["epsg"])
            w, s, e, n = b
        # sample the viewport at ~90k cells
        aspect = max((e - w) / max(n - s, 1e-9), 1e-6)
        oh = max(8, int((90000 / aspect) ** 0.5))
        ow = max(8, int(oh * aspect))
        arr = sample_merc_grid(f, idx, w, s, e, n, ow, oh)
        out = {"channels": []}
        for k in range(arr.shape[0]):
            fin = arr[k][np.isfinite(arr[k])]
            ch = {"count": int(fin.size)}
            if fin.size:
                c, edges = np.histogram(fin, bins=bins)
                ch.update({
                    "counts": [int(v) for v in c],
                    "edges": [float(v) for v in edges],
                    "min": float(fin.min()), "max": float(fin.max()),
                    "mean": float(fin.mean()), "std": float(fin.std()),
                    "p2": float(np.percentile(fin, 2)),
                    "p98": float(np.percentile(fin, 98)),
                    "median": float(np.median(fin)),
                })
            out["channels"].append(ch)
        return 200, json.dumps(out).encode(), "application/json"

    def do_value(q):
        f = open_file(q1(q, "file"))
        if not f["supported"]:
            return 404, b"{}", "application/json"
        lon, lat = float(q1(q, "lon")), float(q1(q, "lat"))
        mx = math.radians(lon) * MERC_R
        lat = max(-85.05112878, min(85.05112878, lat))
        my = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * MERC_R
        count = f["levels"][0]["spp"]
        idx = list(range(count))
        eps = 0.5
        arr = sample_merc_grid(f, idx, mx - eps, my - eps, mx + eps, my + eps, 2, 2)
        vals = [None if not np.isfinite(arr[k, 0, 0]) else float(arr[k, 0, 0])
                for k in range(count)]
        return 200, json.dumps({"values": vals}).encode(), "application/json"

    # ---------------- HTTP ----------------
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/ping":
                    code, body, ct = 200, json.dumps(
                        {"ok": True, "version": VERSION}).encode(), "application/json"
                elif u.path == "/quit":
                    self._send(200, b"bye", "text/plain")
                    threading.Thread(target=srv.shutdown, daemon=True).start()
                    return
                elif u.path.startswith("/tile/"):
                    parts = u.path.split("/")   # '', 'tile', z, x, 'y.png'
                    z, x = int(parts[2]), int(parts[3])
                    y = int(parts[4].split(".")[0])
                    code, body, ct = do_tile(q, z, x, y)
                elif u.path == "/meta":
                    code, body, ct = do_meta(q)
                elif u.path == "/hist":
                    code, body, ct = do_hist(q)
                elif u.path == "/value":
                    code, body, ct = do_value(q)
                else:
                    code, body, ct = 404, b"not found", "text/plain"
            except Exception as e:
                import traceback
                traceback.print_exc()
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
    print(f"tile daemon on 127.0.0.1:{port} (v{VERSION})", flush=True)
    srv.serve_forever()


if __name__ == "__main__" and "--serve" in sys.argv:
    import numpy as np  # noqa: F401  (fail fast if venv lacks numpy)
    _serve()

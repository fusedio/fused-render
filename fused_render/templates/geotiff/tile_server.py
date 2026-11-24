"""Persistent GeoTIFF tile daemon for the geotiff preview.

The fused-render runner spawns a fresh subprocess per runPython call
(~700ms fixed overhead), which caps pan/zoom responsiveness. This module is
both:

  1. a runPython entrypoint `main(action="ensure")` — starts (or reuses) a
     long-lived localhost HTTP daemon and returns its port; and
  2. the daemon itself (run as `python tile_server.py --serve`) — keeps
     GDAL datasets open (and GDAL's block and HTTP caches warm) and serves
     256px Web-Mercator XYZ tiles straight to MapLibre.

Endpoints (all GET, CORS *):
  /ping                        -> {"ok": true, "version": ...}
  /meta?file=                  -> header metadata + supported flag + stretch
  /tile/{z}/{x}/{y}.png?file=&mode=&band=&r=&g=&b=&cmap=&robust=&div=
        &stretch=&vlo=&vhi=    -> PNG tile (transparent where no data)
  /hist?file=&bbox=w,s,e,n(3857)&bins=&band=&mode=  -> viewport stats+hist
  /value?file=&lon=&lat=&band= -> raw value under cursor
  /ltile/{z}/{x}/{y}.png?file=&level=N -> PNG tile rendered ONLY from pyramid
        level N (0 = full res) via rasterio — used by the pyramid template

Every file is read through GDAL (rasterio), behind `open_file` +
`read_window`: a local path directly, an `https://` URL (a streamed bucket
object, D1488) through /vsicurl/, where GDAL does the range requests, caching
and merging itself. One reader for every compression, BigTIFF and COG.
Idle shutdown after 30 min. The state file embeds this file's mtime, so
editing the module auto-respawns a fresh daemon on the next ensure().
"""
# The daemon keeps its OWN venv (DAEMON_VENV below), and that is unchanged by the
# move to folder-scoped environments (SPEC PY-16). `main("ensure")` is the only
# thing run_python ever executes here and it is stdlib-only
# (json/os/subprocess/sys/time/urllib): it starts or reuses the daemon and returns
# its port. Every heavy import lives in the `--serve` half, which runs under
# `_daemon_python()` — the self-managed uv venv below, whose DAEMON_DEPS are the
# real declaration.
#
# So `../pyproject.toml` covers the template's ENTRYPOINT (tiff_reader.py) and
# ensure() rides along in that environment; it deliberately does NOT list
# rasterio, because rasterio is only ever imported under `--serve`. Declaring the
# daemon's deps there would download the same packages TWICE — once into the
# project venv that only runs the stdlib ensure() call, and again into
# DAEMON_VENV where they are actually imported.
#
# DAEMON_VENV is also the mechanism that works under BOTH engines: the built-in
# executor ignores the project declaration entirely, so a pyproject could never
# have served the default engine anyway (D174).

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


# platutil is stdlib-only, so the daemon's venv can import it without fused_render.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(_me())), "shared"))
from platutil import venv_python  # noqa: E402


def _version():
    try:
        return str(os.path.getmtime(_me())) + "|" + _daemon_python()
    except OSError:
        return "0"


DAEMON_VENV = os.path.expanduser("~/.cache/fused-render-geotiff-v2/venv")
DAEMON_DEPS = ["numpy", "pyproj", "imagecodecs", "rasterio"]


def _upgrade_deps(vp):
    """Older venvs predate rasterio (needed by /ltile and the /tile fallback)
    — install it in place instead of rebuilding the venv."""
    import shutil
    import subprocess
    try:
        subprocess.run([vp, "-c", "import rasterio"], check=True,
                       capture_output=True, timeout=60)
        return
    except Exception:
        pass
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    if os.path.exists(uv):
        try:
            subprocess.run([uv, "pip", "install", "-p", vp, "rasterio"],
                           check=True, capture_output=True, timeout=300)
        except Exception:
            pass


def _daemon_python():
    """Interpreter for the daemon: a self-managed uv venv (so imagecodecs is
    available even though the app runner falls back to its bundled python),
    else whatever interpreter is running now."""
    vp = venv_python(DAEMON_VENV)
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
            return {"port": st["port"], "token": st.get("token"),
                    "reused": True, "version": version}
        # stale daemon (old version or dead) — ask it to quit, then respawn
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit?t={st.get('token', '')}",
                timeout=1).read()
        except Exception:
            pass
    except (OSError, ValueError):
        pass

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    log = os.path.join(os.path.dirname(STATE), "daemon.log")
    dp = _daemon_python()
    if dp != sys.executable:
        _upgrade_deps(dp)
    # scrub the app's interpreter env — inherited PYTHONPATH/PYTHONHOME makes
    # the venv python import the APP BUNDLE's packages (mixed-install imports
    # fail randomly under concurrency, e.g. bundle rasterio vs venv rasterio)
    denv = {k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "PYTHONHOME")}
    with open(log, "ab") as lf:
        subprocess.Popen([dp, _me(), "--serve"],
                         stdout=lf, stderr=lf, env=denv,
                         start_new_session=True, cwd=os.path.dirname(_me()))
    # wait for the state file to appear with a live port
    for _ in range(100):
        time.sleep(0.05)
        try:
            with open(STATE) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), version):
                return {"port": st["port"], "token": st.get("token"),
                        "reused": False, "version": version}
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
    import secrets
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    # Per-daemon secret. The daemon binds a random loopback port and answers
    # with open CORS so the template's cross-port iframe can read tiles; that
    # alone leaves it readable by any page in the same browser that guesses the
    # port. Requiring this token on every data endpoint closes that gap — the
    # template gets the token from ensure() and threads it in as ?t=. /ping
    # (version only) stays token-free so liveness probes need no secret.
    TOKEN = secrets.token_urlsafe(32)

    sys.path.insert(0, os.path.dirname(_me()))
    import _tiff_core as T
    import tiff_reader as R          # reuse encode_png / _lut / stretch utils

    VERSION = _version()
    last_hit = [time.time()]

    # GDAL's HTTP reader (/vsicurl/) for streamed files: consecutive ranges
    # merged into one request (S3 has no multi-range GET, so GDAL's default
    # of parallel single-range requests stays), a bigger block cache, a few
    # retries. Set before the first open; an explicit environment value wins.
    for k, v in {"GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
                 "GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "0.5",
                 "CPL_VSIL_CURL_CACHE_SIZE": str(256 << 20),
                 "GDAL_CACHEMAX": "512"}.items():
        os.environ.setdefault(k, v)
    if not (os.environ.get("CURL_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")):
        try:
            import certifi
            os.environ["CURL_CA_BUNDLE"] = certifi.where()
        except ImportError:
            pass

    # A streamed file is drawn from its pyramid: the stretch and the
    # zoomed-out tiles read the smallest level whole. A file whose smallest
    # level is bigger than this (no overviews) would read most of the
    # object for one view, so it is refused with a pointer to Download.
    STREAM_MAX_PX = 4096 * 4096

    def is_url(p):
        return isinstance(p, str) and p.startswith(("https://", "http://"))

    def gdal_open(f_or_name, **kw):
        """rasterio.open; a URL opens without listing its "directory" (each
        sibling-file probe would be a request). Only URLs: a local file
        keeps its .ovr / .aux.xml / .tfw sidecars."""
        import rasterio
        name = f_or_name
        if name.startswith("/vsicurl/"):
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
                return rasterio.open(name, **kw)
        return rasterio.open(name, **kw)

    def crs_arg(c):
        """A file's CRS for pyproj: its EPSG code, or its WKT when it has none."""
        return int(c) if isinstance(c, int) or (isinstance(c, str) and c.isdigit()) else c

    # ---------------- file cache: parsed pyramid per path ----------------
    files = {}          # path -> dict(levels, meta, lock, stretch cache)
    files_lock = threading.Lock()

    def open_file(path):
        """The file's handle, cached: GDAL opens a local path directly and a
        URL through /vsicurl/."""
        key = path if is_url(path) else os.path.abspath(os.path.expanduser(path))
        with files_lock:
            f = files.get(key)
        if f is not None:
            return f
        f = _open_gdal(key)
        with files_lock:
            files[key] = f
        return f

    # ---------------- GDAL file handle ----------------
    def _gdal_level(ds):
        from rasterio.enums import ColorInterp as CI
        th, tw = ds.block_shapes[0]
        ci = ds.colorinterp
        photometric = (2 if tuple(ci[:3]) == (CI.red, CI.green, CI.blue)
                       else 3 if ci and ci[0] == CI.palette else 1)
        return {"W": ds.width, "H": ds.height, "spp": ds.count,
                "dtype": np.dtype(ds.dtypes[0]), "planar": 1,
                "tiled": tw < ds.width, "tw": tw, "th": th, "rps": th,
                "photometric": photometric}

    def _remote_size(url):
        """Content-Length of a URL (one HEAD), or None."""
        import urllib.request
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as r:
                n = r.headers.get("Content-Length")
            return int(n) if n else None
        except Exception:  # noqa: BLE001 - size is informational
            return None

    def _crs_label(wkt):
        try:
            from pyproj import CRS
            return CRS.from_user_input(wkt).name
        except Exception:  # noqa: BLE001
            return None

    def _gdal_meta(ds, path, levels):
        """header_meta's shape, from GDAL instead of parsed tags."""
        epsg = wkt = None
        if ds.crs:
            try:
                epsg = ds.crs.to_epsg()
            except Exception:  # noqa: BLE001
                epsg = None
            if not epsg:
                wkt = ds.crs.to_wkt()
        georef = ds.crs is not None and not ds.transform.is_identity
        b = ds.bounds
        tags = ds.tags()
        ims = ds.tags(ns="IMAGE_STRUCTURE")
        lv0 = levels[0]
        gm = {k: v for k, v in tags.items()
              if not k.startswith("TIFFTAG_") and k != "AREA_OR_POINT"}
        for i in range(ds.count):
            for k, v in ds.tags(i + 1).items():
                gm[f"band {i + 1}: {k}"] = v
        comp = ds.compression.value.lower() if ds.compression else "none"
        return {
            "crs": ({"epsg": int(epsg)} if epsg else {"wkt": wkt} if wkt else None),
            "transform": list(ds.transform)[:6] if georef else None,
            "bounds": ({"left": b.left, "right": b.right,
                        "top": b.top, "bottom": b.bottom} if georef else None),
            "area_or_point": {"area": "Area", "point": "Point"}.get(
                (tags.get("AREA_OR_POINT") or "").lower()),
            "citation": _crs_label(wkt) if wkt else None,
            "width": ds.width, "height": ds.height, "count": ds.count,
            "dtype": ds.dtypes[0], "nodata": ds.nodata,
            "file_size": (_remote_size(path) if is_url(path)
                          else os.path.getsize(path)),
            "tiff": {
                "compression": comp,
                "layout": (f"tiled {lv0['tw']}×{lv0['th']}" if lv0["tiled"]
                           else f"stripped ({lv0['rps']} rows/strip)"),
                "photometric": {1: "minisblack", 2: "rgb", 3: "palette"}.get(
                    lv0["photometric"]),
                "predictor": {"2": "horizontal", "3": "float"}.get(
                    ims.get("PREDICTOR")),
                "byte_order": None,
                "overviews": [[l["W"], l["H"]] for l in levels[1:]],
            },
            "tags": {k: tags[t] for k, t in (
                ("description", "TIFFTAG_IMAGEDESCRIPTION"),
                ("software", "TIFFTAG_SOFTWARE"), ("datetime", "TIFFTAG_DATETIME"),
                ("artist", "TIFFTAG_ARTIST"), ("copyright", "TIFFTAG_COPYRIGHT"))
                if tags.get(t)},
            "gdal_metadata": gm,
            "descriptions": [d or f"band {i + 1}"
                             for i, d in enumerate(ds.descriptions)],
        }

    def _open_gdal(path):
        import rasterio
        from rasterio.enums import ColorInterp as CI
        name = "/vsicurl/" + path if is_url(path) else path
        size = {}
        sizer = None
        if is_url(path):  # the HEAD for the size runs beside GDAL's open
            sizer = threading.Thread(
                target=lambda: size.update(n=_remote_size(path)), daemon=True)
            sizer.start()
        with gdal_open(name) as ds:
            levels = [_gdal_level(ds)]
            for i in range(len(ds.overviews(1)) if ds.count else 0):
                with gdal_open(name, overview_level=i) as o:
                    levels.append(_gdal_level(o))
            levels.sort(key=lambda l: -l["W"])
            meta = _gdal_meta(ds, path, levels)
            palette = None
            if ds.count and ds.colorinterp[0] == CI.palette:
                try:
                    cm = ds.colormap(1)
                except ValueError:
                    cm = None
                if cm:
                    palette = np.zeros((max(cm) + 1, 4), "uint8")
                    for k, v in cm.items():
                        palette[k] = v
        if sizer is not None:
            sizer.join(15)
            meta["file_size"] = size.get("n")
        crs = meta.get("crs") or {}
        epsg = crs.get("epsg") or crs.get("wkt")   # what pyproj gets (crs_arg)
        f = {"path": path, "gdal_name": name,
             "levels": levels, "meta": meta, "epsg": epsg, "palette": palette,
             "transform": meta["transform"], "bounds": meta["bounds"],
             "supported": bool(epsg and meta["transform"]),
             "lock": threading.Lock(), "stretch": {}, "stream_error": None,
             "min_zoom": None}
        small = levels[-1]
        if is_url(path) and not f["supported"]:
            f["stream_error"] = ("This TIFF has no map position GDAL can "
                                 "use, so it can't be streamed onto the map. "
                                 "Download it to view it.")
        elif is_url(path) and small["W"] * small["H"] > STREAM_MAX_PX:
            # No overview small enough for a whole view: stream it zoomed in
            # only, from the zoom where a map tile covers ~1024 file pixels.
            try:
                x0, _, x1, _ = R._merc_bbox(f["bounds"], epsg)
                res0 = (x1 - x0) / levels[0]["W"]
                z = math.log2(2 * MERC_MAX / (TILE * 4 * res0))
                f["min_zoom"] = max(0, min(22, math.ceil(z)))
            except Exception:  # noqa: BLE001
                f["supported"] = False
                f["stream_error"] = (
                    f"Too large to stream: this TIFF is {small['W']:,} × "
                    f"{small['H']:,} px with no overviews. Download it to view it.")
        return f

    class ZoomIn(Exception):
        """A read of a zoom-only file (no overviews) wider than one zoomed-in
        view: the page shows the footprint and asks to zoom in instead."""

    # A zoom-only file reads at most this many file pixels per request (a
    # tile at min_zoom covers about 1024 x 1024; reprojection widens it).
    ZOOM_WINDOW_MAX = 3072 * 3072

    # Open GDAL datasets, pooled per (file, level): a rasterio handle is not
    # thread-safe, and each request runs on its own short-lived thread, so a
    # read borrows a handle and returns it. Reads of different tiles run in
    # parallel; GDAL's HTTP cache is process-wide, so they share fetched bytes.
    _ds_pool = {}
    _ds_pool_lock = threading.Lock()

    def _gdal_read_window(f, li, x0, y0, x1, y1, band_idx, out):
        from rasterio.enums import Resampling
        from rasterio.windows import Window
        key = (f["gdal_name"], li)
        with _ds_pool_lock:
            free = _ds_pool.setdefault(key, [])
            ds = free.pop() if free else None
        if ds is None:
            kw = {} if li == 0 else {"overview_level": li - 1}
            ds = gdal_open(f["gdal_name"], **kw)
        try:
            # out may be smaller than the window: GDAL then reads it
            # decimated (nearest), never holding the full-size window
            out[:] = ds.read([b + 1 for b in band_idx],
                             window=Window(x0, y0, x1 - x0, y1 - y0),
                             out_shape=out.shape, resampling=Resampling.nearest)
        except Exception:
            ds.close()
            raise
        with _ds_pool_lock:
            _ds_pool[key].append(ds)

    def read_window(f, li, x0, y0, x1, y1, band_idx, max_px=None):
        """(len(band_idx), h, w) float32 from level li, NaN nodata: the
        window at full size, or, when it holds more than max_px pixels, a
        nearest-neighbour sample of it about max_px big (a zoomed-out view of
        a local file with no overviews stays bounded in memory)."""
        lv = f["levels"][li]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(lv["W"], x1), min(lv["H"], y1)
        w, h = max(0, x1 - x0), max(0, y1 - y0)
        if f.get("min_zoom") is not None and w * h > ZOOM_WINDOW_MAX:
            raise ZoomIn(f"zoom in to z{f['min_zoom']} to read this TIFF")
        if max_px and w * h > max_px:
            k = math.sqrt(max_px / float(w * h))
            w, h = max(1, int(w * k)), max(1, int(h * k))
        out = np.full((len(band_idx), h, w), np.nan, dtype="float32")
        if x1 <= x0 or y1 <= y0:
            return out
        _gdal_read_window(f, li, x0, y0, x1, y1, band_idx, out)
        nod = f["meta"].get("nodata")
        if nod is not None:
            out[out == nod] = np.nan
        return out

    # ---------------- mercator plumbing ----------------
    from pyproj import Transformer
    _tr_cache = {}

    def merc_to_native(epsg):
        if crs_arg(epsg) == 3857:
            return None
        tr = _tr_cache.get(epsg)
        if tr is None:
            tr = Transformer.from_crs(3857, crs_arg(epsg), always_xy=True)
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
        # at most ~4 file pixels per output pixel: a level finer than that
        # (no overview coarse enough) is read decimated
        win = read_window(f, li, x0, y0, x1, y1, band_idx, max_px=4 * ow * oh)
        kx = win.shape[2] / float(x1 - x0)
        ky = win.shape[1] / float(y1 - y0)
        ix = np.clip(((lpx - x0) * kx).astype(np.int32), 0, win.shape[2] - 1)
        iy = np.clip(((lpy - y0) * ky).astype(np.int32), 0, win.shape[1] - 1)
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
        if f.get("min_zoom") is not None:
            # no overview to read whole: estimate from a central 1024² window
            cx, cy = lv["W"] // 2, lv["H"] // 2
            arr = read_window(f, li, cx - 512, cy - 512, cx + 512, cy + 512,
                              band_idx)
        else:
            # smallest overview, sampled down to ~1M cells for the estimate
            arr = read_window(f, li, 0, 0, lv["W"], lv["H"], band_idx,
                              max_px=1_000_000)
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

    _EMPTY_TILE = []

    def empty_tile():
        if not _EMPTY_TILE:
            _EMPTY_TILE.append(R.encode_png(np.zeros((TILE, TILE, 4), "uint8")))
        return 200, _EMPTY_TILE[0], "image/png"

    def do_tile(q, z, x, y):
        try:
            return _tile(q, z, x, y)
        except ZoomIn:
            return empty_tile()
        except Exception as e:  # noqa: BLE001 - say why the tile failed
            # a network error on a streamed file is worth a retry (the next
            # pan does one); anything else is a file this view can't draw
            return (502 if is_url(q1(q, "file")) else 404), str(e).encode(), \
                "text/plain"

    def _tile(q, z, x, y):
        f = open_file(q1(q, "file"))
        if not f["supported"]:
            raise ValueError("no georeference to draw on the map")
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
        elif q1(q, "cmap") == "palette" and f.get("palette") is not None:
            # the file's own colour table (classification rasters)
            pal = f["palette"]
            vals = arr[0]
            ok = np.isfinite(vals) & (vals >= 0) & (vals < len(pal))
            vlo, vhi = q1(q, "vlo", ""), q1(q, "vhi", "")
            if vlo not in ("", None) and vhi not in ("", None):
                ok = ok & (vals >= float(vlo)) & (vals <= float(vhi))
            rgba = pal[np.where(ok, vals, 0).astype("int64")]
            rgba[:, :, 3] = np.where(ok, rgba[:, :, 3], 0)
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
                crs = CRS.from_user_input(crs_arg(epsg))
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
            if epsg and crs_arg(epsg) != 4326:
                try:
                    t2ll = Transformer.from_crs(crs_arg(epsg), 4326, always_xy=True)
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
        # sidecar files (local only: a streamed file's path is its URL,
        # which never goes back to the page)
        files = [] if is_url(f["path"]) else [f["path"]]
        base = f["path"].rsplit(".", 1)[0]
        for ext in (".aux.xml", ".ovr", ".msk", ".tfw", ".wld", ".prj"):
            for cand in (f["path"] + ext, base + ext):
                if files and os.path.isfile(cand) and cand not in files:
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
        m["engine"] = "GDAL, streamed" if is_url(f["path"]) else "GDAL"
        m["stream_error"] = f.get("stream_error")
        m["has_palette"] = f.get("palette") is not None
        m["min_zoom"] = f.get("min_zoom")
        m["crs_name"] = T._crs_name(m.get("crs")) or m.get("citation")
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
            try:
                m["stretch"] = get_stretch(f, idx, True)
            except Exception:  # noqa: BLE001
                # One transient read failure must not 500 /meta — the
                # client would silently drop to the slow fallback engine for
                # the whole session. Tiles recompute the stretch on demand.
                pass
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
        try:
            arr = sample_merc_grid(f, idx, w, s, e, n, ow, oh)
        except ZoomIn:
            return 200, json.dumps({"channels": [], "zoom_in": True}).encode(), \
                "application/json"
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

    # ------------- forced-level tiles (rasterio — any compression) -------
    # /ltile/{z}/{x}/{y}.png?file=&level=N renders ONLY from pyramid level N
    # (0 = full res), reprojected to web mercator. Used by the overview-
    # pyramid template to compare levels honestly on a basemap.
    _lvl_cache = {}
    _lvl_lock = threading.Lock()
    # rasterio dataset handles are NOT thread-safe: concurrent vrt.read() from
    # the threaded server corrupts libtiff state (TIFFReadEncodedTile failures,
    # then a dead process). All reads through cached VRTs go through this lock.
    _rio_read_lock = threading.Lock()

    def _lvl_vrt(file, level):
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.vrt import WarpedVRT
        key = (file, level, os.path.getmtime(file))
        with _lvl_lock:
            if key in _lvl_cache:
                return _lvl_cache[key]
            kw = {} if level <= 0 else {"overview_level": level - 1}
            src = rasterio.open(file, **kw)
            vrt = WarpedVRT(src, crs="EPSG:3857", resampling=Resampling.nearest)
            _lvl_cache[key] = vrt
            return vrt

    def _render_level(file, level, z, x, y, stretch=None, cmap=None):
        from rasterio.enums import Resampling
        from rasterio.windows import Window
        vrt = _lvl_vrt(file, level)
        mx0, my0, mx1, my1 = tile_bbox(z, x, y)
        rgba = np.zeros((TILE, TILE, 4), "uint8")
        # nearest SOURCE pixel per output pixel, computed in source index
        # space: pasting a fractionally-rounded resampled window instead puts
        # pixel edges in different places per tile (non-square pixels when
        # zoomed past the level's resolution)
        T = vrt.transform
        xs = mx0 + (np.arange(TILE) + 0.5) * (mx1 - mx0) / TILE
        ys = my1 + (np.arange(TILE) + 0.5) * (my0 - my1) / TILE
        cols = np.floor((xs - T.c) / T.a).astype("int64")
        rows = np.floor((ys - T.f) / T.e).astype("int64")
        okc = (cols >= 0) & (cols < vrt.width)
        okr = (rows >= 0) & (rows < vrt.height)
        if okc.any() and okr.any():
            c0, c1 = int(cols[okc].min()), int(cols[okc].max()) + 1
            r0, r1 = int(rows[okr].min()), int(rows[okr].max()) + 1
            ow, oh = min(c1 - c0, TILE), min(r1 - r0, TILE)
            win = Window(c0, r0, c1 - c0, r1 - r0)
            n = min(3, vrt.count)
            with _rio_read_lock:
                data = vrt.read(indexes=list(range(1, n + 1)), window=win,
                                out_shape=(n, oh, ow), resampling=Resampling.nearest)
                msk = vrt.read_masks(1, window=win, out_shape=(oh, ow),
                                     resampling=Resampling.nearest)
            ci = np.clip((cols - c0) * ow // (c1 - c0), 0, ow - 1)
            ri = np.clip((rows - r0) * oh // (r1 - r0), 0, oh - 1)
            sub = data[:, ri[:, None], ci[None, :]]
            m2 = msk[ri[:, None], ci[None, :]].copy()
            m2[~okr, :] = 0
            m2[:, ~okc] = 0
            if n == 1 and cmap:
                vals = sub[0].astype("float64")
                lo, hi = stretch[0] if stretch else (
                    np.percentile(vals[m2 > 0], [2, 98]) if (m2 > 0).any()
                    else (0.0, 1.0))
                lut = R._lut(cmap)
                t = np.clip((vals - lo) / max(hi - lo, 1e-9), 0, 1)
                rgba[:, :, :3] = lut[(t * 255).astype("uint8")]
            else:
                if sub.dtype != np.uint8:  # 2–98% stretch for non-8-bit
                    d = sub.astype("float64")
                    good = np.broadcast_to(m2 > 0, d.shape)
                    for k in range(n):
                        if stretch:
                            lo, hi = stretch[k if k < len(stretch) else 0]
                        else:
                            g = d[k][good[k]]
                            lo, hi = (np.percentile(g, [2, 98]) if g.size
                                      else (0.0, 1.0))
                        d[k] = np.clip((d[k] - lo) / max(hi - lo, 1e-9), 0, 1) * 255
                    sub = d.astype("uint8")
                px = np.transpose(sub, (1, 2, 0))
                if n < 3:
                    px = np.repeat(px[:, :, :1], 3, axis=2)
                rgba[:, :, :3] = px
            rgba[:, :, 3] = np.where(m2 > 0, 255, 0)
        return 200, R.encode_png(np.ascontiguousarray(rgba)), "image/png"

    def do_ltile(q, z, x, y):
        if is_url(q1(q, "file")):
            return 404, b"forced-level tiles are local only", "text/plain"
        file = os.path.abspath(os.path.expanduser(q1(q, "file")))
        # global stretch (sampled once, coarsest level) — a per-tile stretch
        # gives every tile its own contrast and the map shows seams
        return _render_level(file, int(q1(q, "level", "0")), z, x, y,
                             stretch=_rio_stretch(file))

    def _rio_meta(file):
        import rasterio
        key = ("meta", file, os.path.getmtime(file))
        with _lvl_lock:
            if key in _lvl_cache:
                return _lvl_cache[key]
        with rasterio.open(file) as src:
            factors = [1] + list(src.overviews(1))
        meta = {"factors": factors, "res0": _lvl_vrt(file, 0).transform.a}
        with _lvl_lock:
            _lvl_cache[key] = meta
        return meta

    def _rio_stretch(file):
        """Global 2–98% stretch sampled once from the coarsest level, so every
        tile shares the same contrast (no per-tile seams)."""
        key = ("stretch", file, os.path.getmtime(file))
        with _lvl_lock:
            if key in _lvl_cache:
                return _lvl_cache[key]
        m = _rio_meta(file)
        vrt = _lvl_vrt(file, len(m["factors"]) - 1)
        n = min(3, vrt.count)
        oh, ow = max(1, min(512, vrt.height)), max(1, min(512, vrt.width))
        with _rio_read_lock:
            data = vrt.read(indexes=list(range(1, n + 1)), out_shape=(n, oh, ow))
            msk = vrt.read_masks(1, out_shape=(oh, ow))
        st = []
        d = data.astype("float64")
        for k in range(n):
            g = d[k][msk > 0]
            lo, hi = (np.percentile(g, [2, 98]) if g.size else (0.0, 1.0))
            st.append((float(lo), float(hi if hi > lo else lo + 1)))
        with _lvl_lock:
            _lvl_cache[key] = st
        return st

    # ---------------- HTTP ----------------
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path != "/ping" and q.get("t", [""])[0] != TOKEN:
                self._send(403, b"forbidden", "text/plain")
                return
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
                elif u.path.startswith("/ltile/"):
                    parts = u.path.split("/")   # '', 'ltile', z, x, 'y.png'
                    z, x = int(parts[2]), int(parts[3])
                    y = int(parts[4].split(".")[0])
                    code, body, ct = do_ltile(q, z, x, y)
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
        json.dump({"port": port, "token": TOKEN,
                   "pid": os.getpid(), "version": VERSION}, fh)

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

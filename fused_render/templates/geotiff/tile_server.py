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
  /ltile/{z}/{x}/{y}.png?file=&level=N -> PNG tile rendered ONLY from pyramid
        level N (0 = full res) via rasterio — used by the pyramid template

/tile tries the native pure-python engine first (uncompressed/deflate TIFFs);
local files it can't read (BigTIFF, user-defined CRS, exotic compression)
fall back to a rasterio WarpedVRT render instead of the runPython image path.
Idle shutdown after 30 min. The state file embeds this file's mtime, so
editing the module auto-respawns a fresh daemon on the next ensure().
"""
# /// script
# dependencies = ["numpy", "pyproj", "imagecodecs", "rasterio"]
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


class _RangeReader:
    """Byte source for a MOUNT-backed TIFF: pooled HTTP range reads against the
    server's /api/fs/raw instead of mmap page faults over the kernel NFS mount.

    The daemon serves tiles from a ThreadingHTTPServer and decodes chunks
    OUTSIDE the per-file lock, so a screenful of cold tiles fans out that many
    simultaneous byte reads. Over the kernel NFS mount those concurrent page
    faults stall its RPC timeout and get the whole mount dropped (measured:
    hard timeouts + 17–30s tail at z13/z14). /api/fs/raw turns each read into an
    ordinary HTTP GET — the server proxies rclone's shared VFS (and 307s cold
    ranged reads to the store for parallel fetches while a whole-file prefetch
    lands), so the same fan-out is merely parallel HTTP, never a mount wedge.
    `url` already carries the server origin + ?path= (built by the template)."""

    def __init__(self, url):
        self.url = url

    def read(self, off, count):
        import urllib.request
        req = urllib.request.Request(
            self.url, headers={"Range": f"bytes={off}-{off + count - 1}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            status = r.status
        if status != 206:
            # Server ignored Range and sent the whole object: slice our window.
            body = body[off:off + count]
        if len(body) != count:
            raise OSError(
                f"range read: wanted {count}B at {off}, got {len(body)}B")
        return body


def _server_url(src, endpoint, path):
    """Server URL built from `src`'s origin and the daemon's own normalized
    `path`. src is trusted only for the origin: its ?path= carries the
    browser's raw file param (possibly ~-prefixed or relative), and the
    server's fs endpoints do no expansion — mixing the two identities would
    judge remote-ness on one path string and range-read another (404s)."""
    import urllib.parse
    u = urllib.parse.urlsplit(src)
    return f"{u.scheme}://{u.netloc}{endpoint}?path=" + urllib.parse.quote(path)


def _stat_remote(src, path):
    """Whether `path` is mount-backed, per the server's /api/fs/stat — the one
    place that knows the mounts (the template passes `src` for every file and
    stays mount-agnostic). True/False from stat, None when it can't be reached
    (caller falls back to mmap and may retry on a later request)."""
    import urllib.request
    try:
        with urllib.request.urlopen(_server_url(src, "/api/fs/stat", path),
                                    timeout=10) as r:
            return bool(json.load(r).get("remote"))
    except Exception:  # noqa: BLE001
        return None


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
    from concurrent.futures import ThreadPoolExecutor
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
    # Shared pool for coalesced warm reads: _warm_chunks runs on every cold
    # multi-miss request; a per-call executor would spawn+join up to 16 OS
    # threads per tile. do_run never re-enters the pool, so no deadlock.
    warm_pool = ThreadPoolExecutor(max_workers=16)

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

    def _attach_reader(f, src, path):
        """Route f's chunk reads over HTTP and drop its mmap reference
        (releases the EBUSY pin once the caller closes it; get_chunk
        tolerates catching the mmap mid-drop). Only swaps dict fields — no
        I/O — so it's safe to call under files_lock. The caller closes the
        returned old buffer outside the lock and spawns _prefetch_overviews
        once f is visible to other threads."""
        # &pooled=1: this reader does one Range GET per ~64KB block over the
        # server's /api/fs/raw. On a cold mount that endpoint 307-redirects to
        # the store's signed URL, and urllib would re-follow it (fresh TLS) on
        # every block. The flag opts this read into the server's pooled proxy
        # so those range reads share keep-alive sockets to the store. Just a
        # query param on the endpoint we already use — the template stays
        # mount-agnostic.
        f["reader"] = _RangeReader(
            _server_url(src, "/api/fs/raw", path) + "&pooled=1")
        old, f["buf"] = f["buf"], None
        return old

    def open_file(path, src=None):
        path = os.path.abspath(os.path.expanduser(path))
        with files_lock:
            f = files.get(path)
        if f is not None:
            # Opened mmap-backed while remote-ness was unknown (stat was
            # unreachable, or a stale client sent no src): re-ask, and for a
            # mount-backed file attach the reader + drop the mmap so chunk
            # reads stop page-faulting the NFS mount. The non-blocking
            # stat_lock keeps a screenful of concurrent requests from each
            # firing its own blocking stat; losers just serve this request
            # from the mmap as before.
            if (src and f["reader"] is None and f.get("remote") is None
                    and f["stat_lock"].acquire(blocking=False)):
                old_buf = None
                try:
                    remote = _stat_remote(src, path)
                    upgrade = False
                    with files_lock:
                        if remote is not None:
                            f["remote"] = remote
                        if remote and f["reader"] is None:
                            old_buf = _attach_reader(f, src, path)
                            upgrade = True
                finally:
                    f["stat_lock"].release()
                if old_buf is not None:
                    try:
                        old_buf.close()
                    except Exception:  # noqa: BLE001
                        pass
                if upgrade:
                    threading.Thread(target=_prefetch_overviews, args=(f,),
                                     daemon=True).start()
            return f
        # No src -> remote-ness UNKNOWN (not False): a later request that does
        # carry src can still stat + upgrade a mount-backed file off the mmap.
        remote = _stat_remote(src, path) if src else None
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
             "meta": meta, "epsg": epsg, "reader": None,
             "transform": meta.get("transform"), "bounds": meta.get("bounds"),
             "supported": bool(supported and epsg and meta.get("transform")),
             "lock": threading.Lock(), "stat_lock": threading.Lock(),
             "chunks": {}, "chunk_order": [], "stretch": {}}
        if remote is not None:
            f["remote"] = remote
        if remote:
            # Mount-backed: header parse above (single-threaded, brief) can ride
            # the mmap, but the CONCURRENT chunk reads must not — route them over
            # HTTP so nothing page-faults the NFS mount.
            old_buf = _attach_reader(f, src, path)
            try:
                if old_buf is not None:
                    old_buf.close()
            except Exception:  # noqa: BLE001
                pass
        with files_lock:
            files[path] = f
        if f["reader"] is not None:
            # Warm the overviews off-thread so /meta returns immediately and the
            # bulk fetch overlaps the client's first tile requests.
            threading.Thread(target=_prefetch_overviews, args=(f,),
                             daemon=True).start()
        return f

    # ---------------- chunk-granular decode with LRU ----------------
    MAX_CACHE_CHUNKS = 4096

    def _decode_chunk(f, li, ci, raw):
        """Decode already-fetched raw bytes of chunk ci -> (h, w, spp) ndarray,
        cache under the file lock, return it. Split out from get_chunk so a
        coalesced multi-chunk range read can decode each member from one buffer."""
        lv = f["levels"][li]
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
            # A prefetch and an on-demand read can race to decode the same
            # chunk; keep the first, or duplicate chunk_order keys would make
            # LRU eviction drop live entries early.
            if (li, ci) in f["chunks"]:
                return f["chunks"][(li, ci)]
            f["chunks"][(li, ci)] = a
            f["chunk_order"].append((li, ci))
            while len(f["chunk_order"]) > MAX_CACHE_CHUNKS:
                old = f["chunk_order"].pop(0)
                f["chunks"].pop(old, None)
        return a

    def get_chunk(f, li, ci):
        """Decode one TIFF tile/strip of level li -> (h, w, spp) ndarray."""
        lv = f["levels"][li]
        with f["lock"]:
            a = f["chunks"].get((li, ci))
            if a is not None:
                return a
        off, cnt = lv["offs"][ci], lv["counts"][ci]
        rd = f["reader"]
        if rd is not None:
            raw = rd.read(off, cnt)
        else:
            try:
                raw = f["buf"][off:off + cnt]
            except (TypeError, ValueError):
                # mmap dropped by a concurrent late-src upgrade; the reader is
                # always attached before the mmap goes away, so it's there now.
                # A torn read is not possible: CPython slices an mmap in one
                # GIL-held C call (validity check + copy), so close() can't
                # interleave mid-copy — a racing slice either completes with
                # the old bytes or lands here.
                raw = f["reader"].read(off, cnt)
        return _decode_chunk(f, li, ci, raw)

    def _nchunks_y(lv):
        if lv["tiled"]:
            return (lv["H"] + lv["th"] - 1) // lv["th"]
        return (lv["H"] + lv["rps"] - 1) // lv["rps"]

    def _warm_chunks(f, li, cis):
        """Prefetch the chunks a window needs (remote reader only). The file
        fetching happens HERE, daemon-side — it is NOT bound by the browser's
        ~6-connection cap on tile requests — so we both fan out AND coalesce.
        Each HTTP range read pays a full ~round-trip of latency; reading chunks
        one at a time (as the assembly loop would) stalls latency×N. COG tiles
        are stored contiguously, so we merge adjacent chunks into a single range
        read (the whole get_stretch overview scan collapses to one GET), decode
        each member from that one buffer, and run the runs concurrently. This is
        what rclone/kernel readahead did for the mmap path — the reason it was
        fast cold — done explicitly over HTTP instead."""
        rd = f["reader"]
        if rd is None:
            return
        lv = f["levels"][li]
        seen, items = set(), []
        for ci in cis:
            if ci in seen:
                continue
            seen.add(ci)
            if (li, ci) not in f["chunks"]:
                items.append((lv["offs"][ci], lv["counts"][ci], ci))
        if len(items) <= 1:
            return  # single miss: the assembly loop's own get_chunk suffices
        items.sort()
        GAP = 512 * 1024   # bridge small inter-tile gaps; don't read big holes
        runs = []          # [start, end, [(ci, off, cnt), ...]]
        for off, cnt, ci in items:
            if runs and off - runs[-1][1] <= GAP:
                runs[-1][1] = max(runs[-1][1], off + cnt)
                runs[-1][2].append((ci, off, cnt))
            else:
                runs.append([off, off + cnt, [(ci, off, cnt)]])

        def do_run(run):
            start, end, members = run
            try:
                raw = rd.read(start, end - start)
                for ci, off, cnt in members:
                    if (li, ci) not in f["chunks"]:
                        _decode_chunk(f, li, ci, raw[off - start:off - start + cnt])
            except Exception:  # noqa: BLE001
                pass  # warming is best-effort; get_chunk refetches per-chunk

        list(warm_pool.map(do_run, runs))

    def _prefetch_overviews(f):
        """Background bulk-fetch of every reduced-resolution level (remote
        reader only), skipping the full-res base. A screenful of tiles arrives
        as that many separate browser requests — the daemon would read one
        chunk per request, latency-bound, never seeing them as a batch. Here,
        server-side and off the browser's connection budget, we warm each whole
        overview level up front; _warm_chunks coalesces its contiguous chunks
        into ~one bandwidth-bound GET, so pan/zoom then hits the cache. The base
        level stays on-demand — deep zoom only ever views a small part of it."""
        for li in range(1, len(f["levels"])):
            try:
                _warm_chunks(f, li, list(range(len(f["levels"][li]["offs"]))))
            except Exception:  # noqa: BLE001
                pass  # best-effort warmth; real reads still fall back to get_chunk

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
            _warm_chunks(f, li, [
                (bk * per_plane if planar == 2 else 0) + ty * across + tx
                for ty in range(y0 // th, (y1 - 1) // th + 1)
                for tx in range(x0 // tw, (x1 - 1) // tw + 1)
                for bk in band_idx])
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
            _warm_chunks(f, li, [
                (bk * nsy if planar == 2 else 0) + si
                for si in range(y0 // rps, (y1 - 1) // rps + 1)
                for bk in band_idx])
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
        # native pure-python engine first; LOCAL files it can't read (BigTIFF,
        # user-defined CRS, exotic compression) fall back to rasterio below.
        # Mount-backed files keep the old 404 (rasterio can't range-read them
        # through /api/fs/raw) so the page falls back to tiff_reader.py.
        try:
            return _tile_native(q, z, x, y)
        except Exception:
            f = os.path.abspath(os.path.expanduser(q1(q, "file") or ""))
            if not os.path.exists(f):
                return 404, b"unsupported", "text/plain"
            return _tile_rio(q, z, x, y)

    def _tile_native(q, z, x, y):
        f = open_file(q1(q, "file"), q1(q, "src"))
        if not f["supported"]:
            raise ValueError("unsupported by native engine")
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
        f = open_file(q1(q, "file"), q1(q, "src"))
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
            try:
                m["stretch"] = get_stretch(f, idx, True)
            except Exception:  # noqa: BLE001
                # One transient remote-read failure must not 500 /meta — the
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
        f = open_file(q1(q, "file"), q1(q, "src"))
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
        f = open_file(q1(q, "file"), q1(q, "src"))
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

    def _tile_rio(q, z, x, y):
        """Rasterio fallback for local files the native engine can't read:
        picks the overview level a COG reader would and renders through the
        WarpedVRT."""
        file = os.path.abspath(os.path.expanduser(q1(q, "file")))
        m = _rio_meta(file)
        mx0, my0, mx1, my1 = tile_bbox(z, x, y)
        target = (mx1 - mx0) / TILE
        level = 0
        for i, fac in enumerate(m["factors"]):
            if m["res0"] * fac <= target:
                level = i
        return _render_level(file, level, z, x, y, stretch=_rio_stretch(file),
                             cmap=q1(q, "cmap", "viridis"))

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

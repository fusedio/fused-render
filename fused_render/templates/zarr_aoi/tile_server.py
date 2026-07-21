"""Hosted-Zarr AOI streaming daemon.

Preview a LARGE hosted Zarr store (terabytes, e.g. on Source Coop / S3)
without downloading it: every map tile / histogram / pixel probe reads ONLY
the chunk byte-ranges that intersect the requested AOI, via zarr-python 3
(v2+v3 stores, sharding, zstd). All store I/O goes through a counting +
caching wrapper so the UI can show exactly how many requests / bytes were
streamed versus the logical size of the dataset.

Same daemon architecture as zarr/grid_tile_server.py (the app runner costs
~700ms per runPython call, so a long-lived localhost daemon serves MapLibre
directly), but nothing is ever loaded fully into memory — the existing grid
daemon loads one whole 2D slice, which is impossible here (WSF level 0 is
1.5M x 4M px = 6 TB).

Paths under ~/.fused-render/mounts/<name>/ are resolved back to their remote
via mounts.json + rclone.conf and read DIRECTLY over S3/HTTP (not through the
rclone FUSE mount) so byte counts are exact. Plain local stores and s3:// or
https:// URLs also work.

Endpoints (GET, CORS *):
  /ping /quit
  /meta?file=&var=&index=      -> source, levels, chunk/shard/codec layout,
                                  logical size, bounds, sample stats
  /tile/{z}/{x}/{y}.png?file=&var=&index=&cmap=&stretch=&nofill=
  /hist?file=&var=&index=&bbox=w,s,e,n(3857)&bins=
  /probe?file=&var=&index=&lon=&lat=   -> native-res pixel value + I/O cost
  /stats?file=[&reset=1]       -> live counters + recent op log
"""
# /// script
# dependencies = ["numpy", "zarr>=3.0.8", "s3fs", "gcsfs", "crc32c"]
# ///

import hashlib
import json
import math
import os
import re
import sys
import threading
import time

STATE = os.path.expanduser("~/.cache/fused-render-zarraoi/daemon.json")
DAEMON_VENV = os.path.expanduser("~/.cache/fused-render-zarraoi/venv")
DAEMON_DEPS = ["numpy", "zarr>=3.0.8", "s3fs", "gcsfs", "crc32c"]
IDLE_EXIT_S = 30 * 60
TILE = 256
MERC_R = 6378137.0
MERC_MAX = math.pi * MERC_R
MAX_LAT = 85.05112878
CACHE_CAP = 1024 * 1024 * 1024     # LRU byte-cache over store reads (chunky
                                   # stores like CMIP6 have ~92 MB chunks)
WINDOW_CAP = 2048 * 2048           # max cells fetched per tile/hist window

SPATIAL_Y = {"lat", "latitude", "y", "yc", "rlat", "nav_lat"}
SPATIAL_X = {"lon", "longitude", "x", "xc", "rlon", "nav_lon"}


def _me():
    if "__file__" in globals():
        return os.path.abspath(__file__)
    return os.path.join(os.path.abspath(sys.path[0]), "tile_server.py")


def _home_dir():
    """Branch-aware ~/.fused-render, mirroring fused_render.shell.storage.home_dir
    + _branch.branch_dir. The daemon runs in its own venv with no fused_render,
    so the resolution is inlined; main() passes FUSED_RENDER_HOME and
    FUSED_RENDER_BRANCH through the daemon's env, so a per-branch dev server
    (state under ~/.fused-render/branches/<ref>/) is detected as a mount, not
    misread as a plain local path. Keep the sanitize rule in lockstep with
    _branch.sanitize (lowercase, collapse non-[a-z0-9] runs to '-', trim,
    truncate to 12; main/master/head -> baseline)."""
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    raw = os.environ.get("FUSED_RENDER_BRANCH", "")
    ref = ""
    if raw and raw.lower() not in ("main", "master", "head"):
        ref = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:12].rstrip("-")
    return os.path.join(base, "branches", ref) if ref else base


def _daemon_python():
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
                           check=True, capture_output=True, timeout=600)
            return vp
        except Exception:
            import shutil as _sh
            _sh.rmtree(DAEMON_VENV, ignore_errors=True)
    return sys.executable


def _version():
    try:
        h = hashlib.sha256(open(_me(), "rb").read()).hexdigest()[:12]
    except OSError:
        h = "0"
    return h + "|" + _daemon_python()


def _alive(port, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version
    except Exception:
        return False


def main(action: str = "ensure"):
    import subprocess
    version = _version()
    try:
        with open(STATE) as f:
            st = json.load(f)
        if _alive(st.get("port"), version):
            return {"port": st["port"], "token": st.get("token"), "reused": True}
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
    env = dict(os.environ)
    if "FUSED_RENDER_ORIGIN" not in env:
        # The daemon reads store bytes through the server's /api/fs/raw
        # (resolve_source), so it needs the server's origin. main() runs
        # inside the server, where the port module is importable; the
        # daemon's own venv has no fused_render, so pass it via env. The
        # 1777 fallback matches _branch._BASE_PORT (baseline, no branch
        # isolation).
        try:
            from fused_render._branch import branch_port
            env["FUSED_RENDER_ORIGIN"] = f"http://127.0.0.1:{branch_port()}"
        except ImportError:
            pass
    with open(log, "ab") as lf:
        subprocess.Popen([_daemon_python(), _me(), "--serve"],
                         stdout=lf, stderr=lf, env=env,
                         start_new_session=True, cwd=os.path.dirname(_me()))
    for _ in range(600):               # venv build on first run can take a while
        time.sleep(0.1)
        try:
            with open(STATE) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), version):
                return {"port": st["port"], "token": st.get("token"),
                        "reused": False}
        except (OSError, ValueError):
            continue
    return {"error": f"zarr AOI daemon did not start — see {log}"}


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass


# ================================================================ daemon
def _serve():
    import numpy as np
    import secrets
    import zarr
    from collections import OrderedDict, deque
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    from zarr.storage import WrapperStore

    # Per-daemon secret required on every data endpoint (except /ping), threaded
    # in by the template as ?t=; see geotiff/tile_server.py for the rationale.
    TOKEN = secrets.token_urlsafe(32)

    VERSION = _version()
    last_hit = [time.time()]

    # ---------------- counting + caching store ----------------
    class MeterStore(WrapperStore):
        """Counts every store read and serves repeats from an LRU byte cache,
        so the UI can report exactly what was streamed from the network."""

        def __init__(self, store, meter=None):
            super().__init__(store)
            self.meter = meter if meter is not None else new_meter()

        def with_read_only(self, read_only=False):
            # keep the SAME meter when zarr clones the store read-only
            return MeterStore(self._store.with_read_only(read_only), self.meter)

        def _cached(self, key, byte_range):
            ck = (key, repr(byte_range))
            m = self.meter
            with m["lock"]:
                hit = m["cache"].get(ck)
                if hit is not None:
                    m["cache"].move_to_end(ck)
                    m["cache_hits"] += 1
                    m["cache_saved"] += len(hit)
            return ck, hit

        def _cache_put(self, ck, data):
            m = self.meter
            with m["lock"]:
                m["cache"][ck] = data
                m["cache_bytes"] += len(data)
                while m["cache_bytes"] > CACHE_CAP and m["cache"]:
                    _, old = m["cache"].popitem(last=False)
                    m["cache_bytes"] -= len(old)

        async def get(self, key, prototype, byte_range=None):
            ck, hit = self._cached(key, byte_range)
            if hit is not None:
                return prototype.buffer.from_bytes(hit)
            m = self.meter
            t0 = time.perf_counter()
            out = await self._store_get(key, prototype, byte_range)
            ms = (time.perf_counter() - t0) * 1000
            n = len(out) if out is not None else 0
            with m["lock"]:
                m["requests"] += 1
                m["net_bytes"] += n
                m["net_ms"] += ms
                if out is None:
                    m["missing"] += 1
            if out is not None:
                self._cache_put(ck, out.to_bytes())
            return out

        async def _store_get(self, key, prototype, byte_range):
            # big whole-chunk objects: fetch with parallel range requests —
            # single-stream S3 GETs crawl, and chunky stores (e.g. CMIP6
            # 90 MB chunks) make one slice read = one whole object
            if byte_range is None:
                try:
                    import asyncio
                    fs = getattr(self._store, "fs", None)
                    root = getattr(self._store, "path", None)
                    if fs is not None and root:
                        full = f"{root}/{key}"
                        size = int((await fs._info(full)).get("size") or 0)
                        if size > 16 * 1024 * 1024:
                            n = 8
                            step = (size + n - 1) // n
                            parts = await asyncio.gather(*(
                                fs._cat_file(full, start=i * step,
                                             end=min(size, (i + 1) * step))
                                for i in range(n)))
                            with self.meter["lock"]:
                                self.meter["requests"] += n - 1
                            return prototype.buffer.from_bytes(b"".join(parts))
                except FileNotFoundError:
                    return None
                except Exception:
                    pass    # fall through to the plain single GET
            # suffix reads (shard indexes) on an http store: fsspec's
            # HTTPFileSystem emulates "last N bytes" with a HEAD size probe
            # first — one extra remote round trip per shard object. HTTP
            # servers (/api/fs/raw, S3 behind its redirect) accept the native
            # `Range: bytes=-N` form, so send it directly.
            try:
                from zarr.abc.store import SuffixByteRequest
                fs = getattr(self._store, "fs", None)
                root = getattr(self._store, "path", None)
                proto = getattr(fs, "protocol", None)
                proto = proto if isinstance(proto, (tuple, list)) else (proto,)
                if (isinstance(byte_range, SuffixByteRequest) and root
                        and "http" in proto):
                    n = int(byte_range.suffix)
                    session = await fs.set_session()
                    async with session.get(
                            f"{root}/{key}",
                            headers={"Range": f"bytes=-{n}"}) as r:
                        if r.status == 404:
                            return None
                        r.raise_for_status()
                        data = await r.read()
                    if len(data) > n:   # server ignored the Range header
                        data = data[-n:]
                    return prototype.buffer.from_bytes(data)
            except Exception:
                pass    # fall through to fsspec's emulated suffix read
            return await super().get(key, prototype, byte_range)

        async def get_partial_values(self, prototype, key_ranges):
            # route through .get so every range is counted + cached
            import asyncio
            return await asyncio.gather(
                *(self.get(k, prototype, r) for k, r in key_ranges))

        async def list_dir(self, prefix):
            with self.meter["lock"]:
                self.meter["lists"] += 1
            async for k in super().list_dir(prefix):
                yield k

    def new_meter():
        return {"lock": threading.Lock(), "requests": 0, "net_bytes": 0,
                "net_ms": 0.0, "missing": 0, "lists": 0,
                "cache": OrderedDict(), "cache_bytes": 0,
                "cache_hits": 0, "cache_saved": 0,
                "ops": deque(maxlen=40), "opened": time.time()}

    # ---------------- source resolution ----------------
    def rclone_conf():
        out, sec = {}, None
        for p in (os.path.expanduser("~/.config/rclone/rclone.conf"),):
            try:
                for ln in open(p):
                    ln = ln.strip()
                    if ln.startswith("[") and ln.endswith("]"):
                        sec = ln[1:-1]
                        out[sec] = {}
                    elif "=" in ln and sec:
                        k, _, v = ln.partition("=")
                        out[sec][k.strip()] = v.strip()
            except OSError:
                pass
        return out

    def resolve_source(path):
        """path -> dict(kind, url, storage_options, label)"""
        # big parallel chunk fetches must not starve small metadata reads
        S3_POOL = {"config_kwargs": {"max_pool_connections": 48}}
        if path.startswith("s3://"):
            return {"kind": "s3", "url": path,
                    "storage_options": {"anon": True, **S3_POOL},
                    "label": path + " (anonymous S3)"}
        if path.startswith(("http://", "https://")):
            return {"kind": "http", "url": path, "storage_options": {},
                    "label": path}
        path = os.path.abspath(os.path.expanduser(path))
        mroot = os.path.join(_home_dir(), "mounts") + os.sep
        if path.startswith(mroot):
            # Default transport: the server's own ranged-read API. The server
            # decides how the bytes move (rclone-serve proxy, presigned 307,
            # local file) and holds the credentials, so private buckets work
            # and this template never has to know what a mount is.
            # ZARRAOI_TRANSPORT=s3 keeps the old rclone.conf back-resolution
            # (anonymous buckets only) for A/B comparison.
            if os.environ.get("ZARRAOI_TRANSPORT", "raw") != "s3":
                from urllib.parse import quote
                origin = os.environ.get("FUSED_RENDER_ORIGIN",
                                        "http://127.0.0.1:1777")
                return {"kind": "http",
                        "url": origin + "/api/fs/raw?path="
                        + quote(path, safe="/"),
                        "storage_options": {},
                        "label": path + " (server raw API)"}
            rel = path[len(mroot):]
            name, _, rest = rel.partition(os.sep)
            try:
                mounts = json.load(open(os.path.join(
                    _home_dir(), "mounts.json")))
            except (OSError, ValueError):
                mounts = []
            ent = next((m for m in mounts if m.get("name") == name), None)
            if ent and ":" in ent.get("remote", ""):
                rname, _, rpath = ent["remote"].partition(":")
                key = "/".join(s for s in (rpath.strip("/"), rest.replace(os.sep, "/"))
                               if s)
                cfg = rclone_conf().get(rname, {})
                if cfg.get("type") == "s3":
                    so = dict(S3_POOL)
                    anon = cfg.get("env_auth", "false") != "true" and \
                        not cfg.get("access_key_id")
                    if anon:
                        so["anon"] = True
                    ck = {}
                    if cfg.get("region"):
                        ck["region_name"] = cfg["region"]
                    if cfg.get("endpoint"):
                        so["endpoint_url"] = cfg["endpoint"]
                    if ck:
                        so["client_kwargs"] = ck
                    return {"kind": "s3", "url": "s3://" + key,
                            "storage_options": so,
                            "label": f"mount '{name}' → s3://{key}"
                                     + (" (anonymous)" if anon else "")}
                elif cfg.get("type") == "google cloud storage":
                    # GCS analog of the s3 branch: gcsfs takes token="anon" for
                    # anonymous public buckets, and needs no region/endpoint.
                    anon = cfg.get("anonymous") == "true"
                    return {"kind": "gcs", "url": "gcs://" + key,
                            "storage_options": {"token": "anon"} if anon else {},
                            "label": f"mount '{name}' → gcs://{key}"
                                     + (" (anonymous)" if anon else "")}
        return {"kind": "local", "url": path, "label": path + " (local)"}

    # ---------------- dataset open + discovery ----------------
    datasets = {}
    ds_lock = threading.Lock()

    def dims_of(arr):
        md = arr.metadata.to_dict()
        d = md.get("dimension_names")
        if not d:
            d = dict(arr.attrs).get("_ARRAY_DIMENSIONS")
        return [str(x) for x in d] if d else [f"dim{i}" for i in range(arr.ndim)]

    def chunk_info(arr):
        md = arr.metadata.to_dict()
        info = {"chunks": None, "inner": None, "codecs": []}
        if md.get("zarr_format") == 3:
            info["chunks"] = list(md["chunk_grid"]["configuration"]["chunk_shape"])
            for c in md.get("codecs", []):
                if c.get("name") == "sharding_indexed":
                    cfg = c["configuration"]
                    info["inner"] = list(cfg["chunk_shape"])
                    info["codecs"] += [cc.get("name") for cc in cfg.get("codecs", [])
                                       if cc.get("name") != "bytes"]
                elif c.get("name") != "bytes":
                    info["codecs"].append(c.get("name"))
        else:
            info["chunks"] = list(md.get("chunks") or [])
            comp = md.get("compressor")
            if comp:
                info["codecs"] = [comp.get("id", "?")]
        return info

    def open_dataset(path):
        with ds_lock:
            ds = datasets.get(path)
        if ds is not None:
            return ds
        src = resolve_source(path)
        meter = new_meter()
        if src["kind"] == "local":
            inner = zarr.storage.LocalStore(src["url"], read_only=True)
        else:
            inner = zarr.storage.FsspecStore.from_url(
                src["url"], read_only=True,
                storage_options=src["storage_options"])
        store = MeterStore(inner, meter)
        root = zarr.open_group(store=store, mode="r")
        attrs = root.attrs.asdict()

        levels = []      # fine -> coarse: {asset, res, transform, shape}
        var_names = []
        ms = attrs.get("multiscales")
        if isinstance(ms, dict) and ms.get("layout"):
            for ent in ms["layout"]:
                tr = ent.get("spatial:transform")
                sc = (ent.get("transform") or {}).get("scale", [1.0, 1.0])
                if tr is None and levels and levels[0]["transform"]:
                    t0, s0 = levels[0]["transform"], levels[0]["scale"]
                    tr = [t0[0] * sc[1] / s0[1], 0.0, t0[2],
                          0.0, t0[4] * sc[0] / s0[0], t0[5]]
                levels.append({"asset": str(ent["asset"]),
                               "shape": [int(s) for s in ent["spatial:shape"]],
                               "transform": tr, "scale": sc})
            g0 = root[levels[0]["asset"]]
            g0_arrs = {k: a for k, a in g0.arrays() if a.ndim >= 2}
            var_names = list(g0_arrs)
            default_var = max(var_names, key=lambda n: (
                g0_arrs[n].dtype.kind == "f", g0_arrs[n].ndim,
                int(np.prod(g0_arrs[n].shape)))) if var_names else None
        else:
            arrs = {k: a for k, a in root.arrays()}
            var_names = [k for k, a in arrs.items() if a.ndim >= 2
                         and not k.lower().endswith(("_bnds", "_bounds", "bnds"))]
            if var_names:
                def score(n):
                    v = arrs[n]
                    return (v.dtype.kind == "f", v.ndim, int(np.prod(v.shape)))
                main = default_var = max(var_names, key=score)
                a = arrs[main]
                dims = dims_of(a)
                # keep only variables on the same spatial grid (drops aux arrays)
                var_names = [n for n in var_names
                             if dims_of(arrs[n])[-2:] == dims[-2:]]
                H, W = a.shape[-2], a.shape[-1]
                tr = None
                ydim, xdim = dims[-2] if len(dims) >= 2 else None, \
                    dims[-1] if dims else None
                try:
                    ya, xa = root[ydim], root[xdim]
                    y0, y1 = float(ya[0]), float(ya[-1])
                    x0, x1 = float(xa[0]), float(xa[-1])
                    ry = (y1 - y0) / max(H - 1, 1)
                    rx = (x1 - x0) / max(W - 1, 1)
                    tr = [rx, 0.0, x0 - rx / 2, 0.0, ry, y0 - ry / 2]
                    if not (-90.5 <= min(y0, y1) and max(y0, y1) <= 90.5
                            and -360.5 <= min(x0, x1) and max(x0, x1) <= 360.5):
                        tr = None
                except (KeyError, TypeError, ValueError):
                    tr = None
                levels.append({"asset": "", "shape": [H, W],
                               "transform": tr, "scale": [1.0, 1.0]})

        if not levels or not var_names:
            raise RuntimeError("no 2D+ arrays found in store")
        geographic = bool(levels[0]["transform"])

        ds = {"path": path, "src": src, "store": store, "root": root,
              "meter": meter, "attrs": attrs, "levels": levels,
              "vars": sorted(var_names), "default_var": default_var,
              "arrays": {}, "stats": {},
              "read_lock": threading.Lock(), "geographic": geographic}
        with ds_lock:
            datasets[path] = ds
            while len(datasets) > 4:
                datasets.pop(next(iter(datasets)))
        return ds

    def get_array(ds, level_i, var):
        key = (level_i, var)
        a = ds["arrays"].get(key)
        if a is None:
            asset = ds["levels"][level_i]["asset"]
            a = ds["root"][f"{asset}/{var}" if asset else var]
            ds["arrays"][key] = a
        return a

    # ---------------- metered reads + op log ----------------
    def metered(ds, kind, fn, **log):
        """Exact per-op counter deltas need serialized reads — but a cold
        multi-second chunk fetch must not block cache-served tiles. If the
        lock is busy, run anyway and mark the op's attribution approximate."""
        m = ds["meter"]
        got = ds["read_lock"].acquire(timeout=0.25)
        try:
            with m["lock"]:
                r0, b0, h0 = m["requests"], m["net_bytes"], m["cache_hits"]
            t0 = time.perf_counter()
            out = fn()
            ms = (time.perf_counter() - t0) * 1000
            with m["lock"]:
                ent = {"t": time.time(), "kind": kind, "ms": round(ms, 1),
                       "requests": m["requests"] - r0,
                       "net_bytes": m["net_bytes"] - b0,
                       "cache_hits": m["cache_hits"] - h0, **log}
                if not got:
                    ent["approx"] = True     # ran concurrently with another op
                m["ops"].appendleft(ent)
        finally:
            if got:
                ds["read_lock"].release()
        return out, ent

    def read_window(ds, level_i, var, index, r0, r1, c0, c1):
        a = get_array(ds, level_i, var)
        nd = a.ndim
        # the UI slider drives the FIRST extra dim; any further dims pin to 0
        pre = tuple(min(max(int(index), 0), a.shape[k] - 1) if k == 0 else 0
                    for k in range(nd - 2))
        return np.asarray(a[pre + (slice(r0, r1), slice(c0, c1))])

    def var_decode(ds, level_i, var):
        """CF packing info: scale_factor / add_offset / fill value."""
        key = ("dec", level_i, var)
        c = ds["arrays"].get(key)
        if c is None:
            a = get_array(ds, level_i, var)
            at = dict(a.attrs)
            fv = a.fill_value
            for k in ("_FillValue", "missing_value"):
                if k in at:
                    try:
                        fv = float(np.asarray(at[k]).ravel()[0])
                    except (TypeError, ValueError):
                        pass

            def num(k):
                v = at.get(k)
                if v is None:
                    return None
                try:
                    return float(np.asarray(v).ravel()[0])
                except (TypeError, ValueError):
                    return None
            c = {"sf": num("scale_factor"), "ao": num("add_offset"),
                 "fill": None if fv is None or (isinstance(fv, float)
                                                and math.isnan(fv))
                 else float(fv),
                 "units": str(at.get("units", ""))}
            c["packed"] = c["sf"] is not None or c["ao"] is not None
            ds["arrays"][key] = c
        return c

    def decode_vals(data, dec):
        """-> (float64 vals with NaN fill if CF-packed, fill for alpha-mask)."""
        if dec["packed"]:
            v = data.astype("float64")
            if dec["fill"] is not None:
                v = np.where(v == dec["fill"], np.nan, v)
            return v * (dec["sf"] or 1.0) + (dec["ao"] or 0.0), None
        return data, dec["fill"]

    # ---------------- geo helpers ----------------
    def merc_to_lonlat(mx, my):
        return (np.degrees(np.asarray(mx) / MERC_R),
                np.degrees(2 * np.arctan(np.exp(np.asarray(my) / MERC_R)) - np.pi / 2))

    def is_wrap360(lv):
        a, _, west, _, _, _ = lv["transform"]
        return west + lv["shape"][1] * a > 180.5

    def norm_lon(lv, lon):
        """Map lon(s) into the grid's own longitude frame (0-360 grids)."""
        if is_wrap360(lv):
            west = lv["transform"][2]
            return np.mod(np.asarray(lon, dtype="float64") - west, 360.0) + west
        return np.asarray(lon, dtype="float64")

    def lonlat_bounds(lv):
        a, _, west, _, e, north = lv["transform"]
        H, W = lv["shape"]
        south = north + H * e
        east = west + W * a
        if is_wrap360(lv):
            west, east = -180.0, 180.0
        return {"west": max(-180.0, west),
                "south": max(-90.0, min(north, south)),
                "east": min(180.0, east),
                "north": min(90.0, max(north, south))}

    def pick_level(ds, deg_per_px):
        cands = [(i, abs(lv["transform"][0])) for i, lv in enumerate(ds["levels"])
                 if lv["transform"]]
        ok = [(i, r) for i, r in cands if r <= deg_per_px * 1.4]
        if ok:
            return max(ok, key=lambda t: t[1])[0]
        return min(cands, key=lambda t: t[1])[0]

    def window_of(lv, lon0, lat0, lon1, lat1, pad=1):
        a, _, west, _, e, north = lv["transform"]
        H, W = lv["shape"]
        rows = sorted(((lat1 - north) / e, (lat0 - north) / e))
        cols = sorted(((lon0 - west) / a, (lon1 - west) / a))
        r0 = max(0, int(math.floor(rows[0])) - pad)
        r1 = min(H, int(math.ceil(rows[1])) + pad)
        c0 = max(0, int(math.floor(cols[0])) - pad)
        c1 = min(W, int(math.ceil(cols[1])) + pad)
        return r0, r1, c0, c1

    # ---------------- stats sample ----------------
    def sample_stats(ds, var, index):
        # keyed by var only: one sample defines the stretch for ALL indexes —
        # keeps colors comparable across time steps and avoids re-reading a
        # whole (possibly huge) chunk on every slider move
        key = var
        s = ds["stats"].get(key)
        if s is not None:
            return s
        coarse = len(ds["levels"]) - 1
        lv = ds["levels"][coarse]
        H, W = lv["shape"]
        if H * W <= 2_500_000:
            r0, r1, c0, c1 = 0, H, 0, W
        else:                       # single-level giant store: central window
            r0 = max(0, H // 2 - 512); r1 = min(H, H // 2 + 512)
            c0 = max(0, W // 2 - 512); c1 = min(W, W // 2 + 512)
        data, _ = metered(ds, "stats-sample",
                          lambda: read_window(ds, coarse, var, index,
                                              r0, r1, c0, c1),
                          level=coarse, window=[r1 - r0, c1 - c0])
        dec = var_decode(ds, coarse, var)
        vals, fill = decode_vals(data, dec)
        vals = vals.astype("float64")
        if fill is not None:
            vals = np.where(vals == fill, np.nan, vals)
        fin = vals[np.isfinite(vals)]
        s = {"count": int(fin.size), "fill_value": dec["fill"],
             "packed": dec["packed"], "units": dec["units"],
             "sampled_level": coarse}
        if fin.size:
            s.update({"min": float(fin.min()), "max": float(fin.max()),
                      "mean": float(fin.mean()),
                      "p2": float(np.percentile(fin, 2)),
                      "p98": float(np.percentile(fin, 98))})
        ds["stats"][key] = s
        return s

    # ---------------- colormaps + PNG ----------------
    CMAPS = {
        "viridis": ["440154", "472d7b", "3b528b", "2c728e", "21918c", "28ae80", "5ec962", "addc30", "fde725"],
        "magma":   ["000004", "1c1044", "4f127b", "812581", "b5367a", "e55064", "fb8761", "fec287", "fcfdbf"],
        "turbo":   ["30123b", "4145ab", "4675ed", "39a2fc", "1bcfd4", "24eca6", "61fc6c", "a4fc3b", "d1e834",
                    "f3c63a", "fe9b2d", "f36315", "d93806", "b11901", "7a0402"],
        "rdbu":    ["053061", "2166ac", "4393c3", "92c5de", "d1e5f0", "f7f7f7", "fddbc7", "f4a582", "d6604d",
                    "b2182b", "67001f"],
        "grays":   ["111111", "ffffff"],
    }

    def lut(name):
        stops = np.array([[int(h[i:i + 2], 16) for i in (0, 2, 4)]
                          for h in CMAPS.get(name, CMAPS["viridis"])], dtype="float64")
        x = np.linspace(0, len(stops) - 1, 256)
        i = np.clip(x.astype(int), 0, len(stops) - 2)
        f = (x - i)[:, None]
        return (stops[i] * (1 - f) + stops[i + 1] * f).round().astype("uint8")

    def encode_png(rgba):
        import struct
        import zlib
        h, w = rgba.shape[:2]
        rows = np.zeros((h, 1 + w * 4), dtype=np.uint8)
        rows[:, 1:] = rgba.reshape(h, w * 4)
        comp = zlib.compress(rows.tobytes(), 1)

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data +
                    struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
                chunk(b"IDAT", comp) + chunk(b"IEND", b""))

    EMPTY_PNG = None
    PLACEHOLDER_PNG = None

    def empty_png():
        nonlocal EMPTY_PNG
        if EMPTY_PNG is None:
            EMPTY_PNG = encode_png(np.zeros((TILE, TILE, 4), dtype=np.uint8))
        return EMPTY_PNG

    def placeholder_png():
        nonlocal PLACEHOLDER_PNG
        if PLACEHOLDER_PNG is None:
            rgba = np.zeros((TILE, TILE, 4), dtype=np.uint8)
            yy, xx = np.mgrid[0:TILE, 0:TILE]
            band = ((yy + xx) // 10) % 2 == 0
            rgba[band] = (255, 207, 112, 70)
            PLACEHOLDER_PNG = encode_png(rgba)
        return PLACEHOLDER_PNG

    def colorize(q, ds, var, index, vals, fill):
        s = sample_stats(ds, var, index)
        st = q1(q, "stretch", "")
        try:
            lo, hi = [float(x) for x in st.split(",")]
        except (ValueError, AttributeError):
            lo, hi = s.get("p2", 0.0), s.get("p98", 1.0)
        if hi <= lo:
            lo = hi - 1.0     # single-valued data -> bright end of the cmap
        L = lut(q1(q, "cmap", "viridis"))
        v = vals.astype("float64")
        alpha = np.isfinite(v)
        if fill is not None and q1(q, "nofill", "1") == "1":
            alpha &= (v != fill)
        t = np.clip((v - lo) / (hi - lo), 0, 1)
        ix = np.where(np.isfinite(t), t * 255, 0).astype("uint8")
        rgba = np.zeros(v.shape + (4,), dtype="uint8")
        rgba[:, :, :3] = L[ix]
        rgba[:, :, 3] = np.where(alpha, 255, 0)
        return rgba

    # ---------------- endpoints ----------------
    def q1(q, k, dflt=None):
        v = q.get(k)
        return v[0] if v else dflt

    def ds_of(q):
        ds = open_dataset(q1(q, "file"))
        var = q1(q, "var") or ds["default_var"] or ds["vars"][0]
        if var not in ds["vars"]:
            var = ds["default_var"] or ds["vars"][0]
        return ds, var, int(q1(q, "index", "0") or 0)

    def tile_bbox(z, x, y):
        n = 2 ** z
        sz = 2 * MERC_MAX / n
        return (-MERC_MAX + x * sz, MERC_MAX - (y + 1) * sz,
                -MERC_MAX + (x + 1) * sz, MERC_MAX - y * sz)

    def do_tile(q, z, x, y):
        ds, var, index = ds_of(q)
        if not ds["geographic"]:
            return 404, b"not geographic", "text/plain"
        mx0, my0, mx1, my1 = tile_bbox(z, x, y)
        (lon0, lat0) = merc_to_lonlat(mx0, my0)
        (lon1, lat1) = merc_to_lonlat(mx1, my1)
        deg_per_px = (lon1 - lon0) / TILE
        li = pick_level(ds, deg_per_px)
        lv = ds["levels"][li]
        # per-pixel lons in the grid's own frame (handles 0-360 grids)
        px_lons = norm_lon(lv, np.linspace(lon0, lon1, TILE + 1)[:-1]
                           + deg_per_px / 2)
        r0, r1, c0, c1 = window_of(lv, float(px_lons.min()), lat0,
                                   float(px_lons.max()), lat1)
        if r1 <= r0 or c1 <= c0:
            return 200, empty_png(), "image/png"
        if (r1 - r0) * (c1 - c0) > WINDOW_CAP:
            with ds["meter"]["lock"]:
                ds["meter"]["ops"].appendleft(
                    {"t": time.time(), "kind": "tile-skipped", "z": z,
                     "level": li, "window": [r1 - r0, c1 - c0],
                     "note": "window over cap — needs overviews or more zoom"})
            return 200, placeholder_png(), "image/png"
        data, _ = metered(ds, "tile", lambda: read_window(
            ds, li, var, index, r0, r1, c0, c1),
            z=z, level=li, window=[r1 - r0, c1 - c0])
        data, fill = decode_vals(data, var_decode(ds, li, var))
        # sample window onto the tile's mercator grid
        a, _, west, _, e, north = lv["transform"]
        my = np.linspace(my1, my0, TILE + 1)[:-1] - (my1 - my0) / (2 * TILE)
        lats = merc_to_lonlat(0, my)[1]
        iy = np.clip(((lats - north) / e).astype(int) - r0, -1, r1 - r0)
        ix = np.clip(((px_lons - west) / a).astype(int) - c0, -1, c1 - c0)
        yin = (iy >= 0) & (iy < r1 - r0)
        xin = (ix >= 0) & (ix < c1 - c0)
        out = data[np.ix_(np.clip(iy, 0, r1 - r0 - 1),
                          np.clip(ix, 0, c1 - c0 - 1))].astype("float64")
        out[~yin, :] = np.nan
        out[:, ~xin] = np.nan
        return 200, encode_png(np.ascontiguousarray(
            colorize(q, ds, var, index, out, fill))), "image/png"

    def do_meta(q):
        ds, var, index = ds_of(q)
        s = sample_stats(ds, var, index) if ds["geographic"] else {}
        lv0 = ds["levels"][0]
        a0 = get_array(ds, 0, var)
        ci = chunk_info(a0)
        itemsize = np.dtype(a0.dtype).itemsize
        # spatial shapes come from the multiscales attrs — opening every
        # level's array instead costs 2 serial GETs per level on stores
        # without consolidated metadata
        extra_cells = int(np.prod(a0.shape[:-2])) if a0.ndim > 2 else 1
        logical = sum(int(np.prod(lv["shape"])) * extra_cells * itemsize
                      for lv in ds["levels"])
        extra = []
        if a0.ndim > 2:
            dims = dims_of(a0)
            extra = [{"name": dims[k] if k < len(dims) else f"dim{k}",
                      "size": int(a0.shape[k])} for k in range(a0.ndim - 2)]
        out = {
            "file": ds["path"], "source": ds["src"]["label"],
            "kind": ds["src"]["kind"], "geographic": ds["geographic"],
            "vars": ds["vars"], "selected": var, "index": index,
            "extra_dims": extra, "dtype": str(a0.dtype),
            "fill_value": s.get("fill_value"),
            "shape": [int(x) for x in a0.shape],
            "chunks": ci["chunks"], "inner_chunks": ci["inner"],
            "chunk_logical_bytes": int(np.prod(ci["inner"] or ci["chunks"]
                                               or [0])) * itemsize,
            "codecs": ci["codecs"], "zarr_format":
                a0.metadata.to_dict().get("zarr_format"),
            "logical_bytes": logical,
            "n_levels": len(ds["levels"]),
            "levels": [{"asset": lv["asset"], "shape": lv["shape"],
                        "res_deg": abs(lv["transform"][0]) if lv["transform"] else None}
                       for lv in ds["levels"]],
            "lonlat_bounds": lonlat_bounds(lv0) if ds["geographic"] else None,
            "stats": s, "attrs": {str(k): str(v)[:400]
                                  for k, v in ds["attrs"].items()
                                  if k != "multiscales"},
        }
        return 200, json.dumps(out, default=str).encode(), "application/json"

    def do_probe(q):
        ds, var, index = ds_of(q)
        if not ds["geographic"]:
            return 404, b"{}", "application/json"
        lon, lat = float(q1(q, "lon")), float(q1(q, "lat"))
        lv = ds["levels"][0]
        a, _, west, _, e, north = lv["transform"]
        lon = float(norm_lon(lv, lon))
        row, col = int((lat - north) / e), int((lon - west) / a)
        H, W = lv["shape"]
        if not (0 <= row < H and 0 <= col < W):
            return 200, json.dumps({"value": None, "inside": False}).encode(), \
                "application/json"
        data, ent = metered(ds, "probe", lambda: read_window(
            ds, 0, var, index, row, row + 1, col, col + 1),
            lonlat=[round(lon, 5), round(lat, 5)])
        arr = get_array(ds, 0, var)
        ci = chunk_info(arr)
        ch = ci["chunks"] or [1, 1]
        inner = ci["inner"]
        dec = var_decode(ds, 0, var)
        v = float(decode_vals(data, dec)[0].ravel()[0])
        out = {"value": None if math.isnan(v) else v,
               "units": dec["units"], "inside": True,
               "row": row, "col": col, "level": 0,
               "shard": [row // ch[-2], col // ch[-1]],
               "inner_chunk": ([row // inner[-2], col // inner[-1]]
                               if inner else None),
               "cost": {"requests": ent["requests"],
                        "net_bytes": ent["net_bytes"],
                        "cache_hits": ent["cache_hits"], "ms": ent["ms"]}}
        return 200, json.dumps(out).encode(), "application/json"

    def do_hist(q):
        ds, var, index = ds_of(q)
        if not (ds["geographic"] and q1(q, "bbox")):
            return 200, json.dumps({"count": 0}).encode(), "application/json"
        w, s_, e_, n_ = [float(v) for v in q1(q, "bbox").split(",")]
        (lon0, lat0) = merc_to_lonlat(w, s_)
        (lon1, lat1) = merc_to_lonlat(e_, n_)
        li = pick_level(ds, max(lon1 - lon0, 1e-9) / 600)
        lv = ds["levels"][li]
        ln = norm_lon(lv, [lon0, lon1])
        r0, r1, c0, c1 = window_of(lv, float(ln.min()), lat0,
                                   float(ln.max()), lat1, pad=0)
        if r1 <= r0 or c1 <= c0:
            return 200, json.dumps({"count": 0}).encode(), "application/json"
        if (r1 - r0) * (c1 - c0) > WINDOW_CAP:
            return 200, json.dumps({"count": 0, "skipped": True}).encode(), \
                "application/json"
        data, _ = metered(ds, "hist", lambda: read_window(
            ds, li, var, index, r0, r1, c0, c1),
            level=li, window=[r1 - r0, c1 - c0])
        vals, fill = decode_vals(data, var_decode(ds, li, var))
        vals = vals.astype("float64")
        if fill is not None:
            vals = np.where(vals == fill, np.nan, vals)
        fin = vals[np.isfinite(vals)]
        bins = min(max(int(q1(q, "bins", "60")), 4), 200)
        out = {"count": int(fin.size), "level": li,
               "cells": int(vals.size),
               "fill_frac": 1.0 - (fin.size / max(vals.size, 1))}
        if fin.size:
            c, edges = np.histogram(fin, bins=bins)
            out.update({"counts": [int(x) for x in c],
                        "edges": [float(x) for x in edges],
                        "min": float(fin.min()), "max": float(fin.max()),
                        "mean": float(fin.mean()),
                        "p2": float(np.percentile(fin, 2)),
                        "p98": float(np.percentile(fin, 98))})
        return 200, json.dumps(out).encode(), "application/json"

    prefetching = set()
    pf_lock = threading.Lock()

    def do_prefetch(q):
        """Warm the NEXT time-chunk in the background (fire-and-forget from
        the UI after a slider move) so scrubbing forward hits the cache.
        Reads outside the per-dataset op lock — it must not block tiles."""
        ds, var, index = ds_of(q)
        a = get_array(ds, 0, var)
        ci = chunk_info(a)
        if a.ndim < 3 or not ci["chunks"]:
            return 200, b'{"ok": false}', "application/json"
        ct = max(int(ci["chunks"][0]), 1)
        nxt = ((int(index) // ct) + 1) * ct
        if nxt >= a.shape[0]:
            return 200, b'{"ok": false}', "application/json"
        key = (ds["path"], var, nxt // ct)
        with pf_lock:
            if key in prefetching:
                return 200, b'{"ok": true, "already": true}', "application/json"
            prefetching.add(key)
        H, W = a.shape[-2], a.shape[-1]

        def work():
            t0 = time.perf_counter()
            try:
                read_window(ds, 0, var, nxt, H // 2, H // 2 + 1,
                            W // 2, W // 2 + 1)
                with ds["meter"]["lock"]:
                    ds["meter"]["ops"].appendleft(
                        {"t": time.time(), "kind": "prefetch",
                         "note": f"warmed time chunk @ t={nxt}",
                         "ms": round((time.perf_counter() - t0) * 1000, 1)})
            except Exception:
                pass
            finally:
                with pf_lock:
                    prefetching.discard(key)
        threading.Thread(target=work, daemon=True).start()
        return 200, json.dumps({"ok": True, "next_index": nxt}).encode(), \
            "application/json"

    def do_stats(q):
        ds, _, _ = ds_of(q)
        m = ds["meter"]
        with m["lock"]:
            if q1(q, "reset") == "1":
                m.update({"requests": 0, "net_bytes": 0, "net_ms": 0.0,
                          "missing": 0, "lists": 0, "cache_hits": 0,
                          "cache_saved": 0, "opened": time.time()})
                m["ops"].clear()
            out = {k: m[k] for k in ("requests", "net_bytes", "net_ms",
                                     "missing", "lists", "cache_hits",
                                     "cache_saved", "cache_bytes", "opened")}
            out["ops"] = list(m["ops"])
        return 200, json.dumps(out).encode(), "application/json"

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
                    parts = u.path.split("/")
                    z, x = int(parts[2]), int(parts[3])
                    y = int(parts[4].split(".")[0])
                    code, body, ct = do_tile(q, z, x, y)
                elif u.path == "/meta":
                    code, body, ct = do_meta(q)
                elif u.path == "/probe":
                    code, body, ct = do_probe(q)
                elif u.path == "/hist":
                    code, body, ct = do_hist(q)
                elif u.path == "/stats":
                    code, body, ct = do_stats(q)
                elif u.path == "/prefetch":
                    code, body, ct = do_prefetch(q)
                else:
                    code, body, ct = 404, b"not found", "text/plain"
            except Exception as e:
                import traceback
                traceback.print_exc()
                code, body, ct = 500, json.dumps(
                    {"error": str(e)}).encode(), "application/json"
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
    print(f"zarr AOI daemon on 127.0.0.1:{port} (v{VERSION})", flush=True)
    srv.serve_forever()


if __name__ == "__main__" and "--serve" in sys.argv:
    _serve()

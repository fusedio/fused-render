"""Persistent grid tile daemon for the netcdf / zarr previews.

Same architecture as geotiff/tile_server.py (the app runner costs ~700ms
per runPython call, so a long-lived localhost daemon serves MapLibre
directly), but for lat/lon-gridded datasets: it loads ONE 2D slice of a
variable fully into memory (they are small — a global 0.25° grid is ~1M
cells), then serves Web-Mercator tiles / viewport histograms / hover values
from the in-RAM array in a few ms.

Loaders:
  .nc      — NetCDF3 via scipy (pure); NetCDF4/HDF5 via a system Python with
             xarray (one subprocess per slice, arrays via a temp .npz)
  zarr dir — pure chunk reader shared with the classic template
             (_zarr_core.py); zstd/v3 stores via a system Python with zarr

Endpoints (GET, CORS *):
  /ping /quit
  /meta?file=&var=&index=   -> dataset info + loads the slice: variables,
                               extra dims, stats, stretch, lonlat bounds,
                               geographic flag
  /tile/{z}/{x}/{y}.png?file=&var=&index=&cmap=&robust=&div=&stretch=&vlo=&vhi=
  /hist?file=&var=&index=&bbox=w,s,e,n(3857)&bins=
  /value?file=&var=&index=&lon=&lat=
  /img?file=&var=&index=&max_cells=&cmap=...  -> whole-slice PNG (fallback
                               view for non-geographic grids)

This daemon is keyed by a content hash of the script, so identical copies
across templates share one running daemon. netcdf/ is now the sole copy (the
former byte-identical zarr/ twin was removed when that template was retired).
"""
# /// script
# dependencies = ["numpy", "scipy", "zarr"]
# ///

import hashlib
import json
import math
import os
import sys
import threading
import time

DAEMON_DEPS = ["numpy", "scipy", "zarr"]
DAEMON_ROOT = os.path.join(
    os.path.expanduser(os.environ["FUSED_RENDER_CACHE_DIR"]), "daemons", "gridv2"
) if os.environ.get("FUSED_RENDER_CACHE_DIR") else os.path.expanduser(
    "~/.cache/fused-render-gridv2"
)
STATE = os.path.join(DAEMON_ROOT, "daemon.json")
DAEMON_VENV = os.path.join(
    DAEMON_ROOT,
    "venv-" + hashlib.sha256(",".join(DAEMON_DEPS).encode()).hexdigest()[:8],
)
IDLE_EXIT_S = 30 * 60
TILE = 256
MERC_R = 6378137.0
MERC_MAX = math.pi * MERC_R
MAX_LAT = 85.05112878

SPATIAL_Y = {"lat", "latitude", "y", "yc", "rlat", "nav_lat"}
SPATIAL_X = {"lon", "longitude", "x", "xc", "rlon", "nav_lon"}


def _me():
    if "__file__" in globals():
        return os.path.abspath(__file__)
    return os.path.join(os.path.abspath(sys.path[0]), "grid_tile_server.py")


def _daemon_python():
    vp = (os.path.join(DAEMON_VENV, "Scripts", "python.exe") if os.name == "nt"
          else os.path.join(DAEMON_VENV, "bin", "python"))
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


def _version():
    # content hash (not mtime): the netcdf and zarr copies are identical,
    # so whichever spawned the daemon satisfies the other's version check
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
    with open(log, "ab") as lf:
        subprocess.Popen([_daemon_python(), _me(), "--serve"],
                         stdout=lf, stderr=lf,
                         start_new_session=True, cwd=os.path.dirname(_me()))
    for _ in range(200):
        time.sleep(0.05)
        try:
            with open(STATE) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), version):
                return {"port": st["port"], "token": st.get("token"),
                        "reused": False}
        except (OSError, ValueError):
            continue
    return {"error": f"grid daemon did not start — see {log}"}


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

    # Per-daemon secret required on every data endpoint (except /ping), threaded
    # in by the template as ?t=; see geotiff/tile_server.py for the rationale.
    TOKEN = secrets.token_urlsafe(32)

    here = os.path.dirname(_me())
    sys.path.insert(0, here)
    import _zarr_core as Z          # pure zarr chunk reader (v1 code)

    VERSION = _version()
    last_hit = [time.time()]

    # ---------------- colormaps + PNG (self-contained) ----------------
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

    def clean(x):
        if x is None:
            return None
        f = float(x)
        return None if (math.isnan(f) or math.isinf(f)) else f

    # ---------------- system python (xarray / zarr fallbacks) ----------------
    def system_python(module):
        import shutil
        import subprocess
        cands = []
        if os.environ.get("GEO_PYTHON"):
            cands.append(os.environ["GEO_PYTHON"])
        # the daemon venv itself ships zarr (DAEMON_DEPS), so it is normally
        # the interpreter found here — no host python needed
        cands.append(sys.executable)
        home = os.path.expanduser("~")
        cands += [os.path.join(home, p) for p in
                  ("miniforge3/bin/python", "miniconda3/bin/python", "anaconda3/bin/python")]
        for nm in ("python3", "python"):
            w = shutil.which(nm)
            if w:
                cands.append(w)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        seen = set()
        for c in cands:
            # dedup on the resolved target but RUN the original path: a venv's
            # bin/python is a symlink to the base interpreter, and resolving it
            # before exec would escape the venv (losing its site-packages)
            rc = os.path.realpath(c)
            if rc in seen or not os.path.exists(rc):
                continue
            seen.add(rc)
            try:
                r = subprocess.run([c, "-c", f"import {module}"],
                                   capture_output=True, timeout=15, env=env)
                if r.returncode == 0:
                    return c
            except Exception:
                continue
        return None

    _NC_WORKER = r'''
import sys, os, json, tempfile
import numpy as np, xarray as xr
p = json.loads(sys.stdin.read())
SY = {"lat","latitude","y","yc","rlat","nav_lat"}
SX = {"lon","longitude","x","xc","rlon","nav_lon"}
ds = xr.open_dataset(p["file"])
names = [str(n) for n in ds.data_vars]
if not names: print(json.dumps({"error":"no data variables"})); sys.exit(0)
def score(n):
    v = ds[n]; return (v.dtype.kind == "f", v.ndim, int(v.size))
chosen = p["var"] if p["var"] in names else max(names, key=score)
cv = ds[chosen]
dims = [str(d) for d in cv.dims]
ydim = next((d for d in dims if d.lower() in SY), None)
xdim = next((d for d in dims if d.lower() in SX), None)
if not (ydim and xdim) and len(dims) >= 2: ydim, xdim = dims[-2], dims[-1]
extra = [d for d in dims if d not in (ydim, xdim)]
sel = {}; extra_dims = []; idx0 = 0
for i, d in enumerate(extra):
    n0 = int(ds.sizes[d])
    idx = max(0, min(int(p["index"]), n0 - 1)) if i == 0 else 0
    if i == 0: idx0 = idx
    sel[d] = idx
    vals = None
    if d in ds.coords and ds.coords[d].size <= 500:
        vals = [str(x) for x in np.asarray(ds.coords[d].values).ravel()]
    extra_dims.append({"name": d, "size": n0, "values": vals})
data = np.squeeze(cv.isel(**sel).values.astype("float64"))
if data.ndim != 2: data = np.atleast_2d(data)
rem = [d for d in dims if d in (ydim, xdim)]
if len(rem) == 2 and rem == [xdim, ydim]: data = data.T
def coordvals(dim):
    if dim and dim in ds.coords:
        a = np.asarray(ds[dim].values).ravel()
        if np.issubdtype(a.dtype, np.number): return a.astype("float64")
    return None
lats = coordvals(ydim); lons = coordvals(xdim)
tmp = tempfile.NamedTemporaryFile(suffix=".npz", delete=False)
np.savez(tmp.name, vals=data,
         lats=(lats if lats is not None else np.zeros(0)),
         lons=(lons if lons is not None else np.zeros(0)))
allvars = [{"name": n, "dims": [str(d) for d in ds[n].dims],
            "shape": [int(s) for s in ds[n].shape], "dtype": str(ds[n].dtype),
            "long_name": str(ds[n].attrs.get("long_name", n)),
            "units": str(ds[n].attrs.get("units", ""))} for n in names]
print(json.dumps({"npz": tmp.name + ".npz" if not tmp.name.endswith(".npz") else tmp.name,
    "vars": allvars, "chosen": chosen, "index": idx0, "extra_dims": extra_dims,
    "engine": "system (xarray)",
    "attrs": {str(k): str(v)[:400] for k, v in ds.attrs.items()}}))
'''

    _ZARR_WORKER = r'''
import sys, os, json, tempfile
import numpy as np, zarr
p = json.loads(sys.stdin.read())
SY = {"lat","latitude","y","yc","rlat","nav_lat"}
SX = {"lon","longitude","x","xc","rlon","nav_lon"}
g = zarr.open_group(p["file"], mode="r")
BUDGET = 32_000_000     # target cells for an auto-picked 2D slice
HARD_CAP = 256_000_000  # refuse (error, don't OOM) above this
# Multiscale stores (zarr-conventions "multiscales"): the root attrs list every
# pyramid level with its shape, so pick the finest level that fits the budget
# up front and walk ONLY that subgroup — walking all levels costs a metadata
# round-trip per node (slow on remote mounts) and auto-pick-by-size would grab
# the native-resolution level (trillions of cells).
asset = None
ms = dict(g.attrs).get("multiscales")
if isinstance(ms, dict) and isinstance(ms.get("layout"), list):
    levels = []
    for e in ms["layout"]:
        sh = e.get("spatial:shape") or []
        if isinstance(e.get("asset"), str) and len(sh) == 2:
            levels.append((str(e["asset"]), int(sh[0]) * int(sh[1])))
    if levels:
        want = p["var"].split("/", 1)[0] if p["var"] else None
        if any(nm == want for nm, _ in levels):
            asset = want
        else:
            fit = [t for t in levels if t[1] <= BUDGET]
            asset = max(fit, key=lambda t: t[1])[0] if fit else min(levels, key=lambda t: t[1])[0]
arrays = {}
def walk(node, prefix=""):
    for k, v in node.arrays(): arrays[prefix + k] = v
    for k, v in node.groups(): walk(v, prefix + k + "/")
if asset is not None: walk(g[asset], asset + "/")
else: walk(g)
def dims_of(a, n):
    d = a.attrs.get("_ARRAY_DIMENSIONS")
    if not d:
        d = getattr(getattr(a, "metadata", None), "dimension_names", None)
        if d and not all(d): d = None
    return list(d) if d else [f"dim{i}" for i in range(a.ndim)]
dimnames = set()
for n, a in arrays.items():
    for d in dims_of(a, n): dimnames.add(d)
coords, bands = [], []
for n, a in arrays.items():
    base = n.split("/")[-1]
    if base in dimnames or (a.ndim <= 1 and base.lower() in SY | SX | {"time","level","lev","depth","height"}):
        coords.append(n)
    else: bands.append(n)
if not bands: bands = list(arrays)
def slice_cells(n):
    a = arrays[n]
    return int(np.prod(a.shape[-2:])) if a.ndim >= 2 else int(np.prod(a.shape))
def score(n):
    a = arrays[n]; return (a.dtype.kind == "f", a.ndim, int(np.prod(a.shape)))
if p["var"] in bands:
    chosen = p["var"]
else:
    fit = [n for n in bands if slice_cells(n) <= BUDGET]
    chosen = max(fit, key=score) if fit else min(bands, key=slice_cells)
if slice_cells(chosen) > HARD_CAP:
    print(json.dumps({"error": "slice of '%s' is %dM cells - too large to load; pick a coarser overview level or smaller variable" % (chosen, slice_cells(chosen) // 1000000)})); sys.exit(0)
a = arrays[chosen]; dims = dims_of(a, chosen)
ydim = next((d for d in dims if d.split("/")[-1].lower() in SY), None)
xdim = next((d for d in dims if d.split("/")[-1].lower() in SX), None)
if not (ydim and xdim) and len(dims) >= 2: ydim, xdim = dims[-2], dims[-1]
extra = [d for d in dims if d not in (ydim, xdim)]
sl = [slice(None)] * a.ndim; extra_dims = []; idx0 = 0
def coord_arr(d):
    for n in coords:
        if n == d or n.split("/")[-1] == d: return arrays[n]
    return None
for i, d in enumerate(extra):
    pos = dims.index(d); n0 = a.shape[pos]
    idx = max(0, min(int(p["index"]), n0 - 1)) if i == 0 else 0
    if i == 0: idx0 = idx
    sl[pos] = idx
    ca = coord_arr(d); vals = None
    if ca is not None and ca.size <= 500: vals = [str(x) for x in np.asarray(ca[:]).ravel()]
    extra_dims.append({"name": d, "size": int(n0), "values": vals})
def cvals(d):
    ca = coord_arr(d)
    if ca is not None and np.issubdtype(ca.dtype, np.number):
        return np.asarray(ca[:], dtype="float64").ravel()
    return None
# data + coords live in separate files: overlap the reads (remote mounts pay
# a network round-trip per file, so serial reads add up)
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(3) as _ex:
    _fd = _ex.submit(lambda: np.asarray(a[tuple(sl)], dtype="float64"))
    _fy = _ex.submit(cvals, ydim); _fx = _ex.submit(cvals, xdim)
    data = np.squeeze(_fd.result()); lats = _fy.result(); lons = _fx.result()
if data.ndim != 2: data = np.atleast_2d(data)
ypos, xpos = dims.index(ydim), dims.index(xdim)
if ypos > xpos: data = data.T
tmp = tempfile.NamedTemporaryFile(suffix=".npz", delete=False)
np.savez(tmp.name, vals=data,
         lats=(lats if lats is not None else np.zeros(0)),
         lons=(lons if lons is not None else np.zeros(0)))
allvars = [{"name": n, "dims": dims_of(arrays[n], n),
            "shape": [int(s) for s in arrays[n].shape], "dtype": str(arrays[n].dtype),
            "long_name": str(arrays[n].attrs.get("long_name", n)),
            "units": str(arrays[n].attrs.get("units", ""))} for n in bands]
print(json.dumps({"npz": tmp.name + ".npz" if not tmp.name.endswith(".npz") else tmp.name,
    "vars": allvars, "chosen": chosen, "index": idx0, "extra_dims": extra_dims,
    "engine": "system (zarr)",
    "attrs": {str(k): str(v)[:400] for k, v in dict(g.attrs).items()}}))
'''

    def run_worker(code, module, params):
        import subprocess
        py = system_python(module)
        if not py:
            raise Z.Unsupported(f"needs a system Python with {module} (set GEO_PYTHON)")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        # NetCDF4/HDF5 opens request a POSIX file lock. Mounted buckets are
        # served over NFS (see shell/mounts.py), which grants no locks, so HDF5
        # aborts with "errno = 77, No locks available" (ENOLCK). The mounts are
        # read-only, so disabling locking is safe. setdefault leaves an explicit
        # host override intact.
        env.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")
        r = subprocess.run([py, "-c", code], input=json.dumps(params),
                           capture_output=True, text=True, timeout=180, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"system engine failed: {r.stderr[-400:]}")
        d = json.loads(r.stdout.strip().splitlines()[-1])
        if "error" in d:
            raise RuntimeError(d["error"])
        npz = np.load(d["npz"])
        vals = npz["vals"]
        lats = npz["lats"] if npz["lats"].size else None
        lons = npz["lons"] if npz["lons"].size else None
        try:
            os.unlink(d["npz"])
        except OSError:
            pass
        return vals, lats, lons, d

    # ---------------- pure loaders ----------------
    def load_nc_pure(path, var, index):
        from scipy.io import netcdf_file
        f = netcdf_file(path, "r", mmap=False)
        try:
            names = []
            for name in f.variables:
                v = f.variables[name]
                if name in f.dimensions or (len(v.dimensions) <= 1 and
                                            name.lower() in Z.COORD_NAMES):
                    continue
                names.append(name)
            if not names:
                raise RuntimeError("no data variables")

            def score(n):
                v = f.variables[n]
                return (np.asarray(v.data).dtype.kind == "f",
                        len(v.dimensions), int(np.asarray(v.data).size))
            chosen = var if var in names else max(names, key=score)
            cv = f.variables[chosen]
            dims = [str(d) for d in cv.dimensions]
            ydim = next((d for d in dims if d.lower() in SPATIAL_Y), None)
            xdim = next((d for d in dims if d.lower() in SPATIAL_X), None)
            if not (ydim and xdim) and len(dims) >= 2:
                ydim, xdim = dims[-2], dims[-1]
            extra = [d for d in dims if d not in (ydim, xdim)]

            data = np.array(cv.data, dtype="float64", copy=True)
            a = getattr(cv, "_attributes", {}) or {}
            for key in ("_FillValue", "missing_value"):
                if key in a:
                    for fv in np.array(a[key]).ravel():
                        data = np.where(data == float(fv), np.nan, data)
            if "scale_factor" in a:
                data = data * float(np.array(a["scale_factor"]).ravel()[0])
            if "add_offset" in a:
                data = data + float(np.array(a["add_offset"]).ravel()[0])

            extra_dims, idx0 = [], 0
            if extra:
                sl = [slice(None)] * data.ndim
                for i, d in enumerate(extra):
                    n0 = int(f.dimensions[d])
                    idx = max(0, min(int(index), n0 - 1)) if i == 0 else 0
                    if i == 0:
                        idx0 = idx
                    sl[dims.index(d)] = idx
                    vals = None
                    if d in f.variables and np.asarray(f.variables[d].data).size <= 500:
                        vals = [str(x) for x in np.asarray(f.variables[d].data).ravel()]
                    extra_dims.append({"name": d, "size": n0, "values": vals})
                data = data[tuple(sl)]
            data = np.squeeze(data)
            if data.ndim != 2:
                data = np.atleast_2d(data)
            rem = [d for d in dims if d in (ydim, xdim)]
            if len(rem) == 2 and rem == [xdim, ydim]:
                data = data.T

            def axis(d):
                if d and d in f.variables:
                    arr = np.asarray(f.variables[d].data).ravel()
                    if np.issubdtype(arr.dtype, np.number):
                        return arr.astype("float64")
                return None
            lats, lons = axis(ydim), axis(xdim)
            allvars = []
            for n in names:
                v = f.variables[n]
                at = getattr(v, "_attributes", {}) or {}
                allvars.append({"name": n, "dims": [str(d) for d in v.dimensions],
                                "shape": [int(s) for s in v.shape],
                                "dtype": str(np.asarray(v.data).dtype),
                                "long_name": str(at.get("long_name", b"") or n),
                                "units": str(at.get("units", b"") or "")})
            info = {"vars": allvars, "chosen": chosen, "index": idx0,
                    "extra_dims": extra_dims, "engine": "pure (scipy)",
                    "attrs": {str(k): str(v)[:400]
                              for k, v in (getattr(f, "_attributes", {}) or {}).items()}}
            return data, lats, lons, info
        finally:
            f.close()

    def load_zarr_pure(store, var, index):
        arrays, root_attrs = Z._load_meta(store)
        if not arrays:
            raise RuntimeError("no zarr arrays found in store")
        dim_sizes = {}
        for name, info in arrays.items():
            for d, s in zip(Z._dims_of(name, info), info["zarray"]["shape"]):
                dim_sizes[d] = int(s)
        dim_names = set(dim_sizes)
        coord_names, band_names = [], []
        for name, info in arrays.items():
            nd = len(info["zarray"]["shape"])
            base = name.split("/")[-1]
            is_coord = (name in dim_names or base in dim_names) or \
                       (nd <= 1 and base.lower() in Z.COORD_NAMES)
            (coord_names if is_coord else band_names).append(name)
        if not band_names:
            band_names = list(arrays)

        def kind(n):
            return np.dtype(arrays[n]["zarray"]["dtype"]).kind

        def slice_cells(n):
            shape = arrays[n]["zarray"]["shape"]
            return int(np.prod(shape[-2:] if len(shape) >= 2 else shape))
        if var in band_names:
            chosen = var
        else:
            fit = [n for n in band_names if slice_cells(n) <= 32_000_000]
            chosen = max(fit, key=lambda n: (kind(n) == "f",
                                             len(arrays[n]["zarray"]["shape"]),
                                             int(np.prod(arrays[n]["zarray"]["shape"])))) \
                if fit else min(band_names, key=slice_cells)
        if slice_cells(chosen) > 256_000_000:
            raise RuntimeError(f"slice of '{chosen}' is {slice_cells(chosen) // 1000000}M "
                               "cells - too large to load; pick a smaller variable")
        info = arrays[chosen]
        za = info["zarray"]
        dims = Z._dims_of(chosen, info)
        ydim = next((d for d in dims if d.split("/")[-1].lower() in SPATIAL_Y), None)
        xdim = next((d for d in dims if d.split("/")[-1].lower() in SPATIAL_X), None)
        if not (ydim and xdim) and len(dims) >= 2:
            ydim, xdim = dims[-2], dims[-1]
        ypos, xpos = dims.index(ydim), dims.index(xdim)
        extra = [d for d in dims if d not in (ydim, xdim)]

        def coord_array(dim):
            for n in coord_names:
                if n == dim or n.split("/")[-1] == dim:
                    return n
            return None

        fixed, extra_dims, idx0 = {}, [], 0
        for i, d in enumerate(extra):
            pos = dims.index(d)
            size = za["shape"][pos]
            idx = max(0, min(int(index), size - 1)) if i == 0 else 0
            if i == 0:
                idx0 = idx
            fixed[pos] = idx
            ca = coord_array(d)
            vals = None
            if ca and int(np.prod(arrays[ca]["zarray"]["shape"])) <= 500:
                vals = [str(x) for x in
                        Z._read_full_1d(store, ca, arrays[ca]["zarray"])]
            extra_dims.append({"name": d, "size": int(size), "values": vals})

        data = Z._read_2d_slice(store, chosen, za, ypos, xpos, fixed)

        def axis(d):
            ca = coord_array(d)
            if ca:
                arr = np.asarray(Z._read_full_1d(store, ca, arrays[ca]["zarray"]))
                if np.issubdtype(arr.dtype, np.number):
                    return arr.astype("float64")
            return None
        lats, lons = axis(ydim), axis(xdim)
        allvars = [{"name": n, "dims": Z._dims_of(n, arrays[n]),
                    "shape": [int(s) for s in arrays[n]["zarray"]["shape"]],
                    "dtype": str(np.dtype(arrays[n]["zarray"]["dtype"])),
                    "long_name": str(arrays[n].get("zattrs", {}).get("long_name", n)),
                    "units": str(arrays[n].get("zattrs", {}).get("units", ""))}
                   for n in band_names]
        meta = {"vars": allvars, "chosen": chosen, "index": idx0,
                "extra_dims": extra_dims, "engine": "pure",
                "attrs": {str(k): str(v)[:400] for k, v in root_attrs.items()}}
        return data, lats, lons, meta

    def load_slice_raw(path, var, index):
        if os.path.isdir(path) or os.path.isdir(os.path.dirname(path)) and \
                os.path.basename(path) in (".zgroup", ".zattrs", ".zmetadata"):
            store = path if os.path.isdir(path) else os.path.dirname(path)
            try:
                return load_zarr_pure(store, var, index)
            except (Z.Unsupported, Exception) as e:
                if isinstance(e, Z.Unsupported) or "compressor" in str(e):
                    return run_worker(_ZARR_WORKER, "zarr",
                                      {"file": store, "var": var, "index": index})
                raise
        # NetCDF
        with open(path, "rb") as fh:
            magic = fh.read(4)
        if magic[:3] == b"CDF":
            return load_nc_pure(path, var, index)
        return run_worker(_NC_WORKER, "xarray",
                          {"file": path, "var": var, "index": index})

    # ---------------- slice cache ----------------
    slices = {}        # (path, var, index) -> slice dict
    order = []
    s_lock = threading.Lock()

    def prep_geo(vals, lats, lons):
        """Normalize orientation + build cell-edge arrays for index lookup."""
        geo = (lats is not None and lons is not None and
               lats.size == vals.shape[0] and lons.size == vals.shape[1] and
               lats.size >= 2 and lons.size >= 2 and
               np.nanmin(lats) >= -90.5 and np.nanmax(lats) <= 90.5)
        if not geo:
            return None
        lats = lats.copy(); lons = lons.copy()
        flip_y = lats[0] < lats[-1]           # want row 0 = north
        if flip_y:
            lats = lats[::-1]
            vals = vals[::-1, :]
        wrap360 = lons.max() > 180.0
        # ascending lon edges
        if lons[0] > lons[-1]:
            lons = lons[::-1]
            vals = vals[:, ::-1]

        def edges(c, descending=False):
            mid = (c[1:] + c[:-1]) / 2
            first = c[0] - (c[1] - c[0]) / 2
            last = c[-1] + (c[-1] - c[-2]) / 2
            return np.concatenate([[first], mid, [last]])
        lat_e = edges(lats)                   # descending
        lon_e = edges(lons)                   # ascending
        # display bounds in [-180, 180]; a 0–360 grid that spans (or crosses
        # the antimeridian after conversion) is reported as full-width
        if wrap360:
            w = ((lon_e[0] + 180) % 360) - 180
            e = ((lon_e[-1] + 180) % 360) - 180
            if lon_e[-1] - lon_e[0] >= 359 or w >= e:
                w, e = -180.0, 180.0
        else:
            w, e = float(lon_e[0]), float(lon_e[-1])
        return {"vals": vals, "lats": lats, "lons": lons,
                "lat_edges": lat_e, "lon_edges": lon_e, "wrap360": wrap360,
                "bounds": {"west": float(w), "east": float(e),
                           "south": float(max(-90.0, min(lat_e[0], lat_e[-1]))),
                           "north": float(min(90.0, max(lat_e[0], lat_e[-1])))}}

    def get_slice(path, var, index):
        key = (path, var, int(index))
        with s_lock:
            s = slices.get(key)
            if s is not None:
                return s
        vals, lats, lons, info = load_slice_raw(path, var, int(index))
        vals = np.where(np.isfinite(vals), vals, np.nan).astype("float64")
        g = prep_geo(vals, lats, lons)
        fin = vals[np.isfinite(vals)]
        stats = {"count": int(fin.size), "nan": int(vals.size - fin.size)}
        if fin.size:
            stats.update({"min": clean(fin.min()), "max": clean(fin.max()),
                          "mean": clean(fin.mean()), "std": clean(fin.std()),
                          "median": clean(np.median(fin)),
                          "p2": clean(np.percentile(fin, 2)),
                          "p98": clean(np.percentile(fin, 98))})
        s = {"key": key, "vals": (g["vals"] if g else vals), "geo": g,
             "info": info, "stats": stats,
             "stretch": [stats.get("p2", 0.0), stats.get("p98", 1.0)]}
        with s_lock:
            slices[key] = s
            order.append(key)
            while len(order) > 6:
                slices.pop(order.pop(0), None)
        return s

    # ---------------- sampling ----------------
    def merc_to_lat(my):
        return np.degrees(2 * np.arctan(np.exp(my / MERC_R)) - np.pi / 2)

    def sample_bbox(s, mx0, my0, mx1, my1, ow, oh):
        """Sample the slice onto a regular mercator grid -> (oh, ow) float64."""
        g = s["geo"]
        mx = np.linspace(mx0, mx1, ow + 1)[:-1] + (mx1 - mx0) / (2 * ow)
        my = np.linspace(my1, my0, oh + 1)[:-1] - (my1 - my0) / (2 * oh)
        lons = np.degrees(mx / MERC_R)
        lats = merc_to_lat(my)
        if g["wrap360"]:
            # periodic remap into [first_edge, first_edge + 360): the last
            # fraction of a cell before 360° wraps onto cell 0, so global
            # grids have no seam at the prime meridian
            e0 = g["lon_edges"][0]
            lons = np.mod(lons - e0, 360.0) + e0
        # index lookup via cell edges (lat edges descending -> search reversed)
        lat_e = g["lat_edges"]; lon_e = g["lon_edges"]
        iy = lat_e.size - 1 - np.searchsorted(lat_e[::-1], lats, side="left")
        ix = np.searchsorted(lon_e, lons, side="right") - 1
        ny, nx = g["vals"].shape
        yin = (iy >= 0) & (iy < ny)
        xin = (ix >= 0) & (ix < nx)
        out = np.full((oh, ow), np.nan)
        if yin.any() and xin.any():
            iyc = np.clip(iy, 0, ny - 1)
            ixc = np.clip(ix, 0, nx - 1)
            sub = g["vals"][np.ix_(iyc, ixc)]
            sub[~yin, :] = np.nan
            sub[:, ~xin] = np.nan
            out = sub
        return out

    # ---------------- rendering ----------------
    def q1(q, k, dflt=None):
        v = q.get(k)
        return v[0] if v else dflt

    def slice_of(q):
        return get_slice(os.path.abspath(os.path.expanduser(q1(q, "file"))),
                         q1(q, "var", ""), q1(q, "index", "0"))

    def stretch_of(q, s):
        st = q1(q, "stretch", "")
        try:
            v = [float(x) for x in st.split(",")]
            if len(v) == 2:
                return v
        except (ValueError, AttributeError):
            pass
        if q1(q, "robust", "1") == "1":
            return s["stretch"]
        return [s["stats"].get("min", 0.0), s["stats"].get("max", 1.0)]

    def colorize(q, s, vals):
        lo, hi = stretch_of(q, s)
        if q1(q, "div", "0") == "1":
            m = max(abs(lo), abs(hi)) or 1.0
            lo, hi = -m, m
        if hi <= lo:
            hi = lo + 1.0
        L = lut(q1(q, "cmap", "viridis"))
        t = np.clip((vals - lo) / (hi - lo), 0, 1)
        ix = np.where(np.isfinite(t), t * 255, 0).astype("uint8")
        rgba = np.zeros(vals.shape + (4,), dtype="uint8")
        rgba[:, :, :3] = L[ix]
        alpha = np.isfinite(vals)
        vlo, vhi = q1(q, "vlo", ""), q1(q, "vhi", "")
        if vlo not in ("", None) and vhi not in ("", None):
            alpha = alpha & (vals >= float(vlo)) & (vals <= float(vhi))
        rgba[:, :, 3] = np.where(alpha, 255, 0)
        return rgba

    def tile_bbox(z, x, y):
        n = 2 ** z
        sz = 2 * MERC_MAX / n
        return (-MERC_MAX + x * sz, MERC_MAX - (y + 1) * sz,
                -MERC_MAX + (x + 1) * sz, MERC_MAX - y * sz)

    def do_tile(q, z, x, y):
        s = slice_of(q)
        if not s["geo"]:
            return 404, b"not geographic", "text/plain"
        mx0, my0, mx1, my1 = tile_bbox(z, x, y)
        vals = sample_bbox(s, mx0, my0, mx1, my1, TILE, TILE)
        return 200, encode_png(np.ascontiguousarray(colorize(q, s, vals))), "image/png"

    dir_sizes = {}     # store path -> capped-walk size (None when capped)

    def do_meta(q):
        path = os.path.abspath(os.path.expanduser(q1(q, "file")))
        s = slice_of(q)
        g = s["geo"]
        rows, cols = s["vals"].shape
        out = {"file": path, "supported": True, "geographic": bool(g),
               "vars": s["info"]["vars"], "selected": s["info"]["chosen"],
               "index": s["info"]["index"], "extra_dims": s["info"]["extra_dims"],
               "engine": s["info"]["engine"], "attrs": s["info"].get("attrs", {}),
               "stats": s["stats"], "stretch": [s["stretch"]],
               "shape": [rows, cols],
               "lonlat_bounds": (g["bounds"] if g else None)}
        try:
            if os.path.isfile(path):
                out["file_size"] = os.path.getsize(path)
            elif path in dir_sizes:
                out["file_size"] = dir_sizes[path]
            else:
                # capped walk: a store can hold millions of chunk files and on
                # a remote mount every listing/stat is a network round-trip —
                # give up after a short deadline instead of stalling /meta,
                # and cache the answer so only the first /meta per store pays
                deadline = time.time() + 2.0
                total, files, capped = 0, 0, False
                for dp, _, fs in os.walk(path):
                    for f in fs:
                        files += 1
                        # Check per file, not per directory: a flat store can
                        # hold all its chunks in one dir, and a per-dir check
                        # would stat every one before the cap could fire.
                        if time.time() > deadline or files > 20000:
                            capped = True
                            break
                        try:
                            total += os.path.getsize(os.path.join(dp, f))
                        except OSError:
                            pass
                    if capped:
                        break
                dir_sizes[path] = None if capped else total
                out["file_size"] = dir_sizes[path]
        except OSError:
            out["file_size"] = None
        return 200, json.dumps(out, default=str).encode(), "application/json"

    def do_hist(q):
        s = slice_of(q)
        bins = min(max(int(q1(q, "bins", "60")), 4), 200)
        if s["geo"] and q1(q, "bbox"):
            try:
                w, so, e, n = [float(v) for v in q1(q, "bbox").split(",")]
                aspect = max((e - w) / max(n - so, 1e-9), 1e-6)
                oh = max(8, int((90000 / aspect) ** 0.5))
                ow = max(8, int(oh * aspect))
                vals = sample_bbox(s, w, so, e, n, ow, oh)
            except ValueError:
                vals = s["vals"]
        else:
            vals = s["vals"]
        fin = vals[np.isfinite(vals)]
        ch = {"count": int(fin.size)}
        if fin.size:
            c, edges = np.histogram(fin, bins=bins)
            ch.update({"counts": [int(v) for v in c],
                       "edges": [float(v) for v in edges],
                       "min": float(fin.min()), "max": float(fin.max()),
                       "mean": float(fin.mean()), "std": float(fin.std()),
                       "median": float(np.median(fin)),
                       "p2": float(np.percentile(fin, 2)),
                       "p98": float(np.percentile(fin, 98))})
        return 200, json.dumps({"channels": [ch]}).encode(), "application/json"

    def do_value(q):
        s = slice_of(q)
        if not s["geo"]:
            return 404, b"{}", "application/json"
        lon, lat = float(q1(q, "lon")), float(q1(q, "lat"))
        mx = math.radians(lon) * MERC_R
        lat = max(-MAX_LAT, min(MAX_LAT, lat))
        my = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * MERC_R
        v = sample_bbox(s, mx - 0.5, my - 0.5, mx + 0.5, my + 0.5, 1, 1)[0, 0]
        return 200, json.dumps(
            {"values": [None if not np.isfinite(v) else float(v)]}).encode(), "application/json"

    def do_img(q):
        """Whole-slice PNG (non-geographic fallback view)."""
        s = slice_of(q)
        vals = s["vals"]
        max_cells = int(q1(q, "max_cells", "600000"))
        step = 1
        while (vals.shape[0] // step) * (vals.shape[1] // step) > max_cells and step < 64:
            step += 1
        return 200, encode_png(np.ascontiguousarray(
            colorize(q, s, vals[::step, ::step]))), "image/png"

    # ---------------- HTTP ----------------
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: F811

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
                elif u.path == "/hist":
                    code, body, ct = do_hist(q)
                elif u.path == "/value":
                    code, body, ct = do_value(q)
                elif u.path == "/img":
                    code, body, ct = do_img(q)
                else:
                    code, body, ct = 404, b"not found", "text/plain"
            except Exception as e:
                import traceback
                traceback.print_exc()
                code, body, ct = 500, json.dumps({"error": str(e)}).encode(), "application/json"
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
    print(f"grid tile daemon on 127.0.0.1:{port} (v{VERSION})", flush=True)
    srv.serve_forever()


if __name__ == "__main__" and "--serve" in sys.argv:
    import numpy as np  # noqa: F401
    _serve()

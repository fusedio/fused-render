"""COG overview pyramid — every resolution level of a GeoTIFF as real data:
dimensions, ground resolution, bytes on disk, tile counts, plus two rendered
previews per level (whole-image thumbnail + a fixed center crop at native
pixels, so you can SEE the detail each level throws away).

Uses rasterio + pillow + tifffile in the shared raster venv
(~/.cache/fused-render-compressbench — same env as cog_doctor /
compression playground, one install between the three tools).
"""

VENV_DIR_NAME = "fused-render-compressbench"
VENV_DEPS = ["rasterio", "numpy", "pillow", "rio-cogeo", "tifffile"]

# ---------------------------------------------------------------------------
# Mount-safe byte access (mirrors geotiff/tile_server.py). Files under a
# read-only rclone NFS mount stall (18-30s) or drop the mount on ANY kernel I/O
# (os.stat, open, mmap, GDAL). The template stays mount-AGNOSTIC — it never
# imports shell.mounts and never matches ~/.fused-render paths — and instead
# passes a `src` (server origin + /api/fs/raw?path=). All mount knowledge lives
# behind two HTTP endpoints: /api/fs/stat (says whether a path is `remote`) and
# /api/fs/raw (serves bytes, honours Range; proxies rclone's VFS so parallel
# range reads are safe). This block is defined ONCE here (importable + tested in
# tests/test_pyramid_mount.py) and injected verbatim into the worker via
# _worker_source(): the worker runs in a separate uv venv where fused_render is
# not importable, so it needs the source, not an import.
_SHARED = r'''
import io as _io
import json as _json
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq


def _server_url(src, endpoint, path):
    """Server URL built from `src`'s ORIGIN and our own normalized `path`. src
    is trusted only for scheme+netloc: its ?path= carries the browser's raw
    file param (possibly ~-prefixed / relative) and the fs endpoints do no
    expansion — judging remote-ness on one path string and range-reading another
    would 404. So we quote OUR path onto the endpoint, ignoring src's path."""
    u = _urlparse.urlsplit(src)
    return (f"{u.scheme}://{u.netloc}{endpoint}?path="
            + _urlparse.quote(path))


def _stat(src, path):
    """Ask /api/fs/stat about `path`. Returns:
      ("ok", payload)      — payload has bool `remote` and int `size`
      ("missing", None)    — server says the path does not exist (404)
      ("unreachable", None)— server could not be reached / errored; the caller
                             falls back to a local kernel probe (presumed local).
    """
    url = _server_url(src, "/api/fs/stat", path)
    try:
        with _urlreq.urlopen(url, timeout=10) as r:
            return ("ok", _json.load(r))
    except _urlerr.HTTPError as e:
        if e.code == 404:
            return ("missing", None)
        return ("unreachable", None)
    except Exception:  # noqa: BLE001 — any network error -> fall back to local
        return ("unreachable", None)


class _RangeReader:
    """Raw byte source over /api/fs/raw: one HTTP Range GET per read. When the
    server ignores Range and returns the whole object (200), slice our window
    out of the body so callers still get exactly what they asked for."""

    def __init__(self, url):
        self.url = url

    def read(self, off, count):
        req = _urlreq.Request(
            self.url, headers={"Range": f"bytes={off}-{off + count - 1}"})
        with _urlreq.urlopen(req, timeout=120) as r:
            body = r.read()
            status = r.status
        if status != 206:
            body = body[off:off + count]
        if len(body) != count:
            raise OSError(
                f"range read: wanted {count}B at {off}, got {len(body)}B")
        return body


class _HttpRangeFile(_io.RawIOBase):
    """A seekable, read-only binary file object over /api/fs/raw, for handing to
    rasterio (via opener=) and tifffile (which walk many small offsets — IFD
    tags, tile indices). Reads are served from fixed-size blocks with a tiny LRU
    so a burst of nearby small reads collapses into a few Range GETs instead of
    one round-trip per read. `size` comes from /api/fs/stat, so we never kernel-
    stat the mount to learn the length."""

    def __init__(self, url, size, block=65536):
        self._r = _RangeReader(url)
        self._size = int(size)
        self._pos = 0
        self._block = int(block)
        self._cache = {}
        self._order = []

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, off, whence=_io.SEEK_SET):
        if whence == _io.SEEK_SET:
            self._pos = off
        elif whence == _io.SEEK_CUR:
            self._pos += off
        elif whence == _io.SEEK_END:
            self._pos = self._size + off
        else:
            raise ValueError(f"bad whence: {whence}")
        return self._pos

    def _fetch_block(self, bi):
        blk = self._cache.get(bi)
        if blk is not None:
            return blk
        off = bi * self._block
        n = min(self._block, self._size - off)
        if n <= 0:
            return b""
        blk = self._r.read(off, n)
        self._cache[bi] = blk
        self._order.append(bi)
        if len(self._order) > 64:  # tiny LRU — keep memory bounded
            self._cache.pop(self._order.pop(0), None)
        return blk

    def readinto(self, b):
        if self._pos >= self._size:
            return 0
        want = min(len(b), self._size - self._pos)
        out = bytearray()
        p = self._pos
        while len(out) < want:
            bi = p // self._block
            blk = self._fetch_block(bi)
            start = p - bi * self._block
            take = min(len(blk) - start, want - len(out))
            if take <= 0:
                break
            out += blk[start:start + take]
            p += take
        b[:len(out)] = out
        self._pos += len(out)
        return len(out)
'''

exec(_SHARED, globals())


WORKER = r'''
import base64, io, json, math, os, sys
import numpy as np
import rasterio
import tifffile
from PIL import Image

path = sys.argv[1]
action = sys.argv[2] if len(sys.argv) > 2 else "analyze"
opts = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
THUMB = 800          # max px for whole-image previews (small levels stay native)
CROP_CAP = 512       # max stored px for the same-ground crops

# Mount-backed input? Then RAW_URL + SIZE come from main() (via /api/fs/stat)
# and every rasterio/tifffile handle is opened over HTTP range reads — never a
# kernel open/mmap of the NFS mount. Local files keep the plain path.
REMOTE = bool(opts.get("remote"))
RAW_URL = opts.get("raw_url")
SIZE = opts.get("size")


REMOTE_ID = "pyramid-remote.tif"  # the identifier handed to rasterio.open


def _http_opener(p, mode="rb", **_kw):
    # rasterio/GDAL call the opener not just for the dataset but for sidecar
    # probes: p.ovr, p.msk, p.aux.xml, a directory 'test', etc. Serving the main
    # bytes for a '.ovr' probe makes GDAL read the base image as a bogus EXTERNAL
    # overview (measured: a file with NO overviews then reports 32 phantom
    # levels). So serve RAW_URL ONLY for the exact identifier and raise
    # FileNotFoundError for every other path, so GDAL sees no sidecars.
    if str(p) != REMOTE_ID:
        raise FileNotFoundError(p)
    return _HttpRangeFile(RAW_URL, SIZE)


def ropen(**kw):
    """rasterio.open for the input, mount-safe. Remote -> a Python opener that
    range-reads /api/fs/raw (GDAL uses INTERNAL overviews for decimated reads,
    so it does NOT re-download the whole file per handle)."""
    if REMOTE:
        return rasterio.open(REMOTE_ID, opener=_http_opener, **kw)
    return rasterio.open(path, **kw)


def topen():
    """tifffile.TiffFile for the input, mount-safe (file object when remote)."""
    if REMOTE:
        return tifffile.TiffFile(_HttpRangeFile(RAW_URL, SIZE))
    return tifffile.TiffFile(path)

def emit(obj):
    """Print the result AND (for detached build/cogify jobs) persist it to the
    job status file the UI polls."""
    sf = opts.get("status_file")
    if sf:
        tmp = sf + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, sf)
    print(json.dumps(obj))

def fail(msg):
    emit({"error": msg, "state": "error"})
    sys.exit(0)

def overview_factors(w, h):
    """Halve until the largest dimension drops below one tile (512px)."""
    factors, f = [], 2
    while max(w, h) / f >= 512:
        factors.append(f)
        f *= 2
    return factors or [2]

def gdal_env_for(compression, dtype, bands):
    """Overview-creation options matching the file's own codec."""
    comp = (compression or "").upper().replace("ADOBE_DEFLATE", "DEFLATE")
    env = {}
    if comp and comp != "NONE":
        env["COMPRESS_OVERVIEW"] = comp
    if comp in ("DEFLATE", "LZW", "ZSTD"):
        env["PREDICTOR_OVERVIEW"] = "3" if dtype.startswith("float") else "2"
    if comp == "JPEG":
        env["JPEG_QUALITY_OVERVIEW"] = "85"
        if bands == 3:
            env["PHOTOMETRIC_OVERVIEW"] = "YCBCR"
            env["INTERLEAVE_OVERVIEW"] = "PIXEL"
    return env

# ---------- side actions (build / cogify / predict) ----------

if action == "build":
    # append internal overviews to the file IN PLACE (gdaladdo-style)
    from rasterio.enums import Resampling
    before = os.path.getsize(path)
    with rasterio.open(path) as src:
        if src.overviews(1):
            fail("file already has overviews")
        factors = overview_factors(src.width, src.height)
        env = gdal_env_for(src.profile.get("compress"), src.dtypes[0], src.count)
    rs = opts.get("resampling") or "average"
    try:
        with rasterio.Env(**env):
            with rasterio.open(path, "r+") as ds:
                ds.build_overviews(factors, Resampling[rs])
    except Exception as e:
        fail(f"build_overviews failed: {type(e).__name__}: {e}")
    emit({"ok": True, "state": "done", "mode": "in-place", "factors": factors,
          "resampling": rs, "before": before, "after": os.path.getsize(path)})
    sys.exit(0)

if action == "cogify":
    # full rewrite to a sibling file with a proper COG layout, then validate
    from rio_cogeo.cogeo import cog_translate, cog_validate
    from rio_cogeo.profiles import cog_profiles
    prof = opts.get("profile") or "deflate"
    dst = opts.get("out") or os.path.splitext(path)[0] + "_cog.tif"
    if os.path.abspath(dst) == os.path.abspath(path):
        fail("output would overwrite the input — pick another name")
    if os.path.exists(dst) and not opts.get("overwrite"):
        fail(f"already exists: {dst}")
    try:
        p = cog_profiles.get(prof)
        cog_translate(path, dst, p, quiet=True, web_optimized=False,
                      overview_resampling=opts.get("resampling") or "average")
        valid, errors, warnings = cog_validate(dst, quiet=True)
    except Exception as e:
        if os.path.exists(dst):
            os.remove(dst)
        fail(f"cog_translate failed: {type(e).__name__}: {e}")
    emit({"ok": True, "state": "done", "mode": "cogify", "out": dst, "profile": prof,
          "before": os.path.getsize(path), "after": os.path.getsize(dst),
          "valid": bool(valid), "errors": list(errors), "warnings": list(warnings)})
    sys.exit(0)

if action == "predict":
    # sample real blocks, recompress with candidate codecs, extrapolate sizes
    from rasterio.io import MemoryFile
    with ropen() as src:
        w, h, bands, dtype = src.width, src.height, src.count, src.dtypes[0]
        raw_total = w * h * bands * np.dtype(dtype).itemsize
        win = 1024
        # 5×5 grid incl. edges — corner/edge windows catch nodata margins that
        # compress to nothing; a center-heavy sample overestimates by ~30%
        fracs = (0.02, 0.25, 0.5, 0.75, 0.98)
        samples = []
        for fy in fracs:
            for fx in fracs:
                cx = min(max(0, int(w * fx) - win // 2), max(0, w - win))
                cy = min(max(0, int(h * fy) - win // 2), max(0, h - win))
                samples.append(src.read(window=rasterio.windows.Window(
                    cx, cy, min(win, w), min(win, h))))
        nodata = src.nodata
    # candidate codecs = the exact rio-cogeo profiles cogify would use, so the
    # prediction measures what cog_translate will actually write
    from rio_cogeo.profiles import cog_profiles
    cands = ["deflate", "zstd", "lzw"]
    if dtype == "uint8" and bands >= 3:
        cands = ["jpeg", "webp"] + cands
    rows = []
    for comp in cands:
        # pyramid = +1/3 pixels; resampled data compresses ~1.5× worse than the
        # base under lossless codecs (measured on DEMs), same under jpeg/webp
        ov_frac = (1 / 3) * (1.0 if comp in ("jpeg", "webp") else 1.5)
        try:
            base_prof = {k: v for k, v in dict(cog_profiles.get(comp)).items()
                         if k not in ("driver", "interleave")}
            cbytes = rbytes = 0
            for arr in samples:
                a = arr[:3] if comp in ("jpeg", "webp") else arr
                prof = {"driver": "GTiff", "width": a.shape[2], "height": a.shape[1],
                        "count": a.shape[0], "dtype": dtype, **base_prof}
                with MemoryFile() as mem:
                    with mem.open(**prof) as tmp:
                        tmp.write(a)
                    cbytes += len(mem.read())
                rbytes += a.nbytes
            ratio = cbytes / max(1, rbytes)
            base = ratio * raw_total
            rows.append({"profile": comp, "ratio": round(ratio, 4),
                         "est_base": int(base),
                         "est_total": int(base * (1 + ov_frac))})
        except Exception:
            continue  # codec unsupported in this build — drop the row
    print(json.dumps({"ok": True, "raw_bytes": int(raw_total),
                      "current_bytes": SIZE if REMOTE else os.path.getsize(path),
                      "rows": rows,
                      "note": "sampled 25 windows with rio-cogeo's own profiles; "
                              "overview overhead modeled as +33% pixels"}))
    sys.exit(0)

# ---------- default action: analyze ----------

def png64(arr, cap=None):
    """(bands, h, w) array -> base64 PNG. uint8 passes through as true color;
    other dtypes get a per-band 2-98% stretch."""
    raw = np.asarray(arr)
    if raw.ndim == 2:
        raw = raw[None]
    raw = raw[:3] if raw.shape[0] >= 3 else raw[:1]
    if raw.dtype == np.uint8:
        img = np.transpose(raw, (1, 2, 0))
        im = Image.fromarray(img[:, :, 0] if img.shape[2] == 1 else img)
        if cap and max(im.size) > cap:
            r = cap / max(im.size)
            im = im.resize((max(1, round(im.size[0] * r)), max(1, round(im.size[1] * r))),
                           Image.NEAREST)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    a = raw.astype("float64")
    out = np.empty(a.shape, dtype="uint8")
    for i, band in enumerate(a):
        finite = band[np.isfinite(band)]
        if finite.size == 0:
            out[i] = 0
            continue
        lo, hi = np.percentile(finite, (2, 98))
        if hi <= lo:
            lo, hi = finite.min(), max(finite.max(), finite.min() + 1)
        out[i] = np.clip((np.nan_to_num(band, nan=lo) - lo) / (hi - lo) * 255, 0, 255)
    img = np.transpose(out, (1, 2, 0))
    im = Image.fromarray(img[:, :, 0] if img.shape[2] == 1 else img)
    if cap and max(im.size) > cap:
        r = cap / max(im.size)
        im = im.resize((max(1, round(im.size[0] * r)), max(1, round(im.size[1] * r))),
                       Image.NEAREST)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()

try:
    src0 = ropen()
except Exception as e:
    fail(f"not a readable raster: {type(e).__name__}: {e}")

with src0:
    n_ov = len(src0.overviews(1))
    crs = str(src0.crs) if src0.crs else None
    crs_name = None
    try:
        if src0.crs:
            from pyproj import CRS as _PC
            _pc = _PC.from_wkt(src0.crs.to_wkt())
            _epsg = _pc.to_epsg()
            crs_name = f"EPSG:{_epsg}" if _epsg else (_pc.name or None)
    except Exception:
        pass
    unit = None
    try:
        unit = src0.crs.linear_units if src0.crs and src0.crs.is_projected else (
            "degree" if src0.crs else None)
    except Exception:
        pass
    bounds = list(src0.bounds)
    bounds4326 = None
    try:
        from rasterio.warp import transform_bounds
        if src0.crs:
            bounds4326 = list(transform_bounds(src0.crs, "EPSG:4326", *src0.bounds))
    except Exception:
        pass
    dtype = str(src0.dtypes[0])
    nbands = src0.count

# ---- byte accounting per IFD via tifffile (no decode, tags only) ----
ifds = []
try:
    with topen() as tf:
        for pg in tf.pages:
            try:
                h, w = pg.imagelength, pg.imagewidth
            except Exception:
                continue
            ifds.append({
                "w": w, "h": h,
                "bytes": int(sum(pg.databytecounts or [0])),
                "offset": int(min(pg.dataoffsets)) if pg.dataoffsets else None,
                "tiled": pg.is_tiled,
                "tile": [pg.tilelength, pg.tilewidth] if pg.is_tiled else None,
                "compression": str(pg.compression.name).lower(),
                "reduced": bool(pg.is_reduced),
            })
except Exception as e:
    fail(f"could not parse TIFF structure: {type(e).__name__}: {e}")

def ifd_for(w, h):
    for d in ifds:
        if d["w"] == w and d["h"] == h:
            return d
    return None

# ---- one entry per resolution level (level 0 = full res) ----
# All crops cover the SAME center ground patch (a window `base` level-0 pixels
# wide), each read at that level's own resolution — the quality comparison.
levels = []
file_size = SIZE if REMOTE else os.path.getsize(path)
w0 = h0 = base = None
# A decimated (out_shape) read of a level that HAS internal overviews is cheap:
# GDAL satisfies it from the nearest overview. But level 0 of a file with NO
# overviews decodes the ENTIRE base image — a full-file read. Over a mount that
# is the worst case, so skip the L0 thumbnail there and let the UI say so.
thumb_skipped = ("full-resolution preview skipped for remote file without "
                 "overviews (would decode the whole image over the network)")
for lvl in range(-1, n_ov):
    kw = {} if lvl < 0 else {"overview_level": lvl}
    with ropen(**kw) as src:
        w, h = src.width, src.height
        px = src.transform.a
        if lvl < 0:
            w0, h0 = w, h
            base = min(max(256, w0 // 16), w0, h0)
        tw = min(THUMB, w)
        th = max(1, round(h * tw / w))
        skip_thumb = REMOTE and n_ov == 0 and lvl < 0
        thumb = None if skip_thumb else png64(src.read(out_shape=(src.count, th, tw)))
        c = max(4, round(base * w / w0))
        win = rasterio.windows.Window((w - c) // 2, (h - c) // 2, c, c)
        crop = png64(src.read(window=win), cap=CROP_CAP)
        d = ifd_for(w, h) or {}
        blk = d.get("tile") or [src.block_shapes[0][0], src.block_shapes[0][1]]
        levels.append({
            "level": lvl + 1, "width": w, "height": h,
            "pixel_size": abs(px), "unit": unit,
            "bytes": d.get("bytes"), "offset": d.get("offset"),
            "pct": round(100 * d["bytes"] / file_size, 2) if d.get("bytes") else None,
            "compression": d.get("compression"),
            "tiled": d.get("tiled"), "block": blk,
            "n_blocks": math.ceil(w / blk[1]) * math.ceil(h / blk[0]),
            "thumb": thumb, "crop": crop, "crop_px": c,
            "thumb_skipped": thumb_skipped if skip_thumb else None,
            "crop_ground": base * abs(levels[0]["pixel_size"]) if levels else base * abs(px),
        })

masks = [d for d in ifds if not any(d["w"] == l["width"] and d["h"] == l["height"] for l in levels)]

# ---- COG health + "what would fixing cost" estimate ----
if REMOTE:
    # rio-cogeo's cog_validate opens the path with rasterio directly (no opener
    # hook), so it cannot run against /api/fs/raw without a kernel read of the
    # mount. Skip it rather than risk wedging the mount.
    cog = {"valid": None, "errors": [], "warnings": [],
           "skipped": "COG validation skipped for remote file"}
else:
    try:
        from rio_cogeo.cogeo import cog_validate
        _v, _e, _w = cog_validate(path, quiet=True)
        cog = {"valid": bool(_v), "errors": list(_e), "warnings": list(_w)}
    except Exception as e:
        cog = {"valid": None, "errors": [f"validator unavailable: {e}"], "warnings": []}
fix = None
if n_ov == 0:
    factors = overview_factors(w0, h0)
    ov_px = sum(math.ceil(w0 / f) * math.ceil(h0 / f) for f in factors)
    l0d = ifd_for(w0, h0) or {}
    bpp = (l0d.get("bytes") or file_size) / (w0 * h0)   # measured compressed bytes/px at L0
    comp0 = (l0d.get("compression") or "").lower()
    # resampled data packs ~1.5× worse under lossless codecs; jpeg/webp are
    # unaffected and uncompressed bytes are exact (+33% pixels = +33% bytes)
    penalty = 1.5 if comp0 not in ("jpeg", "webp", "none", "") else 1.0
    fix = {"factors": factors, "n_new_levels": len(factors),
           "ov_pixels_pct": round(100 * ov_px / (w0 * h0), 1),
           "est_extra_bytes": int(ov_px * bpp * penalty),
           "src_compression": l0d.get("compression"), "tiled": bool(l0d.get("tiled"))}

print(json.dumps({
    "file_size": file_size, "crs": crs, "crs_name": crs_name,
    "dtype": dtype, "bands": nbands,
    "bounds": bounds, "bounds4326": bounds4326,
    "cog": cog, "fix": fix, "remote": REMOTE,
    "n_overviews": n_ov, "levels": levels,
    "extra_ifds": [{k: d[k] for k in ("w", "h", "bytes", "compression")} for d in masks],
    "accounted": sum(l["bytes"] or 0 for l in levels) + sum(d["bytes"] for d in masks),
}))
'''


def _venv_python():
    import os
    import shutil
    import subprocess
    cache = os.path.expanduser(f"~/.cache/{VENV_DIR_NAME}")
    vpy = os.path.join(cache, "venv", "bin", "python")
    marker = os.path.join(cache, "deps_ok_pyramid")
    if os.path.exists(vpy) and os.path.exists(marker):
        return vpy
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    if not os.path.exists(uv) and not shutil.which("uv"):
        raise RuntimeError(
            "The overview pyramid needs the 'uv' tool to set up rasterio. "
            "Install it with: brew install uv — or: curl -LsSf https://astral.sh/uv/install.sh | sh")
    os.makedirs(cache, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    if not os.path.exists(vpy):
        subprocess.run([uv, "venv", "--python", "3.12", os.path.join(cache, "venv")],
                       check=True, capture_output=True, env=env)
    subprocess.run([uv, "pip", "install", "-p", vpy] + VENV_DEPS,
                   check=True, capture_output=True, env=env)
    # Marker written only AFTER a successful install (both subprocess.run calls
    # use check=True and raise on failure, so we never reach here on a partial
    # install). An existing venv + marker short-circuits at the top of the fn.
    open(marker, "w").write("ok")
    return vpy


def _worker_source():
    """The worker script, run in a separate uv venv where fused_render is not
    importable. Prepend _SHARED so the mount-safe byte helpers (_server_url,
    _RangeReader, _HttpRangeFile) are defined in the worker's namespace."""
    return _SHARED + "\n" + WORKER


def main(file: str = "", action: str = "analyze", resampling: str = "",
         profile: str = "", out: str = "", overwrite: str = "", src: str = ""):
    import json
    import os
    import subprocess
    import tempfile

    if not file:
        return {"error": "no file selected — pass a .tif/.tiff path"}
    file = os.path.abspath(os.path.expanduser(file))
    if os.path.splitext(file)[1].lower() not in (".tif", ".tiff"):
        return {"error": "the overview pyramid only reads .tif/.tiff files"}
    if action not in ("analyze", "build", "cogify", "predict", "status"):
        return {"error": f"unknown action: {action}"}

    # Remote detection REPLACES the os.path.isfile wedge-probe for mount files:
    # a cold negative isfile() on a mount forces rclone to LIST the whole parent
    # S3 prefix. One /api/fs/stat call (via the src origin — the template stays
    # mount-agnostic) says whether `file` is mount-backed and its size. When the
    # server is unreachable we presume local and fall back to the kernel probe.
    # 'status' only polls local job files, so it never asks about `file`.
    remote, remote_size = None, None
    if src and action != "status":
        stat_status, payload = _stat(src, file)
        if stat_status == "missing":
            return {"error": f"not a file: {file}"}
        if stat_status == "ok":
            # stat REPLACES os.path.isfile: a directory (is_dir, size null) is
            # not a file, so reject it here rather than spawning a worker with
            # size=None that would crash _HttpRangeFile on int(None).
            if payload.get("is_dir"):
                return {"error": f"not a file: {file}"}
            remote = bool(payload.get("remote"))
            remote_size = payload.get("size")
        # "unreachable" -> remote stays None -> local kernel fallback below
    if remote is None and action != "status":
        # local presumed (no src, or server unreachable): today's kernel probe,
        # unchanged. 'status' is excluded — it only reads a local job-status
        # file by key and never needs (or should kernel-probe) the input path.
        if not os.path.isfile(file):
            return {"error": f"not a file: {file}"}
    if remote and action in ("build", "cogify"):
        return {"error": "this file is on a read-only remote mount — "
                         "copy it locally to build overviews"}

    opts = {k: v for k, v in [("resampling", resampling), ("profile", profile),
                              ("out", out), ("overwrite", overwrite)] if v}
    if remote:
        # Hand the worker the raw-bytes URL (origin from src, path normalized
        # here) + size, so every rasterio/tifffile open goes over HTTP range
        # reads. Never a kernel open/mmap of a file the server called remote.
        opts["remote"] = True
        opts["raw_url"] = _server_url(src, "/api/fs/raw", file)
        if remote_size is not None:
            opts["size"] = remote_size

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    jobs = os.path.expanduser(f"~/.cache/{VENV_DIR_NAME}/jobs")
    import hashlib
    key = hashlib.md5(f"{file}|{action}|{opts.get('out', '')}".encode()).hexdigest()[:16]

    if action == "status":
        # poll a detached job: `out` carries the status_key returned by build/cogify
        if not out or not out.isalnum():
            return {"error": "status needs the job key in the 'out' param"}
        sf = os.path.join(jobs, out + ".json")
        if not os.path.exists(sf):
            return {"error": "no such job"}
        st = json.load(open(sf))
        if st.get("state") == "running":
            watch = st.get("watch") or file
            st["cur_size"] = os.path.getsize(watch) if os.path.exists(watch) else 0
            if st.get("pid") and not alive(st["pid"]):
                st["state"] = "error"
                st["error"] = "worker process died without reporting a result"
        return st

    try:
        vpy = _venv_python()
    except RuntimeError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"venv setup failed: {type(e).__name__}: {e}"}
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}

    if action in ("build", "cogify"):
        # too slow for the app's 30s runPython budget → detach and poll
        import time
        os.makedirs(jobs, exist_ok=True)
        sf = os.path.join(jobs, key + ".json")
        wfile = os.path.join(jobs, key + ".py")
        watch = opts.get("out") or (os.path.splitext(file)[0] + "_cog.tif"
                                    if action == "cogify" else file)
        if os.path.exists(sf):
            st = json.load(open(sf))
            if st.get("state") == "running" and st.get("pid") and alive(st["pid"]):
                return {"ok": True, "started": True, "already_running": True,
                        "status_key": key, "watch": watch}
        opts["status_file"] = sf
        open(wfile, "w").write(_worker_source())
        st = {"state": "running", "pid": None, "action": action, "watch": watch,
              "before": os.path.getsize(file), "started": time.time()}
        json.dump(st, open(sf, "w"))
        proc = subprocess.Popen([vpy, wfile, file, action, json.dumps(opts)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, env=env)
        st["pid"] = proc.pid
        json.dump(st, open(sf, "w"))
        return {"ok": True, "started": True, "status_key": key, "watch": watch,
                "before": st["before"]}

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(_worker_source())
        wpath = f.name
    try:
        proc = subprocess.run([vpy, wpath, file, action, json.dumps(opts)],
                              capture_output=True, text=True, timeout=900, env=env)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            return {"error": "worker failed: " + (tail[-1] if tail else "unknown error")}
        result = json.loads(proc.stdout)
    finally:
        os.remove(wpath)
    result["file"] = file
    return result


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass

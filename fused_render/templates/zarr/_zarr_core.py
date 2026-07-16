"""Zarr (v2) preview reader for fused-render.

Default engine is pure-Python and runs in the bundled interpreter: it parses the
store JSON (.zmetadata / .zgroup / .zarray / .zattrs), reads only the chunks
needed for the selected 2-D slice, and decompresses them with a built-in
Blosc(lz4/blosclz-raw) + zlib/gzip decoder — no zarr / numcodecs.

Codecs it can't do (zstd, blosc-zstd, bitshuffle, delta filters, zarr v3) raise
`Unsupported`; set engine="system" (UI checkbox) to shell out to a system Python
that has zarr, which handles everything.

Emits the same JSON schema as the NetCDF reader, so its viewer mirrors that one.
"""
# /// script
# dependencies = ["numpy"]
# ///

import json
import sys
import math
import os
import struct

import _grid_common as G

SPATIAL_Y = {"lat", "latitude", "y", "yc", "rlat", "nav_lat"}
SPATIAL_X = {"lon", "longitude", "x", "xc", "rlon", "nav_lon"}
COORD_NAMES = SPATIAL_Y | SPATIAL_X | {
    "time", "year", "valid_time", "step", "number", "depth", "level", "lev",
    "plev", "height", "band", "c", "z", "t", "channel",
}


class Unsupported(Exception):
    pass


# ------------------------------------------------------------ blosc + lz4 (pure)
def _lz4_block(src, dest_size):
    out = bytearray(); i = 0; n = len(src)
    while i < n:
        token = src[i]; i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1; lit += b
                if b != 255: break
        out += src[i:i+lit]; i += lit
        if len(out) >= dest_size: break
        offset = src[i] | (src[i+1] << 8); i += 2
        mlen = token & 15
        if mlen == 15:
            while True:
                b = src[i]; i += 1; mlen += b
                if b != 255: break
        mlen += 4
        start = len(out) - offset
        if offset >= mlen:
            out += out[start:start + mlen]
        else:
            # overlapping match repeats the trailing `offset` bytes as a pattern
            out += (bytes(out[start:]) * (mlen // offset + 1))[:mlen]
    return bytes(out[:dest_size])


def _unshuffle(buf, typesize):
    import numpy as np
    if typesize <= 1:
        return buf
    a = np.frombuffer(buf, dtype=np.uint8)
    m = len(a) // typesize
    return a[: m * typesize].reshape(typesize, m).T.reshape(-1).tobytes()


def _blosc(d, want=None):
    """Decompress a blosc frame. `want=(start, stop)` is a byte range of the
    decompressed buffer: blocks fully outside it are skipped (left zeroed),
    so slicing one plane out of a huge chunk only decodes ~1 block."""
    flags = d[2]; typesize = d[3]
    nbytes, blocksize, cbytes = struct.unpack_from("<III", d, 4)
    codec = flags >> 5            # 0 blosclz, 1 lz4, 3 zlib(via blosc), 4 zstd
    shuffle = flags & 1; memcpy = flags & 2
    if memcpy:
        return d[16:16 + nbytes]
    if codec not in (1,):         # only lz4 (covers xarray/zarr default)
        raise Unsupported(f"blosc codec id {codec} (only lz4 in pure engine)")
    nblocks = (nbytes + blocksize - 1) // blocksize
    offs = struct.unpack_from("<%di" % nblocks, d, 16)
    out = bytearray(nbytes)
    for bi in range(nblocks):
        b0 = bi * blocksize
        bsize = min(blocksize, nbytes - b0)
        if want and (b0 + bsize <= want[0] or b0 >= want[1]):
            continue
        leftover = bsize < blocksize
        cands = [typesize, 1] if (typesize > 1 and not leftover and bsize % typesize == 0) else [1]
        block = None
        for nstreams in cands:
            neblock = bsize // nstreams; c = offs[bi]; acc = bytearray(); ok = True
            try:
                for _ in range(nstreams):
                    csize = struct.unpack_from("<i", d, c)[0]; c += 4
                    if csize < 0 or c + csize > len(d): ok = False; break
                    piece = d[c:c + csize]; c += csize
                    dec = bytes(piece) if csize == neblock else _lz4_block(piece, neblock)
                    if len(dec) != neblock: ok = False; break
                    acc += dec
            except (IndexError, struct.error):
                ok = False
            if ok and len(acc) == bsize:
                block = acc; break
        if block is None:
            raise Unsupported("blosc block decode failed")
        if shuffle:
            block = _unshuffle(block, typesize)
        out[b0:b0 + bsize] = block
    return bytes(out)


def _decompress(raw, compressor, want=None):
    if compressor is None:
        return raw
    cid = compressor.get("id")
    if cid == "blosc":
        return _blosc(raw, want)
    if cid in ("zlib", "gzip"):
        import zlib
        return zlib.decompress(raw)
    raise Unsupported(f"zarr compressor '{cid}' (use system engine)")


# ------------------------------------------------------------ store metadata
def _load_meta(store):
    """Return {arrayname: {'zarray':..., 'zattrs':...}} + root attrs. Uses
    .zmetadata (consolidated) if present, else walks the directory tree."""
    if os.path.isfile(os.path.join(store, "zarr.json")):
        raise Unsupported("zarr v3 store (use system engine)")
    consolidated = os.path.join(store, ".zmetadata")
    arrays, root_attrs = {}, {}
    if os.path.isfile(consolidated):
        meta = json.load(open(consolidated))["metadata"]
        for k, v in meta.items():
            if k == ".zattrs":
                root_attrs = v
            elif k.endswith("/.zarray"):
                name = k[: -len("/.zarray")]
                arrays.setdefault(name, {})["zarray"] = v
            elif k.endswith("/.zattrs"):
                name = k[: -len("/.zattrs")]
                arrays.setdefault(name, {})["zattrs"] = v
        return arrays, root_attrs
    # walk
    ra = os.path.join(store, ".zattrs")
    if os.path.isfile(ra):
        root_attrs = json.load(open(ra))
    for dirpath, _, files in os.walk(store):
        if ".zarray" in files:
            name = os.path.relpath(dirpath, store).replace(os.sep, "/")
            za = json.load(open(os.path.join(dirpath, ".zarray")))
            at = {}
            if ".zattrs" in files:
                at = json.load(open(os.path.join(dirpath, ".zattrs")))
            arrays[name] = {"zarray": za, "zattrs": at}
    return arrays, root_attrs


def _dims_of(name, info):
    d = info.get("zattrs", {}).get("_ARRAY_DIMENSIONS")
    if d:
        return list(d)
    return [f"dim{i}" for i in range(len(info["zarray"]["shape"]))]


def _chunk_stats(store, name, za):
    """(chunk files present on disk, total expected) for one array."""
    total = 1
    for s, c in zip(za["shape"], za["chunks"]):
        total *= max(1, -(-int(s) // max(1, int(c))))   # ceil div
    present = 0
    try:
        for e in os.scandir(os.path.join(store, name)):
            if e.is_file() and not e.name.startswith("."):
                present += 1
    except OSError:
        present = None
    return present, total


def _store_summary(store):
    """Total bytes + file count on disk (walk capped by count AND wall time:
    on a remote mount every listing/stat is a network round-trip, so a large
    store would otherwise stall for minutes long before any file-count cap)."""
    import time
    size = files = 0
    deadline = time.time() + 2.0
    for dirpath, _, names in os.walk(store):
        for n in names:
            files += 1
            if files > 50000 or time.time() > deadline:
                return {"size": size, "files": files, "approx": True}
            try:
                size += os.path.getsize(os.path.join(dirpath, n))
            except OSError:
                pass
    return {"size": size, "files": files, "approx": False}


# ------------------------------------------------------------ chunk reading
def _read_full_1d(store, name, za):
    import numpy as np
    shape = za["shape"]; chunks = za["chunks"]; dt = np.dtype(za["dtype"])
    sep = za.get("dimension_separator", ".")
    comp = za.get("compressor")
    n = shape[0]; cn = chunks[0]
    out = np.zeros(n, dtype=dt)
    for ci in range((n + cn - 1) // cn):
        path = os.path.join(store, name, str(ci)) if sep == "." else os.path.join(store, name, str(ci))
        if not os.path.exists(path):
            continue
        raw = _decompress(open(path, "rb").read(), comp)
        arr = np.frombuffer(raw, dtype=dt)[:cn]
        s = ci * cn
        out[s:s + len(arr)] = arr[: n - s]
    return out


def _read_2d_slice(store, name, za, ypos, xpos, fixed):
    import numpy as np
    shape = za["shape"]; chunks = za["chunks"]; dt = np.dtype(za["dtype"])
    order = za.get("order", "C"); sep = za.get("dimension_separator", ".")
    comp = za.get("compressor"); fill = za.get("fill_value")
    ndim = len(shape)
    ny, nx = shape[ypos], shape[xpos]
    cy, cx = chunks[ypos], chunks[xpos]
    # absent chunk files == all fill_value in zarr; init to fill so they render
    # correctly (NaN only when fill is null/NaN).
    init = np.nan
    if isinstance(fill, (int, float)) and not (isinstance(fill, float) and math.isnan(fill)):
        init = float(fill)
    out = np.full((ny, nx), init, dtype="float64")
    ech = {d: fixed[d] // chunks[d] for d in fixed}
    eloc = {d: fixed[d] % chunks[d] for d in fixed}
    # byte range of the selected plane within a chunk — lets blosc skip blocks
    strides = [0] * ndim
    acc = 1
    for d in (range(ndim - 1, -1, -1) if order == "C" else range(ndim)):
        strides[d] = acc; acc *= chunks[d]
    lo = sum(strides[d] * eloc[d] for d in eloc)
    hi = lo + strides[ypos] * (chunks[ypos] - 1) + strides[xpos] * (chunks[xpos] - 1)
    want = (lo * dt.itemsize, (hi + 1) * dt.itemsize)
    for iy in range((ny + cy - 1) // cy):
        for ix in range((nx + cx - 1) // cx):
            coords = []
            for d in range(ndim):
                coords.append(iy if d == ypos else ix if d == xpos else ech[d])
            path = os.path.join(store, name, sep.join(str(c) for c in coords))
            if not os.path.exists(path):
                continue
            raw = _decompress(open(path, "rb").read(), comp, want)
            chunk = np.frombuffer(raw, dtype=dt).reshape(chunks, order=order)
            idx = [slice(None) if d in (ypos, xpos) else eloc[d] for d in range(ndim)]
            tile = chunk[tuple(idx)]
            if ypos > xpos:
                tile = tile.T
            y0, x0 = iy * cy, ix * cx
            vy, vx = min(cy, ny - y0), min(cx, nx - x0)
            out[y0:y0 + vy, x0:x0 + vx] = tile[:vy, :vx].astype("float64")
    # NB: zarr fill_value marks *uninitialised* regions, not nodata — so we do
    # NOT mask data==fill. Absent chunk files stay NaN (shown transparent).
    return out


# ------------------------------------------------------------ pure engine
def _read_pure(store, var, index, bins, max_cells):
    import numpy as np
    arrays, root_attrs = _load_meta(store)
    if not arrays:
        return {"error": "no zarr arrays found in store"}

    # dim sizes + classification
    dim_sizes = {}
    for name, info in arrays.items():
        dims = _dims_of(name, info)
        for d, s in zip(dims, info["zarray"]["shape"]):
            dim_sizes[d] = int(s)
    dim_names = set(dim_sizes)

    coord_names, band_names = [], []
    for name, info in arrays.items():
        nd = len(info["zarray"]["shape"])
        base = name.split("/")[-1]
        is_coord = (name in dim_names or base in dim_names) or (nd <= 1 and base.lower() in COORD_NAMES)
        (coord_names if is_coord else band_names).append(name)
    if not band_names:
        band_names = list(arrays)   # fall back: everything is data

    def kind(n):
        return np.dtype(arrays[n]["zarray"]["dtype"]).kind

    def slice_cells(n):
        shp = arrays[n]["zarray"]["shape"]
        return int(np.prod(shp[-2:] if len(shp) >= 2 else shp))
    if var in band_names:
        chosen = var
    else:
        fit = [n for n in band_names if slice_cells(n) <= 32_000_000]
        chosen = max(fit, key=lambda n: (kind(n) == "f", len(arrays[n]["zarray"]["shape"]),
                                         int(np.prod(arrays[n]["zarray"]["shape"])))) \
            if fit else min(band_names, key=slice_cells)
    if slice_cells(chosen) > 256_000_000:
        return {"error": f"slice of '{chosen}' is {slice_cells(chosen) // 1000000}M "
                         "cells - too large to load; pick a smaller variable"}

    info = arrays[chosen]
    za = info["zarray"]
    dims = _dims_of(chosen, info)
    shape = za["shape"]

    ydim = next((d for d in dims if d.split("/")[-1].lower() in SPATIAL_Y), None)
    xdim = next((d for d in dims if d.split("/")[-1].lower() in SPATIAL_X), None)
    if not (ydim and xdim):
        ydim, xdim = dims[-2], dims[-1]
    ypos, xpos = dims.index(ydim), dims.index(xdim)

    extra = [d for d in dims if d not in (ydim, xdim)]
    # coordinate arrays (by matching dim name)
    def _coord_array(dim):
        for n in coord_names:
            if n == dim or n.split("/")[-1] == dim:
                return n
        return None

    fixed = {}
    extra_dims = []
    for d in extra:
        pos = dims.index(d)
        size = shape[pos]
        idx = max(0, min(index, size - 1)) if d == extra[0] else 0
        fixed[pos] = idx
        ca = _coord_array(d)
        vals = None
        if ca and int(np.prod(arrays[ca]["zarray"]["shape"])) <= 200:
            vals = [G.clean(x) for x in _read_full_1d(store, ca, arrays[ca]["zarray"])]
        extra_dims.append({"name": d, "size": int(size), "values": vals,
                           "index": idx if d == extra[0] else 0})

    arr = _read_2d_slice(store, chosen, za, ypos, xpos, fixed)

    # coordinate values for lat/lon axes
    def _axis_vals(dim):
        ca = _coord_array(dim)
        if ca:
            return [G.clean(x) for x in _read_full_1d(store, ca, arrays[ca]["zarray"])]
        return None
    lats = _axis_vals(ydim)
    lons = _axis_vals(xdim)

    payload = G.present(arr, lats, lons, bins, max_cells)

    # metadata tables
    coords_meta = []
    for n in coord_names:
        zc = arrays[n]["zarray"]
        vals = _read_full_1d(store, n, zc) if int(np.prod(zc["shape"])) <= 5000 else None
        arrv = np.asarray(vals) if vals is not None else None
        coords_meta.append({
            "name": n, "dims": _dims_of(n, arrays[n]), "size": int(np.prod(zc["shape"])),
            "dtype": str(np.dtype(zc["dtype"])),
            "min": G.clean(np.nanmin(arrv)) if arrv is not None and arrv.size else None,
            "max": G.clean(np.nanmax(arrv)) if arrv is not None and arrv.size else None,
            "values": [G.clean(x) for x in arrv] if (arrv is not None and arrv.size <= 200) else None,
            "attrs": {k: G.clean(v) for k, v in arrays[n].get("zattrs", {}).items() if k != "_ARRAY_DIMENSIONS"},
        })
    vars_meta = []
    for n in band_names:
        zan = arrays[n]["zarray"]
        present, total = _chunk_stats(store, n, zan)
        filters = zan.get("filters") or []
        vars_meta.append({
            "name": n, "dims": _dims_of(n, arrays[n]),
            "shape": [int(s) for s in zan["shape"]],
            "dtype": str(np.dtype(zan["dtype"])),
            "attrs": {k: G.clean(v) for k, v in arrays[n].get("zattrs", {}).items() if k != "_ARRAY_DIMENSIONS"},
            "compressor": (zan.get("compressor") or {}).get("id") or "none",
            "chunks": [int(c) for c in zan["chunks"]],
            "chunks_stored": present, "chunks_total": total,
            "fill_value": G.clean(zan.get("fill_value")),
            "order": zan.get("order", "C"),
            "filters": [f.get("id", "?") for f in filters],
        })

    ll = _lonlat_extent(lats, lons)
    out = {
        "engine": "pure", "dims": dim_sizes, "coords": coords_meta,
        "variables": vars_meta, "bands": band_names,
        "global_attrs": {k: G.clean(v) for k, v in root_attrs.items()},
        "selected": {
            "var": chosen, "index": fixed.get(dims.index(extra[0]), 0) if extra else 0,
            "ydim": ydim, "xdim": xdim, "extra_dims": extra_dims,
            "units": info.get("zattrs", {}).get("units", ""),
            "long_name": info.get("zattrs", {}).get("long_name", chosen),
        },
    }
    out.update(payload)
    out["grid"]["extent"] = ll
    return out


def _lonlat_extent(lats, lons):
    def mm(v):
        if not v:
            return (None, None)
        vv = [x for x in v if x is not None]
        return (min(vv), max(vv)) if vv else (None, None)
    ymin, ymax = mm(lats); xmin, xmax = mm(lons)
    return {"ymin": ymin, "ymax": ymax, "xmin": xmin, "xmax": xmax}


# ------------------------------------------------------------ system engine
def _system_python():
    import shutil
    import subprocess
    cands = []
    if os.environ.get("GEO_PYTHON"):
        cands.append(os.environ["GEO_PYTHON"])
    # when running inside the grid daemon its venv ships zarr (DAEMON_DEPS),
    # so the current interpreter is normally the one found here
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
            r = subprocess.run([c, "-c", "import zarr"], capture_output=True, timeout=15, env=env)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


_WORKER = r'''
import sys, os, json
sys.path.insert(0, sys.argv[1])
import numpy as np, zarr
import _grid_common as G

p = json.loads(sys.stdin.read())
root = zarr.open(p["store"], mode="r")
BUDGET = 32_000_000     # target cells for an auto-picked 2D slice
HARD_CAP = 256_000_000  # refuse (error, don't OOM) above this
# Multiscale stores: pick the finest pyramid level that fits the budget from
# the root attrs alone and walk ONLY that subgroup — walking every level costs
# a metadata round-trip per node (slow on remote mounts) and pick-by-size
# would grab the native-resolution level (can be trillions of cells).
asset = None
ms = dict(getattr(root, "attrs", {})).get("multiscales")
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
    try:
        for k, v in node.arrays(): arrays[prefix + k] = v
        for k, v in node.groups(): walk(v, prefix + k + "/")
    except AttributeError:
        arrays[prefix.rstrip("/") or node.basename or "array"] = node
if asset is not None: walk(root[asset], asset + "/")
else: walk(root)
SY={"lat","latitude","y"}; SX={"lon","longitude","x"}
def dims_of(a):
    d = a.attrs.get("_ARRAY_DIMENSIONS")
    if not d:
        d = getattr(getattr(a, "metadata", None), "dimension_names", None)
        if d and not all(d): d = None
    return list(d) if d else [f"dim{i}" for i in range(a.ndim)]
dim_sizes={}
for n,a in arrays.items():
    for d,s in zip(dims_of(a), a.shape): dim_sizes[d]=int(s)
coord_names=[n for n,a in arrays.items() if (n in dim_sizes or n.split("/")[-1] in dim_sizes) and a.ndim<=1]
band_names=[n for n in arrays if n not in coord_names] or list(arrays)
def slice_cells(n):
    a = arrays[n]
    return int(np.prod(a.shape[-2:])) if a.ndim >= 2 else int(np.prod(a.shape))
if p["var"] in band_names:
    chosen = p["var"]
else:
    fit = [n for n in band_names if slice_cells(n) <= BUDGET]
    chosen = max(fit, key=lambda n:(arrays[n].dtype.kind=="f", arrays[n].ndim, arrays[n].size)) \
        if fit else min(band_names, key=slice_cells)
if slice_cells(chosen) > HARD_CAP:
    print(json.dumps({"error": "slice of '%s' is %dM cells - too large to load; pick a coarser overview level or smaller variable" % (chosen, slice_cells(chosen) // 1000000)})); sys.exit(0)
a=arrays[chosen]; dims=dims_of(a)
ydim=next((d for d in dims if d.split("/")[-1].lower() in SY), None)
xdim=next((d for d in dims if d.split("/")[-1].lower() in SX), None)
if not (ydim and xdim): ydim,xdim=dims[-2],dims[-1]
ypos,xpos=dims.index(ydim),dims.index(xdim)
extra=[d for d in dims if d not in (ydim,xdim)]
sl=[slice(None)]*a.ndim
extra_dims=[]
def coord_for(dim):
    for n in coord_names:
        if n==dim or n.split("/")[-1]==dim: return n
    return None
for d in extra:
    pos=dims.index(d); size=a.shape[pos]
    idx=max(0,min(p["index"],size-1)) if d==extra[0] else 0
    sl[pos]=idx
    ca=coord_for(d); vals=None
    if ca and arrays[ca].size<=200: vals=[G.clean(x) for x in arrays[ca][:]]
    extra_dims.append({"name":d,"size":int(size),"values":vals,"index": idx if d==extra[0] else 0})
# data + coords live in separate files: overlap the reads (remote mounts pay
# a network round-trip per file, so serial reads add up)
from concurrent.futures import ThreadPoolExecutor
def _coordvals(dim):
    ca = coord_for(dim)
    return [G.clean(x) for x in arrays[ca][:]] if ca else None
with ThreadPoolExecutor(3) as _ex:
    _fd = _ex.submit(lambda: np.asarray(a[tuple(sl)], dtype="float64"))
    _fy = _ex.submit(_coordvals, ydim); _fx = _ex.submit(_coordvals, xdim)
    arr = _fd.result(); lats = _fy.result(); lons = _fx.result()
if ypos>xpos: arr=arr.T
payload=G.present(arr, lats, lons, p["bins"], p["max_cells"])
coords_meta=[]
for n in coord_names:
    c=arrays[n]; v=c[:] if c.size<=5000 else None
    coords_meta.append({"name":n,"dims":dims_of(c),"size":int(c.size),"dtype":str(c.dtype),
        "min":G.clean(np.nanmin(v)) if v is not None and v.size else None,
        "max":G.clean(np.nanmax(v)) if v is not None and v.size else None,
        "values":[G.clean(x) for x in v] if (v is not None and v.size<=200) else None,
        "attrs":{k:G.clean(vv) for k,vv in dict(c.attrs).items() if k!="_ARRAY_DIMENSIONS"}})
# zarr-python 3: Array.compressor RAISES for v3 arrays (use .compressors),
# filters codecs have no codec_id, and nchunks_initialized lists the whole
# store (deadly over a remote mount) — read all of these defensively.
def _codec_name(c):
    return str(getattr(c, "codec_id", None) or type(c).__name__)
def _comp_of(a):
    try:
        cs = getattr(a, "compressors", None)
        # v3 exposes .compressors (an empty tuple means uncompressed, not
        # "unknown"); only fall back to the v2 .compressor when the attr is
        # absent, since reading .compressor RAISES on a v3 array.
        if cs is not None:
            return ",".join(_codec_name(c) for c in cs) if cs else "none"
        c = a.compressor
        return _codec_name(c) if c else "none"
    except Exception:
        return "unknown"
def _filt_of(a):
    try:
        return [_codec_name(f) for f in (a.filters or [])]
    except Exception:
        return []
def _stored_of(a):
    try:
        return int(a.nchunks_initialized) if int(a.nchunks) <= 1024 else -1
    except Exception:
        return -1
vars_meta=[{"name":n,"dims":dims_of(arrays[n]),"shape":[int(s) for s in arrays[n].shape],
    "dtype":str(arrays[n].dtype),
    "attrs":{k:G.clean(vv) for k,vv in dict(arrays[n].attrs).items() if k!="_ARRAY_DIMENSIONS"},
    "compressor": _comp_of(arrays[n]),
    "chunks": [int(c) for c in (arrays[n].chunks or [])],
    "chunks_stored": _stored_of(arrays[n]),
    "chunks_total": int(getattr(arrays[n], "nchunks", 0)),
    "fill_value": G.clean(arrays[n].fill_value),
    "order": str(getattr(arrays[n], "order", "C")),
    "filters": _filt_of(arrays[n])} for n in band_names]
def mm(v):
    vv=[x for x in (v or []) if x is not None]
    return (min(vv),max(vv)) if vv else (None,None)
ymin,ymax=mm(lats); xmin,xmax=mm(lons)
out={"engine":"system (zarr)","dims":dim_sizes,"coords":coords_meta,"variables":vars_meta,
    "bands":band_names,"global_attrs":{k:G.clean(v) for k,v in dict(root.attrs).items()},
    "selected":{"var":chosen,"index": sl[dims.index(extra[0])] if extra else 0,"ydim":ydim,"xdim":xdim,
        "extra_dims":extra_dims,"units":a.attrs.get("units",""),"long_name":a.attrs.get("long_name",chosen)}}
out.update(payload)
out["grid"]["extent"]={"ymin":ymin,"ymax":ymax,"xmin":xmin,"xmax":xmax}
print(json.dumps(out))
'''


def _read_system(store, var, index, bins, max_cells):
    import subprocess
    py = _system_python()
    if not py:
        return {"error": "system engine requested but no Python with zarr was found. "
                         "Set GEO_PYTHON or install zarr."}
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    here = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
            else os.path.abspath(sys.path[0]))  # runner exec()s without __file__
    params = {"store": store, "var": var, "index": index, "bins": bins, "max_cells": max_cells}
    r = subprocess.run([py, "-c", _WORKER, here], input=json.dumps(params),
                       capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        return {"error": f"system engine failed: {r.stderr[-500:]}"}
    return json.loads(r.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------ entry point
def main(file: str = "", var: str = "", index: int = 0,
         bins: int = 40, max_cells: int = 160000, engine: str = "auto"):
    # New runner passes params untyped (HTML sends them as strings); coerce the
    # numeric ones the old runner used to convert via the annotations.
    index, bins, max_cells = int(index), int(bins), int(max_cells)
    if not file:
        return {"error": "no store selected"}
    store = os.path.abspath(os.path.expanduser(file))
    if not os.path.isdir(store):
        return {"error": f"not a zarr store (directory): {store}"}

    if engine == "system":
        out = _read_system(store, var, index, bins, max_cells)
        return _finish(out, store)
    try:
        out = _read_pure(store, var, index, bins, max_cells)
    except Unsupported as e:
        if engine == "auto":
            sysout = _read_system(store, var, index, bins, max_cells)
            if "error" not in sysout:
                sysout.setdefault("note", f"pure engine: {e}")
                return _finish(sysout, store)
        return {"error": str(e), "hint": "enable the system engine (zarr) for this store"}
    return _finish(out, store)


def _finish(out, store):
    if "error" in out:
        return out
    out["file"] = store
    s = _store_summary(store)
    s["consolidated"] = os.path.isfile(os.path.join(store, ".zmetadata"))
    s["zarr_format"] = 3 if os.path.isfile(os.path.join(store, "zarr.json")) else 2
    out["store"] = s
    return out


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass

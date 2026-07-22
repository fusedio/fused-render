"""GeoTIFF preview reader for fused-render.

Default engine is a pure-Python TIFF decoder that runs in the bundled
interpreter (stdlib zlib + numpy) — no rasterio/GDAL. It handles the common
GeoTIFF shapes: stripped or tiled, uncompressed or Deflate, predictor 1/2,
planar or chunky, uint8/uint16/int16/uint32/float32/float64, multi-band, plus
GeoTIFF georeferencing tags -> EPSG / transform / bounds.

Anything the pure path can't do (LZW/JPEG compression, odd layouts) raises
`Unsupported`; set engine="system" (a UI checkbox) to shell out to a system
Python that has rasterio, which handles everything.

Presentation (grid/stats/histogram/RGB) is shared via _raster_common so both
engines return an identical JSON schema.
"""
# /// script
# dependencies = ["numpy", "pyproj", "imagecodecs"]
# ///

import os
import struct
import sys

import _raster_common as C


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------- TIFF parsing
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _read_ifd(buf, en, off=None):
    """Parse one IFD at `off` (default: first) -> ({tag: (type, [values])}, next_ifd_off)."""
    if off is None:
        off = struct.unpack_from(en + "I", buf, 4)[0]
    n = struct.unpack_from(en + "H", buf, off)[0]
    tags = {}
    for i in range(n):
        base = off + 2 + i * 12
        tag, typ, count = struct.unpack_from(en + "HHI", buf, base)
        size = _TYPE_SIZE.get(typ, 1) * count
        voff = base + 8 if size <= 4 else struct.unpack_from(en + "I", buf, base + 8)[0]
        tags[tag] = (typ, _read_values(buf, en, typ, count, voff))
    next_off = struct.unpack_from(en + "I", buf, off + 2 + n * 12)[0]
    return tags, next_off


def _overview_levels(buf, en, first_next):
    """Sizes of subsequent IFDs (GDAL stores overviews as extra IFDs)."""
    levels, off, hops = [], first_next, 0
    while off and hops < 32:
        try:
            t, off = _read_ifd(buf, en, off)
        except (struct.error, IndexError):
            break
        w, h = _v1(t, 256), _v1(t, 257)
        if w and h:
            levels.append([int(w), int(h)])
        hops += 1
    return levels


def _read_values(buf, en, typ, count, off):
    if typ == 2:  # ASCII
        return buf[off : off + count].split(b"\x00")[0].decode("latin-1", "replace")
    fmt = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d"}.get(typ)
    if typ == 5:  # RATIONAL
        vals = struct.unpack_from(en + "%dI" % (count * 2), buf, off)
        return [vals[i] / vals[i + 1] if vals[i + 1] else 0.0 for i in range(0, len(vals), 2)]
    if not fmt:
        return list(buf[off : off + count])
    return list(struct.unpack_from(en + fmt * count, buf, off))


def _v(tags, tag, default=None):
    return tags[tag][1] if tag in tags else default


def _v1(tags, tag, default=None):
    x = _v(tags, tag)
    return x[0] if isinstance(x, list) and x else (x if x is not None else default)


def _decompress(raw, comp):
    if comp == 1:
        return raw
    if comp in (8, 32946):  # Deflate / zlib
        import zlib

        return zlib.decompress(raw)
    try:  # LZW / packbits / zstd via imagecodecs
        import imagecodecs as IC

        if comp == 5:
            return IC.lzw_decode(bytes(raw))
        if comp == 32773:
            return IC.packbits_decode(bytes(raw))
        if comp == 50000:
            return IC.zstd_decode(bytes(raw))
    except ImportError:
        pass
    raise Unsupported(f"TIFF compression {comp} (use system engine)")


def _numpy_dtype(sample_format, bits, en):
    import numpy as np

    kind = {1: "u", 2: "i", 3: "f"}.get(sample_format, "u")
    nb = bits // 8
    return np.dtype(("<" if en == "<" else ">") + kind + str(nb))


def _unpredict(a, predictor):
    """Horizontal differencing predictor (2) -> cumulative sum along columns."""
    import numpy as np

    if predictor == 2:
        return np.cumsum(a, axis=1, dtype=a.dtype)
    return a


_COMPRESSION = {
    1: "none",
    2: "CCITT RLE",
    5: "LZW",
    6: "JPEG (old)",
    7: "JPEG",
    8: "deflate",
    32946: "deflate",
    32773: "packbits",
    34712: "JPEG2000",
    34887: "LERC",
    50000: "zstd",
    50001: "webp",
}
_PHOTOMETRIC = {
    0: "min-is-white",
    1: "min-is-black",
    2: "RGB",
    3: "palette",
    4: "mask",
    5: "CMYK",
    6: "YCbCr",
}


def parse_header(path):
    """Read the file + parse the first IFD. Works for ANY compression —
    header metadata is available even when pixels can't be decoded purely."""
    import mmap

    with open(path, "rb") as f:
        # mmap, not read(): header + tile decode only touch the pages they
        # use, so a 300MB COG opens as fast as a 3MB one
        buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    if buf[:2] == b"II":
        en = "<"
    elif buf[:2] == b"MM":
        en = ">"
    else:
        raise Unsupported("not a TIFF (bad byte order mark)")
    if struct.unpack_from(en + "H", buf, 2)[0] == 43:
        raise Unsupported("BigTIFF (use system engine)")
    t, next_off = _read_ifd(buf, en)
    return buf, en, t, next_off


def _gdal_metadata(t):
    """Parse the GDAL_METADATA XML tag (42112) -> {name: value} (band-scoped
    names get a 'band N: ' prefix; sample index is 0-based in the XML)."""
    import re

    raw = _v(t, 42112)
    if not isinstance(raw, str) or "<" not in raw:
        return {}
    items = {}
    for m in re.finditer(r"<Item\s+([^>]*)>(.*?)</Item>", raw, re.S):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        name = attrs.get("name", "?")
        if "sample" in attrs:
            name = f"band {int(attrs['sample']) + 1}: {name}"
        items[name] = m.group(2).strip()
    return items


def header_meta(path, buf, en, t, next_off):
    """gdalinfo-style header metadata from parsed tags (no pixel decode)."""
    W = _v1(t, 256)
    H = _v1(t, 257)
    spp = _v1(t, 277, 1)
    bits = _v(t, 258, [8])
    bits = bits[0] if isinstance(bits, list) else bits
    comp = _v1(t, 259, 1)
    sf = _v(t, 339, [1])
    sf = sf[0] if isinstance(sf, list) else sf
    kind = {1: "uint", 2: "int", 3: "float"}.get(sf, "uint")

    if 322 in t:
        layout = f"tiled {_v1(t, 322)}×{_v1(t, 323)}"
    else:
        rps = _v1(t, 278, H)
        layout = f"stripped ({rps} rows/strip)"

    meta = _geo_meta(t)
    meta.update(
        {
            "width": int(W) if W else None,
            "height": int(H) if H else None,
            "count": int(spp),
            "dtype": f"{kind}{bits}",
            "nodata": _nodata(t),
            "file_size": os.path.getsize(path),
            "tiff": {
                "compression": _COMPRESSION.get(comp, f"code {comp}"),
                "layout": layout,
                "photometric": _PHOTOMETRIC.get(_v1(t, 262), None),
                "predictor": {2: "horizontal", 3: "float"}.get(_v1(t, 317, 1)),
                "byte_order": "little-endian" if en == "<" else "big-endian",
                "overviews": _overview_levels(buf, en, next_off),
            },
            "tags": {
                k: v
                for k, v in {
                    "description": _v(t, 270),
                    "software": _v(t, 305),
                    "datetime": _v(t, 306),
                    "artist": _v(t, 315),
                    "copyright": _v(t, 33432),
                }.items()
                if v
            },
            "gdal_metadata": _gdal_metadata(t),
        }
    )
    # band descriptions live in GDAL_METADATA <Item name="DESCRIPTION" sample=N>
    gm = meta["gdal_metadata"]
    meta["descriptions"] = [
        gm.get(f"band {i + 1}: DESCRIPTION", f"band {i + 1}") for i in range(int(spp))
    ]
    return meta


def _pick_ifd(buf, en, t0, next_off, full_w, full_h, win, max_cells):
    """Pick the smallest IFD (main image or GDAL overview) that still covers
    the zoom window with >= max_cells pixels — QGIS-style pyramid reads, so
    a 10980x10980 COG rendered at 400x400 decodes a ~686x686 overview."""
    wsel = C.parse_win(win, full_w, full_h)
    frac = 1.0
    if wsel:
        x0, y0, x1, y1 = wsel
        frac = max((x1 - x0) * (y1 - y0) / float(full_w * full_h), 1e-9)
    best = (t0, full_w, full_h)
    off, hops = next_off, 0
    while off and hops < 32:
        try:
            t, off = _read_ifd(buf, en, off)
        except (struct.error, IndexError):
            break
        w, h = _v1(t, 256), _v1(t, 257)
        # skip masks/odd IFDs: must be same band count and plain reduced image
        if w and h and _v1(t, 277, 1) == _v1(t0, 277, 1) and w < best[1]:
            if w * h * frac >= max_cells:
                best = (t, int(w), int(h))
        hops += 1
    return best


def read_tiff(path, win="", max_cells=0):
    """Pure-Python decode -> (bands [count,H,W], meta, wsel). When `win` names
    a zoom window, only the tiles/strips intersecting it are decompressed —
    pixels outside the window stay zero and are sliced away by the caller.
    When max_cells > 0, decodes from the smallest sufficient overview IFD."""
    import numpy as np

    buf, en, t0, next_off = parse_header(path)
    full_w = _v1(t0, 256)
    full_h = _v1(t0, 257)
    t = t0
    if max_cells:
        t, _, _ = _pick_ifd(buf, en, t0, next_off, full_w, full_h, win, max_cells)
    W = _v1(t, 256)
    H = _v1(t, 257)
    spp = _v1(t, 277, 1)
    bits = _v(t, 258, [8])
    bits = bits[0] if isinstance(bits, list) else bits
    comp = _v1(t, 259, 1)
    predictor = _v1(t, 317, 1)
    planar = _v1(t, 284, 1)
    sf = _v(t, 339, [1])
    sf = sf[0] if isinstance(sf, list) else sf
    dt = _numpy_dtype(sf, bits, en)

    wsel = C.parse_win(win, W, H)
    wx0, wy0, wx1, wy1 = wsel if wsel else (0, 0, W, H)

    tiled = 322 in t
    out = np.zeros((spp, H, W), dtype=dt)

    if tiled:
        tw, th = _v1(t, 322), _v1(t, 323)
        offs, counts = _v(t, 324), _v(t, 325)
        across = (W + tw - 1) // tw
        down = (H + th - 1) // th
        per_plane = across * down
        planes = spp if planar == 2 else 1
        for p in range(planes):
            for ty in range(wy0 // th, (wy1 - 1) // th + 1):
                for tx in range(wx0 // tw, (wx1 - 1) // tw + 1):
                    ti = p * per_plane + ty * across + tx
                    data = _decompress(buf[offs[ti] : offs[ti] + counts[ti]], comp)
                    samp = 1 if planar == 2 else spp
                    tile = np.frombuffer(data, dtype=dt)[: th * tw * samp].reshape(th, tw, samp)
                    tile = _unpredict(tile, predictor)
                    y0, x0 = ty * th, tx * tw
                    y1, x1 = min(y0 + th, H), min(x0 + tw, W)
                    if planar == 2:
                        out[p, y0:y1, x0:x1] = tile[: y1 - y0, : x1 - x0, 0]
                    else:
                        for s in range(spp):
                            out[s, y0:y1, x0:x1] = tile[: y1 - y0, : x1 - x0, s]
    else:
        rps = _v1(t, 278, H)
        offs, counts = _v(t, 273), _v(t, 279)
        nstrips_plane = (H + rps - 1) // rps
        planes = spp if planar == 2 else 1
        for p in range(planes):
            for si in range(wy0 // rps, (wy1 - 1) // rps + 1):
                gi = p * nstrips_plane + si
                data = _decompress(buf[offs[gi] : offs[gi] + counts[gi]], comp)
                y0 = si * rps
                rows = min(rps, H - y0)
                samp = 1 if planar == 2 else spp
                strip = np.frombuffer(data, dtype=dt)[: rows * W * samp].reshape(rows, W, samp)
                strip = _unpredict(strip, predictor)
                if planar == 2:
                    out[p, y0 : y0 + rows, :] = strip[:, :, 0]
                else:
                    for s in range(spp):
                        out[s, y0 : y0 + rows, :] = strip[:, :, s]

    meta = header_meta(path, buf, en, t0, next_off)
    meta["dtype"] = str(dt).lstrip("<>|")
    meta["engine"] = "pure"
    if t is not t0:
        # decoded an overview: pixel size grows by the reduction factor so
        # win_bounds/geo stay correct against overview pixel coords
        if meta.get("transform"):
            a, b_, c, d, e, f = meta["transform"]
            meta["transform"] = [a * full_w / W, b_, c, d, e * full_h / H, f]
        meta["decode_size"] = [W, H]
        meta["engine"] = f"pure (overview {W}×{H})"
    return out, meta, wsel


def _nodata(t):
    raw = _v(t, 42113)  # GDAL_NODATA (ASCII)
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _geo_meta(t):
    """EPSG + affine transform + native bounds from GeoTIFF tags."""
    epsg = None
    area_or_point = citation = None
    gk = _v(t, 34735)  # GeoKeyDirectory
    if gk and len(gk) >= 4:
        nkeys = gk[3]
        ascii_params = _v(t, 34737) or ""
        keys = {}
        for i in range(nkeys):
            b = 4 + i * 4
            if b + 3 < len(gk):
                kid, loc, cnt, val = gk[b], gk[b + 1], gk[b + 2], gk[b + 3]
                if loc == 0:  # value stored inline
                    keys[kid] = val
                elif loc == 34737:  # substring of GeoAsciiParams
                    keys[kid] = ascii_params[val : val + cnt].rstrip("|").strip()
        epsg = keys.get(3072) or keys.get(2048)  # projected, else geographic
        area_or_point = {1: "Area", 2: "Point"}.get(keys.get(1025))
        citation = keys.get(1026) or keys.get(2049)  # GT, else geographic

    scale = _v(t, 33550)  # ModelPixelScale (sx, sy, sz)
    tie = _v(t, 33922)  # ModelTiepoint (i, j, k, x, y, z)
    mt = _v(t, 34264)  # ModelTransformation (4x4)
    W = _v1(t, 256)
    H = _v1(t, 257)
    transform = bounds = None
    if scale and tie and len(scale) >= 2 and len(tie) >= 6:
        sx, sy = scale[0], scale[1]
        i, j, x0, y0 = tie[0], tie[1], tie[3], tie[4]
        c = x0 - i * sx
        f = y0 + j * sy
        transform = [sx, 0.0, c, 0.0, -sy, f]
    elif mt and len(mt) >= 16:
        transform = [mt[0], mt[1], mt[3], mt[4], mt[5], mt[7]]
    if transform and W and H:
        a, b_, c, d, e, f = transform
        xs = [c, c + a * W, c + a * W + b_ * H, c + b_ * H]
        ys = [f, f + d * W, f + d * W + e * H, f + e * H]
        bounds = {"left": min(xs), "right": max(xs), "top": max(ys), "bottom": min(ys)}
    return {
        "crs": ({"epsg": int(epsg)} if isinstance(epsg, int) and epsg else None),
        "transform": transform,
        "bounds": bounds,
        "area_or_point": area_or_point,
        "citation": citation,
    }


# ---------------------------------------------------------------- system engine
def _system_python():
    import shutil
    import subprocess

    cands = []
    if os.environ.get("GEO_PYTHON"):
        cands.append(os.environ["GEO_PYTHON"])
    home = os.path.expanduser("~")
    cands += [
        os.path.join(home, p)
        for p in ("miniforge3/bin/python", "miniconda3/bin/python", "anaconda3/bin/python")
    ]
    for name in ("python3", "python"):
        w = shutil.which(name)
        if w:
            cands.append(w)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    seen = set()
    for c in cands:
        rc = os.path.realpath(c)
        if rc in seen or not os.path.exists(rc):
            continue
        seen.add(rc)
        try:
            r = subprocess.run(
                [rc, "-c", "import rasterio"], capture_output=True, timeout=15, env=env
            )
            if r.returncode == 0:
                return rc
        except Exception:
            continue
    return None


_WORKER = r"""
import sys, os, json
sys.path.insert(0, sys.argv[1])
import numpy as np, rasterio
import _raster_common as C

p = json.loads(sys.stdin.read())
with rasterio.open(p["file"]) as ds:
    bounds = {"left": ds.bounds.left, "right": ds.bounds.right,
              "top": ds.bounds.top, "bottom": ds.bounds.bottom}
    epsg = ds.crs.to_epsg() if ds.crs else None
    prof = ds.profile
    gtags = ds.tags()
    ovr = ds.overviews(1)
    layout = ("tiled %dx%d" % (prof.get("blockxsize"), prof.get("blockysize"))
              if prof.get("tiled") else "stripped")
    meta = {"width": ds.width, "height": ds.height, "count": ds.count,
            "dtype": str(ds.dtypes[0]), "engine": "system (rasterio)",
            "nodata": ds.nodata, "bounds": bounds,
            "crs": ({"epsg": epsg} if epsg else None),
            "transform": list(ds.transform)[:6],
            "descriptions": [ds.descriptions[i] or f"band {i+1}" for i in range(ds.count)],
            "file_size": os.path.getsize(p["file"]),
            "area_or_point": gtags.get("AREA_OR_POINT"),
            "citation": None,
            "tiff": {"compression": (str(ds.compression.value) if ds.compression else "none"),
                     "layout": layout,
                     "photometric": prof.get("photometric"),
                     "predictor": None,
                     "byte_order": None,
                     "overviews": [[max(1, ds.width // f), max(1, ds.height // f)] for f in ovr]},
            "tags": {k: v for k, v in {
                "description": gtags.get("TIFFTAG_IMAGEDESCRIPTION"),
                "software": gtags.get("TIFFTAG_SOFTWARE"),
                "datetime": gtags.get("TIFFTAG_DATETIME")}.items() if v},
            "gdal_metadata": {k: v for k, v in gtags.items()
                              if not k.startswith("TIFFTAG_") and k != "AREA_OR_POINT"}}
    out = dict(meta)
    out["crs_wkt"] = ds.crs.to_wkt(version="WKT2_2019") if ds.crs else None
    binfo = []
    for i in range(1, ds.count + 1):
        bt = ds.tags(i)
        ci = str(ds.colorinterp[i - 1].name) if ds.colorinterp else None
        mn = bt.get("STATISTICS_MINIMUM"); mx = bt.get("STATISTICS_MAXIMUM")
        binfo.append({"band": i, "description": ds.descriptions[i - 1] or f"band {i}",
                      "colorinterp": ci,
                      "min": float(mn) if mn is not None else None,
                      "max": float(mx) if mx is not None else None})
    out["band_info"] = binfo
    try:
        from pyproj import CRS as _CRS
        out["crs_name"] = _CRS.from_epsg(epsg).name if epsg else None
    except Exception:
        out["crs_name"] = ds.crs.to_string() if ds.crs else None
    out["lonlat_bounds"] = C.lonlat_bounds(bounds, epsg)
    wsel = C.parse_win(p.get("win", ""), ds.width, ds.height)
    x0, y0, x1, y1 = wsel if wsel else (0, 0, ds.width, ds.height)
    vmeta = meta
    if wsel:
        vmeta = dict(meta)
        wb = C.win_bounds(meta["transform"], x0, y0, x1, y1)
        if wb: vmeta["bounds"] = wb
        out["window"] = {"px": [x0, y0, x1, y1], "full": [ds.width, ds.height]}
    # windowed + decimated read: GDAL picks the right overview level itself,
    # so zoomed-out views of huge rasters never read full resolution
    from rasterio.windows import Window
    w = Window.from_slices((y0, y1), (x0, x1))
    h, wd = y1 - y0, x1 - x0
    scale = max(1.0, (h * wd / float(p["max_cells"])) ** 0.5)
    oshape = (max(1, int(h / scale)), max(1, int(wd / scale)))
    want_rgb = (p["mode"] == "rgb") or (p["mode"] == "auto" and ds.count >= 3)
    if want_rgb and ds.count >= 3:
        bands = ds.read(window=w, out_shape=(ds.count,) + oshape).astype("float64")
        out["rgb"] = C.build_rgb(bands, p["idx"], vmeta, p["max_cells"])
        out["rgb"]["hists"] = C.channel_hists(bands, p["idx"], p["bins"])
        out["mode"] = "rgb"
    else:
        band = ds.read(p["band"], window=w, out_shape=oshape).astype("float64")
        if ds.nodata is not None:
            band = np.where(band == ds.nodata, np.nan, band)
        out.update(C.build_single(band, vmeta, p["bins"], p["max_cells"], ds.nodata))
        out["mode"] = "single"
print(json.dumps(out))
"""


def _read_system(params):
    import json
    import subprocess

    py = _system_python()
    if not py:
        return {
            "error": "system engine requested but no Python with rasterio was found "
            "(tried miniforge/conda/PATH). Set GEO_PYTHON or install rasterio."
        }
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    here = (
        os.path.dirname(os.path.abspath(__file__))
        if "__file__" in globals()
        else os.path.abspath(sys.path[0])
    )  # runner exec()s without __file__
    r = subprocess.run(
        [py, "-c", _WORKER, here],
        input=json.dumps(params),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if r.returncode != 0:
        return {"error": f"system engine failed: {r.stderr[-500:]}"}
    return json.loads(r.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- entry point
def main(
    file: str = "",
    band: int = 1,
    mode: str = "auto",
    r: int = 1,
    g: int = 2,
    b: int = 3,
    bins: int = 40,
    max_cells: int = 160000,
    engine: str = "auto",
    win: str = "",
):
    import numpy as np

    # New runner passes params untyped (HTML sends them as strings); coerce the
    # numeric ones the old runner used to convert via the annotations.
    band, r, g, b = int(band), int(r), int(g), int(b)
    bins, max_cells = int(bins), int(max_cells)

    if not file:
        return {"error": "no file selected"}
    file = os.path.abspath(os.path.expanduser(file))
    if not os.path.isfile(file):
        return {"error": f"not a file: {file}"}

    idx = [r, g, b]
    params = {
        "file": file,
        "band": band,
        "mode": mode,
        "idx": idx,
        "bins": bins,
        "max_cells": max_cells,
        "win": win,
    }

    # explicit system engine
    if engine == "system":
        return _finish(_read_system(params), file, band, idx, mode)

    # pure engine (default), auto-fall back to system on Unsupported
    try:
        bands, meta, wsel = read_tiff(file, win, max_cells)
    except Unsupported as e:
        if engine == "auto":
            res = _read_system(params)
            if "error" not in res:
                res.setdefault("note", f"pure engine: {e}")
                return _finish(res, file, band, idx, mode)
        # no pixels available — still return header metadata (gdalinfo-style)
        try:
            buf, en, t, next_off = parse_header(file)
            out = header_meta(file, buf, en, t, next_off)
            out.update(
                {
                    "engine": "pure (header only)",
                    "header_only": True,
                    "mode": "header",
                    "note": f"pixels not decodable: {e} — enable the system "
                    f"engine (rasterio) to render them",
                }
            )
            out["crs_name"] = _crs_name(out.get("crs"))
            out["lonlat_bounds"] = C.lonlat_bounds(
                out.get("bounds"), (out.get("crs") or {}).get("epsg")
            )
            return _finish(out, file, band, idx, mode)
        except Unsupported:
            return {"error": f"{e}", "hint": "enable the system engine (rasterio) for this file"}

    count = meta["count"]
    want_rgb = (mode == "rgb") or (mode == "auto" and count >= 3)
    meta["crs_name"] = _crs_name(meta.get("crs"))
    meta["lonlat_bounds"] = C.lonlat_bounds(meta.get("bounds"), (meta.get("crs") or {}).get("epsg"))

    out = dict(meta)

    # per-band ranges — only on full reads (a windowed decode leaves pixels
    # outside the window zeroed, so full-file stats would be wrong; the UI
    # keeps the band table from the last full read instead)
    if not wsel:
        binfo = []
        for i in range(count):
            a = bands[i].astype("float64")
            if meta.get("nodata") is not None:
                a = np.where(a == meta["nodata"], np.nan, a)
            fin = a[np.isfinite(a)]
            binfo.append(
                {
                    "band": i + 1,
                    "description": meta["descriptions"][i],
                    "min": C.clean(fin.min()) if fin.size else None,
                    "max": C.clean(fin.max()) if fin.size else None,
                }
            )
        out["band_info"] = binfo

    # optional zoom window (fractions of the full raster, from the UI)
    vbands, vmeta = bands, meta
    if wsel:
        x0, y0, x1, y1 = wsel
        vbands = bands[:, y0:y1, x0:x1]
        vmeta = dict(meta)
        wb = C.win_bounds(meta.get("transform"), x0, y0, x1, y1)
        if wb:
            vmeta["bounds"] = wb
        # wsel is in decode-IFD coords; report full-res pixel coords to the UI
        s = meta["width"] / float(bands.shape[2])
        out["window"] = {
            "px": [int(round(v * s)) for v in (x0, y0, x1, y1)],
            "full": [meta["width"], meta["height"]],
        }

    if want_rgb and count >= 3:
        out["mode"] = "rgb"
        vb = vbands.astype("float64")
        out["rgb"] = C.build_rgb(vb, idx, vmeta, max_cells)
        out["rgb"]["hists"] = C.channel_hists(vb, idx, bins)
    else:
        out["mode"] = "single"
        sel = np.clip(band, 1, count)
        arr = vbands[sel - 1].astype("float64")
        if meta.get("nodata") is not None:
            arr = np.where(arr == meta["nodata"], np.nan, arr)
        out.update(C.build_single(arr, vmeta, bins, max_cells, meta.get("nodata")))
    return _finish(out, file, band, idx, mode)


def _crs_name(crs):
    if not crs or not crs.get("epsg"):
        return None
    try:
        from pyproj import CRS

        return CRS.from_epsg(crs["epsg"]).name
    except Exception:
        return f"EPSG:{crs['epsg']}"


def _finish(out, file, band, idx, mode):
    if "error" in out:
        return out
    out["file"] = file
    out["selected"] = {"band": band, "mode": out.get("mode", mode), "rgb_idx": idx}
    return out


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused

    _udf_main = _fused.udf(main)
except ImportError:
    pass

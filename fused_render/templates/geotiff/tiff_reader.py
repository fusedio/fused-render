"""GeoTIFF slippy-map reader (v2) for fused-render.

Instead of shipping pixel grids as JSON (v1), this returns a base64 PNG
warped to Web Mercator plus (single-band) a base64 float32 value grid, so a
MapLibre frontend can overlay the raster on a basemap and compute live
viewport histograms client-side.

Decode engine is shared with v1 via _tiff_core (pure-Python TIFF decoder
with overview-pyramid reads; rasterio system-python fallback for LZW/JPEG/
BigTIFF).
"""
# /// script
# dependencies = ["numpy", "pyproj", "imagecodecs"]
# ///

import os
import sys

import _raster_common as C
import _tiff_core as T


MERC_MAX_LAT = 85.05112878


# ---------------------------------------------------------------- PNG encode
def encode_png(rgba):
    """uint8 (H, W, 4) -> PNG bytes. Pure stdlib: filter 0 rows + zlib lvl 1."""
    import struct
    import zlib
    import numpy as np

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


# ---------------------------------------------------------------- colormaps
_CMAPS = {
    "viridis": ["440154", "472d7b", "3b528b", "2c728e", "21918c", "28ae80", "5ec962", "addc30", "fde725"],
    "magma":   ["000004", "1c1044", "4f127b", "812581", "b5367a", "e55064", "fb8761", "fec287", "fcfdbf"],
    "turbo":   ["30123b", "4145ab", "4675ed", "39a2fc", "1bcfd4", "24eca6", "61fc6c", "a4fc3b", "d1e834",
                "f3c63a", "fe9b2d", "f36315", "d93806", "b11901", "7a0402"],
    "rdbu":    ["053061", "2166ac", "4393c3", "92c5de", "d1e5f0", "f7f7f7", "fddbc7", "f4a582", "d6604d",
                "b2182b", "67001f"],
    "grays":   ["111111", "ffffff"],
}


def _lut(name):
    import numpy as np
    stops = np.array([[int(h[i:i + 2], 16) for i in (0, 2, 4)]
                      for h in _CMAPS.get(name, _CMAPS["viridis"])], dtype="float64")
    x = np.linspace(0, len(stops) - 1, 256)
    i = np.clip(x.astype(int), 0, len(stops) - 2)
    f = (x - i)[:, None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).round().astype("uint8")


# ---------------------------------------------------------------- warping
def _merc_bbox(bounds, epsg):
    """Native window bounds -> tight EPSG:3857 bbox (edge-sampled)."""
    import numpy as np
    from pyproj import Transformer
    l, r, t, b = bounds["left"], bounds["right"], bounds["top"], bounds["bottom"]
    n = 25
    xs = np.concatenate([np.linspace(l, r, n), np.full(n, r), np.linspace(r, l, n), np.full(n, l)])
    ys = np.concatenate([np.full(n, t), np.linspace(t, b, n), np.full(n, b), np.linspace(b, t, n)])
    if int(epsg) == 3857:
        return float(l), float(b), float(r), float(t)
    tr = Transformer.from_crs(int(epsg), 3857, always_xy=True)
    mx, my = tr.transform(xs, ys)
    mx = mx[np.isfinite(mx)]; my = my[np.isfinite(my)]
    if not mx.size:
        raise ValueError("cannot project bounds to mercator")
    return float(mx.min()), float(my.min()), float(mx.max()), float(my.max())


def warp_grid(arr_bands, bounds, epsg, max_cells):
    """Nearest-neighbour warp of (n, H, W) native-grid bands onto an
    EPSG:3857-regular grid covering the same footprint.

    Returns (warped (n, oh, ow) float64 with NaN outside, merc_bbox)."""
    import numpy as np
    from pyproj import Transformer

    n, H, W = arr_bands.shape
    x0, y0, x1, y1 = _merc_bbox(bounds, epsg)
    aspect = max((x1 - x0) / max(y1 - y0, 1e-9), 1e-6)
    oh = max(2, int(round((max_cells / aspect) ** 0.5)))
    ow = max(2, int(round(oh * aspect)))
    # never upsample far past the source resolution
    scale = min(1.0, (2.0 * H * W / (oh * ow)) ** 0.5)
    oh, ow = max(2, int(oh * scale)), max(2, int(ow * scale))

    mx = np.linspace(x0, x1, ow + 1)[:-1] + (x1 - x0) / (2 * ow)
    my = np.linspace(y1, y0, oh + 1)[:-1] - (y1 - y0) / (2 * oh)
    MX, MY = np.meshgrid(mx, my)
    if int(epsg) == 3857:
        NX, NY = MX, MY
    else:
        tr = Transformer.from_crs(3857, int(epsg), always_xy=True)
        NX, NY = tr.transform(MX, MY)

    # native coords -> source pixel indices (bounds are the window's bounds)
    l, r, t, b = bounds["left"], bounds["right"], bounds["top"], bounds["bottom"]
    px = (NX - l) / max(r - l, 1e-12) * W
    py = (t - NY) / max(t - b, 1e-12) * H
    inside = np.isfinite(px) & np.isfinite(py) & (px >= 0) & (px < W) & (py >= 0) & (py < H)
    pxi = np.clip(px, 0, W - 1).astype(np.int32)
    pyi = np.clip(py, 0, H - 1).astype(np.int32)

    out = np.full((n, oh, ow), np.nan, dtype="float64")
    for k in range(n):
        v = arr_bands[k][pyi, pxi]
        out[k] = np.where(inside, v, np.nan)
    return out, (x0, y0, x1, y1)


# ---------------------------------------------------------------- rendering
def _stretch_pair(vals, robust, given):
    import numpy as np
    if given:
        return float(given[0]), float(given[1])
    fin = vals[np.isfinite(vals)]
    if not fin.size:
        return 0.0, 1.0
    if robust:
        lo, hi = float(np.percentile(fin, 2)), float(np.percentile(fin, 98))
    else:
        lo, hi = float(fin.min()), float(fin.max())
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi


def _parse_stretch(s, nchan):
    try:
        v = [float(x) for x in str(s).split(",")]
    except (ValueError, AttributeError):
        return [None] * nchan
    if len(v) == 2 * nchan:
        return [(v[2 * i], v[2 * i + 1]) for i in range(nchan)]
    if len(v) == 2:
        return [(v[0], v[1])] * nchan
    return [None] * nchan


def render_rgb(bands3, robust, stretch):
    """(3, H, W) float -> (rgba uint8, stretch used)."""
    import numpy as np
    h, w = bands3.shape[1:]
    rgba = np.zeros((h, w, 4), dtype="uint8")
    used = []
    valid = np.isfinite(bands3).all(axis=0)
    for k in range(3):
        lo, hi = _stretch_pair(bands3[k], robust, stretch[k])
        v = np.clip((bands3[k] - lo) / (hi - lo), 0, 1)
        rgba[:, :, k] = np.where(np.isfinite(v), v * 255, 0).astype("uint8")
        used.append([C.clean(lo), C.clean(hi)])
    rgba[:, :, 3] = np.where(valid, 255, 0)
    return rgba, used


def render_single(vals, cmap, robust, div, vlo, vhi, stretch):
    import numpy as np
    lo, hi = _stretch_pair(vals, robust, stretch[0])
    if div:
        m = max(abs(lo), abs(hi)) or 1.0
        lo, hi = -m, m
    lut = _lut(cmap)
    t = np.clip((vals - lo) / (hi - lo), 0, 1)
    idx = np.where(np.isfinite(t), t * 255, 0).astype("uint8")
    rgba = np.zeros(vals.shape + (4,), dtype="uint8")
    rgba[:, :, :3] = lut[idx]
    alpha = np.isfinite(vals)
    if vlo is not None and vhi is not None:
        alpha = alpha & (vals >= vlo) & (vals <= vhi)
    rgba[:, :, 3] = np.where(alpha, 255, 0)
    return rgba, [[C.clean(lo), C.clean(hi)]]


# ---------------------------------------------------------------- system decode
_SYS_WORKER = r'''
import sys, os, json, tempfile
import numpy as np, rasterio
from rasterio.windows import Window
p = json.loads(sys.stdin.read())
with rasterio.open(p["file"]) as ds:
    W, H = ds.width, ds.height
    fx0, fy0, fx1, fy1 = p["frac"]
    x0, y0 = int(fx0 * W), int(fy0 * H)
    x1, y1 = min(W, int(round(fx1 * W))), min(H, int(round(fy1 * H)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        x0, y0, x1, y1 = 0, 0, W, H
    h, w = y1 - y0, x1 - x0
    scale = max(1.0, (h * w / float(p["max_cells"])) ** 0.5)
    oshape = (max(1, int(h / scale)), max(1, int(w / scale)))
    win = Window.from_slices((y0, y1), (x0, x1))
    arr = ds.read(window=win, out_shape=(ds.count,) + oshape).astype("float32")
    if ds.nodata is not None:
        arr[arr == ds.nodata] = np.nan
    wb = rasterio.windows.bounds(win, ds.transform)
    meta = {"width": W, "height": H, "count": ds.count, "dtype": str(ds.dtypes[0]),
            "nodata": ds.nodata, "engine": "system (rasterio)",
            "crs": ({"epsg": ds.crs.to_epsg()} if ds.crs and ds.crs.to_epsg() else None),
            "transform": list(ds.transform)[:6],
            "bounds": {"left": ds.bounds.left, "right": ds.bounds.right,
                       "top": ds.bounds.top, "bottom": ds.bounds.bottom},
            "win_bounds": {"left": wb[0], "right": wb[2], "top": wb[3], "bottom": wb[1]},
            "window_px": [x0, y0, x1, y1],
            "descriptions": [ds.descriptions[i] or f"band {i+1}" for i in range(ds.count)],
            "file_size": os.path.getsize(p["file"]),
            "tiff": {"compression": (str(ds.compression.value) if ds.compression else "none"),
                     "layout": "tiled" if ds.profile.get("tiled") else "stripped",
                     "photometric": ds.profile.get("photometric"), "predictor": None,
                     "byte_order": None,
                     "overviews": [[max(1, W // f), max(1, H // f)] for f in ds.overviews(1)]},
            "tags": {}, "gdal_metadata": {}}
    tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False)
    np.save(tmp.name, arr)
    print(json.dumps({"npy": tmp.name, "meta": meta}))
'''


def _decode_system(file, frac, max_cells):
    import json
    import subprocess
    import numpy as np
    py = T._system_python()
    if not py:
        raise T.Unsupported("no system Python with rasterio (set GEO_PYTHON)")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    r = subprocess.run([py, "-c", _SYS_WORKER],
                       input=json.dumps({"file": file, "frac": frac, "max_cells": max_cells}),
                       capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        raise T.Unsupported(f"system engine failed: {r.stderr[-400:]}")
    d = json.loads(r.stdout.strip().splitlines()[-1])
    arr = np.load(d["npy"])
    try:
        os.unlink(d["npy"])
    except OSError:
        pass
    return arr, d["meta"]


# ---------------------------------------------------------------- decode dispatch
def _decode(file, win, max_cells, engine):
    """-> (bands (n,h,w) float32 NaN-masked over the WINDOW ONLY, meta)

    meta gains win_bounds (native bounds of the returned pixels) and
    window_px (full-res pixel coords)."""
    import numpy as np
    frac = [0.0, 0.0, 1.0, 1.0]
    try:
        fx = [float(v) for v in str(win).split(",")]
        if len(fx) == 4:
            frac = [min(max(fx[0], 0.0), 1.0), min(max(fx[1], 0.0), 1.0),
                    min(max(fx[2], 0.0), 1.0), min(max(fx[3], 0.0), 1.0)]
    except (ValueError, AttributeError):
        pass

    if engine == "system":
        return _decode_system(file, frac, max_cells)

    try:
        # decode a slightly padded window from the pyramid (read_tiff decodes
        # only intersecting tiles; pixels outside stay 0 and are sliced off)
        bands, meta, wsel = T.read_tiff(file, win, max_cells)
        dW, dH = bands.shape[2], bands.shape[1]
        if wsel:
            x0, y0, x1, y1 = wsel
        else:
            x0, y0, x1, y1 = 0, 0, dW, dH
        sub = bands[:, y0:y1, x0:x1].astype("float32")
        if meta.get("nodata") is not None:
            sub = np.where(sub == meta["nodata"], np.nan, sub)
        # decimate decoded window down to max_cells (tile decode can overshoot)
        h, w = sub.shape[1:]
        step = 1
        while (h // step) * (w // step) > max_cells and step < 64:
            step += 1
        sub = sub[:, ::step, ::step]
        wb = C.win_bounds(meta.get("transform"), x0, y0, x1, y1)
        meta["win_bounds"] = wb or meta.get("bounds")
        s = meta["width"] / float(dW)
        meta["window_px"] = [int(round(v * s)) for v in (x0, y0, x1, y1)]
        return sub, meta
    except T.Unsupported:
        if engine == "auto":
            return _decode_system(file, frac, max_cells)
        raise


# ---------------------------------------------------------------- entry point
def main(file: str = "", band: int = 1, mode: str = "auto",
         r: int = 1, g: int = 2, b: int = 3,
         cmap: str = "viridis", robust: str = "1", div: str = "0",
         vlo: str = "", vhi: str = "",
         win: str = "", max_cells: int = 250000,
         engine: str = "auto", stretch: str = "",
         meta_only: str = "0"):
    import base64
    import numpy as np

    band, r, g, b = int(band), int(r), int(g), int(b)
    max_cells = int(max_cells)
    robust_b, div_b = str(robust) == "1", str(div) == "1"
    fvlo = float(vlo) if str(vlo) not in ("", "None") else None
    fvhi = float(vhi) if str(vhi) not in ("", "None") else None

    if not file:
        return {"error": "no file selected"}
    file = os.path.abspath(os.path.expanduser(file))
    if not os.path.isfile(file):
        return {"error": f"not a file: {file}"}

    # header-only metadata (fast path for populating the info panel)
    try:
        buf, en, t, next_off = T.parse_header(file)
        hmeta = T.header_meta(file, buf, en, t, next_off)
    except T.Unsupported:
        hmeta = None
    if str(meta_only) == "1" and hmeta:
        hmeta["crs_name"] = T._crs_name(hmeta.get("crs"))
        hmeta["lonlat_bounds"] = C.lonlat_bounds(
            hmeta.get("bounds"), (hmeta.get("crs") or {}).get("epsg"))
        hmeta["file"] = file
        return hmeta

    try:
        bands, meta = _decode(file, win, max_cells, engine)
    except T.Unsupported as e:
        return {"error": str(e), "hint": "try the system engine (rasterio)"}
    except Exception as e:
        return {"error": f"decode failed: {e}"}

    count = meta["count"]
    want_rgb = (mode == "rgb") or (mode == "auto" and count >= 3)
    epsg = (meta.get("crs") or {}).get("epsg")
    wb = meta.get("win_bounds")

    # channels to render
    if want_rgb and count >= 3:
        idx = [min(max(k, 1), count) for k in (r, g, b)]
        chans = np.stack([bands[k - 1] for k in idx]).astype("float64")
        nchan = 3
    else:
        want_rgb = False
        sel = min(max(band, 1), count)
        chans = bands[sel - 1:sel].astype("float64")
        nchan = 1

    # warp to mercator when georeferenced; otherwise render the native grid
    georef = bool(epsg and wb)
    merc = None
    if georef:
        try:
            chans, merc = warp_grid(chans, wb, epsg, max_cells)
        except Exception:
            georef = False
    st = _parse_stretch(stretch, nchan)
    if want_rgb:
        rgba, used = render_rgb(chans, robust_b, st)
    else:
        rgba, used = render_single(chans[0], cmap, robust_b, div_b, fvlo, fvhi, st)

    png = encode_png(np.ascontiguousarray(rgba))
    out = {
        "file": file, "engine": meta.get("engine"),
        "mode": "rgb" if want_rgb else "single",
        "width": meta["width"], "height": meta["height"], "count": count,
        "dtype": meta.get("dtype"), "nodata": C.clean(meta.get("nodata")),
        "descriptions": meta.get("descriptions"),
        "crs": meta.get("crs"), "crs_name": T._crs_name(meta.get("crs")),
        "bounds": meta.get("bounds"),
        "lonlat_bounds": C.lonlat_bounds(meta.get("bounds"), epsg),
        "transform": meta.get("transform"),
        "file_size": meta.get("file_size"), "tiff": meta.get("tiff"),
        "tags": meta.get("tags"), "gdal_metadata": meta.get("gdal_metadata"),
        "georef": georef,
        "window_px": meta.get("window_px"),
        "stretch": used,
        "selected": {"band": band, "mode": "rgb" if want_rgb else "single",
                     "rgb_idx": [r, g, b]},
        "image": {
            "png": base64.b64encode(png).decode("ascii"),
            "width": int(rgba.shape[1]), "height": int(rgba.shape[0]),
            "merc_bbox": list(merc) if merc else None,
            "win_bounds": wb,
        },
    }
    if hmeta:
        out["tiff"] = hmeta.get("tiff") or out["tiff"]

    # raw values for client-side hover + live viewport histogram (single band)
    if not want_rgb:
        v32 = chans[0].astype("<f4")
        out["vals"] = base64.b64encode(v32.tobytes()).decode("ascii")
        fin = chans[0][np.isfinite(chans[0])]
        out["stats"] = {
            "count": int(fin.size), "nan": int(chans[0].size - fin.size),
            "min": C.clean(fin.min()) if fin.size else None,
            "max": C.clean(fin.max()) if fin.size else None,
            "mean": C.clean(fin.mean()) if fin.size else None,
            "std": C.clean(fin.std()) if fin.size else None,
            "p2": C.clean(np.percentile(fin, 2)) if fin.size else None,
            "p98": C.clean(np.percentile(fin, 98)) if fin.size else None,
        }
    return out


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass

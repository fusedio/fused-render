"""Shared raster -> JSON presentation for the GeoTIFF preview.

Both engines feed into this: the pure-Python TIFF decoder (bundled interpreter)
and the rasterio worker (system Python). Given a 2D/3D numpy array + metadata it
builds the exact same JSON payload the HTML expects — downsampled grid, stats,
histogram, and a percentile-stretched RGB composite. numpy + (optional) pyproj
only, so it imports in either interpreter.
"""


def clean(x):
    import math
    import numpy as np
    if x is None:
        return None
    if isinstance(x, (np.floating, float)):
        f = float(x)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(x, (np.integer, int)):
        return int(x)
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    return str(x)


def lonlat_bounds(bounds, epsg):
    """Reproject native bounds corners to EPSG:4326 (lon/lat). None on failure."""
    if not bounds or not epsg:
        return None
    try:
        from pyproj import Transformer
        if int(epsg) == 4326:
            return {"west": bounds["left"], "south": bounds["bottom"],
                    "east": bounds["right"], "north": bounds["top"]}
        tr = Transformer.from_crs(int(epsg), 4326, always_xy=True)
        xs = [bounds["left"], bounds["right"], bounds["right"], bounds["left"]]
        ys = [bounds["top"], bounds["top"], bounds["bottom"], bounds["bottom"]]
        lon, lat = tr.transform(xs, ys)
        return {"west": min(lon), "south": min(lat), "east": max(lon), "north": max(lat)}
    except Exception:
        return None


def parse_win(win, W, H):
    """'x0,y0,x1,y1' fractions of the full raster -> pixel slice, or None."""
    try:
        fx0, fy0, fx1, fy1 = [float(v) for v in str(win).split(",")]
    except (ValueError, AttributeError):
        return None
    fx0 = min(max(fx0, 0.0), 1.0); fy0 = min(max(fy0, 0.0), 1.0)
    fx1 = min(max(fx1, 0.0), 1.0); fy1 = min(max(fy1, 0.0), 1.0)
    x0, x1 = int(fx0 * W), min(W, int(round(fx1 * W)))
    y0, y1 = int(fy0 * H), min(H, int(round(fy1 * H)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    if x0 == 0 and y0 == 0 and x1 == W and y1 == H:
        return None                      # full view == no window
    return x0, y0, x1, y1


def win_bounds(transform, x0, y0, x1, y1):
    """Native-CRS bounds of a pixel window, from the affine transform."""
    if not transform:
        return None
    a, b, c, d, e, f = transform
    xs = [c + a * x0 + b * y0, c + a * x1 + b * y1]
    ys = [f + d * x0 + e * y0, f + d * x1 + e * y1]
    return {"left": min(xs), "right": max(xs), "top": max(ys), "bottom": min(ys)}


def channel_hists(bands, idx, bins):
    """Per-channel raw-value histograms for the RGB composite."""
    import numpy as np
    out = []
    for k in idx:
        b0 = bands[k - 1]
        fin = b0[np.isfinite(b0)]
        if fin.size:
            c, e = np.histogram(fin, bins=max(4, min(bins, 200)))
            out.append({"counts": [int(x) for x in c], "edges": [clean(x) for x in e]})
        else:
            out.append({"counts": [], "edges": []})
    return out


def _downsample_step(h, w, max_cells):
    step = 1
    while (h // step) * (w // step) > max_cells and step < 64:
        step += 1
    return step


def _axis(bounds, n, which, step):
    """Per-row (lat/y) or per-col (lon/x) coordinate centers, sampled by step."""
    if not bounds:
        return None
    if which == "y":
        top, bot = bounds["top"], bounds["bottom"]
        return [top + (i + 0.5) * (bot - top) / n for i in range(0, n, step)]
    left, right = bounds["left"], bounds["right"]
    return [left + (i + 0.5) * (right - left) / n for i in range(0, n, step)]


def build_single(arr, meta, bins, max_cells, nodata):
    """arr: 2D float array of the selected band (nodata already -> NaN)."""
    import numpy as np
    arr = np.where(np.isfinite(arr), arr, np.nan)
    rows, cols = arr.shape
    valid = arr[np.isfinite(arr)]
    nv = int(valid.size)
    stats = {
        "count": nv, "nan": int(arr.size - nv),
        "min": clean(valid.min()) if nv else None,
        "max": clean(valid.max()) if nv else None,
        "mean": clean(valid.mean()) if nv else None,
        "std": clean(valid.std()) if nv else None,
        "median": clean(np.median(valid)) if nv else None,
        "p2": clean(np.percentile(valid, 2)) if nv else None,
        "p98": clean(np.percentile(valid, 98)) if nv else None,
    }
    hist = {"counts": [], "edges": []}
    if nv:
        c, e = np.histogram(valid, bins=max(4, min(bins, 200)))
        hist = {"counts": [int(x) for x in c], "edges": [clean(x) for x in e]}

    step = _downsample_step(rows, cols, max_cells)
    small = np.round(arr[::step, ::step], 4)
    grid = [[clean(x) for x in row] for row in small]
    b = meta.get("bounds")
    return {
        "stats": stats, "histogram": hist,
        "grid": {
            "rows": len(grid), "cols": len(grid[0]) if grid else 0,
            "step": step, "orig_shape": [rows, cols],
            "values": grid,
            "lats": _axis(b, rows, "y", step) if b else None,
            "lons": _axis(b, cols, "x", step) if b else None,
        },
    }


def build_rgb(bands, idx, meta, max_cells):
    """bands: 3D (count, H, W). idx: [r,g,b] 1-based. Percentile-stretch to 0-255."""
    import numpy as np
    _, rows, cols = bands.shape
    step = _downsample_step(rows, cols, max_cells)
    chans, ranges = [], []
    for k in idx:
        band = bands[k - 1][::step, ::step].astype("float64")
        finite = band[np.isfinite(band)]
        lo = float(np.percentile(finite, 2)) if finite.size else 0.0
        hi = float(np.percentile(finite, 98)) if finite.size else 1.0
        if hi <= lo:
            hi = lo + 1.0
        v = np.clip((band - lo) / (hi - lo), 0, 1)
        chans.append(np.where(np.isfinite(band), (v * 255).round(), 0).astype("uint8"))
        ranges.append([clean(lo), clean(hi)])
    r, g, b = chans
    rgb = [[[int(r[i, j]), int(g[i, j]), int(b[i, j])] for j in range(r.shape[1])]
           for i in range(r.shape[0])]
    bb = meta.get("bounds")
    return {
        "rows": r.shape[0], "cols": r.shape[1], "step": step,
        "orig_shape": [rows, cols], "values": rgb, "ranges": ranges,
        "lats": _axis(bb, rows, "y", step) if bb else None,
        "lons": _axis(bb, cols, "x", step) if bb else None,
    }

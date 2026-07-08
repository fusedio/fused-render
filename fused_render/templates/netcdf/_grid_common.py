"""Shared 2D-grid -> JSON presentation for the Zarr preview.

Given a 2D float array (nodata already NaN) plus optional per-row/col coordinate
values, build the grid/stats/histogram payload the HTML expects. Used by both
the pure-Python zarr reader (bundled) and the zarr worker (system Python), so
they emit an identical schema. numpy only.
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


def present(arr, lats, lons, bins, max_cells):
    """arr: 2D float array. lats/lons: full coord lists (len == rows/cols) or None."""
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

    step = 1
    while (rows // step) * (cols // step) > max_cells and step < 64:
        step += 1
    small = np.round(arr[::step, ::step], 4)
    grid = [[clean(x) for x in row] for row in small]

    def _sample(vals, n):
        if vals is None:
            return None
        v = list(vals)
        if len(v) < n:
            return None
        return [clean(x) for x in v[::step][: len(grid) if n == rows else len(grid[0])]]

    return {
        "stats": stats, "histogram": hist,
        "grid": {
            "rows": len(grid), "cols": len(grid[0]) if grid else 0,
            "step": step, "orig_shape": [rows, cols], "values": grid,
            "lats": _sample(lats, rows), "lons": _sample(lons, cols),
        },
    }

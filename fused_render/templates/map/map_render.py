"""runPython target for the map template: classify a geospatial file into a
map-layer descriptor (see geo_classify.py) and cache its artifacts.

Each call is a fresh subprocess, so there's no warm daemon to keep geopandas/
rasterio imported between clicks (unlike the standalone map_viewer app this
template is based on) — imports stay lazy inside main()/geo_classify and the
descriptor + its artifacts (GeoJSON / PNG / binary point buffers) are cached
under ~/.fused-render/cache/map/<hash>/ keyed by (target, mtime, opts), so
re-opening an unchanged file is instant on the next call.
"""
import hashlib
import json
import os

CACHE_ROOT = os.path.expanduser("~/.fused-render/cache/map")
_URLISH = ("http://", "https://", "s3://", "/vsi")


def _artifacts_exist(desc):
    data = desc.get("data") or {}
    for key in ("geojson_path", "image_path", "points_path"):
        p = data.get(key)
        if p and not os.path.exists(p):
            return False
    return True


def main(target: str = "", colormap: str = "viridis", rescale: str = ""):
    """Classify `target` (a geospatial file path or URL) and return a map-layer
    descriptor. colormap/rescale style a single-band raster (rescale = "lo,hi");
    changing either re-renders it."""
    target = (target or "").strip()
    if not target:
        return {"status": "error", "message": "No file selected.", "kind": None,
                "bounds": None, "data": {}, "warnings": []}

    is_url = target.startswith(_URLISH)
    if not is_url:
        target = os.path.abspath(os.path.expanduser(target))
        if not os.path.exists(target):
            return {"status": "error", "message": f"Not found: {target}", "kind": None,
                    "bounds": None, "data": {}, "warnings": []}
        mtime = os.path.getmtime(target)
    else:
        mtime = 0.0

    opts = {"colormap": colormap}
    if rescale:
        try:
            lo, hi = (float(x) for x in rescale.split(","))
            opts["rescale"] = [lo, hi]
        except ValueError:
            pass

    opts_json = json.dumps(opts, sort_keys=True)
    artifact_id = hashlib.sha256(f"{target}|{mtime}|{opts_json}".encode()).hexdigest()[:16]
    cache_dir = os.path.join(CACHE_ROOT, artifact_id)
    desc_path = os.path.join(cache_dir, "descriptor.json")

    if os.path.exists(desc_path):
        try:
            with open(desc_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if _artifacts_exist(cached):
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    os.makedirs(cache_dir, exist_ok=True)
    import geo_classify
    desc = geo_classify.classify(target, cache_dir, artifact_id, opts)

    tmp = f"{desc_path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(desc, fh)
    os.replace(tmp, desc_path)
    return desc


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass

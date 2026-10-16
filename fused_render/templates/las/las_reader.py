"""LAS/LAZ point-cloud reader for fused-render."""

import base64
import hashlib
import json
import os

import numpy as np

CACHE = os.path.expanduser("~/.cache/fused-render-las")

CLASS_NAMES = {
    0: "never classified", 1: "unassigned", 2: "ground", 3: "low vegetation",
    4: "medium vegetation", 5: "high vegetation", 6: "building",
    7: "low point (noise)", 8: "reserved", 9: "water", 10: "rail",
    11: "road surface", 12: "reserved", 13: "wire guard", 14: "wire conductor",
    15: "transmission tower", 16: "wire connector", 17: "bridge deck",
    18: "high noise",
}


def _decode(las, dst, max_points):
    tmp = f"{dst}.{os.getpid()}.tmp.npz"
    h = las.header
    n = h.point_count
    step = max(1, -(-n // max_points))
    idx = np.arange(0, n, step)

    dims = set(las.point_format.dimension_names)
    extra = {}
    if "intensity" in dims:
        extra["intensity"] = np.asarray(las.intensity[idx], np.uint16)
    if "classification" in dims:
        extra["classification"] = np.asarray(las.classification[idx], np.uint8)
    if {"red", "green", "blue"} <= dims:
        r, g, b = (np.asarray(las[c][idx], np.uint16) for c in ("red", "green", "blue"))
        scale = 8 if max(int(r.max(initial=0)), int(g.max(initial=0)), int(b.max(initial=0))) > 255 else 0
        extra["rgb"] = np.stack([r >> scale, g >> scale, b >> scale], 1).astype(np.uint8)

    crs_wkt, epsg, bounds4326 = None, None, None
    try:
        crs = h.parse_crs()
        if crs is not None:
            crs_wkt, epsg = crs.to_wkt(), crs.to_epsg()
            from pyproj import Transformer
            t = Transformer.from_crs(crs, 4326, always_xy=True)
            lon, lat = t.transform([h.mins[0], h.maxs[0]], [h.mins[1], h.maxs[1]])
            bounds4326 = [float(lon[0]), float(lat[0]), float(lon[1]), float(lat[1])]
    except Exception:
        pass

    cls_hist = {}
    if "classification" in extra:
        vals, counts = np.unique(extra["classification"], return_counts=True)
        cls_hist = {int(v): int(c) for v, c in zip(vals, counts)}

    meta = {
        "version": f"{h.version.major}.{h.version.minor}",
        "point_format": int(h.point_format.id),
        "point_count": int(n),
        "shown": int(len(idx)),
        "step": int(step),
        "scales": [float(v) for v in h.scales],
        "offsets": [float(v) for v in h.offsets],
        "mins": [float(v) for v in h.mins],
        "maxs": [float(v) for v in h.maxs],
        "generating_software": str(h.generating_software).strip("\x00 "),
        "system_identifier": str(h.system_identifier).strip("\x00 "),
        "creation_date": str(h.creation_date) if h.creation_date else None,
        "vlr_count": len(h.vlrs),
        "dims": sorted(dims),
        "crs_wkt": crs_wkt, "epsg": epsg, "bounds4326": bounds4326,
        "class_hist": cls_hist,
        "has": {k: k in extra for k in ("intensity", "classification", "rgb")},
    }
    np.savez_compressed(tmp, meta=json.dumps(meta),
                        x=np.asarray(las.x[idx], np.float64),
                        y=np.asarray(las.y[idx], np.float64),
                        z=np.asarray(las.z[idx], np.float64), **extra)
    os.replace(tmp, dst)


def main(file: str = "", max_points: int = 400000):
    max_points = int(max_points)
    if not file:
        return {"error": "no file selected"}
    file = os.path.abspath(os.path.expanduser(file))
    if not os.path.isfile(file):
        return {"error": f"not a file: {file}"}

    key = hashlib.sha1(f"{file}|{os.path.getmtime(file)}|{max_points}".encode()).hexdigest()[:16]
    npz = os.path.join(CACHE, f"pts_{key}.npz")
    if not os.path.exists(npz):
        import laspy

        os.makedirs(CACHE, exist_ok=True)
        try:
            _decode(laspy.read(file), npz, max_points)
        except Exception as e:
            return {"error": f"could not read point cloud: {e}"}

    d = np.load(npz, allow_pickle=False)
    meta = json.loads(str(d["meta"]))
    x, y, z = d["x"], d["y"], d["z"]
    cx, cy, cz = float(x.mean()), float(y.mean()), float(z.mean())
    pos = np.empty((len(x), 3), np.float32)
    pos[:, 0], pos[:, 1], pos[:, 2] = x - cx, y - cy, z - cz
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()

    out = {
        "file": file,
        "file_size": os.path.getsize(file),
        "meta": meta,
        "center": [cx, cy, cz],
        "z_range": [float(z.min()), float(z.max())] if len(z) else [0, 0],
        "pos": b64(pos),
        "class_names": {str(k): CLASS_NAMES.get(int(k), f"class {k}")
                        for k in meta.get("class_hist", {})},
    }
    for k in ("intensity", "classification", "rgb"):
        if k in d.files:
            out[k] = b64(d[k])
    if "intensity" in d.files:
        inten = d["intensity"]
        out["intensity_range"] = [int(inten.min()), int(inten.max())] if len(inten) else [0, 0]
    return out


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass

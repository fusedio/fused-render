"""Detached conversion worker for the usd preview template.

Converts a NuRec/mesh .usdz (or a plain mesh usd/usdz) into browser-renderable
artifacts inside a cache directory:

    model_b<budget>.splat   gaussians in antimatter15 .splat layout (32 B each)
    mesh.glb                proxy/plain meshes as GLB (positions/normals/colors)
    manifest.json           counts, bounding boxes, layer tree for the viewer
    progress.json           polled by the page while this process runs

Run detached by reader.py (runPython has a 30 s budget; large captures can
take much longer to convert):

    python convert_worker.py <source> <cache_dir> <budget>

<source> may be an http(s) URL; it is downloaded into the cache first.
"""

import gzip
import io
import json
import os
import shutil
import struct
import sys
import time
import zipfile
import zlib

import numpy as np

CHUNK = 8 << 20  # 8 MB stream chunks
SH_C0 = 0.28209479177387814
IDENTITY = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


# --------------------------------------------------------------- progress ---

class Progress:
    def __init__(self, cache_dir):
        self.path = os.path.join(cache_dir, "progress.json")
        self.t0 = time.time()

    def update(self, stage, pct, detail="", done=False, error=None):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "stage": stage, "pct": round(float(pct), 1), "detail": detail,
                "done": done, "error": error, "pid": os.getpid(),
                "elapsed": round(time.time() - self.t0, 1), "ts": time.time(),
            }, f)
        os.replace(tmp, self.path)

    def fail(self, message):
        self.update("error", 100, message, done=True, error=message)


# ------------------------------------------------------------ nurec layer ---

def read_nurec(zf, name, prog):
    """Stream-decompress the gzipped msgpack payload with progress."""
    import msgpack
    info = zf.getinfo(name)
    total = info.file_size
    d = zlib.decompressobj(wbits=16 + 15)
    out = io.BytesIO()
    read = 0
    with zf.open(name) as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            out.write(d.decompress(chunk))
            read += len(chunk)
            prog.update("decompress", 100.0 * read / total,
                        f"decompressing gaussians {read >> 20} / {total >> 20} MB")
    out.write(d.flush())
    prog.update("parse", 0, "parsing gaussian tensors")
    data = out.getvalue()
    out.close()
    obj = msgpack.unpackb(data, raw=False, strict_map_key=False,
                          max_bin_len=2**31 - 1, max_str_len=2**31 - 1,
                          max_array_len=2**31 - 1, max_map_len=2**31 - 1)
    return obj["nre_data"]


def finish_splat(pos, scales, quat, color, opacity, budget, prog, out_path,
                 crop=True):
    """Budget-cut, pack and write the 32 B/splat records; return stats.

    crop drops everything outside the robust content bounds — NuRec captures
    wrap the scene in a shell of huge sky/background gaussians that eat the
    budget and render as fog/noise. The budget cut is a uniform random sample
    (preserves the size distribution; any size-biased ranking turns a heavy
    cut into either fog or spikes). Giant outlier scales are clamped.
    """
    total = len(pos)
    prog.update("splats", 10, f"grading {total:,} gaussians")
    keep = opacity > 0.02                            # invisible splats: dead weight
    if crop:
        lo = np.percentile(pos[keep], 1, axis=0)
        hi = np.percentile(pos[keep], 99, axis=0)
        pad = (hi - lo) * 0.15
        keep &= np.all((pos > lo - pad) & (pos < hi + pad), axis=1)
    cap = np.percentile(scales[keep].max(axis=1), 99)  # kill residual streaks
    scales = np.minimum(scales, cap)

    order = np.where(keep)[0]
    content = len(order)
    boost = 1.0
    if budget and budget < content:
        order = np.random.default_rng(42).choice(order, budget, replace=False)
        # scale compensation: keeping 1/k of the splats leaves holes; growing
        # the survivors by sqrt(k) preserves surface coverage (capped so a
        # deep cut degrades to soft blobs rather than mush)
        boost = min((content / budget) ** 0.5, 3.0)
    # stream big splats first: coarse coverage appears early, detail fills in
    size = scales[order].prod(axis=1)
    order = order[np.argsort(-size)]
    kept = len(order)

    prog.update("splats", 40, f"writing {kept:,} of {total:,} splats")
    rec = np.zeros(kept, dtype=[("pos", "<f4", 3), ("scale", "<f4", 3),
                                ("rgba", "u1", 4), ("rot", "u1", 4)])
    rec["pos"] = pos[order]
    rec["scale"] = scales[order] * boost
    rec["rgba"][:, :3] = np.clip(color[order] * 255, 0, 255).astype(np.uint8)
    rec["rgba"][:, 3] = np.clip(opacity[order] * 255, 0, 255).astype(np.uint8)
    q = quat[order]
    q /= np.linalg.norm(q, axis=1, keepdims=True) + 1e-12
    rec["rot"] = np.clip(q * 128 + 128, 0, 255).astype(np.uint8)

    tmp = out_path + ".tmp"
    rec.tofile(tmp)
    os.replace(tmp, out_path)

    body = pos[keep]                                 # robust bbox ignores far sky
    robust = [np.percentile(body, 1, axis=0).tolist(),
              np.percentile(body, 99, axis=0).tolist()]
    full = [pos.min(axis=0).tolist(), pos.max(axis=0).tolist()]
    return {"total": int(total), "kept": int(kept),
            "bboxRobust": robust, "bboxFull": full}


def nurec_to_splat(nre, out_path, budget, crop, prog):
    """Standard 3DGS activations: exp(scale), sigmoid(density), 0.5 + C0*dc."""
    sd = nre["state_dict"]
    pre = ".gaussians_nodes.gaussians."

    def tensor(name):
        return np.frombuffer(sd[pre + name], dtype="<f2") \
                 .reshape(sd[pre + name + ".shape"]).astype(np.float32)

    stats = finish_splat(
        pos=tensor("positions"),
        scales=np.exp(tensor("scales")),
        quat=tensor("rotations"),                    # (w, x, y, z), pre-normalize
        color=0.5 + SH_C0 * tensor("features_albedo"),
        opacity=1.0 / (1.0 + np.exp(-tensor("densities")[:, 0])),
        budget=budget, prog=prog, out_path=out_path, crop=crop)
    stats["shDegree"] = int(nre["config"]["layers"]["gaussians"]["particle"]
                            .get("radiance_sph_degree", 0))
    return stats


# -------------------------------------------------------------- ply layer ---

PLY_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
             "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
             "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
             "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}


def read_ply_vertices(path):
    """Vertex table of a binary-LE PLY as a numpy structured array."""
    with open(path, "rb") as f:
        if f.readline().strip() != b"ply":
            raise ValueError("not a ply file")
        fmt, count, fields, in_vertex = None, 0, [], False
        while True:
            line = f.readline()
            if not line:
                raise ValueError("unterminated ply header")
            parts = line.decode("latin1").strip().split()
            if not parts or parts[0] == "comment":
                continue
            if parts[0] == "format":
                fmt = parts[1]
            elif parts[0] == "element":
                if parts[1] == "vertex":
                    if fields:
                        raise ValueError("vertex is not the first ply element")
                    count, in_vertex = int(parts[2]), True
                else:
                    in_vertex = False
            elif parts[0] == "property" and in_vertex:
                if parts[1] == "list":
                    raise ValueError("list property on vertex unsupported")
                fields.append((parts[2], "<" + PLY_TYPES[parts[1]]))
            elif parts[0] == "end_header":
                break
        if fmt != "binary_little_endian":
            raise ValueError(f"only binary_little_endian ply supported, got {fmt}")
        if not count:
            raise ValueError("ply has no vertices")
        return np.fromfile(f, dtype=np.dtype(fields), count=count)


def ply_to_splat(path, cache_dir, budget, crop, prog):
    """3DGS gaussian PLY -> baked .splat; plain point cloud -> packed xyz+rgb
    buffer for the GPU point-sprite renderer. Returns stats incl. 'file'."""
    prog.update("parse", 0, "reading ply vertices")
    v = read_ply_vertices(path)
    names = set(v.dtype.names)
    pos = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float32)
    ok = np.isfinite(pos).all(axis=1)                # SLAM logs can carry NaNs
    if not ok.all():
        v, pos = v[ok], pos[ok]
    n = len(pos)

    if {"f_dc_0", "opacity", "scale_0", "rot_0"} <= names:
        kind = "gaussian-ply"
        out_name = f"model_b{budget}c{crop}.splat"
        scales = np.exp(np.stack([v["scale_0"], v["scale_1"], v["scale_2"]],
                                 axis=1).astype(np.float32))
        quat = np.stack([v["rot_0"], v["rot_1"], v["rot_2"], v["rot_3"]],
                        axis=1).astype(np.float32)
        color = 0.5 + SH_C0 * np.stack(
            [v["f_dc_0"], v["f_dc_1"], v["f_dc_2"]], axis=1).astype(np.float32)
        opacity = 1.0 / (1.0 + np.exp(-v["opacity"].astype(np.float32)))
    else:
        # plain point cloud: pack xyz f32 + rgba u8 (16 B/point) for the GPU
        # point-sprite renderer — no gaussians, no depth sorting needed
        out_name = f"points_b{budget}.bin"
        out_path = os.path.join(cache_dir, out_name)
        prog.update("splats", 30, f"packing {n:,} points")
        if budget and budget < n:                    # even stride keeps coverage
            sel = np.linspace(0, n - 1, budget).astype(np.int64)
            v, pos = v[sel], pos[sel]
        kept = len(pos)
        rec = np.zeros(kept, dtype=[("pos", "<f4", 3), ("rgba", "u1", 4)])
        rec["pos"] = pos
        if {"red", "green", "blue"} <= names:
            color = np.stack([v["red"], v["green"], v["blue"]],
                             axis=1).astype(np.float32)
            if color.max() > 1.001:                  # uchar 0-255 vs float 0-1
                color /= 255.0
        else:
            color = np.full((kept, 3), 0.7, dtype=np.float32)
        rec["rgba"][:, :3] = np.clip(color * 255, 0, 255).astype(np.uint8)
        rec["rgba"][:, 3] = 255
        tmp = out_path + ".tmp"
        rec.tofile(tmp)
        os.replace(tmp, out_path)

        lo, hi = np.percentile(pos, 1, axis=0), np.percentile(pos, 99, axis=0)
        ext = np.maximum(hi - lo, 1e-6)
        # default world point size from typical spacing; points live on
        # surfaces, so take the larger of area- and volume-based estimates
        area = 2 * (ext[0] * ext[1] + ext[1] * ext[2] + ext[0] * ext[2])
        s = 1.6 * max((area / kept) ** 0.5, (ext.prod() / kept) ** (1 / 3))
        return {"kind": "pointcloud", "file": out_name,
                "total": int(n), "kept": int(kept), "spacing": float(s),
                "bboxRobust": [lo.tolist(), hi.tolist()],
                "bboxFull": [pos.min(axis=0).tolist(),
                             pos.max(axis=0).tolist()]}

    stats = finish_splat(pos, scales, quat, color, opacity, budget, prog,
                         os.path.join(cache_dir, out_name), crop=crop)
    stats["kind"] = kind
    stats["file"] = out_name
    return stats


# ----------------------------------------------------------- usd runtime ---

# usd-core is fetched on demand (D119), not bundled: the pxr package is
# ~224 MB and only this worker's mesh extraction needs it — the same
# large-binary-on-first-use posture as the docs template's pandoc/typst
# (docs/install_worker.py). Version pinned like TYPST_VERSION; the wheel is
# unpacked (a .whl is a zip) into a per-version, per-python site dir — no pip
# involved, because the packaged app's python ships without pip by design
# (SPEC §19 DP-3).
USD_CORE_VERSION = "26.5"


def _usd_site():
    py_tag = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    return py_tag, os.path.expanduser(os.path.join(
        "~", ".fused-render", "usd-site", f"{USD_CORE_VERSION}-{py_tag}"))


def _ensure_pxr(prog):
    """Import pxr, downloading + unpacking the usd-core wheel on first use.

    Progress rides this worker's own progress.json (stage "install-usd"), so
    the template's existing convert poll loop shows the download with zero
    page changes. Raises on failure — the caller's mesh layer is best-effort
    and surfaces the message as manifest.meshError.
    """
    try:
        import pxr  # noqa: F401
        return
    except ImportError:
        pass
    py_tag, site = _usd_site()
    if site not in sys.path:
        sys.path.insert(0, site)
    try:
        import pxr  # noqa: F401  (previously installed on-demand copy)
        return
    except ImportError:
        pass

    import platform
    import urllib.request
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        plat = "macosx"          # single universal2 wheel per python version
    elif system == "Linux":
        # upstream ships manylinux x86_64 only — fail fast on other arches
        # instead of downloading a wheel whose .so files can't load
        if machine != "x86_64":
            raise RuntimeError(
                f"no usd-core wheel for Linux/{machine} (x86_64 only)")
        plat = "manylinux"
    elif system == "Windows":
        if machine not in ("amd64", "x86_64"):
            raise RuntimeError(
                f"no usd-core wheel for Windows/{machine} (amd64 only)")
        plat = "win_amd64"
    else:
        raise RuntimeError(f"no usd-core wheel for platform: {system}")

    prog.update("install-usd", 0,
                f"first USD preview — resolving usd-core {USD_CORE_VERSION}")
    api = f"https://pypi.org/pypi/usd-core/{USD_CORE_VERSION}/json"
    req = urllib.request.Request(api, headers={"User-Agent": "fused-render"})
    with urllib.request.urlopen(req, timeout=30) as r:
        meta = json.load(r)
    url = next((u["url"] for u in meta["urls"]
                if f"-{py_tag}-" in u["filename"] and plat in u["filename"]),
               None)
    if url is None:
        raise RuntimeError(
            f"no usd-core {USD_CORE_VERSION} wheel for {py_tag}/{plat}")

    # All temp names are pid-suffixed: several convert workers can race on
    # the shared site dir (two USD files opened at once), and a shared temp
    # path would let one worker unpack into a file another is still writing.
    os.makedirs(os.path.dirname(site), exist_ok=True)
    whl = f"{site}.{os.getpid()}.whl.tmp"
    req = urllib.request.Request(url, headers={"User-Agent": "fused-render"})
    with urllib.request.urlopen(req, timeout=60) as r, open(whl, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            pct = 90.0 * got / total if total else 50.0
            prog.update("install-usd", pct,
                        f"downloading USD runtime {got >> 20} / {total >> 20} MB")

    prog.update("install-usd", 95, "unpacking USD runtime")
    tmp_site = f"{site}.{os.getpid()}.tmp"
    shutil.rmtree(tmp_site, ignore_errors=True)
    with zipfile.ZipFile(whl) as zf:
        zf.extractall(tmp_site)
    os.remove(whl)
    # Publish atomically: rename only wins if `site` doesn't exist. If a
    # racing worker published first, its install is complete and identical
    # (same pinned wheel) — drop ours and use theirs. Never rmtree a live
    # `site`: another process may already be importing from it.
    try:
        os.rename(tmp_site, site)
    except OSError:
        if not os.path.isdir(site):
            raise
        shutil.rmtree(tmp_site, ignore_errors=True)
    # The failed import above ran while `site` didn't exist yet, and importlib
    # caches a "nothing there" finder for the path — flush it or the fresh
    # unpack stays invisible to this process.
    import importlib
    importlib.invalidate_caches()
    import pxr  # noqa: F401  (raises if the unpacked wheel is unusable)


# ------------------------------------------------------------- mesh layer ---

def smooth_normals(points, tris):
    fn = np.cross(points[tris[:, 1]] - points[tris[:, 0]],
                  points[tris[:, 2]] - points[tris[:, 0]])
    vn = np.zeros_like(points)
    for i in range(3):
        np.add.at(vn, tris[:, i], fn)
    n = np.linalg.norm(vn, axis=1, keepdims=True)
    return (vn / np.maximum(n, 1e-12)).astype(np.float32)


def triangulate(counts, indices):
    if counts.size and (counts == 3).all():
        return indices.reshape(-1, 3)
    tris = []
    off = 0
    for c in counts:
        for i in range(1, c - 1):
            tris.append((indices[off], indices[off + i], indices[off + i + 1]))
        off += c
    return np.asarray(tris, dtype=np.uint32)


def usd_meshes(stage_path, prog):
    """Extract every UsdGeom.Mesh with its world transform and displayColor."""
    _ensure_pxr(prog)
    from pxr import Usd, UsdGeom
    stage = Usd.Stage.Open(stage_path)
    meshes = []
    prims = [p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)]
    for i, prim in enumerate(prims):
        prog.update("mesh", 100.0 * i / max(len(prims), 1),
                    f"reading mesh {prim.GetPath()}")
        m = UsdGeom.Mesh(prim)
        pts = m.GetPointsAttr().Get()
        counts = m.GetFaceVertexCountsAttr().Get()
        idx = m.GetFaceVertexIndicesAttr().Get()
        if not pts or not counts:
            continue
        points = np.asarray(pts, dtype=np.float32)
        tris = triangulate(np.asarray(counts, dtype=np.int64),
                           np.asarray(idx, dtype=np.uint32)).astype(np.uint32)
        xf = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(
            Usd.TimeCode.Default())
        # USD row-vector row-major flat == glTF column-vector column-major flat
        matrix = [float(v) for row in xf for v in row]

        color, vcolor = None, None
        pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar("displayColor")
        if pv and pv.HasValue():
            vals = np.asarray(pv.Get(), dtype=np.float32)
            if len(vals) == len(points):
                vcolor = vals
            elif len(vals) >= 1:
                color = vals[0].tolist()
        meshes.append({
            "name": str(prim.GetPath()), "points": points, "tris": tris,
            "normals": smooth_normals(points, tris.astype(np.int64)),
            "matrix": matrix, "color": color, "vcolor": vcolor,
        })
    up = UsdGeom.GetStageUpAxis(stage)
    return meshes, str(up)


def write_glb(meshes, out_path):
    """Minimal hand-rolled GLB: POSITION/NORMAL/COLOR_0 + uint32 indices."""
    bin_parts, views, accessors, gmeshes, nodes, materials = [], [], [], [], [], []
    offset = 0

    def add_view(data, target):
        nonlocal offset
        b = data.tobytes()
        pad = (4 - len(b) % 4) % 4
        bin_parts.append(b + b"\x00" * pad)
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(b),
                      "target": target})
        offset += len(b) + pad
        return len(views) - 1

    def add_accessor(data, target, ctype, atype, minmax=False):
        acc = {"bufferView": add_view(data, target), "componentType": ctype,
               "count": len(data), "type": atype}
        if minmax:
            acc["min"] = data.min(axis=0).tolist()
            acc["max"] = data.max(axis=0).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    for m in meshes:
        attrs = {
            "POSITION": add_accessor(m["points"], 34962, 5126, "VEC3", True),
            "NORMAL": add_accessor(m["normals"], 34962, 5126, "VEC3"),
        }
        if m["vcolor"] is not None:
            attrs["COLOR_0"] = add_accessor(m["vcolor"], 34962, 5126, "VEC3")
        idx = add_accessor(m["tris"].reshape(-1), 34963, 5125, "SCALAR")
        base = (m["color"] or [0.75, 0.75, 0.78]) + [1.0]
        materials.append({"pbrMetallicRoughness": {
            "baseColorFactor": base, "metallicFactor": 0.0,
            "roughnessFactor": 0.9}, "doubleSided": True})
        gmeshes.append({"name": m["name"], "primitives": [{
            "attributes": attrs, "indices": idx, "mode": 4,
            "material": len(materials) - 1}]})
        nodes.append({"name": m["name"], "mesh": len(gmeshes) - 1,
                      "matrix": m["matrix"]})

    gltf = {"asset": {"version": "2.0", "generator": "fused-render usd template"},
            "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}],
            "nodes": nodes, "meshes": gmeshes, "materials": materials,
            "accessors": accessors, "bufferViews": views,
            "buffers": [{"byteLength": offset}]}

    js = json.dumps(gltf).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    binb = b"".join(bin_parts)
    total = 12 + 8 + len(js) + 8 + len(binb)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(binb), 0x004E4942) + binb)
    os.replace(tmp, out_path)


# ------------------------------------------------------- obj / stl layer ---

def _mesh_dict(name, points, tris):
    return {"name": name, "points": points, "tris": tris,
            "normals": smooth_normals(points, tris.astype(np.int64)),
            "matrix": IDENTITY, "color": None, "vcolor": None}


def read_obj(path, prog):
    """Wavefront OBJ: positions + triangulated faces (UVs/materials ignored)."""
    prog.update("parse", 0, "reading obj vertices")
    verts, faces = [], []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:]:
                    v = tok.split("/")[0]
                    if v:
                        i = int(v)
                        idx.append(i - 1 if i > 0 else len(verts) + i)
                for k in range(1, len(idx) - 1):    # fan-triangulate n-gons
                    faces.append((idx[0], idx[k], idx[k + 1]))
    if not verts or not faces:
        raise ValueError("obj has no geometry")
    points = np.asarray(verts, dtype=np.float32)
    tris = np.asarray(faces, dtype=np.uint32)
    prog.update("mesh", 60, f"{len(points):,} verts, {len(tris):,} tris")
    name = os.path.splitext(os.path.basename(path))[0]
    return [_mesh_dict(name, points, tris)], "Y"


def read_stl(path, prog):
    """STL (binary or ASCII) as a flat triangle soup; normals recomputed."""
    prog.update("parse", 0, "reading stl")
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        data = f.read()
    name = os.path.splitext(os.path.basename(path))[0]
    if size >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if size == 84 + n * 50:                 # exact length -> binary stl
            arr = np.frombuffer(data, count=n, offset=84, dtype=np.dtype(
                [("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")]))
            points = arr["v"].reshape(-1, 3).astype(np.float32)
            tris = np.arange(n * 3, dtype=np.uint32).reshape(-1, 3)
            prog.update("mesh", 60, f"{n:,} triangles (binary stl)")
            return [_mesh_dict(name, points, tris)], "Y"
    verts = []
    for line in data.decode("latin1", "ignore").splitlines():
        p = line.split()
        if len(p) == 4 and p[0] == "vertex":
            verts.append((float(p[1]), float(p[2]), float(p[3])))
    if len(verts) < 3:
        raise ValueError("stl has no vertices")
    points = np.asarray(verts, dtype=np.float32)
    tris = np.arange(len(points) - len(points) % 3, dtype=np.uint32).reshape(-1, 3)
    prog.update("mesh", 60, f"{len(tris):,} triangles (ascii stl)")
    return [_mesh_dict(name, points, tris)], "Y"


# ------------------------------------------------------------------ main ----

def download(url, cache_dir, prog):
    import urllib.request
    dest = os.path.join(cache_dir, "src", os.path.basename(url.split("?")[0])
                        or "download.usdz")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            pct = 100.0 * got / total if total else 50
            prog.update("download", pct, f"downloading {got >> 20} MB")
    return dest


def convert(source, cache_dir, budget, crop=1):
    os.makedirs(cache_dir, exist_ok=True)
    prog = Progress(cache_dir)
    prog.update("start", 0, "starting conversion")
    if source.startswith(("http://", "https://")):
        source = download(source, cache_dir, prog)

    manifest_path = os.path.join(cache_dir, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
    manifest.setdefault("source", source)
    manifest.setdefault("name", os.path.basename(source))
    manifest.setdefault("splatFiles", {})

    ext = os.path.splitext(source)[1].lower()

    if ext == ".ply":
        have = any(os.path.exists(os.path.join(cache_dir, c)) for c in
                   (f"points_b{budget}.bin", f"model_b{budget}c{crop}.splat"))
        if not have:
            stats = ply_to_splat(source, cache_dir, budget, crop, prog)
            kind, fname = stats.pop("kind"), stats.pop("file")
            manifest["kind"] = kind
            manifest["gaussians"] = {
                k: stats[k] for k in ("total", "bboxRobust", "bboxFull")}
            entry = {"file": fname, "count": stats["kept"],
                     "bytes": os.path.getsize(os.path.join(cache_dir, fname))}
            if kind == "pointcloud":
                entry["size"] = stats["spacing"]
                manifest.setdefault("pointFiles", {})[str(budget)] = entry
            else:
                manifest["splatFiles"][f"{budget}c{crop}"] = entry
        manifest["updated"] = time.time()
        tmp = manifest_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(manifest, f, indent=1)
        os.replace(tmp, manifest_path)
        prog.update("done", 100, "conversion complete", done=True)
        return

    if ext in (".obj", ".stl"):
        glb_path = os.path.join(cache_dir, "mesh.glb")
        if not os.path.exists(glb_path):
            meshes, up = (read_obj if ext == ".obj" else read_stl)(source, prog)
            prog.update("mesh", 90, "writing mesh.glb")
            write_glb(meshes, glb_path)
            manifest["kind"] = "mesh"
            manifest["upAxis"] = up
            manifest["mesh"] = {
                "file": "mesh.glb", "bytes": os.path.getsize(glb_path),
                "meshes": [{"name": m["name"], "verts": len(m["points"]),
                            "tris": len(m["tris"]), "color": m["color"],
                            "vertexColors": m["vcolor"] is not None}
                           for m in meshes]}
        else:
            manifest.setdefault("kind", "mesh")
        manifest["updated"] = time.time()
        tmp = manifest_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(manifest, f, indent=1)
        os.replace(tmp, manifest_path)
        prog.update("done", 100, "conversion complete", done=True)
        return

    if ext not in (".usdz", ".usd", ".usda", ".usdc"):
        prog.fail(f"unsupported file type: {ext}")
        return

    src_dir = os.path.join(cache_dir, "src")
    os.makedirs(src_dir, exist_ok=True)

    nurec_name = None
    stage_path = source
    if ext == ".usdz":
        zf = zipfile.ZipFile(source)
        names = zf.namelist()
        nurec_name = next((n for n in names if n.endswith(".nurec")), None)
        prog.update("extract", 0, "extracting usd layers")
        root_layer = None
        for n in names:
            if n.endswith((".usda", ".usd", ".usdc")):
                dest = os.path.join(src_dir, n)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(zf.read(n))
                if root_layer is None or n == "default.usda":
                    root_layer = dest
        stage_path = root_layer

    # ---- gaussian layer
    if nurec_name:
        splat_name = f"model_b{budget}c{crop}.splat"
        splat_path = os.path.join(cache_dir, splat_name)
        if not os.path.exists(splat_path):
            nre = read_nurec(zf, nurec_name, prog)
            stats = nurec_to_splat(nre, splat_path, budget, crop, prog)
            manifest["kind"] = "nurec"
            manifest["gaussians"] = {k: stats[k] for k in
                                     ("total", "bboxRobust", "bboxFull",
                                      "shDegree")}
            manifest["splatFiles"][f"{budget}c{crop}"] = {
                "file": splat_name, "count": stats["kept"],
                "bytes": os.path.getsize(splat_path)}
    else:
        manifest["kind"] = "usd"

    # ---- mesh layer
    glb_path = os.path.join(cache_dir, "mesh.glb")
    if stage_path and not os.path.exists(glb_path):
        try:
            meshes, up = usd_meshes(stage_path, prog)
        except Exception as e:  # mesh is best-effort; splats still usable
            meshes, up = [], "Z"
            manifest["meshError"] = str(e)
        manifest["upAxis"] = up
        if meshes:
            prog.update("mesh", 90, "writing mesh.glb")
            write_glb(meshes, glb_path)
            manifest["mesh"] = {
                "file": "mesh.glb", "bytes": os.path.getsize(glb_path),
                "meshes": [{"name": m["name"], "verts": len(m["points"]),
                            "tris": len(m["tris"]),
                            "color": m["color"],
                            "vertexColors": m["vcolor"] is not None}
                           for m in meshes]}

    manifest["updated"] = time.time()
    tmp = manifest_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=1)
    os.replace(tmp, manifest_path)
    prog.update("done", 100, "conversion complete", done=True)


if __name__ == "__main__":
    src, cache, budget = sys.argv[1], sys.argv[2], int(sys.argv[3])
    crop_arg = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    try:
        convert(src, cache, budget, crop_arg)
    except Exception as e:
        import traceback
        traceback.print_exc()
        Progress(cache).fail(f"{type(e).__name__}: {e}")
        sys.exit(1)

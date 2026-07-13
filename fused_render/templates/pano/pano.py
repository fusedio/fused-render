# /// script
# dependencies = ["py360convert>=1.0.4"]
# ///
"""Backend for the pano preview template: validates/classifies the target
image, normalizes it (any Pillow-readable format -> browser-displayable
JPEG/PNG capped at 8192px), and runs on-the-fly projection conversions
(py360convert e2c/e2p plus custom little-planet / fisheye resamplers).

Everything derived from the image (display copy, converted projections)
is cached under ~/.fused-render/cache/pano/<content-hash>/ — never next
to the user's file.
"""

import hashlib
import json
import math
import os
import time

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    import os, sys
    __file__ = os.path.join(sys.path[0], "pano.py")

CACHE_ROOT = os.path.expanduser(os.path.join("~", ".fused-render", "cache", "pano"))

DISPLAY_MAX_W = 8192   # keep under common WebGL texture limits
CONVERT_MAX_W = 4096   # resample source cap so conversions stay interactive


def _pil():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise RuntimeError("the pano viewer needs Pillow — pip install pillow")
    return Image, ImageOps


def _py360():
    try:
        import py360convert
    except ImportError:
        raise RuntimeError(
            "projection conversion needs py360convert — pip install py360convert")
    return py360convert


# ---------------------------------------------------------------- validation

def _dice_corners_blank(img):
    """A cube-cross layout has uniform (blank) corner blocks; a regular 4:3
    photo almost never does. Checks the top-left and bottom-right face cells."""
    import numpy as np

    g = np.asarray(img.convert("L"))
    ch, cw = g.shape[0] // 3, g.shape[1] // 4
    qh, qw = ch // 4, cw // 4  # central half of each corner cell
    cells = []
    for row in (0, 2):
        for col in (0, 2, 3):
            cell = g[row * ch:(row + 1) * ch, col * cw:(col + 1) * cw]
            cells.append(cell[qh:-qh or None, qw:-qw or None])
    return all(float(c.std()) < 6.0 for c in cells)


def _classify(w, h, raw_head, img):
    """Decide what kind of panorama the pixel dimensions suggest.

    Returns (kind, valid, reasons). kind is one of:
    equirect, equirect_180, cube_dice, cube_horizon, flat.
    """
    reasons = []
    ratio = w / h
    has_gpano = b"GPano" in raw_head or b"equirectangular" in raw_head
    if has_gpano:
        reasons.append("GPano/photo-sphere XMP metadata found")

    def close(a, b, tol=0.02):
        return abs(a - b) <= b * tol

    if close(ratio, 2.0):
        reasons.append(f"aspect ratio {ratio:.3f} ≈ 2:1 (full equirectangular)")
        return "equirect", True, reasons
    if close(ratio, 6.0):
        reasons.append("aspect ratio 6:1 (horizontal cube strip)")
        return "cube_horizon", True, reasons
    if close(ratio, 4.0 / 3.0):
        if _dice_corners_blank(img):
            reasons.append("aspect ratio 4:3 with blank corners (cube cross / dice layout)")
            return "cube_dice", True, reasons
        reasons.append("aspect ratio 4:3 but corners contain image data — regular photo, not a cube cross")
        return "flat", False, reasons
    if close(ratio, 1.0):
        if has_gpano:
            reasons.append("1:1 with photo-sphere metadata (VR180 half pano)")
            return "equirect_180", True, reasons
        reasons.append("aspect ratio 1:1 — could be VR180, treating as half pano")
        return "equirect_180", False, reasons
    if has_gpano:
        reasons.append(f"unusual aspect {ratio:.3f} but metadata says panorama (cropped?)")
        return "equirect", True, reasons
    reasons.append(f"aspect ratio {ratio:.3f} matches no panoramic layout")
    return "flat", False, reasons


# ---------------------------------------------------------------- prepare

def _content_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _prepare(file):
    """Classify the image and build its browser-displayable copy, cached by
    content hash. Returns the meta dict with 'dir' set to the cache folder."""
    file = os.path.abspath(os.path.expanduser(file))
    if not os.path.isfile(file):
        raise ValueError(f"not a file: {file}")
    cdir = os.path.join(CACHE_ROOT, _content_hash(file))
    meta_path = os.path.join(cdir, "meta.json")
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        if os.path.isfile(os.path.join(cdir, meta["display"])):
            meta["dir"] = cdir
            return meta

    Image, ImageOps = _pil()
    Image.MAX_IMAGE_PIXELS = 400_000_000
    img = Image.open(file)
    fmt = (img.format or "?").upper()
    img = ImageOps.exif_transpose(img)
    w, h = img.size
    with open(file, "rb") as f:
        head = f.read(256 * 1024)
    kind, valid, reasons = _classify(w, h, head, img)

    os.makedirs(cdir, exist_ok=True)
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )
    disp = img.convert("RGBA" if has_alpha else "RGB")
    if disp.width > DISPLAY_MAX_W:
        disp = disp.resize(
            (DISPLAY_MAX_W, max(1, round(disp.height * DISPLAY_MAX_W / disp.width))),
            Image.LANCZOS,
        )
    if has_alpha:
        display = "display.png"
        disp.save(os.path.join(cdir, display))
    else:
        display = "display.jpg"
        disp.save(os.path.join(cdir, display), quality=92)

    meta = {"name": os.path.basename(file), "format": fmt,
            "bytes": os.path.getsize(file), "width": w, "height": h,
            "kind": kind, "valid": valid, "reasons": reasons,
            "display": display, "display_w": disp.width, "display_h": disp.height}
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    meta["dir"] = cdir
    return meta


def op_open(file):
    meta = _prepare(file)
    meta["display_path"] = os.path.join(meta.pop("dir"), meta["display"])
    return {"asset": meta}


# ---------------------------------------------------------------- conversion

def _load_equirect(meta):
    """Load the display copy as an equirectangular numpy array (converting
    cube layouts via c2e), capped at CONVERT_MAX_W for interactive speed."""
    import numpy as np

    py360convert = _py360()
    Image, _ = _pil()
    img = Image.open(os.path.join(meta["dir"], meta["display"])).convert("RGB")
    if img.width > CONVERT_MAX_W:
        img = img.resize(
            (CONVERT_MAX_W, max(1, round(img.height * CONVERT_MAX_W / img.width))),
            Image.LANCZOS,
        )
    arr = np.asarray(img)
    if meta["kind"] == "cube_dice":
        face = arr.shape[1] // 4
        arr = py360convert.c2e(arr, face * 2, face * 4, cube_format="dice")
    elif meta["kind"] == "cube_horizon":
        face = arr.shape[1] // 6
        arr = py360convert.c2e(arr, face * 2, face * 4, cube_format="horizon")
    return arr.astype("uint8")


def _sample_equirect(equi, lon, lat):
    """Bilinear-sample equirect image at lon/lat arrays (radians)."""
    import numpy as np

    h, w, _ = equi.shape
    x = (lon / (2 * math.pi) + 0.5) * w - 0.5
    y = (0.5 - lat / math.pi) * h - 0.5
    x0 = np.floor(x).astype(int)
    y0 = np.floor(y).astype(int)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    x0w, x1w = x0 % w, (x0 + 1) % w
    y0c, y1c = np.clip(y0, 0, h - 1), np.clip(y0 + 1, 0, h - 1)
    top = equi[y0c, x0w] * (1 - fx) + equi[y0c, x1w] * fx
    bot = equi[y1c, x0w] * (1 - fx) + equi[y1c, x1w] * fx
    return (top * (1 - fy) + bot * fy).astype("uint8")


def _little_planet(equi, size, zoom, roll):
    """Stereographic 'little planet': sphere projected from the zenith."""
    import numpy as np

    s = np.linspace(-1, 1, size)
    x, y = np.meshgrid(s, -s)
    r = np.sqrt(x * x + y * y) + 1e-9
    theta = np.arctan2(y, x) + math.radians(roll)
    lat = math.pi / 2 - 2 * np.arctan(r / max(zoom, 0.05))
    lon = (theta + math.pi) % (2 * math.pi) - math.pi
    return _sample_equirect(equi, lon, lat)


def _fisheye180(equi, size, yaw, pitch):
    """Equidistant 180-degree fisheye looking at (yaw, pitch)."""
    import numpy as np

    s = np.linspace(-1, 1, size)
    x, y = np.meshgrid(s, -s)
    r = np.sqrt(x * x + y * y)
    theta = r * (math.pi / 2)          # angle from view axis, max 90 deg
    phi = np.arctan2(y, x)
    # direction in camera space (z forward)
    dx = np.sin(theta) * np.cos(phi)
    dy = np.sin(theta) * np.sin(phi)
    dz = np.cos(theta)
    # rotate by pitch (about x) then yaw (about y)
    p, yw = math.radians(pitch), math.radians(yaw)
    dy2 = dy * math.cos(p) + dz * math.sin(p)
    dz2 = -dy * math.sin(p) + dz * math.cos(p)
    dx2 = dx * math.cos(yw) + dz2 * math.sin(yw)
    dz3 = -dx * math.sin(yw) + dz2 * math.cos(yw)
    lon = np.arctan2(dx2, dz3)
    lat = np.arcsin(np.clip(dy2, -1, 1))
    out = _sample_equirect(equi, lon, lat)
    out[r > 1] = 16                    # outside the image circle
    return out


def op_convert(file, mode, fov, yaw, pitch, roll, zoom, out_w, out_h, face_w):
    import numpy as np

    py360convert = _py360()
    Image, _ = _pil()
    t0 = time.time()
    meta = _prepare(file)
    key = f"{mode}:{fov}:{yaw}:{pitch}:{roll}:{zoom}:{out_w}:{out_h}:{face_w}"
    hid = hashlib.sha1(key.encode()).hexdigest()[:16]
    ddir = os.path.join(meta["dir"], "derived")
    os.makedirs(ddir, exist_ok=True)

    def finish(path_or_faces, w, h, cached):
        res = {"w": w, "h": h, "cached": cached,
               "ms": round((time.time() - t0) * 1000)}
        if isinstance(path_or_faces, list):
            res["faces"] = path_or_faces
        else:
            res["path"] = path_or_faces
        return res

    if mode == "cube_faces":
        names = ["F", "R", "B", "L", "U", "D"]
        paths = [os.path.join(ddir, f"{hid}_{n}.jpg") for n in names]
        if all(os.path.isfile(p) for p in paths):
            fw = face_w or 1024
            return finish([{"face": n, "path": p} for n, p in zip(names, paths)],
                          fw, fw, True)
        equi = _load_equirect(meta)
        fw = face_w or min(1024, equi.shape[1] // 4)
        faces = py360convert.e2c(equi, face_w=fw, cube_format="dict")
        for n, p in zip(names, paths):
            Image.fromarray(faces[n]).save(p, quality=90)
        return finish([{"face": n, "path": p} for n, p in zip(names, paths)],
                      fw, fw, False)

    full = os.path.join(ddir, f"{hid}.jpg")
    if os.path.isfile(full):
        with Image.open(full) as im:
            return finish(full, im.width, im.height, True)

    equi = _load_equirect(meta)
    if mode in ("cube_dice", "cube_horizon"):
        fw = face_w or min(1024, equi.shape[1] // 4)
        out = py360convert.e2c(equi, face_w=fw, cube_format=mode.split("_")[1])
    elif mode == "perspective":
        ow, oh = out_w or 1280, out_h or 720
        h_fov = max(1.0, min(fov or 90.0, 175.0))
        v_fov = math.degrees(
            2 * math.atan(math.tan(math.radians(h_fov) / 2) * oh / ow))
        out = py360convert.e2p(equi, (h_fov, v_fov), yaw, pitch, (oh, ow),
                               in_rot_deg=roll)
    elif mode == "little_planet":
        out = _little_planet(equi, out_w or 1024, zoom or 1.0, roll)
    elif mode == "fisheye180":
        out = _fisheye180(equi, out_w or 1024, yaw, pitch)
    elif mode == "equirect":
        out = equi
    else:
        raise ValueError(f"unknown conversion mode {mode!r}")

    out = np.ascontiguousarray(out)
    Image.fromarray(out).save(full, quality=90)
    return finish(full, out.shape[1], out.shape[0], False)


# ---------------------------------------------------------------- dispatcher

def main(
    action: str = "open",
    file: str = "",
    mode: str = "",
    fov: float = 90.0,
    yaw: float = 0.0,
    pitch: float = 0.0,
    roll: float = 0.0,
    zoom: float = 1.0,
    out_w: int = 0,
    out_h: int = 0,
    face_w: int = 0,
):
    if not file:
        raise ValueError("missing 'file' param (the image to view)")
    if action == "open":
        return op_open(file)
    if action == "convert":
        return op_convert(file, mode, fov, yaw, pitch, roll, zoom,
                          out_w, out_h, face_w)
    raise ValueError(f"unknown action {action!r}")

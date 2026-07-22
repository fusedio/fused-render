import hashlib
import json
import os

# --- mount-safe directory listing ------------------------------------------
# A kernel listing (os.scandir/os.listdir/os.walk) on a path under a remote
# rclone NFS mount forces rclone to enumerate the ENTIRE parent S3 prefix and
# can DROP the mount, wedging the server. This template stays mount-AGNOSTIC:
# it never imports shell.mounts and never matches mount paths. Instead the UI
# passes `src` (server origin + /api/fs/raw?path=) and we ask the server whether
# a path is remote (/api/fs/stat); if so we list it via the mount-routed,
# paginated /api/fs/list — never through the kernel. _server_url + _stat are
# copied verbatim from pyramid/overview_pyramid.py.
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq


def _server_url(src, endpoint, path):
    u = _urlparse.urlsplit(src)
    return (f"{u.scheme}://{u.netloc}{endpoint}?path="
            + _urlparse.quote(path))


def _stat(src, path):
    url = _server_url(src, "/api/fs/stat", path)
    try:
        with _urlreq.urlopen(url, timeout=10) as r:
            return ("ok", json.load(r))
    except _urlerr.HTTPError as e:
        if e.code == 404:
            return ("missing", None)
        return ("unreachable", None)
    except Exception:  # noqa: BLE001 — any network error -> fall back to local
        return ("unreachable", None)


def _remote_dir(src, path):
    """True iff the server says `path` is a remote (mount-backed) directory.
    No src / unreachable / missing -> False (presume local, kernel listing OK).
    Never raises: a listing must not fail because the probe failed."""
    if not src or not path:
        return False
    status, meta = _stat(src, path)
    return status == "ok" and bool(meta.get("remote"))


def _list_remote(src, path, cap=5000):
    """List `path` via the server's mount-routed, paginated /api/fs/list — never
    the kernel. Follows the cursor up to `cap` entries so a huge S3 prefix
    returns a bounded page set instead of tripping the NFS deadman. Returns
    (entries, truncated); each entry is {name, is_dir, size, mtime, ignored}."""
    entries, cursor, truncated = [], "", False
    while True:
        url = _server_url(src, "/api/fs/list", path)
        if cursor:
            url += "&cursor=" + _urlparse.quote(cursor)
        with _urlreq.urlopen(url, timeout=30) as r:
            payload = json.load(r)
        entries.extend(payload.get("entries") or [])
        truncated = bool(payload.get("truncated"))
        cursor = payload.get("cursor") or ""
        if len(entries) >= cap or not truncated or not cursor:
            break
    return entries, truncated


NATIVE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"}
TRANSCODE = {".heic", ".heif"}
RAW = {".dng"}
PHOTO_EXT = NATIVE | TRANSCODE | RAW


def _skip_entry(entry) -> bool:
    if entry.name.startswith("."):
        return True
    if os.name == "nt":
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            if attrs & 0x2 or attrs & 0x4:  # HIDDEN or SYSTEM (legacy junctions)
                return True
        except OSError:
            return True
    return False


def _kind(ext: str) -> str:
    if ext in TRANSCODE:
        return "transcode"
    if ext in RAW:
        return "raw"
    return "native"


def _app_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _home() -> str:
    return os.path.abspath(os.path.expanduser("~"))


def _cache_dir() -> str:
    d = os.path.join(_home(), ".fused-render", "cache", "photos")
    os.makedirs(d, exist_ok=True)
    return d


def _fwd(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/")


def _key(path: str, variant: str) -> str:
    st = os.stat(path)
    raw = f"{os.path.normcase(os.path.abspath(path))}|{st.st_mtime_ns}|{st.st_size}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return os.path.join(_cache_dir(), f"{h}-{variant}.webp")


def _register_heif() -> bool:
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        return True
    except Exception:
        return False


def _open_oriented(path: str):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = None
    ext = os.path.splitext(path)[1].lower()
    if ext in {".heic", ".heif"} and not _register_heif():
        raise RuntimeError("no-decoder")
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img


def _encode(img, dst: str, quality: int):
    from PIL import Image

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    tmp = dst + ".tmp"
    img.save(tmp, "WEBP", quality=quality)
    os.replace(tmp, dst)


def _thumb_meta(dst: str) -> str:
    return dst[:-5] + ".json"


def _write_meta(meta: str, w: int, h: int):
    tmp = meta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"w": w, "h": h}, f)
    os.replace(tmp, meta)


def _header_dims(path: str):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    ext = os.path.splitext(path)[1].lower()
    if ext in {".heic", ".heif"} and not _register_heif():
        raise RuntimeError("no-decoder")
    with Image.open(path) as img:
        w, h = img.size
        o = img.getexif().get(274, 1)
    if o in (5, 6, 7, 8):
        w, h = h, w
    return w, h


_TRANSPOSE = None


def _transpose_ops():
    global _TRANSPOSE
    if _TRANSPOSE is None:
        from PIL import Image

        _TRANSPOSE = {
            2: Image.FLIP_LEFT_RIGHT, 3: Image.ROTATE_180, 4: Image.FLIP_TOP_BOTTOM,
            5: Image.TRANSPOSE, 6: Image.ROTATE_270, 7: Image.TRANSVERSE, 8: Image.ROTATE_90,
        }
    return _TRANSPOSE


def _make_thumb(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    dst = _key(path, "t400")
    meta = _thumb_meta(dst)
    if os.path.isfile(dst):
        try:
            with open(meta, encoding="utf-8") as f:
                m = json.load(f)
            w, h = m["w"], m["h"]
        except Exception:
            try:
                w, h = _header_dims(path)
                _write_meta(meta, w, h)
            except Exception:
                w = h = 0
        return {"path": _fwd(path), "ok": True, "cache": _fwd(dst), "w": w, "h": h}

    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    if ext in {".heic", ".heif"} and not _register_heif():
        raise RuntimeError("no-decoder")
    img = Image.open(path)
    orientation = img.getexif().get(274, 1)
    rw, rh = img.size
    swapped = orientation in (5, 6, 7, 8)
    w, h = (rh, rw) if swapped else (rw, rh)
    if ext in {".jpg", ".jpeg"}:
        box = (800, max(1, rh * 800 // rw)) if swapped else (max(1, rw * 800 // rh), 800)
        img.draft("RGB", box)
    img.thumbnail((400, 10000) if swapped else (10000, 400), Image.LANCZOS)
    tmap = _transpose_ops()
    if orientation in tmap:
        img = img.transpose(tmap[orientation])
    _write_meta(meta, w, h)
    _encode(img, dst, 70)
    img.close()
    return {"path": _fwd(path), "ok": True, "cache": _fwd(dst), "w": w, "h": h}


def _make_variant(path: str, variant: str, max_edge: int, quality: int) -> dict:
    dst = _key(path, variant)
    if os.path.isfile(dst):
        return {"mode": "cache", "cache": _fwd(dst)}
    from PIL import Image

    img = _open_oriented(path)
    if max_edge and max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    _encode(img, dst, quality)
    img.close()
    return {"mode": "cache", "cache": _fwd(dst)}


def check_setup() -> dict:
    deps = {"pillow": False, "pillow_heif": False, "rawpy": False}
    try:
        import PIL  # noqa: F401

        deps["pillow"] = True
    except Exception:
        pass
    try:
        import pillow_heif  # noqa: F401

        deps["pillow_heif"] = True
    except Exception:
        pass
    try:
        import rawpy  # noqa: F401

        deps["rawpy"] = True
    except Exception:
        pass
    return {
        "ok": True,
        "app_dir": _fwd(_app_dir()),
        "cache_dir": _fwd(_cache_dir()),
        "home": _fwd(_home()),
        "deps": deps,
    }


def folders(path: str, src: str = "") -> dict:
    if not path:
        roots = []
        if os.name == "nt":
            import string

            for letter in string.ascii_uppercase:
                drive = f"{letter}:/"
                if os.path.exists(drive):
                    roots.append({"name": f"{letter}:", "path": drive})
        else:
            roots.append({"name": "/", "path": "/"})
        return {"ok": True, "path": "", "home": _fwd(_home()), "dirs": roots}

    d = os.path.abspath(os.path.expanduser(path))
    if _remote_dir(src, d):
        # Mount-backed dir: never kernel-scan it. List via /api/fs/list. A network
        # or HTTP hiccup returns a structured error instead of raising out of the
        # action (matches the other templates' picker error handling).
        try:
            ents, _ = _list_remote(src, d)
        except Exception as exc:  # noqa: BLE001
            parent = os.path.dirname(d)
            return {"ok": False, "error": "list-failed", "detail": str(exc)[:200],
                    "path": _fwd(d),
                    "parent": _fwd(parent) if parent and parent != d else "",
                    "photos": 0, "dirs": []}
        dirs = [{"name": e["name"], "path": _fwd(os.path.join(d, e["name"]))}
                for e in ents
                if e.get("is_dir") and not e["name"].startswith(".")]
        n_photos = sum(
            1 for e in ents if not e.get("is_dir")
            and os.path.splitext(e["name"])[1].lower() in PHOTO_EXT)
        dirs.sort(key=lambda e: e["name"].lower())
        parent = os.path.dirname(d)
        return {"ok": True, "path": _fwd(d),
                "parent": _fwd(parent) if parent and parent != d else "",
                "photos": n_photos, "dirs": dirs}
    if not os.path.isdir(d):
        raise NotADirectoryError(f"not a directory: {path}")
    dirs = []
    n_photos = 0
    with os.scandir(d) as it:
        for entry in it:
            if _skip_entry(entry):
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": _fwd(entry.path)})
                elif os.path.splitext(entry.name)[1].lower() in PHOTO_EXT:
                    n_photos += 1
            except OSError:
                pass
    dirs.sort(key=lambda e: e["name"].lower())
    parent = os.path.dirname(d)
    return {
        "ok": True,
        "path": _fwd(d),
        "parent": _fwd(parent) if parent and parent != d else "",
        "photos": n_photos,
        "dirs": dirs,
    }


SORTS = {
    "new": (lambda e: e["mtime"], True),
    "old": (lambda e: e["mtime"], False),
    "az": (lambda e: e["name"].lower(), False),
    "za": (lambda e: e["name"].lower(), True),
    "big": (lambda e: e["size"], True),
    "small": (lambda e: e["size"], False),
    "date": (lambda e: e["mtime"], True),
    "name": (lambda e: e["name"].lower(), False),
}


def _day_epoch(day: str, end: bool) -> float:
    import datetime

    y, m, d = (int(x) for x in day.split("-"))
    t = datetime.datetime(y, m, d, 23, 59, 59) if end else datetime.datetime(y, m, d)
    return t.timestamp()


def list_dir(path: str, sort: str, offset: int, limit: int, q: str, date_from: str, date_to: str, src: str = "") -> dict:
    d = os.path.abspath(os.path.expanduser(path))
    limit = max(1, min(limit, 1000))
    ql = q.lower().strip()
    lo = _day_epoch(date_from, False) if date_from else None
    hi = _day_epoch(date_to, True) if date_to else None
    items = []
    subdirs = []
    if _remote_dir(src, d):
        # Mount-backed dir: list via /api/fs/list; mtime/size come from the
        # server payload so no per-entry kernel stat touches the mount. A network
        # or HTTP hiccup returns a structured error instead of raising out of the
        # action (matches the other templates' picker error handling).
        try:
            ents, _ = _list_remote(src, d)
        except Exception as exc:  # noqa: BLE001
            # Carry the full success shape (items/total/offset/subdirs) so the
            # frontend's loadDir/buildTiles never touch undefined fields — it
            # keys off `ok:false` to surface the error instead of crashing.
            return {"ok": False, "error": "list-failed", "detail": str(exc)[:200],
                    "dir": _fwd(d), "total": 0, "offset": offset,
                    "items": [], "subdirs": []}
        for e in ents:
            name = e["name"]
            if name.startswith("."):
                continue
            if e.get("is_dir"):
                subdirs.append({"name": name, "path": _fwd(os.path.join(d, name))})
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in PHOTO_EXT:
                continue
            if ql and ql not in name.lower():
                continue
            mt = e.get("mtime") or 0
            if lo is not None and mt < lo:
                continue
            if hi is not None and mt > hi:
                continue
            items.append({
                "name": name,
                "path": _fwd(os.path.join(d, name)),
                "size": e.get("size") or 0,
                "mtime": mt,
                "ext": ext,
                "kind": _kind(ext),
            })
        return _list_dir_result(d, items, subdirs, sort, offset, limit)
    if not os.path.isdir(d):
        raise NotADirectoryError(f"not a directory: {path}")
    with os.scandir(d) as it:
        for entry in it:
            name = entry.name
            if _skip_entry(entry):
                continue
            try:
                if entry.is_dir():
                    subdirs.append({"name": name, "path": _fwd(entry.path)})
                    continue
            except OSError:
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in PHOTO_EXT:
                continue
            if ql and ql not in name.lower():
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if lo is not None and st.st_mtime < lo:
                continue
            if hi is not None and st.st_mtime > hi:
                continue
            items.append(
                {
                    "name": name,
                    "path": _fwd(entry.path),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "ext": ext,
                    "kind": _kind(ext),
                }
            )
    return _list_dir_result(d, items, subdirs, sort, offset, limit)


def _list_dir_result(d, items, subdirs, sort, offset, limit):
    key, rev = SORTS.get(sort, SORTS["new"])
    items.sort(key=key, reverse=rev)
    subdirs.sort(key=lambda e: e["name"].lower())
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "ok": True,
        "dir": _fwd(d),
        "total": total,
        "offset": offset,
        "items": page,
        "subdirs": subdirs,
    }


def thumbs(paths: str) -> dict:
    arr = json.loads(paths) if paths else []
    if len(arr) > 48:
        raise ValueError(f"too many paths in one batch: {len(arr)}")
    results = []
    for p in arr:
        try:
            if not os.path.isfile(p):
                results.append({"path": _fwd(p), "ok": False, "error": "not-found", "detail": "file missing"})
                continue
            results.append(_make_thumb(p))
        except RuntimeError as e:
            if str(e) == "no-decoder":
                results.append({"path": _fwd(p), "ok": False, "error": "no-decoder", "detail": "install pillow-heif"})
            else:
                results.append({"path": _fwd(p), "ok": False, "error": "decode-failed", "detail": str(e)[:120]})
        except Exception as e:
            results.append({"path": _fwd(p), "ok": False, "error": "decode-failed", "detail": str(e)[:120]})
    return {"ok": True, "results": results}


def display(path: str, size: str) -> dict:
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    ext = os.path.splitext(path)[1].lower()
    if _kind(ext) == "native":
        return {"ok": True, "mode": "original"}
    if size == "full":
        return {"ok": True, **_make_variant(path, "full", 0, 92)}
    return {"ok": True, **_make_variant(path, "d2048", 2048, 85)}


def _gps_to_decimal(coord, ref) -> str:
    try:
        d, m, s = [float(x) for x in coord]
        val = d + m / 60.0 + s / 3600.0
        if ref in ("S", "W"):
            val = -val
        return f"{val:.6f}"
    except Exception:
        return ""


def exif(path: str) -> dict:
    from PIL import Image, ExifTags

    file = {
        "name": os.path.basename(path),
        "path": _fwd(path),
        "size": os.path.getsize(path),
        "mtime": os.path.getmtime(path),
    }
    Image.MAX_IMAGE_PIXELS = None
    ext = os.path.splitext(path)[1].lower()
    if ext in {".heic", ".heif"}:
        _register_heif()
    try:
        img = Image.open(path)
        fmt = img.format or ""
        w, h = img.size
        raw = img.getexif()
    except Exception:
        return {"ok": True, "file": file, "image": None, "exif": {}}

    if raw.get(274, 1) in (5, 6, 7, 8):
        w, h = h, w
    image = {"w": w, "h": h, "format": fmt}
    wanted = {
        "DateTimeOriginal", "Make", "Model", "LensModel",
        "FNumber", "ExposureTime", "ISOSpeedRatings", "FocalLength",
    }
    out = {}
    try:
        tagmap = {v: k for k, v in ExifTags.TAGS.items()}
        merged = dict(raw)
        ifd = raw.get_ifd(ExifTags.IFD.Exif) if hasattr(ExifTags, "IFD") else {}
        merged.update(ifd)
        for name in wanted:
            tid = tagmap.get(name)
            if tid is not None and tid in merged:
                out[name] = str(merged[tid])
        gps = raw.get_ifd(ExifTags.IFD.GPSInfo) if hasattr(ExifTags, "IFD") else {}
        if gps:
            lat = _gps_to_decimal(gps.get(2), gps.get(1))
            lon = _gps_to_decimal(gps.get(4), gps.get(3))
            if lat:
                out["GPSLatitude"] = lat
            if lon:
                out["GPSLongitude"] = lon
    except Exception:
        pass
    img.close()
    return {"ok": True, "file": file, "image": image, "exif": out}


def clear_cache() -> dict:
    d = _cache_dir()  # local thumbnail cache under ~ — never a user mount path
    removed = 0
    freed = 0
    for name in os.listdir(d):
        fp = os.path.join(d, name)
        try:
            freed += os.path.getsize(fp)
            os.remove(fp)
            removed += 1
        except Exception:
            pass
    return {"ok": True, "removed": removed, "bytes": freed}


def main(
    action: str = "",
    path: str = "",
    paths: str = "",
    size: str = "fit",
    sort: str = "new",
    offset: int = 0,
    limit: int = 500,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    src: str = "",
) -> dict:
    if action == "check_setup":
        return check_setup()
    if action == "folders":
        return folders(path, src)
    if action == "list_dir":
        return list_dir(path, sort, offset, limit, q, date_from, date_to, src)
    if action == "thumbs":
        return thumbs(paths)
    if action == "display":
        return display(path, size)
    if action == "exif":
        return exif(path)
    if action == "clear_cache":
        return clear_cache()
    raise ValueError(f"unknown action: {action!r}")

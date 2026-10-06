"""The apps gallery's backend (docs/bots.md §1, §3 `/api/bot-apps/*`): ports of
OpenBot listapps.py, importapp.py, mkbuild.py and revealapp.py as plain
functions. Each raises ValueError on bad input, as the originals did.

`import_app` takes base64 of one of:

* a plain .zip of the app folder (Finder "Compress", `zip -r`): the single wrapper folder is stripped;
* a v1 .fused export: a zip carrying manifest.json (`fused_app_file: 1`, `name`, `entry`, `root`) with the app under `root`;
* a v2 .fused export: the "FUSEDAPP" container (magic, u16 version, u16 flags, u32 index size, u32 compressed index size,
  zlib(JSON index), then one zlib stream per file). The index lists files as {path, offset, size, csize, sha256}; every
  stream is checked against its declared size and hash while it is written.

The copy lands in a new folder under `root`; an existing folder with the same name is never touched, the copy gets a
`-2`, `-3`… suffix instead. Paths that would escape the target are refused and a rejected import leaves nothing behind.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import struct
import subprocess
import zipfile
import zlib

from fused_render.bots import apptools


# ------------------------------------------------------------------ list ---
def list_apps(root: str = "") -> dict:
    """Every fused app under `root` (apptools.list_apps, the same scan that feeds
    every bot's APPS section) plus the icon and last-modified time the gallery
    sorts by, newest first: {"root", "apps": [{folder, dir, name, desc, tools, skill, icon, mtime}]}."""
    if not root or not os.path.isabs(root):
        raise ValueError("listapps needs an absolute root")
    apps = []
    if not os.path.isdir(root):
        return {"root": root, "apps": apps}
    for a in apptools.list_apps([root], skip_dir="/nonexistent"):
        d = a["dir"]
        icon = next((i for i in ("icon.svg", "icon.png") if os.path.isfile(os.path.join(d, i))), "")
        mtime = 0.0
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if not x.startswith(".")]
            for fn in fns:
                try:
                    mtime = max(mtime, os.path.getmtime(os.path.join(dp, fn)))
                except OSError:
                    pass
        apps.append({**a, "icon": icon, "mtime": mtime})
    apps.sort(key=lambda a: -a["mtime"])
    return {"root": root, "apps": apps}


# ---------------------------------------------------------------- import ---
UPLOAD_MAX = 64 * 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAGIC = b"FUSEDAPP"
HEADER = struct.Struct("<8sHHII")
SKIP_ROOTS = ("__MACOSX",)
SKIP_NAMES = (".DS_Store",)


def _slug(name):
    s = re.sub(r"\.(zip|fused)$", "", name or "", flags=re.I)
    s = re.sub(r"[^A-Za-z0-9._ -]+", "-", s).strip(" .-") or "app"
    return s[:80]


def _fresh(root, slug):
    cand, n = slug, 1
    while os.path.exists(os.path.join(root, cand)):
        n += 1
        cand = f"{slug}-{n}"
    return os.path.realpath(os.path.join(root, cand))


def _safe_parts(p):
    """Path segments of an archive entry, or None when the entry is junk or unsafe."""
    if not isinstance(p, str) or "\\" in p or "\x00" in p or ":" in p:
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts or parts[0] in SKIP_ROOTS or parts[-1] in SKIP_NAMES:
        return None
    return parts


def _target(dest, parts):
    rel = posixpath.join(*parts)
    target = os.path.realpath(os.path.join(dest, rel))
    if not target.startswith(dest + os.sep):
        raise ValueError(f"unsafe path in archive: {rel}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    return target


# ---- v2 container ----
def _container_index(raw):
    if len(raw) < HEADER.size:
        raise ValueError("not a fused app file (truncated header)")
    magic, version, _flags, isize, icsize = HEADER.unpack(raw[:HEADER.size])
    if version != 2:
        raise ValueError(f"unsupported .fused format version {version}")
    if isize > MAX_INDEX_BYTES or icsize > MAX_INDEX_BYTES or HEADER.size + icsize > len(raw):
        raise ValueError("not a fused app file (bad index size)")
    try:
        idx = zlib.decompressobj().decompress(raw[HEADER.size:HEADER.size + icsize], MAX_INDEX_BYTES + 1)
    except zlib.error as e:
        raise ValueError(f"corrupt .fused index: {e}")
    if len(idx) != isize:
        raise ValueError("file index does not match its declared size")
    try:
        index = json.loads(idx)
    except ValueError as e:
        raise ValueError(f"invalid .fused index: {e}")
    if not isinstance(index, dict) or index.get("fused_app_file") != 2 or not isinstance(index.get("files"), list):
        raise ValueError("not a fused app file (no fused_app_file: 2 index)")
    return index, HEADER.size + icsize


def _extract_container(raw, index, data_start, dest):
    written = 0
    for e in index["files"]:
        parts = _safe_parts(e.get("path")) if isinstance(e, dict) else None
        if parts is None:
            raise ValueError(f"rejected .fused: bad path {e.get('path') if isinstance(e, dict) else e!r}")
        off, size, csize, sha = e.get("offset"), e.get("size"), e.get("csize"), e.get("sha256")
        if not all(isinstance(v, int) and v >= 0 for v in (off, size, csize)) or data_start + off + csize > len(raw):
            raise ValueError(f"rejected .fused: entry {e['path']!r} points past the end of the file")
        if size > UPLOAD_MAX:
            raise ValueError(f"rejected .fused: entry {e['path']!r} is too large")
        try:
            body = zlib.decompressobj().decompress(raw[data_start + off:data_start + off + csize], size + 1)
        except zlib.error as ex:
            raise ValueError(f"corrupt data for {e['path']!r}: {ex}")
        if len(body) != size or (isinstance(sha, str) and hashlib.sha256(body).hexdigest() != sha):
            raise ValueError(f"rejected .fused: entry {e['path']!r} does not match its declared size or hash")
        with open(_target(dest, parts), "wb") as out:
            out.write(body)
        written += 1
    return written


# ---- zips: plain folder zip or v1 export ----
def _zip_plan(zf):
    """(files, name_hint): the entries to write as (info, parts relative to the app root) and a name from the archive."""
    manifest = None
    if "manifest.json" in zf.namelist():
        try:
            with zf.open("manifest.json") as f:
                m = json.loads(f.read(1 << 20))
            if isinstance(m, dict) and m.get("fused_app_file") == 1:
                manifest = m
        except (ValueError, OSError, zipfile.BadZipFile):
            pass
    entries = []
    for info in zf.infolist():
        parts = _safe_parts(info.filename.replace("\\", "/"))
        if parts is not None:
            entries.append((info, parts))
    if manifest is not None:
        root = [x for x in str(manifest.get("root") or "").split("/") if x]
        if "root" not in manifest and not any(p == ["index.html"] for _, p in entries) \
                and any(p == ["files", "index.html"] for _, p in entries):
            # fused-render's own v1 writer (appfile.py) carries no `root`: the app sits under files/.
            root = ["files"]
        files = [(i, p[len(root):]) for i, p in entries if not i.is_dir() and p[:len(root)] == root and len(p) > len(root)]
        return files, str(manifest.get("name") or "")
    if not entries:
        raise ValueError("the archive is empty")
    tops = {p[0] for _, p in entries}
    strip = 1 if len(tops) == 1 and not any(len(p) == 1 and not i.is_dir() for i, p in entries) else 0
    files = [(i, p[strip:]) for i, p in entries if not i.is_dir() and len(p) > strip]
    return files, (next(iter(tops)) if strip else "")


def _extract_zip(zf, files, dest):
    for info, parts in files:
        with zf.open(info) as src, open(_target(dest, parts), "wb") as out:
            shutil.copyfileobj(src, out)
    return len(files)


def import_app(root: str = "", name: str = "", data: str = "") -> dict:
    """Copy an uploaded app (base64 `data`) into a fresh folder under `root`:
    {"dir", "folder", "files", "fusedApp"}."""
    if not root or not os.path.isabs(root):
        raise ValueError("importapp needs an absolute root")
    raw = base64.b64decode(data or "")
    if not raw:
        raise ValueError("empty upload")
    if len(raw) > UPLOAD_MAX:
        raise ValueError(f"{name}: too large ({len(raw) // 1048576} MB; limit {UPLOAD_MAX // 1048576} MB)")

    os.makedirs(root, exist_ok=True)
    if raw.startswith(MAGIC):
        index, data_start = _container_index(raw)
        paths = {e.get("path") for e in index["files"] if isinstance(e, dict)}
        if "index.html" not in paths:
            raise ValueError("this .fused file has no index.html; only app exports can be imported here")
        dest = _fresh(root, _slug(str(index.get("name") or "") or name))
        os.makedirs(dest)
        try:
            written = _extract_container(raw, index, data_start, dest)
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise
    else:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise ValueError(f"{name or 'upload'} is neither a zip nor a fused app file")
        files, hint = _zip_plan(zf)
        if not any(p == ["index.html"] for _, p in files):
            raise ValueError("no index.html at the top of the archive; a fused app needs one")
        dest = _fresh(root, _slug(hint) if hint else _slug(name))
        os.makedirs(dest)
        try:
            written = _extract_zip(zf, files, dest)
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise

    with open(os.path.join(dest, "index.html"), "rb") as f:
        head = f.read(4096).decode("utf-8", "replace")
    return {"dir": dest, "folder": os.path.basename(dest), "files": written, "fusedApp": 'name="fused-app"' in head}


# ------------------------------------------------------- mkbuild / reveal ---
def mkbuild(dir: str = "") -> dict:  # noqa: A002 — the wire name
    """Create the folder a build lands in, so the Claude task can start with
    target=that folder: {"dir", "existed"} (existed = it already had files)."""
    if not dir or not os.path.isabs(dir):
        raise ValueError("mkbuild needs an absolute dir")
    os.makedirs(dir, exist_ok=True)
    return {"dir": dir, "existed": bool(os.listdir(dir))}


def reveal(dir: str = "") -> dict:  # noqa: A002
    """Open an app's folder in Finder (the viewer's "Open in Finder")."""
    if not dir or not os.path.isabs(dir) or not os.path.isdir(dir):
        raise ValueError("revealapp needs an absolute path to an existing folder")
    subprocess.Popen(["open", dir], close_fds=False)
    return {"dir": dir}

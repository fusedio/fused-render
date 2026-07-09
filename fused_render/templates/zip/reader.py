"""Reader backing zip/template.html. Inspects and extracts from a zip archive
(also .jar/.whl/.egg, which are zip under the hood) via stdlib zipfile.

Actions (``action`` param):
  - list         : every member as {path, size, compressed, modified, is_dir}.
  - preview      : extract ONE member to a temp dir and return its real path, so
                   the page can hand it to the app's normal template pipeline
                   (an inner .csv opens the csv viewer, a .png the image viewer…).
  - extract      : write ONE member next to the archive under <stem>/…, return
                   the destination.
  - extract_all  : write EVERY member next to the archive under <stem>/….

Every extraction is guarded against zip-slip (a member whose path escapes the
destination via .. or an absolute path is rejected).
"""
import hashlib
import os
import shutil
import tempfile
import zipfile


def _fmt_dt(date_time):
    """ZipInfo.date_time is a (Y, M, D, h, m, s) tuple; render it ISO-ish.
    Returns '' for the zip sentinel epoch (1980-00-00) some tools emit."""
    try:
        y, mo, d, h, mi, s = date_time
    except (TypeError, ValueError):
        return ""
    if y < 1980 or mo < 1 or d < 1:
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"


def _list_entries(zf):
    entries = []
    for info in zf.infolist():
        entries.append(
            {
                "path": info.filename,
                "size": info.file_size,
                "compressed": info.compress_size,
                "modified": _fmt_dt(info.date_time),
                "is_dir": info.is_dir(),
            }
        )
    return {"entries": entries}


def _getinfo(zf, entry):
    try:
        return zf.getinfo(entry)
    except KeyError:
        raise FileNotFoundError(f"no such entry in archive: {entry}")


def _within(root, target):
    """True if `target` is `root` itself or lives inside it — the zip-slip guard."""
    root = os.path.realpath(root)
    target = os.path.realpath(target)
    return target == root or target.startswith(root + os.sep)


def _extract_one(zf, info, dest_root):
    """Write one member under dest_root, preserving its in-archive relative path.
    Returns the written file's absolute path, or None for a directory member.
    Refuses any member whose resolved path would escape dest_root."""
    target = os.path.join(dest_root, info.filename)
    if not _within(dest_root, target):
        raise ValueError(f"unsafe path in archive (path traversal): {info.filename!r}")
    if info.is_dir():
        os.makedirs(target, exist_ok=True)
        return None
    os.makedirs(os.path.dirname(target) or dest_root, exist_ok=True)
    with zf.open(info) as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return os.path.realpath(target)


def _preview_root(file):
    """Stable per-archive temp dir. Keyed on the archive's real path so repeated
    previews of the same zip reuse one dir instead of littering temp."""
    key = hashlib.sha1(os.path.realpath(file).encode("utf-8")).hexdigest()[:16]
    root = os.path.join(tempfile.gettempdir(), "fused-render-zip", key)
    os.makedirs(root, exist_ok=True)
    return root


def _dest_root(file):
    """Persistent extract destination: a folder named after the archive, beside
    it (sample.zip -> <dir>/sample/)."""
    real = os.path.realpath(file)
    stem = os.path.splitext(os.path.basename(real))[0]
    return os.path.join(os.path.dirname(real), stem)


def _preview(zf, file, entry):
    info = _getinfo(zf, entry)
    if info.is_dir():
        return {"is_dir": True}
    target = _extract_one(zf, info, _preview_root(file))
    return {"path": target, "size": info.file_size}


def _extract(zf, file, entry):
    info = _getinfo(zf, entry)
    dest = _dest_root(file)
    os.makedirs(dest, exist_ok=True)
    target = _extract_one(zf, info, dest)
    return {"dest": dest, "path": target, "count": 0 if target is None else 1}


def _extract_all(zf, file):
    dest = _dest_root(file)
    os.makedirs(dest, exist_ok=True)
    count = 0
    for info in zf.infolist():
        if _extract_one(zf, info, dest) is not None:
            count += 1
    return {"dest": dest, "count": count}


def main(file: str, action: str = "list", entry: str = "") -> dict:
    with zipfile.ZipFile(file) as zf:
        if action == "list":
            return _list_entries(zf)
        if action == "preview":
            return _preview(zf, file, entry)
        if action == "extract":
            return _extract(zf, file, entry)
        if action == "extract_all":
            return _extract_all(zf, file)
        raise ValueError(f"unknown action: {action!r}")

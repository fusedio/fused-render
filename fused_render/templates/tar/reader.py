"""Reader backing tar/template.html. Inspects and extracts from tar archives
(.tar plus the compressed variants .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz)
via stdlib tarfile, and also handles a *single* gzip/bzip2/xz-compressed file
(foo.json.gz) that is NOT a tar — those decompress to one member.

Actions (``action`` param) mirror the zip reader:
  - list         : every member as {path, size, compressed, modified, is_dir}.
  - preview      : decompress/extract ONE member to a temp dir and return its
                   real path, so the page can hand it to the app's normal
                   template pipeline (an inner .csv opens the csv viewer, …).
  - extract      : write ONE member next to the archive under <stem>/…, return
                   the destination.
  - extract_all  : write EVERY member next to the archive under <stem>/….

Every extraction is guarded against path traversal (a member whose path escapes
the destination via .. or an absolute path is rejected), and non-regular members
(symlinks, hardlinks, devices, fifos) are refused — never materialized on disk.
"""
import bz2
import gzip
import hashlib
import lzma
import os
import shutil
import tarfile
import tempfile

# Compression suffixes for a single (non-tar) compressed file, longest first so
# ".tar.gz" is never treated as a plain ".gz" wrapper. Maps suffix -> opener.
_SINGLE_OPENERS = (
    (".gz", gzip.open),
    (".bz2", bz2.open),
    (".xz", lzma.open),
    (".lzma", lzma.open),
)


def _fmt_dt(epoch):
    """tarfile stores mtime as epoch seconds; render it ISO-ish in local time.
    Returns '' for the 0 sentinel (some tools emit it for synthetic members)."""
    if not epoch:
        return ""
    import datetime

    try:
        dt = datetime.datetime.fromtimestamp(epoch)
    except (OverflowError, OSError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _within(root, target):
    """True if `target` is `root` itself or lives inside it — the traversal guard."""
    root = os.path.realpath(root)
    target = os.path.realpath(target)
    return target == root or target.startswith(root + os.sep)


def _open_tar(file):
    """Open `file` as a tar (any compression), or None if it is not a tar."""
    try:
        return tarfile.open(file, "r:*")
    except tarfile.ReadError:
        return None


def _single_name(file):
    """Inner filename for a plain compressed file: strip the compression suffix
    (foo.json.gz -> foo.json). Falls back to the basename when there is no known
    suffix left to strip."""
    base = os.path.basename(file)
    low = base.lower()
    for suffix, _ in _SINGLE_OPENERS:
        if low.endswith(suffix):
            return base[: -len(suffix)] or base
    return base


def _single_opener(file):
    low = file.lower()
    for suffix, opener in _SINGLE_OPENERS:
        if low.endswith(suffix):
            return opener
    return gzip.open


# --- listing ---------------------------------------------------------------

def _list_tar(tf):
    entries = []
    for m in tf.getmembers():
        # Only files and dirs are browsable; links/devices are shown so the
        # listing is honest, but extraction will refuse them.
        entries.append(
            {
                "path": m.name,
                "size": m.size,
                "compressed": m.size,  # tar has no per-member compressed size
                "modified": _fmt_dt(m.mtime),
                "is_dir": m.isdir(),
            }
        )
    return {"entries": entries}


def _list_single(file):
    name = _single_name(file)
    # Decompressed size is unknown without reading the whole stream; leave blank.
    return {"entries": [{"path": name, "size": None, "compressed": os.path.getsize(file),
                         "modified": "", "is_dir": False}]}


# --- extraction helpers ----------------------------------------------------

def _write_target(src, target, dest_root, readonly):
    """Stream `src` to `target` and return the real path.

    `readonly` is the preview mode: the copy lands 0444 (a preview copy is a
    throwaway — an edit "saved" to it never reaches the archive, so the
    permission bit routes it through the app's read-only contract). It is
    written to a unique temp file and os.replace'd into place, so a stale
    read-only copy from an earlier preview is swapped out without an unlink
    window and a concurrent preview of the same member never observes a
    half-written or permission-flapping file. Plain extraction keeps the
    original open("wb") semantics — including failing loudly (EACCES) on a
    write-protected existing file rather than silently replacing it."""
    if not readonly:
        with src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return os.path.realpath(target)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or dest_root)
    try:
        with src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.chmod(tmp, 0o444)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return os.path.realpath(target)


def _extract_member(tf, member, dest_root, readonly=False):
    """Write one tar member under dest_root, preserving its relative path.
    Returns the written file's absolute path, or None for a directory or a
    skipped non-regular member. Refuses any member that escapes dest_root."""
    rel = member.name.replace("\\", "/")
    target = os.path.join(dest_root, *[p for p in rel.split("/") if p and p != "."])
    if not _within(dest_root, target):
        raise ValueError(f"unsafe path in archive (path traversal): {member.name!r}")
    if member.isdir():
        os.makedirs(target, exist_ok=True)
        return None
    if not member.isreg():
        # Symlink/hardlink/device/fifo: never materialize (link-target attacks).
        return None
    os.makedirs(os.path.dirname(target) or dest_root, exist_ok=True)
    src = tf.extractfile(member)
    if src is None:
        return None
    return _write_target(src, target, dest_root, readonly)


def _extract_single(file, dest_root, readonly=False):
    """Decompress a plain compressed file into dest_root under its inner name."""
    name = _single_name(file)
    target = os.path.join(dest_root, name)
    if not _within(dest_root, target):
        raise ValueError(f"unsafe path in archive (path traversal): {name!r}")
    os.makedirs(os.path.dirname(target) or dest_root, exist_ok=True)
    return _write_target(_single_opener(file)(file, "rb"), target, dest_root,
                         readonly)


def _preview_root(file):
    """Stable per-archive temp dir, keyed on the archive's real path so repeated
    previews of the same archive reuse one dir instead of littering temp."""
    key = hashlib.sha1(os.path.realpath(file).encode("utf-8")).hexdigest()[:16]
    root = os.path.join(tempfile.gettempdir(), "fused-render-tar", key)
    os.makedirs(root, exist_ok=True)
    return root


def _dest_root(file):
    """Persistent extract destination beside the archive, named after it with
    the compression suffix(es) stripped (sample.tar.gz -> <dir>/sample/)."""
    real = os.path.realpath(file)
    base = os.path.basename(real)
    low = base.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz",
                   ".tar", ".gz", ".bz2", ".xz", ".lzma"):
        if low.endswith(suffix):
            base = base[: -len(suffix)]
            break
    stem = base or os.path.splitext(os.path.basename(real))[0]
    return os.path.join(os.path.dirname(real), stem)


# --- actions ---------------------------------------------------------------

def _member(tf, entry):
    try:
        return tf.getmember(entry)
    except KeyError:
        raise FileNotFoundError(f"no such entry in archive: {entry}")


def _preview(file, entry):
    tf = _open_tar(file)
    if tf is None:
        target = _extract_single(file, _preview_root(file), readonly=True)
        return {"path": target, "size": os.path.getsize(target)}
    with tf:
        member = _member(tf, entry)
        if member.isdir():
            return {"is_dir": True}
        target = _extract_member(tf, member, _preview_root(file), readonly=True)
        if target is None:
            raise ValueError(f"cannot preview non-file member: {entry}")
        return {"path": target, "size": member.size}


def _extract(file, entry):
    dest = _dest_root(file)
    os.makedirs(dest, exist_ok=True)
    tf = _open_tar(file)
    if tf is None:
        target = _extract_single(file, dest)
        return {"dest": dest, "path": target, "count": 1}
    with tf:
        member = _member(tf, entry)
        target = _extract_member(tf, member, dest)
        return {"dest": dest, "path": target, "count": 0 if target is None else 1}


def _extract_all(file):
    dest = _dest_root(file)
    os.makedirs(dest, exist_ok=True)
    tf = _open_tar(file)
    if tf is None:
        _extract_single(file, dest)
        return {"dest": dest, "count": 1}
    with tf:
        count = 0
        for member in tf.getmembers():
            if _extract_member(tf, member, dest) is not None:
                count += 1
        return {"dest": dest, "count": count}


def main(file: str, action: str = "list", entry: str = "") -> dict:
    if action == "list":
        tf = _open_tar(file)
        if tf is None:
            return _list_single(file)
        with tf:
            return _list_tar(tf)
    if action == "preview":
        return _preview(file, entry)
    if action == "extract":
        return _extract(file, entry)
    if action == "extract_all":
        return _extract_all(file)
    raise ValueError(f"unknown action: {action!r}")

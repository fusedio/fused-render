"""The ``.fused`` v2 physical format: an opaque container, not an archive.

v1 (D385) was a zip with its extension renamed. Mail scanners classify an
attachment by its BYTES, not its name: Gmail saw ``PK``, walked the members,
found an ``.html`` and a ``.py`` in a script-shaped bundle, and flagged the
file as suspicious — the rename bought nothing. v2 is a format nothing
classifies as an archive: a fixed magic, a versioned header, a deflated JSON
index and per-file deflate streams. To a scanner it is an unknown binary,
the same class ``.sqlite`` or ``.blend`` attachments fall in.

Layout (all integers little-endian)::

    0   8 bytes  magic          b"FUSEDAPP"
    8   u16      version        2
    10  u16      flags          0 (reserved)
    12  u32      index_size     decompressed index length
    16  u32      index_csize    compressed index length
    20  ...      index          zlib(JSON), ``index_csize`` bytes
    20+index_csize  ...         data section: one zlib stream per file

The index carries what v1's ``manifest.json`` did (``fused_app_file: 2``,
``name``, ``entry``) plus ``files``: a list of ``{path, offset, size, csize,
sha256}`` where ``offset`` is relative to the START OF THE DATA SECTION, so
the writer can stream every file into a temp before it knows the index's own
compressed length. ``sha256`` is of the decompressed bytes and is verified on
extract.

Reading is as hardened as ``zip_import`` is for zips, because a ``.fused``
that arrived by mail is exactly as untrusted as an uploaded template pack —
and every declared number in the header and index is attacker-controlled:

* the index is capped BEFORE decompression (declared size AND the bytes a
  bounded ``decompressobj`` actually yields), so a crafted header cannot
  become a memory bomb;
* every ``path`` must be relative, forward-slash, free of ``\\``, empty
  segments, ``.`` and ``..``, unique, and never both a file and a directory
  of another file (``a`` and ``a/b``) — checked for the whole index before
  any byte lands, so a rejected file leaves nothing behind;
* per-entry and total caps are enforced on bytes actually WRITTEN, not on the
  declared sizes; a stream that yields more than it declared, or fewer bytes,
  or the wrong hash, rejects the whole extract.

This module knows nothing about apps, markers or the cache layout — that is
``appfile.py``'s job; this is the codec plus the safe extractor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import zlib

MAGIC = b"FUSEDAPP"
VERSION = 2
_HEADER = struct.Struct("<8sHHII")
HEADER_SIZE = _HEADER.size  # 20

# Index cap, both the declared decompressed size and the bytes actually
# yielded. 4000 files (appfile.MAX_EXPORT_ENTRIES) of path + hash + numbers is
# ~600 KiB of JSON; 4 MiB is generous and still a bounded read.
MAX_INDEX_BYTES = 4 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COPY_CHUNK = 1024 * 1024


class ContainerError(Exception):
    """A malformed, oversized or unsafe container. The message is fit to
    show a user."""


def is_container(path: str) -> bool:
    """Whether the file at ``path`` starts with this format's magic."""
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def path_problem(rel: object) -> str | None:
    """Why ``rel`` is not a safe payload-relative path, or None if it is.

    Forward slashes only: the writer never emits a backslash, and on Windows
    a backslash inside a "segment" would act as a separator after
    ``os.path.join`` — ``..\\..\\x`` style paths would escape the extract
    dir while passing a ``/``-split ``..`` check. NUL is refused because it
    truncates a C path.
    """
    if not isinstance(rel, str) or not rel:
        return "empty path"
    if "\\" in rel or "\x00" in rel:
        return f"path carries a backslash or NUL: {rel!r}"
    if rel.startswith("/") or os.path.isabs(rel):
        return f"absolute path not allowed: {rel!r}"
    if any(seg in ("", ".", "..") for seg in rel.split("/")):
        return f"path escape or empty segment not allowed: {rel!r}"
    return None


# ---------------------------------------------------------------- writing


def _put_member(data, rel: str, src: str | bytes, offset: int) -> dict:
    """Deflate one member onto ``data``; answer its index entry."""
    comp = zlib.compressobj(6)
    h = hashlib.sha256()
    size = csize = 0

    def put(chunk: bytes) -> None:
        nonlocal size, csize
        size += len(chunk)
        h.update(chunk)
        out = comp.compress(chunk)
        csize += len(out)
        data.write(out)

    if isinstance(src, bytes):
        put(src)
    else:
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(_COPY_CHUNK), b""):
                put(chunk)
    tail = comp.flush()
    csize += len(tail)
    data.write(tail)
    return {"path": rel, "offset": offset, "size": size, "csize": csize,
            "sha256": h.hexdigest()}


def write(out_path: str, index: dict, members: list[tuple[str, str | bytes]]) -> dict:
    """Write a container at ``out_path`` (created; the caller owns the
    non-overwrite decision and any tempfile-then-rename).

    ``index`` is the caller's metadata (``name``, ``entry``, ...); this adds
    ``fused_app_file`` and ``files``. ``members`` is ``[(payload-relative
    path, filesystem source path | literal bytes)]``. Returns the index as
    written.
    """
    for rel, _src in members:
        problem = path_problem(rel)
        if problem:
            raise ContainerError(f"refusing to write {problem}")
    parent = os.path.dirname(os.path.abspath(out_path)) or "."
    fd, data_tmp = tempfile.mkstemp(prefix=".fused-data-", dir=parent)
    files: list[dict] = []
    try:
        with os.fdopen(fd, "wb") as data:
            offset = 0
            for rel, src in members:
                entry = _put_member(data, rel, src, offset)
                files.append(entry)
                offset += entry["csize"]
        full_index = {**index, "fused_app_file": VERSION, "files": files}
        raw = json.dumps(full_index, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_INDEX_BYTES:
            raise ContainerError(
                f"file index is too large ({len(raw)} bytes > {MAX_INDEX_BYTES})")
        cindex = zlib.compress(raw, 9)
        with open(out_path, "wb") as out:
            out.write(_HEADER.pack(MAGIC, VERSION, 0, len(raw), len(cindex)))
            out.write(cindex)
            with open(data_tmp, "rb") as data:
                shutil.copyfileobj(data, out, _COPY_CHUNK)
        return full_index
    finally:
        if os.path.exists(data_tmp):
            os.unlink(data_tmp)


# ---------------------------------------------------------------- reading


def _inflate_bounded(raw: bytes, cap: int) -> bytes:
    """Decompress ``raw`` yielding at most ``cap + 1`` bytes — one past the
    cap so the caller can tell "at the cap" from "over it" — never the
    declared length."""
    d = zlib.decompressobj()
    try:
        out = d.decompress(raw, cap + 1)
    except zlib.error as exc:
        raise ContainerError(f"corrupt compressed data: {exc}")
    return out


def read_index(path: str, *, max_entries: int, max_entry_bytes: int,
               max_total_bytes: int) -> dict:
    """Parse and validate the header + index of the container at ``path``.
    Nothing from the data section is read. The returned dict is the index
    as written, with ``_data_start`` (absolute offset of the data section)
    added for the member readers."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_SIZE)
            if len(head) < HEADER_SIZE:
                raise ContainerError("not a fused app file (truncated header)")
            magic, version, _flags, isize, icsize = _HEADER.unpack(head)
            if magic != MAGIC:
                raise ContainerError("not a fused app file (bad magic)")
            if version != VERSION:
                raise ContainerError(
                    f"unsupported .fused format version {version} — "
                    "update fused-render to open this file")
            if isize > MAX_INDEX_BYTES or icsize > MAX_INDEX_BYTES:
                raise ContainerError(
                    f"file index is too large (> {MAX_INDEX_BYTES} bytes) — not a fused app file")
            cindex = f.read(icsize)
            if len(cindex) != icsize:
                raise ContainerError("not a fused app file (truncated index)")
            size = os.fstat(f.fileno()).st_size
    except OSError as exc:
        raise ContainerError(f"not a readable .fused file: {exc}")
    raw = _inflate_bounded(cindex, MAX_INDEX_BYTES)
    if len(raw) > MAX_INDEX_BYTES or len(raw) != isize:
        raise ContainerError("file index does not match its declared size — not a fused app file")
    try:
        index = json.loads(raw)
    except ValueError as exc:
        raise ContainerError(f"invalid index in .fused file: {exc}")
    if not isinstance(index, dict) or index.get("fused_app_file") != VERSION:
        raise ContainerError("not a fused app file (index carries no fused_app_file: 2)")
    files = index.get("files")
    if not isinstance(files, list):
        raise ContainerError("invalid index: files is not a list")
    if len(files) > max_entries:
        raise ContainerError(f"file has too many entries ({len(files)} > {max_entries})")
    data_start = HEADER_SIZE + icsize
    data_len = size - data_start
    seen: set[str] = set()
    dirs: set[str] = set()
    total = 0
    for f in files:
        if not isinstance(f, dict):
            raise ContainerError("invalid index: file entry is not an object")
        rel = f.get("path")
        problem = path_problem(rel)
        if problem:
            raise ContainerError(f"rejected .fused: {problem}")
        if rel in seen or rel in dirs:
            raise ContainerError(f"rejected .fused: duplicate or conflicting path {rel!r}")
        seen.add(rel)
        parts = rel.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d in seen:
                raise ContainerError(
                    f"rejected .fused: {d!r} is both a file and a directory")
            dirs.add(d)
        for key in ("offset", "size", "csize"):
            v = f.get(key)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ContainerError(f"invalid index: {key} of {rel!r} is not a non-negative integer")
        if f["size"] > max_entry_bytes:
            raise ContainerError(
                f"entry {rel!r} is too large ({f['size']} bytes > {max_entry_bytes})")
        if f["offset"] + f["csize"] > data_len:
            raise ContainerError(f"rejected .fused: entry {rel!r} points past the end of the file")
        total += f["size"]
        if total > max_total_bytes:
            raise ContainerError(f"file is too large to open (> {max_total_bytes} bytes)")
        if not isinstance(f.get("sha256"), str) or not _SHA256_RE.match(f["sha256"]):
            raise ContainerError(f"invalid index: bad sha256 for {rel!r}")
    index["_data_start"] = data_start
    return index


def find(index: dict, rel: str) -> dict | None:
    for f in index["files"]:
        if f["path"] == rel:
            return f
    return None


def read_member(path: str, index: dict, rel: str, cap: int) -> bytes | None:
    """The decompressed bytes of ONE member, bounded by ``cap`` — one past it
    is returned so the caller can detect "over". None when ``rel`` is not in
    the index. Nothing else is read or extracted."""
    entry = find(index, rel)
    if entry is None:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(index["_data_start"] + entry["offset"])
            raw = f.read(entry["csize"])
    except OSError as exc:
        raise ContainerError(f"not a readable .fused file: {exc}")
    return _inflate_bounded(raw, cap)


def extract(path: str, index: dict, dest: str) -> int:
    """Extract every member into ``dest`` (created). Caps come from the
    ``read_index`` call that produced ``index``; here each stream is checked
    against what it DECLARED (size and sha256) as it is written, so a body
    that disagrees with its index rejects the whole extract and ``dest`` is
    removed. Returns bytes written."""
    os.makedirs(dest, exist_ok=True)
    written = 0
    try:
        with open(path, "rb") as src:
            for entry in index["files"]:
                target = os.path.join(dest, *entry["path"].split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src.seek(index["_data_start"] + entry["offset"])
                remaining = entry["csize"]
                d = zlib.decompressobj()
                h = hashlib.sha256()
                got = 0
                with open(target, "wb") as out:
                    while remaining > 0:
                        chunk = src.read(min(_COPY_CHUNK, remaining))
                        if not chunk:
                            raise ContainerError(
                                f"rejected .fused: entry {entry['path']!r} is truncated")
                        remaining -= len(chunk)
                        piece = d.decompress(chunk)
                        got += len(piece)
                        if got > entry["size"]:
                            raise ContainerError(
                                f"rejected .fused: entry {entry['path']!r} inflates past its declared size")
                        h.update(piece)
                        out.write(piece)
                    tail = d.flush()
                    got += len(tail)
                    h.update(tail)
                    out.write(tail)
                if got != entry["size"] or h.hexdigest() != entry["sha256"]:
                    raise ContainerError(
                        f"rejected .fused: entry {entry['path']!r} does not match its index")
                written += got
    except zlib.error as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise ContainerError(f"corrupt compressed data: {exc}")
    except (ContainerError, OSError):
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return written

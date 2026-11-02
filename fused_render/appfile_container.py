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

The same codec carries two KINDS of file, told apart by one index key
(``KINDS``): an app file's index carries ``fused_app_file: 2`` (what v1's
``manifest.json`` did, plus ``name`` and ``entry``), a bot package's
(``.fusedbot``, bots/package.py) carries ``fused_bot_file: 1``. An index
never carries both, and each reader asks for its kind, so a bot package
dropped where an app file belongs is refused with a sentence that says so
rather than half-opened. Either index carries the UTC ``exported_at`` stamp the writer adds so a
consumer can see when the app was exported, plus ``files``: a list of ``{path, offset, size, csize,
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

from fused_render._platform import is_within

MAGIC = b"FUSEDAPP"
VERSION = 2

# kind -> (the index key that names it, that key's version). The header's
# VERSION is the CODEC's version and is shared; the kind key versions what
# the index means, so a bot package can evolve without touching app files.
KINDS = {"app": ("fused_app_file", 2), "bot": ("fused_bot_file", 1)}
# What a refusal calls a file of each kind (the app wording predates kinds
# and stays byte-identical for every existing caller).
_NOUN = {"app": "fused app file", "bot": "fused bot package"}
# The refusal when a reader of `kind` is handed the OTHER kind: it says what
# the file is, which is what tells the user where to open it.
_WRONG_KIND = {"app": "this is a fused bot package, not an app file",
               "bot": "this is a fused app file, not a bot package"}
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

    Forward slashes only: the writer never emits a backslash or a colon, so
    a path carrying one was not written by it. NUL is refused because it
    truncates a C path.
    """
    if not isinstance(rel, str) or not rel:
        return "empty path"
    if "\\" in rel or "\x00" in rel or ":" in rel:
        # Nothing legitimate in an app folder needs a ':'.
        return f"path carries a backslash, colon or NUL: {rel!r}"
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


def _kind_key(kind: str) -> tuple[str, int]:
    try:
        return KINDS[kind]
    except KeyError:
        raise ContainerError(f"unknown container kind {kind!r}")


def write(out_path: str, index: dict, members: list[tuple[str, str | bytes]],
          *, kind: str = "app") -> dict:
    """Write a container at ``out_path`` (created; the caller owns the
    non-overwrite decision and any tempfile-then-rename).

    ``index`` is the caller's metadata (``name``, ``entry``, ...); this adds
    the ``kind``'s key (``KINDS``: ``fused_app_file`` by default) and
    ``files``. An ``index`` that already carries ANOTHER kind's key is
    refused: a file claiming to be both would open as whichever reader saw
    it first. ``members`` is ``[(payload-relative path, filesystem source
    path | literal bytes)]``. Returns the index as written.
    """
    key, key_version = _kind_key(kind)
    for other, (other_key, _v) in KINDS.items():
        if other != kind and other_key in index:
            raise ContainerError(
                f"refusing to write a {_NOUN[kind]} whose index carries {other_key}")
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
        full_index = {**index, key: key_version, "files": files}
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


def _read_raw_index(path: str, noun: str) -> tuple[dict, int, int]:
    """The parsed index of the container at ``path`` plus the data section's
    start and the file's size — header and index only, every declared number
    bounded before it is trusted. ``noun`` names the file in refusals."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_SIZE)
            if len(head) < HEADER_SIZE:
                raise ContainerError(f"not a {noun} (truncated header)")
            magic, version, _flags, isize, icsize = _HEADER.unpack(head)
            if magic != MAGIC:
                raise ContainerError(f"not a {noun} (bad magic)")
            if version != VERSION:
                raise ContainerError(
                    f"unsupported .fused format version {version} — "
                    "update fused-render to open this file")
            if isize > MAX_INDEX_BYTES or icsize > MAX_INDEX_BYTES:
                raise ContainerError(
                    f"file index is too large (> {MAX_INDEX_BYTES} bytes) — not a {noun}")
            cindex = f.read(icsize)
            if len(cindex) != icsize:
                raise ContainerError(f"not a {noun} (truncated index)")
            size = os.fstat(f.fileno()).st_size
    except OSError as exc:
        raise ContainerError(f"not a readable .fused file: {exc}")
    raw = _inflate_bounded(cindex, MAX_INDEX_BYTES)
    if len(raw) > MAX_INDEX_BYTES or len(raw) != isize:
        raise ContainerError(f"file index does not match its declared size — not a {noun}")
    try:
        index = json.loads(raw)
    except (ValueError, RecursionError) as exc:  # a few KB of nested `[` inflates to a RecursionError
        raise ContainerError(f"invalid index in .fused file: {exc}")
    # A non-object index claims no kind: the kind check names the refusal.
    return (index if isinstance(index, dict) else {}), HEADER_SIZE + icsize, size


def _claims(index: dict) -> list[str]:
    """Every kind whose KEY IS PRESENT in ``index`` (a null or odd value still
    counts as a claim): the one predicate every reader and the writer share,
    so a file can never be "an app" to one door and "a bot" to another."""
    return [k for k, (key, _ver) in KINDS.items() if key in index]


def _kind_in(index: dict) -> str | None:
    """Which ``KINDS`` entry ``index`` claims: None when it claims none, or
    more than one, or the one it claims is not at the version this build
    speaks (that file is refused by its reader with the version sentence)."""
    found = _claims(index)
    if len(found) != 1:
        return None
    key, ver = KINDS[found[0]]
    return found[0] if index.get(key) == ver else None


def kind_of(path: str) -> str | None:
    """``"app"`` | ``"bot"`` | None for the file at ``path``: a bounded read
    of the header and index ONLY, so a dropped file can be routed to the page
    that opens it without validating (or trusting) a single member."""
    try:
        index, _start, _size = _read_raw_index(path, "fused file")
    except ContainerError:
        return None
    return _kind_in(index)


def read_index(path: str, *, max_entries: int, max_entry_bytes: int,
               max_total_bytes: int, kind: str = "app") -> dict:
    """Parse and validate the header + index of the container at ``path``.
    Nothing from the data section is read. The returned dict is the index
    as written, with ``_data_start`` (absolute offset of the data section)
    added for the member readers. ``kind`` (``KINDS``) is the kind the
    caller can open; a file of another kind is refused with a sentence that
    names what it is, so the user learns where it does open."""
    key, key_version = _kind_key(kind)
    noun = _NOUN[kind]
    index, data_start, size = _read_raw_index(path, noun)
    claims = _claims(index)
    if len(claims) > 1:
        raise ContainerError(f"not a {noun} (its index claims more than one kind)")
    if claims and claims[0] != kind:
        raise ContainerError(_WRONG_KIND[kind])
    if index.get(key) != key_version:
        raise ContainerError(f"not a {noun} (index carries no {key}: {key_version})")
    files = index.get("files")
    if not isinstance(files, list):
        raise ContainerError("invalid index: files is not a list")
    if len(files) > max_entries:
        raise ContainerError(f"file has too many entries ({len(files)} > {max_entries})")
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
    # `csize` is attacker-controlled up to the file size, and this runs on a
    # hub-card thumbnail: bound the COMPRESSED read too. Deflate cannot
    # inflate by more than a few bytes per block, so this many compressed
    # bytes always suffice to yield `cap + 1` output when there is that much.
    want = min(entry["csize"], cap + cap // 8192 + 64)
    try:
        with open(path, "rb") as f:
            f.seek(index["_data_start"] + entry["offset"])
            raw = f.read(want)
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
    root = os.path.normpath(dest)
    written = 0
    try:
        with open(path, "rb") as src:
            for entry in index["files"]:
                target = os.path.normpath(os.path.join(dest, *entry["path"].split("/")))
                # Redundant with path_problem on purpose (zip_import keeps the
                # same belt-and-braces): normalization catches shapes a
                # per-segment scan does not on some platform.
                if not is_within(target, root, strict=True, fs_root_ok=False):
                    raise ContainerError(
                        f"rejected .fused: entry {entry['path']!r} escapes the extract dir")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src.seek(index["_data_start"] + entry["offset"])
                remaining = entry["csize"]
                d = zlib.decompressobj()
                h = hashlib.sha256()
                got = 0
                declared = entry["size"]
                over = ContainerError(
                    f"rejected .fused: entry {entry['path']!r} inflates past its declared size")

                with open(target, "wb") as out:
                    while remaining > 0:
                        chunk = src.read(min(_COPY_CHUNK, remaining))
                        if not chunk:
                            raise ContainerError(
                                f"rejected .fused: entry {entry['path']!r} is truncated")
                        remaining -= len(chunk)
                        # Bounded inflate: a 1 MiB compressed chunk can yield
                        # ~1 GiB, so never let one call produce more than the
                        # entry has left to declare (+1 to detect overrun).
                        while chunk:
                            piece = d.decompress(chunk, max_length=min(_COPY_CHUNK, declared - got + 1))
                            got += len(piece)
                            if got > declared:
                                raise over
                            h.update(piece)
                            out.write(piece)
                            chunk = d.unconsumed_tail
                    tail = d.flush(min(_COPY_CHUNK, declared - got + 1))
                    got += len(tail)
                    if got > declared:
                        raise over
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

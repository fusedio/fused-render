"""Downloading a remote (S3) object into the local cache, so it can be viewed.

Opening an `s3://` file is "download the whole object, then render the cached
LOCAL file with the existing viewer" — no template ever sees a remote path.
This module owns that cache and the download.

**Layout.** `<cache_root>/<bucket>/<sha1(key)[:16]>/<basename>`: one directory
per key, so an object that is also a prefix (`a.zip` and `a.zip/x`), keys that
differ only by case or Unicode normalisation, and a key ending `.part` beside
its sibling never collide. The file keeps the object's basename (templates are
chosen by suffix), reduced to a conservative shell-safe name (`_safe_name`).
The in-flight download is `<hashdir>/.partial`, outside the object namespace
(a cached name never starts with ".").

**What "cached" means.** One manifest (`manifest_path()`) records, per
`bucket/key`, the `etag`, `size` and Last-Modified (`modified`) of the object
the file was downloaded from, plus the file's own identity at completion
(`mtime_ns`, `ctime_ns`, `ino`). A file is cached only when the entry is
complete and the file on disk still has that size and identity, and (for
`cached_path`) the CURRENT HEAD's size and etag. The manifest lives BESIDE the
cache root, never inside it: agent sessions are granted the cache root (some
backends' grants are writable), so nothing that decides what is trusted may
live there. A rewritten file changes its ctime (which no process can set back),
a swapped one its inode, so planted bytes never pass as the object. The same
entry carries `part_etag` and `part_seal` (the `.partial`'s identity when its
writer stopped) while a `.partial` exists, so a resume never stitches bytes of
two versions of the object together, nor bytes someone else wrote.

**Disk floor.** Nothing here may leave the cache volume with less than
`RESERVE_BYTES` free (`require_space`): a download is refused up front (and
re-checked once the size is certain) with code `no_space`. Agents start
downloads unattended, so this is the only brake.

**Download.** Streams to `.partial`, verifies, renames atomically, and leaves
the file read-only (an editable cached copy would let a save silently diverge
from the bucket) and its hash dir read-only too (a template's write action
must not drop files beside it, invisible from the s3 view). A `.partial` of the same etag is resumed with `Range` +
`If-Range`; a 200 restarts it. When the ETag is a single-part MD5 the bytes
are hashed while streaming (a resumed prefix is hashed first) and a mismatch
discards the download; a multipart ETag gets a size check only. The `.partial`
is KEPT on a cancel or a network drop (both resumable) and DELETED when the
object is gone/refused, the resume lands wrong, or the bytes do not verify.

One download per object: a second `start` joins it (same id). At most
`MAX_ACTIVE` stream at once; later ones wait their turn (`running`, `done: 0`)
and can be cancelled while waiting. Cancel comes from the view (`cancel`) or
the download manager's ✕ (read back off each `jobs.upsert` reply, the
`envinstall` mirror pattern). The jobs row (`sys:remote-fetch:<hash>`) opens
after `_ROW_AFTER_S` of streaming so a logo never flashes a row; the in-memory
registry here is what `status()` answers from.

No import of anything under `fused_render.server`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field

import httpx

from fused_render import jobs, remote_access
from fused_render._view_url_codec import canonical_fs_path
from fused_render.remote_access import (
    ObjectInfo,
    RemoteError,
    canonical_url,
    clean_etag,
    error_for_status,
    require_supported,
)

logger = logging.getLogger(__name__)

#: Uncached files above this size get a "this will be downloaded" notice first.
WARN_BYTES = 100 * 1024 * 1024

#: Most bytes one `/api/remote/read` call serves; a longer window is clamped.
RANGE_MAX_BYTES = 64 * 1024 * 1024

#: Free space a download or a range read must leave on the cache volume.
RESERVE_BYTES = 5 * 1024 * 1024 * 1024

#: Downloads streaming at once; later starts wait their turn.
MAX_ACTIVE = 3

#: Job-id prefix. `sys:` because the server runs this work (jobs.SERVER_ID_PREFIX);
#: the rest is a hash, since an S3 key holds characters a job id may not.
JOB_PREFIX = "sys:remote-fetch:"

#: The manifest, in `<home>/cache/` beside the cache root (see the docstring).
_MANIFEST = "remote-manifest.json"
#: Where it lived before: inside the cache root. Migrated once, then removed.
_OLD_MANIFEST = "manifest.json"
_PARTIAL = ".partial"

#: Open the download-manager row only once a download has streamed this long.
_ROW_AFTER_S = 1.0
#: Minimum gap between two progress ticks to the jobs registry.
_TICK_S = 1.0
#: Finished registry entries are forgotten after this long.
_FINISHED_KEEP_S = 600.0
#: Read timeout while streaming. Below jobs.STALE_AFTER_S (30 s) so a dead
#: connection ends as a resumable network error before the row reads "stalled".
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=25.0, pool=10.0)
_CHUNK = 1 << 20
#: Longest basename kept, in UTF-8 bytes (filesystems cap a name at 255).
_NAME_MAX = 200

_MD5_ETAG = re.compile(r"^[0-9a-f]{32}$")

_slots = threading.BoundedSemaphore(MAX_ACTIVE)


class _Cancelled(Exception):
    pass


# ------------------------------------------------------------------ the cache


def cache_root() -> str:
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), "cache", "remote")


def in_cache(path: str) -> bool:
    """Whether `path` lies inside the remote cache. Bucket content is
    untrusted: the file routes use this to never run it as a same-origin page.

    By identity, not by string: a case-insensitive filesystem (APFS) accepts
    `.../CACHE/Remote/...` for the same directory, and `realpath` keeps the
    spelling it was given. So each ancestor of the resolved path (starting at
    the first that exists) is compared with the cache root by `samestat`. The
    root is stat'ed per call — one syscall — so a root created later counts."""
    try:
        root_st = os.stat(cache_root())
        p = os.path.realpath(path)
    except (OSError, ValueError):
        return False  # no cache root yet: nothing can be inside it
    while True:
        try:
            if os.path.samestat(os.stat(p), root_st):
                return True
        except (OSError, ValueError):
            pass  # not there (yet): keep walking up
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


_NAME_KEEP = set("._-=+,@")


def _safe_name(key: str) -> str:
    """The key's basename as one conservative local filename. Whoever writes
    to the bucket picks the key, and agents may paste this path into a shell
    command, so only letters and digits (any script) and `._-=+,@` survive;
    every other character (whitespace, controls, quotes, `$`, backtick,
    `;&|<>(){}[]*?!\\#%~:`) becomes `_`. Never starts with `-` or `.`; at most
    `_NAME_MAX` UTF-8 bytes, keeping the suffix (templates are chosen by it).
    The hash directory keeps paths unique, so two keys sanitising to the same
    name cannot collide."""
    name = key.rsplit("/", 1)[-1]
    name = "".join(ch if (ch.isalnum() or ch in _NAME_KEEP) else "_" for ch in name)
    if sys.platform == "win32":
        name = name.rstrip(".")
    if not name:
        name = "object"
    if name[0] in "-.":
        name = "_" + name
    if len(name.encode("utf-8")) > _NAME_MAX:
        stem, ext = os.path.splitext(name)
        ext = ext if len(ext.encode("utf-8")) <= 32 else ""
        room = _NAME_MAX - len(ext.encode("utf-8"))
        name = stem.encode("utf-8")[:room].decode("utf-8", "ignore") + ext
    return name


def _hash_dir(bucket: str, key: str) -> str:
    """`<cache_root>/<bucket>/<sha1(key)[:16]>`, confined to the cache root."""
    require_supported(bucket)
    if not isinstance(key, str) or not key or key.endswith("/"):
        raise RemoteError("bad_url", "only an object (not a folder) can be downloaded")
    root = cache_root()
    d = os.path.join(root, bucket, hashlib.sha1(key.encode("utf-8")).hexdigest()[:16])
    real_root = os.path.realpath(root)
    if not os.path.realpath(d).startswith(real_root + os.sep):
        raise RemoteError("bad_url", "this object cannot be cached safely")
    return d


def cache_path(bucket: str, key: str) -> str:
    """Where `s3://bucket/key` is cached (the file need not exist)."""
    return os.path.join(_hash_dir(bucket, key), _safe_name(key))


def _entry_key(bucket: str, key: str) -> str:
    return f"{bucket}/{key}"


_manifest_lock = threading.Lock()


def manifest_path() -> str:
    """`<home>/cache/remote-manifest.json`: beside the cache root, outside
    every directory an agent session is granted."""
    return os.path.join(os.path.dirname(cache_root()), _MANIFEST)


def _load(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _migrate_locked() -> None:
    """Move a manifest from inside the cache root (where it used to live) to
    `manifest_path()`, once; caller holds `_manifest_lock`. Only complete
    entries whose file still matches them come across, stamped with the
    file's identity now; partial-download state is dropped (those restart).
    The old file is removed either way."""
    old = os.path.join(cache_root(), _OLD_MANIFEST)
    if not os.path.isfile(old):
        return
    if not os.path.exists(manifest_path()):
        kept = {}
        for k, entry in (_load(old) or {}).items():
            bucket, _, key = k.partition("/")
            if not isinstance(entry, dict):
                continue
            size, etag, mtime_ns = entry.get("size"), entry.get("etag"), entry.get("mtime_ns")
            if not (isinstance(size, int) and isinstance(etag, str) and isinstance(mtime_ns, int)):
                continue
            try:
                st = os.stat(cache_path(bucket, key))
            except (OSError, RemoteError):
                continue
            if st.st_size == size and st.st_mtime_ns == mtime_ns:
                kept[k] = {"etag": etag, "size": size, **_identity(st)}
        try:
            _write_manifest(kept)
        except OSError:
            logger.warning("could not migrate the remote cache manifest", exc_info=True)
            return
    try:
        os.remove(old)
    except OSError:
        pass


def _read_manifest() -> dict:
    _migrate_locked()
    return _load(manifest_path()) or {}


def _write_manifest(data: dict) -> None:
    path = manifest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def _update_entry(bucket: str, key: str, **fields) -> None:
    """Merge `fields` into one manifest entry; a value of None drops the field;
    an entry left empty is dropped."""
    with _manifest_lock:
        data = _read_manifest()
        k = _entry_key(bucket, key)
        entry = dict(data.get(k) or {})
        for name, value in fields.items():
            if value is None:
                entry.pop(name, None)
            else:
                entry[name] = value
        if entry:
            data[k] = entry
        else:
            data.pop(k, None)
        _write_manifest(data)


def _entry(bucket: str, key: str) -> dict:
    with _manifest_lock:
        entry = _read_manifest().get(_entry_key(bucket, key))
    return entry if isinstance(entry, dict) else {}


def _identity(st: os.stat_result) -> dict:
    """What a manifest entry pins a file on disk to. `ctime_ns` moves on any
    write, rename, chmod or utime and cannot be set back, so a file rewritten
    in place (even with its mtime restored) no longer matches."""
    return {"mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns, "ino": st.st_ino}


def _matches(entry: dict, st: os.stat_result) -> bool:
    return (st.st_size == entry.get("size")
            and all(entry.get(k) == v for k, v in _identity(st).items()))


def _valid_entry(bucket: str, key: str) -> tuple[str, dict] | None:
    """`(path, entry)` when the manifest entry is complete (etag, size and
    the file identity all present) and the file on disk still matches it."""
    try:
        path = cache_path(bucket, key)
    except RemoteError:
        return None
    entry = _entry(bucket, key)
    if not (isinstance(entry.get("etag"), str) and isinstance(entry.get("size"), int)
            and all(isinstance(entry.get(k), int) for k in ("mtime_ns", "ctime_ns", "ino"))):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (path, entry) if _matches(entry, st) else None


def cached_path(bucket: str, key: str, info: ObjectInfo) -> str | None:
    """The cached file for this object, if it holds exactly the object `info`
    describes; else None. Never raises for an uncachable key (answers None)."""
    hit = _valid_entry(bucket, key)
    if hit is None:
        return None
    path, entry = hit
    if info.size is not None and entry["size"] != info.size:
        return None
    if entry["etag"] != info.etag:
        return None
    return canonical_fs_path(os.path.abspath(path))


def cached_copy(bucket: str, key: str) -> tuple[str, ObjectInfo] | None:
    """`(path, info)` of the cached copy of `s3://bucket/key` judged from the
    manifest alone — no network: the entry is complete and the file is still
    the one it recorded. `info` is the object as downloaded (size, etag,
    Last-Modified when recorded). An object changed in the bucket since the
    download is still answered from the old copy."""
    hit = _valid_entry(bucket, key)
    if hit is None:
        return None
    path, entry = hit
    modified = entry.get("modified")
    info = ObjectInfo(size=entry["size"],
                      mtime=float(modified) if isinstance(modified, (int, float)) else None,
                      etag=entry["etag"])
    return canonical_fs_path(os.path.abspath(path)), info


def _fmt_bytes(n: float) -> str:
    """`5.0 GB`, `812.3 MB`, `999 bytes`."""
    if n < 1024:
        return f"{int(n)} bytes"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def free_bytes() -> int:
    """Free bytes on the cache volume (the nearest existing ancestor of the
    cache root, so it answers before the first download)."""
    p = cache_root()
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return shutil.disk_usage(p).free


def require_space(needed: int) -> None:
    """`RemoteError("no_space")` unless writing `needed` more bytes to the
    cache volume leaves at least `RESERVE_BYTES` free. The one disk-floor
    rule: whole downloads (`start`, `_download`) and range reads (the route,
    since the agent's tool server saves ranges to this volume) all use it."""
    try:
        free = free_bytes()
    except OSError:
        return  # cannot tell: do not block on a failed statvfs
    if free - max(0, needed) < RESERVE_BYTES:
        raise RemoteError(
            "no_space",
            f"Not enough free disk space: {_fmt_bytes(free)} free, and fused-render "
            f"keeps {_fmt_bytes(RESERVE_BYTES)} free"
            + (f" after the {_fmt_bytes(needed)} this needs" if needed > 0 else "")
            + ". Free up space and try again.")


# ---------------------------------------------------------------- the registry


@dataclass
class _Fetch:
    id: str
    bucket: str
    key: str
    path: str
    state: str = "running"
    done: int = 0
    total: int | None = None
    error: str | None = None
    code: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    row_open: bool = False
    exited: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {"state": self.state, "done": self.done, "total": self.total,
                "path": (canonical_fs_path(os.path.abspath(self.path))
                         if self.state == "done" else None),
                "error": self.error, "code": self.code}


_lock = threading.Lock()
_fetches: dict[str, _Fetch] = {}


def job_id(bucket: str, key: str) -> str:
    digest = hashlib.sha1(_entry_key(bucket, key).encode("utf-8")).hexdigest()[:24]
    return JOB_PREFIX + digest


def _live(rec: _Fetch | None) -> bool:
    """Its thread is still alive and it was not asked to stop: a start joins it."""
    return rec is not None and not rec.exited.is_set() and not rec.cancel.is_set()


def _prune(now: float) -> None:
    """Caller holds `_lock`."""
    for fid in [f for f, rec in _fetches.items()
                if rec.finished_at is not None and now - rec.finished_at > _FINISHED_KEEP_S]:
        del _fetches[fid]


def start(bucket: str, key: str) -> dict:
    """Begin (or join) the download of `s3://bucket/key`.

    `{"id", "state": "running"|"done", "path"}`. Raises RemoteError: `bad_url`
    for a key that cannot be cached, and whatever the HEAD maps to."""
    path = cache_path(bucket, key)
    fid = job_id(bucket, key)
    with _lock:
        cur = _fetches.get(fid)
        if _live(cur):
            return {"id": fid, "state": "running", "path": None}
    info = remote_access.head(bucket, key)
    now = time.time()
    with _lock:
        _prune(now)
        cur = _fetches.get(fid)
        if _live(cur):
            return {"id": fid, "state": "running", "path": None}
        hit = cached_path(bucket, key, info)
        if hit:
            done = _Fetch(id=fid, bucket=bucket, key=key, path=path, state="done",
                          done=info.size or 0, total=info.size, finished_at=now)
            done.exited.set()  # no thread ever ran for a cache hit
            _fetches[fid] = done
            return {"id": fid, "state": "done", "path": hit}
        require_space(info.size or 0)
        # A cancelled attempt still winding down (cancel is noticed per chunk,
        # not while connecting) is replaced, not joined; the new attempt waits
        # for it to exit so there is never a second writer on the partial.
        prev = cur if cur is not None and not cur.exited.is_set() else None
        rec = _Fetch(id=fid, bucket=bucket, key=key, path=path, total=info.size)
        _fetches[fid] = rec

    def _guarded() -> None:
        try:
            # Wait for the previous attempt, then for a slot; cancellable throughout.
            while ((prev is not None and not prev.exited.wait(0.25))
                   or not _slots.acquire(timeout=0.25)):
                if rec.cancel.is_set():
                    _finish(rec, "cancelled", error="the download was cancelled")
                    return
            try:
                _run(rec, info)
            except BaseException:  # noqa: BLE001 - a stuck "running" is worse
                logger.exception("remote fetch of %s died", canonical_url(bucket, key))
                _finish(rec, "error", error="the download stopped unexpectedly", code="internal")
            finally:
                _slots.release()
        finally:
            rec.exited.set()

    try:
        threading.Thread(target=_guarded, daemon=True, name=f"remote-fetch-{fid[-8:]}").start()
    except Exception:  # noqa: BLE001 - e.g. "can't start new thread"
        logger.exception("could not start the download of %s", canonical_url(bucket, key))
        _finish(rec, "error", error="the download could not be started", code="internal")
        rec.exited.set()
        return {"id": fid, "state": "running", "path": None}
    return {"id": fid, "state": "running", "path": None}


def status(fid: str) -> dict | None:
    """The fetch record for `fid`, or None when there is none (never started,
    or finished long enough ago to be forgotten)."""
    with _lock:
        _prune(time.time())
        rec = _fetches.get(fid)
        return rec.public() if rec is not None else None


def cancel(fid: str) -> bool:
    """Ask a running (or queued) download to stop. False when there is none."""
    with _lock:
        rec = _fetches.get(fid)
    if rec is None:
        return False
    rec.cancel.set()
    return True


# ---------------------------------------------------------------- the download


def _report(rec: _Fetch, **fields) -> None:
    """Mirror progress into the jobs registry; read the ✕ back. Never raises."""
    try:
        if not rec.row_open:
            jobs.clear_cancel_requested(rec.id)
            name = rec.key.rsplit("/", 1)[-1]
            body = {"id": rec.id, "title": f"Downloading {name}", "kind": "download",
                    "state": jobs.RUNNING, "unit": "bytes", "cancellable": True,
                    "total_scope": "download", "message": "",
                    "detail": canonical_url(rec.bucket, rec.key), "origin": "Explorer"}
            body.update(fields)
            record = jobs.upsert(body, server=True)
            rec.row_open = True
        else:
            record = jobs.upsert({"id": rec.id, **fields}, server=True)
        if record.get("cancel_requested"):
            rec.cancel.set()
    except Exception:  # noqa: BLE001 - reporting must never break the work
        logger.debug("could not report remote fetch %s", rec.id, exc_info=True)


def _finish(rec: _Fetch, state: str, *, error: str | None = None,
            code: str | None = None) -> None:
    with _lock:
        if rec.finished_at is not None:
            return
        rec.state = state
        rec.error = error
        rec.code = code
        rec.finished_at = time.time()
    if rec.row_open:
        fields: dict = {"state": state, "done": rec.done}
        if rec.total is not None:
            fields["total"] = rec.total
        if error:
            fields["message"] = error
        _report(rec, **fields)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _content_range(value: str | None) -> tuple[int | None, int | None]:
    """(start, total) of a `bytes a-b/N` header; Nones when unreadable."""
    if not value or not value.startswith("bytes "):
        return None, None
    rng, _, total = value[6:].partition("/")
    start = rng.split("-", 1)[0]
    return (int(start) if start.isdigit() else None,
            int(total) if total.isdigit() else None)


def _hash_prefix(path: str) -> "hashlib._Hash":
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h


def _discard(rec: _Fetch, partial: str) -> None:
    _remove(partial)
    _update_entry(rec.bucket, rec.key, part_etag=None, part_seal=None)


def _seal_of(st: os.stat_result) -> list:
    return [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _seal(rec: _Fetch, partial: str) -> None:
    """Record the kept `.partial`'s identity as its writer leaves it, so a
    resume appends only to bytes this module wrote (see `_download`)."""
    try:
        st = os.stat(partial)
    except OSError:
        return
    _update_entry(rec.bucket, rec.key, part_seal=_seal_of(st))


def _run(rec: _Fetch, info: ObjectInfo) -> None:
    """The hash dir is read-only (0o555) except while this runs: a template's
    write action ("Extract", "Convert to …") must not drop files beside a
    cached object, where the s3 view would never show them. Every write into
    the dir (partial, rename, discard) happens inside `_download`."""
    d = os.path.dirname(rec.path)
    try:
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o755)
    except OSError:
        logger.warning("could not create the cache folder for %s",
                       canonical_url(rec.bucket, rec.key), exc_info=True)
        _finish(rec, "error", error="could not create the local cache folder", code="internal")
        return
    try:
        _download(rec, info)
    finally:
        try:
            os.chmod(d, 0o555)
        except OSError:
            pass


def _download(rec: _Fetch, info: ObjectInfo) -> None:
    bucket, key, path = rec.bucket, rec.key, rec.path
    partial = os.path.join(os.path.dirname(path), _PARTIAL)
    what = canonical_url(bucket, key)
    url = remote_access.read_url(bucket, key)

    # Resume only a .partial of the same object version, and only the very
    # file this module sealed when it stopped writing it: the hash dir is in
    # a tree agents can write, and a multipart ETag gets no content check.
    offset = 0
    if os.path.exists(partial):
        entry = _entry(bucket, key)
        try:
            st = os.stat(partial)
        except OSError:
            st = None
        if (st is not None and info.etag and entry.get("part_etag") == info.etag
                and entry.get("part_seal") == _seal_of(st) and st.st_size > 0
                and (info.size is None or st.st_size < info.size)):
            offset = st.st_size
        else:
            _remove(partial)
    _update_entry(bucket, key, part_etag=info.etag, part_seal=None)

    started = time.monotonic()
    last_tick = 0.0
    try:
        with httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=False,
                          headers={"User-Agent": "fused-render"}) as client:
            for attempt in (0, 1):
                # Raw bytes, never a decoded body: the file must be the object.
                headers = {"Accept-Encoding": "identity"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                    if info.etag:
                        headers["If-Range"] = f'"{info.etag}"'
                with client.stream("GET", url, headers=headers) as r:
                    if r.status_code == 416 and offset and attempt == 0:
                        _remove(partial)
                        offset = 0
                        continue
                    if r.status_code == 206:
                        start_at, total = _content_range(r.headers.get("content-range"))
                        if start_at != offset:
                            _discard(rec, partial)
                            raise RemoteError("network", f"S3 resumed {what} at the wrong offset; try again")
                        mode = "ab"
                    elif r.status_code == 200:
                        offset = 0
                        length = r.headers.get("content-length")
                        total = int(length) if length and length.isdigit() else info.size
                        mode = "wb"
                    else:
                        if r.status_code in (403, 404):
                            _discard(rec, partial)
                        raise error_for_status(r.status_code, what)
                    etag = clean_etag(r.headers.get("etag")) or info.etag
                    if etag != info.etag:
                        _update_entry(bucket, key, part_etag=etag)
                    # A single-part ETag is the object's MD5: verify it.
                    hasher = None
                    if etag and _MD5_ETAG.match(etag):
                        hasher = _hash_prefix(partial) if mode == "ab" else hashlib.md5()
                    with _lock:
                        rec.total = total
                        rec.done = offset
                    if total is not None:
                        # Raised before a byte is written: a partial is kept.
                        require_space(total - offset)
                    with open(partial, mode) as f:
                        for chunk in r.iter_raw(_CHUNK):
                            if rec.cancel.is_set():
                                raise _Cancelled()
                            f.write(chunk)
                            if hasher is not None:
                                hasher.update(chunk)
                            with _lock:
                                rec.done += len(chunk)
                            now = time.monotonic()
                            if now - started >= _ROW_AFTER_S and now - last_tick >= _TICK_S:
                                last_tick = now
                                tick = {"done": rec.done}
                                if rec.total is not None:
                                    tick["total"] = rec.total
                                _report(rec, **tick)
                        if rec.cancel.is_set():
                            raise _Cancelled()
                    if ((total is not None and rec.done != total)
                            or (hasher is not None and hasher.hexdigest() != etag)):
                        _discard(rec, partial)
                        raise RemoteError("network", f"the download of {what} did not match; try again")
                    if os.path.exists(path):
                        os.chmod(path, 0o644)  # replacing a read-only copy (Windows)
                    os.replace(partial, path)
                    try:
                        os.chmod(path, 0o444)
                    except OSError:
                        pass
                    st = os.stat(path)
                    modified = remote_access.http_date(r.headers.get("last-modified"))
                    _update_entry(bucket, key, etag=etag, size=st.st_size,
                                  modified=modified if modified is not None else info.mtime,
                                  part_etag=None, part_seal=None, **_identity(st))
                    _finish(rec, "done")
                    return
    except _Cancelled:
        _seal(rec, partial)
        _finish(rec, "cancelled", error="the download was cancelled")
    except RemoteError as e:
        _seal(rec, partial)  # when one is kept (e.g. no_space before writing)
        _finish(rec, "error", error=e.message, code=e.code)
    except httpx.TransportError:
        # A network drop: the .partial is kept, so the next start resumes.
        _seal(rec, partial)
        logger.info("remote fetch of %s interrupted", what, exc_info=True)
        _finish(rec, "error", error=f"the download of {what} was interrupted; try again",
                code="network")
    except OSError:
        logger.warning("remote fetch of %s could not be saved", what, exc_info=True)
        _discard(rec, partial)
        _finish(rec, "error", error=f"could not save {what} to the local cache", code="internal")

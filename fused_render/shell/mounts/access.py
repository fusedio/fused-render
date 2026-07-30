"""Mount-backed path access mediated by rcd's rc API: is_mount_backed /
is_mount_root, the rc_list_dir / rc_stat_* / rc_kind_for family, the HTTP
serve registry, and the read-only-mountpoints cache."""

from datetime import datetime
import json
import logging
import os
import re
import socket
import stat as stat_mod
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fused_render.shell import storage

from .rcd import _rc_cancellable
from .store import _update_mount, mountpoint, mounts_dir

logger = logging.getLogger(__name__)


def serves_path() -> str:
    return os.path.join(storage.home_dir(), "serves.json")


def _http_serves(port: int) -> dict:
    """{fs: {"addr", "id", "vfs"}} for every live rc HTTP serve. "vfs" is the
    vfs option params the serve was started with (every param except the
    type/fs/addr infra keys — i.e. the vfs_* flags AND dir_cache_time, which
    has no vfs_ prefix but is part of the shared option set). This is the
    drift-check input, compared against SERVE_VFS_OPT; capturing only vfs_*
    keys would drop dir_cache_time and make the check always report drift."""
    from fused_render.shell.mounts import _rc
    try:
        listed = _rc(port, "serve/list").get("list", [])
    except RuntimeError:
        return {}
    return {
        s["params"]["fs"]: {"addr": s.get("addr", ""), "id": s.get("id", ""),
                            "vfs": {k: v for k, v in s["params"].items()
                                    if k not in ("type", "fs", "addr")}}
        for s in listed
        if isinstance(s, dict) and s.get("params", {}).get("type") == "http"
        and s.get("params", {}).get("fs")
    }


_serves_lock = threading.Lock()


_ro_cache_lock = threading.Lock()


_ro_cache: tuple | None = None  # (_mounts_generation, [abs read-only mountpoints])


def _read_only_mountpoints() -> list:
    from fused_render.shell import mounts as _mounts_pkg
    from fused_render.shell.mounts import list_mounts
    gen = _mounts_pkg._mounts_generation
    with _ro_cache_lock:
        if _mounts_pkg._ro_cache is None or _mounts_pkg._ro_cache[0] != gen:
            _mounts_pkg._ro_cache = (gen, [os.path.abspath(mountpoint(c))
                               for c in list_mounts() if c.get("read_only")])
        return _mounts_pkg._ro_cache[1]


def export_ro_mounts_env() -> None:
    """Publish the read-only mountpoints to the environment for template children
    (SPEC PY-15 / D166).

    Templates can't import this module (the fused local execution backend strips
    PYTHONPATH from child processes), so they answer "is this path read-only?"
    from `FUSED_RENDER_RO_MOUNTS` via `templates/shared/appenv.py`. We export the
    derived MOUNTPOINT LIST, not the store: mounts.json's schema stays private to
    this module, so `read_only_user`, legacy records and future flags can change
    without touching a single template.

    `os.pathsep`-joined because that is the platform's own list-in-one-var
    convention (`:` posix, `;` win32) and a mountpoint can contain the other
    separator. Called from `_write` on every store mutation and once at startup
    (`server.export_app_env`) so a server that never writes a mount still leaves a
    correct — possibly empty — value behind rather than an unset var that a child
    couldn't distinguish from "no mounts".

    Reuses `_read_only_mountpoints()` so there is exactly one computation of the
    list, cached on `_mounts_generation`; recomputing here would be a second copy
    of the rule, free to drift.
    """
    os.environ["FUSED_RENDER_RO_MOUNTS"] = os.pathsep.join(_read_only_mountpoints())


def mount_read_only(path: str) -> bool:
    """True when `path` sits under a mount whose remote rejects writes (the
    persisted `read_only` flag: detected at attach, or set at create). The
    kernel mount can't answer this itself: with CacheMode=full a write
    "succeeds" into the local VFS cache and only fails at the async upload,
    so os.access(W_OK) reports writable on a remote that will never take the
    bytes. server._writable folds this in, which flips stat.writable and the
    /api/fs/write guard together (SPEC RO-1). A record without the flag
    (legacy, or detection still inconclusive) stays rw — the pre-flag
    behavior. Deliberately ignores whether the mount is currently attached:
    a file written into a detached read-only mountpoint would be shadowed by
    the next attach, so refusing the write is right either way."""
    from fused_render.shell.mounts import is_mount_backed
    if not is_mount_backed(path):
        return False
    p = os.path.abspath(path)
    return any(p == mp or p.startswith(mp + os.sep)
               for mp in _read_only_mountpoints())


def _s3_without_credentials(cfg: dict) -> bool:
    """An S3 remote config carrying no way to sign requests — rclone sends
    them unsigned, which S3 accepts for public-bucket reads only (the
    built-in aws-open suggestion is exactly this shape). Shared predicate:
    _public_object_url decides "public URL is reachable unsigned" with it
    and _detect_read_only decides "writes can never be accepted"; keep the
    definition single so the two can't drift."""
    return (cfg.get("type") == "s3"
            and str(cfg.get("env_auth", "")).lower() != "true"
            and not (cfg.get("access_key_id") or cfg.get("profile")
                     or cfg.get("shared_credentials_file")
                     or cfg.get("session_token")))


def _gcs_anonymous(cfg: dict) -> bool:
    """A GCS remote configured anonymous=true (the built-in gcs-open
    suggestion) — rclone sends unauthenticated requests, which GCS accepts
    for public-bucket reads only, so writes can never be accepted."""
    return (cfg.get("type") == "google cloud storage"
            and str(cfg.get("anonymous", "")).lower() == "true")


def _detect_read_only(port: int, fs: str) -> bool | None:
    """Best-effort, NON-MUTATING read-onlyness probe for a remote. Never
    writes a probe object into the user's store; instead:
      - operations/fsinfo: a backend advertising no write feature at all
        (Put/PutStream/Copy — e.g. http) can never take a write.
      - config/get: an anonymous S3 or GCS remote (see
        _s3_without_credentials / _gcs_anonymous).
    Returns None when the probe is INCONCLUSIVE — an rc call failed, or the
    reply didn't carry the expected shape (absence of a Features map is
    version skew, not evidence of read-onlyness) — so the caller persists
    nothing and the next attach tries again. Credentials an IAM policy
    limits to read still report writable: only a real write could tell, and
    probing with one would drop junk objects into user buckets."""
    from fused_render.shell.mounts import _rc
    try:
        feats = (_rc(port, "operations/fsinfo", {"fs": fs}, timeout=10)
                 or {}).get("Features")
    except RuntimeError:
        return None
    if not isinstance(feats, dict) or not feats:
        return None
    if not any(feats.get(k) for k in ("Put", "PutStream", "Copy")):
        return True
    try:
        cfg = _rc(port, "config/get", {"name": fs.partition(":")[0]}, timeout=10)
    except RuntimeError:
        return None
    if not isinstance(cfg, dict):
        return None
    return _s3_without_credentials(cfg) or _gcs_anonymous(cfg)


def _refresh_read_only_flag(m: dict, port: int | None = None) -> None:
    """(Re-)detect and persist `read_only` on every attach, so a remote whose
    credentials changed since the last detection (keys added to an
    anonymous remote, or removed) converges without deleting the mount. A
    user-set flag (read_only_user, add_mount) is never overwritten, and an
    inconclusive probe (None) keeps whatever is recorded. Never raises —
    attach_mount's "error string or None" contract must hold even when
    persisting the flag fails."""
    from fused_render.shell.mounts import _live_rcd_port
    try:
        if m.get("read_only_user"):
            return
        if port is None:
            # Resolved only past the user-flag check: this is an rc probe
            # with a timeout, not worth paying when the answer is fixed.
            port = _live_rcd_port()
        if port is None:
            return
        ro = _detect_read_only(port, m["remote"])
        if ro is None or ro == m.get("read_only"):
            return
        m["read_only"] = ro
        _update_mount(m)
    except Exception:
        logger.warning("read-only detection for %r failed", m.get("name"),
                       exc_info=True)


def is_mount_backed(path: str) -> bool:
    """True when `path` sits under the mounts dir — i.e. its bytes come from a
    remote. Cheap enough for every stat: the fast abspath prefix check settles
    the common case with no I/O.

    A symlink whose TARGET is inside the mounts dir would slip past a pure string
    check and be classified LOCAL — landing on the 200ms kernel os.stat ticker,
    the exact GETATTR storm the mount routing avoids. So a path that does NOT
    look mount-backed by string is re-checked through os.path.realpath (which
    resolves the symlink). A genuine mount path already matches on abspath and
    never reaches realpath, so no kernel I/O / mount traversal is added to the
    hot path; only local-looking paths pay one realpath."""
    root = os.path.abspath(mounts_dir())
    ap = os.path.abspath(path)
    if ap == root or ap.startswith(root + os.sep):
        return True
    real_root = os.path.realpath(mounts_dir())
    rp = os.path.realpath(path)
    return rp == real_root or rp.startswith(real_root + os.sep)


def is_mounts_root(path: str) -> bool:
    """True when `path` IS the mounts container itself — the local parent that
    holds each mountpoint as a subdir — as opposed to a path under an individual
    mount. is_mount_backed is true for the root too (its `ap == root` clause), so
    the root is kept off the kernel like any remote path; but the root is under
    no single mount record, so the rc/S3 listing routes have nothing to list.
    Callers list the root by enumerating mount records instead (no kernel or
    remote I/O).

    Mirrors is_mount_backed's symlink handling: a symlink whose TARGET is the
    mounts root looks mount-backed (via that function's realpath branch) yet
    would fail a pure abspath match here, so the guard that keeps the root off
    the rc/S3 routes would miss it. So a path that does NOT resolve within the
    container by abspath is re-checked through os.path.realpath, which follows
    the symlink to the root. The real root matches on abspath and never reaches
    realpath; a path already UNDER the container is a mountpoint (or deeper),
    never the root itself, and is settled by string so realpath never gets to
    kernel-stat a live mount. Only an outside-looking symlink pays one realpath
    (a local resolve, off any mount)."""
    root = os.path.abspath(mounts_dir())
    ap = os.path.abspath(path)
    if ap == root:
        return True
    if ap.startswith(root + os.sep):
        return False
    return os.path.realpath(path) == os.path.realpath(mounts_dir())


def is_mount_root(path: str) -> bool:
    """True when `path` is the ROOT of an individual mount (its mountpoint), as
    opposed to a subpath inside it. A single-level listing of a mount root is a
    listing of the remote's top prefix — or the whole bucket for a bucket-root
    mount — which on a world-scale remote is enormous. Callers use this to avoid
    a standing periodic enumeration of such a prefix (fs/events P1 #4). The
    mounts container itself counts as a root too. Pure string/abspath — no I/O."""
    from .signing import _mount_for
    if is_mounts_root(path):
        return True
    m, rel = _mount_for(path)
    return m is not None and rel == "."


def rc_mtime_for(path: str) -> str | None:
    """ModTime of a mount-backed file, answered by the rclone rcd rc API
    (operations/stat) instead of the kernel NFS mount.

    Background — the fs/events stat storm incident: a read-only S3-backed
    rclone NFS mount died with the macOS "Server connections interrupted"
    dialog. The /api/fs/events poller was calling os.stat() on every watched
    path every 200ms, and each of those is a kernel NFS GETATTR. When the
    attribute cache expires, that GETATTR forces rclone to re-list the
    directory on S3; for a world-scale .zarr on a slow bucket the re-list
    exceeds the macOS NFS client's timeo*retrans ceiling (~2min) and the
    kernel declares the mount dead. Several open preview panes plus the
    Listing view held ~5 such stat loops at once.

    Asking the rcd directly over its loopback rc port removes the kernel from
    the loop entirely: a slow answer here is just a slow HTTP response, never
    a wedged mount. The remote (`fs`) and remote-relative path come from the
    same _mount_for() translation the raw-proxy hot path uses.

    Returns the RFC3339 ModTime string, or None when it cannot be determined
    (path not under a mount, rcd unreachable, rc error/timeout, or missing
    item). Callers MUST treat None as "unchanged" and MUST NOT fall back to
    os.stat — that fallback is the exact GETATTR that killed the mount."""
    item = _rc_stat_item(path)
    # Both "missing" (None) and "indeterminate" (the sentinel) collapse to None
    # here — this preserves rc_mtime_for's documented contract. A caller that
    # must distinguish a confirmed deletion from a transient failure uses
    # rc_stat_for instead.
    if not isinstance(item, dict):
        return None
    return item.get("ModTime") or None


_STAT_INDETERMINATE = object()


RC_STAT_TIMEOUT_S = 10.0


_DIRECT_PROBE_MIN_S = 0.5


def _rc_stat_item(path: str, *, timeout: float = RC_STAT_TIMEOUT_S):
    """The raw operations/stat `item` for a mount-backed path, off the kernel:
      - a dict          -> the item exists (its ModTime may or may not be set);
      - None            -> a healthy rcd answered {"item": null}: the file is
                           GONE (a trustworthy negative);
      - _STAT_INDETERMINATE -> the stat could not be taken (path under no mount,
                           no live rcd port, rc RuntimeError/timeout, or a
                           malformed response). Callers MUST fail open on this
                           and MUST NOT fall back to os.stat — that GETATTR is
                           the exact call that wedged the mount.
    Shared by rc_mtime_for and rc_stat_for so both speak to the rcd once and
    agree on what each outcome means."""
    from fused_render.shell.mounts import _live_rcd_port
    from .signing import _mount_for
    m, rel = _mount_for(path)
    if m is None:
        return _STAT_INDETERMINATE
    port = _live_rcd_port()
    if port is None:
        return _STAT_INDETERMINATE
    # _mount_for returns "." for the mountpoint itself; operations/stat wants ""
    # for the fs root (remote "." returns {"item": null}, so the mount-ROOT
    # watch would never prime — same quirk operations/list has, normalized in
    # rc_list_dir).
    remote = "" if rel == "." else rel
    try:
        # Cancellable: operations/stat runs an UNBOUNDED ListObjectsV2 on a
        # negative/dir probe, so a timed-out sync call would orphan that walk.
        resp = _rc_cancellable(port, "operations/stat",
                               {"fs": m["remote"], "remote": remote},
                               timeout=timeout)
    except RuntimeError:
        return _STAT_INDETERMINATE
    if not isinstance(resp, dict) or "item" not in resp:
        return _STAT_INDETERMINATE  # malformed answer -> fail open
    item = resp["item"]
    if item is None:
        return None  # healthy rcd: file confirmed gone
    if not isinstance(item, dict):
        return _STAT_INDETERMINATE
    return item


def _stat_item(path: str, *, timeout: float = RC_STAT_TIMEOUT_S):
    """Normalized stat outcome for a mount-backed path, DIRECT-PROBE-FIRST:
      - a dict {"IsDir", "Size", "MtimeEpoch"} -> the path exists;
      - None                                   -> confirmed missing;
      - _STAT_INDETERMINATE                    -> could not be determined.

    operations/stat has no S3 point lookup: a negative file probe or a directory
    probe makes rclone run an UNBOUNDED ListObjectsV2 of the whole parent prefix,
    so on a flat world-scale prefix every probe burns the full rc timeout. But
    S3/GCS expose true point lookups — HeadObject answers exists/size/mtime in
    one round trip and a max-keys=1 list answers dir-ness in another — so for the
    anonymous backends we already list unsigned (direct_list_capable) we probe
    the store DIRECTLY and never touch operations/stat. Any direct failure
    (403/301/network — DirectProbeError) falls back to the rc path so a
    misconfigured remote still degrades to the slow-but-correct route.

    Shared by rc_stat_for / rc_kind_for / rc_stat_result so all three speak the
    same direct-first path and agree on what each outcome means. rc_mtime_for
    stays on _rc_stat_item directly (its raw-ModTime-string contract predates
    this and no world-scale caller relies on it)."""
    from fused_render.shell.mounts import direct_list_capable
    from .probe import DirectProbeError, _direct_stat_item
    deadline = time.monotonic() + timeout
    if direct_list_capable(path):
        try:
            return _direct_stat_item(path, deadline=deadline)
        except DirectProbeError:
            pass  # fall through to the slow rc route, on the SAME deadline
    # The rc fallback shares the direct probes' deadline so an indeterminate
    # direct outcome can't add a fresh full timeout on top; below the floor
    # there is no plausible round trip left, so fail open to indeterminate
    # rather than overrun the caller's timeout (the floor is a bail-out
    # threshold, never a grant).
    remaining = deadline - time.monotonic()
    if remaining < _DIRECT_PROBE_MIN_S:
        return _STAT_INDETERMINATE
    item = _rc_stat_item(path, timeout=remaining)
    if not isinstance(item, dict):
        return item  # None (missing) or _STAT_INDETERMINATE pass straight through
    return {"IsDir": bool(item.get("IsDir")), "Size": item.get("Size"),
            "MtimeEpoch": rc_modtime_epoch(item.get("ModTime"))}


def rc_stat_for(path: str, *, timeout: float = RC_STAT_TIMEOUT_S) -> str:
    """Tri-state existence of a mount-backed path, never the kernel: "exists",
    "missing", or "indeterminate".

    Splits apart what rc_mtime_for collapses into None, so a caller can filter a
    genuinely-deleted mount file (a healthy rcd's {"item": null}) while still
    failing open on any transient failure. "missing" is the ONLY outcome that
    proves absence; treat "indeterminate" as "keep / unchanged". Answered by a
    direct point probe where the backend supports it, else operations/stat."""
    item = _stat_item(path, timeout=timeout)
    if item is _STAT_INDETERMINATE:
        return "indeterminate"
    if item is None:
        return "missing"
    return "exists"


def rc_kind_for(path: str, *, timeout: float = RC_STAT_TIMEOUT_S) -> str:
    """Four-state kind of a mount-backed path, never the kernel: "dir", "file",
    "missing", or "indeterminate".

    Extends rc_stat_for's present/absent with the IsDir bit, so a caller can tell
    os.path.isfile from os.path.isdir without a kernel LOOKUP. That LOOKUP is the
    whole point: a cold NEGATIVE os.path.isfile over an rclone-NFS mount forces
    rclone to LIST the entire parent S3 prefix to resolve the miss (~18-24s on a
    world-scale store), which trips the macOS NFS deadman and the mount is
    declared dead. Same "kernel NFS is the enemy, route via a bounded probe"
    hardening rc_list_dir / api_fs_list got.

    "file"/"dir" prove presence; "missing" is the ONLY outcome that proves
    absence; "indeterminate" (backend unreachable / no mount / malformed answer)
    must be treated as "don't know" and MUST NOT fall back to the kernel."""
    item = _stat_item(path, timeout=timeout)
    if item is _STAT_INDETERMINATE:
        return "indeterminate"
    if item is None:
        return "missing"
    return "dir" if item["IsDir"] else "file"


def rc_stat_result(path: str, *, timeout: float = RC_STAT_TIMEOUT_S) -> os.stat_result:
    """A synthesized os.stat_result for a mount-backed path, off the kernel
    GETATTR (the stat-storm/deadman class — see rc_mtime_for).

    Only the fields callers actually read are meaningful — st_mode's dir/file
    bit, st_size, and st_mtime; the rest are zero-filled. Raises
    FileNotFoundError when the backend confirms the item is gone and OSError when
    the stat is indeterminate, so a mount stat fails EXACTLY like the kernel
    os.stat it replaces and callers' existing OSError->404 handling holds — and
    it NEVER falls back to that kernel GETATTR, which is the call that wedged the
    mount."""
    item = _stat_item(path, timeout=timeout)
    if item is _STAT_INDETERMINATE:
        raise OSError(f"rc stat unavailable for {path}")
    if item is None:
        raise FileNotFoundError(path)
    size = item["Size"]
    # rclone reports -1 for a directory / unknown size; clamp to 0.
    size = int(size) if isinstance(size, (int, float)) and size >= 0 else 0
    mtime = item["MtimeEpoch"] or 0.0
    mode = (stat_mod.S_IFDIR | 0o755) if item["IsDir"] else (stat_mod.S_IFREG | 0o644)
    # (mode, ino, dev, nlink, uid, gid, size, atime, mtime, ctime)
    return os.stat_result((mode, 0, 0, 1, 0, 0, size, mtime, mtime, mtime))


_GATE_READ_CAP = 1 << 20


def rc_read_bounded(path: str, cap: int = _GATE_READ_CAP, timeout: float = 10) -> bytes:
    """Up to `cap` bytes of a mount-backed file, fetched over the mount's
    localhost HTTP serve (serve_url_for) instead of a kernel open()/read.

    The condition-gate shim uses this for the one bounded zarr.json read: a
    kernel open of a mount file is the same GETATTR/READ class that wedges the
    mount, while a ranged GET over the serve is at worst slow, never fatal.
    Raises OSError on no live serve / transport error / timeout so the gate fails
    closed (urllib.error.URLError and socket timeouts are already OSError)."""
    from fused_render.shell.mounts import serve_url_for
    url = serve_url_for(path)
    if url is None:
        raise OSError(f"no HTTP serve for {path}")
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{cap - 1}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(cap)
    except OSError as e:  # URLError/HTTPError/socket timeout are all OSError
        raise OSError(f"serve read failed for {path}: {e}") from e


RC_LIST_TIMEOUT_S = 20.0


class RcListError(Exception):
    """The rcd answered but rejected an operations/list — the remote path is
    not a listable directory (a file, or missing). The caller maps this to the
    400 "not a directory" response, the mount-safe equivalent of the
    os.path.isdir guard a local listing runs before scandir."""


class RcListUnavailable(RcListError):
    """The rcd itself is unreachable (not running, or the path resolves to no
    known mount record) — indistinguishable here from a broken mount, so the
    caller consults broken_mount_error and returns 503."""


class RcListTimeout(RcListError):
    """operations/list did not finish within the hard timeout — a directory too
    large to enumerate. The caller surfaces a 503 "too many entries" rather
    than letting a kernel readdir wedge the mount."""


def _rc_timed_out(e: BaseException) -> bool:
    """Whether an _rc RuntimeError was caused by the request timing out. _rc
    wraps every transport failure (OSError, including the socket read timeout)
    into a RuntimeError, so the original timeout survives only on the
    exception's __cause__ chain."""
    cause = e.__cause__
    if isinstance(cause, TimeoutError):  # socket.timeout is an alias since 3.10
        return True
    return (isinstance(cause, urllib.error.URLError)
            and isinstance(getattr(cause, "reason", None), TimeoutError))


def rc_list_dir(path: str, timeout: float | None = None) -> list:
    """Directory listing of a mount-backed path, answered by the rclone rcd rc
    API (operations/list) instead of a kernel os.scandir.

    Background — the mur-sst listing incident: a kernel READDIR on an rclone
    NFS mount forces rclone's VFS to enumerate the ENTIRE remote directory
    before the kernel gets its first entry. On a flat S3 prefix with millions
    of keys (aws-open:mur-sst/zarr-v1 -> analysed_sst/) that runs for minutes,
    blows past the macOS NFS deadman, and the OS kills the mount ("Server
    connections interrupted"). rclone can't paginate a listing at any layer, so
    Phase 1's goal is SAFETY, not speed: ask the rcd directly over its loopback
    rc port, bounded by a hard timeout, so a too-huge directory becomes a failed
    request instead of a wedged mount.

    Returns the raw operations/list array (dicts with Name, Size, IsDir,
    ModTime, ...). Does ZERO kernel I/O on the mount path — no os.stat,
    os.scandir, or os.path.isdir of `path`. The (fs, remote) translation is the
    same _mount_for() one rc_mtime_for and the raw proxy use.

    Raises RcListTimeout when the listing exceeds `timeout`, RcListUnavailable
    when the rcd is unreachable / the path is under no known mount, and
    RcListError when the rcd rejects the listing (the path is a file, not a
    directory)."""
    from fused_render.shell.mounts import RC_LIST_TIMEOUT_S, _live_rcd_port
    from .signing import _mount_for
    if timeout is None:
        timeout = RC_LIST_TIMEOUT_S
    m, rel = _mount_for(path)
    if m is None:
        raise RcListUnavailable(f"{path} is under no known mount")
    port = _live_rcd_port()
    if port is None:
        raise RcListUnavailable("rclone rcd is not running")
    # _mount_for returns "." for the mountpoint itself; operations/list wants
    # "" for the fs root ("." yields {"list": null}/nonsense, same quirk
    # operations/stat has).
    remote = "" if rel == "." else rel
    try:
        # Cancellable: operations/list enumerates the WHOLE prefix (the mur-sst
        # runaway), so on timeout we job/stop it instead of orphaning the walk.
        resp = _rc_cancellable(port, "operations/list",
                               {"fs": m["remote"], "remote": remote,
                                "opt": {"noMimeType": True}}, timeout=timeout)
    except RuntimeError as e:
        if _rc_timed_out(e):
            raise RcListTimeout(f"listing {path} timed out after {timeout:g}s") from e
        raise RcListError(str(e)) from e
    listed = resp.get("list") if isinstance(resp, dict) else None
    return listed if isinstance(listed, list) else []


def rc_modtime_epoch(modtime: str | None) -> float | None:
    """RFC3339 ModTime from an rc listing entry -> epoch seconds (float), or
    None when absent/unparseable. rclone emits e.g. "2024-01-02T03:04:05.12Z"
    or with a numeric offset, up to nanosecond precision; datetime parses only
    microseconds, so trailing sub-microsecond digits are trimmed. rclone
    reports a constant sentinel (2000-01-01) for synthetic S3 directories —
    parsed and passed through like any other timestamp."""
    if not modtime:
        return None
    s = modtime.strip()
    # datetime.fromisoformat only accepts 'Z' on 3.11+; normalize to +00:00 so
    # any interpreter agrees.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Normalize the fractional part to EXACTLY 6 digits. rclone emits anywhere
    # from 1-9 fractional digits, but py3.10's fromisoformat accepts only 3 or 6
    # (7+ never parse on any version); an off-count silently returned None and
    # dropped the mtime. Pad short fractions with zeros and trim long ones to
    # microseconds, preserving any trailing timezone offset.
    m = re.match(r"^(.*?)\.(\d+)(.*)$", s)
    if m:
        frac = (m.group(2) + "000000")[:6]
        s = f"{m.group(1)}.{frac}{m.group(3)}"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def serve_url_for(path: str) -> str | None:
    """Localhost HTTP URL serving `path`'s bytes, if it sits under a mount
    with a live HTTP serve (serves.json). /api/fs/raw proxies from this URL
    instead of reading through the kernel mount: analytical readers (the
    duckdb grid) fan out concurrent range reads that wedge the macOS NFS
    client (see SERVE_VFS_OPT), while the same ranges over HTTP are just
    slow, never fatal. None for anything outside a served mount."""
    serves = storage.read_json(serves_path())
    if not isinstance(serves, dict):
        return None
    p = os.path.abspath(path)
    for mp, base in sorted(serves.items(), key=lambda kv: -len(kv[0])):
        if isinstance(base, str) and (p == mp or p.startswith(mp + os.sep)):
            rel = os.path.relpath(p, mp).replace(os.sep, "/")
            return base.rstrip("/") + "/" + urllib.parse.quote(rel)
    return None

"""Mounts — remote storage mounted as local paths via rclone.

A mount is a named remote-storage mount: an rclone remote spec ("gdrive:" or
"s3remote:bucket/prefix") plus a mountpoint under home_dir()/mounts/<name>.
Once mounted, the path flows through /api/fs/* and every reader untouched —
the app itself still only ever sees local absolute paths (D2/D3 reframed:
remoteness lives in the mount layer). Credentials live exclusively in
rclone's own config; this module stores none.

Mount lifecycle goes through `rclone rcd`, rclone's remote-control daemon,
over its local HTTP API (mount/mount, mount/unmount, mount/listmounts) —
one cross-platform mount API instead of per-OS umount commands. The daemon
is spawned detached with its {port, pid} recorded in home_dir()/rcd.json
and reused across server runs (the spawn-or-reuse pattern of the tile-server
daemons, templates/geotiff/tile_server.py). Mounts therefore deliberately
SURVIVE server restarts; a fresh server adopts them via mount/listmounts
instead of orphaning them. Unmount is an explicit user action.

Store: home_dir()/mounts.json, whole-file last-write-wins like
shell/bookmarks.py. Same acyclic-router + X-Fused-guard conventions.
"""
import configparser
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

logger = logging.getLogger(__name__)

router = APIRouter()

# The vfs options every mount gets, validated against a 204MB COG and a 362MB
# parquet (see DECISIONS): `full` caches read ranges as sparse files so warm
# reads are ~0.01s; max-age raised from the 1h default because eviction is
# what makes revisits slow again; read-ahead measured a net loss and left out.
# VfsReadChunkStreams parallelizes the download of a *single* requested chunk
# across N connections — unlike read-ahead it fetches no extra bytes (so it
# doesn't hurt the random-access COG/parquet case), it just saturates the link
# on the big-file cold read that one serial S3 range GET leaves idle.
# CacheMaxSize is the soft LRU cap on the on-disk cache (open files never
# evicted); it lives here — not only on the serve — because the mount and the
# serve share ONE VFS (see SERVE_VFS_OPT), so both must carry the same cap or
# the option sets diverge and rcd splits them into two VFS instances.
#
# This is the CANONICAL option set: SERVE_VFS_OPT is derived from it below so
# the two can never drift out of sync (drift is what split the VFS in two).
VFS_OPT = {
    "CacheMode": "full",
    "ChunkSize": "8M",
    "ChunkSizeLimit": "64M",
    "ChunkStreams": 4,
    "CacheMaxAge": "24h",
    "CacheMaxSize": "20Gi",
    "FastFingerprint": True,
    "DirCacheTime": "30s",
}

# Each mount's remote is ALSO served over localhost HTTP (rcd serve/start) —
# the duckdb reader's fast path for parquet under a mountpoint. Going through
# the kernel NFS mount breaks down for analytical reads: DuckDB fans out many
# concurrent range reads, each 32KB NFS READ stalls behind an 8M VFS chunk
# fetch (minutes on a slow link), and macOS's 1s NFS timeout (timeo=10) then
# declares the server dead — "server connections interrupted" — and drops the
# whole mount. Over HTTP, DuckDB's range GETs go straight to rclone with no
# kernel filesystem in between, so a slow read is just slow, never fatal.
# The serve keeps a `full` vfs cache: a sparse on-disk copy of every byte
# range read (rclone's cache dir), so re-reads survive DuckDB reconnects AND
# server restarts — DuckDB's own external file cache is RAM, per database
# instance. Measured on the 350MB Ookla file: cold open 45s->26s (rclone's
# sequential chunk streaming beats many scattered range GETs) and a
# post-restart reopen 65s->0.2s. The cost is that readahead caches roughly
# the whole of every touched file, hence the LRU size cap (soft — checked
# every poll interval, open files never evicted). Fingerprints (size+modtime,
# with --use-server-modtime from rcd) invalidate cached files that change
# in the store. sync_serves restarts any serve whose vfs options drift from
# this, so edits here roll out to serves adopted from an older server.
#
# Unlike mount/mount's `vfsOpt` object, serve/start takes vfs options as
# FLAT rc parameters named after the CLI flags (--vfs-cache-mode ->
# vfs_cache_mode). An unknown `vfsOpt` key is silently ignored — the serve
# then runs with the cache OFF and every read, warm or not, goes to the
# store (measured: 1MB re-read 4.4s uncached vs 2ms cached).
#
# The serve and the mount MUST be given the same vfs option set: rcd keeps one
# VFS per (fs, options) and reuses it across mount/mount and serve/start only
# when the options match EXACTLY (verified — matching options yield a single
# entry in vfs/list, and a range cached via the serve then reads in ~0.00s
# through the mount path; mismatched options give two VFS instances that share
# the on-disk cache dir but not their in-memory range state, so a serve-warmed
# range costs a fresh ~0.8s S3 fetch when read through the mount). So the flat
# serve params are DERIVED from VFS_OPT, not hand-written — the two literals
# silently drifting is precisely the bug. Values are stringified because
# serve/list echoes params back as strings (bool -> "true", int -> "4"), and
# sync_serves' drift check compares SERVE_VFS_OPT against that echo.
_VFS_OPT_TO_SERVE_PARAM = {
    "CacheMode": "vfs_cache_mode",
    "ChunkSize": "vfs_read_chunk_size",
    "ChunkSizeLimit": "vfs_read_chunk_size_limit",
    "ChunkStreams": "vfs_read_chunk_streams",
    "CacheMaxAge": "vfs_cache_max_age",
    "CacheMaxSize": "vfs_cache_max_size",
    "FastFingerprint": "vfs_fast_fingerprint",
    "DirCacheTime": "dir_cache_time",
}
# KeyError here on import is deliberate: a VFS_OPT key with no serve mapping
# must not silently fall out of the serve's option set (that would re-split
# the VFS) — add the mapping instead.
SERVE_VFS_OPT = {
    _VFS_OPT_TO_SERVE_PARAM[k]: ("true" if v else "false") if isinstance(v, bool) else str(v)
    for k, v in VFS_OPT.items()
}

# macOS mounts rclone's nfsmount through the loopback NFS client, whose
# request timeout defaults aggressively low — a single slow 8M chunk fetch
# overruns it and the kernel drops the WHOLE mount ("server connections
# interrupted"; this is the failure the HTTP serve was added to route
# analytical reads around). `timeo` is in DECISECONDS on macOS/BSD, so
# timeo=600 raises the per-request ceiling to 60s (vs the ~1s default), and
# retrans allows a couple of tries before the mount is declared dead — enough
# that a genuinely slow read is merely slow, not fatal, for the local-path
# readers that can't go through the serve (the geotiff/grid tile daemons,
# rasterio/PIL/laspy). A truly dead mount still fails, and the health probe
# (mount_state) runs timeout-isolated in its own thread, so a high ceiling
# never blocks a request. Passed via mountOpt.ExtraOptions, which rclone
# forwards verbatim as `-o` flags to the macOS `mount` command (verified in
# the rcd -vv log). nfsmount only — the Linux path uses FUSE `mount`, which
# ignores these NFS options.
NFS_MOUNT_OPT = {"ExtraOptions": ["timeo=600", "retrans=2"]}

# Tile-server daemon state files — the two parallel implementations that can
# hold files open under a mount (geotiff, and the grid server shared by
# zarr + netcdf). Unmount asks each to /quit before retrying (EBUSY fix).
DAEMON_STATE_FILES = (
    os.path.expanduser("~/.cache/fused-render-geotiff-v2/daemon.json"),
    os.path.expanduser("~/.cache/fused-render-gridv2/daemon.json"),
)


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to keep shell↛server
    # acyclic (see shell/bookmarks.py).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


# ------------------------------------------------------------------- store


def _path() -> str:
    p = os.path.join(storage.home_dir(), "mounts.json")
    # Prototype-era migration: rename a legacy connectors.json to mounts.json
    # the first time we read, so early users don't lose their entries.
    if not os.path.exists(p):
        legacy = os.path.join(storage.home_dir(), "connectors.json")
        if os.path.exists(legacy):
            try:
                os.rename(legacy, p)
            except OSError:
                pass
    return p


def mounts_dir() -> str:
    return os.path.join(storage.home_dir(), "mounts")


def list_mounts() -> list:
    data = storage.read_json(_path())
    return data if isinstance(data, list) else []


_mounts_generation = 0  # bumped on every _write; see _read_only_mountpoints


def _write(mounts: list) -> None:
    global _mounts_generation
    storage.write_json(_path(), mounts)
    _mounts_generation += 1


# mounts.json writers are all read-modify-write of the whole list, and they
# now run on two threads: the HTTP handlers (create/delete) and the startup
# automount daemon thread (attach_mount persisting a detected read_only
# flag). Unserialized, one writer's stale snapshot silently drops the
# other's record.
_store_lock = threading.Lock()


def mountpoint(m: dict) -> str:
    return os.path.join(mounts_dir(), m["name"])


def add_mount(name: str, remote: str, read_only: bool | None = None) -> dict:
    """Validate and persist a new mount; raises ValueError on bad input.
    Does NOT mount — the endpoint decides whether create implies mount.
    Every mount is remounted at startup (see run_automount); there is no
    per-mount opt-in.

    `read_only` marks the remote as rejecting writes (stat.writable goes
    false for everything under the mountpoint — see server._writable). An
    explicit value is the user's call and is never overwritten by detection;
    leave it None to have attach_mount detect it from the remote's config."""
    name = (name or "").strip()
    remote = (remote or "").strip()
    if not name or any(ch in name for ch in "/\\:") or name.startswith("."):
        raise ValueError("name must be a plain folder-safe name")
    if ":" not in remote:
        raise ValueError(
            "remote must be an rclone spec like 'gdrive:' or 's3remote:bucket/prefix'"
        )
    # Strict bool, not truthiness: this comes straight off a JSON body, and
    # bool("false") is True — a client sending the string would lock a
    # writable mount read-only AND suppress detection forever.
    if read_only is not None and not isinstance(read_only, bool):
        raise ValueError("read_only must be a boolean")
    with _store_lock:
        mounts = list_mounts()
        if any(c["name"] == name for c in mounts):
            raise ValueError(f"a mount named '{name}' already exists")
        if any(c["remote"] == remote for c in mounts):
            raise ValueError(f"'{remote}' is already connected")
        m: dict = {"id": uuid.uuid4().hex[:12], "name": name, "remote": remote}
        if read_only is not None:
            m["read_only"] = read_only
            # Marks the flag as user-chosen so attach-time re-detection
            # leaves it alone (mount_view never exposes this field).
            m["read_only_user"] = True
        mounts.append(m)
        _write(mounts)
    return m


def _update_mount(m: dict) -> None:
    """Persist changed fields of an existing mount record (matched by id)."""
    with _store_lock:
        _write([m if c["id"] == m["id"] else c for c in list_mounts()])


def get_mount(cid: str) -> dict | None:
    return next((c for c in list_mounts() if c["id"] == cid), None)


def remove_mount(cid: str) -> None:
    with _store_lock:
        _write([c for c in list_mounts() if c["id"] != cid])


# -------------------------------------------------------------- rcd client


def _rcd_state_path() -> str:
    return os.path.join(storage.home_dir(), "rcd.json")


def write_rcd_state(port: int, pid: int) -> None:
    storage.write_json(_rcd_state_path(), {"port": port, "pid": pid})


def _rc(port: int, method: str, params: dict | None = None, timeout: float = 30):
    """One rc call. Returns the decoded JSON on 200; raises RuntimeError with
    rclone's error message on any failure."""
    raw = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{method}",
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            detail = ""
        raise RuntimeError(detail or f"rclone rc {method}: HTTP {e.code}") from e
    except OSError as e:
        raise RuntimeError(f"rclone rc {method}: {e}") from e


def _live_rcd_port() -> int | None:
    """The recorded daemon's port iff it answers core/pid; never spawns."""
    state = storage.read_json(_rcd_state_path())
    if not isinstance(state, dict) or not state.get("port"):
        return None
    try:
        _rc(state["port"], "core/pid", timeout=3)
    except RuntimeError:
        return None
    return state["port"]


def rclone_bin() -> str | None:
    """Path to the rclone binary to run.

    Inside the packaged macOS app (py2app sets sys.frozen = "macosx_app",
    same check as deploy.py's _setup_cli_hint) rclone is bundled at
    Contents/Resources/bin/rclone (D103, build_dmg.sh) so mounts work with
    zero user setup — no brew/apt install, no PATH dependency. Outside the
    bundle (dev checkout, Linux) fall back to the system rclone."""
    if getattr(sys, "frozen", None) == "macosx_app":
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        bundled = os.path.join(contents, "Resources", "bin", "rclone")
        if os.path.isfile(bundled):
            return bundled
    return shutil.which("rclone")


# Spawn-or-reuse must be serialized: the startup automount thread and a user
# mount request can race between "no live daemon" and Popen, each starting
# its own rcd and clobbering rcd.json (the loser's daemon is orphaned).
_rcd_lock = threading.Lock()


def ensure_rcd() -> int:
    """Port of a live rcd daemon, spawning one (detached) if none answers.
    Raises RuntimeError when rclone is not installed or the daemon won't come
    up."""
    with _rcd_lock:
        return _ensure_rcd_locked()


def _ensure_rcd_locked() -> int:
    port = _live_rcd_port()
    if port is not None:
        return port
    bin_ = rclone_bin()
    if not bin_:
        raise RuntimeError("rclone is not installed")
    # Pick the port ourselves (parsing rcd's stderr for a :0 bind is brittle).
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # --use-server-modtime: take an object's mtime from the store's LIST
    # response (S3 LastModified etc.) instead of rclone's default, which HEADs
    # every object to read its precise metadata mtime. That per-object HEAD is
    # what makes directory listings — hence recursive search — crawl over a
    # mount: measured ~300ms/object, turning a 264-file sentinel-cogs subtree
    # into a ~78s walk vs ~1.5s with the LIST mtime. We don't need upload-time
    # precision to browse, so trade it for the 50x faster listing.
    subprocess.Popen(
        [bin_, "rcd", "--rc-no-auth", "--use-server-modtime",
         f"--rc-addr=127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # outlives this server on purpose
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            pid = _rc(port, "core/pid", timeout=2).get("pid", 0)
            write_rcd_state(port, pid)
            return port
        except RuntimeError:
            time.sleep(0.2)
    raise RuntimeError("rclone rcd did not come up within 10s")


def rcd_mount_map() -> dict:
    """{mountpoint: remote fs} for every mount rcd currently serves (empty
    when no daemon is live). Read-only: never spawns a daemon just to answer
    a status question."""
    port = _live_rcd_port()
    if port is None:
        return {}
    try:
        listed = _rc(port, "mount/listmounts").get("mountPoints", [])
    except RuntimeError:
        return {}
    return {m.get("MountPoint"): m.get("Fs") for m in listed if isinstance(m, dict)}


def mounted_paths() -> set:
    return set(rcd_mount_map())


# --------------------------------------------------------------- http serves


def serves_path() -> str:
    return os.path.join(storage.home_dir(), "serves.json")


def _http_serves(port: int) -> dict:
    """{fs: {"addr", "id", "vfs"}} for every live rc HTTP serve. "vfs" is the
    vfs option params the serve was started with (every param except the
    type/fs/addr infra keys — i.e. the vfs_* flags AND dir_cache_time, which
    has no vfs_ prefix but is part of the shared option set). This is the
    drift-check input, compared against SERVE_VFS_OPT; capturing only vfs_*
    keys would drop dir_cache_time and make the check always report drift."""
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


# serve/list-then-serve/start isn't atomic; concurrent syncs (automount thread
# vs a user mount request) would each start a serve for the same remote.
_serves_lock = threading.Lock()


# Read-only mountpoints, cached on _mounts_generation: mount_read_only sits
# on the stat/write hot path (server._writable runs on every /api/fs/stat),
# so it must not re-read and re-parse the store per call the way
# list_mounts() does. Keyed on the in-process write counter rather than
# mounts.json's mtime — add_mount and attach-time _update_mount can each
# write within the same mtime tick (whatever the filesystem's resolution),
# which would leave a stale cache silently in place; the counter can't
# collide since every writer holds _store_lock while bumping it.
_ro_cache_lock = threading.Lock()
_ro_cache: tuple | None = None  # (_mounts_generation, [abs read-only mountpoints])


def _read_only_mountpoints() -> list:
    global _ro_cache
    gen = _mounts_generation
    with _ro_cache_lock:
        if _ro_cache is None or _ro_cache[0] != gen:
            _ro_cache = (gen, [os.path.abspath(mountpoint(c))
                               for c in list_mounts() if c.get("read_only")])
        return _ro_cache[1]


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


def _detect_read_only(port: int, fs: str) -> bool | None:
    """Best-effort, NON-MUTATING read-onlyness probe for a remote. Never
    writes a probe object into the user's store; instead:
      - operations/fsinfo: a backend advertising no write feature at all
        (Put/PutStream/Copy — e.g. http) can never take a write.
      - config/get: an anonymous S3 remote (see _s3_without_credentials).
    Returns None when the probe is INCONCLUSIVE — an rc call failed, or the
    reply didn't carry the expected shape (absence of a Features map is
    version skew, not evidence of read-onlyness) — so the caller persists
    nothing and the next attach tries again. Credentials an IAM policy
    limits to read still report writable: only a real write could tell, and
    probing with one would drop junk objects into user buckets."""
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
    return _s3_without_credentials(cfg)


def _refresh_read_only_flag(m: dict, port: int | None = None) -> None:
    """(Re-)detect and persist `read_only` on every attach, so a remote whose
    credentials changed since the last detection (keys added to an
    anonymous remote, or removed) converges without deleting the mount. A
    user-set flag (read_only_user, add_mount) is never overwritten, and an
    inconclusive probe (None) keeps whatever is recorded. Never raises —
    attach_mount's "error string or None" contract must hold even when
    persisting the flag fails."""
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
    remote. Pure prefix check (no probe, no rc), cheap enough for every stat."""
    root = os.path.abspath(mounts_dir())
    return os.path.abspath(path).startswith(root + os.sep)


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


# ------------------------------------------------------- direct upstream URLs
# Cold ranged reads through the serve are latency-bound: its VFS layer
# serializes concurrent uncached reads of one file (~0.25s/seek — a cold
# sorted parquet scan of 256 row groups took 63s), while the same range GETs
# issued straight at the store run in parallel (same scan direct to S3: 6s).
# So /api/fs/raw routes ranged reads to the store's own URL until the file's
# background prefetch has landed it in the serve cache, then back to the
# serve for local-disk replays. The URL is a presigned link minted by the
# running rcd (operations/publiclink — S3/GCS/B2/Azure; credentials never
# leave rclone, the link never leaves this server) or, for anonymous S3
# remotes (which can't sign — "unsupported signer type noAuth"), the plain
# public object URL. Anything else resolves to None and stays on the serve.

# Presigned links default to a 1h expiry; reuse them for well under that so
# a link handed to a proxied read is never near expiry.
_LINK_TTL_S = 30 * 60.0
_upstream_lock = threading.Lock()
_upstream_links: dict = {}  # (fs, rel) -> (url, monotonic expiry)
_upstream_mode: dict = {}   # fs -> "link" | "public" | "none"


def _mount_for(path: str) -> tuple[dict | None, str]:
    """(mount record, remote-relative path) for a path under a mountpoint."""
    p = os.path.abspath(path)
    for m in list_mounts():
        mp = mountpoint(m)
        if p == mp or p.startswith(mp + os.sep):
            return m, os.path.relpath(p, mp).replace(os.sep, "/")
    return None, ""


def _public_object_url(fs: str, rel: str) -> str | None:
    """Plain https URL for an object on an ANONYMOUS AWS S3 remote — the one
    backend class that can't presign but doesn't need to. Credentialed or
    non-AWS remotes return None (their objects aren't reachable unsigned)."""
    name, _, root = fs.partition(":")
    port = _live_rcd_port()
    if port is None:
        return None
    try:
        cfg = _rc(port, "config/get", {"name": name}, timeout=10)
    except RuntimeError:
        return None
    if (not isinstance(cfg, dict) or not _s3_without_credentials(cfg)
            or cfg.get("endpoint")):
        return None
    bucket, _, prefix = root.partition("/")
    if not bucket:
        return None
    key = (prefix.rstrip("/") + "/" if prefix else "") + rel
    region = cfg.get("region") or "us-east-1"
    return f"https://{bucket}.s3.{region}.amazonaws.com/" + urllib.parse.quote(key)


def upstream_url_for(path: str) -> str | None:
    """Direct store URL for a mount-backed file, or None when the backend has
    no reachable one (the caller then stays on the serve). Never raises —
    this sits on the raw-proxy hot path."""
    try:
        return _upstream_url_for(path)
    except Exception:
        logger.warning("upstream url for %r failed", path, exc_info=True)
        return None


def _upstream_url_for(path: str) -> str | None:
    m, rel = _mount_for(path)
    if m is None:
        return None
    fs = m["remote"]
    now = time.monotonic()
    with _upstream_lock:
        hit = _upstream_links.get((fs, rel))
        if hit is not None and hit[1] > now:
            return hit[0]
        mode = _upstream_mode.get(fs)
    if mode == "none":
        return None
    url = None
    if mode in (None, "link"):
        port = _live_rcd_port()
        if port is None:
            return None
        try:
            url = _rc(port, "operations/publiclink",
                      {"fs": fs, "remote": rel, "expire": "1h"},
                      timeout=10).get("url") or None
        except RuntimeError:
            url = None
        if url is None and mode == "link":
            return None  # transient failure on a known-linkable remote
        if url is not None:
            mode = "link"
    if url is None:
        url = _public_object_url(fs, rel)
        mode = "public" if url else "none"
    with _upstream_lock:
        _upstream_mode[fs] = mode
        if url is not None:
            ttl = _LINK_TTL_S if mode == "link" else 3600.0
            _upstream_links[(fs, rel)] = (url, now + ttl)
    return url


def sync_serves() -> None:
    """Reconcile rcd's HTTP serves with the stored mounts — one serve per
    mount record, stop serves whose record is gone — and write the resulting
    {mountpoint: base_url} map to serves.json (consumed by serve_url_for).
    Best-effort: any failure logs and leaves the previous map in place."""
    with _serves_lock:
        try:
            _sync_serves_locked()
        except Exception:
            logger.warning("sync of mount http serves failed", exc_info=True)


def _sync_serves_locked() -> None:
    port = _live_rcd_port()
    if port is None:
        storage.write_json(serves_path(), {})
        return
    serves = _http_serves(port)
    mounts = list_mounts()
    out = {}
    for m in mounts:
        fs = m["remote"]
        serve = serves.get(fs)
        if serve is not None and serve["vfs"] != SERVE_VFS_OPT:
            # Stale cache options (serves outlive server runs, so a config
            # change here never reaches an already-running serve otherwise).
            if serve["id"]:
                try:
                    _rc(port, "serve/stop", {"id": serve["id"]})
                except RuntimeError:
                    pass
            serve = None
        if serve is None:
            try:
                addr = _rc(port, "serve/start", {
                    "type": "http",
                    "fs": fs,
                    "addr": "127.0.0.1:0",
                    **SERVE_VFS_OPT,
                }, timeout=30).get("addr", "")
            except RuntimeError as e:
                logger.warning("http serve for %r failed: %s", m["name"], e)
                continue
        else:
            addr = serve["addr"]
        if addr:
            out[mountpoint(m)] = f"http://{addr}"
    wanted = {m["remote"] for m in mounts}
    for fs, serve in serves.items():
        if fs not in wanted and serve["id"]:
            try:
                _rc(port, "serve/stop", {"id": serve["id"]})
            except RuntimeError:
                pass
    storage.write_json(serves_path(), out)


def attach_mount(m: dict) -> str | None:
    """Mount via rcd; returns an error string or None."""
    mp = mountpoint(m)
    os.makedirs(mp, exist_ok=True)
    if os.path.ismount(mp):
        # Already a kernel mount — but is it OURS? A stale mount left by a
        # deleted mount of the same name would otherwise pass for the
        # new remote. rcd knows the fs of every mount it serves; a mismatch
        # is an error, not a silent adopt. (A mount rcd doesn't know about
        # has no queryable fs — adopted as-is, the pre-rcd prototype case.)
        fs = rcd_mount_map().get(mp)
        if fs is not None and fs != m["remote"]:
            return (f"mountpoint already serves '{fs}' — unmount it before "
                    f"mounting '{m['remote']}'")
        # Already mounted (double-click, adopted foreign mount) — but the
        # HTTP serve may still be missing (a prior serve/start failed, or the
        # mount predates the serve layer), so reconcile serves here too:
        # without one, /api/fs/raw silently falls back to reads through the
        # wedge-prone kernel mount.
        sync_serves()
        _refresh_read_only_flag(m)
        return None
    try:
        port = ensure_rcd()
        params = {
            "fs": m["remote"],
            "mountPoint": mp,
            "mountType": "nfsmount" if sys.platform == "darwin" else "mount",
            "vfsOpt": VFS_OPT,
        }
        # macOS only: raise the loopback NFS client's timeout (see NFS_MOUNT_OPT).
        # mountOpt is the NFS transport layer, not a vfs option, so it does NOT
        # affect the (fs, vfsOpt) VFS-reuse key — the mount still shares its VFS
        # with the serve.
        if sys.platform == "darwin":
            params["mountOpt"] = NFS_MOUNT_OPT
        _rc(port, "mount/mount", params, timeout=60)
    except RuntimeError as e:
        return str(e)
    sync_serves()
    _refresh_read_only_flag(m, port)
    return None


def _quit_tile_daemons() -> None:
    """Best-effort /quit to every live tile-server daemon so they release
    open files under the mount (measured EBUSY cause). Absent/corrupt state
    files and dead ports are skipped silently."""
    for state_file in DAEMON_STATE_FILES:
        state = storage.read_json(state_file)
        if not isinstance(state, dict) or not state.get("port"):
            continue
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{state['port']}/quit", timeout=3).read()
        except OSError:
            continue


def _force_unmount(mp: str) -> str | None:
    """Kernel-level unmount for a DEAD mount, escalating to force. Only for
    mounts whose serving daemon is gone/wedged — there is nothing left to
    corrupt, and rcd's own unmount either failed or can't be asked. Returns
    an error string or None."""
    attempts = [["umount", mp]]
    if sys.platform == "darwin":
        attempts += [["umount", "-f", mp], ["diskutil", "unmount", "force", mp]]
    else:
        attempts += [["umount", "-l", mp]]  # lazy: detach now, cleanup later
    last = ""
    for cmd in attempts:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            last = (r.stderr or r.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            last = str(e)
            continue
        if not os.path.ismount(mp):
            return None
    if not os.path.ismount(mp):
        return None
    return f"force unmount of {mp} failed: {last or 'still mounted'}"


def detach_mount(m: dict, force: bool = False) -> str | None:
    """Unmount via rcd; on failure ask the tile daemons to release their
    open files and retry once. Returns an error string or None. Never
    force-unmounts on its own — failing loudly beats corrupted reads.
    `force=True` (an explicit user action on a mount already shown as
    disconnected) escalates every dead end below to _force_unmount."""
    mp = mountpoint(m)
    port = _live_rcd_port()
    if port is None:
        # No daemon: nothing rcd-owned to unmount. A foreign mount at the
        # path (pre-rcd prototype, manual rclone) is not ours to force.
        if os.path.ismount(mp):
            if force:
                return _force_unmount(mp)
            return ("mounted outside the app (no rclone daemon running) — "
                    "unmount it from the terminal")
        return None
    params = {"mountPoint": mp}
    try:
        _rc(port, "mount/unmount", params)
        return None
    except RuntimeError as e:
        # Quitting the tile daemons only helps when the failure is an
        # open-file busy error ("resource busy", "device busy" — macOS and
        # Linux both say "busy"); on any other failure quitting them would
        # tear down previews of unrelated LOCAL files for nothing.
        if "busy" not in str(e).lower():
            if force and os.path.ismount(mp):
                return _force_unmount(mp)
            return f"unmount failed: {e}"
    _quit_tile_daemons()
    time.sleep(0.5)
    try:
        _rc(port, "mount/unmount", params)
        return None
    except RuntimeError as e:
        if force and os.path.ismount(mp):
            return _force_unmount(mp)
        return f"unmount failed (a preview may still hold a file open): {e}"


def _stop_serve_for(port: int, fs: str) -> None:
    """Stop the HTTP serve for `fs`, if one is live. Used by reconnect: rcd
    shares ONE VFS between a mount and its serve, and `mount/unmount` shuts
    that VFS down regardless of the serve's reference to it — verified: after
    unmounting, the serve still replays disk-cached ranges but hangs on any
    uncached read (vfs/list drops to 0). Dropping the serve here lets the
    following sync_serves start a fresh one that re-binds to the remounted
    VFS. Best-effort: a missing serve or a failed stop is fine."""
    serve = _http_serves(port).get(fs)
    if serve and serve["id"]:
        try:
            _rc(port, "serve/stop", {"id": serve["id"]})
        except RuntimeError:
            pass


def reconnect_mount(m: dict) -> str | None:
    """Repair a disconnected mount: clear whatever is wedged at the
    mountpoint, then mount fresh. Returns an error string or None.

    Order matters: ask rcd nicely first (clears its tracking when it still
    lists the mount), force-unmount whatever kernel mount remains (a dead
    NFS mount rejects plain umount — the state this whole path exists for),
    ask rcd once more so a force-cleared mount doesn't linger in its
    listmounts and block the remount, drop the HTTP serve (it shares the
    mount's VFS, which the unmount just tore down — see _stop_serve_for),
    then attach as usual (attach_mount's sync_serves starts a fresh serve
    that re-binds to the remounted VFS)."""
    mp = mountpoint(m)
    port = _live_rcd_port()
    if port is not None:
        try:
            _rc(port, "mount/unmount", {"mountPoint": mp})
        except RuntimeError:
            pass  # wedged: rcd's own umount fails; the force path handles it
    if os.path.ismount(mp):
        err = _force_unmount(mp)
        if err:
            return err
        if port is not None:
            try:
                _rc(port, "mount/unmount", {"mountPoint": mp})
            except RuntimeError:
                pass  # "mount not found" once the kernel mount is gone — fine
    if port is not None:
        _stop_serve_for(port, m["remote"])
    return attach_mount(m)


# How long a mount gets to answer a stat/listdir before it is declared
# disconnected. A wedged NFS mount (rclone's macOS backend) can block those
# syscalls indefinitely, so every probe runs in a throwaway daemon thread —
# a hang costs one leaked (blocked) thread, never a stuck request.
PROBE_TIMEOUT = 3.0


def mount_state(m: dict, rcd_mounts: set, timeout: float = PROBE_TIMEOUT) -> str:
    """Health of one mount: "mounted" | "disconnected" | "unmounted".

    "mounted" requires both that a live rcd serves the mountpoint AND that the
    filesystem actually answers a listdir. The failure this catches: the rclone
    daemon (or its NFS serve) dies while the kernel mount entry survives —
    os.path.ismount() still says True, listings return stale/empty data, and
    a plain unmount fails ("failed to umount the NFS volume"). That state is
    "disconnected", which the UI repairs via /reconnect (force unmount +
    remount) instead of showing a green dot over an empty folder.
    """
    mp = mountpoint(m)
    out: dict = {}

    def probe() -> None:
        try:
            is_mnt = os.path.ismount(mp)
            served = mp in rcd_mounts
            if not is_mnt and not served:
                out["state"] = "unmounted"
            elif is_mnt != served:
                # Kernel and daemon disagree: either a kernel mount whose rcd
                # is gone (or a foreign mount we can't health-check), or rcd
                # still tracking a mount the kernel dropped — the mountpoint
                # is a plain dir masquerading as remote data. Either way,
                # remote data isn't flowing.
                out["state"] = "disconnected"
            else:
                os.listdir(mp)  # the actual I/O health check
                out["state"] = "mounted"
        except OSError:
            out["state"] = "disconnected"

    t = threading.Thread(target=probe, daemon=True, name=f"mount-probe-{m['name']}")
    t.start()
    t.join(timeout)
    return out.get("state", "disconnected")  # no answer in time == wedged


def mount_view(m: dict, rcd_mounts: set | None = None, state: str | None = None) -> dict:
    mp = mountpoint(m)
    listed = mounted_paths() if rcd_mounts is None else rcd_mounts
    if state is None:
        state = mount_state(m, listed)
    return {
        # Only the persisted fields the UI needs; drop any stray keys (e.g. a
        # legacy "automount" flag from prototype-era records).
        "id": m["id"],
        "name": m["name"],
        "remote": m["remote"],
        "mountpoint": mp,
        "state": state,
        # Healthy only — a disconnected mount must not read as mounted.
        "mounted": state == "mounted",
        # Remote rejects writes (see mount_read_only); unflagged legacy
        # records read as rw, the pre-flag behavior.
        "read_only": bool(m.get("read_only")),
    }


# ---------------------------------------------------------- automount/startup


def run_automount() -> None:
    """Remount every mount that isn't already mounted. All mounts are
    remounted at startup — there is no per-mount opt-in. Adoption is implicit:
    mount/listmounts is the status source of truth, so mounts that survived a
    server restart just show up. Best-effort — a failure logs and moves on,
    never blocks startup."""
    mounts = list_mounts()
    if not mounts:
        return
    live = mounted_paths()
    for m in mounts:
        if mountpoint(m) in live or os.path.ismount(mountpoint(m)):
            # Survived the restart, so attach_mount (and its read-only
            # detection) never runs for it below — re-detect here instead,
            # otherwise a legacy record without the flag stays "writable"
            # forever once the kernel mount is already live.
            _refresh_read_only_flag(m)
            continue
        err = attach_mount(m)
        if err:
            logger.warning("automount of %r failed: %s", m["name"], err)
    # Mounts that survived a server restart skip attach_mount above, so their
    # HTTP serves (lost with any rcd restart) get re-ensured here.
    sync_serves()


def startup() -> None:
    """Called from create_app: automount in a daemon thread so a slow or
    missing rclone never delays server start."""
    threading.Thread(target=run_automount, daemon=True, name="mounts-automount").start()


# ---------------------------------------------------------------- endpoints


# --------------------------------------------------- credential auto-detection
# Rather than re-entering keys, surface credentials the user already has in the
# usual dotfiles as ready-to-mount remotes. Each materializes (on first use,
# via /remotes/detect) into a *keyless* rclone remote — env_auth=true, so
# rclone resolves the real credentials from the environment at mount time and
# nothing sensitive is ever written to rclone's config or to this store.


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "profile"


def _aws_profiles() -> list[str]:
    """Profile names from ~/.aws/credentials and ~/.aws/config, honoring the
    AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE overrides. ~/.aws/config
    names non-default profiles '[profile foo]'; credentials uses bare '[foo]'."""
    names: set[str] = set()
    for path, is_config in (
        (os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or "~/.aws/credentials", False),
        (os.environ.get("AWS_CONFIG_FILE") or "~/.aws/config", True),
    ):
        parser = configparser.RawConfigParser()
        try:
            parser.read(os.path.expanduser(path))
        except (configparser.Error, OSError):
            continue
        for section in parser.sections():
            if is_config and section.startswith("profile "):
                names.add(section[len("profile "):].strip())
            else:
                names.add(section.strip())
    return sorted(n for n in names if n)


def _credential_suggestions() -> list[dict]:
    """Remotes offerable without re-entering keys. Full specs (rclone backend +
    params) — the endpoint consumes these; the API view (below) exposes only
    id/label/remote_name/kind.

    The first entry is always present: an anonymous S3 remote for public buckets
    (AWS Open Data, etc.). It needs no credentials — env_auth=false with blank
    keys makes rclone send unsigned requests — so it works even when the user
    has no (or expired) AWS creds. region is just the endpoint rclone starts at;
    it follows S3's region redirect to reach buckets in any region. The rest are
    credential-backed (kind="detected", defaulted in _suggestions_view)."""
    out: list[dict] = [{
        "id": "aws-open-public",
        "label": "AWS S3 — public buckets (no credentials)",
        "remote_name": "aws-open",
        "backend": "s3",
        "kind": "public",
        "params": {"provider": "AWS", "env_auth": "false", "region": "us-west-2"},
    }]
    for prof in _aws_profiles():
        out.append({
            "id": f"aws-profile:{prof}",
            "label": f"AWS S3 — {prof} profile",
            "remote_name": "aws" if prof == "default" else f"aws-{_slug(prof)}",
            "backend": "s3",
            "params": {"provider": "AWS", "env_auth": "true", "profile": prof},
        })
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        out.append({
            "id": "aws-env",
            "label": "AWS S3 — environment credentials",
            "remote_name": "aws-env",
            "backend": "s3",
            "params": {"provider": "AWS", "env_auth": "true"},
        })
    if os.path.exists(os.path.expanduser(
            "~/.config/gcloud/application_default_credentials.json")):
        out.append({
            "id": "gcs-adc",
            "label": "Google Cloud Storage — application default credentials",
            "remote_name": "gcs",
            "backend": "google cloud storage",
            "params": {"env_auth": "true"},
        })
    return out


def _suggestions_view(remotes: list[str]) -> list[dict]:
    """Public shape, minus any suggestion already materialized as a remote (so
    the built-in aws-open drops out of the suggestions once created and shows
    under Remotes instead). `kind` groups them in the dropdown: 'public' vs the
    default 'detected'."""
    return [
        {"id": s["id"], "label": s["label"], "remote_name": s["remote_name"],
         "kind": s.get("kind", "detected")}
        for s in _credential_suggestions()
        if f'{s["remote_name"]}:' not in remotes
    ]


def _rclone_state() -> dict:
    bin_ = rclone_bin()
    if not bin_:
        return {"available": False, "version": None, "remotes": [], "suggested": []}
    try:
        version = subprocess.run(
            [bin_, "version"], capture_output=True, text=True, timeout=10
        ).stdout.splitlines()[0]
        remotes_out = subprocess.run(
            [bin_, "listremotes"], capture_output=True, text=True, timeout=10
        ).stdout
        remotes = [r.strip() for r in remotes_out.splitlines() if r.strip()]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return {"available": False, "version": None, "remotes": [], "suggested": []}
    return {"available": True, "version": version, "remotes": remotes,
            "suggested": _suggestions_view(remotes)}


def broken_mount_error(path: str) -> str | None:
    """If `path` sits under one of our mountpoints whose mount isn't healthy,
    the user-facing reason — else None. /api/fs/list consults this before
    trusting an empty or failed listing: a dead mount leaves a plain (empty)
    local dir or a wedged NFS mount behind, which would otherwise render as
    an ordinary empty folder with no hint the remote data ever existed."""
    root = mounts_dir()
    if not path.startswith(root + os.sep):
        return None
    name = path[len(root) + 1:].split(os.sep, 1)[0]
    m = next((c for c in list_mounts() if c["name"] == name), None)
    if m is None:
        return None
    state = mount_state(m, mounted_paths())
    if state == "mounted":
        return None
    reason = "disconnected" if state == "disconnected" else "not mounted"
    return (f"mount '{name}' is {reason} — reconnect it from the Mounts page "
            f"in the sidebar")


@router.get("/api/mounts")
def get_mounts():
    live = mounted_paths()
    mounts = list_mounts()
    # Probe states concurrently: each disconnected/wedged mount blocks its
    # probe for the full PROBE_TIMEOUT, and serially those would stack.
    states: list[str | None] = [None] * len(mounts)
    threads = []
    for i, m in enumerate(mounts):
        def probe(i=i, m=m):
            states[i] = mount_state(m, live)
        t = threading.Thread(target=probe, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(PROBE_TIMEOUT + 1)
    return {
        "rclone": _rclone_state(),
        "mounts": [
            mount_view(m, live, state=s or "disconnected")
            for m, s in zip(mounts, states)
        ],
    }


@router.post("/api/mounts")
def create_mount(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        # An explicit read_only in the body wins over attach-time detection —
        # the caller knows their credentials better than the probe does.
        # add_mount validates it (strict bool or absent).
        m = add_mount(body.get("name") or "", body.get("remote") or "",
                      read_only=body.get("read_only"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    err = attach_mount(m)
    if err:
        # Create implies mount; a mount that never mounted is not kept.
        remove_mount(m["id"])
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.post("/api/mounts/{cid}/mount")
def mount_endpoint(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = attach_mount(m)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.post("/api/mounts/{cid}/reconnect")
def reconnect_endpoint(cid: str, x_fused: str | None = Header(default=None)):
    """Repair a disconnected mount: force-clear the dead mountpoint, remount."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = reconnect_mount(m)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.post("/api/mounts/{cid}/unmount")
def unmount_endpoint(cid: str, force: str = "0",
                     x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = detach_mount(m, force=force == "1")
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return mount_view(m)


@router.delete("/api/mounts/{cid}")
def delete_mount(cid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    m = get_mount(cid)
    if m is None:
        return JSONResponse({"error": "unknown mount"}, status_code=404)
    err = detach_mount(m)
    mp = mountpoint(m)
    if err and os.path.ismount(mp):
        # Deleting the record while the filesystem is still mounted would
        # strand a live mount (and let a re-added name silently reuse it).
        return JSONResponse({"error": f"not deleted — {err}"}, status_code=502)
    if os.path.isdir(mp) and not os.path.ismount(mp) and not os.listdir(mp):
        os.rmdir(mp)
    remove_mount(cid)
    sync_serves()  # stop the deleted mount's HTTP serve, drop its map entry
    return {"ok": True}


@router.post("/api/mounts/remotes")
def create_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Create an S3-compatible rclone remote non-interactively from keys.
    OAuth backends (Drive etc.) are deliberately NOT handled here — users run
    `rclone config` in a terminal; the page explains that. Credentials go
    straight into rclone's own config, never through the store."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bin_ = rclone_bin()
    if not bin_:
        return JSONResponse({"error": "rclone is not installed"}, status_code=502)
    name = (body.get("name") or "").strip()
    if not name or ":" in name or "/" in name:
        return JSONResponse({"error": "invalid remote name"}, status_code=400)
    p = body.get("params") or {}
    cmd = [
        bin_, "config", "create", name, "s3",
        "provider", p.get("provider") or "Other",
        "access_key_id", p.get("access_key_id") or "",
        "secret_access_key", p.get("secret_access_key") or "",
        "env_auth", "false",
    ]
    if p.get("endpoint"):
        cmd += ["endpoint", p["endpoint"]]
    if p.get("region"):
        cmd += ["region", p["region"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "rclone config create timed out (30s)"}, status_code=502)
    if r.returncode != 0:
        return JSONResponse({"error": (r.stderr or r.stdout or "").strip()[-500:]}, status_code=502)
    return {"ok": True, "name": name + ":"}


@router.post("/api/mounts/remotes/detect")
def create_detected_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Materialize a keyless rclone remote from an auto-detected credential
    source (see _credential_suggestions). The spec comes from the server's own
    detection keyed by `id` — never from client-supplied rclone params — and
    env_auth=true means no keys are written. Idempotent: an already-created
    remote is returned as-is."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bin_ = rclone_bin()
    if not bin_:
        return JSONResponse({"error": "rclone is not installed"}, status_code=502)
    sid = (body.get("id") or "").strip()
    sugg = next((s for s in _credential_suggestions() if s["id"] == sid), None)
    if sugg is None:
        return JSONResponse({"error": f"unknown credential source {sid!r}"}, status_code=404)
    name = sugg["remote_name"]
    if f"{name}:" in _rclone_state().get("remotes", []):
        return {"ok": True, "name": name + ":"}
    cmd = [bin_, "config", "create", name, sugg["backend"]]
    for k, v in sugg["params"].items():
        cmd += [k, v]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "rclone config create timed out (30s)"}, status_code=502)
    if r.returncode != 0:
        return JSONResponse({"error": (r.stderr or r.stdout or "").strip()[-500:]}, status_code=502)
    return {"ok": True, "name": name + ":"}

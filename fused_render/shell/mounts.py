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
import xml.etree.ElementTree as ElementTree
from datetime import datetime

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
    # The per-mount ReadOnly (added by _vfs_opt_for) maps to the serve's
    # --read-only flag (NOT --vfs-read-only, which rcd silently ignores — see
    # _serve_vfs_opt_for's history). Listed here so _serve_params derives it
    # rather than any caller hand-writing "read_only".
    "ReadOnly": "read_only",
}


def _serve_params(vfs_opt: dict) -> dict:
    """Map a mount/mount vfsOpt dict to the HTTP serve's flat rc params, via the
    single _VFS_OPT_TO_SERVE_PARAM table. Values are stringified because
    serve/list echoes them back as strings (bool -> "true"/"false", int -> "4")
    and sync_serves' drift check compares against that echo.

    A KeyError here is deliberate: a vfsOpt key with no serve mapping must not
    silently fall out of the serve's option set (that would re-split the VFS
    into a second instance) — add the mapping to _VFS_OPT_TO_SERVE_PARAM
    instead. The guard now covers the per-mount ReadOnly key too, not just the
    canonical VFS_OPT set."""
    return {
        _VFS_OPT_TO_SERVE_PARAM[k]: ("true" if v else "false") if isinstance(v, bool) else str(v)
        for k, v in vfs_opt.items()
    }


# The canonical serve params for the shared VFS options (no per-mount ReadOnly);
# _serve_vfs_opt_for layers each mount's read_only on via _serve_params below.
SERVE_VFS_OPT = _serve_params(VFS_OPT)

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


# INCIDENT (2026-07-16): a mount recorded read_only=true in mounts.json still
# mounted WRITABLE at the rclone layer — vfs/stats reported ReadOnly:false and
# the kernel NFS mount was not rdonly. With CacheMode=full a write (macOS's
# .DS_Store) is accepted into the VFS cache and then retried forever against
# the store, which answers PutObject 403 AccessDenied — 6642 accumulated
# errors before the mount wedged ("Server connections interrupted"). The
# read_only flag was purely an app-level guard (mount_read_only ->
# server._writable): it flipped stat.writable and blocked /api/fs/write, but
# nothing stopped rclone itself, or a non-app writer (Finder), from queuing
# doomed uploads. These helpers push read_only DOWN into the two layers that
# actually accept the bytes, so a read-only remote rejects the write before it
# is ever cached:
#   - the VFS (ReadOnly), shared by the mount's vfsOpt and the HTTP serve's
#     flat params — both must set it identically or rcd splits the VFS in two
#     (see SERVE_VFS_OPT), so the serve carries read_only too;
#   - the macOS kernel NFS mount (rdonly), so even Finder can't write.
# read_write mounts get the explicit falses / no rdonly — the pre-incident
# behavior, stated rather than left to defaults.


def _vfs_opt_for(m: dict) -> dict:
    """The mount's vfsOpt: the canonical VFS_OPT plus ReadOnly driven by the
    record's read_only flag. Explicit False (not omission) so a read_write
    mount reads back ReadOnly:false in vfs/stats and matches its serve's
    read_only=false — the two option sets must agree exactly for the mount
    and serve to share one VFS."""
    return {**VFS_OPT, "ReadOnly": bool(m.get("read_only"))}


def _serve_vfs_opt_for(m: dict) -> dict:
    """The HTTP serve's flat vfs params for this mount: SERVE_VFS_OPT plus
    read_only, the serve-side spelling of the mount's vfsOpt.ReadOnly (the CLI
    flag is --read-only, NOT --vfs-read-only — an unknown rc param is silently
    ignored, and an ignored one here leaves the serve's VFS at ReadOnly:false,
    which both defeats the write guard AND splits the mount/serve VFS in two;
    verified live against rcd: read_only joins the mount's VFS, vfs_read_only
    forked a second instance per remote). Stringified like the rest of
    SERVE_VFS_OPT because serve/list echoes params back as strings, and
    sync_serves' drift check compares against that echo.

    Derived from _vfs_opt_for through _serve_params — NOT hand-written — so the
    mount's vfsOpt and the serve's flat params can never drift (drift is what
    split the VFS in two; the module's derive-don't-hand-write rule)."""
    return _serve_params(_vfs_opt_for(m))


def _nfs_mount_opt(m: dict) -> dict:
    """macOS-only mountOpt for this mount: the NFS transport tuning plus, for a
    read_only record, "rdonly" so the kernel mount itself rejects writes (a
    belt-and-suspenders companion to the VFS ReadOnly above — the VFS stops the
    app and rclone, rdonly stops anything that reaches the kernel mount, e.g.
    Finder dropping a .DS_Store). Same nfsmount-only gating as the timeo/retrans
    options: the Linux FUSE path takes different mount flags and ignores these,
    so this is only ever passed on darwin (see attach_mount)."""
    extra = list(NFS_MOUNT_OPT["ExtraOptions"])
    if m.get("read_only"):
        extra.append("rdonly")
    return {"ExtraOptions": extra}

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


def _rcd_log_path() -> str:
    return os.path.join(storage.home_dir(), "rcd.log")


# rclone's --log-file has no built-in rotation, so cap it ourselves.
RCD_LOG_MAX_BYTES = 10 * 1024 * 1024


def _rotate_rcd_log() -> str:
    """Before spawning rcd, roll the log if it has grown past the cap:
    rcd.log -> rcd.log.1 (overwriting any previous .1). One generation is
    enough — this is diagnostic breadcrumbs, not an audit trail. Returns the
    (current) log path to hand to --log-file.

    INCIDENT 2026-07-16: rcd ran with NO --log-file, so when a read-only mount
    wedged under load there was zero rclone-side evidence to diagnose with (no
    record of the 403 PutObject loop). Best-effort: a stat/rename failure just
    means we append to whatever is there."""
    log = _rcd_log_path()
    try:
        if os.path.getsize(log) > RCD_LOG_MAX_BYTES:
            os.replace(log, log + ".1")
    except OSError:
        pass
    return log


def _copytruncate_rcd_log() -> None:
    """Enforce the log cap against a LIVE daemon (server startup path).

    _rotate_rcd_log's os.replace only rolls the file when THIS process spawns a
    new rcd, but the daemon is detached and outlives server restarts — so a
    long-lived rcd's log grows unbounded, its cap never re-checked. os.replace
    can't rotate under it either: rclone holds the inode open in append mode and
    would keep writing to the renamed file. Copytruncate instead — copy the
    current contents to rcd.log.1, then truncate the live file in place (its fd
    keeps appending past offset 0, which is safe for O_APPEND writers). Fully
    best-effort: any failure (missing file, permissions) must never block
    startup, so it's swallowed."""
    log = _rcd_log_path()
    try:
        if os.path.getsize(log) <= RCD_LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        with open(log, "r+b") as f:
            data = f.read()
            with open(log + ".1", "wb") as backup:
                backup.write(data)
            f.seek(0)
            f.truncate(0)
    except OSError:
        logger.warning("rcd log copytruncate failed", exc_info=True)


def write_rcd_state(port: int, pid: int, log_path: str | None = None) -> None:
    # Record the log path alongside port/pid so tooling (and a human tailing
    # the daemon) can find it without reconstructing home_dir() (INCIDENT).
    storage.write_json(
        _rcd_state_path(),
        {"port": port, "pid": pid, "log": log_path or _rcd_log_path()},
    )


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


# A live-port probe (core/pid over the loopback rc port, timeout=3) runs on
# EVERY rc-routed call — rc_list_dir, rc_mtime_for, _remote_config, the S3
# capability check. A single fs/walk fans that probe out across every directory
# it lists, so an un-memoized probe is up to ~3s of pure overhead per dir. Cache
# the verified port for a short TTL, keyed on the recorded (port, pid): a new
# daemon writes a new state (different key) and is picked up at once, while a
# burst of calls within the window shares one probe. Only a SUCCESSFUL probe is
# cached — "rcd down" always re-probes (a refused connection is cheap), so the
# daemon coming up is never masked. The short TTL keeps liveness detection: a
# daemon that dies is noticed within _LIVE_PORT_TTL_S.
_LIVE_PORT_TTL_S = 1.0
_live_port_lock = threading.Lock()
_live_port_cache: tuple | None = None  # ((port, pid), port, monotonic expiry)


def _live_rcd_port() -> int | None:
    """The recorded daemon's port iff it answers core/pid; never spawns.
    Memoized for _LIVE_PORT_TTL_S per recorded (port, pid) so a walk over many
    directories doesn't re-probe core/pid for every listing."""
    global _live_port_cache
    state = storage.read_json(_rcd_state_path())
    if not isinstance(state, dict) or not state.get("port"):
        return None
    key = (state.get("port"), state.get("pid"))
    now = time.monotonic()
    with _live_port_lock:
        c = _live_port_cache
        if c is not None and c[0] == key and c[2] > now:
            return c[1]
    try:
        _rc(state["port"], "core/pid", timeout=3)
    except RuntimeError:
        return None
    with _live_port_lock:
        _live_port_cache = (key, state["port"], now + _LIVE_PORT_TTL_S)
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
    # --log-file/--log-level: give the detached daemon a durable log so a mount
    # that wedges under load leaves rclone-side evidence (INCIDENT 2026-07-16 —
    # the daemon had none, so the read-only PutObject 403 loop was invisible).
    # Rotate first since rclone won't cap the file itself. stdout/stderr stay
    # DEVNULL: --log-file captures everything, and a detached daemon has no
    # console to write to anyway.
    log_path = _rotate_rcd_log()
    subprocess.Popen(
        [bin_, "rcd", "--rc-no-auth", "--use-server-modtime",
         f"--rc-addr=127.0.0.1:{port}",
         f"--log-file={log_path}", "--log-level", "INFO"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # outlives this server on purpose
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            pid = _rc(port, "core/pid", timeout=2).get("pid", 0)
            write_rcd_state(port, pid, log_path)
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
    remote I/O)."""
    return os.path.abspath(path) == os.path.abspath(mounts_dir())


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
    m, rel = _mount_for(path)
    if m is None:
        return None
    port = _live_rcd_port()
    if port is None:
        return None
    # _mount_for returns "." for the mountpoint itself; operations/stat wants ""
    # for the fs root (remote "." returns {"item": null}, so the mount-ROOT
    # watch would never prime — same quirk operations/list has, normalized in
    # rc_list_dir).
    remote = "" if rel == "." else rel
    try:
        resp = _rc(port, "operations/stat",
                   {"fs": m["remote"], "remote": remote}, timeout=10)
    except RuntimeError:
        return None
    item = resp.get("item") if isinstance(resp, dict) else None
    if not isinstance(item, dict):
        return None
    return item.get("ModTime") or None


# operations/list can't be paginated at any rclone layer (verified: `rclone lsf
# <dir> | head` takes as long as the full listing), so a directory with millions
# of keys takes minutes to enumerate. A hard timeout turns that "dead mount"
# outcome into a plain "listing failed" HTTP error: the request fails, the mount
# lives. 20s is generous for a healthy directory yet well under the macOS NFS
# deadman that a kernel readdir would otherwise trip.
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
        resp = _rc(port, "operations/list",
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
_upstream_cfg: dict = {}    # remote name -> config/get dict (successes only)


def _remote_config(name: str) -> dict | None:
    """config/get for a remote, memoized. Public-URL minting consults the
    config per OBJECT (a zarr store touches thousands), and remote configs
    only change through this process (add_remote restarts nothing that would
    invalidate them) — so one rc round trip per remote, then pure lookups.
    Failures aren't cached: rcd may simply not be up yet."""
    with _upstream_lock:
        cfg = _upstream_cfg.get(name)
    if cfg is not None:
        return cfg
    port = _live_rcd_port()
    if port is None:
        return None
    try:
        cfg = _rc(port, "config/get", {"name": name}, timeout=10)
    except RuntimeError:
        return None
    if not isinstance(cfg, dict):
        return None
    with _upstream_lock:
        _upstream_cfg[name] = cfg
    return cfg


def _anonymous_s3(cfg: dict | None) -> bool:
    """True when the remote is plain AWS S3 with no credentials — the one
    backend class whose objects are reachable by unsigned public URL and
    which can never presign ("unsupported signer type noAuth")."""
    return (cfg is not None and _s3_without_credentials(cfg)
            and not cfg.get("endpoint"))


def _cannot_presign(cfg: dict | None) -> bool:
    """True when the remote is anonymous S3 or anonymous GCS — the backend
    classes that can never presign (S3's "unsupported signer type noAuth", and
    anonymous GCS carrying no signing key at all) but reach their public
    objects by a plain unsigned URL instead. Lets _upstream_url_for skip the
    wasted publiclink rc call for either backend."""
    return cfg is not None and (_anonymous_s3(cfg) or _gcs_anonymous(cfg))


def _mount_for(path: str) -> tuple[dict | None, str]:
    """(mount record, remote-relative path) for a path under a mountpoint."""
    p = os.path.abspath(path)
    for m in list_mounts():
        mp = mountpoint(m)
        if p == mp or p.startswith(mp + os.sep):
            return m, os.path.relpath(p, mp).replace(os.sep, "/")
    return None, ""


def _s3_base_url(bucket: str, region: str) -> str:
    """Base https URL addressing a bucket, applying the dotted-bucket rule once.
    Virtual-hosted style puts the bucket in the TLS hostname, but
    *.s3.<region>.amazonaws.com can't match a bucket whose name contains dots
    (e.g. us-west-2.opendata.source.coop) — every client fails the handshake —
    so a dotted bucket goes path-style instead. Single source of this rule;
    _public_object_url (object URLs), s3_list_page (list query URLs), and
    _fix_dotted_bucket_url (the rewrite case) all route through it."""
    if "." in bucket:
        return f"https://s3.{region}.amazonaws.com/{bucket}"
    return f"https://{bucket}.s3.{region}.amazonaws.com"


def _fix_dotted_bucket_url(url: str) -> str | None:
    """Virtual-hosted S3 URLs put the bucket in the TLS hostname, and
    *.s3.<region>.amazonaws.com can't match a bucket with dots in its name
    (e.g. us-west-2.opendata.source.coop) — every client fails the handshake.
    Rewrite an unsigned dotted-bucket URL to path-style; a SIGNED one can't be
    rewritten (SigV4 covers the Host header), so drop it — the caller then
    stays on the serve proxy, which is slow but works."""
    p = urllib.parse.urlsplit(url)
    m = re.match(r"^(.+)\.s3[.-]([a-z0-9-]+)\.amazonaws\.com$",
                 p.hostname or "")
    if not m or "." not in m.group(1):
        return url
    if p.query:
        return None
    bucket, region = m.group(1), m.group(2)
    return _s3_base_url(bucket, region) + p.path


def _s3_bucket_prefix_region(fs: str, cfg: dict) -> tuple[str, str, str] | None:
    """(bucket, key prefix, region) for an AWS S3 remote's fs string
    (e.g. "aws-open:mur-sst/zarr-v1" -> ("mur-sst", "zarr-v1", "us-east-1")).
    The key prefix is stripped of any trailing slash; region defaults to
    us-east-1. None when the fs carries no bucket. Shared by _public_object_url
    (per-object URLs) and s3_list_page (ListObjectsV2 prefixes) so the two can't
    derive the bucket/region differently."""
    _, _, root = fs.partition(":")
    bucket, _, prefix = root.partition("/")
    if not bucket:
        return None
    return bucket, prefix.rstrip("/"), cfg.get("region") or "us-east-1"


def _public_object_url(fs: str, rel: str) -> str | None:
    """Plain https URL for an object on an ANONYMOUS AWS S3 remote — the one
    backend class that can't presign but doesn't need to. Credentialed or
    non-AWS remotes return None (their objects aren't reachable unsigned).
    Pure string building once _remote_config has memoized the config — no rc
    round trip per object."""
    cfg = _remote_config(fs.partition(":")[0])
    if not _anonymous_s3(cfg):
        return None
    assert cfg is not None
    derived = _s3_bucket_prefix_region(fs, cfg)
    if derived is None:
        return None
    bucket, prefix, region = derived
    key = (prefix + "/" if prefix else "") + rel
    # _s3_base_url applies the dotted-bucket path-style rule (see there).
    return _s3_base_url(bucket, region) + "/" + urllib.parse.quote(key)


def _gcs_public_object_url(fs: str, rel: str) -> str | None:
    """Plain https URL for an object on an ANONYMOUS GCS remote — the GCS
    analog of _public_object_url. GCS always path-addresses the bucket
    (storage.googleapis.com/<bucket>/<key>), so there is no region and no
    dotted-bucket rule. Credentialed or non-GCS remotes return None (their
    objects aren't reachable unsigned). Pure string building once
    _remote_config has memoized the config — no rc round trip per object."""
    cfg = _remote_config(fs.partition(":")[0])
    if not _gcs_anonymous(cfg or {}):
        return None
    derived = _gcs_bucket_prefix(fs)
    if derived is None:
        return None
    bucket, prefix = derived
    key = (prefix + "/" if prefix else "") + rel
    # Match _public_object_url's key quoting (same default safe chars).
    return f"https://storage.googleapis.com/{bucket}/{urllib.parse.quote(key)}"


# ------------------------------------------------------- direct S3 pagination
# rclone can't paginate operations/list at any layer (see rc_list_dir), so a
# flat S3 prefix with millions of keys (aws-open:mur-sst/zarr-v1 ->
# analysed_sst/) times out and the user sees nothing. But S3's own
# ListObjectsV2 paginates fine (~300ms per 1000-key page), so for the one
# backend class that dominates our mounts — anonymous plain AWS S3
# (_anonymous_s3) — fetch a single page at a time straight from S3, unsigned.
# Credentialed / custom-endpoint remotes can't be listed unsigned and stay on
# the Phase 1 rc path.
S3_LIST_TIMEOUT_S = 15.0
_S3_XMLNS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class DirectListError(Exception):
    """A direct (unsigned) S3/GCS listing page failed — an HTTP status (403
    needs auth, 301 wrong region), a network error, or an unparseable body
    (S3 XML / GCS JSON). The caller falls back to rc_list_dir; kept distinct
    from the RcList* family so the fallback ladder (direct -> rc -> 503) reads
    cleanly."""


# Back-compat alias: the direct-listing path started S3-only. Kept so callers
# (and tests) that still import S3ListError keep working.
S3ListError = DirectListError


def s3_direct_capable(path: str) -> bool:
    """True when `path` is mount-backed by an anonymous plain-AWS-S3 remote —
    the one backend class s3_list_page can enumerate unsigned. Lets the server
    pick the fast path without re-deriving the _anonymous_s3 test."""
    m, _ = _mount_for(path)
    if m is None:
        return False
    return _anonymous_s3(_remote_config(m["remote"].partition(":")[0]))


def _s3_listing_prefix(store_prefix: str, rel: str) -> str:
    """ListObjectsV2 prefix for a mount-relative directory: <store prefix>/<rel>,
    no leading slash and exactly one trailing slash (with delimiter=/, that
    groups the directory's immediate children). The mountpoint itself (rel ".")
    lists the store prefix's children; a bucket-root mountpoint (no store
    prefix) yields "" — the whole bucket."""
    if rel == ".":
        joined = store_prefix
    elif store_prefix:
        joined = store_prefix + "/" + rel
    else:
        joined = rel
    joined = joined.strip("/")
    return joined + "/" if joined else ""


def s3_list_page(path: str, *, max_keys: int, continuation: str | None = None,
                 timeout: float | None = None) -> tuple[list, str | None]:
    """One ListObjectsV2 page for a mount-backed directory on an anonymous AWS
    S3 remote, fetched by a plain unsigned HTTPS GET — no kernel I/O on the
    mount, no rclone, no boto3.

    Returns (entries, next_token): entries shaped exactly like rc_list_dir
    output (Name/Size/IsDir/ModTime dicts) so downstream mapping is shared, and
    next_token the S3 continuation token when the listing is truncated, else
    None. CommonPrefixes become synthetic directories (Size/ModTime None);
    Contents become files; the zero-byte placeholder object whose key IS the
    prefix (an S3-console "directory" marker) is skipped.

    Raises S3ListError on any HTTP/network/XML failure so the caller can fall
    back to rc_list_dir; a 403/301 (needs auth / wrong region) raises too,
    never crashes."""
    if timeout is None:
        timeout = S3_LIST_TIMEOUT_S
    m, rel = _mount_for(path)
    if m is None:
        raise S3ListError(f"{path} is under no known mount")
    fs = m["remote"]
    cfg = _remote_config(fs.partition(":")[0])
    if not _anonymous_s3(cfg):
        raise S3ListError(f"{path}: remote {fs!r} is not anonymous AWS S3")
    assert cfg is not None
    derived = _s3_bucket_prefix_region(fs, cfg)
    if derived is None:
        raise S3ListError(f"{path}: remote {fs!r} carries no bucket")
    bucket, store_prefix, region = derived
    prefix = _s3_listing_prefix(store_prefix, rel)
    params = {"list-type": "2", "delimiter": "/", "prefix": prefix,
              "max-keys": str(max_keys)}
    if continuation:
        params["continuation-token"] = continuation
    query = urllib.parse.urlencode(params)
    # _s3_base_url applies the dotted-bucket path-style rule. Path-style
    # addresses the bucket in the path already; virtual-hosted style needs the
    # root "/" before the query string.
    base = _s3_base_url(bucket, region)
    url = f"{base}?{query}" if "." in bucket else f"{base}/?{query}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise S3ListError(f"S3 list {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise S3ListError(f"S3 list {path}: {e}") from e
    try:
        root_el = ElementTree.fromstring(body)
    except ElementTree.ParseError as e:
        raise S3ListError(f"S3 list {path}: unparseable XML") from e
    entries: list = []
    for cp in root_el.findall(f"{_S3_XMLNS}CommonPrefixes"):
        p = cp.findtext(f"{_S3_XMLNS}Prefix") or ""
        name = p[len(prefix):].rstrip("/")
        if name:
            entries.append({"Name": name, "Size": None, "IsDir": True,
                            "ModTime": None})
    for obj in root_el.findall(f"{_S3_XMLNS}Contents"):
        key = obj.findtext(f"{_S3_XMLNS}Key") or ""
        # The zero-byte object whose key IS the prefix is the directory
        # placeholder S3 consoles create — it's this directory, not an entry.
        if key == prefix:
            continue
        name = key[len(prefix):]
        if not name:
            continue
        size_txt = obj.findtext(f"{_S3_XMLNS}Size")
        entries.append({
            "Name": name,
            "Size": int(size_txt) if size_txt and size_txt.isdigit() else None,
            "IsDir": False,
            # RFC3339 already; the mapping site runs rc_modtime_epoch on it.
            "ModTime": obj.findtext(f"{_S3_XMLNS}LastModified"),
        })
    next_token = None
    if (root_el.findtext(f"{_S3_XMLNS}IsTruncated") or "").lower() == "true":
        next_token = root_el.findtext(f"{_S3_XMLNS}NextContinuationToken") or None
    return entries, next_token


# ------------------------------------------------------ direct GCS pagination
# The GCS analog of the S3-direct path above. rclone can't paginate a listing
# at any layer either, so a flat GCS prefix with hundreds of thousands of
# children times out on the rc route exactly as an S3 one does. But GCS's own
# JSON API (objects.list) paginates fine, and anonymous GCS (the gcs-open
# suggestion, _gcs_anonymous) serves it with a plain unsigned GET — so fetch a
# single page at a time straight from GCS, unsigned. There is no dotted-bucket
# / virtual-host rule here: the GCS JSON endpoint always carries the bucket in
# the path, and there is no region.
GCS_LIST_TIMEOUT_S = 15.0
_GCS_LIST_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o"


def gcs_direct_capable(path: str) -> bool:
    """True when `path` is mount-backed by an anonymous GCS remote — the one
    GCS backend class gcs_list_page can enumerate unsigned. Mirrors
    s3_direct_capable for the GCS side."""
    m, _ = _mount_for(path)
    if m is None:
        return False
    return _gcs_anonymous(_remote_config(m["remote"].partition(":")[0]) or {})


def _gcs_bucket_prefix(fs: str) -> tuple[str, str] | None:
    """(bucket, key prefix) for a GCS remote's fs string
    (e.g. "gcs-open:mur-sst/zarr-v1" -> ("mur-sst", "zarr-v1")). The key prefix
    is stripped of any trailing slash. None when the fs carries no bucket. The
    GCS analog of _s3_bucket_prefix_region — no region (GCS has none)."""
    _, _, root = fs.partition(":")
    bucket, _, prefix = root.partition("/")
    if not bucket:
        return None
    return bucket, prefix.rstrip("/")


def gcs_list_page(path: str, *, max_keys: int, continuation: str | None = None,
                  timeout: float | None = None) -> tuple[list, str | None]:
    """One objects.list page for a mount-backed directory on an anonymous GCS
    remote, fetched by a plain unsigned HTTPS GET against the GCS JSON API — no
    kernel I/O on the mount, no rclone, no google SDK.

    Returns (entries, next_token) in the identical shape to s3_list_page:
    entries are Name/Size/IsDir/ModTime dicts (so downstream mapping is shared),
    and next_token the GCS pageToken when the listing is truncated, else None.
    `prefixes` become synthetic directories (Size/ModTime None); `items` become
    files; the zero-byte placeholder object whose name IS the prefix (a GCS
    "directory" marker) is skipped, exactly as s3_list_page skips the key ==
    prefix.

    Raises DirectListError on any HTTP/network/JSON failure so the caller can
    fall back to rc_list_dir; a 403 (needs auth) raises too, never crashes."""
    if timeout is None:
        timeout = GCS_LIST_TIMEOUT_S
    m, rel = _mount_for(path)
    if m is None:
        raise DirectListError(f"{path} is under no known mount")
    fs = m["remote"]
    cfg = _remote_config(fs.partition(":")[0])
    if not _gcs_anonymous(cfg or {}):
        raise DirectListError(f"{path}: remote {fs!r} is not anonymous GCS")
    derived = _gcs_bucket_prefix(fs)
    if derived is None:
        raise DirectListError(f"{path}: remote {fs!r} carries no bucket")
    bucket, store_prefix = derived
    # _s3_listing_prefix is backend-agnostic (prefix/delimiter join) — reuse it.
    prefix = _s3_listing_prefix(store_prefix, rel)
    params = {"delimiter": "/", "prefix": prefix, "maxResults": str(max_keys)}
    if continuation:
        params["pageToken"] = continuation
    query = urllib.parse.urlencode(params)
    url = f"{_GCS_LIST_URL.format(bucket=bucket)}?{query}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise DirectListError(f"GCS list {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DirectListError(f"GCS list {path}: {e}") from e
    try:
        doc = json.loads(body)
    except (ValueError, TypeError) as e:
        raise DirectListError(f"GCS list {path}: unparseable JSON") from e
    entries: list = []
    for p in doc.get("prefixes") or []:
        name = str(p)[len(prefix):].rstrip("/")
        if name:
            entries.append({"Name": name, "Size": None, "IsDir": True,
                            "ModTime": None})
    for obj in doc.get("items") or []:
        key = obj.get("name") or ""
        # The zero-byte object whose name IS the prefix is the directory
        # placeholder GCS consoles create — it's this directory, not an entry.
        if key == prefix:
            continue
        name = key[len(prefix):]
        if not name:
            continue
        size_txt = obj.get("size")
        entries.append({
            "Name": name,
            "Size": int(size_txt) if isinstance(size_txt, str)
            and size_txt.isdigit() else None,
            "IsDir": False,
            # RFC3339 already; the mapping site runs rc_modtime_epoch on it.
            "ModTime": obj.get("updated"),
        })
    return entries, doc.get("nextPageToken") or None


# ------------------------------------------- unified direct-listing dispatch
# The server routes every listing through these two so a call site need not
# know whether the mount is S3 or GCS. direct_list_page re-derives the backend
# from `path`, so a continuation token always feeds back to the backend that
# produced it (an S3 continuation-token and a GCS pageToken never cross).


def direct_list_capable(path: str) -> bool:
    """True when `path` is mount-backed by ANY backend the direct (unsigned)
    pager can enumerate — anonymous plain AWS S3 or anonymous GCS."""
    return s3_direct_capable(path) or gcs_direct_capable(path)


def direct_list_page(path: str, *, max_keys: int, continuation: str | None = None,
                     timeout: float | None = None) -> tuple[list, str | None]:
    """One direct (unsigned) listing page for `path`, routed to the S3 or GCS
    pager by the backend the path resolves to. Returns (entries, next_token) in
    the shared rc/direct shape; raises DirectListError when the path is backed
    by neither direct-listable backend (or the chosen pager fails)."""
    if s3_direct_capable(path):
        return s3_list_page(path, max_keys=max_keys,
                            continuation=continuation, timeout=timeout)
    if gcs_direct_capable(path):
        return gcs_list_page(path, max_keys=max_keys,
                            continuation=continuation, timeout=timeout)
    raise DirectListError(f"{path}: no direct-listable backend")


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
    if mode is None and _cannot_presign(_remote_config(fs.partition(":")[0])):
        # Anonymous S3 or GCS can never presign — don't burn an rc call per
        # remote learning that from publiclink's "unsupported signer type"
        # error.
        mode = "public"
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
        if url is not None:
            url = _fix_dotted_bucket_url(url)
        if url is None and mode == "link":
            return None  # transient failure on a known-linkable remote
        if url is not None:
            mode = "link"
    if url is None:
        # Dispatch by backend, mirroring direct_list_page: whichever object-URL
        # builder recognizes the remote returns a non-None URL, the rest None.
        url = _public_object_url(fs, rel) or _gcs_public_object_url(fs, rel)
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
        want_vfs = _serve_vfs_opt_for(m)
        serve = serves.get(fs)
        if serve is not None and serve["vfs"] != want_vfs:
            # Stale cache options (serves outlive server runs, so a config
            # change here never reaches an already-running serve otherwise).
            # This now also fires when a mount's read_only flips: the serve's
            # read_only must track the mount's vfsOpt.ReadOnly or the two
            # stop sharing one VFS (INCIDENT 2026-07-16).
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
                    **want_vfs,
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
        # Refresh read_only BEFORE reconciling serves: sync_serves derives the
        # serve's read_only param from this record, so refreshing afterwards
        # would leave the serve on the stale flag (disagreeing with the mount
        # and splitting the shared VFS) until some later sync.
        _refresh_read_only_flag(m)
        # An adopted rcd mount keeps whatever vfsOpt it was created with —
        # mount options only apply at mount/mount, and listmounts doesn't echo
        # them — so a mount created before read_only was known (legacy record,
        # or detection just flipped the flag) still has a WRITABLE VFS no
        # matter what the record now says: the doomed-upload retry loop the
        # flag exists to prevent. mounted_read_only records what was actually
        # baked in at mount time; on mismatch, remount to apply. Only for
        # rcd-known mounts (fs set) — a foreign kernel mount is adopted as-is.
        if fs is not None and bool(m.get("read_only")) != bool(
            m.get("mounted_read_only")
        ):
            return reconnect_mount(m)  # unmounts first, so no recursion here
        # Already mounted (double-click, adopted foreign mount) — but the
        # HTTP serve may still be missing (a prior serve/start failed, or the
        # mount predates the serve layer), so reconcile serves here too:
        # without one, /api/fs/raw silently falls back to reads through the
        # wedge-prone kernel mount.
        sync_serves()
        return None
    try:
        port = ensure_rcd()
        # Detect and persist read_only BEFORE mounting (INCIDENT 2026-07-16):
        # ReadOnly/rdonly have to be baked into the vfsOpt/mountOpt of the very
        # mount/mount call, so read-onlyness must be settled first. Previously
        # this ran AFTER the mount, so an auto-detected read-only remote mounted
        # WRITABLE on its first attach and only became read-only after a
        # restart — long enough to accumulate the doomed-upload loop. A
        # user-set flag short-circuits detection, and an inconclusive probe
        # leaves whatever is recorded, so this never blocks the mount.
        _refresh_read_only_flag(m, port)
        params = {
            "fs": m["remote"],
            "mountPoint": mp,
            "mountType": "nfsmount" if sys.platform == "darwin" else "mount",
            # Per-mount vfsOpt: VFS_OPT plus ReadOnly from the record, so a
            # read-only remote's VFS rejects writes instead of caching them for
            # a forever-retried upload (see _vfs_opt_for).
            "vfsOpt": _vfs_opt_for(m),
        }
        # macOS only: raise the loopback NFS client's timeout, and add "rdonly"
        # for a read-only mount (see NFS_MOUNT_OPT / _nfs_mount_opt). mountOpt is
        # the NFS transport layer, not a vfs option, so it does NOT affect the
        # (fs, vfsOpt) VFS-reuse key — the mount still shares its VFS with the
        # serve (whose read_only matches the vfsOpt.ReadOnly here).
        if sys.platform == "darwin":
            params["mountOpt"] = _nfs_mount_opt(m)
        _rc(port, "mount/mount", params, timeout=60)
    except RuntimeError as e:
        return str(e)
    # Record what was actually baked into this mount's vfsOpt: rcd never
    # echoes mount options back, so this is the only way the adopt path above
    # can tell a live VFS predates a read_only change and must be remounted.
    if bool(m.get("mounted_read_only")) != bool(m.get("read_only")):
        m["mounted_read_only"] = bool(m.get("read_only"))
        _update_mount(m)
    sync_serves()
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
    that re-binds to the remounted VFS).

    The leading mount/unmount is also what heals the "stale" split-brain
    (INCIDENT 2026-07-16): rcd lists a mountpoint the kernel already dropped,
    and would refuse to remount over its own stale entry — clearing it first
    lets attach_mount's mount/mount start clean."""
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
    """Health of one mount: "mounted" | "stale" | "disconnected" | "unmounted".

    "mounted" requires both that a live rcd serves the mountpoint AND that the
    filesystem actually answers a listdir. The failures this catches are the
    two ways the kernel mount table and rcd's mount/listmounts disagree:

      - kernel says mounted, rcd does NOT list it: the rclone daemon (or its
        NFS serve) died while the kernel mount entry survives —
        os.path.ismount() still says True, listings return stale/empty data,
        and a plain unmount fails ("failed to umount the NFS volume"). Reported
        "disconnected".

      - rcd lists the mount, kernel does NOT (os.path.ismount False): the
        split-brain from INCIDENT 2026-07-16 — the user hit "Disconnect" on the
        macOS "Server connections interrupted" dialog, the kernel unmounted,
        but mount/listmounts still showed the mount (inUse:2). The mountpoint
        is now a plain local dir masquerading as remote data and rcd will
        refuse to remount over its own stale entry. Reported "stale" — a
        distinct state so the cause is diagnosable in logs/UI, though reconnect
        heals both the same way (its leading mount/unmount clears rcd's stale
        entry before remounting; see reconnect_mount).

    Either mismatch means remote data isn't flowing; the UI repairs both via
    /reconnect instead of showing a green dot over an empty folder.
    """
    mp = mountpoint(m)
    out: dict = {}

    def probe() -> None:
        try:
            is_mnt = os.path.ismount(mp)
            served = mp in rcd_mounts
            if not is_mnt and not served:
                out["state"] = "unmounted"
            elif served and not is_mnt:
                # rcd tracks a mount the kernel dropped (INCIDENT split-brain).
                out["state"] = "stale"
            elif is_mnt and not served:
                # Kernel mount whose rcd is gone (or a foreign mount we can't
                # health-check).
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
        mp = mountpoint(m)
        if mp in live and not os.path.ismount(mp):
            # Split-brain: rcd lists the mount but the kernel dropped it.
            # mount/mount over rcd's own stale entry would fail — leave it
            # for mount_state to surface as "stale" and Reconnect to heal.
            continue
        # A mount that survived the restart takes attach_mount's
        # already-mounted branch, which re-runs read-only detection and
        # remounts if the live VFS was created before the current
        # read_only flag (adopted mounts keep their original vfsOpt) —
        # otherwise a legacy writable VFS would outlive the flag forever.
        err = attach_mount(m)
        if err:
            logger.warning("automount of %r failed: %s", m["name"], err)
    # Mounts that survived a server restart skip attach_mount above, so their
    # HTTP serves (lost with any rcd restart) get re-ensured here.
    sync_serves()


def startup() -> None:
    """Called from create_app: automount in a daemon thread so a slow or
    missing rclone never delays server start."""
    # Enforce the rcd log cap here too, not only on respawn: the daemon outlives
    # server restarts, so this is the one reliable moment to cap a log a
    # long-lived rcd has grown past it (see _copytruncate_rcd_log).
    _copytruncate_rcd_log()
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

    The first two entries are always present: anonymous S3 and anonymous GCS
    remotes for public buckets (AWS Open Data, public GCS datasets, etc.).
    They need no credentials — S3 via env_auth=false with blank keys (unsigned
    requests), GCS via anonymous=true — so they work even when the user has no
    (or expired) cloud creds. region is just the endpoint rclone starts at; it
    follows S3's region redirect to reach buckets in any region. The rest are
    credential-backed (kind="detected", defaulted in _suggestions_view)."""
    out: list[dict] = [{
        "id": "aws-open-public",
        "label": "AWS S3 — public buckets (no credentials)",
        "remote_name": "aws-open",
        "backend": "s3",
        "kind": "public",
        "params": {"provider": "AWS", "env_auth": "false", "region": "us-west-2"},
    }, {
        "id": "gcs-open-public",
        "label": "Google Cloud Storage — public buckets (no credentials)",
        "remote_name": "gcs-open",
        "backend": "google cloud storage",
        "kind": "public",
        "params": {"anonymous": "true"},
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
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        out.append({
            "id": "gcs-env",
            "label": "Google Cloud Storage — environment credentials",
            "remote_name": "gcs-env",
            "backend": "google cloud storage",
            "params": {"env_auth": "true"},
        })
    return out


def _rclone_config_dump(bin_: str) -> dict:
    """Every remote's stored config as {bare_name: {"type": …, …params}} via
    `rclone config dump` — a plain subprocess, no rcd daemon required (keeps
    _rclone_state callable before any mount exists). {} on any failure, so
    _remote_label just degrades to bare names rather than raising."""
    try:
        out = subprocess.run(
            [bin_, "config", "dump"], capture_output=True, text=True, timeout=10
        ).stdout
        cfg = json.loads(out) if out.strip() else {}
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}


def _remote_label(remote: str, suggestions: list[dict], configs: dict) -> str:
    """Friendly label for a materialized rclone remote, so it presents under the
    SAME human name the suggestion used across its whole lifecycle (e.g. the
    built-in public option shows as "AWS S3 — public buckets…", not the cryptic
    "aws-open:" it materializes into). Match against the FULL suggestion set —
    including ones already materialized, which _suggestions_view drops.

    Matching is by PROVENANCE, not name alone: the remote's stored config (from
    `rclone config dump`, keyed by bare name) must match the suggestion's backend
    and every param it was created with. A user's own remote that merely happens
    to be named `aws`/`gcs` therefore keeps its bare name instead of inheriting a
    credential-source label it never came from. Values compare case-insensitively
    (rclone normalizes booleans). No match (e.g. "myminio:") → the bare string."""
    cfg = configs.get(remote.rstrip(":"), {})
    for s in suggestions:
        if (f'{s["remote_name"]}:' == remote
                and str(cfg.get("type", "")).lower() == s["backend"].lower()
                and all(str(cfg.get(k, "")).lower() == str(v).lower()
                        for k, v in s["params"].items())):
            return s["label"]
    return remote


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
        names = [r.strip() for r in remotes_out.splitlines() if r.strip()]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return {"available": False, "version": None, "remotes": [], "suggested": []}
    # Each remote carries its verbatim rclone spec (`name`, incl trailing ':',
    # used unchanged as the mount base) plus a friendly `label` for display —
    # so a remote reads under one stable human name whatever its lifecycle stage.
    # Compute the suggestion set and the config dump once, then label every
    # remote against them (both do I/O, so a per-remote call would be O(N)).
    suggestions = _credential_suggestions()
    configs = _rclone_config_dump(bin_)
    remotes = [{"name": n, "label": _remote_label(n, suggestions, configs)}
               for n in names]
    return {"available": True, "version": version, "remotes": remotes,
            "suggested": _suggestions_view(names)}


def broken_mount_error(path: str) -> str | None:
    """If `path` sits under one of our mountpoints whose mount isn't healthy,
    the user-facing reason — else None. /api/fs/list consults this before
    trusting an empty or failed listing: a dead mount leaves a plain (empty)
    local dir or a wedged NFS mount behind, which would otherwise render as
    an ordinary empty folder with no hint the remote data ever existed."""
    # abspath (NOT realpath) the input before the prefix check, consistent with
    # is_mount_backed: a raw request path carrying ".." or a missing leading
    # slash would otherwise fail the prefix match and misclassify a broken mount
    # as a plain 400 instead of the 503 "reconnect" it deserves.
    root = os.path.abspath(mounts_dir())
    p = os.path.abspath(path)
    if not p.startswith(root + os.sep):
        return None
    name = p[len(root) + 1:].split(os.sep, 1)[0]
    m = next((c for c in list_mounts() if c["name"] == name), None)
    if m is None:
        return None
    state = mount_state(m, mounted_paths())
    if state == "mounted":
        return None
    # A mount backed by detected (env_auth) credentials that have since
    # expired stops flowing with an opaque kernel I/O error — same
    # "disconnected" symptom as a dead daemon, but "reconnect" can't fix an
    # expired SSO token. When the remote probes credential-shaped, tell the
    # user to refresh their credentials instead of pointing them at reconnect.
    if state in ("disconnected", "stale"):
        cred_err = _mount_credential_error(m)
        if cred_err:
            return f"mount '{name}' — {cred_err}"
    # "stale" (the INCIDENT split-brain) and "disconnected" both mean a mount
    # that was there and stopped flowing — same user-facing wording; only a
    # never-mounted mount reads as "not mounted".
    reason = "not mounted" if state == "unmounted" else "disconnected"
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


# Errors that mean the credential material itself is bad — an expired STS/SSO
# session, a revoked OAuth grant, a deleted access key — as opposed to valid
# credentials that merely lack a permission (AccessDenied) or a transient
# network failure. Matched case-insensitively against rclone's output.
_BAD_CRED_MARKERS = (
    "expiredtoken", "expired token", "token has expired", "token is expired",
    # Google ADC/OAuth refresh failure: "Token has been expired or revoked."
    # — matches neither "has expired" nor "is expired" above.
    "has been expired or revoked",
    "invalidaccesskeyid", "invalidclienttokenid", "signaturedoesnotmatch",
    "no valid credential", "nocredentialproviders",
    "invalid_grant", "unauthenticated", "401 unauthorized",
    "could not find default credentials",
)


def _detected_credential_error(bin_: str, name: str) -> str | None:
    """Probe a just-materialized env_auth remote with a top-level listing and
    return a user-facing message when the underlying credentials are expired
    or invalid, else None. Detection surfaces creds that merely EXIST in the
    dotfiles — nothing proves they still work, and mounting with a stale SSO
    token fails later with an opaque I/O error, so catch it here where the
    fix is actionable. Only credential-shaped failures (_BAD_CRED_MARKERS)
    reject: AccessDenied (valid keys without ListBuckets permission) and
    transient/network errors pass — the check exists to catch stale keys
    early, not to demand list permission."""
    try:
        r = subprocess.run(
            [bin_, "lsd", f"{name}:", "--max-depth", "1",
             "--contimeout", "5s", "--timeout", "10s",
             "--retries", "1", "--low-level-retries", "2"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return None
    err = ((r.stderr or "") + (r.stdout or "")).lower()
    if any(m in err for m in _BAD_CRED_MARKERS):
        return ("the detected credentials appear expired or invalid — "
                "refresh them (e.g. `aws sso login` or `gcloud auth "
                "application-default login`) and try again")
    return None


def _mount_credential_error(m: dict) -> str | None:
    """For a broken mount whose remote is backed by detected (env_auth)
    credentials, the 'refresh your credentials' message when a top-level
    listing now fails credential-shaped — else None. broken_mount_error uses
    this to distinguish an expired-credential mount (reconnect won't help;
    the user must re-auth) from a merely dead daemon. Only env_auth remotes
    are probed: anonymous/public and key-carrying remotes don't expire this
    way, and the probe (an rclone `lsd`) is paid only on the already-broken
    fs/list path, never on a healthy listing."""
    bin_ = rclone_bin()
    if not bin_:
        return None
    name = m["remote"].partition(":")[0]
    cfg = _remote_config(name)
    if not isinstance(cfg, dict) or str(cfg.get("env_auth", "")).lower() != "true":
        return None
    return _detected_credential_error(bin_, name)


@router.post("/api/mounts/remotes/detect")
def create_detected_remote(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Materialize a keyless rclone remote from an auto-detected credential
    source (see _credential_suggestions). The spec comes from the server's own
    detection keyed by `id` — never from client-supplied rclone params — and
    env_auth=true means no keys are written. Idempotent: an already-created
    remote is returned as-is — but a detected (env_auth) one is re-probed
    first, since its creds may have expired since creation."""
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
    # Public (anonymous) remotes carry no credentials to go stale; only the
    # detected, env_auth-backed ones get the validity probe (an rclone `lsd`).
    detected = sugg.get("kind", "detected") == "detected"
    # remotes are {name,label} objects now — match on the bare rclone spec.
    if any(r["name"] == f"{name}:" for r in _rclone_state().get("remotes", [])):
        # Idempotent re-entry: the remote already exists. Don't report it
        # healthy on faith — a detected remote's creds may have expired since
        # it was created, and returning {"ok": True} here would invite a doomed
        # mount just as surely as a freshly created stale one. Re-probe (one
        # `lsd`) so an expired detected remote is never reported ok; anonymous
        # remotes carry nothing that expires and return quickly.
        if detected:
            cred_err = _detected_credential_error(bin_, name)
            if cred_err:
                return JSONResponse({"error": cred_err}, status_code=502)
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
    # A detected remote whose creds turn out expired is rolled back so the
    # broken thing doesn't linger under Remotes inviting doomed mounts.
    if detected:
        cred_err = _detected_credential_error(bin_, name)
        if cred_err:
            # Roll back the just-created remote. If the delete itself fails
            # (non-zero exit or an OSError/timeout) the remote may still exist,
            # so say so rather than returning the bare cred error as if cleanup
            # succeeded — a silently-lingering remote would be reported ok on
            # the next detect and re-invite the doomed mount.
            try:
                d = subprocess.run([bin_, "config", "delete", name],
                                   capture_output=True, text=True, timeout=30)
                removed = d.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                removed = False
            if not removed:
                cred_err += (" (the half-created remote could not be removed "
                             "automatically — delete it manually before "
                             "retrying)")
            return JSONResponse({"error": cred_err}, status_code=502)
    return {"ok": True, "name": name + ":"}

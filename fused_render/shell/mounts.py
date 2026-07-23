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
is spawned with its {port, pid} recorded in home_dir()/rcd.json and reused
across server runs (the spawn-or-reuse pattern of the tile-server daemons,
templates/geotiff/tile_server.py). Unmount is an explicit user action.

Whether the daemon (and its mounts) survives the server dying depends on
FUSED_RENDER_RCLONE_PERSIST (see _rclone_should_persist). In DEV (dev.sh sets
it) rcd is spawned detached (setsid) so it deliberately SURVIVES the frequent
watchfiles restarts — a fresh server re-adopts the live mounts via
mount/listmounts instead of re-mounting + re-warming the VFS cache. In
PRODUCTION (unset) rcd is a normal child that dies with the server, so quitting
the app tears the mounts down cleanly; the next launch finds the dead pid in
rcd.json stale and respawns.

Store: home_dir()/mounts.json, whole-file last-write-wins like
shell/bookmarks.py. Same acyclic-router + X-Fused-guard conventions.
"""
import collections
import configparser
import email.utils
import json
import logging
import os
import re
import shutil
import signal
import socket
import stat as stat_mod
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

from fused_render.shell import gcssign, s3sign, storage

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
#
# "nobrowse" is a standard macOS mount flag (`mount_nfs -o nobrowse`): it keeps
# the volume out of Finder's sidebar/desktop and off Spotlight's auto-scan
# radar. Without it, Finder browsing or Spotlight indexing walks the mount with
# readdir, which on an S3-backed remote turns into a full prefix enumeration —
# a latent mount-wedge trigger even when no app touches the mount. Harmless on
# Linux (FUSE ignores it), but only ever passed on darwin anyway (see
# _nfs_mount_opt / attach_mount).
NFS_MOUNT_OPT = {"ExtraOptions": ["timeo=600", "retrans=2", "nobrowse"]}


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


def ensure_mounts_dir() -> str:
    """Create the mounts root and mark it so macOS Spotlight never indexes it,
    returning the path. A `.metadata_never_index` marker in a directory tells
    mds (the Spotlight daemon) to skip the whole subtree — the simplest,
    permission-safe, no-subprocess way to keep Spotlight from auto-walking the
    S3-backed mounts with readdir (a prefix-enumeration mount-wedge trigger,
    the browse-side companion to the "nobrowse" mount flag above). Dropped at
    the root, not per-mount, so it covers mountpoints created later too. A
    best-effort `mdutil -i off` would need privileges and often no-ops, so the
    marker is the primary mechanism; we don't shell out. Idempotent."""
    root = mounts_dir()
    os.makedirs(root, exist_ok=True)
    marker = os.path.join(root, ".metadata_never_index")
    if not os.path.exists(marker):
        try:
            with open(marker, "w"):
                pass
        except OSError:
            # Non-fatal: the mount still works, it just isn't Spotlight-excluded.
            pass
    return root


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


def _rcd_registry_path() -> str:
    """Path to the central registry of every rcd this machine has spawned,
    one entry per home (state) dir.

    Unlike rcd.json — which lives INSIDE home_dir() and so vanishes with the
    dir when a pytest temp home is rmtree'd or a git worktree is deleted, taking
    the only record of that daemon's pid with it — the registry lives at the
    BASELINE home (never branch-nested). One registry is shared by the baseline
    run and every per-branch/worktree run, so reap_stale_rcd() on any run can
    still see, and reap, a daemon whose own (now-deleted) home would otherwise
    leave no trace. This is the ONLY place we learn the pid of a daemon whose
    home dir is already gone."""
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, "rcd-registry.json")


def _register_rcd(pid: int, port: int) -> None:
    """Record a freshly spawned daemon in the central registry, keyed by its
    home dir (a new daemon for the same home replaces the old record). Purely
    additive breadcrumb for reap_stale_rcd — a failure here must never fail a
    mount, so it's swallowed."""
    try:
        home = storage.home_dir()
        reg = storage.read_json(_rcd_registry_path())
        entries = [e for e in reg if isinstance(e, dict)] if isinstance(reg, list) else []
        entries = [e for e in entries if e.get("dir") != home]  # dedupe by home
        entries.append({"pid": pid, "port": port, "dir": home})
        storage.write_json(_rcd_registry_path(), entries)
    except OSError:
        logger.warning("rcd registry write failed", exc_info=True)


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` currently exists. Signal 0 probes without
    delivering anything; EPERM means it exists but is owned by someone else."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_looks_like_rcd(pid: int) -> bool:
    """True only when pid's command line is recognisably an `rclone ... rcd`.

    A conservative identity guard: the reaper must NEVER signal a process that
    merely inherited a pid we once recorded (pids are recycled). Best-effort —
    any ps failure is treated as 'not confirmed', so we fail closed (don't
    kill on doubt)."""
    if not pid or pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return "rclone" in out and "rcd" in out


def _confirmed_our_rcd(entry: dict) -> bool:
    """Proof that entry's pid IS the rclone rcd we recorded, gating a kill.
    Two independent checks, either suffices: (1) the recorded rc port still
    answers core/pid with the recorded pid; (2) the pid's command line is an
    rclone rcd. Both fail closed."""
    pid = entry.get("pid") or 0
    port = entry.get("port")
    if port:
        try:
            if _rc(int(port), "core/pid", timeout=3).get("pid") == pid:
                return True
        except (RuntimeError, ValueError, TypeError):
            pass
    return _pid_looks_like_rcd(pid)


def reap_stale_rcd() -> None:
    """Kill rcd daemons that outlived the home/worktree that spawned them, and
    prune dead entries from the registry. Best-effort and deliberately
    CONSERVATIVE — the rcd is spawned detached and 'outlives the server on
    purpose', so nothing else ever reaps it; days-old orphans from finished
    pytest runs and deleted worktrees are the observed failure mode.

    An entry is only ever killed when BOTH hold:
      * its recorded home (state) dir no longer exists  -> orphaned, AND
      * the pid is still alive AND provably our rclone rcd (_confirmed_our_rcd).
    Then it gets a SIGTERM (rcd unmounts cleanly on SIGTERM) and its registry
    entry is dropped.

    Everything else is left as safe as possible:
      * home dir still present            -> assumed in use, untouched (this is
                                             also the daemon we're about to
                                             reuse/spawn);
      * pid already dead                  -> just drop the stale registry entry;
      * orphaned but NOT provably ours    -> left in the registry, never
                                             blind-killed, for a later run to
                                             reconsider.

    Wired into the (rare) spawn path of _ensure_rcd_locked, not a timer."""
    reg = storage.read_json(_rcd_registry_path())
    if not isinstance(reg, list):
        return
    kept: list = []
    changed = False
    for e in reg:
        if not isinstance(e, dict):
            changed = True
            continue
        pid = e.get("pid") or 0
        home = e.get("dir")
        home_present = isinstance(home, str) and os.path.isdir(home)
        if home_present:
            kept.append(e)  # dir present -> in use, leave alone
            continue
        # home dir is gone -> candidate orphan
        if not _pid_alive(pid):
            changed = True  # already dead: drop the stale record
            continue
        if _confirmed_our_rcd(e):
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info("reaped orphaned rcd pid=%s (home %s gone)", pid, home)
            except OSError:
                logger.warning("failed to signal orphaned rcd pid=%s", pid, exc_info=True)
            changed = True  # drop after signalling
        else:
            kept.append(e)  # alive but unidentifiable -> never blind-kill
    if changed:
        try:
            storage.write_json(_rcd_registry_path(), kept)
        except OSError:
            logger.warning("rcd registry prune failed", exc_info=True)


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
    # spawner_pid records WHO spawned the daemon: rcd is shared per-home, so a
    # later process reusing it (e.g. the macOS app alongside a CLI server) must
    # be able to tell on quit whether the daemon is its own to stop — see
    # stop_local_rcd's ownership gate.
    storage.write_json(
        _rcd_state_path(),
        {
            "port": port,
            "pid": pid,
            "log": log_path or _rcd_log_path(),
            "spawner_pid": os.getpid(),
        },
    )
    # Also record in the central registry so a future run can reap this daemon
    # even after its home dir (and this rcd.json) is deleted (INCIDENT: leaked
    # rcd daemons outliving pytest runs / deleted worktrees for days).
    _register_rcd(pid, port)


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


# Poll cadence for a cancellable async job. A job/status round trip is cheap, so
# a tight interval keeps the added latency of the async path (vs. a synchronous
# _rc) down to one poll for a fast-finishing call — the list/stat hot path pays
# ~this, not a full second.
_RC_JOB_POLL_S = 0.05


def _rc_cancellable(port: int, method: str, params: dict | None = None,
                    timeout: float = 30):
    """Like _rc, but runs `method` as a CANCELLABLE rclone job so a timed-out
    call actually stops rclone's server-side work instead of orphaning it.

    Why this exists (the 14h-runaway INCIDENT): operations/list and
    operations/stat make rclone run an UNBOUNDED ListObjectsV2 over the whole
    prefix (see rc_list_dir / _rc_stat_item). A plain urlopen socket timeout
    only abandons the CLIENT socket — rclone KEEPS enumerating, and repeated
    timed-out calls pile up orphaned walks that pinned a CPU for 14h. Submitting
    with `_async=true` returns a {"jobid": N} immediately; we poll job/status
    until the job finishes or the deadline passes, and on timeout call job/stop
    so rclone's context cancellation propagates into the S3 lister and the walk
    STOPS.

    Returns the job's `output` dict — the SAME shape the synchronous _rc call
    returns for this method (operations/list -> {"list": [...]}, operations/stat
    -> {"item": ...}). Raises RuntimeError exactly where _rc would, so callers
    keep their existing except handling:
      - deadline exceeded -> raised FROM a TimeoutError, so _rc_timed_out()
        recognizes it (rc_list_dir maps it to RcListTimeout; _rc_stat_item to
        the indeterminate sentinel);
      - a failed job      -> raised with rclone's own error message and NO
        timeout cause (rc_list_dir maps it to RcListError, same as the sync
        HTTPError path)."""
    deadline = time.monotonic() + timeout
    p = dict(params or {})
    p["_async"] = True
    # Submitting a job returns at once (only the enumeration is slow), so cap the
    # submit round trip modestly rather than granting it the whole budget.
    submit = _rc(port, method, p, timeout=min(timeout, 10))
    jobid = submit.get("jobid") if isinstance(submit, dict) else None
    if jobid is None:
        # No jobid handed back. If the peer IGNORED _async and ran the command
        # synchronously, `submit` already holds the full payload (operations/list
        # -> {"list": [...]}, operations/stat -> {"item": ...}) — return it rather
        # than re-issuing the same unbounded enumeration a second time. Only a
        # truly empty/absent ack falls back to a fresh sync call on the remaining
        # budget, so behavior still degrades to the old path when there's nothing
        # to reuse.
        if isinstance(submit, dict) and submit:
            return submit
        remaining = deadline - time.monotonic()
        return _rc(port, method, params, timeout=max(remaining, 0.1))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            status = _rc(port, "job/status", {"jobid": jobid},
                         timeout=min(remaining, 10))
        except RuntimeError:
            # Polling itself failed — common near the deadline when the budget
            # (min(remaining, 10)) is tiny, or when a busy rcd is slow to answer.
            # Do NOT let it propagate uncancelled: break out and stop the job
            # below so the in-flight enumeration can't outlive us (the INCIDENT).
            logger.warning("rc job/status failed for job %s; cancelling",
                           jobid, exc_info=True)
            break
        if isinstance(status, dict) and status.get("finished"):
            if status.get("error"):
                raise RuntimeError(status["error"])  # sync error path equivalent
            out = status.get("output")
            return out if isinstance(out, dict) else {}
        time.sleep(min(_RC_JOB_POLL_S, max(deadline - time.monotonic(), 0)))
    # Deadline passed (or polling failed) with the job still running: cancel it
    # server-side so rclone stops enumerating (the whole point), then raise a
    # timeout the callers recognize. job/stop failing is non-fatal — we still raise.
    try:
        _rc(port, "job/stop", {"jobid": jobid}, timeout=3)
    except RuntimeError:
        logger.warning("rc job/stop failed for job %s", jobid, exc_info=True)
    raise RuntimeError(
        f"rc {method} timed out after {timeout:g}s (job {jobid} cancelled)"
    ) from TimeoutError()


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

    Resolution order:
    1. An explicit FUSED_RENDER_RCLONE_BIN pointing at a real file. The
       supervisor's child_environment sets this in packaged builds (Windows
       installer, Linux AppImage) to the rclone bundled in the payload — env
       beats path-guessing per platform, so mounts work with zero user setup.
       A stale/wrong override (not a file) is ignored so it can't shadow a real
       rclone in a dev checkout.
    2. The packaged macOS app bundle (py2app sets sys.frozen = "macosx_app",
       same check as deploy.py's _setup_cli_hint): rclone at
       Contents/Resources/bin/rclone (D103, build_dmg.sh).
    3. The system rclone on PATH (dev checkout, or a host that installed it)."""
    override = os.environ.get("FUSED_RENDER_RCLONE_BIN")
    if override and os.path.isfile(override):
        return override
    if getattr(sys, "frozen", None) == "macosx_app":
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        bundled = os.path.join(contents, "Resources", "bin", "rclone")
        if os.path.isfile(bundled):
            return bundled
    return shutil.which("rclone")


# Whether a freshly spawned rcd should DETACH into its own session (setsid) and
# so outlive this server, or run as a normal child that dies with it.
#
# Detaching is a DEV-ITERATION convenience: dev.sh restarts the server on every
# .py edit (watchfiles), and keeping rcd alive across those restarts skips the
# re-mount + VFS-cache re-warm each time. In PRODUCTION we want a clean teardown
# — quitting the app should kill rcd (and thus unmount) — so detaching is OFF by
# default and only turned on by scripts/dev.sh via FUSED_RENDER_RCLONE_PERSIST.
def _rclone_should_persist() -> bool:
    return os.environ.get("FUSED_RENDER_RCLONE_PERSIST") not in (None, "", "0")


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
    # About to spawn a fresh daemon — a natural, rare moment to opportunistically
    # reap any rcd that outlived a deleted home/worktree (best-effort, never
    # blocks the spawn; NOT on a timer). The hot reuse path above skips this.
    try:
        reap_stale_rcd()
    except Exception:
        logger.warning("reap_stale_rcd failed", exc_info=True)
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
        # Dev (FUSED_RENDER_RCLONE_PERSIST set): setsid into its own session so
        # the daemon outlives watchfiles server restarts. Production (unset):
        # stay a normal child so app teardown reaps it (on Linux via the
        # server's process-group killpg; on Windows it stays in the supervisor's
        # Job either way; on macOS app.py SIGTERMs it explicitly on quit).
        start_new_session=_rclone_should_persist(),
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


# How long _kill_current_rcd waits for a signalled daemon to actually exit
# before escalating / giving up, mirroring the bounded poll ensure_rcd uses on
# the way up.
_KILL_TIMEOUT_S = 5.0


def _kill_current_rcd() -> None:
    """Terminate the recorded rcd daemon, if there is one to terminate.

    Safety invariant (the single most important constraint here): only ever
    signal a pid we can PROVE is our rclone rcd. Reuses the exact gates
    reap_stale_rcd trusts — _pid_alive + _confirmed_our_rcd (which itself folds
    in the rc core/pid check and _pid_looks_like_rcd). A recorded pid that is
    alive but NOT confirmed ours raises rather than risk killing an unrelated
    process that inherited a recycled pid.

    No recorded daemon / a dead pid is a clean no-op: the caller's fresh spawn
    just starts one. SIGTERM first (rcd unmounts cleanly on it), escalating to
    SIGKILL only if it won't exit within _KILL_TIMEOUT_S; we poll until the
    daemon's port stops answering AND the pid is gone."""
    entry = storage.read_json(_rcd_state_path())
    if not isinstance(entry, dict):
        return  # no daemon on record — nothing to kill
    pid = entry.get("pid") or 0
    if not _pid_alive(pid):
        return  # already gone; the stale rcd.json is harmless (spawn overwrites)
    if not _confirmed_our_rcd(entry):
        # Alive but unprovable: pids get recycled, so this could be anything.
        # Fail loud instead of blind-killing (the critical safety invariant).
        raise RuntimeError(
            f"refusing to kill pid {pid}: not confirmed to be our rclone rcd"
        )
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return  # raced us and exited between the check and the signal
        except OSError as e:
            raise RuntimeError(f"failed to signal rcd pid {pid}: {e}") from e
        deadline = time.time() + _KILL_TIMEOUT_S
        while time.time() < deadline:
            if _live_rcd_port() is None and not _pid_alive(pid):
                return  # daemon gone
            time.sleep(0.1)
    raise RuntimeError(f"rcd pid {pid} did not exit after SIGKILL")


def stop_local_rcd() -> None:
    """Best-effort teardown of the rcd we spawned, for the app's quit path.

    Only needed where nothing else reaps rcd on quit — notably macOS, which has
    no supervisor tree-kill (the server runs in-process; app.py, a rumps app).
    On Linux/Windows the process-group killpg / Job Object already collect a
    non-detached rcd, so this is redundant there but harmless.

    Gated on NOT persisting: when FUSED_RENDER_RCLONE_PERSIST is set (dev) the
    detached daemon is meant to outlive the process, so we leave it running.
    Reuses _kill_current_rcd's safety gates (only ever signals a pid PROVEN to
    be our rclone rcd) and swallows every error — a reap failure must never
    block app quit.

    Ownership gate: rcd is shared per-home, so the daemon on record may have
    been spawned by ANOTHER process that is still using it (e.g. the app
    quitting while a CLI `fused-render` server keeps serving mounts). When
    rcd.json records a spawner_pid that is not us and that pid is still alive,
    leave the daemon alone — it is the spawner's to reap. A missing
    spawner_pid (an rcd.json written before the field existed) preserves the
    old behavior and kills."""
    if _rclone_should_persist():
        return
    with _rcd_lock:
        entry = storage.read_json(_rcd_state_path())
        if isinstance(entry, dict):
            spawner_pid = entry.get("spawner_pid") or 0
            if spawner_pid and spawner_pid != os.getpid() and _pid_alive(spawner_pid):
                logger.info(
                    "stop_local_rcd: rcd was spawned by pid %s which is still "
                    "alive; leaving the shared daemon to its owner",
                    spawner_pid,
                )
                return
        try:
            _kill_current_rcd()
        except Exception:
            logger.warning("stop_local_rcd: rcd teardown failed", exc_info=True)


def restart_rcd() -> None:
    """Clean restart of the rcd daemon plus a full re-mount of everything.

    Recovers wedged/disconnected mounts, applies changed mount params, and —
    the credential-expiry fix — forces a brand-new daemon to re-read the static
    credentials (e.g. ~/.aws/credentials): the long-lived rcd reads them ONCE at
    fs instantiation and never again, so a refreshed SSO/STS token only reaches
    a mount after the daemon itself is replaced (neither Reconnect nor a server
    restart helps — the rcd survives both).

    Sequence, serialized against ensure_rcd via _rcd_lock:
      1. force-detach every kernel NFS mount FIRST, so killing rcd can't strand
         a wedged mount (best-effort — a mount already gone is fine);
      2. kill the current daemon (only if confirmed ours — see _kill_current_rcd);
      3. spawn a fresh daemon via the already-locked body (we hold _rcd_lock, so
         calling ensure_rcd() would deadlock the non-reentrant Lock).
    run_automount() (which re-mounts every mount and rebuilds serves.json via
    sync_serves) runs OUTSIDE _rcd_lock — it takes its own _serves_lock, and
    holding both would invert the lock order. A spawn failure propagates: the
    endpoint maps it to a 500 and mounts are left honestly unmounted."""
    with _rcd_lock:
        for m in list_mounts():
            try:
                detach_mount(m, force=True)
            except Exception:
                logger.warning("restart: detach of %r failed",
                               m.get("name"), exc_info=True)
        _kill_current_rcd()
        _ensure_rcd_locked()
    run_automount()


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


# Sentinel returned by _rc_stat_item when operations/stat could not be answered
# at all (no mount record, no live rcd, rc error/timeout, malformed response) —
# as opposed to None, which is a healthy rcd's TRUSTWORTHY "the item is gone".
_STAT_INDETERMINATE = object()

# Default per-call ceiling for a single rc operations/stat. operations/stat has
# NO S3 point lookup — rclone answers a negative or a directory probe with an
# UNBOUNDED ListObjectsV2 of the whole parent prefix, so on a flat world-scale
# prefix (source.coop/earthgenome/...) every probe burns this full timeout. The
# ceiling caps ONE probe; the per-gate budget (_mount_gate_builtins) shrinks it
# further so a gate's serialized probes can't stack to N*ceiling. Direct-capable
# mounts skip this path entirely (see _stat_item -> direct_head/direct_is_dir).
RC_STAT_TIMEOUT_S = 10.0

# A direct stat may run TWO probes back-to-back (a HeadObject, then a max-keys=1
# list) and, on a direct miss, fall back to operations/stat. All of them share
# the caller's SINGLE `timeout` via one deadline (see _stat_item), so a slow
# first probe can't hand the next one a fresh full budget — which is how one
# logical stat used to burn up to 2x the cap. This floor is the smallest slice
# worth spending on a follow-on probe: below it there is no plausible round trip
# left, so we stop and report indeterminate rather than overrun the deadline.
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


# A shim'd condition gate reads exactly one small known file (the zarr.json
# node_type probe). Cap it so a surprise huge file can't stream unbounded
# through the serve — a store's zarr.json is a few KB; 1 MiB is generous.
_GATE_READ_CAP = 1 << 20


def rc_read_bounded(path: str, cap: int = _GATE_READ_CAP, timeout: float = 10) -> bytes:
    """Up to `cap` bytes of a mount-backed file, fetched over the mount's
    localhost HTTP serve (serve_url_for) instead of a kernel open()/read.

    The condition-gate shim uses this for the one bounded zarr.json read: a
    kernel open of a mount file is the same GETATTR/READ class that wedges the
    mount, while a ranged GET over the serve is at worst slow, never fatal.
    Raises OSError on no live serve / transport error / timeout so the gate fails
    closed (urllib.error.URLError and socket timeouts are already OSError)."""
    url = serve_url_for(path)
    if url is None:
        raise OSError(f"no HTTP serve for {path}")
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{cap - 1}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(cap)
    except OSError as e:  # URLError/HTTPError/socket timeout are all OSError
        raise OSError(f"serve read failed for {path}: {e}") from e


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
# Sign-mode presign lifetime. A signed link is handed to the 307 client and
# consumed at once, so this only bounds the window a leaked link is replayable;
# creds are re-resolved every _CRED_TTL_S regardless.
_SIGN_EXPIRY_S = 15 * 60
_SIGN_VALIDATE_TIMEOUT_S = 5.0
_CRED_TTL_S = 60.0  # re-read env / ~/.aws so a rotated key / STS refresh lands
# The botocore provider chain (SSO/IMDS/credential_process) is expensive to walk
# — a black-holed IMDS probe stalls ~1-2s — so its self-refreshing credentials
# object is cached far longer than the 60s frozen-credential window; the object
# refreshes STS itself near expiry, and this bound just lets a fresh `aws sso
# login` be picked up without a restart.
_BOTOCORE_CHAIN_TTL_S = 10 * 60.0
# Re-resolve a GCS bearer token this many seconds BEFORE its stated expiry, so a
# read never hands GCS a token that expires mid-flight.
_GCS_TOKEN_SLACK_S = 60.0
# When sign-mode validation fails while no fallback can be committed (rcd down,
# so publiclink can't cache a "link" mode either), negative-cache the failed
# validation per fs for this window so every raw read doesn't re-run the 1-2
# blocking 5s validation GETs. Short so a transient failure self-heals.
_SIGN_NEG_TTL_S = 45.0
_UPSTREAM_LINKS_CAP = 4096  # bound the publiclink cache (sign mode never inserts)
# A publiclink URL minted under a dying STS token shouldn't stay replayable for
# the full half hour, so a session-token remote gets a shorter link TTL.
_SESSION_TOKEN_LINK_TTL_S = 5 * 60.0
_upstream_lock = threading.Lock()
_upstream_links: dict = {}  # (fs, rel) -> (url, monotonic expiry)
_upstream_mode: dict = {}   # fs -> "link"|"public"|"none"|"sign"|"gsign"|"bearer"
_upstream_cfg: dict = {}    # remote name -> config/get dict (successes only)
_upstream_region: dict = {}  # fs -> region (self-corrected from x-amz-bucket-region)
_cred_cache: dict = {}      # remote name -> (Credentials|None, monotonic expiry)
_botocore_creds_cache: dict = {}  # remote name -> (botocore creds obj|None, exp)
_gcs_token_cache: dict = {}  # remote name -> (gcssign.Token|None, monotonic exp)
_gcs_creds_cache: dict = {}  # remote name -> (google-auth creds obj|None, mono exp)
_gcs_signer_cache: dict = {}  # remote name -> (gcssign.Signer|None, monotonic exp)
_sign_neg_cache: dict = {}  # fs -> monotonic expiry: skip sign validation until
_validation_locks: dict = {}  # fs -> Lock: per-fs single-flight for validation
# fs -> monotonic expiry: a signable-shaped GCS remote whose gsign is in its
# retry window (contended validation, neg-cached, or a momentarily-unresolvable
# signer) should serve THIS request via the bearer proxy rather than dead-end at
# the serve — so bearer_upstream_for treats the remote as bearer-eligible while
# this is live even though its SA key still parses (finding 1).
_gcs_bearer_fallback: dict = {}
# Per-(cache, name) single-flight locks: a cold miss on any upstream resolver
# runs the (network-blocking) source ONCE across N concurrent readers instead of
# each thread independently walking it. Keyed by the cache's id() so every
# per-name cache shares this one registry (the GCS token refresh also serializes
# through it, giving the shared google-auth credential object a single refresher,
# finding 7).
_cache_locks: dict = {}

# Registries so an invalidator can't miss a cache. _NAME_CACHES are the per-
# remote-name resolver caches; _GCS_NAME_CACHES the subset _invalidate_gcs_creds
# drops (the GCS token + credential object); _UPSTREAM_MAPS the per-fs / per-
# remote state that full invalidation also clears.
_GCS_NAME_CACHES = (_gcs_token_cache, _gcs_creds_cache)
_NAME_CACHES = (_cred_cache, _botocore_creds_cache, _gcs_signer_cache,
                *_GCS_NAME_CACHES)
_UPSTREAM_MAPS = (_upstream_cfg, _upstream_mode, _upstream_region,
                  _upstream_links, _sign_neg_cache, _validation_locks,
                  _gcs_bearer_fallback)


def _cached_resolve(cache: dict, name: str, ttl, resolve):
    """Per-name TTL cache with per-name single-flight, shared by every upstream
    resolver cache. `cache` maps name -> (value, monotonic expiry); on a miss,
    exactly ONE thread (per name) runs `resolve()` while the rest wait on the
    per-name lock and then read the value it cached — so N concurrent cold reads
    don't each walk a black-holed IMDS probe / OAuth+ADC round trip.

    `ttl` is the lifetime in seconds, or a callable value->seconds when the
    lifetime depends on the resolved value (the GCS bearer token runs to its own
    expiry). A None result IS cached — the negative caching is load-bearing (it
    bounds how often an absent [cloud-auth] / black-holed metadata endpoint is
    re-probed). Double-checked: the cache is re-read after the lock is acquired
    so a racer that already resolved is reused, not re-resolved. resolve() runs
    WITHOUT _upstream_lock held, so it may call other _cached_resolve caches."""
    now = time.monotonic()
    with _upstream_lock:
        hit = cache.get(name)
        if hit is not None and hit[1] > now:
            return hit[0]
        lock = _cache_locks.setdefault((id(cache), name), threading.Lock())
    with lock:
        with _upstream_lock:
            hit = cache.get(name)
            if hit is not None and hit[1] > time.monotonic():
                return hit[0]
        value = resolve()
        ttl_s = ttl(value) if callable(ttl) else ttl
        with _upstream_lock:
            cache[name] = (value, time.monotonic() + ttl_s)
        return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that never follows: sign-mode validation must observe
    a wrong-region 301/307 itself (with its x-amz-bucket-region header) rather
    than have urllib chase it to a host the signature — which covers Host —
    doesn't match, turning a correctable region hint into an opaque 403."""

    def redirect_request(self, *args, **kwargs):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


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


def _invalidate_upstream_caches() -> None:
    """Drop every memoized upstream fact — config, resolved mode/region,
    credentials, and per-object links. Called on remote create/delete: those
    change a remote's config or credentials out from under the memoization
    (config/get is otherwise cached for the process lifetime), and a changed
    key must be picked up without a restart. Wholesale, not per-name — these
    are rare, user-initiated events, and for anonymous remotes it only forces a
    cheap re-derivation of the public mode."""
    with _upstream_lock:
        for d in (*_UPSTREAM_MAPS, *_NAME_CACHES):
            d.clear()
        _cache_locks.clear()


def _store_upstream_link(key, url: str, expiry: float, now: float) -> None:
    """Insert into the bounded publiclink cache (caller holds _upstream_lock).
    At the cap, evict expired entries first, then the oldest by expiry. Sign
    mode never inserts, so this only guards the publiclink path."""
    if key not in _upstream_links and len(_upstream_links) >= _UPSTREAM_LINKS_CAP:
        for k in [k for k, (_u, exp) in _upstream_links.items() if exp <= now]:
            del _upstream_links[k]
        while len(_upstream_links) >= _UPSTREAM_LINKS_CAP:
            del _upstream_links[min(_upstream_links,
                                    key=lambda k: _upstream_links[k][1])]
    _upstream_links[key] = (url, expiry)


def _link_ttl(fs: str) -> float:
    """publiclink cache TTL for `fs`: the short session-token clamp when the
    remote's credentials carry an STS session token, else _LINK_TTL_S. The token
    can arrive three ways and all three must clamp so a dying token isn't
    replayable for the full half hour: a config `session_token`, or — via
    resolve_credentials — `AWS_SESSION_TOKEN` in the env or `aws_session_token`
    in ~/.aws/credentials on an env_auth/profile remote (e.g. a non-signable
    custom-endpoint S3). Rides the cached _signable_credentials so this adds no
    per-call env/file parsing on the link path. Must be called WITHOUT
    _upstream_lock held (_remote_config / _signable_credentials take it)."""
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    if (cfg or {}).get("session_token"):
        return _SESSION_TOKEN_LINK_TTL_S
    creds = _signable_credentials(name, cfg)
    if creds is not None and creds.session_token:
        return _SESSION_TOKEN_LINK_TTL_S
    return _LINK_TTL_S


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
    wasted publiclink rc call for either backend. (Credentialed GCS is NOT here:
    it presigns via gsign or reads via the bearer proxy — handled by the
    _gcs_signable / _gcs_credentialed branches after this gate.)"""
    return cfg is not None and (_anonymous_s3(cfg) or _gcs_anonymous(cfg))


def _s3_signable_shape(cfg: dict | None) -> bool:
    """Cheap, UNCACHED config-shape gate for the sign path: plain AWS S3 with no
    custom endpoint. NOT custom-endpoint S3 (R2/MinIO/source.coop mirrors):
    endpoint addressing and region conventions vary per provider, so those keep
    the publiclink path. Says nothing about credentials — that is resolved
    separately through the CACHED _signable_credentials, so the hot path never
    re-reads env / ~/.aws per object (anonymous S3 also matches this shape but
    resolves no creds, and callers check _anonymous_s3 first regardless)."""
    return cfg is not None and cfg.get("type") == "s3" and not cfg.get("endpoint")


def _s3_signable(name: str, cfg: dict | None) -> bool:
    """True when the remote is plain AWS S3 (no custom endpoint) whose
    credentials resolve locally — the class we can presign in-process instead
    of round-tripping publiclink per object. NOT anonymous S3 (that carries no
    creds and is handled first by _anonymous_s3/_cannot_presign), and NOT
    custom-endpoint S3. Credential resolution rides the cached
    _signable_credentials so the predicate itself is hot-path safe and can't
    disagree with the URL builder within a _CRED_TTL_S window. Callers must
    check _anonymous_s3 FIRST so an anonymous remote never reaches the
    resolver (its creds resolve to None, so this would return False anyway)."""
    if not _s3_signable_shape(cfg):
        return False
    return _signable_credentials(name, cfg) is not None


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


def _s3_object_url(bucket: str, prefix: str, rel: str, region: str) -> str:
    """One S3 object URL: prefix-join then percent-quote the key, applying the
    dotted-bucket path-style rule via _s3_base_url. The single builder both the
    anonymous (unsigned) and signable (presigned-input) branches of
    _s3_request_url join their key through, so the two can't diverge on the
    prefix-join or the quoting."""
    key = (prefix + "/" if prefix else "") + rel
    return _s3_base_url(bucket, region) + "/" + urllib.parse.quote(key)


def _s3_list_root(bucket: str, region: str) -> str:
    """The bucket-root URL a ListObjectsV2 query hangs off, applying the
    dotted-bucket rule once: a dotted bucket is already path-style (root has no
    trailing slash — "s3.<r>.amazonaws.com/<bucket>?..."), a virtual-hosted one
    needs the "/" before the "?". Shared by the anonymous (base?query) and
    signable (presigned) branches so the dotted-bucket handling can't diverge."""
    base = _s3_base_url(bucket, region)
    return base if "." in bucket else base + "/"


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
    return _s3_object_url(bucket, prefix, rel, region)


def _gcs_public_object_url(fs: str, rel: str) -> str | None:
    """Plain https URL for an object on an ANONYMOUS GCS remote — the GCS
    analog of _public_object_url. GCS always path-addresses the bucket
    (storage.googleapis.com/<bucket>/<key>), so there is no region and no
    dotted-bucket rule. Credentialed or non-GCS remotes return None (their
    objects aren't reachable unsigned). Pure string building once
    _remote_config has memoized the config — no rc round trip per object. Gates
    on anonymous, then delegates the URL construction to _gcs_object_url so the
    path-style layout and key quoting live in one place (C2)."""
    cfg = _remote_config(fs.partition(":")[0])
    if not _gcs_anonymous(cfg or {}):
        return None
    return _gcs_object_url(fs, rel)


def _botocore_chain(name: str, cfg: dict | None):
    """Cached botocore provider-chain credentials OBJECT per remote. The chain
    walk (which can stall ~1-2s on a black-holed IMDS probe) runs at most once
    per _BOTOCORE_CHAIN_TTL_S; the cached object self-refreshes STS near expiry,
    so frozen_from_botocore on it is cheap between walks. Cleared by
    _invalidate_upstream_caches. None (also cached) when the chain yields
    nothing. Single-flight per name via _cached_resolve so a black-holed IMDS
    probe is walked once, not once per concurrent cold reader (finding 10)."""
    return _cached_resolve(_botocore_creds_cache, name, _BOTOCORE_CHAIN_TTL_S,
                           lambda: s3sign.resolve_botocore_chain(cfg))


def _signable_credentials(name: str, cfg: dict | None):
    """resolve_credentials for a remote, cached per name for _CRED_TTL_S so a
    rotated ~/.aws/credentials or an STS refresh is picked up without a
    restart, but the env/file reads aren't paid per object on the sign hot
    path. The botocore rung rides the LONGER-lived, self-refreshing chain-object
    cache (_botocore_chain), so the expensive provider-chain walk isn't repeated
    every _CRED_TTL_S — only get_frozen_credentials runs per window. None (also
    cached) when nothing resolves. Single-flight per name via _cached_resolve
    (finding 10)."""
    def resolve():
        creds = s3sign.resolve_static_credentials(cfg)
        if creds is None and s3sign.needs_botocore(cfg):
            creds = s3sign.frozen_from_botocore(_botocore_chain(name, cfg))
        return creds
    return _cached_resolve(_cred_cache, name, _CRED_TTL_S, resolve)


def _gcs_credentials(name: str, cfg: dict | None):
    """The google-auth credential OBJECT for a GCS remote, cached per name for
    _CRED_TTL_S so the sources (SA key parse / rclone oauth / ADC + GCE metadata
    probe) aren't re-walked per object. The cached object is self-refreshing, so
    token_from_credentials renews it near expiry WITHOUT rebuilding the chain.
    Re-derived from config only once per window (picks up a rotated key), and
    dropped by _invalidate_upstream_caches / invalidate_gcs_token. None (also
    cached) when nothing resolves. Single-flight per name via _cached_resolve so
    the ADC/GCE-metadata probe is walked once, not per concurrent cold reader,
    and the shared credential object gets a single refresher (findings 7, 10)."""
    return _cached_resolve(_gcs_creds_cache, name, _CRED_TTL_S,
                           lambda: gcssign.resolve_credentials(cfg))


def _gcs_bearer_token(name: str, cfg: dict | None):
    """A bearer access Token for a GCS remote, cached per name — the GCS analog
    of _signable_credentials. Derives the token from the cached (self-refreshing)
    credential object, so a live token is reused for its whole life instead of
    forcing an OAuth round trip per _CRED_TTL_S window. The token cache runs to
    expiry minus _GCS_TOKEN_SLACK_S (re-resolved before GCS would reject it); a
    None result (not credentialed / [cloud-auth] absent) is cached for
    _CRED_TTL_S. Returns a gcssign.Token or None. Single-flight per name via
    _cached_resolve — the one refresher requirement of finding 7 (the shared
    google-auth credential's refresh() runs under this per-name lock)."""
    def resolve():
        creds = _gcs_credentials(name, cfg)
        return (gcssign.token_from_credentials(creds)
                if creds is not None else None)

    def ttl(tok):
        if tok is None:
            return _CRED_TTL_S  # None (not credentialed / no [cloud-auth])
        # token.expiry_epoch is wall-clock (time.time); map its remaining life
        # onto the monotonic clock _cached_resolve keys off. Runs to expiry-slack
        # (not clamped to _CRED_TTL_S) — the self-refreshing creds object picks
        # up rotation on the next _CRED_TTL_S re-derivation.
        return max(0.0, tok.expiry_epoch - time.time() - _GCS_TOKEN_SLACK_S)

    return _cached_resolve(_gcs_token_cache, name, ttl, resolve)


def _invalidate_gcs_creds(name: str) -> None:
    """Drop a remote's cached bearer token AND credential object so the next
    resolution re-derives from config (forces a fresh token). Used by the direct
    fetch helper and the bearer read proxy on a 401 (stale/rotated token). Clears
    exactly the GCS token + credential-object caches (the registry keeps this in
    lockstep with what those caches are)."""
    with _upstream_lock:
        for cache in _GCS_NAME_CACHES:
            cache.pop(name, None)


def _gcs_credentialed(name: str, cfg: dict | None) -> bool:
    """True when the remote is GCS, NOT anonymous, and a bearer token resolves —
    the class we can list/probe/read directly with an Authorization header
    instead of crawling the serialized VFS serve. Anonymous GCS carries no
    credentials and is handled first by _gcs_anonymous, so callers must check it
    FIRST (an anonymous remote's token resolves to None, so this returns False
    anyway); the guard keeps the resolver off the anonymous path. Token
    resolution rides the cached _gcs_bearer_token so the predicate is hot-path
    safe and can't disagree with the fetch helper within a _CRED_TTL_S window."""
    if not isinstance(cfg, dict) or cfg.get("type") != "google cloud storage":
        return False
    if _gcs_anonymous(cfg):
        return False
    return _gcs_bearer_token(name, cfg) is not None


def _gcs_object_url(fs: str, rel: str) -> str | None:
    """Plain path-style storage.googleapis.com URL for an object on a GCS remote
    — the unsigned URL both the V4 signer and the bearer proxy start from. Same
    key quoting as _gcs_public_object_url (default safe chars, '/' kept). None
    when the fs carries no bucket. Unlike _gcs_public_object_url this does NOT
    gate on anonymous — the caller has already decided the remote is signable /
    credentialed."""
    derived = _gcs_bucket_prefix(fs)
    if derived is None:
        return None
    bucket, prefix = derived
    key = (prefix + "/" if prefix else "") + rel
    return f"https://storage.googleapis.com/{bucket}/{urllib.parse.quote(key)}"


def _gcs_signer(name: str, cfg: dict | None):
    """gcssign.resolve_signer for a remote, cached per name for _CRED_TTL_S — the
    signer analog of _signable_credentials / _gcs_bearer_token. Without this the
    SA key file is re-opened, re-parsed and re-deserialized on EVERY gsign read
    (once per object) — and a transient open() error would otherwise stick as a
    permanent demotion; the cache bounds that window to one TTL. None (also
    cached) when the remote has no signer-capable SA key. Returns a
    gcssign.Signer or None. Single-flight per name via _cached_resolve
    (finding 10)."""
    return _cached_resolve(_gcs_signer_cache, name, _CRED_TTL_S,
                           lambda: gcssign.resolve_signer(cfg))


def _gcs_signable(name: str, cfg: dict | None) -> bool:
    """True when the remote is a GCS remote whose SERVICE-ACCOUNT KEY resolves —
    the class we can V4-sign locally (raw reads 307 to a signed URL). NOT
    anonymous GCS (public URL) and NOT a token-only GCS remote (user oauth / ADC
    tokens can't sign — those take the bearer proxy). Callers check
    _gcs_anonymous FIRST. Rides the cached _gcs_signer so the predicate is
    hot-path safe."""
    if not isinstance(cfg, dict) or cfg.get("type") != "google cloud storage":
        return False
    if _gcs_anonymous(cfg):
        return False
    return _gcs_signer(name, cfg) is not None


def _gcs_signed_url(fs: str, rel: str) -> str | None:
    """A locally V4-signed GET URL for one object on an SA-key GCS remote, or
    None when the remote isn't signer-capable (no SA key / [cloud-auth] absent /
    a transient key-read error) or carries no bucket. The signer rides the
    cached _gcs_signer (no per-object key re-parse); mints per object — gsign
    mode never caches links, same rationale as S3 sign mode."""
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    signer = _gcs_signer(name, cfg)
    if signer is None:
        return None
    url = _gcs_object_url(fs, rel)
    if url is None:
        return None
    return gcssign.sign_url(url, method="GET", signer=signer.signer,
                            sa_email=signer.sa_email, expires=_SIGN_EXPIRY_S)


def _s3_request_url(fs: str, rel: str, *, method: str = "GET",
                    query: dict | None = None,
                    region_override: str | None = None) -> str | None:
    """The direct S3 URL for one request against a mount's remote — the single
    dispatch the raw-read, listing and probe sites share so they can't diverge
    on how a URL is built or signed:
      - anonymous remote  -> the EXISTING unsigned builders, verbatim
        (_public_object_url for an object; the same base?query build the pager
        used for a listing) — byte-identical to today, resolver never consulted;
      - signable remote   -> a locally presigned URL for `method`;
      - neither           -> None (caller falls back to rc).
    `query` present means a ListObjectsV2 request against the bucket root (its
    params are signed through the presigner, canonicalized in one place);
    absent means an object request keyed by `rel`. `region_override` presigns
    for a specific region WITHOUT publishing it to _upstream_region — used by
    validation to try a trial region before it's been confirmed; absent, the
    adopted (self-corrected) region wins over the config default."""
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    if cfg is None:
        return None
    # Anonymous FIRST: an anonymous remote never resolves credentials, never
    # signs — its URLs are the exact unsigned ones today's code produces.
    if _anonymous_s3(cfg):
        if query is None:
            return _public_object_url(fs, rel)
        derived = _s3_bucket_prefix_region(fs, cfg)
        if derived is None:
            return None
        bucket, _prefix, region = derived
        return f"{_s3_list_root(bucket, region)}?{urllib.parse.urlencode(query)}"
    # Cheap uncached shape gate, then ONE cached credential resolution (the
    # single source of the sign/no-sign decision on this path — no separate
    # uncached pre-gate that could disagree with it within the TTL window).
    if not _s3_signable_shape(cfg):
        return None
    creds = _signable_credentials(name, cfg)
    derived = _s3_bucket_prefix_region(fs, cfg)
    if creds is None or derived is None:
        return None
    bucket, prefix, cfg_region = derived
    if region_override is not None:
        region = region_override
    else:
        with _upstream_lock:
            region = _upstream_region.get(fs, cfg_region)  # adopted region wins
    if query is None:
        url = _s3_object_url(bucket, prefix, rel, region)
        return s3sign.presign_url(url, method=method, region=region,
                                  credentials=creds, expires=_SIGN_EXPIRY_S)
    return s3sign.presign_url(_s3_list_root(bucket, region), method=method,
                              region=region, credentials=creds,
                              extra_query=query, expires=_SIGN_EXPIRY_S)


def _signing_region(fs: str, cfg: dict | None) -> str | None:
    """The region _s3_request_url would presign `fs` for right now: the adopted
    (self-corrected) region if one exists, else the config default. A caller
    captures this just before it signs so it can tell _adopt_region_on_301 what
    region THIS request actually used (see that function)."""
    derived = _s3_bucket_prefix_region(fs, cfg or {})
    if derived is None:
        return None
    with _upstream_lock:
        return _upstream_region.get(fs, derived[2])


def _adopt_region_on_301(fs: str, code: int, headers,
                         signed_region: str | None) -> bool:
    """A wrong-region S3 response (301/307/400) carries the bucket's true region
    in x-amz-bucket-region (307 Temporary Redirect is what S3 returns for a
    newly created bucket whose region hasn't propagated). When it does, adopt it
    into the shared map (under _upstream_lock) so later requests sign for the
    right region, and report whether THIS request should retry — which it must
    whenever the hint differs from the region it actually signed with
    (`signed_region`), NOT whether the shared map already holds the correction.
    Keying the retry off the shared map would let a concurrent request that still
    signed with the stale region see a sibling's correction already applied and
    skip its own needed re-sign/retry, failing it into the slow rc path. Mirrors
    _validate_and_sign's rule so signed listings/probes region-correct exactly
    as signed raw reads do."""
    if code not in (301, 307, 400):
        return False
    corrected = headers.get("x-amz-bucket-region") if headers else None
    if not corrected:
        return False
    with _upstream_lock:
        if _upstream_region.get(fs) != corrected:
            _upstream_region[fs] = corrected
    return corrected != signed_region


def _s3_get_direct(fs: str, rel: str, *, query: dict | None = None,
                   method: str = "GET", timeout: float) -> bytes:
    """Body bytes of a direct S3 listing/probe request, shared by s3_list_page
    and _s3_has_children so they can't diverge on transport.

    ANONYMOUS remote: a plain urlopen on the unsigned URL — byte-identical to
    the pre-existing code (the resolver is never consulted, redirects are
    followed exactly as before).

    SIGNABLE remote: the presigned URL fetched through the NON-redirect opener
    so a wrong/unset-region 301/307/400 is observable (the default opener would
    chase it to a host the SigV4 signature — which covers Host — doesn't match,
    turning a correctable region hint into an opaque 403). On such a response
    carrying x-amz-bucket-region, adopt the region, re-sign and retry ONCE.

    Propagates HTTPError/URLError/OSError to the caller's error mapping; a
    missing direct URL surfaces as URLError (both callers map it)."""
    cfg = _remote_config(fs.partition(":")[0])
    if _anonymous_s3(cfg):
        url = _s3_request_url(fs, rel, method=method, query=query)
        if url is None:
            raise urllib.error.URLError(f"{fs}: no direct S3 URL")
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    for attempt in (1, 2):
        signed_region = _signing_region(fs, cfg)
        url = _s3_request_url(fs, rel, method=method, query=query)
        if url is None:
            raise urllib.error.URLError(f"{fs}: no direct S3 URL")
        req = urllib.request.Request(url, method=method)
        try:
            with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if attempt == 1 and _adopt_region_on_301(
                    fs, e.code, e.headers, signed_region):
                continue
            raise
    raise urllib.error.URLError(f"{fs}: no direct S3 URL")  # unreachable


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
    """True when `path` is mount-backed by a plain-AWS-S3 remote s3_list_page can
    enumerate directly — anonymous (unsigned) OR credentialed-SHAPED (static keys
    present, or ambient auth opted into via env_auth/profile/shared_credentials).

    A PURE config-shape check: it resolves NO credentials (finding 12). The
    conditions/stat callers gate on this unbudgeted, and a live provider-chain
    walk here (~1-2s on a black-holed IMDS) stalled fs/stat and fs/conditions.
    Actual credential resolution happens inside the budgeted fetch paths
    (s3_list_page / direct_head), which fall back to rc on failure — so a
    credentialed-shaped remote whose creds don't resolve costs one cheap direct
    attempt, not a stalled predicate."""
    m, _ = _mount_for(path)
    if m is None:
        return False
    name = m["remote"].partition(":")[0]
    cfg = _remote_config(name)
    if _anonymous_s3(cfg):
        return True
    if not _s3_signable_shape(cfg):  # plain AWS S3, no custom endpoint
        return False
    assert cfg is not None
    return bool(cfg.get("access_key_id") and cfg.get("secret_access_key")) \
        or s3sign.needs_botocore(cfg)


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
    """One ListObjectsV2 page for a mount-backed directory on a direct-listable
    AWS S3 remote — anonymous (plain unsigned GET) or signable (locally
    presigned GET) — off the kernel mount, no rclone, no boto3.

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
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    if not (_anonymous_s3(cfg) or _s3_signable(name, cfg)):
        raise S3ListError(f"{path}: remote {fs!r} is not direct-listable S3")
    assert cfg is not None
    derived = _s3_bucket_prefix_region(fs, cfg)
    if derived is None:
        raise S3ListError(f"{path}: remote {fs!r} carries no bucket")
    _bucket, store_prefix, _region = derived
    prefix = _s3_listing_prefix(store_prefix, rel)
    params = {"list-type": "2", "delimiter": "/", "prefix": prefix,
              "max-keys": str(max_keys)}
    if continuation:
        params["continuation-token"] = continuation
    # _s3_get_direct builds the unsigned URL (anonymous — byte-identical to the
    # old base?query with the dotted-bucket path-style rule) or the presigned
    # URL (signable — the list params ride through the presigner), and for a
    # signable remote self-corrects the region on a wrong-region 301/307/400.
    try:
        body = _s3_get_direct(fs, rel, query=params, timeout=timeout)
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
    """True when `path` is mount-backed by a Google Cloud Storage remote
    gcs_list_page can enumerate directly — anonymous (unsigned) OR credentialed
    (bearer). Credentialed covers SA-key, oauth-token and ADC-only remotes alike;
    an ADC-only remote carries no config marker, so the shape check is permissive
    — ANY GCS remote qualifies.

    A PURE config-shape check that resolves NO token (finding 12): a live
    ADC/GCE-metadata probe here stalled the unbudgeted conditions/stat callers.
    Actual token resolution happens inside the budgeted fetch paths
    (gcs_list_page / direct_head), which fall back to rc on failure."""
    m, _ = _mount_for(path)
    if m is None:
        return False
    name = m["remote"].partition(":")[0]
    cfg = _remote_config(name)
    return isinstance(cfg, dict) and cfg.get("type") == "google cloud storage"


def _gcs_get_direct(url: str, name: str, cfg: dict | None,
                    timeout: float) -> bytes:
    """Body bytes of a direct GCS listing/probe/metadata GET, shared by
    gcs_list_page, _gcs_head and _gcs_has_children so they can't diverge on
    transport (the GCS analog of _s3_get_direct's role).

    ANONYMOUS remote: a plain urlopen on the unsigned URL — byte-identical to
    the pre-existing code (no Authorization header, resolver never consulted).

    CREDENTIALED remote: the same GET carrying `Authorization: Bearer <token>`.
    On a 401 (stale/rotated token) the cached credential is dropped ONCE and
    re-resolved, then the GET retried — a token that expired early self-heals; a
    second 401 propagates. A 403 is a permission denial WITH a valid token (a
    per-object/prefix IAM policy), so it propagates immediately — re-resolving
    the token wouldn't help and would churn the credential per denied probe. The
    token value is never logged and never placed in the URL.

    Propagates HTTPError/URLError/OSError to the caller's error mapping (each
    keeps its own DirectListError / DirectProbeError wrapping and 404 handling);
    a missing token surfaces as URLError, which both callers already map."""
    if _gcs_anonymous(cfg or {}):
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    for attempt in (1, 2):
        tok = _gcs_bearer_token(name, cfg)
        if tok is None:
            raise urllib.error.URLError(f"{name}: no GCS bearer token")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {tok.access_token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            # 401 (bad/expired token) self-heals once; 403 (permission denial
            # with a valid token) and every other status propagate unchanged.
            if attempt == 1 and e.code == 401:
                _invalidate_gcs_creds(name)  # force a freshly-resolved token
                continue
            raise
    raise urllib.error.URLError(f"{name}: GCS bearer retry exhausted")


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
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    # Anonymous FIRST: an anonymous remote never resolves a token, never sends
    # an Authorization header — its request is the exact unsigned one today's
    # code produces.
    if not (_gcs_anonymous(cfg or {}) or _gcs_credentialed(name, cfg)):
        raise DirectListError(
            f"{path}: remote {fs!r} is not direct-listable GCS")
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
    # _gcs_get_direct fetches unsigned (anonymous — byte-identical) or with a
    # bearer header (credentialed), self-healing one stale-token 401/403.
    try:
        body = _gcs_get_direct(url, name, cfg, timeout)
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


def direct_list_anonymous(path: str) -> bool:
    """True when `path` is mount-backed by an ANONYMOUS direct-listable remote
    (anonymous plain AWS S3 or anonymous GCS), as opposed to a credentialed-
    SHAPED one. A PURE config-shape check that resolves NO credentials/token
    (finding 12), mirroring direct_list_capable.

    An anonymous remote carries no credentials that can fail to resolve, so its
    direct pager never raises DirectListError for a missing/expired credential —
    callers that can't fall back to rc (the mount-root watch) use this to keep
    anonymous behavior byte-identical while letting a credentialed-shaped remote
    whose creds don't resolve fall through to rc."""
    m, _ = _mount_for(path)
    if m is None:
        return False
    name = m["remote"].partition(":")[0]
    cfg = _remote_config(name)
    return _anonymous_s3(cfg) or _gcs_anonymous(cfg or {})


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


# ------------------------------------------------- direct point probes (stat)
# The stat analog of the direct-listing path above. operations/stat has no S3
# point lookup — rclone resolves a negative file probe or a directory probe by
# an UNBOUNDED ListObjectsV2 of the whole parent prefix, so on a flat
# world-scale prefix (source.coop/earthgenome/sentinel2-temporal-mosaics) every
# probe burns the full rc timeout, and an existing directory even 404s after the
# timeout expires. But S3/GCS expose real point lookups: HeadObject answers
# exists/size/mtime, and a max-keys=1 list answers dir-ness — each in ~one round
# trip. For the anonymous backends we already list unsigned, probe those
# directly and never touch the rc path. Path-style S3 URLs come from the shared
# _s3_base_url (its dotted-bucket rule; see there) via _public_object_url.
_HEAD_TIMEOUT_S = 5.0
# objects.get (metadata) is the GCS analog of S3 HeadObject; the list endpoint
# (_GCS_LIST_URL) answers dir-ness. Both carry the bucket in the path (no region).
_GCS_OBJ_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{key}"

# (exists, size, mtime) for one object; mtime is RFC3339 (rc_modtime_epoch parses
# it) so a direct head and an rc item feed rc_modtime_epoch identically.
DirectHead = collections.namedtuple("DirectHead", ["exists", "size", "mtime"])


class DirectProbeError(Exception):
    """A direct (unsigned) S3/GCS point probe could not decide — an HTTP status
    other than 404 (403 needs auth, 301 wrong region), a network error, or an
    unparseable body. Distinct from a 404, which is a TRUSTWORTHY "the object is
    not there". The caller falls back to operations/stat; kept separate from
    DirectListError so the two fallback ladders read independently."""


def _http_date_epoch(value: str | None) -> str | None:
    """An HTTP-date Last-Modified header ("Wed, 21 Oct 2015 07:28:00 GMT") ->
    RFC3339, so a direct S3 head yields the same ModTime shape rc_modtime_epoch
    already parses off an rc item. None when absent/unparseable."""
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return None


def direct_head(path: str, *, timeout: float = _HEAD_TIMEOUT_S) -> DirectHead:
    """Point existence+metadata probe for a mount-backed FILE via an unsigned
    S3 HeadObject / GCS objects.get — the fast alternative to operations/stat's
    parent-prefix list. Returns DirectHead(exists, size, mtime): exists=False on
    a definitive 404 (the object is not there). The mountpoint itself (rel ".")
    is never an object -> exists=False. Raises DirectProbeError on any
    indeterminate outcome (non-404 HTTP, network, unparseable) so the caller can
    fall back to rc, and when `path` is under no direct-probe-capable backend."""
    if s3_direct_capable(path):
        return _s3_head(path, timeout)
    if gcs_direct_capable(path):
        return _gcs_head(path, timeout)
    raise DirectProbeError(f"{path}: no direct-probe backend")


def direct_is_dir(path: str, *, timeout: float = _HEAD_TIMEOUT_S) -> bool:
    """Whether any key lives under `path`'s prefix — the point dir-ness probe, a
    max-keys=1 S3 ListObjectsV2 / GCS objects.list. True even when only the
    zero-byte directory-marker object exists (that marker IS the directory).
    Raises DirectProbeError on any indeterminate outcome (so the caller falls
    back to rc) and when the backend is not direct-probe-capable."""
    if s3_direct_capable(path):
        return _s3_has_children(path, timeout)
    if gcs_direct_capable(path):
        return _gcs_has_children(path, timeout)
    raise DirectProbeError(f"{path}: no direct-probe backend")


def _direct_stat_item(path: str, *, deadline: float):
    """The _stat_item dict|None outcome via direct probes: a HeadObject decides
    FILE, else a max-keys=1 list decides DIR, else confirmed missing (None). Any
    probe raising DirectProbeError propagates so _stat_item falls back to rc.
    Two round trips at worst (dir/miss); one for the common file hit.

    Both probes share the caller's single `deadline` (monotonic seconds): the
    head gets the whole remaining budget, the dir list only what the head left,
    so one logical stat never spends up to 2x the timeout. If the head consumed
    the budget the dir probe can't fit -> raise so _stat_item treats it as
    indeterminate (and its own rc fallback is bounded by the same deadline)."""
    remaining = deadline - time.monotonic()
    if remaining < _DIRECT_PROBE_MIN_S:
        raise DirectProbeError(f"{path}: budget spent before head probe")
    head = direct_head(path, timeout=remaining)
    if head.exists:
        return {"IsDir": False, "Size": head.size,
                "MtimeEpoch": rc_modtime_epoch(head.mtime)}
    if deadline - time.monotonic() < _DIRECT_PROBE_MIN_S:
        raise DirectProbeError(f"{path}: budget spent before dir probe")
    if direct_is_dir(path, timeout=deadline - time.monotonic()):
        # S3/GCS have no real directories; a present prefix (or marker) is a dir.
        return {"IsDir": True, "Size": None, "MtimeEpoch": None}
    return None  # no object, no children -> a trustworthy miss


def _s3_head(path: str, timeout: float) -> DirectHead:
    m, rel = _mount_for(path)
    if rel == ".":
        return DirectHead(False, None, None)  # the mountpoint is not an object
    fs = m["remote"]
    # Anonymous: plain urlopen on the unsigned URL, byte-identical to before.
    # Signable: presigned HEAD (signed explicitly — a presigned GET rejects a
    # HEAD) through the non-redirect opener, self-correcting the region once on
    # a wrong-region 301/307/400 so a probe doesn't wedge on an unset/wrong region.
    cfg = _remote_config(fs.partition(":")[0])
    anonymous = _anonymous_s3(cfg)
    for attempt in (1, 2):
        signed_region = None if anonymous else _signing_region(fs, cfg)
        url = _s3_request_url(fs, rel, method="HEAD")
        if url is None:  # neither anonymous nor signable — caller falls back
            raise DirectProbeError(f"{path}: no direct S3 object URL")
        req = urllib.request.Request(url, method="HEAD")
        opener = (urllib.request.urlopen if anonymous
                  else _NO_REDIRECT_OPENER.open)
        try:
            with opener(req, timeout=timeout) as resp:
                size = resp.headers.get("Content-Length")
                return DirectHead(
                    True,
                    int(size) if size and size.isdigit() else None,
                    _http_date_epoch(resp.headers.get("Last-Modified")))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return DirectHead(False, None, None)  # trustworthy negative
            if (not anonymous and attempt == 1
                    and _adopt_region_on_301(
                        fs, e.code, e.headers, signed_region)):
                continue
            raise DirectProbeError(f"S3 head {path}: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise DirectProbeError(f"S3 head {path}: {e}") from e
    raise DirectProbeError(f"S3 head {path}: region-correction retry exhausted")


def _gcs_head(path: str, timeout: float) -> DirectHead:
    m, rel = _mount_for(path)
    if rel == ".":
        return DirectHead(False, None, None)  # the mountpoint is not an object
    fs = m["remote"]
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    derived = _gcs_bucket_prefix(fs)
    if derived is None:
        raise DirectProbeError(f"{path}: remote {fs!r} carries no bucket")
    bucket, store_prefix = derived
    key = (store_prefix + "/" if store_prefix else "") + rel
    url = _GCS_OBJ_URL.format(bucket=bucket,
                              key=urllib.parse.quote(key, safe=""))
    # Unsigned for anonymous (byte-identical), bearer-authorized for
    # credentialed — the same transport the pager uses via _gcs_get_direct.
    try:
        body = _gcs_get_direct(url, name, cfg, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return DirectHead(False, None, None)  # trustworthy negative
        raise DirectProbeError(f"GCS head {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DirectProbeError(f"GCS head {path}: {e}") from e
    try:
        doc = json.loads(body)
    except (ValueError, TypeError) as e:
        raise DirectProbeError(f"GCS head {path}: unparseable JSON") from e
    size = doc.get("size")
    return DirectHead(
        True,
        int(size) if isinstance(size, str) and size.isdigit() else None,
        doc.get("updated"))  # RFC3339 already


def _s3_has_children(path: str, timeout: float) -> bool:
    m, rel = _mount_for(path)
    fs = m["remote"]
    derived = _s3_bucket_prefix_region(
        fs, _remote_config(fs.partition(":")[0]) or {})
    if derived is None:
        raise DirectProbeError(f"{path}: remote {fs!r} carries no bucket")
    _bucket, store_prefix, _region = derived
    prefix = _s3_listing_prefix(store_prefix, rel)
    # NO delimiter and max-keys=1: cheapest "does anything live here" — one key
    # (the marker included) proves the directory. delimiter would only add
    # CommonPrefixes work we don't need for a boolean.
    params = {"list-type": "2", "prefix": prefix, "max-keys": "1"}
    # Unsigned for anonymous (byte-identical to the old base?query), presigned
    # for signable (with region self-correction) — the same transport the pager
    # uses via _s3_get_direct.
    try:
        body = _s3_get_direct(fs, rel, query=params, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise DirectProbeError(f"S3 list {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DirectProbeError(f"S3 list {path}: {e}") from e
    try:
        root_el = ElementTree.fromstring(body)
    except ElementTree.ParseError as e:
        raise DirectProbeError(f"S3 list {path}: unparseable XML") from e
    return root_el.find(f"{_S3_XMLNS}Contents") is not None


def _gcs_has_children(path: str, timeout: float) -> bool:
    m, rel = _mount_for(path)
    fs = m["remote"]
    name = fs.partition(":")[0]
    cfg = _remote_config(name)
    derived = _gcs_bucket_prefix(fs)
    if derived is None:
        raise DirectProbeError(f"{path}: remote {fs!r} carries no bucket")
    bucket, store_prefix = derived
    prefix = _s3_listing_prefix(store_prefix, rel)  # backend-agnostic join
    params = {"prefix": prefix, "maxResults": "1"}
    query = urllib.parse.urlencode(params)
    url = f"{_GCS_LIST_URL.format(bucket=bucket)}?{query}"
    # Unsigned for anonymous, bearer-authorized for credentialed (via
    # _gcs_get_direct) — the same transport the pager and head probe use.
    try:
        body = _gcs_get_direct(url, name, cfg, timeout)
    except urllib.error.HTTPError as e:
        raise DirectProbeError(f"GCS list {path}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DirectProbeError(f"GCS list {path}: {e}") from e
    try:
        doc = json.loads(body)
    except (ValueError, TypeError) as e:
        raise DirectProbeError(f"GCS list {path}: unparseable JSON") from e
    return bool(doc.get("items") or doc.get("prefixes"))


def upstream_url_for(path: str) -> str | None:
    """Direct store URL for a mount-backed file, or None when the backend has
    no reachable one (the caller then stays on the serve). Never raises —
    this sits on the raw-proxy hot path."""
    try:
        return _upstream_url_for(path)
    except Exception:
        logger.warning("upstream url for %r failed", path, exc_info=True)
        return None


def bearer_upstream_for(path: str) -> tuple[str, dict] | None:
    """(plain object URL, {"Authorization": "Bearer <token>"}) for a mount-backed
    file on a credentialed GCS remote the server should proxy (no URL may carry
    the token). None for anonymous GCS (reachable by public URL) and every
    non-GCS backend. Anonymous is checked FIRST, so it never consults the token
    resolver.

    Signability is only a TIE-BREAKER, and only when the remote is NOT already
    on the bearer path. We serve the bearer token when EITHER (a) _upstream_url_for
    has PINNED mode="bearer" (a gsign validation reject, or a token-only remote),
    OR (b) the fs is in a gsign RETRY window (_gcs_bearer_fallback — validation
    contended / neg-cached / signer momentarily unresolvable): in both cases the
    signed-URL path can't serve THIS request, so the token is the only working
    fast path (finding 1). Only when neither holds do we defer to _gcs_signable
    (that remote 307s via _upstream_url_for instead) — without this the retry
    window would dead-end at the slow serve despite a resolvable token. The token
    value is never logged and never placed in a URL. Never raises — this sits on
    the raw-proxy hot path alongside upstream_url_for."""
    try:
        m, rel = _mount_for(path)
        if m is None:
            return None
        fs = m["remote"]
        name = fs.partition(":")[0]
        cfg = _remote_config(name)
        if _gcs_anonymous(cfg or {}):
            return None
        now = time.monotonic()
        with _upstream_lock:
            on_bearer_path = (_upstream_mode.get(fs) == "bearer"
                              or _gcs_bearer_fallback.get(fs, 0.0) > now)
        if not on_bearer_path and _gcs_signable(name, cfg):
            return None  # 307-signable and not on the bearer path -> not ours
        tok = _gcs_bearer_token(name, cfg)
        if tok is None:  # not credentialed / no [cloud-auth] -> fall to serve
            return None
        url = _gcs_object_url(fs, rel)
        if url is None:
            return None
        return url, {"Authorization": f"Bearer {tok.access_token}"}
    except Exception:
        logger.warning("bearer upstream for %r failed", path, exc_info=True)
        return None


def invalidate_gcs_token(path: str) -> None:
    """Drop the cached bearer token + credential object for `path`'s remote so
    the next bearer_upstream_for re-resolves from config. Called by the raw read
    proxy when a bearer GET comes back 401/403 (the token went stale/rotated),
    so a single retry can self-heal. Never raises — sits on the raw hot path."""
    try:
        m, _ = _mount_for(path)
        if m is None:
            return
        _invalidate_gcs_creds(m["remote"].partition(":")[0])
    except Exception:
        logger.warning("invalidate gcs token for %r failed", path, exc_info=True)


def _sign_validation_status(url: str) -> tuple[int, str | None]:
    """Sign mode's one validation probe: a Range: bytes=0-0 GET against a
    presigned URL, redirects NOT followed (see _NoRedirect). Returns (HTTP
    status, x-amz-bucket-region header) — (0, None) on a network error. Only
    the status/region is surfaced; the presigned URL is never logged."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"Range": "bytes=0-0"})
    try:
        with _NO_REDIRECT_OPENER.open(
                req, timeout=_SIGN_VALIDATE_TIMEOUT_S) as resp:
            return resp.status, resp.headers.get("x-amz-bucket-region")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("x-amz-bucket-region")
    except (urllib.error.URLError, OSError):
        return 0, None


def _validate_and_sign(fs: str, rel: str, cfg: dict) -> tuple[str | None, str]:
    """One-time sign-mode validation for a remote: presign a ranged GET for
    `rel` and issue it once, returning (url, verdict):
      - (url, "ok")            S3 accepted the signature (200/206/404/416 — a
                               404 means accepted, object merely absent);
      - (None, "reject")       S3 definitively rejected it (403, or an
                               uncorrectable 301/307/400) — sign mode can't
                               serve this remote; caller settles on publiclink;
      - (None, "inconclusive") network error / 5xx — transient; the caller must
                               NOT pin a mode so sign is re-attempted later.
    The trial region is passed via region_override and NEVER published to
    _upstream_region until the signature is accepted — so a losing racer can't
    erase a winner's adopted region. Self-corrects the region ONCE on a
    301/307/400 carrying x-amz-bucket-region and re-signs (307 is S3's
    newly-created-bucket redirect); on success writes _upstream_region[fs]
    exactly once."""
    derived = _s3_bucket_prefix_region(fs, cfg)
    if derived is None:
        return None, "reject"
    with _upstream_lock:
        region = _upstream_region.get(fs, derived[2])  # a prior correction wins
    verdict = "inconclusive"
    for attempt in (1, 2):
        url = _s3_request_url(fs, rel, method="GET", region_override=region)
        if url is None:
            return None, "reject"
        status, corrected = _sign_validation_status(url)
        if status in (200, 206, 404, 416):
            with _upstream_lock:
                _upstream_region[fs] = region  # publish once, on success only
            return url, "ok"
        if (attempt == 1 and status in (301, 307, 400)
                and corrected and corrected != region):
            region = corrected
            continue
        # 403 / uncorrectable 301/307/400 (< 500) is a definite reject; a network
        # error (status 0) or 5xx is inconclusive — don't let a transient blip
        # permanently pin the remote to the slow link path (finding 7).
        verdict = "reject" if 0 < status < 500 else "inconclusive"
        break
    return None, verdict


def _sign_single_flight(fs: str, mode_name: str, mint_fn, validate_fn,
                        reject_disp: str) -> tuple[str | None, str]:
    """The per-fs single-flight state machine shared by S3 sign mode and GCS
    gsign mode, so N concurrent first reads issue ONE validation GET instead of
    N. Parameterized on:
      - mode_name    the mode string pinned on success ("sign" / "gsign");
      - mint_fn()    -> url|None, the per-object URL when the mode is
                     active/just-validated (a presigned S3 URL / a signed GCS
                     URL); None means creds/signer rotated away mid-flight;
      - validate_fn() -> (url, verdict) with verdict "ok"/"reject"/
                     "inconclusive" — the one-time validation probe;
      - reject_disp  the disposition returned on a definite reject ("link" for
                     S3's publiclink ladder, "bearer" for GCS's proxy).
    Returns (url, disposition):
      - (url, mode_name)  mode active/just-validated -> use this URL (url may be
                     None if creds/signer rotated away mid-flight — the caller
                     demotes and falls through, finding 4);
      - (None, reject_disp)  validated as a definite reject -> caller settles on
                     the fallback path; safe to cache that mode;
      - (None, "retry")  validation inconclusive OR negative-cached OR ANOTHER
                     THREAD holds the validation lock -> serve THIS request via
                     the fallback but DON'T pin a mode, so the mode is
                     re-attempted once the window lapses (findings 5 and 7)."""
    now = time.monotonic()
    with _upstream_lock:
        if _upstream_mode.get(fs) == mode_name:
            active = True
        else:
            active = False
            if _sign_neg_cache.get(fs, 0.0) > now:
                return None, "retry"  # recently failed -> skip validation window
        lock = _validation_locks.setdefault(fs, threading.Lock())
    if active:
        return mint_fn(), mode_name
    if not lock.acquire(blocking=False):
        return None, "retry"  # another thread is validating; don't pile on
    try:
        with _upstream_lock:
            if _upstream_mode.get(fs) == mode_name:
                won = False
            elif _sign_neg_cache.get(fs, 0.0) > time.monotonic():
                return None, "retry"
            else:
                won = True
        if not won:  # a racer finished validating while we took the lock
            return mint_fn(), mode_name
        url, verdict = validate_fn()
        if verdict == "ok":
            with _upstream_lock:
                _upstream_mode[fs] = mode_name
                _sign_neg_cache.pop(fs, None)
            return url, mode_name
        # Failed: negative-cache so we don't re-run the blocking validation GET
        # on every request while no fallback is committed; a definite reject
        # settles on the fallback mode, an inconclusive one leaves it open.
        with _upstream_lock:
            _sign_neg_cache[fs] = time.monotonic() + _SIGN_NEG_TTL_S
        return None, (reject_disp if verdict == "reject" else "retry")
    finally:
        lock.release()


def _sign_mode_url(fs: str, rel: str, cfg: dict) -> tuple[str | None, str]:
    """S3 sign mode via the shared single-flight machine: mint a presigned URL
    when active/won, validate once otherwise, and fall to the publiclink ladder
    ("link") on a definite reject (findings 4, 5, 7 — see _sign_single_flight)."""
    return _sign_single_flight(
        fs, "sign",
        mint_fn=lambda: _s3_request_url(fs, rel, method="GET"),
        validate_fn=lambda: _validate_and_sign(fs, rel, cfg),
        reject_disp="link")


def _gcs_validate_and_sign(fs: str, rel: str,
                           signer) -> tuple[str | None, str]:
    """One-time gsign-mode validation for a GCS remote — the slim, region-less
    GCS analog of _validate_and_sign (there is no x-amz-bucket-region machinery,
    so a single attempt suffices). Signs a GET for `rel` and probes it once via
    _sign_validation_status (a Range: bytes=0-0 GET), returning (url, verdict):
      - (url, "ok")            GCS accepted the signature (200/206/404/416);
      - (None, "reject")       definitely rejected (403/401 or other <500) —
                               gsign can't serve this remote;
      - (None, "inconclusive") network error / 5xx — transient; caller must NOT
                               pin a mode so gsign is re-attempted later."""
    url = _gcs_object_url(fs, rel)
    if url is None:
        return None, "reject"
    signed = gcssign.sign_url(url, method="GET", signer=signer.signer,
                              sa_email=signer.sa_email, expires=_SIGN_EXPIRY_S)
    status, _region = _sign_validation_status(signed)
    if status in (200, 206, 404, 416):
        return signed, "ok"
    return None, ("reject" if 0 < status < 500 else "inconclusive")


def _gcs_sign_mode_url(fs: str, rel: str,
                       cfg: dict) -> tuple[str | None, str]:
    """GCS gsign mode via the shared single-flight machine. Only called for a
    SIGNABLE-SHAPED remote (an SA key is configured); the signer resolution is
    hoisted BEFORE the machine. When the signer momentarily fails to resolve (SA
    file transiently unreadable, [cloud-auth] absent this window) we return
    "retry", NOT "bearer": a signable-shaped remote must serve bearer for THIS
    request WITHOUT permanently pinning bearer, so gsign is retried after the
    cache TTL (finding 2). Only a genuine validation reject pins bearer.
    Otherwise mint a V4-signed URL when active/won, validate once, and fall to
    the bearer proxy on a definite reject (see _sign_single_flight)."""
    signer = _gcs_signer(fs.partition(":")[0], cfg)
    if signer is None:  # signer momentarily unresolvable -> transient, retry
        return None, "retry"
    return _sign_single_flight(
        fs, "gsign",
        mint_fn=lambda: _gcs_signed_url(fs, rel),
        validate_fn=lambda: _gcs_validate_and_sign(fs, rel, signer),
        reject_disp="bearer")


def _demote_gsign(fs: str) -> None:
    """Un-pin gsign mode for `fs` (idempotent), so the next request re-derives
    the mode from scratch — gsign again once the signer returns, bearer meanwhile
    if a token resolves. The demotion is NON-permanent (findings 4, 5). Shared by
    both un-pin sites so they can't drift (C5)."""
    with _upstream_lock:
        if _upstream_mode.get(fs) == "gsign":
            _upstream_mode.pop(fs, None)


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
    name = fs.partition(":")[0]
    # cache_mode stays True unless a sign validation was INCONCLUSIVE (transient)
    # — then we serve this request via publiclink but don't pin a mode, so sign
    # is retried after the negative-cache window (findings 5, 7).
    cache_mode = True
    if mode == "sign":
        # In-process signing is microseconds and creds rotate, so sign mode
        # mints per object and never caches a link.
        signed = _s3_request_url(fs, rel, method="GET")
        if signed is not None:
            return signed
        # None means creds stopped resolving (a rotated-away key). Demote to the
        # link ladder for this and future requests rather than returning None
        # per-request forever (finding 4); a remote change (invalidation) or a
        # cred refresh re-derives the mode from scratch. (No live 403 detection
        # of an expired-but-present token — out of scope.)
        with _upstream_lock:
            if _upstream_mode.get(fs) == "sign":
                _upstream_mode[fs] = "link"
        mode = "link"
    if mode == "gsign":
        # SA-key GCS: mint a V4 signed URL per object (in-process signing is
        # microseconds and creds rotate), never caching a link — same rationale
        # as S3 sign mode.
        signed = _gcs_signed_url(fs, rel)
        if signed is not None:
            return signed
        # Signer unavailable (rotated key, or a transient cached-None window):
        # UN-PIN the mode rather than pin "bearer" permanently, so the next
        # request re-derives — bearer for now if a token resolves, gsign again
        # once the signer comes back (the demotion is non-permanent, finding 5).
        _demote_gsign(fs)
        return None
    if mode == "bearer":
        # Token-only GCS: no URL may carry the token, so there is nothing to
        # 307 to — the server proxies via bearer_upstream_for. Returning None
        # here routes it there.
        return None
    url = None
    if mode is None:
        cfg = _remote_config(name)
        if _cannot_presign(cfg):
            # Anonymous S3 or GCS can never presign — don't burn an rc call per
            # remote learning that from publiclink's "unsupported signer type"
            # error. CHECKED FIRST: an anonymous remote never resolves creds,
            # never signs, never issues the validation GET; its mode is "public".
            mode = "public"
        elif _s3_signable(name, cfg):
            # Credentialed plain-AWS S3: presign locally instead of a publiclink
            # rc call per object. Single-flight validation the first time; on a
            # definite reject fall to publiclink ("link"), on a transient
            # failure fall to publiclink for this request but keep retrying sign.
            signed, disp = _sign_mode_url(fs, rel, cfg)
            if disp == "sign" and signed is not None:
                return signed
            if disp == "sign":  # active but creds vanished -> demote (finding 4)
                with _upstream_lock:
                    if _upstream_mode.get(fs) == "sign":
                        _upstream_mode[fs] = "link"
                mode = "link"
            elif disp == "retry":
                cache_mode = False
            # disp == "link": mode stays None; publiclink below caches "link"
        elif gcssign._is_sa_configured(cfg):
            # SA-key-SHAPED GCS: V4-sign locally instead of a publiclink rc call
            # (which ALWAYS fails for GCS — rclone reports PublicLink: False).
            # The tier is decided on config SHAPE, not a live signer resolution,
            # so a momentarily-unresolvable signer is treated as transient rather
            # than pinning bearer permanently (finding 2). Single-flight
            # validation the first time.
            signed, disp = _gcs_sign_mode_url(fs, rel, cfg)
            if disp == "gsign" and signed is not None:
                return signed
            if disp == "gsign":  # won validation but signer vanished mid-flight
                _demote_gsign(fs)  # un-pin, re-derive next time
                return None
            elif disp == "retry":
                # Contended / neg-cached / signer momentarily unresolvable: serve
                # bearer for THIS request but DON'T pin bearer, so gsign is
                # retried after the window. Mark the fs so bearer_upstream_for
                # serves the token now even though the SA key still parses —
                # otherwise its tie-breaker would dead-end at the serve (finding
                # 1). The mark expires with the neg-cache window.
                cache_mode = False
                mode = "bearer"
                with _upstream_lock:
                    _gcs_bearer_fallback[fs] = time.monotonic() + _SIGN_NEG_TTL_S
            else:  # "bearer": definite validation reject -> pin bearer
                mode = "bearer"
        elif gcssign._is_gcs_signable_shape(cfg):
            # Token-only-SHAPED GCS (no SA key to sign with): bearer proxy, safe
            # to pin — no SA key will ever let it sign locally. Skip the
            # publiclink rc call; it always fails for GCS (PublicLink: False).
            mode = "bearer"
    if mode == "bearer":
        with _upstream_lock:
            if cache_mode:
                _upstream_mode[fs] = "bearer"
        return None  # no URL may carry the token; server uses bearer_upstream_for
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
    # _link_ttl reads config (takes _upstream_lock), so resolve it first.
    ttl = (_link_ttl(fs) if mode == "link" else 3600.0) if url is not None else 0.0
    with _upstream_lock:
        if cache_mode:
            _upstream_mode[fs] = mode
        if url is not None:
            _store_upstream_link((fs, rel), url, now + ttl, now)
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


# After mount/mount, the kernel NFS mount is normally live the instant rcd's
# `mount` command returns — but a flap-prone loopback NFS mount can attach late
# or not at all. Poll ismount briefly so a mount that genuinely took confirms
# fast, while one that never attached is caught here instead of reported as
# success (see attach_mount's verify below).
_MOUNT_ATTACH_DEADLINE_S = 3.0
_MOUNT_ATTACH_POLL_S = 0.1


def _await_ismount(mp: str, deadline: float = _MOUNT_ATTACH_DEADLINE_S) -> bool:
    """True once os.path.ismount(mp) holds within `deadline` seconds, else False."""
    end = time.monotonic() + deadline
    while True:
        if os.path.ismount(mp):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(_MOUNT_ATTACH_POLL_S)


def attach_mount(m: dict) -> str | None:
    """Mount via rcd; returns an error string or None."""
    mp = mountpoint(m)
    # Create the mounts root (with its Spotlight-exclusion marker) before the
    # per-mount mountpoint, so the marker is in place the moment the mount goes
    # live and Spotlight never gets a chance to scan it.
    ensure_mounts_dir()
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
    # mount/mount returns success once rcd's NFS server is up and it has
    # invoked the macOS `mount` command — but on a flap-prone loopback NFS
    # mount the kernel attach can silently fail (or drop within seconds),
    # leaving rcd's serve alive while os.path.ismount stays False: the exact
    # "stale" split-brain reconnect_mount exists to heal. Without this check
    # attach_mount returned None (success) over a mount that never took, so a
    # /reconnect reported OK while the folder stayed empty. Confirm the kernel
    # mount actually attached before claiming success.
    if not _await_ismount(mp):
        return (f"mount did not attach at {mp} — rcd serves the remote but the "
                f"kernel NFS mount is absent; retry reconnect")
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
        # /quit is token-gated (D122); the state file carries the daemon's
        # token, so forward it or the daemon 403s, keeps the mount files open,
        # and the EBUSY retry never releases them. Token-less state = a daemon
        # predating the token, which accepts a plain /quit.
        tok = state.get("token")
        path = f"/quit?t={tok}" if tok else "/quit"
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{state['port']}{path}", timeout=3).read()
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


def mount_state(m: dict, rcd_mounts: set, timeout: float = PROBE_TIMEOUT,
                *, probe_io: bool = True) -> str:
    """Health of one mount: "mounted" | "stale" | "disconnected" | "unmounted".

    "mounted" requires both that a live rcd serves the mountpoint AND (when
    `probe_io`) that the filesystem actually answers a listdir. Pass
    probe_io=False to SKIP that os.listdir — on an S3-backed mount a kernel
    READDIR of the root is itself a wedge trigger (a slow syscall the timeout
    abandons but cannot cancel), so a caller polling on a timer (the health
    monitor) classifies from os.path.ismount + rcd membership ALONE and never
    touches the mount. The failures this catches are the two ways the kernel
    mount table and rcd's mount/listmounts disagree:

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
                if probe_io:
                    os.listdir(mp)  # the actual I/O health check
                out["state"] = "mounted"
        except OSError:
            out["state"] = "disconnected"

    t = threading.Thread(target=probe, daemon=True, name=f"mount-probe-{m['name']}")
    t.start()
    t.join(timeout)
    return out.get("state", "disconnected")  # no answer in time == wedged


# Sentinel: distinguishes "caller already ran the credential probe, here is its
# tri-state result" from "no result supplied, run the probe if the credentials
# branch is reached". A plain None default couldn't tell them apart.
_UNSET = object()


def mount_restart_reason(m: dict, rcd_mounts: set | None = None,
                         state: str | None = None, cred_status=_UNSET) -> str | None:
    """Why restarting rclone would help this mount, or None. Surfaced on
    mount_view so the UI can prompt (both reasons route to the SAME global
    Restart button):

      "params"      — the mount is live but its RUNNING options differ from what
                      the record now wants, so a restart is needed to apply them.
                      Conservative subset: read_only is the one mount param the
                      UI can change, and mounted_read_only records what was
                      actually baked into the live VFS (rcd never echoes vfsOpt
                      back — same signal attach_mount's adopt branch remounts on).
                      A MISSING mounted_read_only (a legacy record adopted via
                      listmounts that never went through attach_mount) is
                      "unknown, assume no drift" — never a false prompt.
                      Broader vfsOpt/mountOpt diffing is deliberately deferred.
      "credentials" — a disconnected/stale mount on an env_auth remote whose
                      credentials probe POSITIVELY VALID again: the long-lived
                      daemon still holds the pre-refresh keys, so Reconnect (and
                      even a server restart) can't help — only replacing the
                      daemon re-reads the refreshed creds (see restart_rcd).
                      An inconclusive probe (timeout/network/AccessDenied) is NOT
                      treated as valid, so a transient failure can't spam a false
                      restart prompt.

    `cred_status` lets a caller that already ran the probe (e.g. get_mounts,
    which threads it off the serial view-building path) pass the tri-state
    result in so we don't pay a second `rclone lsd`; left unset, the credentials
    branch runs the probe itself. `mounted_paths()` is only fetched when `state`
    isn't supplied — the error path passes state and never needs the rc call."""
    if state is None:
        if rcd_mounts is None:
            rcd_mounts = mounted_paths()
        state = mount_state(m, rcd_mounts)
    if state == "mounted":
        baked = m.get("mounted_read_only")
        # Only a KNOWN-and-differing baked flag is drift; a missing key is an
        # adopted legacy mount whose live vfsOpt we can't compare — not a prompt.
        if baked is not None and bool(m.get("read_only")) != bool(baked):
            return "params"
        return None
    if state in ("disconnected", "stale"):
        if cred_status is _UNSET:
            cred_status = _mount_credential_status(m)
        # Only a POSITIVE "valid" means the daemon is holding stale-but-now-good
        # keys; "bad"/"inconclusive"/"n/a" are Reconnect's or refresh's job.
        return "credentials" if cred_status == "valid" else None
    return None


def mount_view(m: dict, rcd_mounts: set | None = None, state: str | None = None,
               cred_status=_UNSET) -> dict:
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
        # Shipped-with-the-app mount (see ensure_learn_mount); the UI can
        # treat it differently from a user-created mount (e.g. hide delete).
        "builtin": bool(m.get("builtin")),
        # Why a Restart rclone would help (params drift / re-authed creds), or
        # None. Reuses the state just computed AND (on the get_mounts bulk path)
        # a cred_status probed off the serial path, so building a view never
        # blocks on a per-mount `rclone lsd`.
        "restart_reason": mount_restart_reason(m, listed, state, cred_status),
    }


# ---------------------------------------------------------- automount/startup


LEARN_MOUNT_NAME = "learn"


def learn_zip_path() -> str | None:
    """Path to the bundled learn.zip, or None outside the packaged app.

    FUSED_RENDER_LEARN_ZIP overrides for dev/testing (a dev checkout has the
    loose learn/ dir, not a zip — build_dmg.sh only creates the zip at DMG
    build time). Packaged (same sys.frozen check as rclone_bin) it lives at
    Contents/Resources/learn.zip (build_dmg.sh step 4e). Existence-checked
    either way so a stale env var or a hand-pruned bundle yields None, not a
    mount record pointing at nothing."""
    override = os.environ.get("FUSED_RENDER_LEARN_ZIP")
    if override:
        return override if os.path.isfile(override) else None
    if getattr(sys, "frozen", None) == "macosx_app":
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        bundled = os.path.join(contents, "Resources", "learn.zip")
        if os.path.isfile(bundled):
            return bundled
    return None


def ensure_learn_mount() -> None:
    """Upsert the builtin "learn" mount record: rclone's archive backend
    (v1.74) mounts the bundled learn.zip read-only through the same mounts
    surface as any remote (D123).

    Builtin records carry `"builtin": "learn"` so they're distinguishable
    from a user-created mount that happens to be named "learn" — that user
    mount is never touched. The remote embeds the zip's absolute path inside
    the app bundle, which changes across versions/relocations, so an existing
    record's remote is refreshed every startup; with no zip (dev checkout,
    downgrade) the builtin record is removed so it can't linger as a broken
    mount in the UI. read_only_user pins the flag: the archive backend is
    inherently read-only, and pinning keeps attach-time detection from ever
    reconsidering it — mount, serve, and kernel all get read-only baked in
    via the existing read_only plumbing.

    Never raises — this runs on the automount path and a storage failure
    must not break the user's own mounts.

    BUGBOT (2026-07-21): rcd survives server restarts (module docstring), so
    an already-live mount at the learn mountpoint is never naturally
    refreshed. Two staleness paths that opened:
      - the bundle relocates (remote string changes) — a live mount still
        serves the OLD fs, and attach_mount would then reject the new record
        outright (fs mismatch — see attach_mount's already-mounted branch);
      - an in-place app upgrade overwrites learn.zip at the SAME path — the
        remote string never changes, so nothing signalled a refresh was
        needed at all, and the live VFS + on-disk cache kept serving last
        version's bytes indefinitely.
    Fixed the same way for both: whenever a live rcd mount already sits at
    the learn mountpoint, force-detach it here (best-effort) so run_automount's
    normal per-mount loop right after this call does a fresh attach_mount —
    unconditionally, not just when the remote string happens to differ, since
    content can change under an unchanged path. Cheap: this is a small local
    archive, not a network remote.

    BUGBOT (2026-07-21): the force-detach talks to rcd (mounted_paths,
    detach_mount's busy-unmount retry, _stop_serve_for) and can block for the
    full rc timeout window. That must never happen while _store_lock is
    held — every mount create/delete/update takes the same lock, and rcd I/O
    under it would stall them all on every startup. So the store
    read-modify-write happens entirely inside `with _store_lock`, and
    whatever forced-detach is needed is only PLANNED there (captured as
    `detach_target`) and executed after the `with` block exits."""
    try:
        path = learn_zip_path()
        detach_target: tuple[dict, str] | None = None
        with _store_lock:
            mounts = list_mounts()
            builtin = next(
                (m for m in mounts if m.get("builtin") == LEARN_MOUNT_NAME), None
            )
            if path is None:
                if builtin is not None:
                    old_remote = builtin["remote"]
                    _write([m for m in mounts if m is not builtin])
                    detach_target = (builtin, old_remote)
            else:
                remote = f":archive:{path}"
                if builtin is not None:
                    # Captured BEFORE any mutation below: the live mount/serve
                    # (if any) are keyed to whatever fs string was in effect
                    # at the end of the LAST run, not the one we're about to
                    # write.
                    old_remote = builtin["remote"]
                    if old_remote != remote:
                        builtin["remote"] = remote
                        _write(mounts)
                    # Force a fresh mount every startup, changed or not (see
                    # the upgrade-same-path staleness case above).
                    detach_target = (builtin, old_remote)
                elif any(m["name"] == LEARN_MOUNT_NAME for m in mounts):
                    logger.warning(
                        "not adding the builtin learn mount: a user mount "
                        "named %r already exists", LEARN_MOUNT_NAME,
                    )
                else:
                    mounts.append({
                        "id": uuid.uuid4().hex[:12],
                        "name": LEARN_MOUNT_NAME,
                        "remote": remote,
                        "read_only": True,
                        "read_only_user": True,
                        "builtin": LEARN_MOUNT_NAME,
                    })
                    _write(mounts)
        if detach_target is not None:
            _force_detach_learn_mount(*detach_target)
    except Exception:
        logger.exception("ensure_learn_mount failed")


def learn_mount_ready() -> bool:
    """True when the builtin learn mount is actually attached — both rcd and
    the kernel agree the mountpoint is live — not merely when its record
    exists in mounts.json.

    The sidebar's Learn entry (Sidebar.tsx) uses this, surfaced through
    /api/config, to decide whether to render at all.

    BUGBOT (record-presence was not enough): ensure_learn_mount now
    force-detaches and remounts the learn mountpoint on EVERY startup (see
    its docstring) — including the common case where the record already
    existed on disk from a prior run. A presence-only check would read
    "ready" the instant that record is read off disk, well before
    run_automount's attach_mount loop (which runs after ensure_learn_mount
    returns, on the same background automount thread) has remounted it —
    an early click would hit an empty mountpoint whose HTTP serve was just
    stopped. Checking BOTH mounted_paths() (rcd's own bookkeeping) and
    os.path.ismount() (the kernel mount table) is exactly attach_mount's own
    signal for "there is nothing left to attach" (see its `os.path.ismount`
    check), so this reads true only once that loop has actually succeeded."""
    builtin = next(
        (m for m in list_mounts() if m.get("builtin") == LEARN_MOUNT_NAME), None
    )
    if builtin is None:
        return False
    mp = mountpoint(builtin)
    return mp in mounted_paths() and os.path.ismount(mp)


def _force_detach_learn_mount(builtin: dict, old_remote: str) -> None:
    """Best-effort unmount of the builtin learn mountpoint if rcd (or the
    kernel) still has one live from a prior server run, so the caller's
    upserted record gets a genuinely fresh mount/mount instead of being
    silently adopted with stale fs/content — see ensure_learn_mount's BUGBOT
    note. Runs OUTSIDE any lock the caller already holds being irrelevant
    here since detach_mount only talks to rcd/the kernel, never mounts.json.

    Also stops the HTTP serve for `old_remote` (BUGBOT: rcd shares ONE VFS
    between a mount and its serve — mount/unmount tears that VFS down but
    leaves the serve pointed at it, so a leftover serve is wedged exactly
    like reconnect_mount's own _stop_serve_for call documents). Without
    this, sync_serves sees the OLD remote/options still "in use" by that
    wedged serve and reuses it instead of starting a fresh one bound to the
    new mount, so /api/fs/raw reads of Learn hang. `old_remote` — not
    `builtin["remote"]`, which may already have been rewritten to the NEW
    fs by the time this runs — is what the live serve is actually keyed to.

    Swallows everything: a failed detach/stop just means run_automount's
    subsequent attach_mount adopts (or errors on) whatever is still there,
    exactly like before this fix — never worse.

    BUGBOT: detach_mount's default (force=False) deliberately leaves a
    non-busy failure (rcd down but a kernel mount survives, a busy-retry
    that still fails, ...) in place — "failing loudly beats corrupted
    reads" is the right call for an explicit user unmount, but it defeats
    the very point of THIS call: attach_mount treats a still-kernel-mounted
    path with no matching rcd record as a foreign mount and adopts it
    as-is, silently keeping stale content across the refresh this path
    exists to guarantee. force=True escalates every dead end to
    _force_unmount instead, so a genuinely fresh mount/mount follows.

    BUGBOT: force=True alone still isn't enough — detach_mount only
    escalates to _force_unmount when the rc `mount/unmount` call itself
    FAILS; it never re-checks os.path.ismount after a call that reports
    success. reconnect_mount already has to guard against exactly this on
    macOS (learn is attached via nfsmount): rc's mount/unmount can report
    success while the kernel NFS mount lingers, and reconnect_mount
    re-checks os.path.ismount afterward for that reason. Mirror that same
    re-check here, rather than trusting detach_mount's return value alone.

    BUGBOT: _force_unmount operates purely at the kernel level (umount /
    diskutil) — it never tells rcd anything, so a successful force-unmount
    can leave rcd's OWN mount/listmounts bookkeeping still claiming the
    mountpoint. run_automount's loop treats exactly that combination (rcd
    still lists it, kernel does not) as the split-brain case and
    `continue`s PAST attach_mount for it — leaving the builtin mount never
    remounted after the very refresh this whole path exists to perform.
    reconnect_mount avoids this by re-issuing rc mount/unmount a second
    time after its own force-unmount, purely to clear rcd's bookkeeping (a
    "mount not found" failure at that point is expected and fine, since the
    kernel mount is already gone) — mirror that same follow-up call here."""
    try:
        mp = mountpoint(builtin)
        live = mp in mounted_paths() or os.path.ismount(mp)
        port = _live_rcd_port()
        if live:
            detach_mount(builtin, force=True)
            if os.path.ismount(mp):
                _force_unmount(mp)
                if port is not None:
                    try:
                        _rc(port, "mount/unmount", {"mountPoint": mp})
                    except RuntimeError:
                        pass  # "mount not found" once the kernel mount is gone — fine
        if port is not None:
            _stop_serve_for(port, old_remote)
    except Exception:
        logger.warning("force-detach of builtin learn mount failed", exc_info=True)


# ------------------------------------------------------ mount health monitor
# A background poll loop that watches every mount for a wedge/disconnect and
# auto-reconnects it ONCE per disconnect episode, so the common macOS NFS drop
# (rclone's serve dies, the kernel mount entry survives, reads hang forever)
# self-heals without the user having to hit Reconnect. It drives the exact same
# primitives the UI does — mount_state (the cheap ismount + listmounts-membership
# probe, timeout-isolated) as the detector, reconnect_mount (force-unmount +
# fresh mount, taking _rcd_lock) as the repair — just on a timer.
#
# Everything here is in-process only: this is live health telemetry, not a
# persisted record. mounts.json already survives restarts on its own, and a
# fresh server re-derives health from scratch on its next tick.

HEALTH_POLL_INTERVAL = 20.0  # seconds between health ticks

# Bounded event log the frontend polls (GET /api/mounts/health). We keep a
# window (not just the latest) so a briefly-away user still sees what the loop
# did. Ids are a monotonic in-process counter — NOT time/random — so the
# frontend can reliably dedup / "seen up to id N" even when two events share a
# ts (time.time() has coarse resolution and two reconnect events can collide).
_health_log_lock = threading.Lock()
_health_events: "collections.deque[dict]" = collections.deque(maxlen=100)
_health_event_seq = 0  # next event id; only mutated under _health_log_lock

# Per-mount episode state, keyed by mount id: the last state we observed and
# whether this episode's single reconnect has been spent. An "episode" is one
# continuous stretch of not-healthy; we RE-ARM (allow a fresh attempt) only
# after the mount is observed "mounted" again — so a mount that stays wedged
# gets exactly one reconnect, not one every tick. Owned by the single health
# loop thread (poll_once); health_snapshot only does GIL-atomic reads of it.
_health_episodes: "dict[str, dict]" = {}
_health_thread: "threading.Thread | None" = None
_health_started = threading.Lock()  # guards start_health_monitor idempotency

# States that mean "remote data isn't flowing, a reconnect would help". Note
# "unmounted" is deliberately EXCLUDED: mount_state returns it when neither the
# kernel nor rcd knows the mountpoint, which is exactly what detach_mount leaves
# behind (it unmounts but keeps the mounts.json record). Auto-reconnecting that
# would resurrect a mount the user intentionally detached — attach/detach stays
# a user decision, so the monitor never touches an "unmounted" mount.
_NEEDS_RECONNECT = ("disconnected", "stale")


def _health_emit(mount_id: str, name: str, kind: str, detail: str = "") -> None:
    """Append one event to the bounded log under its lock. kind is one of
    "disconnected" | "reconnected" | "reconnect_failed". ts is epoch seconds —
    wall-clock is fine for a UI log; ordering is by the monotonic id, not ts."""
    global _health_event_seq
    with _health_log_lock:
        _health_event_seq += 1
        _health_events.append({
            "id": _health_event_seq,
            "mount_id": mount_id,
            "name": name,
            "kind": kind,
            "ts": time.time(),
            "detail": detail,
        })


def poll_once() -> None:
    """One health tick: snapshot rcd's served set once, classify every mount
    against it, and emit ONE "disconnected" event per episode so the UI can
    notify the user (who then repairs it via a manual /reconnect).

    DETECTION ONLY — auto-reconnect is intentionally OFF (2026-07-22). On these
    flap-prone S3/NFS mounts an automatic reconnect churned: reconnects raced to
    "mount_nfs: Resource busy" (remounting before the prior mount finished
    tearing down), and a failed reconnect left the mount stale, which the next
    tick re-detected — a loop across several mounts. The underlying drops are
    pre-existing and real; repair is now the user's explicit action. A redesign
    (backoff + settle-before-remount, no reconnect while one is in flight) is
    tracked before re-enabling.

    Detection is I/O-FREE: mount_state(..., probe_io=False) classifies from
    os.path.ismount + rcd listmounts membership ALONE. It must never os.listdir
    the mount root here — that kernel READDIR on an S3 mount is itself a wedge
    trigger, and a 20s timer firing it (via abandoned-but-uncancellable probe
    threads) across every mount is exactly the load this loop must not add.

    Fire only on a genuine healthy->broken transition (prev == "mounted"), once
    per episode; a return to "mounted" re-arms for the next drop. A mount broken
    at startup (prev None) is left alone."""
    # One mount/listmounts call per tick, shared across every mount_state.
    live = mounted_paths()
    for m in list_mounts():
        mid = m["id"]
        state = mount_state(m, live, probe_io=False)
        ep = _health_episodes.setdefault(mid, {"state": None, "notified": False})
        prev = ep["state"]
        ep["state"] = state
        if state == "mounted":
            ep["notified"] = False  # healthy: re-arm for the next drop
            continue
        if state not in _NEEDS_RECONNECT:
            # "unmounted" (user-detached) or any unexpected state: hands off.
            continue
        # Notify once, on the transition INTO the broken episode.
        if prev != "mounted" or ep["notified"]:
            continue
        ep["notified"] = True
        _health_emit(mid, m["name"], "disconnected", detail=f"state={state}")


def _health_loop() -> None:
    """Daemon-thread body: poll_once() on a timer, forever. A tick's exceptions
    are already caught inside poll_once, but wrap here too so nothing — not even
    an error building `live` — can ever kill the loop."""
    while True:
        try:
            poll_once()
        except Exception:
            logger.exception("mount health poll tick failed")
        time.sleep(HEALTH_POLL_INTERVAL)


def start_health_monitor() -> None:
    """Start the background health poll loop. Idempotent — safe to call once at
    server startup; a redundant call while the thread is alive is a no-op."""
    global _health_thread
    with _health_started:
        if _health_thread is not None and _health_thread.is_alive():
            return
        _health_thread = threading.Thread(
            target=_health_loop, daemon=True, name="mounts-health-monitor")
        _health_thread.start()


def health_snapshot() -> dict:
    """The GET /api/mounts/health payload: current per-mount state + the running
    event log. Per-mount state is served from the loop's last observation (at
    most HEALTH_POLL_INTERVAL stale) so a frequently-polled UI never pays a
    per-mount PROBE_TIMEOUT on the request path; a mount added since the last
    tick (no cached state yet) gets one fresh probe.

    That fresh probe is I/O-FREE (probe_io=False, same as the periodic loop): a
    kernel os.listdir on an S3-backed mount root is itself a wedge trigger, and
    this endpoint is polled every ~15s by the UI, so it must never touch the
    mount contents."""
    live = mounted_paths()
    mounts_out = []
    for m in list_mounts():
        ep = _health_episodes.get(m["id"])
        state = (ep["state"] if ep and ep["state"] is not None
                 else mount_state(m, live, probe_io=False))
        mounts_out.append({
            "id": m["id"],
            "name": m["name"],
            "state": state,
            "mountpoint": mountpoint(m),
        })
    with _health_log_lock:
        events = list(_health_events)  # oldest->newest; sort by id UI-side
    return {"mounts": mounts_out, "events": events}


def run_automount() -> None:
    """Remount every mount that isn't already mounted. All mounts are
    remounted at startup — there is no per-mount opt-in. Adoption is implicit:
    mount/listmounts is the status source of truth, so mounts that survived a
    server restart just show up. Best-effort — a failure logs and moves on,
    never blocks startup."""
    # Upsert the builtin learn mount BEFORE the snapshot below: a fresh
    # install has zero user mounts, and skipping the attach loop below would
    # otherwise skip the builtin's very first mount too.
    ensure_learn_mount()
    mounts = list_mounts()
    if mounts:
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
        # Mounts that survived a server restart skip attach_mount above, so
        # their HTTP serves (lost with any rcd restart) get re-ensured here.
        sync_serves()
    elif os.path.exists(serves_path()):
        # BUGBOT: `mounts` came back empty, which usually means a genuinely
        # mount-less install (nothing to sync, and serves_path() was never
        # written — skipping here keeps a fresh install from gaining a
        # home_dir()/serves.json write it never needed). But it can ALSO
        # mean ensure_learn_mount above just removed the builtin record
        # (zip gone) and stopped its rc serve directly via
        # _force_detach_learn_mount — and serves.json on disk is ONLY ever
        # rewritten by sync_serves, so skipping unconditionally (the old
        # behavior) would leave a stale {mountpoint: dead_url} entry that
        # serve_url_for keeps resolving forever. The existence check tells
        # the two cases apart: a serves.json only exists once some earlier
        # run actually had something to serve.
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


def _rclone_state_view(version: str | None, names: list[str],
                       bin_: str | None) -> dict:
    """Assemble the available:True payload from a version string and the remote
    names (each carrying its verbatim rclone spec, incl trailing ':', used
    unchanged as the mount base). Each remote also gets a friendly `label` so it
    reads under one stable human name whatever its lifecycle stage. Compute the
    suggestion set and the config dump once, then label every remote against them
    (both do I/O, so a per-remote call would be O(N)). `bin_` may be None when a
    live daemon vouched for rclone but the binary didn't resolve on PATH — the
    config dump is then skipped and labels degrade to bare names."""
    suggestions = _credential_suggestions()
    configs = _rclone_config_dump(bin_) if bin_ else {}
    remotes = [{"name": n, "label": _remote_label(n, suggestions, configs)}
               for n in names]
    return {"available": True, "version": version, "remotes": remotes,
            "suggested": _suggestions_view(names)}


def _rclone_state() -> dict:
    bin_ = rclone_bin()
    if bin_:
        try:
            version = subprocess.run(
                [bin_, "version"], capture_output=True, text=True, timeout=10
            ).stdout.splitlines()[0]
            remotes_out = subprocess.run(
                [bin_, "listremotes"], capture_output=True, text=True, timeout=10
            ).stdout
            names = [r.strip() for r in remotes_out.splitlines() if r.strip()]
            return _rclone_state_view(version, names, bin_)
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass  # fall through to the daemon vouch below
    # The direct probe couldn't confirm rclone — the binary didn't resolve on
    # PATH, or the version/listremotes subprocess hiccupped. Observed on a fresh
    # server launch: the first probe reports unavailable while an already-running
    # rcd is happily serving mounts, so the Mounts page shows a spurious "rclone
    # not found" until the process is bounced. A live rcd daemon is itself proof
    # rclone works, so ask IT for version/remotes rather than reporting a false
    # "not installed". Only when there's no daemon either do we report unavailable.
    port = _live_rcd_port()
    if port is not None:
        # The daemon's liveness already settles availability; fetch version and
        # remotes INDEPENDENTLY so one rc call failing doesn't discard what the
        # other returned (a shared try would drop a good version when only
        # listremotes hiccups). Each degrades to its own empty on failure.
        try:
            version = _rc(port, "core/version").get("version")
        except RuntimeError:
            version = None
        try:
            names = [f"{n}:" for n in _rc(port, "config/listremotes").get("remotes", [])]
        except RuntimeError:
            names = []
        return _rclone_state_view(version, names, bin_)
    return {"available": False, "version": None, "remotes": [], "suggested": []}


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
        # One probe, three outcomes (see _credential_probe):
        cred_status = _mount_credential_status(m)
        if cred_status == "bad":
            return f"mount '{name}' — {_CRED_EXPIRED_MSG}"
        # "valid": the user re-authed, but the long-lived daemon still holds the
        # pre-refresh keys, so Reconnect (which reuses that daemon) can't help —
        # only a daemon restart re-reads them. "inconclusive"/"n/a" fall through
        # to the generic reconnect message (a transient failure or a
        # non-credential remote must NOT suggest a restart).
        if cred_status == "valid":
            return (f"mount '{name}' — your credentials look refreshed; restart "
                    f"rclone from the Mounts page in the sidebar to pick up the "
                    f"new credentials")
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
    bin_ = rclone_bin()
    # Probe states — AND, for a broken mount, its credential status — concurrently:
    # each disconnected/wedged mount blocks its state probe for the full
    # PROBE_TIMEOUT, and the credential `rclone lsd` can take ~10s more. Serially
    # (states threaded but the credential probe left to the mount_view loop) a
    # few broken aws mounts would stall the polled Mounts page for tens of
    # seconds — exactly when the user is polling to recover. So do BOTH in the
    # per-mount worker and hand the results to mount_view, which never probes.
    states: list[str | None] = [None] * len(mounts)
    cred_statuses: list[str] = ["n/a"] * len(mounts)
    threads = []
    for i, m in enumerate(mounts):
        def probe(i=i, m=m):
            st = mount_state(m, live)
            states[i] = st
            # Credentials only matter for a broken mount; a healthy/unmounted
            # one never pays the lsd probe.
            if st in ("disconnected", "stale"):
                cred_statuses[i] = _mount_credential_status(m, bin_)
        t = threading.Thread(target=probe, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        # Allow for the state probe PLUS the credential lsd (30s cap) so a slow
        # probe still lands in this listing rather than defaulting to unknown.
        t.join(PROBE_TIMEOUT + 31)
    return {
        "rclone": _rclone_state(),
        "mounts": [
            mount_view(m, live, state=s or "disconnected", cred_status=cs)
            for m, s, cs in zip(mounts, states, cred_statuses)
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


@router.post("/api/mounts/restart")
def restart_endpoint(x_fused: str | None = Header(default=None, alias="X-Fused")):
    """Global recovery: restart the rcd daemon and re-mount everything. The one
    tool that fixes a stale-credential daemon (a fresh daemon re-reads refreshed
    keys) and applies changed mount params — see restart_rcd. Sync def so the
    multi-second unmount+kill+spawn+remount runs in the threadpool, never the
    event loop. Returns the same payload as GET /api/mounts."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        restart_rcd()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return get_mounts()


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
    if m.get("builtin"):
        # BUGBOT: nothing stopped this — a deleted builtin record only
        # reappears at the next full SERVER restart (ensure_learn_mount runs
        # once, from run_automount at startup), while the already-open
        # Sidebar's learnMountReady state never rechecks once true, leaving
        # a dead Learn link for the rest of the session. Bundled read-only
        # content isn't something a user action should be able to
        # permanently remove out from under a running session anyway —
        # unmounting (POST .../unmount) still works to free the mountpoint.
        return JSONResponse(
            {"error": "this is a bundled default mount and can't be deleted"},
            status_code=400,
        )
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
    _invalidate_upstream_caches()  # the gone remote's memoized facts mustn't linger
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
    _invalidate_upstream_caches()  # new/changed keys must be picked up without restart
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


_CRED_EXPIRED_MSG = (
    "the detected credentials appear expired or invalid — refresh them "
    "(e.g. `aws sso login` or `gcloud auth application-default login`) "
    "and try again"
)


def _credential_probe(bin_: str, name: str) -> str:
    """Tri-state result of a top-level `lsd` against an env_auth remote:

      "valid"        — the listing succeeded (returncode 0): the credentials
                       positively work right now.
      "bad"          — a credential-shaped failure (_BAD_CRED_MARKERS): expired
                       SSO/STS token, revoked key, missing default creds.
      "inconclusive" — the probe couldn't decide: a timeout, network error, or a
                       non-credential failure like AccessDenied (valid keys that
                       merely lack ListBuckets). NOT proof the creds work.

    The three-way split matters because "not bad" is not the same as "good":
    only a POSITIVE success may drive the "credentials refreshed → Restart"
    prompt, or a transient failure would spam a false restart suggestion."""
    try:
        r = subprocess.run(
            [bin_, "lsd", f"{name}:", "--max-depth", "1",
             "--contimeout", "5s", "--timeout", "10s",
             "--retries", "1", "--low-level-retries", "2"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "inconclusive"
    if r.returncode == 0:
        return "valid"
    err = ((r.stderr or "") + (r.stdout or "")).lower()
    if any(m in err for m in _BAD_CRED_MARKERS):
        return "bad"
    return "inconclusive"


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
    return _CRED_EXPIRED_MSG if _credential_probe(bin_, name) == "bad" else None


def _mount_credential_status(m: dict, bin_: str | None = None) -> str:
    """Tri-state credential health of a broken mount's remote, or "n/a":

      "valid" / "bad" / "inconclusive" — the _credential_probe outcome, for an
                       env_auth remote (see there).
      "n/a"          — not an env_auth remote (anonymous/public or key-carrying
                       remotes don't expire this way), or no rclone binary.

    Only env_auth remotes are probed, and the probe (an rclone `lsd`) is paid
    only on an already-broken mount, never on a healthy listing. Callers may
    pass a resolved `bin_` to avoid re-resolving rclone per mount."""
    if bin_ is None:
        bin_ = rclone_bin()
    if not bin_:
        return "n/a"
    name = m["remote"].partition(":")[0]
    cfg = _remote_config(name)
    if not isinstance(cfg, dict) or str(cfg.get("env_auth", "")).lower() != "true":
        return "n/a"
    return _credential_probe(bin_, name)


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
    _invalidate_upstream_caches()  # new/changed keys must be picked up without restart
    return {"ok": True, "name": name + ":"}

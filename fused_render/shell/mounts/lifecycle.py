"""Kernel-mount lifecycle: sync_serves, attach_mount/detach_mount/
reconnect_mount, and the mount_state/mount_restart_reason/mount_view status
views the UI and health monitor read."""

import errno
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fused_render.shell import storage

from .access import _http_serves, _refresh_read_only_flag, _serves_lock, serves_path
from .config import _nfs_mount_opt, _serve_vfs_opt_for, _vfs_opt_for
from .rcd import _winfsp_missing_error, rcd_mount_map
from .store import _ismount, _update_mount, ensure_mounts_dir, mountpoint

logger = logging.getLogger(__name__)


DAEMON_STATE_FILES = (
    os.path.expanduser("~/.cache/fused-render-geotiff-v2/daemon.json"),
    os.path.expanduser("~/.cache/fused-render-gridv2/daemon.json"),
)


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
    from fused_render.shell.mounts import _live_rcd_port, _rc, list_mounts
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


_MOUNT_ATTACH_DEADLINE_S = 3.0


_MOUNT_ATTACH_POLL_S = 0.1


def _await_ismount(mp: str, deadline: float = _MOUNT_ATTACH_DEADLINE_S) -> bool:
    """True once _ismount(mp) holds within `deadline` seconds, else False."""
    end = time.monotonic() + deadline
    while True:
        if _ismount(mp):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(_MOUNT_ATTACH_POLL_S)


def _mount_wedged(mp: str) -> bool:
    # True when mp is a mountpoint whose backend process is gone: the kernel
    # still holds the mount, so the path exists, but every stat on it fails.
    if sys.platform == "win32":
        # A dead WinFsp mount keeps its volume reparse point while the volume
        # query behind ntpath.ismount fails (WinError 123) — _ismount swallows
        # that raise to stay True for reconnect's heal path, so the wedge
        # signature must be re-derived here for state classification.
        try:
            os.path.ismount(mp)
        except OSError:
            return os.path.lexists(mp)
        return False
    try:
        os.lstat(mp)
    except OSError as e:
        return e.errno in (errno.ENOTCONN, errno.EHOSTDOWN, errno.ESTALE)
    except ValueError:
        return False
    return False


def _is_mounted(mp: str) -> bool:
    # _ismount (not the bare os.path.ismount: WinFsp reparse mounts read False
    # there), plus wedged mounts. posixpath.ismount swallows the OSError from
    # lstat and reports False for a mountpoint whose FUSE daemon died
    # (ENOTCONN) — precisely the state reconnect exists to repair, so the bare
    # predicate skips the force-unmount and then crashes in attach_mount's
    # makedirs.
    return _ismount(mp) or _mount_wedged(mp)


def attach_mount(m: dict) -> str | None:
    """Mount via rcd; returns an error string or None."""
    from fused_render.shell.mounts import (
        _await_ismount,
        _rc,
        _winfsp_available,
        ensure_rcd,
        reconnect_mount,
    )
    mp = mountpoint(m)
    # Create the mounts root (with its Spotlight-exclusion marker) before the
    # per-mount mountpoint, so the marker is in place the moment the mount goes
    # live and Spotlight never gets a chance to scan it.
    ensure_mounts_dir()
    # Mountpoint-leaf semantics diverge by platform. POSIX backends (FUSE, and
    # the macOS loopback NFS mount) attach OVER an existing empty directory, so
    # we pre-create the leaf. WinFsp — rclone's Windows mount backend — is the
    # exact opposite: it creates the mountpoint itself and REFUSES to mount when
    # the leaf already exists, so a pre-created directory makes mount/mount fail
    # outright. Hence on win32 we must NOT create the leaf; we only clear a stale
    # EMPTY leaf a previous mount left behind (os.rmdir succeeds only when empty)
    # and refuse a non-empty leaf rather than delete a user's files. If the leaf
    # is already a live mount we leave it for the adopt/reconcile path below.
    if _mount_wedged(mp):
        # Nothing can be mounted over a path whose backend is gone: on POSIX
        # makedirs' exist_ok check can't recognise it as a directory and
        # raises FileExistsError; on win32 the dead reparse point would pass
        # _ismount below and be silently adopted as a live mount.
        # reconnect_mount clears this before re-attaching; anyone else
        # (automount, a plain mount) is told to.
        return (f"mountpoint {mp} is wedged — its backend is gone; "
                f"reconnect the mount to repair it")
    if sys.platform == "win32":
        if os.path.isdir(mp) and not _ismount(mp):
            try:
                os.rmdir(mp)
            except FileNotFoundError:
                # Raced delete: the stale leaf is already gone, which is exactly
                # the state we were trying to reach — proceed.
                pass
            except OSError as e:
                if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    # A real non-empty leaf: never delete a user's files; ask
                    # them to clear it.
                    return (f"mountpoint {mp} already exists and is not empty — "
                            f"remove it before mounting")
                # Anything else (permissions, sharing violation, …) — report the
                # actual failure instead of misblaming it on non-emptiness.
                return f"could not clear stale mountpoint {mp}: {e}"
    else:
        os.makedirs(mp, exist_ok=True)
    if _ismount(mp):
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
            # Normally we remount (reconnect_mount) to bake the corrected
            # read_only into the live VFS. But reconnect UNMOUNTS FIRST, and on
            # win32 without WinFsp the re-attach can't succeed — so remounting
            # here would destroy a healthy survivor to apply a mere safety tweak.
            # The read_only remount is an improvement (it stops the doomed-upload
            # retry loop on a read-only remote), not worth tearing down a working
            # mount over: adopt as-is and let a later attach — once WinFsp is
            # present — apply it. (reconnect_mount also gates this, so a direct
            # /reconnect is safe too; this keeps the automount adopt path from
            # even entering the unmount.) The record's read_only now sits AHEAD
            # of what the live VFS baked (mounted_read_only); sync_serves below
            # derives the serve's ReadOnly from mounted_read_only while a live
            # mount exists (see _effective_serve_read_only), so the serve still
            # SHARES the mount's VFS and does not fork a second one here.
            if not (sys.platform == "win32" and not _winfsp_available()):
                return reconnect_mount(m)  # unmounts first, so no recursion here
        # Already mounted (double-click, adopted foreign mount) — but the
        # HTTP serve may still be missing (a prior serve/start failed, or the
        # mount predates the serve layer), so reconcile serves here too:
        # without one, /api/fs/raw silently falls back to reads through the
        # wedge-prone kernel mount.
        sync_serves()
        return None
    # Only the mount-CREATION path needs WinFsp. Gate it here — AFTER the adopt
    # branch above has returned for an already-live mount — so a mount that
    # survived a restart (run_automount re-attaches it via this same function)
    # is never rejected just because the detector false-negatives (non-default
    # WinFsp install location) or WinFsp was removed while rcd + the mount stay
    # alive under the Job Object. Still fail fast for a genuinely new mount:
    # before ensure_rcd and mount/mount, rclone would otherwise error deep in
    # the backend with an opaque message; point the user at the installer
    # instead (vacuously True off Windows, so POSIX is unaffected).
    if not _winfsp_available():
        return _winfsp_missing_error()
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
            "mountType": (
                "nfsmount" if sys.platform == "darwin"
                else "cmount" if sys.platform == "win32"
                else "mount"
            ),
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
        # win32 only: force WinFsp DISK mode (NetworkMode off). rclone defaults
        # Windows mounts to network-redirector mode, which does NOT create a
        # mount point at the leaf at all. Every win32 detection path here leans
        # on _ismount: the _await_ismount verify below would fail a SUCCESSFUL
        # mount, and a retry could then mistake the live mount's contents for a
        # non-empty leaf (or the force-unmount poll / mount_state would call a
        # live mount dead). Disk mode creates the volume-device reparse point
        # that _ismount detects. Like the darwin mountOpt, NetworkMode is a
        # transport option, NOT a vfs option, so it does not affect the
        # (fs, vfsOpt) VFS-reuse key — the mount still shares its VFS with the
        # serve.
        elif sys.platform == "win32":
            params["mountOpt"] = {"NetworkMode": False}
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
    # (and _effective_serve_read_only) can tell a live VFS predates a read_only
    # change. Persist the bake EXPLICITLY, False included — a writable attach
    # must write mounted_read_only=False, not leave the key absent: otherwise a
    # later read_only=true drift on the win32 deferred-remount path can't tell
    # the live VFS is writable and forks a second serve VFS. Only genuinely
    # legacy/foreign records (adopted via listmounts, never through here) keep an
    # absent key, which the consumers treat as "unknown bake". Compare against
    # the raw stored value so an already-correct bool is a no-op (no churn).
    baked = bool(m.get("read_only"))
    if m.get("mounted_read_only") != baked:
        m["mounted_read_only"] = baked
        _update_mount(m)
    sync_serves()
    return None


def _quit_tile_daemons() -> None:
    """Best-effort /quit to every live tile-server daemon so they release
    open files under the mount (measured EBUSY cause). Absent/corrupt state
    files and dead ports are skipped silently."""
    from fused_render.shell.mounts import DAEMON_STATE_FILES
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


_FORCE_UNMOUNT_WIN32_BUDGET_S = 15.0


def _force_unmount(mp: str) -> str | None:
    """Kernel-level unmount for a DEAD mount, escalating to force. Only for
    mounts whose serving daemon is gone/wedged — there is nothing left to
    corrupt, and rcd's own unmount either failed or can't be asked. Returns
    an error string or None."""
    # win32 has no `umount` and no per-mount kernel detach. A WinFsp mount is
    # backed by rcd's serving process and vanishes the instant that process dies
    # — so process teardown, not a shell-out, IS the force-unmount. We do NOT
    # kill rcd here: that would tear down EVERY mount it serves. So the win32
    # branch simply polls _ismount within the same budget the POSIX ladder gets and
    # reports success once the reparse point is gone (rcd already exited /
    # unmounted), else an honest failure — there is nothing else safe to try.
    from fused_render.shell.mounts import _FORCE_UNMOUNT_WIN32_BUDGET_S
    if sys.platform == "win32":
        deadline = time.time() + _FORCE_UNMOUNT_WIN32_BUDGET_S
        while True:
            if not _ismount(mp):
                return None
            if _mount_wedged(mp) or time.time() >= deadline:
                break
            time.sleep(0.1)
        if _mount_wedged(mp):
            # Orphaned reparse point — its volume device is gone, so nothing
            # serves it and waiting cannot help. RemoveDirectory deletes the
            # reparse point itself (junction semantics: no recursion, no
            # privileges); DeleteVolumeMountPointW is the canonical fallback.
            try:
                os.rmdir(mp)
            except OSError:
                pass
            if not _is_mounted(mp):
                return None
            try:
                import ctypes
                ctypes.windll.kernel32.DeleteVolumeMountPointW(mp.rstrip("\\") + "\\")
                os.rmdir(mp)
            except (OSError, AttributeError):
                pass
            if not _is_mounted(mp):
                return None
            return f"force unmount of {mp} failed: orphaned mountpoint could not be removed"
        return (f"force unmount of {mp} is not possible on Windows while rclone "
                f"still serves it — disconnect the mount (or quit) to clear it")
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
        # _is_mounted, not the bare predicate: a mountpoint still wedged
        # (ENOTCONN) reads as ismount False, so plain ismount would call every
        # failed attempt a success and let the caller remount over a path
        # nothing can be mounted over.
        if not _is_mounted(mp):
            return None
    if not _is_mounted(mp):
        return None
    return f"force unmount of {mp} failed: {last or 'still mounted'}"


_QUIT_UNMOUNT_BUDGET_S = 6.0


def _unmount_for_quit(m: dict) -> None:
    """The unmount ladder for ONE mount on the quit path. Never raises — the
    caller runs these in parallel and one wedged mount must not take the others
    (or the quit) with it.

    `detach_mount(force=True)` IS the ladder (ask rcd; on a busy failure quiesce
    the tile daemons that hold files open under the mount and retry; then
    `_force_unmount`), the same rung restart_rcd already uses before it kills a
    daemon — quit is that same "the daemon is about to go away" situation, so it
    gets the same treatment rather than a second copy of the ladder. `force=True`
    is required, not incidental: the plain call refuses to force ("failing loudly
    beats corrupted reads", right for a user action on a live mount), whereas on
    quit an attached mount whose NFS server is about to be signalled IS the harm.

    Plus a last rung on the one question that actually matters here — is anything
    still attached? — because detach_mount can answer "done" with a kernel mount
    still in place. Its own force gate is `_ismount`, which is False for a WEDGED
    mount (backend already gone, kernel entry retained: every stat ENOTCONNs and
    posixpath.ismount swallows it), and its success path trusts rcd's OK, which
    the INCIDENT 2026-07-16 split-brain shows can sit over a kernel mount that
    stays behind. `_is_mounted` sees both. On the normal path it is False by then
    and this rung costs nothing; on a mount detach_mount already tried and failed
    to force it is one last attempt before the process goes away, which the
    caller's budget bounds."""
    from fused_render.shell.mounts import (
        _force_unmount,
        _is_mounted,
        detach_mount,
    )
    mp = mountpoint(m)
    try:
        err = detach_mount(m, force=True)
        if err:
            logger.warning("quit: unmount of %r: %s", m.get("name"), err)
    except Exception:
        logger.warning("quit: unmount of %r failed", m.get("name"), exc_info=True)
    try:
        if _is_mounted(mp):
            err = _force_unmount(mp)
            if err:
                logger.warning("quit: %s", err)
    except Exception:
        logger.warning("quit: force unmount of %r failed", m.get("name"),
                       exc_info=True)


def unmount_all_for_quit(budget_s: float = _QUIT_UNMOUNT_BUDGET_S) -> None:
    """Detach every configured mount before rcd is signalled, for app quit.

    The bug this closes (INCIDENT 2026-07-29): on macOS a mount is attached as
    an `nfsmount`, so the KERNEL holds a real NFS mount whose server IS the rcd
    process. The quit path killed rcd with those mounts still attached — the NFS
    server vanished under its own loopback client, the client timed out, and
    macOS raised "server connection interrupted / disks not ejected properly"
    for every mount. rclone's SIGTERM unmount cannot be relied on to prevent it
    (plain umount, rejected by a busy nfsmount) and the SIGKILL escalation skips
    it entirely. So quit now goes through the same rc-unmount -> force-unmount
    ladder every other teardown in this module uses.

    Gated on _rcd_is_ours_to_reap: with FUSED_RENDER_RCLONE_PERSIST set (dev) or
    a still-live foreign spawner, the daemon keeps running — and a running
    daemon's mounts must keep running with it.

    Bounded and best-effort. Each mount is unmounted on its own thread (the
    mount_state pattern) so a wedged `umount -f` — which can block for tens of
    seconds in the kernel, and which we cannot cancel — costs only its share of
    the wait: after `budget_s` we stop waiting and let the quit proceed. Threads
    are daemons, so anything still stuck simply dies with the process."""
    from fused_render.shell.mounts import (
        _rcd_is_ours_to_reap,
        _unmount_for_quit,
        list_mounts,
    )
    if not _rcd_is_ours_to_reap():
        return
    try:
        mounts = list_mounts()
    except Exception:
        logger.warning("quit: could not read the mount store", exc_info=True)
        return
    threads = []
    for m in mounts:
        t = threading.Thread(target=_unmount_for_quit, args=(m,), daemon=True,
                             name=f"quit-unmount-{m.get('name')}")
        t.start()
        threads.append((m, t))
    deadline = time.monotonic() + budget_s
    for m, t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            logger.warning(
                "quit: unmount of %r did not finish within %.1fs; leaving it to "
                "the process exit", m.get("name"), budget_s)


def detach_mount(m: dict, force: bool = False) -> str | None:
    """Unmount via rcd; on failure ask the tile daemons to release their
    open files and retry once. Returns an error string or None. Never
    force-unmounts on its own — failing loudly beats corrupted reads.
    `force=True` (an explicit user action on a mount already shown as
    disconnected) escalates every dead end below to _force_unmount."""
    from fused_render.shell.mounts import _force_unmount, _live_rcd_port, _rc
    mp = mountpoint(m)
    port = _live_rcd_port()
    if port is None:
        # No daemon: nothing rcd-owned to unmount. A foreign mount at the
        # path (pre-rcd prototype, manual rclone) is not ours to force.
        if _ismount(mp):
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
            if force and _ismount(mp):
                return _force_unmount(mp)
            return f"unmount failed: {e}"
    _quit_tile_daemons()
    time.sleep(0.5)
    try:
        _rc(port, "mount/unmount", params)
        return None
    except RuntimeError as e:
        if force and _ismount(mp):
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
    from fused_render.shell.mounts import _rc
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
    from fused_render.shell.mounts import (
        _force_unmount,
        _live_rcd_port,
        _rc,
        _stop_serve_for,
        _winfsp_available,
    )
    mp = mountpoint(m)
    # Gate BEFORE the very first unmount below: reconnect tears the mount down
    # and then re-attaches, but on win32 without WinFsp the re-attach cannot
    # succeed — so unmounting would destroy a live mount we can no longer
    # rebuild. Refuse up front, leaving the existing mount untouched. (Vacuously
    # True off Windows, so POSIX reconnects are unaffected.)
    if not _winfsp_available():
        return _winfsp_missing_error()
    port = _live_rcd_port()
    if port is not None:
        try:
            _rc(port, "mount/unmount", {"mountPoint": mp})
        except RuntimeError:
            pass  # wedged: rcd's own umount fails; the force path handles it
    # _is_mounted: a mount whose FUSE daemon died is invisible to plain ismount,
    # and skipping the force-unmount here left the wedged path in place for
    # attach_mount to trip over — the one state this whole function exists for.
    if _is_mounted(mp):
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


PROBE_TIMEOUT = 3.0


def mount_state(m: dict, rcd_mounts: set, timeout: float = PROBE_TIMEOUT,
                *, probe_io: bool = True) -> str:
    """Health of one mount: "mounted" | "stale" | "disconnected" | "unmounted".

    "mounted" requires both that a live rcd serves the mountpoint AND (when
    `probe_io`, POSIX only) that the filesystem actually answers a listdir. Pass
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
            is_mnt = _is_mounted(mp)
            served = mp in rcd_mounts
            if is_mnt and _mount_wedged(mp):
                # Backend gone, kernel mount still held: every stat ENOTCONNs.
                # Classified here rather than left to the listdir below, so the
                # probe_io=False caller (the health monitor) sees it too — and
                # so plain ismount answering False can't read as "unmounted".
                out["state"] = "disconnected"
            elif not is_mnt and not served:
                out["state"] = "unmounted"
            elif served and not is_mnt:
                # rcd tracks a mount the kernel dropped (INCIDENT split-brain).
                out["state"] = "stale"
            elif is_mnt and not served:
                # Kernel mount whose rcd is gone (or a foreign mount we can't
                # health-check).
                out["state"] = "disconnected"
            else:
                # POSIX only: on win32 this readdir can fail for the lifetime of
                # one process while the mount is healthy (INCIDENT 2026-07-30).
                if probe_io and sys.platform != "win32":
                    os.listdir(mp)  # the actual I/O health check
                out["state"] = "mounted"
        except OSError as e:
            logger.warning("mount %r probe failed at %s: %s", m["name"], mp, e)
            out["state"] = "disconnected"

    t = threading.Thread(target=probe, daemon=True, name=f"mount-probe-{m['name']}")
    t.start()
    t.join(timeout)
    return out.get("state", "disconnected")  # no answer in time == wedged


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
    from fused_render.shell.mounts import _mount_credential_status, mount_state, mounted_paths
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
    from fused_render.shell.mounts import mount_state, mounted_paths
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

"""Built-in mounts ("learn", "sessions"), each staged from a bundled zip and
attached automatically at startup."""

import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid

from .store import _ismount, _store_lock, _write, mountpoint

logger = logging.getLogger(__name__)


LEARN_MOUNT_NAME = "learn"
SESSIONS_MOUNT_NAME = "sessions"

# Every builtin mount: bundled zip basename + the env var that overrides its
# location for dev/testing. Adding a builtin = one row here plus the packaging
# steps (build_dmg.sh, build_windows_installer.ps1, supervisor/paths.py).
BUILTIN_MOUNTS = {
    LEARN_MOUNT_NAME: ("learn.zip", "FUSED_RENDER_LEARN_ZIP"),
    SESSIONS_MOUNT_NAME: ("sessions.zip", "FUSED_RENDER_SESSIONS_ZIP"),
}


# Whether each builtin mount is attached RIGHT NOW, tracked across the automount
# lifecycle rather than probed live on the request path (builtin_mount_ready).
# run_automount is the sole writer: it clears every builtin to False before its
# force-detach+remount pass, then flips one True the moment its own attach_mount
# succeeds. The invariant that matters: this is True only for a mount THIS run
# attached — never one that merely survived from a previous run — because the
# frontend sticky-caches the first True it sees (platform/lib/hooks.ts), so a
# stale True would pin Learn/Sessions over an empty mountpoint for the session.
_builtin_ready_lock = threading.Lock()
_builtin_ready: dict[str, bool] = {name: False for name in BUILTIN_MOUNTS}


def set_builtin_ready(name: str, ready: bool) -> None:
    with _builtin_ready_lock:
        _builtin_ready[name] = ready


def builtin_zip_path(name: str) -> str | None:
    """Path to a builtin's bundled zip, or None outside the packaged app.

    The env var overrides for dev/testing (a dev checkout has the loose
    content dir, not a zip — build_dmg.sh only creates the zip at DMG
    build time). Packaged (same sys.frozen check as rclone_bin) it lives at
    Contents/Resources/<name>.zip (build_dmg.sh step 4e) on macOS; on the
    Windows/Linux payload layouts it sits next to the bundled runtime
    (payload/python/pythonw.exe -> payload/assets/<name>.zip), resolved from
    sys.executable so the server finds it without depending on the
    supervisor-injected env var. Existence-checked either way so a stale env
    var or a hand-pruned bundle yields None, not a mount record pointing at
    nothing."""
    zip_name, env_var = BUILTIN_MOUNTS[name]
    override = os.environ.get(env_var)
    if override:
        return override if os.path.isfile(override) else None
    if getattr(sys, "frozen", None) == "macosx_app":
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        bundled = os.path.join(contents, "Resources", zip_name)
        if os.path.isfile(bundled):
            return bundled
    runtime_root = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    adjacent = os.path.join(runtime_root, "assets", zip_name)
    if os.path.isfile(adjacent):
        return adjacent
    return None


def learn_zip_path() -> str | None:
    return builtin_zip_path(LEARN_MOUNT_NAME)


def ensure_builtin_mounts() -> None:
    """Upsert every builtin mount record (BUILTIN_MOUNTS)."""
    for name in BUILTIN_MOUNTS:
        _ensure_builtin_mount(name)


def ensure_learn_mount() -> None:
    _ensure_builtin_mount(LEARN_MOUNT_NAME)


def _ensure_builtin_mount(name: str) -> None:
    """Upsert a builtin mount record: rclone's archive backend
    (v1.74) mounts the bundled zip read-only through the same mounts
    surface as any remote (D123).

    Builtin records carry `"builtin": "learn"` so they're distinguishable
    from a user-created mount that happens to be named "learn" — that user
    mount is never touched. The remote embeds the zip's absolute path inside
    the app bundle, which changes across versions/relocations, so an existing
    record's remote is refreshed every startup; the record is removed only
    when the zip it points at is actually gone (uninstall, downgrade) so it
    can't linger as a broken mount in the UI — a process that merely can't
    RESOLVE a zip (a dev checkout sharing the real home) leaves a valid
    record alone. read_only_user pins the flag: the archive backend is
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
    from fused_render.shell.mounts import list_mounts
    try:
        path = builtin_zip_path(name)
        detach_target: tuple[dict, str] | None = None
        with _store_lock:
            mounts = list_mounts()
            builtin = next(
                (m for m in mounts if m.get("builtin") == name), None
            )
            if path is None:
                # Removal is gated on the RECORD's zip being gone from disk,
                # not on this process failing to resolve one: a dev-checkout
                # server sharing the real home resolves nothing but must not
                # delete the packaged app's perfectly valid record.
                if builtin is not None and not os.path.isfile(
                        builtin["remote"].partition(":archive:")[2]):
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
                elif any(m["name"] == name for m in mounts):
                    logger.warning(
                        "not adding the builtin %r mount: a user mount "
                        "named %r already exists", name, name,
                    )
                else:
                    mounts.append({
                        "id": uuid.uuid4().hex[:12],
                        "name": name,
                        "remote": remote,
                        "read_only": True,
                        "read_only_user": True,
                        "builtin": name,
                    })
                    _write(mounts)
        if detach_target is not None:
            _force_detach_builtin_mount(*detach_target)
    except Exception:
        logger.exception("ensure_builtin_mount(%r) failed", name)


def learn_mount_ready() -> bool:
    return builtin_mount_ready(LEARN_MOUNT_NAME)


def sessions_mount_ready() -> bool:
    return builtin_mount_ready(SESSIONS_MOUNT_NAME)


def builtin_mount_ready(name: str) -> bool:
    """True when run_automount has attached this builtin THIS run — an I/O-free
    read of the lifecycle-tracked _builtin_ready flag, never a live probe.

    The sidebar's Learn entry (Sidebar.tsx) uses this, surfaced through
    /api/config, to decide whether to render at all.

    Why not a live probe: the old check was `mp in mounted_paths()` plus
    `_ismount(mp)`, and on Windows `_ismount()` (os.path.ismount/os.lstat on the
    WinFsp reparse point) BLOCKS for ~the rc timeout while run_automount is
    mid-attach of the mountpoint. /api/config embeds this for BOTH builtins, so
    a cold start dragged /api/config to ~60s and the browser window couldn't
    paint. macOS (nfsmount) and Linux (FUSE) attach fast enough to hide it; the
    flag read fixes it on every platform.

    Why not the health monitor's cached state: that cache can hold "mounted"
    from a mount a PREVIOUS run left behind, before ensure_builtin_mount
    force-detaches it on startup — and since the frontend sticky-caches the
    first True (platform/lib/hooks.ts), a single stale True pins Learn over an
    empty mountpoint for the whole session. run_automount instead drops the flag
    to False before its force-detach and only sets it True once its own
    attach_mount has re-attached the mount this run, so True always means a
    mount that is live now. The frontend polls until it sees that True, so the
    seconds run_automount takes to attach cost only a briefly-absent sidebar
    entry, never a wrong one. True is written only by an operation that
    SUCCESSFULLY attached the mount this run — run_automount's own attach, or a
    manual reconnect_mount after the split-brain `continue` — never inferred
    from an observed "mounted" (a mount lingering from a failed force-detach
    would read mounted while serving stale content). The health monitor only
    ever CLEARS the flag, when a mount is observed no longer live."""
    with _builtin_ready_lock:
        return _builtin_ready.get(name, False)


def _force_detach_builtin_mount(builtin: dict, old_remote: str) -> None:
    """Best-effort unmount of a builtin mountpoint if rcd (or the
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
    from fused_render.shell.mounts import (
        _force_unmount,
        _live_rcd_port,
        _rc,
        _stop_serve_for,
        detach_mount,
        mounted_paths,
    )
    try:
        mp = mountpoint(builtin)
        live = mp in mounted_paths() or _ismount(mp)
        port = _live_rcd_port()
        if live:
            detach_mount(builtin, force=True)
            if _ismount(mp):
                _force_unmount(mp)
                if port is not None:
                    try:
                        _rc(port, "mount/unmount", {"mountPoint": mp})
                    except RuntimeError:
                        pass  # "mount not found" once the kernel mount is gone — fine
        if port is not None:
            _stop_serve_for(port, old_remote)
    except Exception:
        logger.warning("force-detach of builtin mount %r failed",
                       builtin.get("name"), exc_info=True)

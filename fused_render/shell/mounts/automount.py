"""Cleanup for the retired builtin mounts.

The app used to ship bundled content as read-only zip mounts attached at
startup (D123 `learn`, D227 `sessions`, each an rclone `:archive:` over a zip
in the bundle). Both are retired — learn moved to the community catalog
(D419), and the sessions inbox was superseded by the native Tasks/Schedule
surface (routers/claude_sessions.py reads ~/.claude/projects directly). No
builtin mounts remain.

What must survive their retirement is the prune: installs that ran an older
version still carry a `builtin: <name>` record in mounts.json, and
delete_mount refused builtin records for as long as the concept existed — so
without this startup pass the stale record strands forever as a broken mount
in the UI (the exact failure D419's prune was added to prevent).
"""

import logging

from .store import _ismount, _store_lock, _write, mountpoint

logger = logging.getLogger(__name__)


def prune_builtin_mounts() -> None:
    """Remove every mount record carrying a `builtin` marker — all builtins
    are retired, so any such record is a leftover from an older version.

    Same lock discipline the old upsert path used: the store
    read-modify-write happens entirely under `_store_lock`, and the
    rcd-touching force-detach (which can block for a full rc timeout) is only
    PLANNED there and executed after the `with` block. Never raises — a
    storage failure here must not break the user's own mounts."""
    from fused_render.shell.mounts import list_mounts
    try:
        detach_targets: list[tuple[dict, str]] = []
        with _store_lock:
            mounts = list_mounts()
            retired = [m for m in mounts if m.get("builtin")]
            if retired:
                retired_ids = {id(m) for m in retired}
                _write([m for m in mounts if id(m) not in retired_ids])
                for m in retired:
                    logger.info("dropped retired builtin mount record %r (%r)",
                                m.get("name"), m.get("builtin"))
                    # The live mount and its serve are keyed to the remote the
                    # record carried.
                    detach_targets.append((m, m.get("remote", "")))
        for target in detach_targets:
            _force_detach_builtin_mount(*target)
    except Exception:
        logger.exception("pruning retired builtin mount records failed")


def _force_detach_builtin_mount(builtin: dict, old_remote: str) -> None:
    """Best-effort unmount of a retired builtin's mountpoint if rcd (or the
    kernel) still has one live from a prior server run, plus a stop of the
    HTTP serve keyed to `old_remote` (rcd shares ONE VFS between a mount and
    its serve — tearing down the mount leaves the serve wedged, and
    sync_serves would then reuse the wedged serve instead of noticing the
    remote is gone).

    Swallows everything: a failed detach/stop just means whatever is still
    there lingers until the next restart — never worse than doing nothing.

    BUGBOT: detach_mount's default (force=False) deliberately leaves a
    non-busy failure (rcd down but a kernel mount survives, a busy-retry
    that still fails, ...) in place — "failing loudly beats corrupted
    reads" is the right call for an explicit user unmount, but it defeats
    the very point of THIS call. force=True escalates every dead end to
    _force_unmount instead.

    BUGBOT: force=True alone still isn't enough — detach_mount only
    escalates to _force_unmount when the rc `mount/unmount` call itself
    FAILS; it never re-checks os.path.ismount after a call that reports
    success. reconnect_mount already guards against exactly this on macOS
    (rc can report success while the kernel NFS mount lingers) — mirror
    that same re-check here.

    BUGBOT: _force_unmount operates purely at the kernel level (umount /
    diskutil) — it never tells rcd anything, so a successful force-unmount
    can leave rcd's OWN mount/listmounts bookkeeping still claiming the
    mountpoint, which run_automount treats as the split-brain case. Mirror
    reconnect_mount's follow-up rc mount/unmount call purely to clear rcd's
    bookkeeping (a "mount not found" failure at that point is expected and
    fine, since the kernel mount is already gone)."""
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

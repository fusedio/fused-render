"""Background health monitor: polls mounted paths for wedged/disconnected
state, logs episodes for the UI, and owns run_automount/startup plus the
full daemon-restart-and-remount orchestration (restart_rcd)."""

import collections
import json
import logging
import os
import re
import threading
import time

from .access import serves_path
from .automount import BUILTIN_MOUNTS, ensure_builtin_mounts, set_builtin_ready
from .lifecycle import attach_mount, sync_serves
from .rcd import _copytruncate_rcd_log, _rcd_lock
from .store import _ismount, mountpoint

logger = logging.getLogger(__name__)


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
    from fused_render.shell.mounts import (
        _ensure_rcd_locked,
        _kill_current_rcd,
        detach_mount,
        list_mounts,
        run_automount,
    )
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


HEALTH_POLL_INTERVAL = 20.0  # seconds between health ticks


_health_log_lock = threading.Lock()


_health_events: "collections.deque[dict]" = collections.deque(maxlen=100)


_health_event_seq = 0  # next event id; only mutated under _health_log_lock


_health_episodes: "dict[str, dict]" = {}


_health_thread: "threading.Thread | None" = None


_health_started = threading.Lock()  # guards start_health_monitor idempotency


_NEEDS_RECONNECT = ("disconnected", "stale")


def _health_emit(mount_id: str, name: str, kind: str, detail: str = "") -> None:
    """Append one event to the bounded log under its lock. kind is one of
    "disconnected" | "reconnected" | "reconnect_failed". ts is epoch seconds —
    wall-clock is fine for a UI log; ordering is by the monotonic id, not ts."""
    from fused_render.shell import mounts as _mounts_pkg
    from fused_render.shell.mounts import _health_events
    with _health_log_lock:
        _mounts_pkg._health_event_seq += 1
        _health_events.append({
            "id": _mounts_pkg._health_event_seq,
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
    from fused_render.shell.mounts import _health_episodes, list_mounts, mount_state, mounted_paths
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
    from fused_render.shell.mounts import (
        _health_episodes,
        _health_events,
        list_mounts,
        mount_state,
        mounted_paths,
    )
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
    # Upsert the builtin mounts (learn, sessions) BEFORE the snapshot below: a
    # fresh install has zero user mounts, and skipping the attach loop below
    # would otherwise skip the builtins' very first mount too.
    from fused_render.shell.mounts import list_mounts, mounted_paths
    # Drop builtin readiness before the force-detach+remount below: a mount that
    # survived from a previous run must not read as ready during the empty
    # window between detach and re-attach (builtin_mount_ready). Re-set True
    # per builtin only once its own attach_mount succeeds, so True always means
    # a mount THIS run attached.
    for name in BUILTIN_MOUNTS:
        set_builtin_ready(name, False)
    ensure_builtin_mounts()
    mounts = list_mounts()
    if mounts:
        live = mounted_paths()
        for m in mounts:
            mp = mountpoint(m)
            if mp in live and not _ismount(mp):
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
            elif m.get("builtin"):
                set_builtin_ready(m["builtin"], True)
        # Mounts that survived a server restart skip attach_mount above, so
        # their HTTP serves (lost with any rcd restart) get re-ensured here.
        sync_serves()
    elif os.path.exists(serves_path()):
        # BUGBOT: `mounts` came back empty, which usually means a genuinely
        # mount-less install (nothing to sync, and serves_path() was never
        # written — skipping here keeps a fresh install from gaining a
        # home_dir()/serves.json write it never needed). But it can ALSO
        # mean ensure_builtin_mounts above just removed a builtin record
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
    from fused_render.shell.mounts import run_automount
    _copytruncate_rcd_log()
    threading.Thread(target=run_automount, daemon=True, name="mounts-automount").start()

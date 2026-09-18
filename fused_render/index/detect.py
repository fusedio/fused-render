"""Change detection on home-page focus.

Home search is index-backed and global (routers/index.py's `/api/index/rank`),
so a file dropped anywhere under a scan root — a browser download landing in
`~/Downloads`, say — is invisible to it until something scans that root again.
Every existing trigger structurally misses this case: the startup scheduler
runs once per boot, `index/freshness.py`'s folder-open check only sees the
folder being LISTED (and its own docstring already names the deeper bound —
a root's mtime moves only on its DIRECT entries, so a change three levels
down never makes the root look stale), and `index_touch.py` only fires for
mutations this app itself makes.

The macOS FSEvents journal does not share that blind spot: it records changes
at ANY depth under a volume, which `index/fsevents.py:hint()` already replays
— today only from inside a scan (`index/scan.py`'s "checking for changes"
phase). This module runs that same replay standalone, triggered by the home
page regaining focus, and only starts a scan when it reports an actual
change — mirroring how `index/freshness.py` holds the folder-open policy
while `server/routers/index.py` holds only the wiring (the thread, the
`_index_job_wake` nudge). See SPEC-focus-change-detection.md.

`hint()`'s three outcomes, load-bearing for everything below (confirmed in
fsevents.py, do not re-litigate):

* `None` — "cannot tell": non-darwin, no saved journal position, a
  multi-volume root, a UUID mismatch, dropped/purged history, a ctypes
  failure, a 20s timeout, or a change set so large (>=200_000 events) that
  `_replay` gave up. MUST NOT be read as "nothing changed" — it is a no-op
  here, not a "nothing to do".
* `(set(), [])` — the journal really did report nothing under this root.
  Also a no-op, but for the opposite reason: this is the case the whole
  design exists to detect the ABSENCE of, cheaply, without a scan.
* anything else — a real change. Kick off the ordinary incremental scan of
  that root; `run_scan(..., incremental=True)` already consults the same
  journal itself (capturing its own position first) and visits only what
  moved, so this is seconds of work, not a second walk.

`hint()` is read-only (`fsevents.save_state` is called only from
`index/scan.py`, nowhere else), so calling it here any number of times never
consumes journal state — it always measures from the last successful scan of
that root, same as if a scan itself had just checked.
"""
import logging
import threading
import time

from fused_render.index import freshness, fsevents, runner
from fused_render.index.config import IndexConfig
from fused_render.index.ignore import MountGuard, ignored_for_index, is_inside_leaf_dir
from fused_render.shell import index_gate

logger = logging.getLogger(__name__)

# How long the user must have been away from the home page before a focus
# event is worth acting on. The signal this module exists for is "went away
# and did something else" (a browser download, a Finder move) — not "alt-tabbed
# between two of this app's own windows", which fires a `visibilitychange` just
# as readily but changed nothing on disk. Enforced server-side: the client's
# hidden-duration is an input this module decides with, never a decision the
# client already made — a stale tab or a modified client must not be able to
# force a check by just claiming a bigger number.
MIN_HIDDEN_S = 30.0

# Floor between DETECT CHECKS of one root — i.e. between journal replays this
# trigger itself performs — kept in this module the same bounded,
# no-eviction, per-root dict `routers/index._freshness_checked` uses (root
# count is always a handful, so nothing here needs an eviction policy).
#
# This is the CHECKS floor, the trigger-specific twin of
# `routers/index.FRESHNESS_CHECK_S`; `freshness.MIN_INTERVAL_S` (below) is
# the SCANS floor, shared by every trigger via `scans.json`. The two answer
# different questions and both apply: a browser tab that keeps stealing and
# losing focus (a video call, a second monitor) must not turn into a journal
# replay on every transition, independently of whether any of those replays
# would actually go on to start a scan.
#
# Unlike FRESHNESS_CHECK_S, this number is not chosen to keep a POLLING
# cadence comfortably above MIN_INTERVAL_S (60s) — a focus event is not a
# poll, it fires only on a real visibility transition, which is rarely more
# often than once every few seconds even for a flappy window manager. 30s is
# instead sized against the cost of the thing it paces: a quiet replay is
# 0.1-2.9s of the journal (freshness.py's own measurement, scaled to a
# shorter window since focus events are frequent by comparison to a scan
# interval), cheap enough to allow well under a minute between checks without
# it becoming a background cost anyone would notice.
DETECT_INTERVAL_S = 30.0

# root -> when it was last checked by this trigger. Bounded by the number of
# configured scan roots (a handful); no eviction needed, same shape as
# `routers/index._freshness_checked`.
_detect_checked: dict = {}
_detect_checked_lock = threading.Lock()


def _detect_due(root: str, now: float) -> bool:
    with _detect_checked_lock:
        last = _detect_checked.get(root)
        if last is not None and (now - last) < DETECT_INTERVAL_S:
            return False
        _detect_checked[root] = now
        return True


def _hint(cfg: IndexConfig, root: str):
    """`fsevents.hint`, defensively. `hint` does `int(st["event_id"])` with
    no guard of its own — `index/scan.py` deliberately lets that raise into a
    run's own failure handler, but this caller has no run to fail into.
    Housekeeping must never become the answer (the same rule
    `routers/git_repos._note_tab_opened` follows): any exception here is
    logged at debug and treated exactly like `hint` answering `None`."""
    try:
        return fsevents.hint(cfg, root)
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("fsevents.hint failed for %s", root, exc_info=True)
        return None


def _filter_hint(cfg: IndexConfig, guard: MountGuard, forced: set, subtrees: list):
    """Drop everything from a raw hint that a real scan would never index
    anyway, or that is this app's own doing — the exact per-path filter
    `scan._run_fsevents` applies to these same two collections
    (`ignored_for_index(..., tree=True) or guard.blocks(...) or
    is_inside_leaf_dir(...)`), reused here rather than re-derived because
    `fsevents.hint` itself applies none of it: it only prefix-filters by
    root (fsevents.py), so its raw output includes every write anywhere
    under `root` — including this app's own state home and whatever the
    user's ignore list (or the built-in defaults — `.cache`, `.fused`, and,
    as of SPEC-focus-change-detection.md's review, `~/Library`) already
    excludes from the index.

    Load-bearing, not cosmetic: on a real `~` root, skipping this step made
    `hinted` non-empty on nearly every check — the scan's own writes under
    `~/.fused-render` and macOS's constant `~/Library` churn (Safari/Mail
    caches, saved app state, Spotlight) both land in the raw hint — which
    made the design's stated quiet case (`(set(), [])`, no scan) effectively
    unreachable despite `_check_root` handling it correctly once reached.
    `tree=True` because, same as the journal-driven call in scan.py, a
    hinted path arrives without its ancestors having been checked."""
    def _keep(p: str) -> bool:
        return not (ignored_for_index(cfg.rules, p, tree=True)
                    or guard.blocks(p) or is_inside_leaf_dir(p))
    return {p for p in forced if _keep(p)}, [p for p in subtrees if _keep(p)]


def _check_root(cfg: IndexConfig, root: str, now: float) -> bool:
    """Whether a scan of `root` was started. Every gate ordered cheapest
    first, same discipline as `freshness.note_folder_opened`: the in-memory
    pacing checks and the mount/active-run checks (pure string/local-file
    work, no syscall on `root` itself) all run before the journal replay —
    the most expensive step here, and the only one that touches `root` at
    all (via `fsevents.device_uuid`'s `os.stat`)."""
    if not _detect_due(root, now):
        return False
    last = runner.last_scan(cfg, root)
    if last is not None and (now - last) < freshness.MIN_INTERVAL_S:
        return False
    # BEFORE any kernel syscall on `root` — same rule, same ordering,
    # `freshness.note_folder_opened` follows for the identical reason:
    # `os.stat` on a wedged rclone/NFS mount blocks the calling thread
    # forever, and `_hint` below reaches exactly that syscall
    # (fsevents.device_uuid). Currently latent for this caller (a mount-backed
    # root has no saved fsevents state, so `hint` returns `None` at its own
    # first check, before it opens anything) but the ordering must be correct
    # regardless of whether a root has state today.
    guard = MountGuard(mounts_dir=runner._mounts_dir())
    if guard.blocks(root):
        return False
    # A focus event must not be the thing that CANCELS an in-flight scan of
    # this root. `runner.start` (below) treats a live run under a matching
    # `ignore_sig` as a join, but a DIFFERENT sig — exactly the case right
    # after an ignore-list edit, while the reconciling rescan it triggered is
    # still walking — makes `start` supersede it: cancel the live run and
    # spawn a new one. Losing that walk's progress to a tab-back is the same
    # mistake `freshness.note_folder_opened` refuses for the folder-open
    # trigger ("a triggered scan must not be the thing that discovers a
    # mismatch"); this trigger operates at the root level already, so the
    # same refusal applies directly rather than needing translation.
    if runner.active_run(cfg, root) is not None:
        return False
    hinted = _hint(cfg, root)
    if hinted is None:
        return False
    forced, subtrees = _filter_hint(cfg, guard, *hinted)
    if not forced and not subtrees:
        return False
    try:
        # `runner.start` is the backstop for every gate a scan-starting
        # caller must honour — the indexing-pref/FDA gate, the MountGuard /
        # never-a-mount / never-"/" refusals — each raising ValueError. This
        # module does not re-derive any of them; it only needs to absorb the
        # refusal the same way `freshness.note_folder_opened`'s caller never
        # has to check them separately.
        result = runner.start(cfg, root)
    except ValueError:
        return False
    # `start` JOINS a live run instead of spawning one when the ignore_sig
    # matches — and the `active_run` check above is not atomic with this
    # call, so that race window is real even though it is now rare. A joined
    # run is not something THIS check started: it was already going to
    # finish regardless of this focus event, so reporting it as "started"
    # would make the caller's log ("home-page focus found changes...
    # rescanning %s") and its `_wake_index_job_bridge()` nudge fire for a
    # scan this trigger had no hand in — misleading exactly while a scan is
    # already running, which a full home scan makes a common window.
    if result.get("already_running"):
        return False
    return True


def note_home_focused(cfg: IndexConfig, roots, hidden_s: float,
                      now: float | None = None) -> list:
    """The home page regained focus after being hidden `hidden_s` seconds:
    check every configured root's change journal, starting an incremental
    scan for any root the journal says actually changed.

    Returns the roots a scan was started for (possibly empty — the common
    case, since this module exists specifically to make the quiet path
    cheap). Never raises: this is an accelerator, not a request any caller
    should have to handle failing."""
    now = time.time() if now is None else now
    if hidden_s < MIN_HIDDEN_S:
        return []
    # Same single gate every scan-starting trigger checks before doing any
    # work — `index_gate.indexing_blocked()` covers both the user pref and
    # the macOS Full Disk Access grant. `runner.start` re-checks this itself
    # as a backstop (see `_check_root`), but failing here, before a journal
    # replay per root, is what keeps a disabled pref from paying that cost.
    if not index_gate.indexing_allowed():
        return []
    started = []
    for root in roots or []:
        try:
            if _check_root(cfg, root, now):
                started.append(root)
        except Exception:  # noqa: BLE001 - housekeeping must never surface
            logger.exception("could not run focus-change detection for %s", root)
    return started

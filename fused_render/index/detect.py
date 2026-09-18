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


def _check_root(cfg: IndexConfig, root: str, now: float) -> bool:
    """Whether a scan of `root` was started. Every gate ordered cheapest
    first, same discipline as `freshness.note_folder_opened`: the two
    in-memory pacing checks (below) are free, so the journal replay — the
    most expensive step here — is unreachable for a root that was already
    going to be refused."""
    if not _detect_due(root, now):
        return False
    last = runner.last_scan(cfg, root)
    if last is not None and (now - last) < freshness.MIN_INTERVAL_S:
        return False
    hinted = _hint(cfg, root)
    if not hinted:
        # Covers both `None` ("cannot tell") and a hint whose two collections
        # (forced dirs, walk subtrees) are both empty ("nothing changed") —
        # `bool(hinted)` is false for `None`, and `any(hinted)` is false for
        # `(set(), [])` since neither piece is truthy. Both are no-ops here.
        return False
    forced, subtrees = hinted
    if not forced and not subtrees:
        return False
    try:
        # `runner.start` is the backstop for every gate a scan-starting
        # caller must honour — the indexing-pref/FDA gate, the MountGuard /
        # never-a-mount / never-"/" refusals — each raising ValueError. This
        # module does not re-derive any of them; it only needs to absorb the
        # refusal the same way `freshness.note_folder_opened`'s caller never
        # has to check them separately.
        runner.start(cfg, root)
    except ValueError:
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

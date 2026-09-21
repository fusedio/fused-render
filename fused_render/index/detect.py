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

WHAT THIS USED TO DO, AND WHY IT DOESN'T ANYMORE (measured 2026-09-19, do not
re-litigate — see SPEC-focus-change-detection.md's errata and DECISIONS.md):
the original design replayed the macOS FSEvents journal STANDALONE
(`fsevents.hint`) on focus, and only started a scan when that replay reported
a change. That inverts on a real `~` root. `hint`'s replay
(`fsevents._replay`) gives up and returns `None` — "cannot tell" — past a 20s
timeout or a 200_000-event cap, and `None` MUST be treated as a no-op, never
as "nothing changed". Measured against this machine's real home root: right
after a scan, a check costs 48ms for 123 events; 7.2 hours after the last
scan it cost 11.9s for 199,943 events, sitting on the cap, and a call in the
same minute returned `None` at 5.8s having answered nothing. `~` generates
about 28,000 FSEvents/hour, so the replay stops being able to answer at all
after roughly 7 hours without a scan — which is exactly the situation (a
long-idle root) where a new download is most likely to be missing. The
longer since the last scan, the more certain this design was to burn 6-12s
of background work and then do nothing.

So this module no longer asks the journal anything itself. On a qualifying
focus event it simply starts the ORDINARY incremental scan of a stale-enough
root — `run_scan(..., incremental=True)` (index/scan.py) already tries the
same journal replay internally, in a background thread, racing the dir-cache
read; when the replay succeeds it still visits only what moved, and when it
answers `None` the scan falls back to its own cache-shortcut walk (mtime
compares against the cached signature, one `scandir` per directory, no
per-file rehash for anything unchanged) rather than to a bare "no answer".
That fallback walk was measured directly, forcing exactly the `None` case
against a same-day copy of this machine's real production cache for
`/Users/iamsdas` (~78,700 directories): 4.4s wall-clock, bounded by directory
count rather than event count, and it always leaves the index actually
current — the one thing the standalone-replay design's quiet path never
guaranteed once the journal stopped answering. See DECISIONS.md for the full
measurement.

This module now holds only the STALENESS policy for the focus trigger —
mirroring how `index/freshness.py` holds the folder-open policy while
`server/routers/index.py` holds only the wiring (the thread, the
`_index_job_wake` nudge). "Stale enough to be worth it" used to be answered
entirely by three time-since-last-scan floors, with no evidence of change at
all — see PHASE 1 below for why that was still wrong.

PHASE 1 (this round): the pure-staleness design above has no evidence of
change, only "how long since the last scan" — and the two floors that answer
that question (`freshness.MIN_INTERVAL_S`, 60s; `FOCUS_STALE_S`, 300s) are
both correct against the question they answer (stop a flappy tab-away from
restarting a full scan every minute) and both wrong against a real report: a
scan finished, a file landed directly under the root 12s later, the user
switched back and searched 30s after that — 42s total, short of both floors,
and the file could not be found. Fixing this needs a way to distinguish "42s
since the last scan, nothing happened" from "42s since the last scan, and
something did" — which the pure-staleness design structurally cannot do.

The scan root's own mtime supplies that evidence for free, for the common
case: a file created (or removed, or renamed) DIRECTLY under the root moves
the root's own mtime, so a single `os.stat(root)` compared against what the
index recorded for `root` (`freshness.is_newer_than_indexed` — the same
comparison `note_folder_opened` already makes for a listed folder, reused
rather than re-derived) answers "did anything change here" with no journal
replay at all. This is the SAME documented depth-1 bound `note_folder_opened`
and `git_repos._note_tab_opened` already live with: a file two levels down
does not move the root's mtime, so this evidence check has nothing to say
about it (that gap is Phase 2, tracked in DECISIONS.md and SPEC's Phase 2
section — deliberately not built this round).

With that evidence, `MIN_INTERVAL_S`/`FOCUS_STALE_S` no longer apply — both
exist to stop a scan nobody has shown is needed, and this is exactly a scan
we have proof is needed. Only a small pacing floor remains,
`EVIDENCE_MIN_INTERVAL_S`, sized to stop a scan storm (repeated evidence
against a churning root, or a race between two focus events) rather than to
judge staleness. Without evidence, `MIN_INTERVAL_S` and `FOCUS_STALE_S`
still apply exactly as before — that fallback also covers Phase 2's gap and
any case `os.stat` itself cannot answer (the root vanished, a permission
error): both degrade to "no evidence", never to an exception.
"""
import logging
import os
import threading
import time

from fused_render.index import freshness, runner
from fused_render.index.config import IndexConfig
from fused_render.index.ignore import MountGuard
from fused_render.shell import index_gate

logger = logging.getLogger(__name__)

# How long the user must have been away from the home page before a focus
# event is worth acting on. The signal this module exists for is "went away
# and did something else" (a browser download, a Finder move) — not "alt-tabbed
# between two of this app's own windows", which fires a `visibilitychange` just
# as readily but changed nothing on disk.
#
# Enforced server-side because the server cannot verify a client-reported
# duration at all, not because a bigger number is somehow dangerous — the
# gate below is `hidden_s < MIN_HIDDEN_S`, so a LARGER claimed value is
# exactly what passes it. What actually bounds a client (honest or modified)
# spamming this endpoint with a huge `hidden_s` on every keystroke is the
# PACING floors, not this one: `DETECT_INTERVAL_S` collapses repeat checks of
# one root in-memory, and `freshness.MIN_INTERVAL_S`/`FOCUS_STALE_S` (below)
# refuse to start a scan at all unless the root is actually stale. Losing
# either of those floors would be the real hole; this constant only screens
# out a genuinely-brief transition (two of this app's own windows trading
# focus) that a legitimate client would never claim a large duration for in
# the first place.
MIN_HIDDEN_S = 30.0

# Floor between DETECT CHECKS of one root, kept in this module the same
# bounded, no-eviction, per-root dict `routers/index._freshness_checked`
# uses (root count is always a handful, so nothing here needs an eviction
# policy).
#
# This is the CHECKS floor, the trigger-specific twin of
# `routers/index.FRESHNESS_CHECK_S`; `freshness.MIN_INTERVAL_S` (below) is
# the SCANS floor, shared by every trigger via `scans.json`. The two answer
# different questions and both apply: a browser tab that keeps stealing and
# losing focus (a video call, a second monitor) must not turn this in-memory
# check into a function call on every transition, independently of whether
# `freshness.MIN_INTERVAL_S` would go on to refuse the scan anyway.
#
# NOTE this constant's justification changed along with the redesign above:
# it used to pace a standalone journal replay (0.1-2.9s on a fresh root, far
# more once stale — see the module docstring). It no longer paces anything
# expensive: with the journal replay gone from this module, everything
# `_check_root` does before `runner.start` is in-memory or a local small-file
# read, so this floor is now pure request-collapsing (one flappy window
# shouldn't call `runner.last_scan`/`runner.active_run` fifty times a
# second), not a cost control. 30s is kept because it is still comfortably
# below `FOCUS_STALE_S` (below) — the floor that actually decides whether a
# scan starts — with room for a focus event to be the one that notices a
# root has gone stale soon after it clears.
DETECT_INTERVAL_S = 30.0

# How long a root must have gone unscanned before a FOCUS event is allowed to
# be the thing that rescans it. This is deliberately its OWN constant, not a
# reuse of `freshness.MIN_INTERVAL_S` (below): the two answer different
# questions. `MIN_INTERVAL_S` answers "is this root due for a rescan at all"
# for every trigger equally (the startup scheduler, a manual button, a
# folder-open); this one answers "is it worth THIS trigger, specifically,
# firing" — a focus event is cheap to observe (every tab switch) but not free
# to act on, and code review correctly flagged that reusing the 60s
# `MIN_INTERVAL_S` here meant a user who kept tabbing away for just over 30s
# (`MIN_HIDDEN_S`) and back could trigger a full incremental scan roughly
# once a minute, indefinitely, for as long as they kept doing it.
#
# A full incremental walk of a real, large home root was measured at 4.37s
# for 78,717 directories (DECISIONS.md, "2026-09-19 — standalone replay
# removed"). At this floor's 300s (5 minute) period, that walk's worst-case
# duty cycle is 4.37 / 300 ≈ 1.5% — comfortably background work even if a
# user managed to keep a root exactly on this boundary indefinitely, versus
# up to ~7% at the old 60s floor (4.37 / 60). `freshness.MIN_INTERVAL_S` is
# kept as the separate, lower floor it always was — it still guards every
# OTHER trigger, and nothing here removes it as the shared "just scanned,
# leave it alone" backstop this trigger also honours (see `_check_root`).
FOCUS_STALE_S = 300.0

# Floor between scans of a root WHEN THIS TRIGGER HAS POSITIVE EVIDENCE the
# root changed (see `_has_root_evidence`). Deliberately much smaller than
# `MIN_INTERVAL_S`/`FOCUS_STALE_S` above: those two exist to stop a scan
# nobody has shown is needed, so they do not apply once evidence says
# otherwise (see `_check_root`) — but "evidence" alone is not unconditional
# either. Without SOME floor, a root whose mtime keeps moving (a churning
# directory, or two focus events racing the same evidence check) would start
# a new incremental scan on every single check, which is the exact scan-
# storm failure mode `MIN_INTERVAL_S` was invented to prevent for every other
# trigger. 10s is not a staleness judgment (unlike the two floors above) —
# it only needs to outlast one scan-start-to-next-check cycle, which is why
# it can be an order of magnitude below `MIN_INTERVAL_S` without reopening
# the flappy-tab problem `FOCUS_STALE_S` was added to fix: that problem was
# about a NO-evidence root being rescanned on a timer regardless of whether
# anything changed, not about a root evidence keeps legitimately re-flagging.
EVIDENCE_MIN_INTERVAL_S = 10.0

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


def _has_root_evidence(cfg: IndexConfig, root: str) -> bool:
    """Positive evidence `root` itself changed since the index last recorded
    it: one `stat` of the root, compared against `dirs.parquet`'s row for it
    via `freshness.is_newer_than_indexed` — the exact comparison
    `note_folder_opened` already makes for a listed folder, reused here
    rather than re-derived (see this module's docstring, PHASE 1).

    Depth-1 bound: only sees a change to the root's DIRECT entries — the
    same documented limit `note_folder_opened`/`git_repos._note_tab_opened`
    already live with. A deeper change (Phase 2, not built this round)
    reads as no evidence here, same as `root` vanishing or an OSError from
    `os.stat` — none of those are a reason to raise out of a housekeeping
    check, only to fall back to the staleness floors below."""
    try:
        disk_ns = os.stat(root).st_mtime_ns
    except OSError:
        return False
    return freshness.is_newer_than_indexed(cfg, root, disk_ns)


def _check_root(cfg: IndexConfig, root: str, now: float) -> bool:
    """Whether a scan of `root` was started. Every gate ordered cheapest
    first, same discipline as `freshness.note_folder_opened`: the in-memory
    pacing check and the mount/active-run checks (pure string/local-file
    work, no syscall on `root` itself) all run before `runner.start`, the
    only thing here that ever touches `root`'s filesystem — and it orders
    its OWN guard check before its own syscalls (index/runner.py's `start`
    docstring), so this function never has to reach past that guard itself."""
    if not _detect_due(root, now):
        return False
    last = runner.last_scan(cfg, root)
    if _has_root_evidence(cfg, root):
        # Evidence means both staleness floors below are answering the wrong
        # question ("do we need to check" — we already know) — only the
        # small storm floor still applies (see its own comment).
        if last is not None and (now - last) < EVIDENCE_MIN_INTERVAL_S:
            return False
    else:
        # No evidence (nothing to compare, the root is unchanged at depth 1,
        # or Phase 2's deeper-change gap): fall back to the pure-staleness
        # floors exactly as before Phase 1. Two floors, kept separate on
        # purpose (see each constant's own comment): `MIN_INTERVAL_S` is the
        # shared "just scanned, leave it alone" backstop every trigger
        # honours; `FOCUS_STALE_S` is this trigger's OWN, higher bar for how
        # stale a root must be before a focus event specifically is allowed
        # to be the thing that rescans it. Checking both (rather than only
        # the larger one) keeps this trigger correct even if a future change
        # ever lowered `FOCUS_STALE_S` below `MIN_INTERVAL_S`.
        if last is not None and (now - last) < freshness.MIN_INTERVAL_S:
            return False
        if last is not None and (now - last) < FOCUS_STALE_S:
            return False
    # Pure string comparison against roots resolved at construction time — no
    # syscall on `root` (MountGuard's own docstring) — so this cheaply short
    # circuits the common "not mount-backed" case before even reaching
    # `runner.start`'s own (also-guarded) checks.
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
    # would make the caller's log and its `_wake_index_job_bridge()` nudge
    # fire for a scan this trigger had no hand in.
    if result.get("already_running"):
        return False
    return True


def note_home_focused(cfg: IndexConfig, roots, hidden_s: float,
                      now: float | None = None) -> list:
    """The home page regained focus after being hidden `hidden_s` seconds:
    start the ordinary incremental scan of every configured root that is
    stale enough to be worth it.

    Returns the roots a scan was started for (possibly empty — most calls
    land inside `DETECT_INTERVAL_S`/`FOCUS_STALE_S`/`freshness.MIN_INTERVAL_S`
    of the last one and do nothing). Never raises: this is an accelerator,
    not a request any caller should have to handle failing."""
    now = time.time() if now is None else now
    if hidden_s < MIN_HIDDEN_S:
        return []
    # Same single gate every scan-starting trigger checks before doing any
    # work — `index_gate.indexing_allowed()` covers both the user pref and
    # the macOS Full Disk Access grant. `runner.start` re-checks this itself
    # as a backstop (see `_check_root`), but failing here, before touching
    # any root, is what keeps a disabled pref from paying even that cost.
    #
    # Wrapped the same as every per-root check below: this function's own
    # docstring promises "never raises", and `index_gate.indexing_allowed()`
    # is housekeeping the same way `runner.start`/`runner.active_run` are —
    # it must not be the one thing here allowed to break that contract just
    # because it happens to run before the per-root loop starts.
    try:
        allowed = index_gate.indexing_allowed()
    except Exception:  # noqa: BLE001 - housekeeping must never surface
        logger.exception("could not check whether indexing is allowed")
        return []
    if not allowed:
        return []
    started = []
    for root in roots or []:
        try:
            if _check_root(cfg, root, now):
                started.append(root)
        except Exception:  # noqa: BLE001 - housekeeping must never surface
            logger.exception("could not run focus-change detection for %s", root)
    return started

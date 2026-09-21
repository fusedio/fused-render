"""Change detection on home-page focus.

See SPEC-focus-change-detection.md and its errata, and DECISIONS.md. The
original design replayed the macOS FSEvents journal standalone
(`fsevents.hint`) on focus and only started a scan when that replay reported
a change. That inverts on a real `~` root: `hint`'s replay gives up (a 20s
timeout, or a 200_000-event cap) and returns `None` — "cannot tell" — more
and more often the longer it has been since the last scan, which is exactly
when a change is most likely to be missing. `index/detect.py` no longer asks
the journal anything itself: on a qualifying focus event it just starts the
ORDINARY incremental scan of a stale-enough root (which still consults the
journal internally, and still falls back to a bounded cache-shortcut walk
when the journal can't answer).

Style follows tests/test_index_freshness.py: assert on the trigger (did
`runner.start` get called, for which root), never on a real scan.
"""
import logging
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index import detect, runner
from fused_render.index.config import IndexConfig
from fused_render.index.store import Sink, compact
from fused_render.shell import index_gate

NOW = 1_000_000.0
NS = 1_000_000_000


@pytest.fixture()
def spawned(monkeypatch):
    """runner.start recorded instead of spawning a worker."""
    calls = []

    def fake_start(cfg, root, full=False):
        calls.append({"root": root, "full": full})
        return {"run_id": "r1", "root": root}

    monkeypatch.setattr(runner, "start", fake_start)
    return calls


@pytest.fixture(autouse=True)
def _detect_state_reset():
    """`detect._detect_checked` is module-level, keyed by root string, the
    same bounded-no-eviction shape as routers/index._freshness_checked — so a
    root name reused across tests would otherwise carry a stamp from a
    previous test's clock into this one."""
    detect._detect_checked.clear()
    yield
    detect._detect_checked.clear()


@pytest.fixture(autouse=True)
def _indexing_allowed(monkeypatch):
    """Every test gets an unblocked gate by default; the one test that wants
    the pref off overrides this."""
    monkeypatch.setattr(index_gate, "indexing_blocked", lambda: "")


def _cfg(tmp_path):
    return IndexConfig(dir=str(tmp_path / "ix"))


def _root(tmp_path):
    d = tmp_path / "root"
    d.mkdir()
    return runner.canonical_root(str(d))


def _no_recent_scan(monkeypatch):
    monkeypatch.setattr(runner, "last_scan", lambda cfg, root: None)


def _indexed_root(tmp_path, root, mtime_s: float) -> IndexConfig:
    """A real dirs.parquet recording `root` itself as last scanned with
    mtime `mtime_s` (seconds) — the Phase-1 evidence check's "what did the
    index think" half. Mirrors tests/test_index_freshness.py's `_index`."""
    cfg = _cfg(tmp_path)
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    sink.add(root, "s", ("sig", [], 0, int(mtime_s * NS), 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


# -- the core contract: stale enough -> scan, not stale enough -> nothing -------

def test_a_stale_root_starts_the_ordinary_incremental_scan(tmp_path, monkeypatch, spawned):
    """The replacement for "hint says something changed": no journal check
    of its own at all, just start `runner.start` — the scan's own internal
    journal replay (or its fallback walk) is what actually decides what gets
    visited."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


def test_this_module_never_calls_fsevents_hint_itself(tmp_path, monkeypatch, spawned):
    """The whole point of the redesign: this module must not replay the
    journal standalone any more — that's what made the quiet path
    unreliable on a real, long-idle home root in the first place."""
    from fused_render.index import fsevents

    def boom(cfg, root):
        raise AssertionError("detect.py must not call fsevents.hint directly")

    monkeypatch.setattr(fsevents, "hint", boom)
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


def test_a_root_the_journal_cannot_answer_for_still_gets_scanned(
        tmp_path, monkeypatch, spawned):
    """The real bug this redesign fixes: a root the journal can't answer for
    (a 7-hour-idle `~`, in the field) must not be a no-op just because a
    standalone replay would have returned None. This module doesn't call
    `hint` at all any more, so it has no way to be fooled by its answer —
    asserted here by making `runner.start` itself (the ordinary scan
    machinery) the only thing that decides anything, with no fsevents
    involvement in this module whatsoever."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


# -- pacing ---------------------------------------------------------------------

def test_detect_interval_floor_refuses_a_second_call_inside_the_window(
        tmp_path, monkeypatch, spawned):
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    cfg = _cfg(tmp_path)
    assert detect.note_home_focused(
        cfg, [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert detect.note_home_focused(
        cfg, [root], detect.MIN_HIDDEN_S,
        now=NOW + detect.DETECT_INTERVAL_S - 1) == []
    # Still just the one call from the first, successful check.
    assert spawned == [{"root": root, "full": False}]


def test_detect_interval_floor_clears_after_the_window(tmp_path, monkeypatch, spawned):
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    cfg = _cfg(tmp_path)
    assert detect.note_home_focused(cfg, [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert detect.note_home_focused(
        cfg, [root], detect.MIN_HIDDEN_S,
        now=NOW + detect.DETECT_INTERVAL_S + 1) == [root]
    assert spawned == [{"root": root, "full": False}, {"root": root, "full": False}]


def test_freshness_min_interval_still_refuses_when_a_scan_just_ran(
        tmp_path, monkeypatch, spawned):
    """freshness.MIN_INTERVAL_S, read off scans.json via runner.last_scan, is
    the floor every OTHER trigger already respects — this one must too, or a
    focus event could rescan a root the scheduler or a manual button just
    finished. It is the LOWER of the two staleness floors this trigger
    checks (see test_focus_stale_s_* below for the higher, trigger-specific
    one) — a root freshly scanned by anything is refused regardless of which
    floor would otherwise bind."""
    from fused_render.index import freshness

    root = _root(tmp_path)
    monkeypatch.setattr(runner, "last_scan",
                        lambda cfg, r: NOW - freshness.MIN_INTERVAL_S + 1)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert spawned == []


def test_focus_stale_s_still_refuses_after_min_interval_s_has_cleared(
        tmp_path, monkeypatch, spawned):
    """Code review (medium): `_check_root` used to reuse the shared, 60s
    `freshness.MIN_INTERVAL_S` as its OWN staleness bar, so a root scanned
    61s ago (well past `MIN_INTERVAL_S`, well within `FOCUS_STALE_S`) was
    rescanned by this trigger alone — a 31s-hidden tab-away could burn a full
    incremental scan roughly once a minute, indefinitely. `FOCUS_STALE_S`
    (300s) is the trigger-specific floor that actually decides this now;
    `MIN_INTERVAL_S` having already cleared is not enough on its own."""
    from fused_render.index import freshness

    root = _root(tmp_path)
    last = NOW - freshness.MIN_INTERVAL_S - 1
    assert (NOW - last) < detect.FOCUS_STALE_S  # sanity: still inside the new floor
    monkeypatch.setattr(runner, "last_scan", lambda cfg, r: last)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert spawned == []


def test_focus_stale_s_no_longer_refuses_once_it_has_elapsed(
        tmp_path, monkeypatch, spawned):
    """This is now THE staleness threshold this trigger checks on top of the
    shared `freshness.MIN_INTERVAL_S` floor: with the standalone journal
    check gone, `FOCUS_STALE_S` (plus `DETECT_INTERVAL_S`'s in-memory
    pacing) is the entire answer to "is this root stale enough to be worth
    a FOCUS event specifically rescanning it"."""
    root = _root(tmp_path)
    monkeypatch.setattr(runner, "last_scan",
                        lambda cfg, r: NOW - detect.FOCUS_STALE_S - 1)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


# -- gates shared with every other trigger ---------------------------------------

def test_indexing_pref_off_is_a_no_op(tmp_path, monkeypatch, spawned):
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(index_gate, "indexing_blocked", lambda: "disabled")
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert spawned == []


def test_indexing_allowed_check_raising_does_not_escape(
        tmp_path, monkeypatch, spawned, caplog):
    """Code review (low): `index_gate.indexing_allowed()` used to sit outside
    the per-root try/except, so it could raise straight out of
    `note_home_focused` despite that function's own "Never raises"
    docstring. This must be swallowed exactly like a per-root failure is."""
    root = _root(tmp_path)

    def boom():
        raise RuntimeError("gate check blew up")

    monkeypatch.setattr(index_gate, "indexing_allowed", boom)
    with caplog.at_level(logging.ERROR):
        result = detect.note_home_focused(
            _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW)
    assert result == []
    assert spawned == []


def test_a_mount_backed_root_is_refused_via_runner_start(tmp_path, monkeypatch):
    """detect.py does not re-derive the MountGuard/FDA/isdir refusals —
    runner.start already raises ValueError for each, and this is the backstop
    that absorbs it."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)

    def refuse(cfg, root, full=False):
        raise ValueError(f"{root} is mount-backed")

    monkeypatch.setattr(runner, "start", refuse)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []


def test_a_mount_backed_root_is_refused_before_runner_start_is_even_called(
        tmp_path, monkeypatch):
    """`detect._check_root`'s own `MountGuard.blocks(root)` pre-check (pure
    string comparison, no syscall on `root`) short-circuits the common case
    without needing `runner.start`'s exception at all."""
    _no_recent_scan(monkeypatch)
    mounts = tmp_path / "mounts"
    mounts.mkdir()
    monkeypatch.setattr(runner, "_mounts_dir", lambda: str(mounts))
    root = runner.canonical_root(str(mounts / "bucket"))
    (mounts / "bucket").mkdir()

    def boom(cfg, root, full=False):
        raise AssertionError("runner.start reached for a mount-backed root")

    monkeypatch.setattr(runner, "start", boom)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []


# -- the hidden-duration gate -----------------------------------------------------

def test_hidden_duration_below_the_floor_is_a_no_op(tmp_path, monkeypatch, spawned):
    """The signal is "went away and did something else", not "alt-tabbed
    between our own windows" — enforced server-side; the client value is an
    input, not a decision."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S - 1, now=NOW) == []
    assert spawned == []


def test_hidden_duration_at_the_floor_is_allowed(tmp_path, monkeypatch, spawned):
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


# -- multiple configured roots ----------------------------------------------------

def test_each_configured_root_is_checked_independently(tmp_path, monkeypatch, spawned):
    """A root within its own MIN_INTERVAL_S floor is skipped; a root past it
    is scanned — independently, in the same call."""
    from fused_render.index import freshness

    fresh = _root(tmp_path)
    (tmp_path / "root2").mkdir()
    stale = runner.canonical_root(str(tmp_path / "root2"))

    def fake_last_scan(cfg, r):
        return NOW - freshness.MIN_INTERVAL_S + 1 if r == fresh else None

    monkeypatch.setattr(runner, "last_scan", fake_last_scan)
    assert detect.note_home_focused(
        _cfg(tmp_path), [fresh, stale], detect.MIN_HIDDEN_S, now=NOW) == [stale]
    assert spawned == [{"root": stale, "full": False}]


# -- must not cancel an in-flight reconciling scan --------------------------------

def test_a_live_run_of_the_root_is_never_cancelled_by_a_focus_event(
        tmp_path, monkeypatch):
    """`runner.start` would SUPERSEDE (cancel + respawn) a live run under a
    different `ignore_sig` — exactly the state right after an ignore-list
    edit, while the reconciling rescan it triggered is still walking. A tab
    regaining focus must never be the thing that discards that walk's
    progress, so this trigger has to refuse outright, the same way
    `freshness.note_folder_opened` refuses when a run is already live."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(runner, "active_run",
                        lambda cfg, r: {"run_id": "live", "root": r,
                                        "ignore_sig": "different", "full": False})
    started_calls = []
    monkeypatch.setattr(runner, "start",
                        lambda cfg, root, full=False: started_calls.append(root))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert started_calls == []


# -- joining a live run must not be reported as "started" -------------------------

def test_joining_an_already_running_scan_is_not_reported_as_started(
        tmp_path, monkeypatch):
    """`runner.start` returns `{"already_running": True}` when it joins a
    live run instead of spawning one (a race with the `active_run` guard
    above, or any other caller that started it first). That is not a scan
    THIS check started, and reporting it as such would make the router log
    "focus found a stale root... rescanning" and wake the Activity card for a
    run that would have happened regardless."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(runner, "start",
                        lambda cfg, root, full=False: {
                            "run_id": "r1", "root": root, "already_running": True})
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []


# -- Phase 1: root-mtime evidence overrides the staleness floors -----------------
#
# The reported failure: a scan finished at T, `~/a.txt` landed directly under
# the root at T+12 (moving the root's OWN mtime — a direct child, the
# documented depth-1 bound freshness.py already names), and the focus event at
# T+42 was refused: 42s had cleared neither `freshness.MIN_INTERVAL_S` (60s)
# nor `FOCUS_STALE_S` (300s). Both floors were doing their job (stopping a
# pointless rescan) against the WRONG question — neither one asks whether
# anything actually changed. A `stat` of the root itself, compared against
# what the index recorded for it (`freshness.is_newer_than_indexed`, the same
# comparison `note_folder_opened` already uses), answers exactly that for a
# direct child, for the cost of one syscall.

def test_evidence_from_a_freshly_touched_root_overrides_both_stale_floors(
        tmp_path, monkeypatch, spawned):
    """The regression test: reproduces the reported failure exactly (a scan
    at T, a direct child created at T+12, a focus event at T+42) and asserts
    a scan now starts despite neither MIN_INTERVAL_S nor FOCUS_STALE_S having
    elapsed. Fails against pre-Phase-1 HEAD, where `_check_root` has no way
    to distinguish this from any other 42s-stale root."""
    from fused_render.index import freshness

    root = _root(tmp_path)
    t = NOW
    cfg = _indexed_root(tmp_path, root, t)
    os.utime(root, (t + 12, t + 12))  # a direct child landing moves root's mtime
    monkeypatch.setattr(runner, "last_scan", lambda cfg, r: t)
    now = t + 42
    assert (now - t) < freshness.MIN_INTERVAL_S  # sanity: neither floor has cleared
    assert (now - t) < detect.FOCUS_STALE_S
    assert detect.note_home_focused(cfg, [root], detect.MIN_HIDDEN_S, now=now) == [root]
    assert spawned == [{"root": root, "full": False}]


def test_no_evidence_and_a_recently_scanned_root_is_a_no_op(tmp_path, monkeypatch, spawned):
    """No evidence (root mtime unchanged since the index recorded it) and the
    root is within `MIN_INTERVAL_S`: refused, same as today."""
    root = _root(tmp_path)
    t = NOW
    cfg = _indexed_root(tmp_path, root, t)
    os.utime(root, (t, t))  # matches the indexed mtime exactly: no evidence
    monkeypatch.setattr(runner, "last_scan", lambda cfg, r: t)
    assert detect.note_home_focused(cfg, [root], detect.MIN_HIDDEN_S, now=t + 30) == []
    assert spawned == []


def test_no_evidence_and_a_root_past_focus_stale_s_is_still_scanned(
        tmp_path, monkeypatch, spawned):
    """No evidence, but the root is past `FOCUS_STALE_S`: the existing
    fallback (unchanged by Phase 1) still fires."""
    root = _root(tmp_path)
    t = NOW
    cfg = _indexed_root(tmp_path, root, t)
    os.utime(root, (t, t))  # no evidence
    monkeypatch.setattr(runner, "last_scan", lambda cfg, r: t)
    now = t + detect.FOCUS_STALE_S + 1
    assert detect.note_home_focused(cfg, [root], detect.MIN_HIDDEN_S, now=now) == [root]
    assert spawned == [{"root": root, "full": False}]


def test_evidence_with_a_scan_moments_ago_is_refused_by_the_storm_floor(
        tmp_path, monkeypatch, spawned):
    """Evidence bypasses `MIN_INTERVAL_S`/`FOCUS_STALE_S`, but not
    unconditionally — a small floor (`EVIDENCE_MIN_INTERVAL_S`) still stops a
    scan storm: a root just scanned 2s ago, even with fresh evidence, is
    refused. This is what keeps a flappy tab-away from restarting a scan on
    every focus event just because the root keeps getting touched."""
    root = _root(tmp_path)
    t = NOW
    cfg = _indexed_root(tmp_path, root, t)
    os.utime(root, (t + 1, t + 1))  # evidence: root moved since the index
    monkeypatch.setattr(runner, "last_scan", lambda cfg, r: t + 1)
    now = t + 1 + 2  # a scan finished 2s ago
    assert detect.note_home_focused(cfg, [root], detect.MIN_HIDDEN_S, now=now) == []
    assert spawned == []


def test_evidence_stat_failure_is_treated_as_no_evidence_not_an_exception(
        tmp_path, monkeypatch, spawned):
    """A root that vanishes between the focus event and the check (or any
    other `OSError` from `os.stat`) must degrade to "no evidence", not
    raise — this is a housekeeping path with the same "never raises"
    contract as everything else in this module."""
    root = _root(tmp_path)
    t = NOW
    cfg = _indexed_root(tmp_path, root, t)
    monkeypatch.setattr(runner, "last_scan", lambda cfg, r: t)

    real_stat = os.stat

    def flaky_stat(path, *a, **kw):
        if path == root:
            raise OSError("gone")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(os, "stat", flaky_stat)
    # No evidence (stat failed) and still within FOCUS_STALE_S: refused, not raised.
    assert detect.note_home_focused(cfg, [root], detect.MIN_HIDDEN_S, now=t + 30) == []
    assert spawned == []


# -- exceptions never escape -------------------------------------------------------

def test_an_unexpected_exception_from_runner_start_does_not_escape(
        tmp_path, monkeypatch, caplog):
    """Housekeeping must never surface. `_check_root` only explicitly catches
    `ValueError` from `runner.start` (the documented refusal shape); anything
    else must still be swallowed by `note_home_focused`'s own per-root
    try/except, exactly like `git_repos._note_tab_opened`."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)

    def boom(cfg, root, full=False):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(runner, "start", boom)
    with caplog.at_level(logging.ERROR):
        result = detect.note_home_focused(
            _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW)
    assert result == []

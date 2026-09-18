"""Change detection on home-page focus.

See SPEC-focus-change-detection.md. The whole feature is: replay the macOS
FSEvents journal standalone (fsevents.hint), on the home page regaining focus
after being hidden a while, and only start the ordinary incremental scan when
that replay reports a change. Home search is index-backed and global, and the
mtime-based freshness check (index/freshness.py) is structurally blind to a
change at depth under a root whose own direct entries did not move — the
journal is not.

Style follows tests/test_index_freshness.py: assert on the trigger (did
`runner.start` get called, for which root), never on a real scan.
"""
import logging
import os

import pytest

from fused_render.index import detect, fsevents, runner
from fused_render.index.config import IndexConfig
from fused_render.shell import index_gate

NOW = 1_000_000.0


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


def _fake_home(monkeypatch, tmp_path):
    """Redirect `~` to `tmp_path` for BOTH `default_home_dirs()` (this app's
    own state home, consulted by `MountGuard`) and `detect._os_noise_roots()`
    (this trigger's own `~/Library`-style noise list) — the two things a
    `~`-shaped path in this test needs to line up with.

    `monkeypatch.setenv("HOME", ...)` alone is NOT portable: `os.path.
    expanduser("~")` reads `HOME` on POSIX but ignores it entirely on
    Windows (it consults `USERPROFILE`/`HOMEDRIVE`+`HOMEPATH` instead), so a
    `HOME`-only redirect silently resolves to the REAL Windows user profile
    in CI, and none of this test's tmp-path-shaped noise paths land under it
    — the noise filter then never fires and a scan starts where the test
    expects none. `tests/test_index_mount_safe.py` (`test_the_guard_blocks_
    every_fused_render_home_not_just_the_current_one`) hit the identical
    problem and fixed it the same way: patch `os.path.expanduser` directly,
    which is honoured on every platform because it IS the thing every
    caller here (`default_home_dirs()`, `detect._os_noise_roots()`) calls."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        os.path, "expanduser",
        lambda p: p.replace("~", str(tmp_path), 1) if p.startswith("~") else p)


# -- the three hint() outcomes -------------------------------------------------

def test_hint_none_starts_no_scan(tmp_path, monkeypatch, spawned):
    """None means "cannot tell" (non-darwin, no saved state, a UUID mismatch,
    a huge change set...) and must never be read as "nothing changed"."""
    _no_recent_scan(monkeypatch)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, root: None)
    root = _root(tmp_path)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert spawned == []


def test_hint_quiet_starts_no_scan(tmp_path, monkeypatch, spawned):
    """(set(), []) is the journal reporting nothing under this root — the
    case the whole design exists for. Asserted explicitly, per the spec."""
    _no_recent_scan(monkeypatch)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, root: (set(), []))
    root = _root(tmp_path)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert spawned == []


def test_hint_nonempty_starts_a_scan_of_that_root(tmp_path, monkeypatch, spawned):
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(fsevents, "hint",
                        lambda cfg, r: ({root + "/Downloads"}, []))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


def test_hint_nonempty_subtrees_only_still_starts_a_scan(tmp_path, monkeypatch, spawned):
    """A `_FSE_MUST_SCAN_SUBDIRS` flag lands in the subtrees list, not the
    forced-dirs set — either being non-empty is "something changed"."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: (set(), [root]))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


def test_hint_raising_starts_no_scan_and_does_not_escape(tmp_path, monkeypatch, spawned, caplog):
    """hint() does `int(st["event_id"])` with no guard of its own — scan.py
    lets it raise into the run's failure handler, but this caller has no run
    to fail into and must swallow it."""
    _no_recent_scan(monkeypatch)

    def boom(cfg, root):
        raise ValueError("not an int")

    monkeypatch.setattr(fsevents, "hint", boom)
    root = _root(tmp_path)
    with caplog.at_level(logging.DEBUG):
        result = detect.note_home_focused(
            _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW)
    assert result == []
    assert spawned == []


# -- pacing ---------------------------------------------------------------------

def test_detect_interval_floor_refuses_a_second_call_inside_the_window(
        tmp_path, monkeypatch, spawned):
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))
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
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))
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
    finished."""
    from fused_render.index import freshness

    root = _root(tmp_path)
    monkeypatch.setattr(runner, "last_scan",
                        lambda cfg, r: NOW - freshness.MIN_INTERVAL_S + 1)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert spawned == []


def test_freshness_min_interval_no_longer_refuses_once_it_has_elapsed(
        tmp_path, monkeypatch, spawned):
    from fused_render.index import freshness

    root = _root(tmp_path)
    monkeypatch.setattr(runner, "last_scan",
                        lambda cfg, r: NOW - freshness.MIN_INTERVAL_S - 1)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


# -- gates shared with every other trigger ---------------------------------------

def test_indexing_pref_off_is_a_no_op(tmp_path, monkeypatch, spawned):
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))
    monkeypatch.setattr(index_gate, "indexing_blocked", lambda: "disabled")
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert spawned == []


def test_a_mount_backed_root_is_refused_via_runner_start(tmp_path, monkeypatch):
    """detect.py does not re-derive the MountGuard/FDA/isdir refusals —
    runner.start already raises ValueError for each, and this is the backstop
    that absorbs it."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))

    def refuse(cfg, root, full=False):
        raise ValueError(f"{root} is mount-backed")

    monkeypatch.setattr(runner, "start", refuse)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []


# -- the hidden-duration gate -----------------------------------------------------

def test_hidden_duration_below_the_floor_is_a_no_op(tmp_path, monkeypatch, spawned):
    """The signal is "went away and did something else", not "alt-tabbed
    between our own windows" — enforced server-side; the client value is an
    input, not a decision."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S - 1, now=NOW) == []
    assert spawned == []


def test_hidden_duration_at_the_floor_is_allowed(tmp_path, monkeypatch, spawned):
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


# -- multiple configured roots ----------------------------------------------------

def test_each_configured_root_is_checked_independently(tmp_path, monkeypatch, spawned):
    _no_recent_scan(monkeypatch)
    quiet = _root(tmp_path)
    (tmp_path / "root2").mkdir()
    changed = runner.canonical_root(str(tmp_path / "root2"))

    def fake_hint(cfg, r):
        return (set(), []) if r == quiet else ({r + "/x"}, [])

    monkeypatch.setattr(fsevents, "hint", fake_hint)
    assert detect.note_home_focused(
        _cfg(tmp_path), [quiet, changed], detect.MIN_HIDDEN_S, now=NOW) == [changed]
    assert spawned == [{"root": changed, "full": False}]


# -- code review: the raw hint must be filtered before it is collapsed ------------
#
# `fsevents.hint` does no ignore-rule filtering of its own (it only
# prefix-filters by root) — so on a real `~` root, its raw output routinely
# includes noise no scan would ever act on: this app's own state home, and
# (as of this review) `~/Library`'s constant macOS churn. A test that hands
# `hint` a hand-picked `(set(), [])` proves nothing about this — it has to be
# a REALISTIC shape, containing paths the ignore rules are actually supposed
# to drop, or it can't catch the collapse-to-boolean bug the review found.

def test_realistic_noisy_hint_on_a_real_home_root_starts_no_scan(
        tmp_path, monkeypatch):
    """The case the whole design exists for, made real: on the default `~`
    root, `hint` reporting only noise (this app's own state home, plus
    `~/Library` churn) must still take the quiet path — not "look changed"
    just because the raw journal saw something move."""
    _fake_home(monkeypatch, tmp_path)
    _no_recent_scan(monkeypatch)
    calls = []
    monkeypatch.setattr(runner, "start",
                        lambda cfg, root, full=False: calls.append(root))
    root = runner.canonical_root(str(tmp_path))
    noisy = {
        # this app's own state home — a scan's OWN writes land here
        str(tmp_path / ".fused-render" / "index" / "runs" / "r1" / "spec.json"),
        str(tmp_path / ".fused-render" / "index" / "dirs.parquet"),
        # macOS home noise, never anything a user searches home for
        str(tmp_path / "Library" / "Caches" / "com.apple.example" / "foo"),
        str(tmp_path / "Library" / "Saved Application State" / "bar"),
    }
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: (noisy, []))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert calls == []


def test_realistic_noisy_hint_mixed_with_a_real_change_still_scans(
        tmp_path, monkeypatch, spawned):
    """The filter must not overreach: noise dropped alongside a genuine
    change under the same root still starts the scan."""
    _fake_home(monkeypatch, tmp_path)
    _no_recent_scan(monkeypatch)
    root = runner.canonical_root(str(tmp_path))
    mixed = {
        str(tmp_path / ".fused-render" / "index" / "dirs.parquet"),
        str(tmp_path / "Library" / "Caches" / "foo"),
        str(tmp_path / "Downloads" / "report.pdf"),
    }
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: (mixed, []))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == [root]
    assert spawned == [{"root": root, "full": False}]


# -- code review: must not cancel an in-flight reconciling scan -------------------

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
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))
    monkeypatch.setattr(runner, "active_run",
                        lambda cfg, r: {"run_id": "live", "root": r,
                                        "ignore_sig": "different", "full": False})
    started_calls = []
    monkeypatch.setattr(runner, "start",
                        lambda cfg, root, full=False: started_calls.append(root))
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []
    assert started_calls == []


# -- code review: joining a live run must not be reported as "started" -----------

def test_joining_an_already_running_scan_is_not_reported_as_started(
        tmp_path, monkeypatch):
    """`runner.start` returns `{"already_running": True}` when it joins a
    live run instead of spawning one (a race with the `active_run` guard
    above, or any other caller that started it first). That is not a scan
    THIS check started, and reporting it as such would make the router log
    "focus found changes... rescanning" and wake the Activity card for a
    run that would have happened regardless."""
    _no_recent_scan(monkeypatch)
    root = _root(tmp_path)
    monkeypatch.setattr(fsevents, "hint", lambda cfg, r: ({root + "/x"}, []))
    monkeypatch.setattr(runner, "start",
                        lambda cfg, root, full=False: {
                            "run_id": "r1", "root": root, "already_running": True})
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []


# -- code review: MountGuard before any syscall on `root` -------------------------

def test_a_mount_backed_root_never_reaches_fsevents_hint(tmp_path, monkeypatch):
    """`fsevents.hint` reaches `os.stat(root)` (via `device_uuid`) with no
    guard of its own, and `os.stat` on a wedged rclone/NFS mount blocks the
    calling thread forever. The MountGuard check must come first, the same
    ordering `freshness.note_folder_opened` uses ahead of its own `os.stat`."""
    _no_recent_scan(monkeypatch)
    mounts = tmp_path / "mounts"
    mounts.mkdir()
    monkeypatch.setattr(runner, "_mounts_dir", lambda: str(mounts))
    root = runner.canonical_root(str(mounts / "bucket"))
    (mounts / "bucket").mkdir()

    def boom(cfg, r):
        raise AssertionError("fsevents.hint reached a mount-backed root")

    monkeypatch.setattr(fsevents, "hint", boom)
    assert detect.note_home_focused(
        _cfg(tmp_path), [root], detect.MIN_HIDDEN_S, now=NOW) == []

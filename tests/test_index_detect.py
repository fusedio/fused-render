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

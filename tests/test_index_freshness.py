"""Keeping the open folder's slice of the index fresh.

See fused_render/index/specs/scan-incremental.md §5. The whole feature is one
mtime comparison plus pacing: the trigger reuses the ordinary incremental scan,
so what is worth guarding here is the decision to fire it — every gate that
must refuse, in the order that makes the expensive ones unreachable.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index import freshness, runner
from fused_render.index.config import IndexConfig
from fused_render.index.freshness import (
    MIN_INTERVAL_S,
    QUIET_S,
    FreshnessCheck,
    enclosing_root,
    indexed_mtime_ns,
    note_folder_opened,
)
from fused_render.index.runner import canonical_root
from fused_render.index.store import Sink, compact

NS = 1_000_000_000
NEVER = FreshnessCheck()


def _index(tmp_path, root, dirs):
    """A real index whose dirs.parquet holds `dirs` = {abs dir: mtime_ns}.

    Every key goes through `canonical_root` before it becomes a dirs.parquet
    row: `indexed_mtime_ns`/`enclosing_root` look a directory up by
    `norm(os.path.abspath(...))` of their OWN argument (freshness.py), so a
    row filed under the raw literal this helper is handed — "/r", or a
    native-separator `str(tmp_path / ...)` on Windows — silently misses every
    query built from the same literal once that literal isn't already its own
    abspath (a POSIX-only coincidence)."""
    cfg = IndexConfig(dir=str(tmp_path / "ix"))
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    root = canonical_root(root)
    for d, mtime_ns in dirs.items():
        sink.add(canonical_root(d), "s", ("sig", [], 0, mtime_ns, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


@pytest.fixture()
def spawned(monkeypatch):
    """runner.start recorded instead of spawning a worker."""
    calls = []

    def fake_start(cfg, root, full=False):
        calls.append({"root": root, "full": full})
        return {"run_id": "r1", "root": root}

    monkeypatch.setattr(runner, "start", fake_start)
    # No mounts records anywhere near tmp_path, so the guard is a no-op here
    # except in the test that points it at one.
    monkeypatch.setattr(runner, "_mounts_dir", lambda: "/nonexistent-mounts")
    return calls


def _tree(tmp_path, rel):
    d = tmp_path / rel
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


# -- the staleness check -------------------------------------------------------

def test_the_indexed_mtime_of_a_recorded_directory_is_read_back(tmp_path):
    cfg = _index(tmp_path, "/r", {"/r": 500 * NS, "/r/sub": 700 * NS})
    assert indexed_mtime_ns(cfg, "/r/sub") == 700 * NS


def test_a_directory_the_index_never_recorded_reads_as_unknown(tmp_path):
    cfg = _index(tmp_path, "/r", {"/r": 500 * NS})
    assert indexed_mtime_ns(cfg, "/r/never-scanned") is None


def test_an_index_that_was_never_built_reads_as_unknown(tmp_path):
    assert indexed_mtime_ns(IndexConfig(dir=str(tmp_path / "ix")), "/r") is None


# -- which root a folder belongs to -------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/home/me/code/app", canonical_root("/home/me/code")),
    ("/home/me/code", canonical_root("/home/me/code")),
    ("/home/me/other", None),
    # segment-wise, so a sibling with the root as a name prefix is not inside it
    ("/home/me/code-old/app", None),
])
def test_enclosing_root_is_matched_segment_wise(path, expected):
    # `enclosing_root` returns its OWN canonicalized spelling of a match
    # (norm+abspath, freshness.py), not the caller's literal — hence
    # `canonical_root(...)` rather than the bare "/home/me/code" above; the
    # None cases need no such wrap since a non-match stays a non-match on
    # every platform.
    assert enclosing_root(["/home/me/code"], path) == expected


def test_the_deepest_configured_root_wins():
    """Roots may nest. The scan that answers for the folder soonest is the
    narrower one, and firing the outer root as well would scan its subtree
    twice."""
    roots = ["/a", "/a/b"]
    assert enclosing_root(roots, "/a/b/c") == canonical_root("/a/b")


# -- the trigger --------------------------------------------------------------

def test_a_folder_whose_mtime_moved_since_the_scan_triggers_a_rescan(
        tmp_path, spawned):
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) == FreshnessCheck(
        started=canonical_root(root))
    assert spawned == [{"root": canonical_root(root), "full": False}]


def test_an_unchanged_folder_triggers_nothing(tmp_path, spawned):
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS,
                                 sub: os.stat(sub).st_mtime_ns})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) == NEVER
    assert spawned == []


def test_a_folder_the_index_never_recorded_triggers_nothing(tmp_path, spawned):
    """An uncovered folder already falls back to the live walk (query.md §6),
    so a scan buys its search nothing — and treating "absent" as stale would
    make every folder outside the index a trigger."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) == NEVER
    assert spawned == []


def test_an_unknown_recorded_mtime_triggers_nothing(tmp_path, spawned):
    """A partition written before mtime_ns existed reads 0 (store._compact_locked).
    Comparing against 0 would read as "stale forever" and fire on every open."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 0})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) == NEVER
    assert spawned == []


def test_a_folder_that_changed_moments_ago_is_left_to_settle(tmp_path, spawned):
    """The quiet period. A build directory's mtime moves continuously, so it is
    never quiet and never triggers — which is what stops it queueing scan after
    scan. Refused here comes back with `retry_after`: the caller gets to ask
    again once the folder has actually gone quiet, rather than the refusal
    being the end of the question."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    now = os.stat(sub).st_mtime + QUIET_S - 1
    result = note_folder_opened(cfg, sub, [root], now=now)
    assert result.started is None
    assert result.retry_after == pytest.approx(1.0)
    assert spawned == []


def test_a_folder_quiet_for_only_a_few_seconds_is_already_actionable(tmp_path, spawned):
    """QUIET_S only has to outlast a single write burst, not tolerate a whole
    coffee break of one — MIN_INTERVAL_S is what stops a churning directory
    from queueing scan after scan, independently. A directory edited five
    seconds ago, well past a realistic single burst, is already stale-worthy:
    the file someone just saved must be findable within a handful of seconds,
    not thirty of them."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    now = os.stat(sub).st_mtime + 5
    assert note_folder_opened(cfg, sub, [root], now=now) == FreshnessCheck(
        started=canonical_root(root))
    assert spawned == [{"root": canonical_root(root), "full": False}]


def test_a_root_scanned_within_the_floor_is_not_rescanned(tmp_path, spawned):
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    # `_record_scan` (unlike `runner.start`, which calls it internally) does
    # NOT canonicalize its own `root` argument before filing it — only
    # `last_scan`'s READ side does — so calling it directly, as this test
    # does to seed the floor without a real scan, has to canonicalize first
    # or the write and the read never agree on the same key.
    runner._record_scan(cfg, canonical_root(root))
    assert runner.last_scan(cfg, root) is not None
    # `now` is in the future relative to the record just written, so express the
    # floor from the record itself.
    at = runner.last_scan(cfg, root) + MIN_INTERVAL_S - 1
    assert note_folder_opened(cfg, sub, [root], now=max(now, at)) == NEVER
    assert spawned == []


def test_a_root_scanned_two_minutes_ago_is_rescanned(tmp_path, spawned):
    """The floor is a minute, not ten. Browsing must not queue scan after scan,
    but a folder that changed out of band should not stay stale for the length
    of a coffee break either — and a scan started sooner is a CHEAPER scan: both
    dominant costs (the journal replay and the set of dirs it names) scale with
    the window since the last one."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    runner._record_scan(cfg, canonical_root(root))  # see the floor test above
    at = runner.last_scan(cfg, root) + 120
    assert note_folder_opened(cfg, sub, [root], now=at) == FreshnessCheck(
        started=canonical_root(root))
    assert spawned == [{"root": canonical_root(root), "full": False}]


def test_the_scan_floor_sits_below_the_routers_check_debounce(tmp_path):
    """Two floors. freshness.MIN_INTERVAL_S paces the SCANS (read off scans.json,
    so it also sees the startup scheduler and the manual buttons);
    routers.index.FRESHNESS_CHECK_S paces the CHECKS, per root, in memory.

    `_freshness_due` stamps the check clock whenever a check comes due, whether
    or not that check goes on to actually scan. So a check that lands before
    MIN_INTERVAL_S has elapsed since the last scan stamps for nothing: it
    refuses on the scan floor, and the next check is a full FRESHNESS_CHECK_S
    later. Keeping FRESHNESS_CHECK_S above MIN_INTERVAL_S (plus the ~1s spawn
    offset a scan takes to record itself) means every check that comes due
    finds the scan floor already clear, so the folder-open rescan cadence is
    FRESHNESS_CHECK_S itself — see
    test_the_effective_folder_open_scan_cadence_tracks_freshness_check_s below,
    which asserts that cadence directly rather than this relationship alone."""
    from fused_render.server.routers.index import FRESHNESS_CHECK_S

    assert FRESHNESS_CHECK_S > MIN_INTERVAL_S + 1


def test_the_effective_folder_open_scan_cadence_tracks_freshness_check_s(tmp_path, spawned, monkeypatch):
    """The number that actually governs how often an open folder gets rescanned
    is not FRESHNESS_CHECK_S read back in isolation — it is what falls out of
    that constant interacting with note_folder_opened's own MIN_INTERVAL_S
    scan floor. Simulated on a synthetic clock (seconds, not real time) so this
    runs instantly rather than over several real minutes.

    `runner._record_scan` stamps the real wall clock, not an injectable `now`,
    so the simulated "a scan just started" moment is recorded directly into
    scans.json via the same storage module it uses, keyed the same way
    (`canonical_root`) — bypassing the wall-clock dependency entirely rather
    than monkeypatching `time.time` globally, which would also perturb
    anything else in the loop that reads the real clock."""
    from fused_render.server.routers.index import FRESHNESS_CHECK_S
    from fused_render.server.routers import index as index_router
    from fused_render.shell import storage
    import time as time_mod

    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    canon = canonical_root(root)
    # An mtime_ns of 1 is permanently "older than disk", so every check that
    # clears the quiet window and the scan floor also finds the folder stale.
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    storage.write_json(cfg.scans_json, {canon: 0.0})
    monkeypatch.setattr(index_router, "_freshness_checked", {})

    t0 = time_mod.time()
    scans_at = []
    t = t0
    end = t0 + 6 * FRESHNESS_CHECK_S
    while t < end:
        if index_router._freshness_due(canon, t):
            result = note_folder_opened(cfg, sub, [root], now=t)
            if result.started:
                scans_at.append(t)
                storage.write_json(cfg.scans_json, {canon: t})
        t += 1.0

    # At least 4 scans over the simulated window, or the gaps below would be
    # measuring too few samples to mean anything.
    assert len(scans_at) >= 4
    gaps = [b - a for a, b in zip(scans_at, scans_at[1:])]
    # The bug this guards: every OTHER due check wasted on a scan floor not
    # yet clear, doubling the real cadence to roughly 2×FRESHNESS_CHECK_S.
    # Fixed, every due check can act, so each gap tracks FRESHNESS_CHECK_S
    # itself.
    for gap in gaps:
        assert MIN_INTERVAL_S <= gap <= FRESHNESS_CHECK_S + 2


def test_the_deferral_is_absorbed_inside_the_check_interval(tmp_path):
    """The delay must stay small against the check interval it lives inside.

    _run_freshness_check waits FRESHNESS_DELAY_S and then stamps, so a root's
    checks recur every FRESHNESS_CHECK_S + FRESHNESS_DELAY_S rather than every
    FRESHNESS_CHECK_S. That shifts the schedule by a rounding error, which is
    the whole claim the deferral makes: it does not introduce a refusal that
    was not already happening (the refusals, if any, come from the check
    interval interacting with the scan floor — see the tests above — not from
    this). A delay of the same order as the interval would stop being absorbed
    and start being the cadence, so the margin, not merely the ordering, is
    what is asserted.

    Deliberately NOT asserted: any relation between this pair and
    MIN_INTERVAL_S — FRESHNESS_CHECK_S sitting above MIN_INTERVAL_S is what the
    test above already covers, and a second test here forbidding it would only
    duplicate that constraint under a different name."""
    from fused_render.server.routers.index import (
        FRESHNESS_CHECK_S,
        FRESHNESS_DELAY_S,
    )

    assert 0 < FRESHNESS_DELAY_S <= FRESHNESS_CHECK_S / 10


def test_a_folder_outside_every_configured_root_triggers_nothing(
        tmp_path, spawned):
    root = _tree(tmp_path, "root")
    outside = _tree(tmp_path, "elsewhere")
    cfg = _index(tmp_path, root, {root: 1 * NS, outside: 1 * NS})
    now = os.stat(outside).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, outside, [root], now=now) == NEVER
    assert spawned == []


def test_a_live_scan_of_the_root_is_not_joined_by_a_second_one(
        tmp_path, spawned, monkeypatch):
    """runner.start would join it, but only for an EXACT root-string match, and
    a triggered scan must not be the thing that discovers that. Refuse here."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    monkeypatch.setattr(runner, "active_run",
                        lambda cfg, r: {"run_id": "live", "root": r})
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) == NEVER
    assert spawned == []


def test_the_scan_it_starts_is_of_the_configured_root_not_the_open_folder(
        tmp_path, monkeypatch):
    """`scans.json` is keyed by root string and read by the startup debounce,
    so a per-folder root would both pollute it and defeat runner.start's
    exact-match join. Let the real runner.start record, and check the key."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    monkeypatch.setattr(runner, "_mounts_dir", lambda: "/nonexistent-mounts")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: _Spawned())
    now = os.stat(sub).st_mtime + QUIET_S + 1
    assert note_folder_opened(cfg, sub, [root], now=now) == FreshnessCheck(
        started=canonical_root(root))
    assert runner.last_scan(cfg, root) is not None
    assert runner.last_scan(cfg, sub) is None


class _Spawned:
    pid = 4242


def test_a_mount_backed_folder_is_refused_without_touching_the_kernel(
        tmp_path, spawned, monkeypatch):
    """os.stat on a wedged rclone mount blocks the request thread forever (this
    repo's documented mount-wedge class), so the guard has to come first — and
    it is pure string work against the mount records."""
    mounts = _tree(tmp_path, "home/mounts")
    root = _tree(tmp_path, "home")
    under = _tree(tmp_path, "home/mounts/bucket/data")
    cfg = _index(tmp_path, root, {root: 1 * NS, under: 1 * NS})
    monkeypatch.setattr(runner, "_mounts_dir", lambda: mounts)

    # Scoped to `under`, not a blanket boom() on every os.stat call: `os` is a
    # single process-wide module object, so patching it unconditionally also
    # patches every OTHER thread's os.stat — including Python's own linecache,
    # which pytest's thread-exception hook calls (to format a DIFFERENT
    # thread's traceback) at whatever moment that thread happens to raise.
    # That corrupted the hook itself under xdist, intermittently failing an
    # unrelated test or crashing a worker outright. Only refusing the one path
    # this test cares about keeps the assertion just as sharp while leaving
    # every other os.stat call in the process alone.
    real_stat = os.stat

    def boom(p, *args, **kwargs):
        if isinstance(p, str) and p == under:
            raise AssertionError("stat reached a mount-backed path")
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(freshness.os, "stat", boom)
    now = 10 ** 10
    assert note_folder_opened(cfg, under, [root], now=now) == NEVER
    assert spawned == []


def test_a_vanished_folder_is_not_an_error(tmp_path, spawned):
    root = _tree(tmp_path, "root")
    gone = os.path.join(root, "gone")
    cfg = _index(tmp_path, root, {root: 1 * NS, gone: 1 * NS})
    assert note_folder_opened(cfg, gone, [root], now=10 ** 10) == NEVER
    assert spawned == []


# -- the retry signal -----------------------------------------------------

def test_churning_is_the_only_refusal_that_asks_to_be_retried(tmp_path, spawned):
    """The one distinction the caller needs: `retry_after` is set precisely
    when everything cheaper than the quiet check has already passed, and it
    names the moment the folder will actually have gone quiet."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: 1 * NS})
    disk_mtime = os.stat(sub).st_mtime
    now = disk_mtime + QUIET_S - 5
    result = note_folder_opened(cfg, sub, [root], now=now)
    assert result == FreshnessCheck(retry_after=pytest.approx(5.0))
    # And asking again once that many seconds have actually passed succeeds.
    assert note_folder_opened(
        cfg, sub, [root], now=now + result.retry_after + 0.01
    ) == FreshnessCheck(started=canonical_root(root))
    assert spawned == [{"root": canonical_root(root), "full": False}]


def test_a_change_that_turns_out_not_to_be_stale_still_reports_retry_after(
        tmp_path, spawned):
    """The quiet gate runs before the staleness lookup (cheapest-first), so a
    change inside the quiet window is reported as retryable even when it will
    turn out, once the folder is actually quiet, not to be stale at all. That
    costs the caller one wasted retry, never a wrong scan."""
    root = _tree(tmp_path, "root")
    sub = _tree(tmp_path, "root/sub")
    cfg = _index(tmp_path, root, {root: 1 * NS, sub: os.stat(sub).st_mtime_ns})
    now = os.stat(sub).st_mtime + QUIET_S - 1
    result = note_folder_opened(cfg, sub, [root], now=now)
    assert result.retry_after == pytest.approx(1.0)
    assert note_folder_opened(
        cfg, sub, [root], now=now + result.retry_after + 0.01) == NEVER
    assert spawned == []

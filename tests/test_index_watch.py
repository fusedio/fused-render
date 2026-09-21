"""The live filesystem watcher (fused_render/server/index_watch.py).

There is no timer that can answer "did anything change?" (SPEC-index-live-watch.md
§1 — three time-based designs shipped green unit tests and failed the real
test). This module observes the change stream directly instead: `watchfiles`
feeds a `WatchLoop` per configured root, which filters at arrival, reduces
batches to folders, and forwards to the existing `RescanQueue` policy
(`index_touch.note_index_folders`) no more often than a flush floor.

What is testable here, and what these tests pin, is the POLICY: filtering,
batching, the flush floor, the outermost-folder collapse, error/backoff
behaviour, the periodic safety net, and the indexing gate. The real
`watchfiles.watch` integration gets one end-to-end test against the real
filesystem (the last test in this file).
"""
import os
import threading
import time

from fused_render.index.ignore import IgnoreRules, default_ignore, norm
from fused_render.server import index_touch
from fused_render.server.index_watch import (
    BACKOFF_SCHEDULE_S,
    MAX_FOLDERS,
    WATCH_FLUSH_FLOOR_S,
    WATCH_RESCAN_S,
    WatchLoop,
    _make_loop,
    make_dropped,
)


class Fake:
    """Deps for a WatchLoop: a clock, a sleeper, and the recorded effects."""

    def __init__(self, live=(), last_scan=None, gate=True):
        self.t = 1000.0
        self.forwarded = []
        self.slept = []
        self.opened = 0
        self.live = set(live)
        self.scans = dict(last_scan or {})
        self._gate = gate

    def now(self):
        return self.t

    def sleep(self, delay):
        self.slept.append(delay)

    def forward(self, folders):
        self.forwarded.append(set(folders))

    def last_scan(self, root):
        return self.scans.get(root)

    def live_run_covers(self, root):
        return root in self.live

    def gate_open(self):
        return self._gate

    def dropped(self, path):
        return False

    def loop(self, root, open_source, **kw):
        stop = kw.pop("stop_event", threading.Event())
        return WatchLoop(root, open_source=open_source, dropped=self.dropped,
                         forward=self.forward, now=self.now,
                         last_scan=self.last_scan,
                         live_run_covers=self.live_run_covers,
                         gate_open=self.gate_open, sleep=self.sleep,
                         stop_event=stop, **kw)


def _added(path):
    from watchfiles import Change

    return (Change.added, path)


# --------------------------------------------------------------- filtering


def test_a_batch_of_file_paths_is_reduced_to_parent_folders_and_forwarded():
    f = Fake()

    def source(root):
        yield {_added("/home/me/proj/a.txt"), _added("/home/me/proj/b.txt")}

    loop = f.loop("/home/me/proj", source, flush_floor_s=0.0)
    loop._run_one_watch()
    assert f.forwarded == [{"/home/me/proj"}]


def test_paths_under_an_ignored_tree_or_the_store_dir_are_dropped():
    """The index store writing itself must never trigger a flush — a scan
    writes parquet into cfg.dir, and without this filter the watcher would
    trigger the scan that triggers the watcher."""
    rules = IgnoreRules(default_ignore())
    mounts_dir = norm(os.path.expanduser("~/.fused-render/mounts"))
    dropped = make_dropped(rules, mounts_dir)

    assert dropped(norm(os.path.expanduser("~/proj/node_modules/pkg/index.js")))
    assert dropped(norm(os.path.expanduser("~/Library/Caches/com.example/x")))
    # the store itself, under the fused-render home MountGuard also covers
    assert dropped(norm(os.path.expanduser("~/.fused-render/index/dirs.parquet")))
    # an ordinary file is not dropped
    assert not dropped(norm(os.path.expanduser("~/proj/notes.txt")))


def test_the_filter_drives_what_a_watch_loop_ever_sees():
    """A batch entirely under an ignored tree never reaches a folder, never
    arms a flush."""
    f = Fake()

    def dropped(path):
        return "node_modules" in path

    def source(root):
        yield {_added("/home/me/proj/node_modules/pkg/index.js")}

    loop = WatchLoop("/home/me/proj", open_source=source, dropped=dropped,
                     forward=f.forward, now=f.now, last_scan=f.last_scan,
                     live_run_covers=f.live_run_covers, gate_open=f.gate_open,
                     sleep=f.sleep, stop_event=threading.Event(),
                     flush_floor_s=0.0)
    loop._run_one_watch()
    assert f.forwarded == []


def test_a_change_on_the_watched_root_itself_clamps_to_the_root_not_its_parent():
    """`_folder_of` returns the PARENT. A change on the watched root itself
    (touch/chmod on `~`, a rename or delete of the root, a top-level event in
    the non-recursive fallback) must forward the root, never the root's
    parent — forwarding `/Users` for a root of `~` would be a scan broader
    than any configured root."""
    f = Fake()

    def source(root):
        yield {_added("/home/me")}  # the root itself changed, not a child

    loop = f.loop("/home/me", source, flush_floor_s=0.0)
    loop._run_one_watch()
    assert f.forwarded == [{"/home/me"}]


# --------------------------------------------------------------- the floor


def test_two_batches_inside_the_floor_forward_once_with_the_union():
    f = Fake()

    ticks = [
        {_added("/home/me/proj/a.txt")},
        set(),  # an idle tick, still inside the floor
        {_added("/home/me/other/b.txt")},
        set(),  # the tick that finally crosses the floor
    ]
    times = [1000.0, 1010.0, 1015.0, 1000.0 + WATCH_FLUSH_FLOOR_S]

    def source(root):
        yield from ticks

    def now():
        return times.pop(0) if times else 1000.0 + WATCH_FLUSH_FLOOR_S

    loop = WatchLoop("/home/me", open_source=source, dropped=f.dropped,
                     forward=f.forward, now=now, last_scan=f.last_scan,
                     live_run_covers=f.live_run_covers, gate_open=f.gate_open,
                     sleep=f.sleep, stop_event=threading.Event())
    loop._run_one_watch()
    assert f.forwarded == [{"/home/me/proj", "/home/me/other"}]


# ------------------------------------------------------- MAX_FOLDERS collapse


def test_more_than_max_folders_in_one_flush_collapses_to_the_root():
    f = Fake()
    many = {_added(f"/home/me/d{i}/x.txt") for i in range(MAX_FOLDERS + 1)}

    def source(root):
        yield many

    loop = f.loop("/home/me", source, flush_floor_s=0.0)
    loop._run_one_watch()
    assert f.forwarded == [{"/home/me"}]


def test_max_folders_or_fewer_forward_the_actual_set():
    f = Fake()
    some = {_added(f"/home/me/d{i}/x.txt") for i in range(MAX_FOLDERS)}

    def source(root):
        yield some

    loop = f.loop("/home/me", source, flush_floor_s=0.0)
    loop._run_one_watch()
    assert f.forwarded == [{f"/home/me/d{i}" for i in range(MAX_FOLDERS)}]


# --------------------------------------------------------- errors / backoff


def test_a_source_that_raises_forwards_the_root_and_backs_off():
    f = Fake()

    def source(root):
        raise RuntimeError("kernel watch overflowed")
        yield  # pragma: no cover - makes this a generator function

    loop = f.loop("/home/me", source)
    loop._run_one_watch()
    assert f.forwarded == [{"/home/me"}]
    assert f.slept == [BACKOFF_SCHEDULE_S[0]]


def test_repeated_failures_escalate_the_documented_schedule_and_cap():
    f = Fake()

    def source(root):
        raise RuntimeError("still broken")
        yield  # pragma: no cover

    loop = f.loop("/home/me", source)
    for _ in range(len(BACKOFF_SCHEDULE_S) + 1):
        loop._run_one_watch()
    expected = list(BACKOFF_SCHEDULE_S) + [BACKOFF_SCHEDULE_S[-1]]
    assert f.slept == expected


def test_a_source_that_ends_with_stop_set_neither_forwards_nor_backs_off():
    f = Fake()
    stop = threading.Event()
    stop.set()

    def source(root):
        return iter(())  # the generator simply ends, no exception

    loop = f.loop("/home/me", source, stop_event=stop)
    loop._run_one_watch()
    assert f.forwarded == []
    assert f.slept == []


def test_a_healthy_tick_resets_the_backoff_counter():
    """One good watch after failures means the NEXT failure starts the
    schedule over, rather than picking up where it left off."""
    f = Fake()
    calls = {"n": 0}

    def flaky_source(root):
        calls["n"] += 1
        if calls["n"] in (1, 3):
            raise RuntimeError("boom")
            yield  # pragma: no cover
        yield set()  # a healthy tick

    loop = f.loop("/home/me", flaky_source, flush_floor_s=0.0)
    loop._run_one_watch()  # fails -> sleeps BACKOFF_SCHEDULE_S[0]
    loop._run_one_watch()  # succeeds -> resets
    loop._run_one_watch()  # fails again -> sleeps BACKOFF_SCHEDULE_S[0] again
    assert f.slept == [BACKOFF_SCHEDULE_S[0], BACKOFF_SCHEDULE_S[0]]


# ------------------------------------------------------- periodic safety net


def test_a_stale_root_is_rescanned_on_an_idle_tick():
    f = Fake(last_scan={"/home/me": 1000.0 - WATCH_RESCAN_S - 1})

    def source(root):
        yield set()  # idle tick, no changes at all

    loop = f.loop("/home/me", source)
    loop._run_one_watch()
    assert f.forwarded == [{"/home/me"}]


def test_a_live_run_over_the_root_suppresses_the_periodic_rescan():
    f = Fake(live={"/home/me"}, last_scan={"/home/me": 1000.0 - WATCH_RESCAN_S - 1})

    def source(root):
        yield set()

    loop = f.loop("/home/me", source)
    loop._run_one_watch()
    assert f.forwarded == []


def test_a_recently_scanned_root_is_not_rescanned_on_an_idle_tick():
    f = Fake(last_scan={"/home/me": 999.0})  # 1s ago

    def source(root):
        yield set()

    loop = f.loop("/home/me", source)
    loop._run_one_watch()
    assert f.forwarded == []


def test_a_never_scanned_root_is_rescanned_on_an_idle_tick():
    f = Fake()  # no last_scan entry at all

    def source(root):
        yield set()

    loop = f.loop("/home/me", source)
    loop._run_one_watch()
    assert f.forwarded == [{"/home/me"}]


# ----------------------------------------------------------------- the gate


def test_indexing_disabled_opens_nothing_and_forwards_nothing():
    f = Fake(gate=False)
    opened = []

    def source(root):
        opened.append(root)
        yield set()

    stop_calls = {"n": 0}

    class Stop:
        def is_set(self):
            stop_calls["n"] += 1
            return stop_calls["n"] > 2  # let run() poll the gate twice, then exit

    loop = f.loop("/home/me", source, stop_event=Stop())
    loop.run()
    assert opened == []
    assert f.forwarded == []
    assert f.slept == [loop.gate_poll_s, loop.gate_poll_s]


def test_flipping_the_gate_on_mid_run_opens_the_watch_on_the_next_poll():
    f = Fake(gate=False)
    opened = []

    def source(root):
        opened.append(root)
        f._gate = False  # only ever run once
        yield set()

    class Stop:
        def __init__(self):
            self.n = 0

        def is_set(self):
            self.n += 1
            if self.n == 2:
                f._gate = True  # flips on for the NEXT poll
            return self.n > 3

    loop = f.loop("/home/me", source, stop_event=Stop())
    loop.run()
    assert opened == ["/home/me"]


# ----------------------------------------------- the real forward callable


def test_a_real_flush_actually_reaches_the_rescan_queue(tmp_path, monkeypatch):
    """`_make_loop` wires `forward=index_touch.note_index_folders`, the REAL
    callable — not a fake with a friendlier shape. `note_index_folders` is
    `def note_index_folders(*folders: str | None)`; a `WatchLoop` that calls
    `self.forward({self.root})` hands it ONE positional argument holding a
    set, not the folders unpacked, so `note_index_folders` would filter that
    set out (it isn't a `str`) and queue nothing. This test crosses the real
    seam between `WatchLoop` and `index_touch` instead of stopping at a fake
    whose `forward(folders)` accepts an iterable and can't catch the bug."""
    monkeypatch.setattr(index_touch, "_queue", index_touch.RescanQueue(
        start=lambda root: None, live_run_covers=lambda root: False,
        blocked=lambda root: False, last_scan=lambda root: None,
        schedule=lambda delay, fn: None, now=time.time))
    monkeypatch.setattr("fused_render.shell.index_gate.indexing_allowed",
                        lambda: True)

    root = str(tmp_path)
    stop = threading.Event()
    loop = _make_loop(root, stop)
    loop.open_source = lambda r: iter([{_added(os.path.join(root, "a.txt"))}])
    loop.flush_floor_s = 0.0
    loop._run_one_watch()

    assert index_touch._queue._pending, (
        "a real flush must actually queue the folder with RescanQueue; "
        "got an empty _pending, meaning the forward call queued nothing")


# --------------------------------------------------------- real filesystem

def test_a_real_change_arrives_through_the_real_filter(tmp_path):
    """One end-to-end check against the real `watchfiles.watch`, generously
    timed: the Windows CI lane is starved and flaky on main already."""
    import watchfiles

    rules = IgnoreRules(default_ignore())
    dropped = make_dropped(rules, str(tmp_path / "not-a-real-mounts-dir"))
    seen = []
    stop = threading.Event()

    def source(root):
        def _filter(change, path):
            return not dropped(path)

        yield from watchfiles.watch(root, watch_filter=_filter, stop_event=stop,
                                    rust_timeout=1000, yield_on_timeout=True)

    def forward(folders):
        seen.append(set(folders))
        stop.set()

    loop = WatchLoop(str(tmp_path), open_source=source, dropped=lambda p: False,
                     forward=forward, now=time.time, last_scan=lambda r: time.time(),
                     live_run_covers=lambda r: False, gate_open=lambda: True,
                     sleep=lambda s: None, stop_event=stop, flush_floor_s=0.0)

    def write_soon():
        time.sleep(0.3)
        (tmp_path / "new-file.txt").write_text("hi", encoding="utf-8")

    writer = threading.Thread(target=write_soon, daemon=True)
    writer.start()
    loop._run_one_watch()
    writer.join(timeout=5)

    assert seen, "the real watch never saw the created file within the timeout"
    assert seen[0] == {str(tmp_path)}

"""The app's own writes make the index wrong; a targeted rescan makes it right.

There is no filesystem watcher, so the index is a snapshot: rename a file in
the explorer and the index keeps spelling the old name. The in-folder search
used to route around that — the folder was pinned to a live streamed walk for
the rest of the session — and with the walk gone for indexed folders that
escape hatch has to be replaced by the honest fix: scan the folder the app
just changed.

What is testable here, and what these tests pin, is the POLICY around that
scan rather than the scan itself: which folder gets scanned, that a burst of
mutations costs one scan and not fifty, that a mount is never scanned, and
that a scan already in flight over the folder is waited out rather than raced.
"""
import pytest

from fused_render.server.index_touch import RescanQueue


class Fake:
    """Deps for the queue: a clock, a scheduler, and the recorded effects."""

    def __init__(self, live=(), blocked=()):
        self.t = 1000.0
        self.started = []
        self.armed = []
        self.live = list(live)
        self.blocked = set(blocked)

    # -- injected deps
    def now(self):
        return self.t

    def schedule(self, delay, fn):
        self.armed.append(delay)
        self._fn = fn

    def start(self, root):
        self.started.append(root)

    def live_run_covers(self, root):
        return any(r == root or root.startswith(r + "/") or r.startswith(root + "/")
                   for r in self.live)

    def blocks(self, root):
        return root in self.blocked

    # -- driving
    def queue(self, **kw):
        return RescanQueue(start=self.start, live_run_covers=self.live_run_covers,
                           blocked=self.blocks, schedule=self.schedule,
                           now=self.now, **kw)

    def fire(self):
        self._fn()


def test_a_mutation_schedules_a_rescan_of_its_folder():
    f = Fake()
    q = f.queue()
    q.note("/home/me/proj/notes.txt")
    assert f.started == []  # coalesced, not immediate
    assert f.armed == [q.coalesce_s]
    f.fire()
    assert f.started == ["/home/me/proj"]


def test_a_burst_of_mutations_costs_one_scan():
    """Deleting fifty files in a folder is one change to the index, and fifty
    detached workers over one directory would be absurd."""
    f = Fake()
    q = f.queue()
    for i in range(50):
        q.note(f"/home/me/proj/f{i}.txt")
    f.fire()
    assert f.started == ["/home/me/proj"]
    assert len(f.armed) == 1  # one timer, re-noted while already armed


def test_a_renamed_directory_is_covered_by_its_parents():
    """Both ends of a move. A scan of the parent recurses, so the renamed
    directory's whole subtree is re-read under its new name without naming it
    separately."""
    f = Fake()
    q = f.queue()
    q.note("/home/me/proj/old", "/home/me/other/new")
    f.fire()
    assert sorted(f.started) == ["/home/me/other", "/home/me/proj"]


def test_nested_folders_collapse_to_the_outermost():
    """A scan of a folder covers everything under it; starting the inner one
    too would walk the same tree twice and compact twice."""
    f = Fake()
    q = f.queue()
    q.note("/home/me/proj/a.txt", "/home/me/proj/sub/b.txt")
    f.fire()
    assert f.started == ["/home/me/proj"]


def test_a_mount_backed_folder_is_never_scanned():
    """The structural refusal, checked here as well as in runner.start: a
    kernel crawl of an rclone mount can wedge it, and this path is reached by
    the app's own writes rather than by anything a user asked for."""
    f = Fake(blocked=["/home/me/.fused-render/mounts/s3"])
    q = f.queue()
    q.note("/home/me/.fused-render/mounts/s3/data.parquet")
    f.fire()
    assert f.started == []


def test_the_filesystem_root_is_never_scanned():
    """A file written directly into "/" must not spawn a whole-disk crawl."""
    f = Fake()
    q = f.queue()
    q.note("/loose.txt")
    assert f.armed == [] and f.started == []


def test_a_scan_already_covering_the_folder_is_waited_out():
    """Starting a second scan over a tree being scanned races the compaction
    for no benefit — and JOINING the live one is worse than useless here,
    since it may already have walked past the folder we just changed."""
    f = Fake(live=["/home/me"])
    q = f.queue()
    q.note("/home/me/proj/a.txt")
    f.fire()
    assert f.started == []
    assert f.armed == [q.coalesce_s, q.coalesce_s]  # re-armed, still pending
    f.live.clear()
    f.fire()
    assert f.started == ["/home/me/proj"]


def test_waiting_out_a_scan_has_a_ceiling():
    """A run that never ends (a wedged worker) must not keep one folder
    circling for the process lifetime."""
    f = Fake(live=["/home/me"])
    q = f.queue()
    q.note("/home/me/proj/a.txt")
    f.fire()
    f.t += q.deadline_s + 1
    f.fire()
    assert f.started == ["/home/me/proj"]
    f.fire()
    assert f.started == ["/home/me/proj"]  # and it is off the queue


def test_nothing_is_armed_when_there_is_nothing_to_do():
    f = Fake()
    q = f.queue()
    q.note("")
    assert f.armed == []


def test_a_start_that_fails_does_not_strand_the_rest():
    """One bad folder (gone between the mutation and the scan) must not stop
    the others, and must not raise into a request thread."""
    f = Fake()

    def start(root):
        f.started.append(root)
        if root == "/home/me/bad":
            raise ValueError("not a directory")

    q = RescanQueue(start=start, live_run_covers=f.live_run_covers,
                    blocked=f.blocks, schedule=f.schedule, now=f.now)
    q.note("/home/me/bad/x.txt", "/home/me/good/y.txt")
    f.fire()
    assert sorted(f.started) == ["/home/me/bad", "/home/me/good"]


@pytest.mark.parametrize("route", ["write", "mkdir", "delete", "rename", "copy",
                                   "compress", "upload", "trash_move"])
def test_every_mutating_route_reports_what_it_changed(route):
    """Source guard. The point of this module is that the index learns about
    EVERY change the app makes; a route added without the call is a folder the
    search box silently keeps lying about."""
    import inspect

    from fused_render.server import fs_mutate

    fn = getattr(fs_mutate, "api_fs_" + route)
    assert "note_index_mutation" in inspect.getsource(fn)

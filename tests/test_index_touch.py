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
from fused_render.index.runner import canonical_root
from fused_render.server.index_touch import RescanQueue

# `RescanQueue.note()` resolves every path it is given through `_folder_of`
# (index_touch.py), which is `norm(os.path.abspath(...))` — the same
# canonicalization `canonical_root` does. Every hardcoded "/home/me/..."
# literal below is a no-op of that on POSIX (already absolute), but on
# Windows a leading-slash-no-drive literal is only drive-RELATIVE, so it
# resolves against the runner's current drive (e.g. "D:/home/me/proj"). The
# FILE paths handed to `q.note(...)` are left as raw literals — that mirrors
# a real caller, and `_folder_of` canonicalizes them itself — but every
# FOLDER identity these tests compare against (`Fake(live=...)`,
# `Fake(blocked=...)`, `Fake(last_scan=...)` keys, and `f.started`) has to be
# run through the same canonicalization or it silently never matches what
# `note()` actually produced.


class Fake:
    """Deps for the queue: a clock, a scheduler, and the recorded effects."""

    def __init__(self, live=(), blocked=(), last_scan=None):
        self.t = 1000.0
        self.started = []
        self.armed = []
        self.live = list(live)
        self.blocked = set(blocked)
        self.scans = dict(last_scan or {})

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
        # Tree-wise, like the real dep: the mount guard and the ignore rules
        # both answer for everything under a blocked path, not just for it.
        return any(root == b or root.startswith(b + "/") for b in self.blocked)

    def last_scan(self, root):
        return self.scans.get(root)

    # -- driving
    def queue(self, **kw):
        return RescanQueue(start=self.start, live_run_covers=self.live_run_covers,
                           blocked=self.blocks, last_scan=self.last_scan,
                           schedule=self.schedule, now=self.now, **kw)

    def fire(self):
        self._fn()


def test_a_mutation_schedules_a_rescan_of_its_folder():
    f = Fake()
    q = f.queue()
    q.note("/home/me/proj/notes.txt")
    assert f.started == []  # coalesced, not immediate
    assert f.armed == [q.coalesce_s]
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]


def test_a_burst_of_mutations_costs_one_scan():
    """Deleting fifty files in a folder is one change to the index, and fifty
    detached workers over one directory would be absurd."""
    f = Fake()
    q = f.queue()
    for i in range(50):
        q.note(f"/home/me/proj/f{i}.txt")
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]
    assert len(f.armed) == 1  # one timer, re-noted while already armed


def test_a_renamed_directory_is_covered_by_its_parents():
    """Both ends of a move. A scan of the parent recurses, so the renamed
    directory's whole subtree is re-read under its new name without naming it
    separately."""
    f = Fake()
    q = f.queue()
    q.note("/home/me/proj/old", "/home/me/other/new")
    f.fire()
    assert sorted(f.started) == sorted(
        [canonical_root("/home/me/other"), canonical_root("/home/me/proj")])


def test_nested_folders_collapse_to_the_outermost():
    """A scan of a folder covers everything under it; starting the inner one
    too would walk the same tree twice and compact twice."""
    f = Fake()
    q = f.queue()
    q.note("/home/me/proj/a.txt", "/home/me/proj/sub/b.txt")
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]


def test_a_mount_backed_folder_is_never_scanned():
    """The structural refusal, checked here as well as in runner.start: a
    kernel crawl of an rclone mount can wedge it, and this path is reached by
    the app's own writes rather than by anything a user asked for."""
    f = Fake(blocked=[canonical_root("/home/me/.fused-render/mounts/s3")])
    q = f.queue()
    q.note("/home/me/.fused-render/mounts/s3/data.parquet")
    f.fire()
    assert f.started == []


def test_the_filesystem_root_is_never_scanned():
    """A file written directly into "/" must not spawn a whole-disk crawl.

    NOT a canonical_root() fixture mismatch like its neighbours: `_folder_of`
    (index_touch.py) guards against a bare root with `parent in ("", "/")`
    after `norm(os.path.abspath(...))`, and that guard only recognized the
    POSIX spelling — on Windows `os.path.dirname("D:/loose.txt")` is `"D:/"`,
    truthy and not `"/"`, so a drive letter's own root slipped through the
    same way `query.search_under`'s `.rstrip("/") or "/"` would (both
    generalize the POSIX bare-root special case, not the Windows one).
    `_folder_of`'s `_DRIVE_ROOT` regex now recognizes that form too."""
    f = Fake()
    q = f.queue()
    q.note("/loose.txt")
    assert f.armed == [] and f.started == []


def test_a_scan_already_covering_the_folder_is_waited_out():
    """Starting a second scan over a tree being scanned races the compaction
    for no benefit — and JOINING the live one is worse than useless here,
    since it may already have walked past the folder we just changed."""
    f = Fake(live=[canonical_root("/home/me")])
    q = f.queue()
    q.note("/home/me/proj/a.txt")
    f.fire()
    assert f.started == []
    assert f.armed == [q.coalesce_s, q.coalesce_s]  # re-armed, still pending
    f.live.clear()
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]


def test_waiting_out_a_scan_has_a_ceiling():
    """A run that never ends (a wedged worker) must not keep one folder
    circling for the process lifetime."""
    f = Fake(live=[canonical_root("/home/me")])
    q = f.queue()
    q.note("/home/me/proj/a.txt")
    f.fire()
    f.t += q.deadline_s + 1
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]  # and it is off the queue


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
        if root == canonical_root("/home/me/bad"):
            raise ValueError("not a directory")

    q = RescanQueue(start=start, live_run_covers=f.live_run_covers,
                    blocked=f.blocks, last_scan=f.last_scan,
                    schedule=f.schedule, now=f.now)
    q.note("/home/me/bad/x.txt", "/home/me/good/y.txt")
    f.fire()
    assert sorted(f.started) == sorted(
        [canonical_root("/home/me/bad"), canonical_root("/home/me/good")])


def _mutating_routes():
    """Every POST handler on the fs-mutation router, by name.

    ENUMERATED, not listed: a hardcoded list cannot fail for a route that does
    not exist yet, which is the only thing this guard is for. /api/fs/trash-move
    arrived on main while this module was being written and would have shipped
    without telling the index anything."""
    from fused_render.server import fs_mutate

    out = []
    for route in fs_mutate.router.routes:
        if "POST" not in getattr(route, "methods", set()):
            continue
        out.append(route.endpoint)
    assert out, "no POST routes found — has the router moved?"
    return out


# Routes that legitimately change nothing the index stores. Named one by one,
# with the reason, so adding a route here is a decision somebody made rather
# than a name that quietly matched a pattern.
NOT_A_PATH_CHANGE: dict = {}


def test_every_mutating_route_reports_what_it_changed():
    """The point of this module is that the index learns about EVERY change the
    app makes; a route added without the call is a folder the search box
    silently keeps lying about."""
    import inspect

    missing = []
    for fn in _mutating_routes():
        if fn.__name__ in NOT_A_PATH_CHANGE:
            continue
        if "_note_index_mutation" not in inspect.getsource(fn):
            missing.append(fn.__name__)
    assert missing == []


def test_the_guard_would_notice_a_new_route():
    """...and it can only mean that if it actually reads the router."""
    names = [fn.__name__ for fn in _mutating_routes()]
    assert "api_fs_rename" in names and "api_fs_trash_move" in names
    assert len(names) >= 8


# ---------------------------------------------------------------- the floor
#
# Every scan of a folder ends in a COMPACTION, and a compaction re-sorts and
# rewrites every partition in the store plus dirs.parquet — "keep the rows
# outside this root" is a query predicate, not an incremental write. So the
# cost of a rescan is a function of the whole index, not of the folder, and a
# mechanism that fires on every write is a mechanism that rewrites a 571k-row
# store on a cadence set by whoever is typing.

def test_a_folder_scanned_moments_ago_waits_rather_than_rescanning():
    f = Fake(last_scan={canonical_root("/home/me/proj"): 995.0})  # 5s ago
    q = f.queue()
    q.note("/home/me/proj/a.txt")
    f.fire()
    assert f.started == []
    assert f.armed == [q.coalesce_s, q.coalesce_s]  # deferred, still pending


def test_the_deferred_rescan_is_not_lost():
    """DEFERRED, never dropped. A rename whose rescan is skipped outright is a
    file the search cannot find until something else happens to scan — which is
    the exact failure this whole mechanism exists to prevent."""
    f = Fake(last_scan={canonical_root("/home/me/proj"): 995.0})
    q = f.queue()
    q.note("/home/me/proj/a.txt")
    f.fire()
    f.t += q.floor_s
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]


def test_the_floor_cannot_hold_a_folder_past_the_deadline():
    f = Fake(last_scan={canonical_root("/home/me/proj"): 995.0})
    q = f.queue()
    q.note("/home/me/proj/a.txt")
    f.fire()
    f.scans[canonical_root("/home/me/proj")] = f.t  # something keeps rescanning it
    f.t += q.deadline_s + 1
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]


def test_a_folder_never_scanned_has_no_floor_to_clear():
    f = Fake()
    q = f.queue()
    q.note("/home/me/proj/a.txt")
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]


def test_a_folder_the_scan_rules_exclude_is_never_rescanned():
    """A save inside node_modules would otherwise spawn a worker that walks it,
    indexes nothing, and rewrites the whole store to say so."""
    f = Fake(blocked=[canonical_root("/home/me/proj/node_modules")])
    q = f.queue()
    q.note("/home/me/proj/node_modules/pkg/index.js")
    f.fire()
    assert f.started == []


# ------------------------------------------------- what a write is worth
#
# The route half of the same argument.

def _spy_on_the_index(monkeypatch):
    """Watch what the routes report. The autouse fixture in conftest stubs the
    real entry point (a mutation must not spawn a scan from a test), so a test
    that is ABOUT what gets reported has to put its own spy in its place."""
    seen = []

    from fused_render.server import fs_mutate

    monkeypatch.setattr(fs_mutate, "note_index_mutation",
                        lambda *paths: seen.extend(paths))
    return seen


def test_overwriting_a_file_reports_nothing(tmp_path, monkeypatch):
    """The index stores NAMES. Overwriting a file changes its bytes, and the
    markdown editor autosaves every 2 seconds — so reporting every write means
    rewriting the whole store for as long as somebody is typing a note."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    seen = _spy_on_the_index(monkeypatch)
    target = tmp_path / "note.md"
    target.write_text("before", encoding="utf-8")
    client = TestClient(create_app(start_dir=str(tmp_path)))
    resp = client.post("/api/fs/write",
                       json={"path": str(target), "content": "after"},
                       headers={"X-Fused": "1"})
    assert resp.status_code == 200
    assert seen == []


def test_a_write_that_creates_the_file_DOES_report(tmp_path, monkeypatch):
    """`create` is the caller's "409 rather than clobber" flag, not a
    statement about the name set: `fused.writeFile("out.csv", data)` — the
    documented page pattern — creates a file with create unset. What decides
    is whether the path was there before."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    seen = _spy_on_the_index(monkeypatch)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    fresh = tmp_path / "out.csv"
    resp = client.post("/api/fs/write",
                       json={"path": str(fresh), "content": "a,b\n"},
                       headers={"X-Fused": "1"})
    assert resp.status_code == 200 and fresh.exists()
    assert seen == [str(fresh)]


def test_an_explicit_create_reports_too(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    seen = _spy_on_the_index(monkeypatch)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    fresh = tmp_path / "new.md"
    client.post("/api/fs/write",
                json={"path": str(fresh), "content": "hi", "create": True},
                headers={"X-Fused": "1"})
    assert seen == [str(fresh)]


def test_a_refused_write_reports_nothing(tmp_path, monkeypatch):
    """A 409 or a 403 changed nothing, so there is nothing to rescan."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    seen = _spy_on_the_index(monkeypatch)
    target = tmp_path / "note.md"
    target.write_text("before", encoding="utf-8")
    client = TestClient(create_app(start_dir=str(tmp_path)))
    resp = client.post("/api/fs/write",
                       json={"path": str(target), "content": "x", "create": True},
                       headers={"X-Fused": "1"})
    assert resp.status_code == 409
    assert seen == []


def test_the_write_response_says_whether_it_created(tmp_path, monkeypatch):
    """The client's caption has to match the server's decision, and it cannot
    work this out for itself — the file exists either way by the time it
    looks."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    _spy_on_the_index(monkeypatch)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    fresh = tmp_path / "out.csv"
    assert client.post("/api/fs/write", json={"path": str(fresh), "content": "a"},
                       headers={"X-Fused": "1"}).json()["created"] is True
    assert client.post("/api/fs/write", json={"path": str(fresh), "content": "b"},
                       headers={"X-Fused": "1"}).json()["created"] is False

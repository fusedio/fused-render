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
        self.start_hints = []
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

    def start(self, root, hint=None):
        self.started.append(root)
        self.start_hints.append(hint)

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


def test_more_than_max_folders_defers_the_rest_instead_of_losing_them():
    """`_outermost` used to truncate with `out[:MAX_FOLDERS]` AFTER `_fire`
    had already cleared `self._pending` — so folders past the sixteenth
    were dropped permanently, never scanned. This needed a pathological
    caller before RescanQueue.note_folders existed; the live watcher makes
    exceeding MAX_FOLDERS in one burst ordinary. The excess must stay
    pending for the next cycle, the same as a folder deferred for a live
    run or a floor."""
    from fused_render.server.index_touch import MAX_FOLDERS

    f = Fake()
    q = f.queue()
    many = [f"/home/me/d{i:03d}" for i in range(MAX_FOLDERS + 3)]
    q.note_folders(*many)
    f.fire()
    assert len(f.started) == MAX_FOLDERS, (
        "one cycle still scans at most MAX_FOLDERS — the rest defer, they "
        "don't all fire at once")
    assert f.armed[-1] == q.coalesce_s  # re-armed for the deferred excess

    f.fire()  # the deferred cycle
    assert len(f.started) == MAX_FOLDERS + 3, (
        "the folders past the sixteenth must eventually be scanned too, "
        "not lost")
    assert sorted(f.started) == sorted(canonical_root(p) for p in many)


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


# ------------------------------------------------- note_folders (D-watch)
#
# The live filesystem watcher (index_watch.py) already knows the folder a
# change belongs to — it reduces raw paths to `_folder_of(path)` itself, at
# the batching layer, so it can collapse the outermost-only set BEFORE the
# flush floor decides how much churn to report. Routing that back through
# `_folder_of` a second time here would be a no-op for an ordinary folder but
# wrong for a scan ROOT: `_folder_of` always returns the PARENT, so a watcher
# forwarding `{root}` on overflow must not have it turned into the root's own
# parent.

def test_note_folders_takes_folders_as_is_not_their_parent():
    f = Fake()
    q = f.queue()
    q.note_folders("/home/me/proj", "/home/me/other")
    f.fire()
    assert sorted(f.started) == sorted(
        [canonical_root("/home/me/proj"), canonical_root("/home/me/other")])


def test_note_folders_still_collapses_to_the_outermost():
    f = Fake()
    q = f.queue()
    q.note_folders("/home/me/proj", "/home/me/proj/sub")
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]


def test_note_folders_never_queues_the_filesystem_root():
    """`note()` routes through `_folder_of`, which refuses a bare "/" (and a
    bare Windows drive root). `note_folders` routes through `_canon_folder`
    instead, whose last line is `return norm(...).rstrip("/") or "/"` — it
    PRODUCES "/" for a root-ish input rather than refusing it. Without the
    same refusal, `note_folders("/")` queues a whole-disk crawl, exactly the
    thing the module docstring says never happens ("Never a mount, never
    `/`")."""
    f = Fake()
    q = f.queue()
    q.note_folders("/")
    assert f.armed == []  # refused before it was ever queued, nothing to fire
    assert f.started == []


# --------------------------------------------------------- hint wiring
# (SPEC-scan-cost.md part 2: a folder noted through `note_folders` — the
# watcher already observed it change, in process — is scanned with a
# `forced` hint instead of an unhinted (journal-replaying, or full) scan. A
# folder noted through `note()` (an app mutation) never is: a rename needs a
# real recursive walk of the new name's subtree, which nothing "hints" at.

def test_a_single_watcher_folder_is_started_with_itself_as_the_hint():
    f = Fake()
    q = f.queue()
    q.note_folders("/home/me/proj")
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]
    assert f.start_hints == [([canonical_root("/home/me/proj")], [])]


def test_a_mutation_folder_is_started_with_no_hint():
    f = Fake()
    q = f.queue()
    q.note("/home/me/proj/notes.txt")
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]
    assert f.start_hints == [None]


def test_collapsed_watcher_folders_hint_every_absorbed_folder_not_just_the_root():
    """`proj` and `proj/sub` both come from the watcher and collapse to one
    scan of `proj` (outermost-only). A hint of `[proj]` alone would miss
    `sub`: `_run_fsevents` only force-visits a dir it is told about, or a
    brand-new one discovered under a forced dir — `sub`, already in the dir
    cache, is neither. The hint must carry both originally-noted folders."""
    f = Fake()
    q = f.queue()
    q.note_folders("/home/me/proj", "/home/me/proj/sub")
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]
    assert f.start_hints == [
        (sorted([canonical_root("/home/me/proj"),
                canonical_root("/home/me/proj/sub")]), [])]


def test_a_mutation_absorbed_into_a_watcher_folder_falls_back_to_no_hint():
    """`proj` (watcher) and `proj/sub` (an app mutation, e.g. a rename)
    collapse to one scan of `proj`. `sub`'s new name has no "originally
    noted" dir a hint could name, so the whole thing must fall back to a
    real recursive scan rather than a forced hint that would miss it."""
    f = Fake()
    q = f.queue()
    q.note_folders("/home/me/proj")
    q.note("/home/me/proj/sub/renamed.txt")
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]
    assert f.start_hints == [None]


def test_the_same_folder_noted_both_ways_falls_back_to_no_hint():
    """A folder noted via `note_folders` AND via `note` in the same
    coalescing window is poisoned back to unhinted — the mutation's own
    reason (a possible rename) applies regardless of note order."""
    f = Fake()
    q = f.queue()
    q.note("/home/me/proj/notes.txt")  # folder is /home/me/proj
    q.note_folders("/home/me/proj")
    f.fire()
    assert f.started == [canonical_root("/home/me/proj")]
    assert f.start_hints == [None]


def test_disjoint_watcher_folders_each_get_their_own_hint():
    f = Fake()
    q = f.queue()
    q.note_folders("/home/me/proj", "/home/me/other")
    f.fire()
    assert sorted(f.started) == sorted(
        [canonical_root("/home/me/proj"), canonical_root("/home/me/other")])
    for root, hint in zip(f.started, f.start_hints):
        assert hint == ([root], [])


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


# ------------------------------------------------ the module-level entry
#
# `note_index_mutation` itself, not the injectable `RescanQueue` the tests
# above exercise directly — the toggle is checked at this one entry point.

def test_note_index_mutation_no_ops_while_indexing_is_off(monkeypatch, tmp_path):
    import fused_render.shell.prefs as prefs_mod
    from fused_render.server import index_touch

    monkeypatch.setattr(prefs_mod, "indexing_enabled", lambda: False)
    noted = []
    monkeypatch.setattr(index_touch._queue, "note", lambda *p: noted.append(p))
    index_touch.note_index_mutation(str(tmp_path / "a.txt"))
    assert noted == []


def test_note_index_mutation_queues_normally_while_indexing_is_on(monkeypatch,
                                                                   tmp_path):
    import fused_render.shell.prefs as prefs_mod
    from fused_render.server import index_touch

    monkeypatch.setattr(prefs_mod, "indexing_enabled", lambda: True)
    noted = []
    monkeypatch.setattr(index_touch._queue, "note", lambda *p: noted.append(p))
    path = str(tmp_path / "a.txt")
    index_touch.note_index_mutation(path)
    assert noted == [(path,)]


def test_note_index_folders_no_ops_while_indexing_is_off(monkeypatch, tmp_path):
    import fused_render.shell.prefs as prefs_mod
    from fused_render.server import index_touch

    monkeypatch.setattr(prefs_mod, "indexing_enabled", lambda: False)
    noted = []
    monkeypatch.setattr(index_touch._queue, "note_folders",
                        lambda *f: noted.append(f))
    index_touch.note_index_folders(str(tmp_path))
    assert noted == []


def test_note_index_folders_queues_normally_while_indexing_is_on(monkeypatch,
                                                                   tmp_path):
    import fused_render.shell.prefs as prefs_mod
    from fused_render.server import index_touch

    monkeypatch.setattr(prefs_mod, "indexing_enabled", lambda: True)
    noted = []
    monkeypatch.setattr(index_touch._queue, "note_folders",
                        lambda *f: noted.append(f))
    folder = str(tmp_path)
    index_touch.note_index_folders(folder)
    assert noted == [(folder,)]


# ---------------------------------------------- the bridge wake (D732)
#
# `_real_start` is the sixth path that calls `runner.start` (the other five
# live in routers/index.py and all wake the job bridge themselves). Without
# the wake here, a mutation-triggered rescan's Activity row can lag behind
# the idle backoff by up to INDEX_JOB_IDLE_S.

def test_real_start_wakes_the_index_job_bridge(monkeypatch, tmp_path):
    from fused_render.index import runner
    from fused_render.server import index_touch
    from fused_render.server.routers import index as index_router

    monkeypatch.setattr(runner, "start",
                        lambda cfg, root, hint=None: {"run_id": "r1"})
    woke = []
    monkeypatch.setattr(index_router, "_wake_index_job_bridge",
                        lambda: woke.append(True))
    index_touch._real_start(str(tmp_path))
    assert woke == [True]


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

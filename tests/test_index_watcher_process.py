"""The live watcher's process side: what it watches (fused_render/index/watcher.py)
and how its events reach the server (`index_watch._process_source`).

The watching runs outside the server because `watchfiles` sets its watches up
with the GIL held; these tests pin the pruned watch plan, the re-plan
triggers, the JSON-lines plumbing, and the process lifetime (it ends with the
generator, and on stdin EOF when the server is gone).
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from watchfiles import Change

from fused_render.index.ignore import IgnoreRules, default_ignore, norm
from fused_render.index.watcher import Pruner, _Session, plan_watch
from fused_render.server import index_watch
from fused_render.server.index_watch import WATCHER_MODULE, _process_source


def _tree(root, *dirs):
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)


def _pruner(root, **kw):
    return Pruner(str(root), IgnoreRules(default_ignore()),
                  kw.pop("mounts_dir", str(root / "state" / "mounts")), **kw)


def _n(root, *rel):
    return sorted(norm(str(root / r)) if r else norm(str(root)) for r in rel)


# ------------------------------------------------------------------ the plan


def test_clean_subtrees_are_watched_recursively_and_their_ancestors_flat(tmp_path):
    root = tmp_path / "home"
    _tree(root, "docs/a/b", "empty", "proj/src/deep", "proj/node_modules/pkg",
          "repo/.git/objects", "repo/lib", "state/mounts/s3/bucket", "idx/runs")
    (root / "a.txt").write_text("x", encoding="utf-8")

    recursive, flat = plan_watch(str(root), _pruner(root, index_dir=str(root / "idx")))

    assert recursive == _n(root, "docs", "empty", "proj/src", "repo/lib")
    # The root is flat because it holds the mounts dir and the index dir;
    # `proj` for its node_modules, `repo` for its .git, `state` for mounts.
    assert flat == _n(root, "", "proj", "repo", "state")


def test_nothing_pruned_means_one_recursive_watch_of_the_root(tmp_path):
    _tree(tmp_path, "a/b/c", "d")

    recursive, flat = plan_watch(str(tmp_path), _pruner(tmp_path))

    assert (recursive, flat) == (_n(tmp_path, ""), [])


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
def test_a_symlinked_directory_keeps_its_parent_off_the_recursive_watch(tmp_path):
    """The recursive watch follows symlinks and the index walk does not, so
    a directory holding a link to another tree must not be watched
    recursively."""
    _tree(tmp_path, "home/linkparent/real", "elsewhere/huge")
    os.symlink(tmp_path / "elsewhere", tmp_path / "home" / "linkparent" / "link")
    root = tmp_path / "home"

    recursive, flat = plan_watch(str(root), _pruner(root))

    assert recursive == _n(root, "linkparent/real")
    assert flat == _n(root, "", "linkparent")


def test_a_directory_on_another_filesystem_is_not_watched(tmp_path, monkeypatch):
    """The scan never leaves the root's device; neither does the watch."""
    _tree(tmp_path, "mnt/remote", "local")
    pruner = _pruner(tmp_path)
    real_lstat = os.lstat
    foreign = norm(str(tmp_path / "mnt"))

    class Stat:
        st_dev = -1

    def lstat(p, *a, **kw):
        return Stat() if norm(str(p)) == foreign else real_lstat(p, *a, **kw)

    monkeypatch.setattr(os, "lstat", lstat)
    recursive, flat = plan_watch(str(tmp_path), pruner)
    monkeypatch.undo()

    assert recursive == _n(tmp_path, "local")
    assert flat == _n(tmp_path, "")


def test_the_shallow_plan_is_the_root_and_its_kept_children_non_recursively(tmp_path):
    _tree(tmp_path, "docs/a", "node_modules/x", "Downloads")

    recursive, flat = plan_watch(str(tmp_path), _pruner(tmp_path), shallow=True)

    assert recursive == []
    assert flat == _n(tmp_path, "", "Downloads", "docs")


# ------------------------------------------------------------------ re-plan


def test_what_makes_a_plan_stale(tmp_path):
    root = tmp_path
    _tree(root, "docs/sub", "docs/node_modules", "proj/node_modules", "newdir")
    (root / "b.txt").write_text("x", encoding="utf-8")
    session = _Session(_n(root, "docs"), _n(root, "", "proj"),
                       _pruner(root), out=None)

    def stale(change, rel):
        return session.stale_by(change, str(root / rel))

    # A new folder under a flat watch: nothing watches inside it yet.
    assert stale(Change.added, "newdir")
    # An ignored folder appearing inside a recursive watch.
    assert stale(Change.added, "docs/node_modules")
    # Neither changes the plan.
    assert not stale(Change.added, "b.txt")
    assert not stale(Change.added, "docs/sub")
    assert not stale(Change.added, "proj/node_modules")
    assert not stale(Change.modified, "newdir")


def test_the_shallow_plan_is_stale_only_for_a_new_folder_under_the_root(tmp_path):
    """The shallow plan watches the root and its children, never deeper, so
    only a new kept child of the root changes it."""
    root = tmp_path
    _tree(root, "Downloads/new", "Downloads/node_modules", "Music")
    pruner = _pruner(root)
    recursive, flat = plan_watch(norm(str(root)), pruner, shallow=True)
    session = _Session(recursive, flat, pruner, out=None,
                       grows_under=[norm(str(root))])

    assert session.stale_by(Change.added, str(root / "Music"))
    assert not session.stale_by(Change.added, str(root / "Downloads" / "new"))
    assert not session.stale_by(Change.added,
                                str(root / "Downloads" / "node_modules"))


def test_a_whole_tree_session_never_goes_stale(tmp_path):
    """The darwin watch is `[root]` recursive with `replan=False`: the same
    shape a planned, nothing-pruned root has, so the opt-out is explicit and
    an ignored folder appearing inside it must not end the session."""
    root = tmp_path
    _tree(root, "proj/node_modules")
    pruner = _pruner(root)
    whole = _Session([norm(str(root))], [], pruner, out=None, replan=False)
    planned = _Session([norm(str(root))], [], pruner, out=None)
    added = str(root / "proj" / "node_modules")

    assert whole._filter(Change.added, added) is False  # still pruned
    assert not whole.done.is_set()
    assert planned._filter(Change.added, added) is False
    assert planned.done.is_set()


# --------------------------------------------------------------- the plumbing


FAKE_CHILD = r"""
import json, sys
print(json.dumps({"log": "planned"}), flush=True)
print(json.dumps({"changes": [[1, "/r/a.txt"], [2, "/r/b.txt"]]}), flush=True)
print("not json", flush=True)
mode = sys.argv[1]
if mode == "error":
    print(json.dumps({"error": "OSError: boom"}), flush=True)
    sys.exit(1)
if mode == "exit":
    sys.exit(3)
sys.stdin.read()
"""


@pytest.fixture
def spawned(monkeypatch):
    procs = []
    real = subprocess.Popen

    def popen(*a, **kw):
        p = real(*a, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(index_watch.subprocess, "Popen", popen)
    return procs


def _fake(mode):
    return [sys.executable, "-c", FAKE_CHILD, mode]


def test_changes_arrive_as_watchfiles_batches_then_ticks(spawned, caplog):
    stop = threading.Event()
    caplog.set_level("INFO", logger="fused_render.server.index_watch")
    source = _process_source(_fake("wait"), stop, tick_s=0.2, poll_s=0.05)

    assert next(source) == {(Change.added, "/r/a.txt"), (Change.modified, "/r/b.txt")}
    assert next(source) == set()  # an idle tick
    assert "planned" in caplog.text

    stop.set()
    assert list(source) == []
    assert spawned[0].poll() is not None, "the watcher outlived its source"


def test_closing_the_source_ends_the_process(spawned):
    source = _process_source(_fake("wait"), threading.Event(), poll_s=0.05)
    next(source)
    source.close()
    assert spawned[0].poll() is not None


def test_a_reported_error_raises_for_the_backoff(spawned):
    source = _process_source(_fake("error"), threading.Event(), poll_s=0.05)
    next(source)
    with pytest.raises(OSError, match="boom"):
        next(source)


def test_a_process_that_exits_on_its_own_raises(spawned):
    source = _process_source(_fake("exit"), threading.Event(), poll_s=0.05)
    next(source)
    with pytest.raises(RuntimeError, match="code 3"):
        next(source)


@pytest.mark.skipif(not getattr(subprocess, "_USE_POSIX_SPAWN", False),
                    reason="this platform's CPython never uses posix_spawn")
def test_the_watcher_is_posix_spawned_and_inherits_no_listening_socket(monkeypatch):
    """A fork() of a server with PROJ loaded dies with SIGSEGV before exec, so
    the spawn must take CPython's posix_spawn path; and leaving fds open for
    that must not hand the child the server's listening socket."""
    import socket
    spawns = []
    real = subprocess.Popen._posix_spawn

    def record(self, *a, **kw):
        spawns.append(a[0])
        return real(self, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "_posix_spawn", record)
    procs = []
    real_popen = subprocess.Popen

    def popen(*a, **kw):
        procs.append(real_popen(*a, **kw))
        return procs[-1]

    monkeypatch.setattr(index_watch.subprocess, "Popen", popen)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        source = _process_source(_fake("wait"), threading.Event(), poll_s=0.05)
        try:
            next(source)
            assert spawns == [_fake("wait")], "the watcher was forked"
            fd_dir = f"/proc/{procs[0].pid}/fd"
            if os.path.isdir(fd_dir):
                inode = f"socket:[{os.fstat(listener.fileno()).st_ino}]"
                held = {os.readlink(os.path.join(fd_dir, fd))
                        for fd in os.listdir(fd_dir)}
                assert inode not in held, "the watcher holds the listening socket"
        finally:
            source.close()
    assert procs[0].poll() is not None


# ------------------------------------------------------ the real watcher process


def _watcher_argv(root):
    spec = {"root": str(root), "ignore": default_ignore(),
            "mounts_dir": str(root / "not-a-mounts-dir"), "index_dir": None}
    return [sys.executable, "-m", WATCHER_MODULE, json.dumps(spec)]


def test_the_watcher_exits_when_its_stdin_closes(tmp_path):
    """How it dies with the server: the server holds the pipe's write end."""
    proc = subprocess.Popen(_watcher_argv(tmp_path), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    try:
        first = json.loads(proc.stdout.readline())
        assert "log" in first, first
        proc.stdin.close()
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def _wait_for(source, want, timeout=30.0):
    seen = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for _change, path in next(source):
            seen.add(norm(path))
        if want <= seen:
            return seen
    return seen


def test_a_real_change_arrives_from_the_real_watcher(tmp_path):
    """End to end against the real `watchfiles` in the real process, generously
    timed: the Windows CI lane is starved. The root holds a pruned folder, so
    it is watched non-recursively — a file in the root, one in a recursively
    watched child, and one in a folder created after the watch opened (which
    only a re-plan can cover) all have to arrive."""
    _tree(tmp_path, "node_modules/pkg", "docs")
    stop = threading.Event()
    source = _process_source(_watcher_argv(tmp_path), stop, tick_s=0.5)
    try:
        next(source)  # the watch is open once the first tick arrives
        time.sleep(1.0)
        (tmp_path / "top.txt").write_text("hi", encoding="utf-8")
        (tmp_path / "docs" / "doc.txt").write_text("hi", encoding="utf-8")
        (tmp_path / "node_modules" / "pkg" / "ignored.js").write_text("x", encoding="utf-8")
        want = {norm(str(tmp_path / "top.txt")), norm(str(tmp_path / "docs" / "doc.txt"))}
        seen = _wait_for(source, want)
        assert want <= seen, seen
        assert norm(str(tmp_path / "node_modules" / "pkg" / "ignored.js")) not in seen

        (tmp_path / "later").mkdir()
        late = tmp_path / "later" / "late.txt"
        deadline = time.time() + 30
        seen = set()
        while time.time() < deadline and norm(str(late)) not in seen:
            # Rewritten until seen: the first writes can land before the
            # re-plan has watched the new folder.
            late.write_text(str(time.time()), encoding="utf-8")
            seen |= _wait_for(source, {norm(str(late))}, timeout=2.0)
        assert norm(str(late)) in seen, seen
    finally:
        stop.set()
        source.close()

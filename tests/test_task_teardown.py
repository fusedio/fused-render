"""Archive tears a task down (Workspace scope, unit 3): its processes end, its
clean and pushed worktrees go, and every worktree it keeps is reported with the
reason. Real git repositories in tmp dirs — the unpushed and dirty rules are
git's, and a faked `_git` would only prove the fake."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from fused_render.server import task_teardown as td

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _run(cwd, *args):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    """A main checkout with a pushed `main` and a bare origin."""
    origin = tmp_path / "origin.git"
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    root.mkdir()
    _run(root, "init", "-b", "main")
    (root / "a.txt").write_text("a")
    _run(root, "add", ".")
    _run(root, "commit", "-m", "one")
    _run(root, "remote", "add", "origin", str(origin))
    _run(root, "push", "-u", "origin", "main")
    return root


def _worktree(repo, name, *, push=False):
    path = repo / ".claude" / "worktrees" / name
    _run(repo, "worktree", "add", "-b", f"worktree-{name}", str(path))
    if push:
        _run(path, "push", "-u", "origin", f"worktree-{name}")
    return str(path)


def test_a_clean_worktree_with_nothing_new_holds_nothing_open(repo):
    path = _worktree(repo, "clean")
    got = td.inspect_worktree(path)
    assert got["exists"] is True
    assert got["dirty_files"] == 0
    assert got["unpushed"] == 0
    assert td.holds_open(got) == []


def test_untracked_and_modified_files_are_counted(repo):
    path = _worktree(repo, "dirty")
    open(os.path.join(path, "new.txt"), "w").write("x")
    open(os.path.join(path, "a.txt"), "w").write("changed")
    got = td.inspect_worktree(path)
    assert got["dirty_files"] == 2
    assert td.holds_open(got) == ["dirty"]


def test_a_commit_past_the_upstream_is_unpushed(repo):
    path = _worktree(repo, "ahead", push=True)
    open(os.path.join(path, "b.txt"), "w").write("b")
    _run(path, "add", ".")
    _run(path, "commit", "-m", "two")
    got = td.inspect_worktree(path)
    assert got["unpushed"] == 1
    assert td.holds_open(got) == ["unpushed"]


def test_without_an_upstream_the_remotes_are_the_yardstick(repo):
    path = _worktree(repo, "noup")
    open(os.path.join(path, "b.txt"), "w").write("b")
    _run(path, "add", ".")
    _run(path, "commit", "-m", "two")
    # No upstream is set: `@{upstream}..HEAD` errors, `HEAD --not --remotes`
    # counts the commit no remote branch has.
    assert td.inspect_worktree(path)["unpushed"] == 1


def test_git_that_cannot_answer_is_unknown_and_holds_open(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    got = td.inspect_worktree(str(not_a_repo))
    assert got["exists"] is True
    assert got["unpushed"] is None
    assert "unknown" in td.holds_open(got)


def test_a_folder_that_is_gone_has_nothing_to_hold(tmp_path):
    got = td.inspect_worktree(str(tmp_path / "gone"))
    assert got["exists"] is False
    assert td.holds_open(got) == []


def test_teardown_removes_clean_and_keeps_dirty(repo):
    clean = _worktree(repo, "clean")
    dirty = _worktree(repo, "dirty")
    open(os.path.join(dirty, "x"), "w").write("x")
    out = td.teardown_worktrees([clean, dirty])
    assert out["removed"] == [clean]
    assert not os.path.exists(clean)
    assert os.path.isdir(dirty)
    (kept,) = out["kept"]
    assert kept["path"] == dirty
    assert kept["reasons"] == ["dirty"]
    assert kept["dirty_files"] == 1


def test_teardown_names_every_reason_a_worktree_is_kept(repo):
    path = _worktree(repo, "both", push=True)
    open(os.path.join(path, "b.txt"), "w").write("b")
    _run(path, "add", ".")
    _run(path, "commit", "-m", "two")
    open(os.path.join(path, "c.txt"), "w").write("c")
    (kept,) = td.teardown_worktrees([path])["kept"]
    assert kept["reasons"] == ["dirty", "unpushed"]
    assert kept["unpushed"] == 1


def test_force_removes_a_kept_worktree(repo):
    path = _worktree(repo, "dirty")
    open(os.path.join(path, "x"), "w").write("x")
    out = td.teardown_worktrees([path], force=True)
    assert out["removed"] == [path]
    assert out["kept"] == []
    assert not os.path.exists(path)


def test_a_worktree_outside_the_managed_dir_is_never_removed(repo, tmp_path):
    outside = tmp_path / "by-hand"
    _run(repo, "worktree", "add", "-b", "hand", str(outside))
    out = td.teardown_worktrees([str(outside)], force=True)
    assert out["removed"] == []
    assert out["kept"][0]["reasons"] == ["not-managed"]
    assert outside.is_dir()


def test_a_missing_worktree_is_reported_gone_not_kept(tmp_path):
    out = td.teardown_worktrees([str(tmp_path / "gone")])
    assert out == {"removed": [], "kept": [], "gone": [str(tmp_path / "gone")]}


# ----------------------------------------------------------------- processes


def _spawn(code):
    return subprocess.Popen([sys.executable, "-c", code], close_fds=False,
                            stdout=subprocess.PIPE, start_new_session=True)


def test_kill_pids_ends_a_polite_process_with_sigterm():
    p = _spawn("import time; time.sleep(60)")
    time.sleep(0.2)
    killed = td.kill_pids([p.pid], grace_s=3.0)
    assert p.wait(timeout=5) == -signal.SIGTERM
    assert killed == [p.pid]


def test_kill_pids_escalates_to_sigkill_when_term_is_ignored():
    p = _spawn("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
               "print('up', flush=True); time.sleep(60)")
    time.sleep(0.5)
    killed = td.kill_pids([p.pid], grace_s=0.3)
    assert p.wait(timeout=5) == -signal.SIGKILL
    assert killed == [p.pid]


def test_kill_pids_ignores_a_pid_that_is_already_gone():
    p = _spawn("pass")
    p.wait()
    assert td.kill_pids([p.pid], grace_s=0.1) == []


class _Agent:
    """Just enough agent for `stop_session`."""

    def __init__(self, runs_dir):
        self.RUNS = str(runs_dir)
        self.cancelled = []

    def _alive(self, run_dir):
        try:
            pid = int(open(os.path.join(run_dir, "pid")).read())
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    def _cancel(self, run_id, interrupt_first=True, queued=False):
        self.cancelled.append((run_id, interrupt_first))
        pid = int(open(os.path.join(self.RUNS, run_id, "pid")).read())
        os.kill(pid, signal.SIGTERM)
        return {"cancelled": run_id}


def test_stop_session_cancels_the_live_run_and_kills_a_stray_child(tmp_path):
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    # The "CLI": spawns a child that escapes its group (a dev server).
    child_code = "import time; time.sleep(60)"
    parent = _spawn(
        "import subprocess, sys, time; "
        f"c = subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session=True); "
        "print(c.pid, flush=True); time.sleep(60)")
    # Parent could be mid-startup; read the child's pid it prints.
    child_pid = int(parent.stdout.readline()) if parent.stdout else None
    (runs / "r1" / "pid").write_text(str(parent.pid))
    agent = _Agent(runs)
    scanned = [{"run_id": "r1", "run_dir": str(runs / "r1"), "sessions": ["s1"]}]
    out = td.stop_session("s1", agent, scanned, grace_s=2.0)
    assert agent.cancelled == [("r1", False)]
    assert out["stopped"] == 1
    parent.wait(timeout=5)
    assert child_pid
    # SIGKILL lands asynchronously: give the kernel a moment to reap it.
    deadline = time.monotonic() + 5
    while td._alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not td._alive(child_pid)


def test_stop_session_leaves_other_sessions_runs_alone(tmp_path):
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    p = _spawn("import time; time.sleep(60)")
    (runs / "r1" / "pid").write_text(str(p.pid))
    agent = _Agent(runs)
    scanned = [{"run_id": "r1", "run_dir": str(runs / "r1"), "sessions": ["other"]}]
    out = td.stop_session("s1", agent, scanned, grace_s=0.1)
    assert out["stopped"] == 0
    assert agent.cancelled == []
    assert p.poll() is None
    p.kill()
    p.wait()


# ------------------------------------------------------------- holds-open data


def test_holds_for_reports_each_existing_worktree_with_its_reasons(repo):
    td.reset_holds_cache()
    clean = _worktree(repo, "clean")
    dirty = _worktree(repo, "dirty")
    open(os.path.join(dirty, "x"), "w").write("x")
    got = td.holds_for([clean, dirty, str(repo / "gone")])
    by_path = {w["path"]: w for w in got}
    assert set(by_path) == {clean, dirty}, "a folder that is gone holds nothing"
    assert by_path[clean]["reasons"] == []
    assert by_path[dirty]["reasons"] == ["dirty"]
    assert by_path[dirty]["dirty_files"] == 1


def test_holds_for_is_cached_for_the_ttl(repo, monkeypatch):
    td.reset_holds_cache()
    path = _worktree(repo, "c")
    calls = []
    real = td.inspect_worktree
    monkeypatch.setattr(td, "inspect_worktree",
                        lambda p: calls.append(p) or real(p))
    clock = [500.0]
    monkeypatch.setattr(td.time, "monotonic", lambda: clock[0])
    td.holds_for([path])
    td.holds_for([path])
    assert len(calls) == 1
    clock[0] += td.HOLDS_TTL_S + 1
    td.holds_for([path])
    assert len(calls) == 2


def test_a_raising_remove_is_kept_and_the_rest_still_processed(repo, monkeypatch):
    paths = [_worktree(repo, n, push=True) for n in ("a", "b", "c")]
    real = td.wt.remove_worktree

    def flaky(path, name, force=False):
        if path == paths[0]:
            raise td.wt._Refusal("git said no")
        return real(path, name, force=force)

    monkeypatch.setattr(td.wt, "remove_worktree", flaky)
    got = td.teardown_worktrees(paths)
    assert sorted(got["removed"]) == sorted(paths[1:])
    assert [k["path"] for k in got["kept"]] == [paths[0]]
    assert "remove-failed" in got["kept"][0]["reasons"]
    assert "git said no" in got["kept"][0]["error"]

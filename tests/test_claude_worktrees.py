"""`claude_agent/worktrees.py` and `POST /api/claude/worktrees` (SPEC W-2/W-3/W-5).

Real temp git repos, not a faked `git`: the module is a thin reader of git's
own porcelain, and the cases that matter (a linked worktree's `.git` FILE, the
common dir, `worktree remove` refusing a dirty tree) are git's behaviour.
"""
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from fused_render.claude_agent import worktrees as wt
from fused_render.server import create_app

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git is not installed")

FUSED = {"X-Fused": "1"}


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "one")
    return str(os.path.realpath(root))


def _exclude(root):
    with open(os.path.join(root, ".git", "info", "exclude")) as f:
        return f.read()


def test_list_on_main(repo):
    out = wt.list_worktrees(os.path.join(repo, "a.txt"))
    assert out["root"] == repo
    assert out["current"] == repo
    assert out["branch_current"] == "main"
    [row] = out["worktrees"]
    assert row == {"path": repo, "branch": "main", "head": row["head"],
                   "name": "repo", "managed": False, "is_main": True,
                   "dirty": False}
    assert len(row["head"]) >= 40


def test_repo_root_from_inside_a_worktree(repo):
    made = wt.create_worktree(repo, "feat")
    assert wt.repo_root(made["path"]) == repo
    assert wt.repo_root(os.path.join(made["path"], "a.txt")) == repo


def test_create_then_list_and_exclude_line_once(repo):
    made = wt.create_worktree(repo, "feat")
    target = os.path.join(repo, ".claude", "worktrees", "feat")
    assert made == {"path": target, "branch": "feat"}
    assert os.path.isfile(os.path.join(target, ".git"))  # a linked worktree
    assert _exclude(repo).splitlines().count(".claude/worktrees/") == 1

    wt.create_worktree(repo, "other")
    assert _exclude(repo).splitlines().count(".claude/worktrees/") == 1
    # The exclude works: main does not see its worktrees as untracked.
    assert _git(repo, "status", "--porcelain") == ""

    out = wt.list_worktrees(target)
    assert out["current"] == target
    assert out["branch_current"] == "feat"
    rows = {r["name"]: r for r in out["worktrees"]}
    assert rows["feat"]["managed"] is True and rows["feat"]["is_main"] is False
    assert rows["repo"]["managed"] is False and rows["repo"]["is_main"] is True
    assert rows["feat"]["dirty"] is False


def test_create_same_name_twice_is_an_error(repo):
    assert "error" not in wt.create_worktree(repo, "feat")
    again = wt.create_worktree(repo, "feat")
    assert set(again) == {"error"}
    assert "already exists" in again["error"]


def test_branch_collision_gets_a_suffix(repo):
    _git(repo, "branch", "feat")
    assert wt.create_worktree(repo, "feat")["branch"] == "feat-2"
    _git(repo, "branch", "x")
    _git(repo, "branch", "x-2")
    assert wt.create_worktree(repo, "x")["branch"] == "x-3"


def test_create_from_base_and_bad_names(repo):
    first = _git(repo, "rev-parse", "HEAD").strip()
    with open(os.path.join(repo, "b.txt"), "w") as f:
        f.write("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "two")
    made = wt.create_worktree(repo, "old", base=first)
    assert _git(made["path"], "rev-parse", "HEAD").strip() == first
    for bad in ("", ".", "..", "a/b", "x" * 65, "sp ace"):
        assert set(wt.create_worktree(repo, bad)) == {"error"}, bad
    assert "error" in wt.create_worktree(repo, "y", base="no-such-ref")
    assert "error" in wt.create_worktree(repo, "z", base="--orphan")


def test_remove_managed_clean(repo):
    made = wt.create_worktree(repo, "feat")
    assert wt.remove_worktree(repo, "feat") == {"removed": made["path"]}
    assert not os.path.exists(made["path"])
    # The branch is left alone.
    assert "feat" in _git(repo, "branch", "--list", "feat")


def test_remove_dirty_refused_then_forced(repo):
    made = wt.create_worktree(repo, "feat")
    with open(os.path.join(made["path"], "new.txt"), "w") as f:
        f.write("x\n")
    assert wt.list_worktrees(made["path"])["worktrees"][1]["dirty"] is True
    refused = wt.remove_worktree(repo, "feat")
    assert set(refused) == {"error"} and "uncommitted" in refused["error"]
    assert os.path.isdir(made["path"])
    assert wt.remove_worktree(repo, "feat", force=True) == {"removed": made["path"]}
    assert not os.path.exists(made["path"])


def test_remove_unmanaged_refused(repo, tmp_path):
    outside = str(tmp_path / "hand-made")
    _git(repo, "worktree", "add", "-q", "-b", "hand", outside)
    for name in (outside, repo):
        out = wt.remove_worktree(repo, name)
        assert set(out) == {"error"} and ".claude/worktrees" in out["error"]
    assert os.path.isdir(outside)
    # By name: there is no managed worktree called that.
    assert set(wt.remove_worktree(repo, "hand-made")) == {"error"}


def test_non_repo_path_is_an_error(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert wt.repo_root(str(plain)) == ""
    for out in (wt.list_worktrees(str(plain)),
                wt.create_worktree(str(plain), "x"),
                wt.remove_worktree(str(plain), "x")):
        assert set(out) == {"error"}
    assert set(wt.list_worktrees("")) == {"error"}


# --------------------------------------------------------------------- route

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_route_403_without_header(client, repo):
    r = client.post("/api/claude/worktrees", json={"action": "list", "file": repo})
    assert r.status_code == 403
    assert "error" in r.json()


def test_route_unknown_action_400(client, repo):
    r = client.post("/api/claude/worktrees", headers=FUSED,
                    json={"action": "prune", "file": repo})
    assert r.status_code == 400
    assert isinstance(r.json()["error"], str)


def test_route_round_trip(client, repo):
    r = client.post("/api/claude/worktrees", headers=FUSED,
                    json={"action": "create", "file": repo, "name": "feat"})
    assert r.status_code == 200
    made = r.json()
    assert made["branch"] == "feat"

    r = client.post("/api/claude/worktrees", headers=FUSED,
                    json={"action": "list", "file": made["path"]})
    assert r.status_code == 200
    assert r.json()["current"] == made["path"]

    with open(os.path.join(made["path"], "n.txt"), "w") as f:
        f.write("n\n")
    r = client.post("/api/claude/worktrees", headers=FUSED,
                    json={"action": "remove", "file": repo, "name": "feat"})
    assert r.status_code == 200 and "error" in r.json()
    r = client.post("/api/claude/worktrees", headers=FUSED,
                    json={"action": "remove", "file": repo, "name": "feat",
                          "force": True})
    assert r.json() == {"removed": made["path"]}


def test_route_error_is_a_200_answer(client, tmp_path):
    r = client.post("/api/claude/worktrees", headers=FUSED,
                    json={"action": "list", "file": str(tmp_path)})
    assert r.status_code == 200
    assert set(r.json()) == {"error"}

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
                   "dirty": False,
                   "mapped": {"path": os.path.join(repo, "a.txt"), "exists": True}}
    assert out["relative"] == "a.txt"
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


# ------------------------------------------------------------- review fixes

def _hook(repo, body):
    path = os.path.join(repo, ".git", "hooks", "post-checkout")
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


def _assert_nothing_left(repo, name):
    out = wt.list_worktrees(repo)
    assert [r["path"] for r in out["worktrees"]] == [repo]
    assert not os.path.exists(os.path.join(repo, ".claude", "worktrees", name))
    assert _git(repo, "branch", "--list", name) == ""


# A post-checkout hook runs inside `git worktree add`, after the worktree is
# registered: it stands in for an add that dies mid-checkout, and it writes the
# "initializing" lock git leaves when killed there.
_LOCK = 'echo initializing > "$(git rev-parse --git-dir)/locked"\n'


def test_failed_add_is_undone_including_its_lock(repo):
    hook = _hook(repo, _LOCK + "exit 1\n")
    out = wt.create_worktree(repo, "feat")
    assert set(out) == {"error"}
    _assert_nothing_left(repo, "feat")
    os.remove(hook)
    assert wt.create_worktree(repo, "feat")["branch"] == "feat"


def test_timed_out_add_is_undone(repo, monkeypatch):
    hook = _hook(repo, _LOCK + "sleep 3\n")
    monkeypatch.setattr(wt, "BUDGET_S", 1.5)
    out = wt.create_worktree(repo, "feat")
    assert set(out) == {"error"} and "undone" in out["error"]
    _assert_nothing_left(repo, "feat")
    os.remove(hook)
    monkeypatch.setattr(wt, "BUDGET_S", 16.0)
    assert wt.create_worktree(repo, "feat")["branch"] == "feat"


def test_force_remove_clears_a_locked_worktree(repo):
    made = wt.create_worktree(repo, "feat")
    _git(repo, "worktree", "lock", made["path"])
    assert wt.remove_worktree(repo, "feat", force=True) == {"removed": made["path"]}
    assert not os.path.exists(made["path"])


@pytest.mark.parametrize("link", [".claude", os.path.join(".claude", "worktrees")])
def test_symlinked_managed_dir_refused(repo, tmp_path, link):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.makedirs(os.path.dirname(os.path.join(repo, link)), exist_ok=True)
    os.symlink(elsewhere, os.path.join(repo, link))
    out = wt.create_worktree(repo, "feat")
    assert set(out) == {"error"} and "symlink" in out["error"]
    assert list(elsewhere.iterdir()) == []
    assert set(wt.remove_worktree(repo, "feat")) == {"error"}


def test_separate_git_dir_and_submodule_are_repos(tmp_path):
    gd, co = str(tmp_path / "gd.git"), str(tmp_path / "co")
    subprocess.run(["git", "init", "-q", "-b", "main", f"--separate-git-dir={gd}",
                    co], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T"),
                 ("commit.gpgsign", "false")):
        _git(co, "config", k, v)
    _git(co, "commit", "-q", "--allow-empty", "-m", "one")
    co = os.path.realpath(co)
    assert wt.repo_root(co) == co
    made = wt.create_worktree(co, "feat")
    assert made["path"] == os.path.join(co, ".claude", "worktrees", "feat")
    out = wt.list_worktrees(co)
    assert out["root"] == co and out["worktrees"][0]["path"] == co
    assert out["worktrees"][0]["is_main"] is True
    # From inside the linked worktree: core.worktree is unset and the porcelain
    # names the git dir, so the main checkout cannot be found — "" not a guess.
    assert wt.repo_root(made["path"]) == ""

    outer = str(tmp_path / "outer")
    subprocess.run(["git", "init", "-q", outer], check=True)
    subprocess.run(["git", "-C", outer, "-c", "protocol.file.allow=always",
                    "submodule", "add", "-q", co, "sm"], check=True,
                   capture_output=True)
    sm = os.path.realpath(os.path.join(outer, "sm"))
    assert wt.repo_root(sm) == sm
    sub_made = wt.create_worktree(sm, "feat")
    assert "error" not in sub_made, sub_made
    assert wt.repo_root(sub_made["path"]) == sm


def test_bare_repo_refused(tmp_path):
    bare = str(tmp_path / "b.git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    assert wt.repo_root(bare) == ""
    assert set(wt.list_worktrees(bare)) == {"error"}


def test_one_deadline_and_one_common_dir_read(repo, monkeypatch):
    seen = []
    real = wt._git

    def counting(root, *args, **kw):
        seen.append((args, kw.get("deadline")))
        return real(root, *args, **kw)

    monkeypatch.setattr(wt, "_git", counting)
    assert "error" not in wt.create_worktree(repo, "feat")
    assert sum("--git-common-dir" in a for a, _ in seen) == 1
    # Every command of the create shares one deadline.
    assert len({d for _, d in seen}) == 1 and seen[0][1] is not None

    monkeypatch.setattr(wt, "BUDGET_S", 0.0)
    out = wt.create_worktree(repo, "late")
    assert set(out) == {"error"} and "time" in out["error"]
    assert not os.path.exists(os.path.join(repo, ".claude", "worktrees", "late"))


def test_tag_shadowing_a_branch_still_collides(repo):
    _git(repo, "branch", "feat")
    _git(repo, "tag", "feat")
    assert wt.create_worktree(repo, "feat")["branch"] == "feat-2"


def test_reserved_device_names_refused(repo):
    for bad in ("CON", "nul", "Aux", "prn", "com1", "LPT9", "com1.txt"):
        out = wt.create_worktree(repo, bad)
        assert set(out) == {"error"} and "reserved" in out["error"], bad
    assert "error" not in wt.create_worktree(repo, "console")


def test_branch_collision_case_folded_on_case_insensitive_platforms(repo, monkeypatch):
    _git(repo, "branch", "Feat")
    monkeypatch.setattr(wt.sys, "platform", "darwin")
    assert wt.create_worktree(repo, "feat")["branch"] == "feat-2"


def test_route_runs_on_its_own_executor(client, repo, monkeypatch):
    import threading

    from fused_render.server.routers import claude_agent as router

    def main(action: str = "list", file: str = "", name: str = "",
             base: str = "", force: bool = False) -> dict:
        return {"thread": threading.current_thread().name}

    monkeypatch.setattr(wt, "main", main)
    r = client.post("/api/claude/worktrees", headers=FUSED,
                    json={"action": "list", "file": repo})
    assert r.status_code == 200
    assert r.json()["thread"].startswith("claude-worktrees")
    assert router._WORKTREE_POOL._max_workers == 2


def test_git_child_env_drops_inherited_repository_location(monkeypatch):
    """A server launched from a hook (or a shell with GIT_DIR exported) must
    not have its worktree calls redirected to that repository (CodeRabbit,
    PR #1635)."""
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/elsewhere")
    monkeypatch.setenv("GIT_INDEX_FILE", "/elsewhere/.git/index")
    monkeypatch.setenv("UNRELATED", "kept")
    env = wt._child_env()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        assert name not in env
    assert env["UNRELATED"] == "kept"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_list_maps_a_file_target_into_every_worktree(repo):
    """A chat open on a FILE switches to the same file in another worktree
    when it exists there, else to that worktree's root (the user: "it should
    behave the same as the folder level does")."""
    os.mkdir(os.path.join(repo, "src"))
    with open(os.path.join(repo, "src", "app.py"), "w") as f:
        f.write("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add app")
    made = wt.create_worktree(repo, "feat")
    assert "error" not in made
    out = wt.list_worktrees(os.path.join(repo, "src", "app.py"))
    assert out["relative"] == os.path.join("src", "app.py")
    by_name = {w["name"]: w for w in out["worktrees"]}
    main = by_name["repo"]
    assert main["mapped"] == {"path": os.path.join(repo, "src", "app.py"),
                              "exists": True}
    feat = by_name["feat"]
    assert feat["mapped"] == {"path": os.path.join(made["path"], "src", "app.py"),
                              "exists": True}
    # A file that only the main checkout has: the other worktree maps to it
    # too, but says it is not there.
    with open(os.path.join(repo, "src", "new.py"), "w") as f:
        f.write("y = 2\n")
    out = wt.list_worktrees(os.path.join(repo, "src", "new.py"))
    feat = next(w for w in out["worktrees"] if w["name"] == "feat")
    assert feat["mapped"]["exists"] is False
    # A folder target keeps the old shape: relative "" and the roots.
    out = wt.list_worktrees(repo)
    assert out["relative"] == ""
    assert all(w["mapped"]["path"] == w["path"] for w in out["worktrees"])


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_folder_inside_the_checkout_maps_as_written(repo):
    """`src` as a symlink to a sibling folder: the chat's `src/app.py` is
    carried over lexically, not as its resolved path (Bugbot, PR #1635)."""
    os.mkdir(os.path.join(repo, "real"))
    with open(os.path.join(repo, "real", "app.py"), "w") as f:
        f.write("x = 1\n")
    os.symlink("real", os.path.join(repo, "src"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "link")
    made = wt.create_worktree(repo, "feat")
    assert "error" not in made
    out = wt.list_worktrees(os.path.join(repo, "src", "app.py"))
    assert out["relative"] == os.path.join("src", "app.py")
    feat = next(w for w in out["worktrees"] if w["name"] == "feat")
    assert feat["mapped"] == {"path": os.path.join(made["path"], "src", "app.py"),
                              "exists": True}

"""The `versions` template (app git history): the condition gate offers it
only inside git-backed app folders, and its Python backend can list the log,
materialise any commit as a rendered snapshot, and revert — always by adding
a commit on top, never by rewriting history.

Real git in tmp workspaces (FUSED_RENDER_DIR / FUSED_RENDER_WORKSPACE_DIR),
same fixtures shape as tests/test_app_git.py. The template modules are loaded
from the package source via importlib — they are exec'd standalone in
production too (conditions by server._run_condition, the backend by /api/run),
so nothing here goes through a package import.
"""
import importlib.util
import os
import subprocess

import pytest

from fused_render import app_git

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "versions")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("test_versions_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE_DIR", str(fdir))
    # Snapshots land under the shell home; keep them in the tmp tree.
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(tmp_path / "home"))
    return fdir


def _git(d, *args):
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t",
                           "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True)


def _make_app(workspace, tag="local", name="demo"):
    d = workspace / tag / name
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html>v1</html>")
    assert app_git.init_repo(str(d))
    return d


def _log_subjects(d):
    return _git(d, "log", "--format=%s").stdout.strip().splitlines()


def _shas(d):
    return _git(d, "log", "--format=%H").stdout.strip().splitlines()


# ------------------------------------------------------------------- gate

def test_condition_true_only_inside_git_backed_apps(workspace, tmp_path):
    cond = _load("condition")
    d = _make_app(workspace)
    assert cond.main(str(d)) is True                     # the app dir itself
    assert cond.main(str(d / "index.html")) is True      # a file inside
    assert cond.main(str(d / "sub" / "x.py")) is True    # nested path
    assert cond.main(str(workspace)) is False            # workspace root
    assert cond.main(str(workspace / "local")) is False  # tag level
    assert cond.main(str(tmp_path / "elsewhere")) is False
    # App-shaped folder without a repo: no history to show.
    plain = workspace / "local" / "plain"
    plain.mkdir(parents=True)
    assert cond.main(str(plain)) is False


# -------------------------------------------------------------------- log

def test_log_lists_commits_newest_first(workspace):
    v = _load("versions")
    d = _make_app(workspace)
    (d / "index.html").write_text("<html>v2</html>")
    app_git.commit(str(d / "index.html"), "Edit index.html")
    res = v.main(action="log", file=str(d / "index.html"))
    subjects = [c["subject"] for c in res["commits"]]
    assert subjects == ["Edit index.html", "New app from starter"]
    assert all(c["ts"] > 0 for c in res["commits"])
    # Same answer for the directory target — the repo is the app.
    assert v.main(action="log", file=str(d))["commits"][0]["sha"] == \
        res["commits"][0]["sha"]


def test_actions_refuse_paths_outside_apps(workspace, tmp_path):
    v = _load("versions")
    repo = tmp_path / "userrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    for action in ("log", "snapshot", "revert"):
        assert "error" in v.main(action=action, file=str(repo / "f.txt"),
                                 sha="deadbeef")


# --------------------------------------------------------------- snapshot

def test_snapshot_materialises_the_selected_commit(workspace):
    v = _load("versions")
    d = _make_app(workspace)
    first = _shas(d)[0]
    (d / "index.html").write_text("<html>v2</html>")
    (d / "extra.py").write_text("x = 1")
    app_git.commit(str(d), "Add extra")
    res = v.main(action="snapshot", file=str(d / "index.html"), sha=first)
    assert res["entry"].endswith("index.html")
    with open(res["entry"], encoding="utf-8") as f:
        assert f.read() == "<html>v1</html>"
    assert not os.path.exists(os.path.join(res["dir"], "extra.py"))
    # Idempotent: a commit is immutable, the snapshot is reused.
    again = v.main(action="snapshot", file=str(d), sha=first[:7])
    assert again["dir"] == res["dir"]
    # Garbage sha never reaches git.
    assert "error" in v.main(action="snapshot", file=str(d), sha="; rm -rf /")
    assert "error" in v.main(action="snapshot", file=str(d), sha="a" * 40)


# ----------------------------------------------------------------- revert

def test_revert_adds_a_commit_and_restores_the_tree(workspace):
    v = _load("versions")
    d = _make_app(workspace)
    first = _shas(d)[0]
    (d / "index.html").write_text("<html>v2</html>")
    (d / "extra.py").write_text("x = 1")
    app_git.commit(str(d), "Add extra")
    res = v.main(action="revert", file=str(d / "index.html"), sha=first)
    assert res.get("reverted") is True
    # Tree restored: edited file back, later file gone — via a NEW commit.
    assert (d / "index.html").read_text() == "<html>v1</html>"
    assert not (d / "extra.py").exists()
    subjects = _log_subjects(d)
    assert len(subjects) == 3 and subjects[0].startswith("Reverted to ")
    assert "New app from starter" in subjects[0]
    # History intact — the reverted-away commit is still reachable.
    assert len(_shas(d)) == 3
    # Reverting to HEAD is a no-op, not an empty commit.
    assert v.main(action="revert", file=str(d), sha=_shas(d)[0])["noop"] is True
    assert len(_shas(d)) == 3


def test_same_tree_revert_preserves_uncommitted_edits(workspace):
    # A commit whose tree matches HEAD but whose sha differs (revert-of-a-
    # revert lands on a DIFFERENT commit with the SAME content as an earlier
    # one) must be reported a no-op WITHOUT ever running the destructive
    # working-tree reset — otherwise dirty, uncommitted edits would be
    # silently discarded to reach a tree that was already there.
    v = _load("versions")
    d = _make_app(workspace)
    original = _shas(d)[0]  # v1
    (d / "index.html").write_text("<html>v2</html>")
    app_git.commit(str(d), "Edit index.html")
    # Revert to v1: lands on a NEW commit (c3) whose tree equals `original`'s.
    res = v.main(action="revert", file=str(d), sha=original)
    assert res.get("reverted") is True
    reverted_sha = _shas(d)[0]
    assert reverted_sha != original
    assert (d / "index.html").read_text() == "<html>v1</html>"
    # Dirty the working copy without committing.
    (d / "index.html").write_text("<html>UNCOMMITTED</html>")
    # Ask to revert to `original` again: different sha than HEAD (reverted_sha)
    # but an IDENTICAL tree — must noop without touching the dirty file.
    res = v.main(action="revert", file=str(d), sha=original)
    assert res.get("noop") is True
    assert (d / "index.html").read_text() == "<html>UNCOMMITTED</html>"
    assert _shas(d)[0] == reverted_sha  # no new commit, HEAD unmoved

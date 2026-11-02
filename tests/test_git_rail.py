"""GET /api/git/rail — the Claude rail's Git section, against a real repo
(tests/test_git_commits.py's fixture style)."""
import os
import subprocess

import pytest

from fused_render.server.routers import git_rail as gr
from fused_render.server.routers import git_snapshot as gs


def _git(*args, cwd, check=True):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, env=env,
                          capture_output=True)


def _commit(cwd, message):
    _git("add", "-A", cwd=cwd)
    _git("-c", "user.name=T", "-c", "user.email=t@e", "commit", "-qm", message,
         cwd=cwd)
    return _git("rev-parse", "HEAD", cwd=cwd).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    app_dir = root / "myapp"
    app_dir.mkdir(parents=True)
    (app_dir / "index.html").write_text("<html>v1</html>", encoding="utf-8")
    (root / "other.txt").write_text("x\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", cwd=root)
    shas = [_commit(root, "v1")]
    (app_dir / "index.html").write_text("<html>v2</html>", encoding="utf-8")
    shas.append(_commit(root, "v2"))
    (root / "other.txt").write_text("y\n", encoding="utf-8")
    shas.append(_commit(root, "elsewhere"))
    (app_dir / "index.html").write_text("<html>v3</html>", encoding="utf-8")
    shas.append(_commit(root, "v3"))
    (app_dir / "index.html").write_text("<html>v4</html>", encoding="utf-8")
    shas.append(_commit(root, "v4"))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return {"root": root, "app_dir": app_dir, "shas": shas}


def test_branch_and_last_three_commits_scoped_to_the_folder(repo):
    out = gr.rail_status(str(repo["app_dir"]))
    assert out["ok"] is True
    r = out["repo"]
    assert r["root"] == os.path.realpath(str(repo["root"]))
    assert r["branch"] == "main"
    # Three newest commits that touched myapp/ — "elsewhere" is not one.
    assert [c["subject"] for c in r["commits"]] == ["v4", "v3", "v2"]
    assert r["commits"][0]["sha"] == repo["shas"][-1]
    assert set(r["commits"][0]) >= {"sha", "short", "subject", "author", "when"}
    # No remote: nothing to count against.
    assert r["has_remote"] is False
    assert r["default_branch"] is None
    assert r["ahead"] is None and r["behind"] is None


def test_a_file_scopes_to_that_file_and_the_whole_root_sees_everything(repo):
    by_file = gr.rail_status(str(repo["app_dir"] / "index.html"))["repo"]
    assert [c["subject"] for c in by_file["commits"]] == ["v4", "v3", "v2"]
    whole = gr.rail_status(str(repo["root"]))["repo"]
    assert [c["subject"] for c in whole["commits"]] == ["v4", "v3", "elsewhere"]


def test_outside_a_repo_is_no_repo_not_an_error(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gr.rail_status(str(plain)) == {"ok": True, "repo": None}


def test_relative_path_is_400():
    with pytest.raises(gs._Refused) as exc:
        gr.rail_status("myapp")
    assert exc.value.status == 400


def test_check_without_a_remote_reports_unchecked_but_still_answers(repo):
    out = gr.rail_check(str(repo["app_dir"]))
    assert out["checked"] is False
    assert out["repo"]["branch"] == "main"
    assert len(out["repo"]["commits"]) == 3


def test_a_remote_with_no_recorded_default_branch_is_still_a_remote(repo, tmp_path):
    """`git init` + `remote add` + push leaves no refs/remotes/origin/HEAD:
    the GET says has_remote with no default branch (no network re-ask); the
    POST, which may ask, resolves it off the bare remote on disk."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=bare)
    _git("remote", "add", "origin", str(bare), cwd=repo["root"])
    _git("push", "-q", "origin", "main", cwd=repo["root"])
    r = gr.rail_status(str(repo["app_dir"]))["repo"]
    assert r["has_remote"] is True
    assert r["default_branch"] is None
    out = gr.rail_check(str(repo["app_dir"]))
    assert out["repo"]["default_branch"] == "main"
    assert out["repo"]["ahead"] == 0 and out["repo"]["behind"] == 0


def test_detached_head_has_no_branch_but_still_its_commits(repo):
    _git("checkout", "-q", "--detach", cwd=repo["root"])
    r = gr.rail_status(str(repo["app_dir"]))["repo"]
    assert r["branch"] is None
    assert [c["subject"] for c in r["commits"]] == ["v4", "v3", "v2"]

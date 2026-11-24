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


def test_limit_asks_for_more_commits_and_is_clamped(repo):
    # Default: the flag-off rail's three.
    assert len(gr.rail_status(str(repo["root"]))["repo"]["commits"]) == 3
    # The Git door's card asks for a window's worth (owner, 2026-11-10).
    whole = gr.rail_status(str(repo["root"]), limit=50)["repo"]
    assert [c["subject"] for c in whole["commits"]] == ["v4", "v3", "elsewhere", "v2", "v1"]
    assert len(gr.rail_status(str(repo["root"]), limit=0)["repo"]["commits"]) == 1
    assert gr._limit(10_000) == gr.COMMITS_MAX
    assert gr._limit(None) == gr.COMMITS
    out = gr.rail_check(str(repo["app_dir"]), limit=50)
    assert [c["subject"] for c in out["repo"]["commits"]] == ["v4", "v3", "v2", "v1"]


def test_upstream_counts_what_a_push_or_pull_would_move(repo, tmp_path):
    # No remote, no upstream: null.
    assert gr.rail_status(str(repo["root"]))["repo"]["upstream"] is None
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=bare)
    _git("remote", "add", "origin", str(bare), cwd=repo["root"])
    _git("push", "-q", "origin", "main", cwd=repo["root"])
    # Pushed but no tracking set: still no upstream.
    assert gr.rail_status(str(repo["root"]))["repo"]["upstream"] is None
    _git("branch", "-q", "--set-upstream-to=origin/main", cwd=repo["root"])
    assert gr.rail_status(str(repo["root"]))["repo"]["upstream"] == {"ahead": 0, "behind": 0}
    (repo["root"] / "other.txt").write_text("z\n", encoding="utf-8")
    _commit(repo["root"], "local")
    assert gr.rail_status(str(repo["root"]))["repo"]["upstream"] == {"ahead": 1, "behind": 0}
    _git("reset", "-q", "--hard", "HEAD~2", cwd=repo["root"])
    assert gr.rail_status(str(repo["root"]))["repo"]["upstream"] == {"ahead": 0, "behind": 1}
    # Detached: no upstream.
    _git("checkout", "-q", "--detach", cwd=repo["root"])
    assert gr.rail_status(str(repo["root"]))["repo"]["upstream"] is None


def _tracked_clone(repo, tmp_path):
    """`repo` pushed to a bare origin it tracks, plus a second clone that
    commits to that origin (what another machine would do)."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=bare)
    _git("remote", "add", "origin", str(bare), cwd=repo["root"])
    _git("push", "-q", "-u", "origin", "main", cwd=repo["root"])
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True,
                   capture_output=True)
    return bare, other


def test_sync_push_sends_the_branch_and_answers_the_fresh_counts(repo, tmp_path):
    bare, _ = _tracked_clone(repo, tmp_path)
    (repo["app_dir"] / "index.html").write_text("<html>v5</html>", encoding="utf-8")
    _commit(repo["root"], "v5")
    assert gr.rail_status(str(repo["app_dir"]))["repo"]["upstream"] == {"ahead": 1, "behind": 0}
    out = gr.rail_sync(str(repo["app_dir"]), "push")
    assert out["result"]["ok"] is True and out["result"]["op"] == "push"
    assert out["repo"]["upstream"] == {"ahead": 0, "behind": 0}
    assert _git("rev-parse", "main", cwd=bare).stdout == \
        _git("rev-parse", "HEAD", cwd=repo["root"]).stdout


def test_sync_pull_fast_forwards_onto_the_remote(repo, tmp_path):
    _, other = _tracked_clone(repo, tmp_path)
    (other / "other.txt").write_text("from elsewhere\n", encoding="utf-8")
    _commit(other, "remote work")
    _git("push", "-q", "origin", "main", cwd=other)
    _git("fetch", "-q", "origin", cwd=repo["root"])
    out = gr.rail_sync(str(repo["app_dir"]), "pull")
    assert out["result"]["ok"] is True and out["result"]["op"] == "pull"
    assert out["repo"]["upstream"] == {"ahead": 0, "behind": 0}
    assert (repo["root"] / "other.txt").read_text(encoding="utf-8") == "from elsewhere\n"


def test_sync_refusal_is_a_sentence_not_an_error(repo):
    # No remote: ops.py refuses in words; the card shows them.
    out = gr.rail_sync(str(repo["app_dir"]), "push")
    assert out["ok"] is True
    assert out["result"]["ok"] is False and out["result"]["message"]
    assert out["repo"]["branch"] == "main"


def test_sync_a_crashed_runner_is_a_sentence_too(repo):
    def killed(path, params, timeout):
        return {"ok": False, "error": {"type": "RuntimeError",
                                       "message": "Runner exited with code -9"}}
    out = gr.rail_sync(str(repo["app_dir"]), "push", _run=killed)
    assert out["result"] == {"ok": False, "reason": "failed",
                             "message": "Runner exited with code -9"}


def test_sync_only_push_or_pull(repo):
    with pytest.raises(gs._Refused) as exc:
        gr.rail_sync(str(repo["app_dir"]), "reset")
    assert exc.value.status == 400


def test_sync_route_needs_x_fused_and_an_absolute_path(repo, tmp_path):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app

    (tmp_path / "Fused").mkdir(exist_ok=True)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    body = {"path": str(repo["app_dir"]), "op": "push"}
    assert client.post("/api/git/rail/sync", json=body).status_code == 403
    r = client.post("/api/git/rail/sync", json={"path": "myapp", "op": "push"},
                    headers={"X-Fused": "1"})
    assert r.status_code == 400
    r = client.post("/api/git/rail/sync", json={**body, "op": "reset"},
                    headers={"X-Fused": "1"})
    assert r.status_code == 400


def test_sync_outside_a_repo_is_no_repo_and_runs_nothing(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    def never(*_a, **_k):
        raise AssertionError("ops.py must not run outside a repository")
    out = gr.rail_sync(str(plain), "push", _run=never)
    assert out["repo"] is None
    assert out["result"]["ok"] is False and out["result"]["reason"] == "not-a-repo"


def test_a_copy_of_someone_elses_app_is_guarded(repo, tmp_path):
    bare, _ = _tracked_clone(repo, tmp_path)
    assert gr.rail_status(str(repo["app_dir"]))["repo"]["guarded"] is False
    # contrib §16.0 P2: the push guard on origin marks a copy that follows
    # its author — the card then offers no Send / Get latest.
    _git("config", "remote.origin.pushurl", "no-push://fused", cwd=repo["root"])
    assert gr.rail_status(str(repo["app_dir"]))["repo"]["guarded"] is True

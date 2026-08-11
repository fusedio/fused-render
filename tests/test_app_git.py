"""Version control for app folders (fused_render/app_git.py and its hooks):
every new app ships as a git repo with one boilerplate commit
(POST /api/apps/new), and the claude template's turn-commit helper
(agent._commit_turn) scopes itself to app dirs exactly like the server side.
Manual /api/fs mutations deliberately commit nothing (D245).

Real git, in tmp workspaces (FUSED_RENDER_DIR) — best-effort behaviour is the
contract under test, so nothing here may raise even on non-repos.
"""
import importlib.util
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from fused_render import app_git
from fused_render.server import create_app


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


HDRS = {"X-Fused": "1"}


def _log(app_dir):
    out = subprocess.run(["git", "-C", str(app_dir), "log", "--format=%s"],
                         capture_output=True, text=True)
    return out.stdout.strip().splitlines()


def _make_app(workspace, tag="local", name="demo"):
    d = workspace / tag / name
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html></html>")
    assert app_git.init_repo(str(d))
    return d


# ------------------------------------------------------------------- scoping

def test_app_dir_for_scopes_to_two_levels(workspace, tmp_path):
    app = workspace / "local" / "demo"
    assert app_git.app_dir_for(str(app / "index.html")) == str(app)
    assert app_git.app_dir_for(str(app / "sub" / "x.py")) == str(app)
    assert app_git.app_dir_for(str(workspace)) is None            # root
    assert app_git.app_dir_for(str(workspace / "local")) is None  # tag only
    assert app_git.app_dir_for(str(tmp_path / "elsewhere" / "f")) is None
    assert app_git.app_dir_for(str(workspace / ".hidden" / "x" / "f")) is None


# ------------------------------------------------------- init_repo / commit

def test_init_repo_ships_boilerplate_commit(workspace):
    d = _make_app(workspace)
    assert (d / ".git").is_dir()
    assert _log(d) == ["New app from starter"]
    # Session sidecars stay out of history.
    assert "*.html.json" in (d / ".gitignore").read_text()


def test_commit_records_changes_and_noops_when_clean(workspace):
    d = _make_app(workspace)
    (d / "index.html").write_text("<html>v2</html>")
    assert app_git.commit(str(d / "index.html"), "Edit index.html")
    assert _log(d)[0] == "Edit index.html"
    # Clean tree: nothing to commit, no error.
    assert not app_git.commit(str(d / "index.html"), "Edit index.html")
    # Ignored sidecar change alone: still nothing to commit.
    (d / "index.html.json").write_text("{}")
    assert not app_git.commit(str(d / "index.html"), "Edit index.html")


def test_commit_survives_a_fork_hostile_process(workspace):
    # The server ends up with libproj resident (the fused-engine availability
    # probe imports it), and PROJ's pthread_atfork child handler SIGSEGVs
    # every fork()ed child before exec — subprocess-based git died rc=-11 in
    # the field. app_git spawns via os.posix_spawnp, which runs no atfork
    # handlers. Simulate the hostile process in an isolated child (an atfork
    # abort cannot be unregistered, so it must not poison this test process).
    d = _make_app(workspace)
    (d / "index.html").write_text("<html>v2</html>")
    script = (
        "import os, sys\n"
        "os.register_at_fork(after_in_child=os.abort)\n"
        "from fused_render import app_git\n"
        "ok = app_git.commit(sys.argv[1], 'Edit index.html')\n"
        "sys.exit(0 if ok else 1)\n"
    )
    r = subprocess.run([sys.executable, "-c", script,
                        str(d / "index.html")],
                       env={**os.environ, "FUSED_RENDER_DIR": str(workspace)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert _log(d)[0] == "Edit index.html"


def test_commit_never_touches_non_app_repos(workspace, tmp_path):
    # A real user repo outside the workspace must never be committed to.
    repo = tmp_path / "userrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f.txt").write_text("x")
    assert not app_git.commit(str(repo / "f.txt"), "nope")
    assert _log(repo) == []
    # An app folder without a repo (pre-feature app): silent no-op.
    d = workspace / "local" / "plain"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html></html>")
    assert not app_git.commit(str(d / "index.html"), "nope")


# ----------------------------------------------------------- creation hook

def test_new_app_is_a_repo_with_one_commit(client, workspace):
    r = client.post("/api/apps/new", json={"name": "mine"}, headers=HDRS)
    assert r.status_code == 200
    d = workspace / "local" / "mine"
    assert (d / ".git").is_dir()
    assert _log(d) == ["New app from starter"]


# ------------------------------------------------- claude template mirror

def _agent_module():
    from fused_render.server import templates as server_templates

    path = os.path.join(server_templates.TEMPLATES_DIR, "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("test_claude_agent_git", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_agent_commit_turn_commits_app_and_skips_others(workspace, tmp_path,
                                                        monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE_DIR", str(workspace))
    agent = _agent_module()
    d = _make_app(workspace)
    (d / "index.html").write_text("<html>by claude</html>")
    agent._commit_turn(str(d / "index.html"), "make it pretty")
    assert _log(d)[0] == "Claude: make it pretty"
    # Outside the workspace: never a commit, even in a real repo.
    repo = tmp_path / "userrepo2"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f.txt").write_text("x")
    agent._commit_turn(str(repo / "f.txt"), "nope")
    assert _log(repo) == []

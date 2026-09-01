"""Version control for app folders (fused_render/app_git.py and its hooks):
the `local` tag is ONE shared repository (D626) — every new app lands in it
as one scoped boilerplate commit (POST /api/apps/new), commits are
pathspec-scoped so sibling apps never ride each other's commits, and the
claude template's turn-commit helper (agent._commit_turn) scopes itself to
app dirs exactly like the server side. Manual /api/fs mutations deliberately
commit nothing (D245).

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


def _log(app_dir, scoped=False):
    args = ["git", "-C", str(app_dir), "log", "--format=%s"]
    if scoped:
        args += ["--", "."]
    out = subprocess.run(args, capture_output=True, text=True)
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

def test_init_repo_lands_in_the_shared_local_repo(workspace):
    d = _make_app(workspace)
    # The repo heads the TAG, not the app (D626) — the app gets no `.git`.
    assert (workspace / "local" / ".git").is_dir()
    assert not (d / ".git").exists()
    assert _log(d, scoped=True) == ["New app from starter"]
    # Session sidecars stay out of history — the root `.gitignore`, written
    # once when the shared repo is created.
    gi = (workspace / "local" / ".gitignore").read_text()
    assert "*.html.json" in gi
    # …and so does the app's own `.fused/` state folder (D548). The trailing
    # slash is load-bearing: it ignores the DIRECTORY without touching an
    # exported `<name>.fused` app file (SPEC §43) sitting in the same folder.
    assert ".fused/" in gi
    # …and so does the env-install worker's write-probe
    # (`_env_install_worker._writable_dir`'s `.fused-render-write-probe.<pid>`):
    # best-effort-unlinked, so a process that dies between the `os.open` and the
    # `os.unlink` leaves a stray zero-byte file behind, and a scoped `git add -A`
    # must never sweep it into the app's history.
    assert ".fused-render-write-probe.*" in gi


def test_sibling_apps_never_ride_each_others_commits(workspace):
    """The load-bearing scoping rule: a bare `git add -A` is whole-tree since
    git 2.0, so every write must be pathspec-scoped or a commit about one app
    sweeps a concurrent session's work on its sibling."""
    a = _make_app(workspace, name="alpha")
    b = _make_app(workspace, name="beta")
    (a / "index.html").write_text("<html>a2</html>")
    (b / "index.html").write_text("<html>b2</html>")
    assert app_git.commit(str(a / "index.html"), "Edit alpha")
    # beta's change is still uncommitted — alpha's commit did not sweep it.
    assert _log(b, scoped=True) == ["New app from starter"]
    st = subprocess.run(["git", "-C", str(workspace / "local"), "status",
                         "--porcelain", "--", "beta"],
                        capture_output=True, text=True)
    assert st.stdout.strip()
    assert app_git.commit(str(b / "index.html"), "Edit beta")
    assert _log(b, scoped=True)[0] == "Edit beta"
    # Pathspec-magic name: `[draft]` must scope to ITS folder, not pattern-
    # match siblings (the `:(literal)` armor in _pathspec).
    m = _make_app(workspace, name="[draft]")
    (m / "index.html").write_text("<html>m2</html>")
    (a / "index.html").write_text("<html>a3</html>")
    assert app_git.commit(str(m / "index.html"), "Edit draft")
    assert _log(m, scoped=True)[0] == "Edit draft"
    st = subprocess.run(["git", "-C", str(workspace / "local"), "status",
                         "--porcelain", "--", ":(literal)alpha"],
                        capture_output=True, text=True)
    assert st.stdout.strip()  # alpha's edit still uncommitted


def test_legacy_per_app_repo_keeps_committing_into_itself(workspace):
    """An unmigrated app heading its own `.git` (or one the migration skipped
    for having a remote) keeps the old behaviour — nested repo wins."""
    d = workspace / "local" / "old"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html></html>")
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                    "-m", "seed"], check=True)
    (d / "index.html").write_text("<html>v2</html>")
    assert app_git.commit(str(d / "index.html"), "Edit old app")
    out = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    assert out.stdout.strip() == str(d)
    assert _log(d)[0] == "Edit old app"


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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.fork() (and os.register_at_fork with it) does not exist on "
           "Windows — there is no fork() there at all, so a forked child "
           "cannot hold a stale copy of parent state, which is exactly the "
           "hazard this test simulates and the reason app_git spawns via "
           "posix_spawnp rather than fork+exec in the first place")
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
    assert (workspace / "local" / ".git").is_dir()
    assert not (d / ".git").exists()
    assert _log(d, scoped=True) == ["New app from starter"]


# ------------------------------------------------------------- migration

def _own_repo(d, subjects=("seed",), dirty=None):
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    for s in subjects:
        subprocess.run(["git", "-C", str(d), "-c", "user.name=t",
                        "-c", "user.email=t@t", "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "-c", "user.name=t",
                        "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", s], check=True)
    if dirty:
        (d / dirty).write_text("dirty")


def test_local_monorepo_migration_adopts_per_app_repos(workspace, monkeypatch,
                                                       tmp_path):
    """D626: per-app `.git`s are deleted (history discarded — owner's call),
    each folder lands as one adopt commit, a dirty tree is adopted as-is, a
    repo with a REMOTE is skipped and left its own, and a completed run
    stamps so the next start is a no-op."""
    from fused_render import local_monorepo

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    local = workspace / "local"
    a = local / "alpha"; a.mkdir(parents=True)
    (a / "index.html").write_text("<html></html>")
    # A user-edited .gitignore (a rule of its own) survives the migration.
    (a / ".gitignore").write_text(app_git._GITIGNORE + "recordings/\n")
    _own_repo(a, subjects=("old history",), dirty="new.txt")
    b = local / "beta"; b.mkdir()
    (b / "index.html").write_text("<html></html>")  # never had a repo
    # Our old per-app boilerplate: fully covered by the root file → deleted.
    (b / ".gitignore").write_text(app_git._GITIGNORE)
    # The old starter's unscoped-git instructions must be rewritten on adopt.
    (b / "CLAUDE.md").write_text("# App\n\n" + local_monorepo._OLD_VC + "\n")
    c = local / "gamma"; c.mkdir()
    (c / "index.html").write_text("<html></html>")
    _own_repo(c)
    subprocess.run(["git", "-C", str(c), "remote", "add", "origin",
                    "https://example.invalid/r.git"], check=True)

    local_monorepo.run_once(str(workspace))

    assert (local / ".git").is_dir()
    assert not (a / ".git").exists()          # history discarded
    assert not (b / ".git").exists()
    assert (c / ".git").is_dir()              # remote ⇒ left its own repo
    assert _log(a, scoped=True) == ["Adopt alpha into the workspace repo"]
    assert _log(b, scoped=True) == ["Adopt beta into the workspace repo"]
    # Boilerplate per-app .gitignore gone (root one covers it); user's kept.
    assert not (b / ".gitignore").exists()
    assert (a / ".gitignore").exists()
    # CLAUDE.md's bare `git add -A` instruction rewritten to the scoped verbs.
    md = (b / "CLAUDE.md").read_text()
    assert "git add -A -- ." in md
    assert local_monorepo._OLD_SWEEP not in md
    # The dirty file was adopted as-is into alpha's baseline.
    tracked = subprocess.run(["git", "-C", str(local), "ls-files", "alpha"],
                             capture_output=True, text=True).stdout
    assert "alpha/new.txt" in tracked
    # Stamped: a second run adopts nothing more.
    (b / "later.txt").write_text("x")
    local_monorepo.run_once(str(workspace))
    assert _log(b, scoped=True) == ["Adopt beta into the workspace repo"]


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

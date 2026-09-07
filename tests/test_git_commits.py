"""GET /api/git/commits — a bounded, recent-first log for the version picker.

Model: a real repository (tests/test_git_snapshot.py's fixture style), so the
route is exercised against actual `git log` behavior rather than a mocked one.
"""
import os
import subprocess

import pytest

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
    """A repo with an app folder (`myapp/`) touched by some commits, and an
    unrelated top-level file touched by others — so a test can prove the log
    is scoped to the app folder rather than the whole repo."""
    root = tmp_path / "repo"
    app_dir = root / "myapp"
    app_dir.mkdir(parents=True)
    (app_dir / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>v1</body></html>',
        encoding="utf-8")
    (app_dir / "reader.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "README.md").write_text("repo readme\n", encoding="utf-8")
    _git("init", "-q", cwd=root)
    app_sha_1 = _commit(root, "myapp: v1")

    (root / "README.md").write_text("repo readme, updated\n", encoding="utf-8")
    _commit(root, "unrelated: touch the readme")  # must not appear in myapp's log

    (app_dir / "reader.py").write_text("VALUE = 2\n", encoding="utf-8")
    app_sha_2 = _commit(root, "myapp: v2")

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return {"root": root, "app_dir": app_dir, "app_sha_1": app_sha_1,
            "app_sha_2": app_sha_2}


def test_commits_scoped_to_the_app_folder_not_the_whole_repo(repo):
    result = gs.list_commits(str(repo["app_dir"] / "reader.py"))
    assert result["ok"] is True
    shas = [c["sha"] for c in result["commits"]]
    assert shas == [repo["app_sha_2"], repo["app_sha_1"]]  # newest first
    subjects = [c["subject"] for c in result["commits"]]
    assert subjects == ["myapp: v2", "myapp: v1"]
    assert result["has_more"] is False
    assert result["total"] == 2


def test_commit_fields(repo):
    result = gs.list_commits(str(repo["app_dir"]))
    top = result["commits"][0]
    assert top["sha"] == repo["app_sha_2"]
    assert top["short"] and repo["app_sha_2"].startswith(top["short"])
    assert top["subject"] == "myapp: v2"
    assert top["author"] == "T"
    assert isinstance(top["when"], int) and top["when"] > 0


def test_a_path_with_no_app_folder_404s(repo):
    plain = repo["root"] / "notes.txt"
    plain.write_text("x", encoding="utf-8")
    _commit(repo["root"], "add notes")
    with pytest.raises(gs._Refused) as exc:
        gs.list_commits(str(plain))
    assert exc.value.status == 404


def test_relative_path_is_400(repo):
    with pytest.raises(gs._Refused) as exc:
        gs.list_commits("myapp/reader.py")
    assert exc.value.status == 400


def test_mount_backed_path_refuses(repo, monkeypatch):
    monkeypatch.setattr(
        "fused_render.server.routers.git_snapshot.shell_mounts.is_mount_backed",
        lambda p: True)
    with pytest.raises(gs._Refused) as exc:
        gs.list_commits(str(repo["app_dir"] / "reader.py"))
    assert exc.value.status == 400


def test_limit_honoured_and_has_more_truthful(repo):
    result = gs.list_commits(str(repo["app_dir"]), limit=1)
    assert len(result["commits"]) == 1
    assert result["commits"][0]["sha"] == repo["app_sha_2"]
    assert result["has_more"] is True
    # `total` counts ALL commits touching the app folder, not just the one
    # that fit under `limit` — otherwise a capped list's newest row would be
    # labelled `v1` (the returned list's own length) instead of the true `v2`.
    assert result["total"] == 2

    exact = gs.list_commits(str(repo["app_dir"]), limit=2)
    assert len(exact["commits"]) == 2
    assert exact["has_more"] is False  # exactly two commits touch myapp/, no more
    assert exact["total"] == 2


def test_total_stays_correct_regardless_of_cap(repo):
    """The same total for limit=1 and limit=30 proves `total` is not merely
    echoing `len(commits)` under a different name — it is an independent
    count that does not move when the cap does."""
    capped = gs.list_commits(str(repo["app_dir"]), limit=1)
    uncapped = gs.list_commits(str(repo["app_dir"]), limit=30)
    assert capped["total"] == uncapped["total"] == 2


def test_an_empty_repository_returns_ok_with_an_empty_list(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    app_dir = root / "myapp"
    app_dir.mkdir(parents=True)
    (app_dir / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body></body></html>',
        encoding="utf-8")
    _git("init", "-q", cwd=root)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    # No commit made yet: HEAD is unborn. `_resolve_app_dir` still finds the
    # app folder on disk (app_entry needs no git history), so this is a real
    # exercise of `_run_log`'s "no commits yet" path, not a 404.
    result = gs.list_commits(str(app_dir))
    assert result == {"ok": True, "commits": [], "has_more": False, "total": 0}

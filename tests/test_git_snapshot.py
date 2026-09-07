"""GET /api/git/snapshot — extracting the enclosing app folder at a commit.

Model: a real repository, built with real `git` calls (`tests/test_git_scope.py`'s
fixture style), so the route is exercised against actual git behavior rather than
a mocked one.
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
    """A repo with an app folder (`myapp/`) that changes across two commits."""
    root = tmp_path / "repo"
    app_dir = root / "myapp"
    app_dir.mkdir(parents=True)
    (app_dir / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><body>v1</body></html>',
        encoding="utf-8")
    (app_dir / "reader.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git("init", "-q", cwd=root)
    old_sha = _commit(root, "v1")

    (app_dir / "reader.py").write_text("VALUE = 2\n", encoding="utf-8")
    new_sha = _commit(root, "v2")

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return {"root": root, "app_dir": app_dir, "old_sha": old_sha,
            "new_sha": new_sha}


def _cache_dirs(cache_root):
    if not os.path.isdir(cache_root):
        return []
    out = []
    for key in os.listdir(cache_root):
        key_dir = os.path.join(cache_root, key)
        if os.path.isdir(key_dir):
            out += [os.path.join(key_dir, s) for s in os.listdir(key_dir)]
    return out


# --------------------------------------------------------------- the happy path


def test_extraction_carries_the_old_commits_bytes(repo):
    result = gs.extract_snapshot(
        str(repo["app_dir"] / "reader.py"), repo["old_sha"])
    assert result["ok"] is True
    with open(os.path.join(result["dir"], "reader.py"), encoding="utf-8") as f:
        assert f.read() == "VALUE = 1\n"
    # Not today's content, which the live file already has.
    with open(repo["app_dir"] / "reader.py", encoding="utf-8") as f:
        assert f.read() == "VALUE = 2\n"
    assert result["entry"] == os.path.join(result["dir"], "index.html")
    assert result["app_dir"] == os.path.realpath(str(repo["app_dir"]))


def test_a_second_call_is_a_cache_hit_that_spawns_no_git(repo, monkeypatch):
    first = gs.extract_snapshot(str(repo["app_dir"] / "reader.py"),
                                repo["old_sha"])

    called = []
    real_popen = subprocess.Popen

    def _tracking_popen(*args, **kwargs):
        called.append(args)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _tracking_popen)
    second = gs.extract_snapshot(str(repo["app_dir"] / "reader.py"),
                                 repo["old_sha"])
    assert second == first
    assert called == []  # no git, no tar — the cache hit never forks


def test_no_app_above_the_path_is_404(repo):
    plain = repo["root"] / "notes.txt"
    plain.write_text("x", encoding="utf-8")
    _commit(repo["root"], "add notes")
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot(str(plain), repo["old_sha"])
    assert exc.value.status == 404


def test_non_hex_sha_is_400(repo):
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), "HEAD~1")
    assert exc.value.status == 400


def test_relative_path_is_400(repo):
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot("myapp/reader.py", repo["old_sha"])
    assert exc.value.status == 400


def test_mount_backed_path_refuses(repo, monkeypatch):
    monkeypatch.setattr(
        "fused_render.server.routers.git_snapshot.shell_mounts.is_mount_backed",
        lambda p: True)
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), repo["old_sha"])
    assert exc.value.status == 400


def test_entry_resolves_from_the_extracted_tree_when_renamed_since(repo):
    """An app whose entry page was renamed since the target commit must open
    at the entry that commit had, not at today's filename."""
    app_dir = repo["app_dir"]
    _git("mv", "index.html", "main.html", cwd=app_dir)
    renamed_sha = _commit(repo["root"], "rename entry")

    result = gs.extract_snapshot(str(app_dir / "main.html"), renamed_sha)
    assert result["entry"] == os.path.join(result["dir"], "main.html")

    # But the OLD commit's extraction still finds the OLD entry name.
    old_result = gs.extract_snapshot(str(app_dir / "reader.py"),
                                     repo["old_sha"])
    assert old_result["entry"] == os.path.join(old_result["dir"], "index.html")


def test_unknown_sha_is_404(repo):
    with pytest.raises(gs._Refused) as exc:
        gs.extract_snapshot(str(repo["app_dir"] / "reader.py"), "f" * 40)
    assert exc.value.status == 404

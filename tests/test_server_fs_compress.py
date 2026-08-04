"""Tests for /api/fs/compress (fused_render/server/fs_mutate.py).

Like test_server_fs_mutate.py these drive the module-level `_fs_compress`
helper directly (not through the TestClient), asserting both what lands on
disk and the wire error contract shared with the other mutations:
  400 bad path / bad format, 403 readonly ("readonly"), 404 missing source,
  409 conflict ("conflict"), plus the X-Fused guard.
"""
import json
import os
import stat
import subprocess
import tarfile
import zipfile

import pytest
from fastapi.responses import JSONResponse

from fused_render.server.fs_mutate import _fs_compress as COMPRESS

from tests.test_server_git_repo import git, make_repo

skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


def make_tree(root):
    root.mkdir(parents=True)
    (root / "a.txt").write_text("alpha\n")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("beta\n")
    (root / "empty").mkdir()
    return root


# ------------------------------------------------------------ X-Fused guard

def test_guard_rejects_missing_header(tmp_path):
    resp = COMPRESS({"path": str(tmp_path), "format": "zip"}, x_fused=None)
    assert _status(resp) == 403
    assert "X-Fused" in _data(resp)["error"]


# ------------------------------------------------------------------- zip

def test_zip_writes_sibling_archive_and_returns_stat(tmp_path):
    src = make_tree(tmp_path / "proj")
    out = _data(COMPRESS({"path": str(src), "format": "zip"}, x_fused="1"))
    dest = tmp_path / "proj.zip"
    assert dest.is_file()
    assert out["path"] == str(dest)
    assert out["name"] == "proj.zip"
    assert out["is_dir"] is False
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
        assert "proj/a.txt" in names
        assert "proj/sub/b.txt" in names
        assert z.read("proj/sub/b.txt") == b"beta\n"
    # An empty subdirectory survives as a directory entry.
    assert any(n.rstrip("/") == "proj/empty" for n in names)


def test_zip_honours_an_explicit_dest(tmp_path):
    src = make_tree(tmp_path / "proj")
    dest = tmp_path / "proj 2.zip"
    out = _data(COMPRESS({"path": str(src), "format": "zip", "dest": str(dest)},
                         x_fused="1"))
    assert dest.is_file() and out["name"] == "proj 2.zip"


def test_zip_does_not_contain_itself(tmp_path):
    # Pathological but cheap to guard: a dest INSIDE the folder being zipped
    # must not be archived into itself.
    src = make_tree(tmp_path / "proj")
    dest = src / "inner.zip"
    COMPRESS({"path": str(src), "format": "zip", "dest": str(dest)}, x_fused="1")
    with zipfile.ZipFile(dest) as z:
        assert not any(n.endswith("inner.zip") for n in z.namelist())


def test_zip_stores_symlinks_without_following_them(tmp_path):
    src = make_tree(tmp_path / "proj")
    outside = tmp_path / "secret.txt"
    outside.write_text("do not inline me\n")
    try:
        os.symlink(str(outside), str(src / "link.txt"))
        os.symlink(str(src), str(src / "loop"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    out = _data(COMPRESS({"path": str(src), "format": "zip"}, x_fused="1"))
    with zipfile.ZipFile(out["path"]) as z:
        info = z.getinfo("proj/link.txt")
        assert stat.S_ISLNK(info.external_attr >> 16)
        assert z.read("proj/link.txt") == str(outside).encode()  # target, not content
        # The self-referential dir symlink is stored, never descended into.
        assert stat.S_ISLNK(z.getinfo("proj/loop").external_attr >> 16)
        assert not any(n.startswith("proj/loop/") for n in z.namelist())


def test_zip_leaves_nothing_behind_when_it_fails(tmp_path, monkeypatch):
    # The archive is built to a temp file and renamed, so a mid-build failure
    # never leaves a truncated .zip sitting at the final path.
    src = make_tree(tmp_path / "proj")
    from fused_render.server import fs_mutate as mod
    real = mod.zipfile.ZipFile

    class Boom(real):
        def write(self, *a, **k):
            raise OSError("disk full")

    monkeypatch.setattr(mod.zipfile, "ZipFile", Boom)
    resp = COMPRESS({"path": str(src), "format": "zip"}, x_fused="1")
    assert _status(resp) == 400
    assert not (tmp_path / "proj.zip").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


# ------------------------------------------------------------- git formats

def test_git_bundle_of_a_repo_root(tmp_path):
    repo = make_repo(tmp_path / "repo")
    out = _data(COMPRESS({"path": str(repo), "format": "git-bundle"}, x_fused="1"))
    dest = tmp_path / "repo.bundle"
    assert dest.is_file() and out["name"] == "repo.bundle"
    # A real bundle: git can verify it and clone from it.
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(dest), str(clone))
    assert (clone / "a.txt").read_text() == "hello\n"


def test_git_archive_of_head(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "untracked.txt").write_text("nope\n")
    out = _data(COMPRESS({"path": str(repo), "format": "git-archive"}, x_fused="1"))
    dest = tmp_path / "repo.tar.gz"
    assert dest.is_file() and out["name"] == "repo.tar.gz"
    with tarfile.open(dest, "r:gz") as t:
        names = set(t.getnames())
    assert "a.txt" in names
    assert "untracked.txt" not in names  # tracked files at HEAD only


def test_git_bundle_of_a_repo_with_no_commits_400(tmp_path):
    repo = make_repo(tmp_path / "empty", commit=False)
    resp = COMPRESS({"path": str(repo), "format": "git-bundle"}, x_fused="1")
    assert _status(resp) == 400
    assert "no commits" in _data(resp)["error"]
    assert not (tmp_path / "empty.bundle").exists()


def test_git_archive_of_a_repo_with_no_commits_400(tmp_path):
    repo = make_repo(tmp_path / "empty", commit=False)
    resp = COMPRESS({"path": str(repo), "format": "git-archive"}, x_fused="1")
    assert _status(resp) == 400
    assert "no commits" in _data(resp)["error"]


def test_git_format_on_a_non_repo_400(tmp_path):
    d = make_tree(tmp_path / "plain")
    resp = COMPRESS({"path": str(d), "format": "git-bundle"}, x_fused="1")
    assert _status(resp) == 400
    assert "not a git repository" in _data(resp)["error"]


def test_git_format_on_a_repo_subdirectory_400(tmp_path):
    # The menu never offers this, but the endpoint must not quietly bundle the
    # whole parent repo when asked for a subdirectory.
    repo = make_repo(tmp_path / "repo")
    sub = repo / "src"
    sub.mkdir()
    resp = COMPRESS({"path": str(sub), "format": "git-bundle"}, x_fused="1")
    assert _status(resp) == 400
    assert "not a git repository" in _data(resp)["error"]


def test_zip_of_a_repo_subdirectory_is_fine(tmp_path):
    repo = make_repo(tmp_path / "repo")
    sub = repo / "src"
    sub.mkdir()
    (sub / "c.txt").write_text("gamma\n")
    out = _data(COMPRESS({"path": str(sub), "format": "zip"}, x_fused="1"))
    assert out["name"] == "src.zip"
    assert (repo / "src.zip").is_file()


# ------------------------------------------------------------ error contract

@pytest.mark.parametrize("fmt", ["zip", "git-bundle", "git-archive"])
def test_relative_path_400(fmt):
    resp = COMPRESS({"path": "relative/dir", "format": fmt}, x_fused="1")
    assert _status(resp) == 400


def test_missing_path_400():
    assert _status(COMPRESS({"format": "zip"}, x_fused="1")) == 400


@pytest.mark.parametrize("fmt", [None, "", "tar", "rar", "zip;rm -rf /", "ZIP", 7])
def test_bad_format_400(tmp_path, fmt):
    src = make_tree(tmp_path / "proj")
    resp = COMPRESS({"path": str(src), "format": fmt}, x_fused="1")
    assert _status(resp) == 400
    assert "format" in _data(resp)["error"]


def test_source_missing_404(tmp_path):
    resp = COMPRESS({"path": str(tmp_path / "nope"), "format": "zip"}, x_fused="1")
    assert _status(resp) == 404


def test_source_is_a_file_400(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    resp = COMPRESS({"path": str(f), "format": "zip"}, x_fused="1")
    assert _status(resp) == 400
    assert "not a directory" in _data(resp)["error"]


def test_existing_destination_409(tmp_path):
    src = make_tree(tmp_path / "proj")
    (tmp_path / "proj.zip").write_text("in the way")
    resp = COMPRESS({"path": str(src), "format": "zip"}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"
    assert (tmp_path / "proj.zip").read_text() == "in the way"


def test_relative_dest_400(tmp_path):
    src = make_tree(tmp_path / "proj")
    resp = COMPRESS({"path": str(src), "format": "zip", "dest": "out.zip"}, x_fused="1")
    assert _status(resp) == 400


def test_missing_dest_parent_400(tmp_path):
    src = make_tree(tmp_path / "proj")
    resp = COMPRESS({"path": str(src), "format": "zip",
                     "dest": str(tmp_path / "gone" / "proj.zip")}, x_fused="1")
    assert _status(resp) == 400
    assert "parent directory does not exist" in _data(resp)["error"]


@skip_root
def test_readonly_destination_403(tmp_path):
    holder = tmp_path / "ro"
    holder.mkdir()
    src = make_tree(holder / "proj")
    os.chmod(holder, 0o555)
    try:
        resp = COMPRESS({"path": str(src), "format": "zip"}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
    finally:
        os.chmod(holder, 0o755)


# -------------------------------------------------------------------- mounts

def test_mount_backed_source_is_refused_without_walking_it(tmp_path, monkeypatch):
    from fused_render.shell import mounts as shell_mounts

    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda p: True)
    monkeypatch.setattr(shell_mounts, "mount_read_only", lambda p: False)
    walked = []
    monkeypatch.setattr(os, "walk", lambda *a, **k: walked.append(a) or iter(()))
    resp = COMPRESS({"path": str(tmp_path / "mnt" / "proj"), "format": "zip"},
                    x_fused="1")
    assert _status(resp) == 400
    assert "compress unsupported" in _data(resp)["error"]
    assert walked == []


def test_read_only_mount_source_is_readonly_403(tmp_path, monkeypatch):
    from fused_render.shell import mounts as shell_mounts

    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda p: True)
    monkeypatch.setattr(shell_mounts, "mount_read_only", lambda p: True)
    resp = COMPRESS({"path": str(tmp_path / "mnt" / "proj"), "format": "zip"},
                    x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"


def test_mount_backed_destination_is_refused(tmp_path, monkeypatch):
    from fused_render.shell import mounts as shell_mounts

    src = make_tree(tmp_path / "proj")
    dest = tmp_path / "mnt" / "proj.zip"
    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda p: str(p).startswith(str(tmp_path / "mnt")))
    monkeypatch.setattr(shell_mounts, "mount_read_only", lambda p: False)
    resp = COMPRESS({"path": str(src), "format": "zip", "dest": str(dest)}, x_fused="1")
    assert _status(resp) == 400
    assert "compress unsupported" in _data(resp)["error"]


# ------------------------------------------------------------ no shell, ever

def test_git_is_run_as_an_argv_list_with_no_shell(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    seen = {}
    from fused_render.server import fs_mutate as mod
    real_run = mod.subprocess.run

    def spy(cmd, **kw):
        if isinstance(cmd, list) and cmd[:1] == ["git"]:
            seen.setdefault("calls", []).append((cmd, kw))
        return real_run(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", spy)
    COMPRESS({"path": str(repo), "format": "git-bundle"}, x_fused="1")
    assert seen["calls"], "expected git to be invoked"
    for cmd, kw in seen["calls"]:
        assert isinstance(cmd, list)
        assert kw.get("shell", False) is False
        assert kw.get("stdin") is subprocess.DEVNULL
        assert kw.get("timeout")

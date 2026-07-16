"""Tests for the file-mutation POSTs (fused_render/server.py):
/api/fs/mkdir, /api/fs/delete, /api/fs/rename, /api/fs/copy.

Like test_server_fs_write.py these drive the module-level _fs_* helpers
directly (not through the starlette TestClient), asserting both the happy
path on disk and the wire error contract shared with _fs_write:
  400 relative/invalid path, 403 readonly ("readonly"), 404 missing source,
  409 conflict ("conflict"). All four also carry the X-Fused guard.
"""
import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

from fused_render import server
from fused_render.server import _fs_copy as COPY
from fused_render.server import _fs_delete as DELETE
from fused_render.server import _fs_mkdir as MKDIR
from fused_render.server import _fs_rename as RENAME


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


# ---------------------------------------------------------------- X-Fused guard

@pytest.mark.parametrize("fn,body", [
    (MKDIR, {"path": "/x"}),
    (DELETE, {"path": "/x"}),
    (RENAME, {"src": "/x", "dst": "/y"}),
    (COPY, {"src": "/x", "dst": "/y"}),
])
def test_guard_rejects_missing_header(fn, body):
    resp = fn(body, x_fused=None)
    assert _status(resp) == 403
    assert "X-Fused" in _data(resp)["error"]


# --------------------------------------------------------------------- mkdir

def test_mkdir_creates_and_returns_stat(tmp_path):
    d = tmp_path / "newdir"
    out = _data(MKDIR({"path": str(d)}, x_fused="1"))
    assert d.is_dir()
    assert out["is_dir"] is True
    assert out["path"] == str(d)
    assert out["name"] == "newdir"


def test_mkdir_relative_path_400(tmp_path):
    resp = MKDIR({"path": "relative/dir"}, x_fused="1")
    assert _status(resp) == 400


def test_mkdir_missing_parent_400(tmp_path):
    resp = MKDIR({"path": str(tmp_path / "a" / "b")}, x_fused="1")
    assert _status(resp) == 400
    assert not (tmp_path / "a").exists()


def test_mkdir_existing_path_409(tmp_path):
    d = tmp_path / "exists"
    d.mkdir()
    resp = MKDIR({"path": str(d)}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"


def test_mkdir_readonly_parent_403(tmp_path):
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        resp = MKDIR({"path": str(tmp_path / "nope")}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
    finally:
        os.chmod(tmp_path, stat.S_IRWXU)


# -------------------------------------------------------------------- delete

def test_delete_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    out = _data(DELETE({"path": str(f)}, x_fused="1"))
    assert not f.exists()
    assert out["deleted"] == str(f)


def test_delete_empty_dir_without_flag(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert _status(DELETE({"path": str(d)}, x_fused="1")) == 200
    assert not d.exists()


def test_delete_nonempty_dir_requires_recursive(tmp_path):
    d = tmp_path / "full"
    d.mkdir()
    (d / "child").write_text("x")
    resp = DELETE({"path": str(d)}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"
    assert d.exists()  # untouched


def test_delete_nonempty_dir_recursive(tmp_path):
    d = tmp_path / "full"
    d.mkdir()
    (d / "child").write_text("x")
    assert _status(DELETE({"path": str(d), "recursive": True}, x_fused="1")) == 200
    assert not d.exists()


def test_delete_missing_404(tmp_path):
    resp = DELETE({"path": str(tmp_path / "ghost")}, x_fused="1")
    assert _status(resp) == 404


def test_delete_symlink_to_dir_removes_link_not_target(tmp_path):
    # A symlink to a directory must be unlinked as the link itself — never
    # rmtree'd (which raises) and never followed into the target (which would
    # wipe the target's contents).
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    out = _data(DELETE({"path": str(link)}, x_fused="1"))
    assert out["deleted"] == str(link)
    assert not link.exists()  # link gone
    assert target.is_dir()  # target survives
    assert (target / "keep.txt").read_text() == "x"  # contents intact


def test_delete_readonly_file_403(tmp_path):
    f = tmp_path / "ro.txt"
    f.write_text("x")
    os.chmod(f, stat.S_IRUSR)
    try:
        resp = DELETE({"path": str(f)}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
        assert f.exists()
    finally:
        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)


# --------------------------------------------------------------- delete: trash


def _fake_home(monkeypatch, tmp_path):
    """Point Path.home() at a throwaway home dir and force trash 'supported'
    so the trash path is exercised regardless of the CI platform. Returns the
    fake home's .Trash directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(server, "_trash_supported", lambda: True)
    return home / ".Trash"


def test_delete_trash_moves_into_home_trash(tmp_path, monkeypatch):
    trash = _fake_home(monkeypatch, tmp_path)
    f = tmp_path / "f.txt"
    f.write_text("keep")
    out = _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert out == {"deleted": str(f), "trashed": True}
    assert not f.exists()  # moved out of its folder
    assert (trash / "f.txt").read_text() == "keep"  # landed in the Trash


def test_delete_trash_dedupe_suffix(tmp_path, monkeypatch):
    trash = _fake_home(monkeypatch, tmp_path)
    trash.mkdir(parents=True)
    (trash / "f.txt").write_text("old")  # a same-named file already in the Bin
    f = tmp_path / "f.txt"
    f.write_text("new")
    _data(DELETE({"path": str(f), "trash": True}, x_fused="1"))
    assert (trash / "f.txt").read_text() == "old"  # existing one untouched
    assert (trash / "f 2.txt").read_text() == "new"  # new one deduped


def test_delete_trash_unsupported_returns_501(tmp_path, monkeypatch):
    # Non-darwin (or Trash otherwise unavailable) → a 501 the frontend keys on
    # to fall back to a hard delete; the file must be left in place.
    monkeypatch.setattr(server, "_trash_supported", lambda: False)
    f = tmp_path / "f.txt"
    f.write_text("x")
    resp = DELETE({"path": str(f), "trash": True}, x_fused="1")
    assert _status(resp) == 501
    assert _data(resp)["error"] == "trash unsupported"
    assert f.exists()  # untouched — caller falls back


# -------------------------------------------------------------------- rename

def test_rename_file(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi")
    dst = tmp_path / "b.txt"
    out = _data(RENAME({"src": str(src), "dst": str(dst)}, x_fused="1"))
    assert not src.exists()
    assert dst.read_text() == "hi"
    assert out["path"] == str(dst)
    assert out["is_dir"] is False


def test_rename_dir(tmp_path):
    src = tmp_path / "d1"
    src.mkdir()
    (src / "c").write_text("x")
    dst = tmp_path / "d2"
    out = _data(RENAME({"src": str(src), "dst": str(dst)}, x_fused="1"))
    assert not src.exists()
    assert (dst / "c").read_text() == "x"
    assert out["is_dir"] is True


def test_rename_missing_src_404(tmp_path):
    resp = RENAME({"src": str(tmp_path / "ghost"), "dst": str(tmp_path / "x")}, x_fused="1")
    assert _status(resp) == 404


def test_rename_dst_exists_409(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "b.txt"
    dst.write_text("b")
    resp = RENAME({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"
    assert src.exists() and dst.read_text() == "b"


def test_rename_overwrite(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "b.txt"
    dst.write_text("b")
    _data(RENAME({"src": str(src), "dst": str(dst), "overwrite": True}, x_fused="1"))
    assert not src.exists()
    assert dst.read_text() == "a"


def test_rename_relative_dst_400(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    assert _status(RENAME({"src": str(src), "dst": "rel"}, x_fused="1")) == 400


def test_rename_dir_into_itself_400(tmp_path):
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    for dst in (d / "d", d / "sub" / "d"):
        resp = RENAME({"src": str(d), "dst": str(dst)}, x_fused="1")
        assert _status(resp) == 400
        assert "into itself" in _data(resp)["error"]
    assert (d / "sub").is_dir()  # tree untouched


def test_rename_readonly_src_403(tmp_path):
    # A move deletes the source, so a readonly source must refuse the same way
    # delete does — otherwise rename lifts entries off a read-only location.
    src = tmp_path / "a.txt"
    src.write_text("a")
    os.chmod(src, stat.S_IRUSR)
    try:
        resp = RENAME({"src": str(src), "dst": str(tmp_path / "b.txt")}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
        assert src.exists()
    finally:
        os.chmod(src, stat.S_IRWXU)


def test_rename_missing_dst_parent_400(tmp_path):
    # A missing dst parent is a 400 (not the misleading "readonly" 403 that
    # _writable would otherwise produce for an outside/unwritable ancestor).
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "nope" / "b.txt"
    resp = RENAME({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 400
    assert "parent directory does not exist" in _data(resp)["error"]
    assert src.exists()  # untouched


# ---------------------------------------------------------------------- copy

def test_copy_file(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi")
    dst = tmp_path / "b.txt"
    out = _data(COPY({"src": str(src), "dst": str(dst)}, x_fused="1"))
    assert src.read_text() == "hi"  # source kept
    assert dst.read_text() == "hi"
    assert out["path"] == str(dst)
    assert out["is_dir"] is False


def test_copy_dir(tmp_path):
    src = tmp_path / "d1"
    src.mkdir()
    (src / "c").write_text("x")
    dst = tmp_path / "d2"
    out = _data(COPY({"src": str(src), "dst": str(dst)}, x_fused="1"))
    assert (src / "c").exists()
    assert (dst / "c").read_text() == "x"
    assert out["is_dir"] is True


def test_copy_missing_src_404(tmp_path):
    resp = COPY({"src": str(tmp_path / "ghost"), "dst": str(tmp_path / "x")}, x_fused="1")
    assert _status(resp) == 404


def test_copy_dst_exists_409(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "b.txt"
    dst.write_text("b")
    resp = COPY({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"
    assert dst.read_text() == "b"


def test_copy_overwrite(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "b.txt"
    dst.write_text("b")
    _data(COPY({"src": str(src), "dst": str(dst), "overwrite": True}, x_fused="1"))
    assert dst.read_text() == "a"


def test_copy_missing_dst_parent_400(tmp_path):
    # Same honest 400 as rename when dst's parent doesn't exist.
    src = tmp_path / "a.txt"
    src.write_text("a")
    dst = tmp_path / "nope" / "b.txt"
    resp = COPY({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 400
    assert "parent directory does not exist" in _data(resp)["error"]
    assert src.exists()  # source untouched


def test_copy_dir_into_descendant_400(tmp_path):
    src = tmp_path / "d1"
    src.mkdir()
    dst = src / "sub"
    resp = COPY({"src": str(src), "dst": str(dst)}, x_fused="1")
    assert _status(resp) == 400
    assert not dst.exists()

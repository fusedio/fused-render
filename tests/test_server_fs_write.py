"""Tests for /api/fs/stat's `writable` flag and /api/fs/write's read-only
guard (fused_render/server.py).

The route handlers are thin wrappers over module-level _fs_stat / _fs_write,
which these drive directly — the same "avoid starlette TestClient" discipline
as test_server_session.py.

Rationale: the atomic write lands bytes in a temp file and os.replace()s it
over the target, which succeeds on a chmod -w file as long as the parent
directory is writable — silently bypassing the read-only bit. The guard makes
the write endpoint refuse instead, and `writable` on the stat payload lets
templates render read-only mode up front.
"""
import json
import os
import stat

import pytest
from fastapi.responses import JSONResponse

from fused_render.server import _fs_stat as STAT
from fused_render.server import _fs_write as WRITE


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


def _write(path, content, **kw):
    body = {"path": str(path), "content": content, **kw}
    return WRITE(body, x_fused="1")


@pytest.fixture
def target(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("original")
    return f


@pytest.fixture
def readonly(target):
    os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    yield target
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)  # so tmp_path cleanup works


# ------------------------------------------------------------- stat.writable

def test_stat_writable_true_for_writable_file(target):
    out = _data(STAT(str(target)))
    assert out["writable"] is True


def test_stat_writable_false_for_readonly_file(readonly):
    out = _data(STAT(str(readonly)))
    assert out["writable"] is False


def test_stat_writable_on_directory(tmp_path):
    assert _data(STAT(str(tmp_path)))["writable"] is True
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert _data(STAT(str(tmp_path)))["writable"] is False
    finally:
        os.chmod(tmp_path, stat.S_IRWXU)


# --------------------------------------------------------- write guard (403)

def test_write_refuses_readonly_target(readonly):
    resp = _write(readonly, "clobbered")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"
    assert readonly.read_text() == "original"  # bytes untouched


def test_write_succeeds_and_reports_writable(target):
    out = _data(_write(target, "updated"))
    assert target.read_text() == "updated"
    assert out["writable"] is True


def test_write_creates_new_file_in_writable_dir(tmp_path):
    f = tmp_path / "new.txt"
    out = _data(_write(f, "hello"))
    assert f.read_text() == "hello"
    assert out["writable"] is True


# ----------------------------------------------------- create guard (New File)

def test_write_create_conflicts_on_existing_file(target):
    resp = _write(target, "clobbered", create=True)
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"
    assert target.read_text() == "original"  # bytes untouched


def test_write_create_ok_for_new_file(tmp_path):
    f = tmp_path / "fresh.txt"
    out = _data(_write(f, "hi", create=True))
    assert f.read_text() == "hi"
    assert out["writable"] is True

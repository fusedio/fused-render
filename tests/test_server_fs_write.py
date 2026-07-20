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


# ---------------------------------------------------- read-only remote mounts
# Under a mount, os.access(W_OK) lies: with CacheMode=full a kernel write
# lands in the local VFS cache and only fails at async upload. The mount
# record carries a `read_only` flag (detected at attach, see
# test_shell_mounts), and _writable must consult it so stat.writable and the
# write guard stay in agreement (RO-1) for remote paths too.

@pytest.fixture
def mounted(tmp_path, monkeypatch):
    """A real file sitting under a fake mountpoint inside a redirected
    FUSED_RENDER_HOME. Returns a factory: mounted(read_only=...) -> file path.

    fs/stat routes a mount-backed stat through the rclone rc API instead of the
    kernel (a kernel GETATTR over a mount can wedge it), so a live stub rcd must
    answer operations/stat for the mounted file."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    import fused_render.shell.mounts as mounts
    from test_shell_mounts import StubRcd

    stub = StubRcd()
    stub.responses["operations/stat"] = {"item": {"Size": len(b"original")}}
    mounts.write_rcd_state(stub.port, 4242)

    def make(name, read_only):
        m = mounts.add_mount(name, f"{name}-remote:bucket", read_only=read_only)
        mp = mounts.mountpoint(m)
        os.makedirs(mp)
        f = os.path.join(mp, "notes.txt")
        with open(f, "w") as fh:
            fh.write("original")
        return f

    yield make
    stub.close()


def test_stat_not_writable_under_read_only_mount(mounted):
    out = _data(STAT(mounted("pub", read_only=True)))
    assert out["writable"] is False
    assert out["remote"] is True


def test_stat_writable_under_writable_mount(mounted):
    out = _data(STAT(mounted("data", read_only=False)))
    assert out["writable"] is True


def test_write_refuses_read_only_mount(mounted):
    f = mounted("pub", read_only=True)
    resp = _write(f, "changed")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"
    with open(f) as fh:
        assert fh.read() == "original"


def test_write_refuses_new_file_under_read_only_mount(mounted):
    f = mounted("pub", read_only=True)
    resp = _write(os.path.join(os.path.dirname(f), "new.txt"), "x")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"

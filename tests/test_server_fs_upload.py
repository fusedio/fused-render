"""Tests for POST /api/fs/upload — the binary sibling of /api/fs/write.

/api/fs/write hard-requires `content` to be a string (fs_mutate._fs_write), so
there was no way to put arbitrary BYTES on disk from a template. The markdown
template's paste/drop of an image or a video needs exactly that, and base64 in
JSON would inflate a pasted video by a third for no reason.

The contract is deliberately _fs_write's, minus the text-only parts (no
optimistic lock, no create-exclusive): X-Fused, an absolute `path`, a parent
that must already exist (no mkdir -p), a "readonly" 403 for a protected target
or a read-only mount, and the same /api/fs/stat payload on success.

The happy path goes through the real TestClient because multipart parsing is
half of what is being added; the refusals drive the `_fs_upload` helper
directly, the way tests/test_server_fs_mutate.py drives its siblings.
"""
import json
import os
import stat

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.server.fs_mutate import _fs_upload as UPLOAD

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00\xff\x01\xfe binary \x00 bytes"

# os.access always says yes for root, so the chmod-based gates can't trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


# ------------------------------------------------------------- the round trip

def test_upload_writes_the_exact_bytes_and_returns_a_stat(client, tmp_path):
    dest = tmp_path / "assets" / "pasted-20260802-143022.png"
    dest.parent.mkdir()
    res = client.post(
        "/api/fs/upload",
        data={"path": str(dest)},
        files={"file": ("blob", PNG, "image/png")},
        headers={"X-Fused": "1"},
    )
    assert res.status_code == 200, res.text
    # Byte-exact: a text round trip through UTF-8 would mangle every one of
    # these, which is the whole reason this endpoint is not /api/fs/write.
    assert dest.read_bytes() == PNG
    out = res.json()
    assert out["is_dir"] is False
    assert out["size"] == len(PNG)


def test_upload_overwrites_an_existing_file(client, tmp_path):
    dest = tmp_path / "a.bin"
    dest.write_bytes(b"old and longer")
    res = client.post("/api/fs/upload", data={"path": str(dest)},
                      files={"file": ("blob", b"new", "application/octet-stream")},
                      headers={"X-Fused": "1"})
    assert res.status_code == 200, res.text
    assert dest.read_bytes() == b"new"


# ------------------------------------------------------------- the guards

def test_upload_requires_the_x_fused_header(client, tmp_path):
    dest = tmp_path / "a.bin"
    res = client.post("/api/fs/upload", data={"path": str(dest)},
                      files={"file": ("blob", PNG, "image/png")})
    assert res.status_code == 403
    assert "X-Fused" in res.json()["error"]
    assert not dest.exists()


def test_upload_rejects_a_relative_path(tmp_path):
    resp = UPLOAD("assets/x.png", PNG, x_fused="1")
    assert _status(resp) == 400
    assert "absolute" in _data(resp)["error"]


def test_upload_rejects_a_missing_parent_directory(tmp_path):
    # Same contract as _fs_write: intermediate directories are never created,
    # so the template has to mkdir assets/ itself before it uploads into it.
    dest = tmp_path / "assets" / "x.png"
    resp = UPLOAD(str(dest), PNG, x_fused="1")
    assert _status(resp) == 404
    assert "parent directory does not exist" in _data(resp)["error"]
    assert not dest.exists()


def test_upload_rejects_a_directory_target(tmp_path):
    d = tmp_path / "assets"
    d.mkdir()
    resp = UPLOAD(str(d), PNG, x_fused="1")
    assert _status(resp) == 400
    assert "is a directory" in _data(resp)["error"]


@skip_root
def test_upload_refuses_a_readonly_target(tmp_path):
    dest = tmp_path / "a.bin"
    dest.write_bytes(b"keep")
    os.chmod(dest, stat.S_IRUSR)
    try:
        resp = UPLOAD(str(dest), PNG, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
        assert dest.read_bytes() == b"keep"
    finally:
        os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)


def test_upload_refuses_a_read_only_mount(tmp_path, monkeypatch):
    """A read-only mount refuses BEFORE any kernel probe of the path.

    The order matters as much as the answer: a cold negative os.stat under a
    mount is the full-prefix enumeration the whole fs_mutate mount branch
    exists to avoid, so the refusal has to come first (_fs_write:52-56).
    """
    from fused_render.shell import mounts as shell_mounts
    dest = tmp_path / "on-a-mount.png"
    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda p: True)
    monkeypatch.setattr(shell_mounts, "mount_read_only", lambda p: True)

    def boom(p):  # any probe at all is the bug
        raise AssertionError("probed a read-only mount before refusing")

    monkeypatch.setattr("fused_render.server.fs_mutate._mount_probe", boom)
    resp = UPLOAD(str(dest), PNG, x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"
    assert not dest.exists()

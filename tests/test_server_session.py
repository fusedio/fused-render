"""Tests for GET/PUT /api/session (fused_render/server.py) — the per-file
lastSession sidecar (LSN-*).

The sidecar lives next to the TARGET file (`<file>.json`), not under
FUSED_RENDER_HOME. The route handlers are thin wrappers over module-level
_session_get / _session_put, which these drive directly — the same "avoid
starlette TestClient" discipline as test_shell_bookmark_history.py (keeps the
module importable in venvs where TestClient's httpx dependency is missing, and
sidesteps create_app's built-shell requirement).
"""
import json
import os

import pytest
from fastapi.responses import JSONResponse

from fused_render.server import _session_get as GET
from fused_render.server import _session_put as PUT
from fused_render.templates.claude import agent


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _sidecar(f):
    return json.loads((f.parent / (f.name + ".json")).read_text())


def _target(tmp_path):
    f = tmp_path / "sample.html"
    f.write_text("<html></html>")
    return f


def test_get_absent(tmp_path):
    f = _target(tmp_path)
    assert GET(path=str(f)) == {"lastSession": None}


def test_get_non_file(tmp_path):
    resp = GET(path=str(tmp_path / "missing.html"))
    assert _status(resp) == 404


def test_put_then_get_roundtrips(tmp_path):
    f = _target(tmp_path)
    assert PUT(body={"path": str(f), "search": "city=oslo&limit=50&_mode=code"},
               x_fused="1") == {"ok": True}
    r = GET(path=str(f))
    assert r["lastSession"]["search"] == "city=oslo&limit=50&_mode=code"
    assert isinstance(r["lastSession"]["updated_at"], float)


def test_put_requires_fused(tmp_path):
    f = _target(tmp_path)
    resp = PUT(body={"path": str(f), "search": "a=1"}, x_fused=None)
    assert _status(resp) == 403


def test_put_rejects_relative_path(tmp_path):
    resp = PUT(body={"path": "relative/foo.html", "search": "a=1"}, x_fused="1")
    assert _status(resp) == 400


def test_put_rejects_missing_file(tmp_path):
    resp = PUT(body={"path": str(tmp_path / "nope.html"), "search": "a=1"},
               x_fused="1")
    assert _status(resp) == 404


def test_put_rejects_non_string_search(tmp_path):
    f = _target(tmp_path)
    resp = PUT(body={"path": str(f), "search": 42}, x_fused="1")
    assert _status(resp) == 400


def test_coexists_with_sessions(tmp_path):
    f = _target(tmp_path)
    (tmp_path / "sample.html.json").write_text(
        json.dumps({"claudeSessions": [{"id": "x"}]}))
    PUT(body={"path": str(f), "search": "a=1"}, x_fused="1")
    data = _sidecar(f)
    assert data["claudeSessions"] == [{"id": "x"}]
    assert data["lastSession"]["search"] == "a=1"


def test_reverse_coexistence_record_session_preserves_last_session(tmp_path):
    # Regression for the §6 loader fix: a claude turn on a file that only has a
    # lastSession must not clobber it off disk.
    f = _target(tmp_path)
    PUT(body={"path": str(f), "search": "a=1"}, x_fused="1")
    agent._record_session(str(f), "sess-1", "hello", "")
    data = _sidecar(f)
    assert data["lastSession"]["search"] == "a=1"
    assert [e["id"] for e in data["claudeSessions"]] == ["sess-1"]


def test_put_overwrites(tmp_path):
    f = _target(tmp_path)
    PUT(body={"path": str(f), "search": "a=1"}, x_fused="1")
    PUT(body={"path": str(f), "search": "a=2"}, x_fused="1")
    assert GET(path=str(f))["lastSession"]["search"] == "a=2"


# --- LSN-3 _mode gate (server-side authority) -------------------------------


def test_mode_only_does_not_start_session(tmp_path):
    # _mode alone must not CREATE a lastSession.
    f = _target(tmp_path)
    r = PUT(body={"path": str(f), "search": "_mode=code"}, x_fused="1")
    assert r == {"ok": True, "skipped": True}
    assert GET(path=str(f)) == {"lastSession": None}


def test_empty_query_does_not_start_session(tmp_path):
    f = _target(tmp_path)
    assert PUT(body={"path": str(f), "search": ""}, x_fused="1")["skipped"] is True
    assert GET(path=str(f)) == {"lastSession": None}


def test_mode_only_updates_existing_session(tmp_path):
    # Once a session exists (started by a qualifying param), a later _mode-only
    # query IS recorded so the file's last _mode is remembered.
    f = _target(tmp_path)
    PUT(body={"path": str(f), "search": "city=oslo"}, x_fused="1")
    r = PUT(body={"path": str(f), "search": "_mode=map"}, x_fused="1")
    assert r == {"ok": True}
    assert GET(path=str(f))["lastSession"]["search"] == "_mode=map"


def test_empty_query_does_not_clobber_existing_session(tmp_path):
    f = _target(tmp_path)
    PUT(body={"path": str(f), "search": "city=oslo"}, x_fused="1")
    assert PUT(body={"path": str(f), "search": ""}, x_fused="1")["skipped"] is True
    assert GET(path=str(f))["lastSession"]["search"] == "city=oslo"


# ------------------------------------------------- read-only remote mounts
# A file browsed inside a read-only S3 mount must not get a lastSession
# sidecar: os.access(W_OK) lies under CacheMode=full (the write lands in the
# VFS cache and only 403s at the async upload — the sidecar-write incident),
# so _session_put must consult the mount's read_only flag and skip the write
# entirely rather than loop the doomed PutObject.

@pytest.fixture
def ro_mount(tmp_path, monkeypatch):
    """A real file under a fake read-only mountpoint inside a redirected
    FUSED_RENDER_HOME. Returns the absolute file path."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    import fused_render.shell.mounts as mounts

    m = mounts.add_mount("pub", "pub-remote:bucket", read_only=True)
    mp = mounts.mountpoint(m)
    os.makedirs(mp)
    f = os.path.join(mp, "cog.tif")
    with open(f, "w") as fh:
        fh.write("x")
    return f


def test_put_skips_under_read_only_mount(ro_mount):
    # A qualifying (non-_mode) query would normally start a session.
    resp = PUT(body={"path": ro_mount, "search": "_mode=geotiff&stretch=2,1471"},
               x_fused="1")
    assert resp == {"ok": True, "skipped": True}
    # No sidecar written next to the mounted file.
    assert not os.path.exists(ro_mount + ".json")
    # And GET reports no session for it.
    assert GET(path=ro_mount)["lastSession"] is None

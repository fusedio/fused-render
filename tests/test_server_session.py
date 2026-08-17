"""Tests for GET/PUT /api/session (fused_render/server.py) — the per-file
lastSession sidecar (LSN-*).

The sidecar now lives under home_dir()/sidecar/<mapped path>.json (D83-
reversal), never next to the TARGET file — see shell/storage.py's
sidecar_path. FUSED_RENDER_HOME is pinned to an isolated tmp dir for every
test here so a real sidecar under the developer's actual ~/.fused-render is
never touched. The route handlers are thin wrappers over module-level
_session_get / _session_put, which these drive directly — the same "avoid
starlette TestClient" discipline as test_shell_bookmark_history.py (keeps the
module importable in venvs where TestClient's httpx dependency is missing, and
sidesteps create_app's built-shell requirement).
"""
import json
import os

import pytest
from fastapi.responses import JSONResponse

from fused_render.server.session import _session_get as GET
from fused_render.server.session import _session_put as PUT
from fused_render.shell import storage
from fused_render.templates.claude import agent


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _sidecar(f):
    return json.loads(open(storage.sidecar_path(str(f)), encoding="utf-8").read())


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
    sidecar_path = storage.sidecar_path(str(f))
    os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump({"claudeSessions": [{"id": "x"}]}, fh)
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


# --- LSN-12: `_side` never round-trips through a sidecar (D323) --------------
# The file preview's companion sidebar is session-only by policy: it opens at its
# default on every page load, and a refresh is the way back from any change. A
# sidecar that recorded `_side` broke exactly that — the refresh is when the
# sidecar is replayed — so one file remembered a sidebar forever while its
# neighbour never had one. Stripped on WRITE and ignored on READ, so the sidecars
# already on disk self-heal on the next write and are inert before it.


def test_side_alone_does_not_start_a_session(tmp_path):
    # `_side` is not a qualifying param: opening the sidebar on a file must not be
    # the thing that starts that file's session.
    f = _target(tmp_path)
    r = PUT(body={"path": str(f), "search": "_side=claude"}, x_fused="1")
    assert r == {"ok": True, "skipped": True}
    assert GET(path=str(f)) == {"lastSession": None}
    # ...nor with `_mode`, the other non-qualifying one, for company.
    assert PUT(body={"path": str(f), "search": "_side=off&_mode=code"},
               x_fused="1")["skipped"] is True
    assert GET(path=str(f)) == {"lastSession": None}


def test_side_is_stripped_from_a_stored_query(tmp_path):
    f = _target(tmp_path)
    PUT(body={"path": str(f), "search": "city=oslo&_side=git&limit=50"}, x_fused="1")
    assert _sidecar(f)["lastSession"]["search"] == "city=oslo&limit=50"
    assert GET(path=str(f))["lastSession"]["search"] == "city=oslo&limit=50"


def test_side_only_update_does_not_clobber_an_existing_session(tmp_path):
    # Stripping happens BEFORE the LSN-3 gate, so a `_side`-only query is an empty
    # one: it must not overwrite a real session down to "".
    f = _target(tmp_path)
    PUT(body={"path": str(f), "search": "city=oslo"}, x_fused="1")
    assert PUT(body={"path": str(f), "search": "_side=git"},
               x_fused="1")["skipped"] is True
    assert GET(path=str(f))["lastSession"]["search"] == "city=oslo"


def test_an_old_side_only_sidecar_reads_as_no_session(tmp_path):
    # The self-healing half, and the shape that matters most: a sidecar written
    # before this rule whose ONLY param was `_side` must read as NO session, not as
    # an empty-string one — an empty session would still be a `lastSession` dict,
    # which is what LSN-3 keys "a session already exists" off.
    f = _target(tmp_path)
    sidecar_path = storage.sidecar_path(str(f))
    os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump({"lastSession": {"search": "_side=claude", "updated_at": 1.0}}, fh)
    assert GET(path=str(f)) == {"lastSession": None}
    # ...and because it reads as no session, a later `_mode`-only query still does
    # not start one (LSN-3), rather than being let through by a session that only
    # ever held a sidebar.
    assert PUT(body={"path": str(f), "search": "_mode=code"},
               x_fused="1")["skipped"] is True


def test_an_old_mixed_sidecar_replays_everything_but_side(tmp_path):
    f = _target(tmp_path)
    sidecar_path = storage.sidecar_path(str(f))
    os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
    with open(sidecar_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"lastSession": {"search": "_side=git&city=oslo", "updated_at": 1.0}}, fh
        )
    assert GET(path=str(f))["lastSession"]["search"] == "city=oslo"


def test_stripping_leaves_other_params_verbatim(tmp_path):
    # LSN-2: the stored query is the shell's query string verbatim. The strip must
    # not re-encode what it keeps.
    f = _target(tmp_path)
    PUT(body={"path": str(f), "search": "q=a+b%2Cc&stretch=2,1471&_side=git"},
        x_fused="1")
    assert GET(path=str(f))["lastSession"]["search"] == "q=a+b%2Cc&stretch=2,1471"


def test_stripping_is_not_fooled_by_a_similar_name(tmp_path):
    f = _target(tmp_path)
    PUT(body={"path": str(f), "search": "_sidebar=1&x_side=2"}, x_fused="1")
    assert GET(path=str(f))["lastSession"]["search"] == "_sidebar=1&x_side=2"


# --------------------------------------------------- read-only remote mounts
# D83-reversal: the sidecar now lives under home_dir()/sidecar/, never on the
# mounted file's own filesystem, so a read-only remote mount no longer has any
# bearing on whether a lastSession sidecar can be written — the old
# sidecar-write incident (CacheMode=full 403-looping a doomed PutObject)
# structurally can't happen anymore. This used to be a skip case; now it's a
# plain success case.

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


def test_put_succeeds_under_read_only_mount(ro_mount):
    # A qualifying (non-_mode) query starts a session even under a read-only
    # mount, since the sidecar write never touches the mount.
    resp = PUT(body={"path": ro_mount, "search": "_mode=geotiff&stretch=2,1471"},
               x_fused="1")
    assert resp == {"ok": True}
    assert GET(path=ro_mount)["lastSession"]["search"] == "_mode=geotiff&stretch=2,1471"


# --- mount-safe existence gate (_is_file_mount_safe) ----------------------
# GET/PUT gate on _is_file_mount_safe. On a mount-backed path it MUST answer
# via the rclone rc API (rc_kind_for), NEVER a kernel os.path.isfile — a cold
# GETATTR there enumerates the whole parent S3 prefix and wedges the mount.
# The gate is files-only, matching os.path.isfile: "file" passes, "dir" and
# "missing" 404, and an "indeterminate" rc probe fails OPEN (never 404s a file
# the user just opened on a transient rcd hiccup).

@pytest.fixture
def mount_gate(monkeypatch):
    """Force every path mount-backed, make any kernel os.path.isfile on it fail
    loudly, and let each test dictate rc_kind_for's answer. Returns a setter."""
    import fused_render.shell.mounts as mounts

    monkeypatch.setattr(mounts, "is_mount_backed", lambda p: True)

    def _no_isfile(path):
        raise AssertionError(f"kernel os.path.isfile({path}) touched the mount")

    monkeypatch.setattr(os.path, "isfile", _no_isfile)

    def _set(kind):
        monkeypatch.setattr(mounts, "rc_kind_for", lambda path, **kw: kind)

    return _set


@pytest.mark.parametrize("kind,ok", [
    ("file", True),           # a real file -> gate passes
    ("indeterminate", True),  # rcd down/timeout -> fail open, gate passes
    ("dir", False),           # a directory is not a file -> 404
    ("missing", False),       # confirmed absent -> 404
])
def test_get_gate_is_files_only_via_rc(mount_gate, kind, ok):
    mount_gate(kind)
    resp = GET(path="/mnt/pub/big-prefix/cog.tif")
    if ok:
        assert _status(resp) == 200
        assert resp == {"lastSession": None}
    else:
        assert _status(resp) == 404


def test_put_gate_rejects_mount_directory_via_rc(mount_gate):
    # A mount-backed DIRECTORY must 404 at the existence gate before any write,
    # answered by rc_kind_for — regression for the gate accepting dirs.
    mount_gate("dir")
    resp = PUT(body={"path": "/mnt/pub/big-prefix", "search": "a=1"}, x_fused="1")
    assert _status(resp) == 404

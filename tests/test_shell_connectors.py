"""Tests for the Connectors backend (shell/connectors.py): the persisted
connector store, the rclone rcd client (against a stub rc server — real
rclone is never invoked), and the /api/connectors endpoints.

FUSED_RENDER_HOME is redirected per test so no test touches the real
~/.fused-render or a real mount.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fused_render.shell.connectors as conn_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


class StubRcd:
    """Minimal rclone rcd stand-in: answers the rc methods the module uses
    and records every call. Responses are per-method canned JSON that a test
    may override; mount/unmount default to success."""

    def __init__(self):
        self.calls = []
        self.responses = {
            "core/pid": {"pid": 4242},
            "mount/mount": {},
            "mount/unmount": {},
            "mount/listmounts": {"mountPoints": []},
        }
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                method = self.path.lstrip("/")
                stub.calls.append((method, body))
                resp = stub.responses.get(method)
                if resp is None:
                    payload, code = {"error": f"unknown method {method}"}, 404
                elif isinstance(resp, tuple):
                    code, payload = resp
                else:
                    payload, code = resp, 200
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def rcd(home):
    """A live stub rcd whose port is recorded in the state file, so
    _ensure_rcd reuses it instead of spawning real rclone."""
    stub = StubRcd()
    conn_mod.write_rcd_state(stub.port, 4242)
    yield stub
    stub.close()


# -- store ---------------------------------------------------------------------


def test_store_roundtrip(home):
    assert conn_mod.list_connectors() == []
    c = conn_mod.add_connector("data", "remote:bucket/prefix")
    assert c["name"] == "data" and c["remote"] == "remote:bucket/prefix"
    assert c["automount"] is False
    [stored] = conn_mod.list_connectors()
    assert stored["id"] == c["id"]
    conn_mod.remove_connector(c["id"])
    assert conn_mod.list_connectors() == []


def test_add_rejects_bad_names_and_remotes(home):
    for bad in ("", "a/b", "a\\b", "a:b", ".hidden"):
        with pytest.raises(ValueError):
            conn_mod.add_connector(bad, "remote:x")
    with pytest.raises(ValueError):
        conn_mod.add_connector("ok", "no-colon-spec")


def test_add_rejects_duplicates(home):
    conn_mod.add_connector("data", "remote:bucket")
    with pytest.raises(ValueError):
        conn_mod.add_connector("data", "remote:other")
    with pytest.raises(ValueError):
        conn_mod.add_connector("other", "remote:bucket")


def test_mountpoint_derives_from_branch_aware_home(home):
    c = conn_mod.add_connector("data", "remote:bucket")
    mp = conn_mod.mountpoint(c)
    assert mp.startswith(str(home))
    assert mp.endswith("/mounts/data")


def test_set_automount(home):
    c = conn_mod.add_connector("data", "remote:bucket")
    conn_mod.set_automount(c["id"], True)
    [stored] = conn_mod.list_connectors()
    assert stored["automount"] is True


# -- rcd client ----------------------------------------------------------------


def test_ensure_rcd_reuses_live_daemon(home, rcd):
    port = conn_mod.ensure_rcd()
    assert port == rcd.port
    assert ("core/pid", {}) in rcd.calls


def test_mount_calls_rc_with_vfs_options(home, rcd):
    c = conn_mod.add_connector("data", "remote:bucket/prefix")
    err = conn_mod.mount_connector(c)
    assert err is None
    [(method, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert body["fs"] == "remote:bucket/prefix"
    assert body["mountPoint"] == conn_mod.mountpoint(c)
    assert body["vfsOpt"]["CacheMode"] == "full"
    assert body["vfsOpt"]["CacheMaxAge"] == "24h"


def test_mount_surfaces_rc_error(home, rcd):
    rcd.responses["mount/mount"] = (500, {"error": "mount helper failed"})
    c = conn_mod.add_connector("data", "remote:bucket")
    err = conn_mod.mount_connector(c)
    assert err is not None and "mount helper failed" in err


def test_unmount_calls_rc(home, rcd):
    c = conn_mod.add_connector("data", "remote:bucket")
    assert conn_mod.unmount_connector(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/unmount"]
    assert body["mountPoint"] == conn_mod.mountpoint(c)


def test_mounted_paths_merges_listmounts(home, rcd):
    c = conn_mod.add_connector("data", "remote:bucket")
    mp = conn_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {"mountPoints": [{"Fs": c["remote"], "MountPoint": mp}]}
    assert mp in conn_mod.mounted_paths()
    view = conn_mod.connector_view(c)
    assert view["mounted"] is True and view["mountpoint"] == mp


def test_mounted_paths_empty_when_rcd_down(home):
    # No state file, no daemon: status degrades to unmounted, never raises.
    # (ensure_rcd would spawn; mounted_paths must NOT spawn just to read.)
    assert conn_mod.mounted_paths() == set()

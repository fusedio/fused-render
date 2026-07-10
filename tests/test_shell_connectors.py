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
                if isinstance(resp, list):  # per-call sequence; last repeats
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
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


def test_mount_rejects_mountpoint_serving_other_remote(home, rcd, monkeypatch):
    # A stale mount from a deleted same-name connector must not pass for the
    # new remote: rcd lists the old fs at the mountpoint -> mount errors.
    c = conn_mod.add_connector("data", "remote:new")
    mp = conn_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:old", "MountPoint": mp}]}
    monkeypatch.setattr(conn_mod.os.path, "ismount", lambda p: p == mp)
    err = conn_mod.mount_connector(c)
    assert err is not None and "remote:old" in err
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_mount_adopts_matching_existing_mount(home, rcd, monkeypatch):
    c = conn_mod.add_connector("data", "remote:bucket")
    mp = conn_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:bucket", "MountPoint": mp}]}
    monkeypatch.setattr(conn_mod.os.path, "ismount", lambda p: p == mp)
    assert conn_mod.mount_connector(c) is None
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_mounted_paths_empty_when_rcd_down(home):
    # No state file, no daemon: status degrades to unmounted, never raises.
    # (ensure_rcd would spawn; mounted_paths must NOT spawn just to read.)
    assert conn_mod.mounted_paths() == set()


# -- endpoints -------------------------------------------------------------------

FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


@pytest.fixture()
def client(home):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app

    return TestClient(create_app(start_dir=str(home)))


def test_get_shape(client, rcd):
    data = client.get("/api/connectors").json()
    assert data["connectors"] == []
    assert set(data["rclone"]) == {"available", "version", "remotes"}


def test_writes_require_fused_header(client):
    assert client.post("/api/connectors", json={"name": "x", "remote": "r:"}).status_code == 403
    assert client.delete("/api/connectors/nope").status_code == 403


def test_create_validates_before_mounting(client, rcd):
    r = client.post("/api/connectors", json={"name": "a/b", "remote": "r:"}, headers=FUSED)
    assert r.status_code == 400
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_create_mounts_and_persists(client, rcd):
    r = client.post(
        "/api/connectors", json={"name": "data", "remote": "r:bucket"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["name"] == "data"
    assert any(m == "mount/mount" for m, _ in rcd.calls)
    assert len(client.get("/api/connectors").json()["connectors"]) == 1


def test_create_rolls_back_store_on_mount_failure(client, rcd):
    rcd.responses["mount/mount"] = (500, {"error": "boom"})
    r = client.post(
        "/api/connectors", json={"name": "data", "remote": "r:bucket"}, headers=FUSED)
    assert r.status_code == 502
    assert client.get("/api/connectors").json()["connectors"] == []


def test_mount_unmount_delete_unknown_id(client, rcd):
    assert client.post("/api/connectors/nope/mount", headers=FUSED).status_code == 404
    assert client.post("/api/connectors/nope/unmount", headers=FUSED).status_code == 404
    assert client.delete("/api/connectors/nope", headers=FUSED).status_code == 404


def test_automount_toggle_endpoint(client, rcd):
    cid = client.post(
        "/api/connectors", json={"name": "data", "remote": "r:bucket"},
        headers=FUSED).json()["id"]
    r = client.put(f"/api/connectors/{cid}", json={"automount": True}, headers=FUSED)
    assert r.status_code == 200 and r.json()["automount"] is True
    r = client.put(f"/api/connectors/{cid}", json={"automount": "yes"}, headers=FUSED)
    assert r.status_code == 400


def test_delete_unmounts_and_removes(client, rcd):
    cid = client.post(
        "/api/connectors", json={"name": "data", "remote": "r:bucket"},
        headers=FUSED).json()["id"]
    assert client.delete(f"/api/connectors/{cid}", headers=FUSED).status_code == 200
    assert client.get("/api/connectors").json()["connectors"] == []
    assert any(m == "mount/unmount" for m, _ in rcd.calls)


def test_create_s3_remote_builds_rclone_argv(client, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(conn_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(conn_mod, "rclone_bin", lambda: "/usr/bin/rclone")
    r = client.post("/api/connectors/remotes", json={
        "name": "mys3",
        "params": {"access_key_id": "AK", "secret_access_key": "SK",
                   "endpoint": "https://e.example", "region": "us-east-1"},
    }, headers=FUSED)
    assert r.status_code == 200
    cmd = seen["cmd"]
    assert cmd[:4] == ["/usr/bin/rclone", "config", "create", "mys3"]
    assert "s3" in cmd and "AK" in cmd and "https://e.example" in cmd


def test_create_remote_rejects_bad_name(client):
    r = client.post("/api/connectors/remotes", json={"name": "a:b"}, headers=FUSED)
    assert r.status_code == 400


# -- unmount-busy: release tile daemons, retry once -------------------------------


@pytest.fixture()
def tile_daemon(tmp_path, monkeypatch):
    """A stub tile-server daemon (records /quit) plus a state file pointing at
    it, wired in as one of the module's DAEMON_STATE_FILES."""
    quits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            quits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"bye")

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = tmp_path / "daemon.json"
    state.write_text(json.dumps({"port": server.server_address[1], "pid": 1}))
    missing = tmp_path / "absent" / "daemon.json"  # the parallel file, absent
    monkeypatch.setattr(conn_mod, "DAEMON_STATE_FILES", (str(state), str(missing)))
    yield quits
    server.shutdown()


def test_unmount_busy_quits_daemons_and_retries(home, rcd, tile_daemon, monkeypatch):
    monkeypatch.setattr(conn_mod.time, "sleep", lambda s: None)
    c = conn_mod.add_connector("data", "remote:bucket")
    rcd.responses["mount/unmount"] = [(500, {"error": "device busy"}), {}]
    assert conn_mod.unmount_connector(c) is None
    assert tile_daemon == ["/quit"]
    assert sum(1 for m, _ in rcd.calls if m == "mount/unmount") == 2


def test_unmount_success_never_touches_daemons(home, rcd, tile_daemon):
    c = conn_mod.add_connector("data", "remote:bucket")
    assert conn_mod.unmount_connector(c) is None
    assert tile_daemon == []


def test_unmount_non_busy_error_skips_daemons(home, rcd, tile_daemon):
    # Only a busy error means a daemon holds a file open; on any other
    # failure quitting tile servers would kill unrelated local previews.
    c = conn_mod.add_connector("data", "remote:bucket")
    rcd.responses["mount/unmount"] = (500, {"error": "some rc failure"})
    err = conn_mod.unmount_connector(c)
    assert err is not None and "some rc failure" in err
    assert tile_daemon == []
    assert sum(1 for m, _ in rcd.calls if m == "mount/unmount") == 1


def test_unmount_still_busy_after_release_reports_error(home, rcd, tile_daemon, monkeypatch):
    monkeypatch.setattr(conn_mod.time, "sleep", lambda s: None)
    c = conn_mod.add_connector("data", "remote:bucket")
    rcd.responses["mount/unmount"] = (500, {"error": "device busy"})
    err = conn_mod.unmount_connector(c)
    assert err is not None and "hold a file open" in err
    assert tile_daemon == ["/quit"]


def test_delete_blocked_while_still_mounted(client, rcd, tile_daemon, monkeypatch):
    monkeypatch.setattr(conn_mod.time, "sleep", lambda s: None)
    cid = client.post(
        "/api/connectors", json={"name": "data", "remote": "r:bucket"},
        headers=FUSED).json()["id"]
    mp = conn_mod.mountpoint(conn_mod.get_connector(cid))
    rcd.responses["mount/unmount"] = (500, {"error": "device busy"})
    monkeypatch.setattr(conn_mod.os.path, "ismount", lambda p: p == mp)
    r = client.delete(f"/api/connectors/{cid}", headers=FUSED)
    assert r.status_code == 502 and "not deleted" in r.json()["error"]
    assert len(client.get("/api/connectors").json()["connectors"]) == 1


# -- automount at startup --------------------------------------------------------


def test_run_automount_mounts_flagged_connectors(home, rcd):
    a = conn_mod.add_connector("auto", "r:one", automount=True)
    conn_mod.add_connector("manual", "r:two", automount=False)
    conn_mod.run_automount()
    mounted = [b["fs"] for m, b in rcd.calls if m == "mount/mount"]
    assert mounted == ["r:one"]
    assert conn_mod.mountpoint(a).endswith("/mounts/auto")


def test_run_automount_skips_already_mounted(home, rcd):
    c = conn_mod.add_connector("auto", "r:one", automount=True)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "r:one", "MountPoint": conn_mod.mountpoint(c)}]}
    conn_mod.run_automount()
    assert not any(m == "mount/mount" for m, _ in rcd.calls)

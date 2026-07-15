"""Tests for the Mounts backend (shell/mounts.py): the persisted
mount store, the rclone rcd client (against a stub rc server — real
rclone is never invoked), and the /api/mounts endpoints.

FUSED_RENDER_HOME is redirected per test so no test touches the real
~/.fused-render or a real mount.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fused_render.shell.mounts as mounts_mod


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
            "serve/list": {"list": []},
            "serve/start": {"addr": "127.0.0.1:59999", "id": "http-stub"},
            "serve/stop": {},
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
    mounts_mod.write_rcd_state(stub.port, 4242)
    yield stub
    stub.close()


# -- rclone_bin ------------------------------------------------------------


def test_rclone_bin_prefers_bundled_when_packaged(tmp_path, monkeypatch):
    contents = tmp_path / "FusedRender.app" / "Contents"
    bundled = contents / "Resources" / "bin" / "rclone"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "executable", str(contents / "MacOS" / "python"))
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/should/not/be/used")
    assert mounts_mod.rclone_bin() == str(bundled)


def test_rclone_bin_falls_back_when_packaged_bundle_missing(tmp_path, monkeypatch):
    contents = tmp_path / "FusedRender.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "executable", str(contents / "MacOS" / "python"))
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/local/bin/rclone")
    assert mounts_mod.rclone_bin() == "/usr/local/bin/rclone"


def test_rclone_bin_uses_path_when_unpackaged(monkeypatch):
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/local/bin/rclone")
    assert mounts_mod.rclone_bin() == "/usr/local/bin/rclone"


# -- store ---------------------------------------------------------------------


def test_store_roundtrip(home):
    assert mounts_mod.list_mounts() == []
    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    assert c["name"] == "data" and c["remote"] == "remote:bucket/prefix"
    [stored] = mounts_mod.list_mounts()
    assert stored["id"] == c["id"]
    mounts_mod.remove_mount(c["id"])
    assert mounts_mod.list_mounts() == []


def test_add_rejects_bad_names_and_remotes(home):
    for bad in ("", "a/b", "a\\b", "a:b", ".hidden"):
        with pytest.raises(ValueError):
            mounts_mod.add_mount(bad, "remote:x")
    with pytest.raises(ValueError):
        mounts_mod.add_mount("ok", "no-colon-spec")


def test_add_rejects_duplicates(home):
    mounts_mod.add_mount("data", "remote:bucket")
    with pytest.raises(ValueError):
        mounts_mod.add_mount("data", "remote:other")
    with pytest.raises(ValueError):
        mounts_mod.add_mount("other", "remote:bucket")


def test_mountpoint_derives_from_branch_aware_home(home):
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    assert mp.startswith(str(home))
    assert mp.endswith("/mounts/data")



# -- rcd client ----------------------------------------------------------------


def test_ensure_rcd_reuses_live_daemon(home, rcd):
    port = mounts_mod.ensure_rcd()
    assert port == rcd.port
    assert ("core/pid", {}) in rcd.calls


def test_mount_calls_rc_with_vfs_options(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    err = mounts_mod.attach_mount(c)
    assert err is None
    [(method, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert body["fs"] == "remote:bucket/prefix"
    assert body["mountPoint"] == mounts_mod.mountpoint(c)
    assert body["vfsOpt"]["CacheMode"] == "full"
    assert body["vfsOpt"]["CacheMaxAge"] == "24h"
    # The mount carries the same on-disk cache cap as the serve — both must
    # hold every vfs option or rcd splits them into two VFS instances.
    assert body["vfsOpt"]["CacheMaxSize"] == "20Gi"


def test_serve_vfs_opt_derived_from_mount_vfs_opt():
    # The mount (vfsOpt object) and the serve (flat vfs_* params) must describe
    # the SAME option set or rcd builds two independent VFS instances that don't
    # share cached ranges — the whole reason this bug existed. SERVE_VFS_OPT is
    # DERIVED from VFS_OPT to guarantee it; assert every key maps and the values
    # agree (bools -> "true"/"false", ints -> str, as serve/list echoes them).
    assert set(mounts_mod._VFS_OPT_TO_SERVE_PARAM) == set(mounts_mod.VFS_OPT)
    for obj_key, flat_key in mounts_mod._VFS_OPT_TO_SERVE_PARAM.items():
        v = mounts_mod.VFS_OPT[obj_key]
        expected = ("true" if v else "false") if isinstance(v, bool) else str(v)
        assert mounts_mod.SERVE_VFS_OPT[flat_key] == expected


@pytest.mark.skipif(mounts_mod.sys.platform != "darwin",
                    reason="nfsmount timeo override is macOS-only")
def test_mount_raises_nfs_timeout_on_macos(home, rcd):
    # The loopback NFS client's low default timeout drops the whole mount on a
    # slow chunk fetch; attach must pass a raised timeo so local-path reads are
    # slow, not fatal. mountOpt is NFS transport, not a vfs option, so it does
    # not affect VFS sharing with the serve.
    c = mounts_mod.add_mount("data", "remote:bucket")
    assert mounts_mod.attach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/mount"]
    assert body["mountType"] == "nfsmount"
    assert body["mountOpt"]["ExtraOptions"] == mounts_mod.NFS_MOUNT_OPT["ExtraOptions"]
    assert any(o.startswith("timeo=") for o in body["mountOpt"]["ExtraOptions"])


def test_mount_surfaces_rc_error(home, rcd):
    rcd.responses["mount/mount"] = (500, {"error": "mount helper failed"})
    c = mounts_mod.add_mount("data", "remote:bucket")
    err = mounts_mod.attach_mount(c)
    assert err is not None and "mount helper failed" in err


def test_unmount_calls_rc(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket")
    assert mounts_mod.detach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "mount/unmount"]
    assert body["mountPoint"] == mounts_mod.mountpoint(c)


def test_mounted_paths_merges_listmounts(home, rcd, monkeypatch):
    import os

    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    rcd.responses["mount/listmounts"] = {"mountPoints": [{"Fs": c["remote"], "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    assert mp in mounts_mod.mounted_paths()
    view = mounts_mod.mount_view(c)
    assert view["mounted"] is True and view["mountpoint"] == mp
    assert view["state"] == "mounted"


def test_mount_rejects_mountpoint_serving_other_remote(home, rcd, monkeypatch):
    # A stale mount from a deleted same-name mount must not pass for the
    # new remote: rcd lists the old fs at the mountpoint -> mount errors.
    c = mounts_mod.add_mount("data", "remote:new")
    mp = mounts_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:old", "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    err = mounts_mod.attach_mount(c)
    assert err is not None and "remote:old" in err
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_mount_adopts_matching_existing_mount(home, rcd, monkeypatch):
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "remote:bucket", "MountPoint": mp}]}
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    assert mounts_mod.attach_mount(c) is None
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_mounted_paths_empty_when_rcd_down(home):
    # No state file, no daemon: status degrades to unmounted, never raises.
    # (ensure_rcd would spawn; mounted_paths must NOT spawn just to read.)
    assert mounts_mod.mounted_paths() == set()


# -- endpoints -------------------------------------------------------------------

FUSED = {"X-Fused": "1"}  # D3 guard header required on writes


@pytest.fixture()
def client(home):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app

    return TestClient(create_app(start_dir=str(home)))


def test_get_shape(client, rcd):
    data = client.get("/api/mounts").json()
    assert data["mounts"] == []
    assert set(data["rclone"]) == {"available", "version", "remotes", "suggested"}


def test_writes_require_fused_header(client):
    assert client.post("/api/mounts", json={"name": "x", "remote": "r:"}).status_code == 403
    assert client.delete("/api/mounts/nope").status_code == 403


def test_create_validates_before_mounting(client, rcd):
    r = client.post("/api/mounts", json={"name": "a/b", "remote": "r:"}, headers=FUSED)
    assert r.status_code == 400
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_create_mounts_and_persists(client, rcd):
    r = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["name"] == "data"
    assert any(m == "mount/mount" for m, _ in rcd.calls)
    assert len(client.get("/api/mounts").json()["mounts"]) == 1


def test_create_rolls_back_store_on_mount_failure(client, rcd):
    rcd.responses["mount/mount"] = (500, {"error": "boom"})
    r = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"}, headers=FUSED)
    assert r.status_code == 502
    assert client.get("/api/mounts").json()["mounts"] == []


def test_mount_unmount_delete_unknown_id(client, rcd):
    assert client.post("/api/mounts/nope/mount", headers=FUSED).status_code == 404
    assert client.post("/api/mounts/nope/unmount", headers=FUSED).status_code == 404
    assert client.delete("/api/mounts/nope", headers=FUSED).status_code == 404


def test_mount_view_has_no_automount_field(client, rcd):
    m = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"},
        headers=FUSED).json()
    # automount is implicit for every mount now — the field is gone.
    assert "automount" not in m
    assert set(m) == {"id", "name", "remote", "mountpoint", "mounted", "state",
                      "read_only"}


def test_delete_unmounts_and_removes(client, rcd):
    cid = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"},
        headers=FUSED).json()["id"]
    assert client.delete(f"/api/mounts/{cid}", headers=FUSED).status_code == 200
    assert client.get("/api/mounts").json()["mounts"] == []
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

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")
    r = client.post("/api/mounts/remotes", json={
        "name": "mys3",
        "params": {"access_key_id": "AK", "secret_access_key": "SK",
                   "endpoint": "https://e.example", "region": "us-east-1"},
    }, headers=FUSED)
    assert r.status_code == 200
    cmd = seen["cmd"]
    assert cmd[:4] == ["/usr/bin/rclone", "config", "create", "mys3"]
    assert "s3" in cmd and "AK" in cmd and "https://e.example" in cmd


def test_create_remote_rejects_bad_name(client):
    r = client.post("/api/mounts/remotes", json={"name": "a:b"}, headers=FUSED)
    assert r.status_code == 400


# -- credential auto-detection (keyless env_auth remotes) ------------------------


def test_aws_profiles_and_suggestions_from_dotfiles(tmp_path, monkeypatch):
    (tmp_path / "credentials").write_text(
        "[default]\naws_access_key_id = AK\n\n[work]\naws_access_key_id = WK\n")
    (tmp_path / "config").write_text(
        "[default]\nregion = us-east-1\n\n[profile prod]\nregion = eu-west-1\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    assert mounts_mod._aws_profiles() == ["default", "prod", "work"]
    by_id = {s["id"]: s for s in mounts_mod._credential_suggestions()}
    # "[profile prod]" in config is unwrapped; default keeps the bare "aws" name
    assert by_id["aws-profile:default"]["remote_name"] == "aws"
    assert by_id["aws-profile:work"]["remote_name"] == "aws-work"
    assert by_id["aws-profile:prod"]["params"] == {
        "provider": "AWS", "env_auth": "true", "profile": "prod"}


def test_suggestions_view_hides_already_materialized(monkeypatch):
    monkeypatch.setattr(mounts_mod, "_credential_suggestions", lambda: [
        {"id": "aws-env", "label": "L", "remote_name": "aws-env",
         "backend": "s3", "params": {"provider": "AWS", "env_auth": "true"}},
    ])
    # kind defaults to "detected" for entries that don't set it.
    assert mounts_mod._suggestions_view([]) == [
        {"id": "aws-env", "label": "L", "remote_name": "aws-env",
         "kind": "detected"}]
    assert mounts_mod._suggestions_view(["aws-env:"]) == []


def test_public_bucket_suggestion_always_present(monkeypatch):
    """The anonymous public-bucket remote is offered even with no AWS/gcloud
    credentials at all — it's what lets a user mount open data without keys."""
    monkeypatch.setattr(mounts_mod, "_aws_profiles", lambda: [])
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setattr(mounts_mod.os.path, "exists", lambda p: False)

    by_id = {s["id"]: s for s in mounts_mod._credential_suggestions()}
    pub = by_id["aws-open-public"]
    assert pub["remote_name"] == "aws-open"
    assert pub["kind"] == "public"
    # anonymous: env_auth off, no key material anywhere in the spec
    assert pub["params"]["env_auth"] == "false"
    assert not any("secret" in k or "key" in k for k in pub["params"])


def test_public_suggestion_hidden_once_materialized(monkeypatch):
    monkeypatch.setattr(mounts_mod, "_aws_profiles", lambda: [])
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setattr(mounts_mod.os.path, "exists", lambda p: False)
    # not yet created → shown under kind="public"
    assert any(s["id"] == "aws-open-public" and s["kind"] == "public"
               for s in mounts_mod._suggestions_view([]))
    # once aws-open: exists it drops out (shows under Remotes instead)
    assert not any(s["id"] == "aws-open-public"
                   for s in mounts_mod._suggestions_view(["aws-open:"]))


def test_detect_materializes_public_anonymous_remote(client, monkeypatch):
    """Selecting the built-in public option creates an anonymous S3 remote —
    no secret material, env_auth=false — so unsigned requests reach open data."""
    created = []
    _fake_rclone(monkeypatch, existing_remotes=(), record=created)

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-open-public"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["name"] == "aws-open:"
    [cmd] = created
    assert cmd[:5] == ["/usr/bin/rclone", "config", "create", "aws-open", "s3"]
    assert "env_auth" in cmd and "false" in cmd
    assert not any("secret" in str(x).lower() for x in cmd)


def _fake_rclone(monkeypatch, existing_remotes=(), record=None):
    """Stub rclone_bin + subprocess.run: version/listremotes canned, every
    other argv appended to `record` and reported as success."""
    def fake_run(cmd, **kw):
        if record is not None and "create" in cmd:
            record.append(cmd)

        class R:
            returncode = 0
            stderr = ""
            stdout = ("rclone v1.2\n" if "version" in cmd
                      else "".join(f"{r}\n" for r in existing_remotes)
                      if "listremotes" in cmd else "")
        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: "/usr/bin/rclone")


def test_detect_materializes_keyless_remote(client, monkeypatch):
    monkeypatch.setattr(mounts_mod, "_credential_suggestions", lambda: [
        {"id": "aws-profile:work", "label": "AWS S3 — work profile",
         "remote_name": "aws-work", "backend": "s3",
         "params": {"provider": "AWS", "env_auth": "true", "profile": "work"}},
    ])
    created = []
    _fake_rclone(monkeypatch, existing_remotes=(), record=created)

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-profile:work"}, headers=FUSED)
    assert r.status_code == 200
    assert r.json()["name"] == "aws-work:"
    [cmd] = created
    assert cmd[:5] == ["/usr/bin/rclone", "config", "create", "aws-work", "s3"]
    assert "env_auth" in cmd and "true" in cmd and "work" in cmd
    # keyless — no secret material is ever passed
    assert not any("secret" in str(x).lower() for x in cmd)


def test_detect_is_idempotent_when_remote_exists(client, monkeypatch):
    monkeypatch.setattr(mounts_mod, "_credential_suggestions", lambda: [
        {"id": "aws-env", "label": "L", "remote_name": "aws-env",
         "backend": "s3", "params": {"provider": "AWS", "env_auth": "true"}},
    ])
    created = []
    _fake_rclone(monkeypatch, existing_remotes=("aws-env:",), record=created)

    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "aws-env"}, headers=FUSED)
    assert r.status_code == 200 and r.json()["name"] == "aws-env:"
    assert created == []  # already present → no create attempted


def test_detect_unknown_source_is_404(client, monkeypatch):
    monkeypatch.setattr(mounts_mod, "_credential_suggestions", lambda: [])
    _fake_rclone(monkeypatch)
    r = client.post("/api/mounts/remotes/detect",
                    json={"id": "nope"}, headers=FUSED)
    assert r.status_code == 404


def test_detect_requires_fused_header(client):
    assert client.post("/api/mounts/remotes/detect",
                       json={"id": "x"}).status_code == 403


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
    monkeypatch.setattr(mounts_mod, "DAEMON_STATE_FILES", (str(state), str(missing)))
    yield quits
    server.shutdown()


def test_unmount_busy_quits_daemons_and_retries(home, rcd, tile_daemon, monkeypatch):
    monkeypatch.setattr(mounts_mod.time, "sleep", lambda s: None)
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["mount/unmount"] = [(500, {"error": "device busy"}), {}]
    assert mounts_mod.detach_mount(c) is None
    assert tile_daemon == ["/quit"]
    assert sum(1 for m, _ in rcd.calls if m == "mount/unmount") == 2


def test_unmount_success_never_touches_daemons(home, rcd, tile_daemon):
    c = mounts_mod.add_mount("data", "remote:bucket")
    assert mounts_mod.detach_mount(c) is None
    assert tile_daemon == []


def test_unmount_non_busy_error_skips_daemons(home, rcd, tile_daemon):
    # Only a busy error means a daemon holds a file open; on any other
    # failure quitting tile servers would kill unrelated local previews.
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["mount/unmount"] = (500, {"error": "some rc failure"})
    err = mounts_mod.detach_mount(c)
    assert err is not None and "some rc failure" in err
    assert tile_daemon == []
    assert sum(1 for m, _ in rcd.calls if m == "mount/unmount") == 1


def test_unmount_still_busy_after_release_reports_error(home, rcd, tile_daemon, monkeypatch):
    monkeypatch.setattr(mounts_mod.time, "sleep", lambda s: None)
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["mount/unmount"] = (500, {"error": "device busy"})
    err = mounts_mod.detach_mount(c)
    assert err is not None and "hold a file open" in err
    assert tile_daemon == ["/quit"]


def test_delete_blocked_while_still_mounted(client, rcd, tile_daemon, monkeypatch):
    monkeypatch.setattr(mounts_mod.time, "sleep", lambda s: None)
    cid = client.post(
        "/api/mounts", json={"name": "data", "remote": "r:bucket"},
        headers=FUSED).json()["id"]
    mp = mounts_mod.mountpoint(mounts_mod.get_mount(cid))
    rcd.responses["mount/unmount"] = (500, {"error": "device busy"})
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    r = client.delete(f"/api/mounts/{cid}", headers=FUSED)
    assert r.status_code == 502 and "not deleted" in r.json()["error"]
    assert len(client.get("/api/mounts").json()["mounts"]) == 1


# -- health detection + reconnect (dead/wedged mounts) ----------------------------
#
# The failure these cover: the rclone daemon (or its NFS serve) dies while the
# kernel mount entry survives. os.path.ismount() still says True, listings
# return stale/empty data, and a plain unmount fails. The mount must report
# "disconnected" (not a green "mounted") and reconnect must force-clear the
# mountpoint before remounting.

import os as _os


def _make_mount(home, rcd, name="data", remote="remote:bucket", served=True):
    c = mounts_mod.add_mount(name, remote)
    mp = mounts_mod.mountpoint(c)
    _os.makedirs(mp, exist_ok=True)
    if served:
        rcd.responses["mount/listmounts"] = {
            "mountPoints": [{"Fs": remote, "MountPoint": mp}]}
    return c, mp


def test_state_mounted_when_served_and_listable(home, rcd, monkeypatch):
    c, mp = _make_mount(home, rcd)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    assert mounts_mod.mount_state(c, mounts_mod.mounted_paths()) == "mounted"


def test_state_disconnected_when_kernel_mount_has_no_daemon(home, rcd, monkeypatch):
    # ismount True but rcd doesn't list it: the daemon that served it is gone.
    c, mp = _make_mount(home, rcd, served=False)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    assert mounts_mod.mount_state(c, mounts_mod.mounted_paths()) == "disconnected"


def test_state_disconnected_when_rcd_tracks_a_dropped_kernel_mount(home, rcd):
    # rcd still lists the mount but the kernel mount is gone: the mountpoint
    # is a plain local dir masquerading as remote data.
    c, mp = _make_mount(home, rcd)
    assert mounts_mod.mount_state(c, mounts_mod.mounted_paths()) == "disconnected"


def test_state_disconnected_when_listing_hangs(home, rcd, monkeypatch):
    # A wedged NFS mount blocks listdir forever; the probe times out instead.
    c, mp = _make_mount(home, rcd)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)
    ev = threading.Event()

    def hang(p):
        ev.wait(5)
        return []

    monkeypatch.setattr(mounts_mod.os, "listdir", hang)
    try:
        state = mounts_mod.mount_state(c, mounts_mod.mounted_paths(), timeout=0.2)
    finally:
        ev.set()  # release the probe thread
    assert state == "disconnected"


def test_state_unmounted_when_nothing_there(home, rcd):
    c, mp = _make_mount(home, rcd, served=False)
    assert mounts_mod.mount_state(c, mounts_mod.mounted_paths()) == "unmounted"


def test_reconnect_force_unmounts_dead_mount_then_remounts(home, rcd, monkeypatch):
    # rcd's own unmount fails (the wedged-NFS case) -> force umount -> remount.
    c, mp = _make_mount(home, rcd, served=False)
    rcd.responses["mount/unmount"] = (500, {"error": "failed to umount the NFS volume"})
    still_mounted = {"v": True}
    monkeypatch.setattr(mounts_mod.os.path, "ismount",
                        lambda p: p == mp and still_mounted["v"])
    forced = []

    def fake_run(cmd, **kw):
        forced.append(cmd)
        still_mounted["v"] = False  # umount succeeded

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    assert mounts_mod.reconnect_mount(c) is None
    assert forced and forced[0][:1] == ["umount"]
    assert any(m == "mount/mount" for m, _ in rcd.calls)


def test_reconnect_stops_serve_so_it_rebinds_to_fresh_vfs(home, rcd):
    # rcd shares one VFS between a mount and its serve; unmounting tears that
    # VFS down and leaves the serve wedged on uncached reads (verified against
    # real rclone). Reconnect must stop the serve so the following sync_serves
    # starts a fresh one bound to the remounted VFS. The serve's options match
    # SERVE_VFS_OPT, so the ONLY serve/stop here is reconnect's, not a drift.
    c, mp = _make_mount(home, rcd, served=False)
    rcd.responses["serve/list"] = {"list": [{
        "id": "http-live", "addr": "127.0.0.1:41000",
        "params": {"type": "http", "fs": "remote:bucket",
                   **mounts_mod.SERVE_VFS_OPT}}]}
    assert mounts_mod.reconnect_mount(c) is None
    assert ("serve/stop", {"id": "http-live"}) in rcd.calls
    assert any(m == "mount/mount" for m, _ in rcd.calls)


def test_reconnect_reports_force_unmount_failure(home, rcd, monkeypatch):
    c, mp = _make_mount(home, rcd, served=False)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: p == mp)

    def fake_run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "umount: busy"

        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    err = mounts_mod.reconnect_mount(c)
    assert err is not None and "force unmount" in err
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


def test_forced_detach_escalates_on_rc_failure(home, rcd, monkeypatch):
    c, mp = _make_mount(home, rcd, served=False)
    rcd.responses["mount/unmount"] = (500, {"error": "failed to umount the NFS volume"})
    still_mounted = {"v": True}
    monkeypatch.setattr(mounts_mod.os.path, "ismount",
                        lambda p: p == mp and still_mounted["v"])

    def fake_run(cmd, **kw):
        still_mounted["v"] = False

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(mounts_mod.subprocess, "run", fake_run)
    assert mounts_mod.detach_mount(c, force=True) is None
    # unforced stays loud: same rc failure, no force fallback
    still_mounted["v"] = True
    err = mounts_mod.detach_mount(c)
    assert err is not None and "unmount failed" in err


def test_reconnect_endpoint(client, rcd, monkeypatch):
    m = client.post("/api/mounts", json={"name": "data", "remote": "r:bucket"},
                    headers=FUSED).json()
    assert client.post(f"/api/mounts/{m['id']}/reconnect").status_code == 403
    r = client.post(f"/api/mounts/{m['id']}/reconnect", headers=FUSED)
    assert r.status_code == 200
    assert client.post("/api/mounts/nope/reconnect", headers=FUSED).status_code == 404


def test_fs_list_errors_for_dead_mount_instead_of_empty(client, rcd, home):
    # The user-visible bug: a dead mount leaves a plain empty dir at the
    # mountpoint, and the folder view rendered it as an ordinary empty
    # folder. It must 503 with a pointer to the Mounts page instead.
    m = client.post("/api/mounts", json={"name": "data", "remote": "r:bucket"},
                    headers=FUSED).json()
    mp = m["mountpoint"]
    _os.makedirs(mp, exist_ok=True)
    # rcd tracks the mount (create succeeded) but nothing is kernel-mounted:
    # state != "mounted" -> the empty listing is not trustworthy.
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "r:bucket", "MountPoint": mp}]}
    r = client.get("/api/fs/list", params={"path": mp})
    assert r.status_code == 503
    assert "Mounts page" in r.json()["error"]


def test_fs_list_normal_empty_dir_still_lists(client, home, tmp_path):
    d = tmp_path / "plain-empty"
    d.mkdir()
    r = client.get("/api/fs/list", params={"path": str(d)})
    assert r.status_code == 200 and r.json()["entries"] == []


# -- automount at startup --------------------------------------------------------


def test_run_automount_mounts_every_mount(home, rcd):
    mounts_mod.add_mount("one", "r:one")
    mounts_mod.add_mount("two", "r:two")
    mounts_mod.run_automount()
    mounted = sorted(b["fs"] for m, b in rcd.calls if m == "mount/mount")
    # No per-mount opt-in: every mount is remounted at startup.
    assert mounted == ["r:one", "r:two"]


def test_run_automount_skips_already_mounted(home, rcd):
    c = mounts_mod.add_mount("auto", "r:one")
    rcd.responses["mount/listmounts"] = {
        "mountPoints": [{"Fs": "r:one", "MountPoint": mounts_mod.mountpoint(c)}]}
    mounts_mod.run_automount()
    assert not any(m == "mount/mount" for m, _ in rcd.calls)


# -- http serves (the duckdb reader's mounted-parquet fast path) -----------------


def test_attach_starts_http_serve_and_writes_map(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket/prefix")
    assert mounts_mod.attach_mount(c) is None
    [(_, body)] = [x for x in rcd.calls if x[0] == "serve/start"]
    assert body["type"] == "http"
    assert body["fs"] == "remote:bucket/prefix"
    # Serve-side vfs cache is a capped on-disk range cache: unlike DuckDB's
    # in-RAM external file cache it survives reconnects and server restarts.
    # serve/start takes vfs options as FLAT vfs_* rc params — a `vfsOpt`
    # object is mount/mount-only and gets silently ignored (cache stays off).
    assert "vfsOpt" not in body
    assert body["vfs_cache_mode"] == "full"
    assert body["vfs_cache_max_size"] == "20Gi"
    # The serve is started with the FULL derived option set (not just cache
    # mode/size) so it shares the mount's VFS — every SERVE_VFS_OPT key present.
    for k, v in mounts_mod.SERVE_VFS_OPT.items():
        assert body[k] == v
    serves = json.load(open(mounts_mod.serves_path()))
    assert serves == {mounts_mod.mountpoint(c): "http://127.0.0.1:59999"}


def test_attach_syncs_serves_when_already_mounted(home, rcd, monkeypatch):
    # The already-a-kernel-mount early return must still reconcile serves:
    # without one, /api/fs/raw falls back to reads through the kernel mount.
    c = mounts_mod.add_mount("data", "remote:bucket")
    monkeypatch.setattr(mounts_mod.os.path, "ismount",
                        lambda p: p == mounts_mod.mountpoint(c))
    assert mounts_mod.attach_mount(c) is None
    assert not any(m == "mount/mount" for m, _ in rcd.calls)
    [(_, body)] = [x for x in rcd.calls if x[0] == "serve/start"]
    assert body["fs"] == "remote:bucket"
    serves = json.load(open(mounts_mod.serves_path()))
    assert serves == {mounts_mod.mountpoint(c): "http://127.0.0.1:59999"}


def test_sync_serves_reuses_existing_serve(home, rcd):
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["serve/list"] = {"list": [{
        "id": "http-live", "addr": "127.0.0.1:41000",
        "params": {"type": "http", "fs": "remote:bucket",
                   **mounts_mod.SERVE_VFS_OPT}}]}
    mounts_mod.sync_serves()
    assert not any(m == "serve/start" for m, _ in rcd.calls)
    serves = json.load(open(mounts_mod.serves_path()))
    assert serves == {mounts_mod.mountpoint(c): "http://127.0.0.1:41000"}


def test_sync_serves_restarts_serve_with_stale_vfs_opts(home, rcd):
    # Serves outlive server runs, so a SERVE_VFS_OPT change would never reach
    # an adopted serve unless the sync notices the drift and restarts it.
    # A serve started by the old vfsOpt-object code has NO flat vfs_* params
    # (rcd ignored the object) — exactly this drift, so the fix self-deploys.
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["serve/list"] = {"list": [{
        "id": "http-stale", "addr": "127.0.0.1:41000",
        "params": {"type": "http", "fs": "remote:bucket",
                   "vfsOpt": {"CacheMode": "full", "CacheMaxSize": "5Gi"}}}]}
    mounts_mod.sync_serves()
    [(_, stop)] = [x for x in rcd.calls if x[0] == "serve/stop"]
    assert stop == {"id": "http-stale"}
    [(_, start)] = [x for x in rcd.calls if x[0] == "serve/start"]
    assert start["vfs_cache_mode"] == "full"
    assert start["vfs_cache_max_size"] == "20Gi"
    serves = json.load(open(mounts_mod.serves_path()))
    assert serves == {mounts_mod.mountpoint(c): "http://127.0.0.1:59999"}


def test_sync_serves_stops_orphaned_serve(home, rcd):
    # A serve whose mount record is gone (deleted mount) gets stopped.
    rcd.responses["serve/list"] = {"list": [{
        "id": "http-orphan", "addr": "127.0.0.1:41001",
        "params": {"type": "http", "fs": "remote:deleted"}}]}
    mounts_mod.sync_serves()
    [(_, body)] = [x for x in rcd.calls if x[0] == "serve/stop"]
    assert body == {"id": "http-orphan"}
    assert json.load(open(mounts_mod.serves_path())) == {}


def test_sync_serves_without_daemon_clears_map(home):
    # No rcd: nothing can be served, so a stale map must not send the duckdb
    # reader to dead URLs (it would fall back, but why leave the trap).
    mounts_mod.add_mount("data", "remote:bucket")
    mounts_mod.sync_serves()
    assert json.load(open(mounts_mod.serves_path())) == {}


def test_sync_serves_survives_serve_start_failure(home, rcd):
    c = mounts_mod.add_mount("a", "remote:a")
    c2 = mounts_mod.add_mount("b", "remote:b")
    rcd.responses["serve/start"] = [
        (500, {"error": "boom"}),
        {"addr": "127.0.0.1:42000", "id": "http-b"},
    ]
    mounts_mod.sync_serves()
    serves = json.load(open(mounts_mod.serves_path()))
    # The failed remote is just absent; the other one still gets served.
    assert list(serves.values()) == ["http://127.0.0.1:42000"]


def test_serve_url_for_maps_and_quotes(home, rcd):
    import os

    c = mounts_mod.add_mount("data", "remote:bucket")
    mounts_mod.sync_serves()  # stub serve at 127.0.0.1:59999
    mp = mounts_mod.mountpoint(c)
    url = mounts_mod.serve_url_for(os.path.join(mp, "year=2022", "a b.parquet"))
    assert url == "http://127.0.0.1:59999/year%3D2022/a%20b.parquet"
    assert mounts_mod.serve_url_for("/somewhere/else.parquet") is None


def test_stat_marks_mount_backed_files_remote(client, home):
    import os

    mp = os.path.join(mounts_mod.mounts_dir(), "data")
    os.makedirs(mp)
    f = os.path.join(mp, "x.parquet")
    open(f, "wb").write(b"pq")
    assert client.get("/api/fs/stat", params={"path": f}).json()["remote"] is True
    plain = os.path.join(str(home), "y.parquet")
    open(plain, "wb").write(b"pq")
    assert client.get("/api/fs/stat", params={"path": plain}).json()["remote"] is False


def test_fs_raw_proxies_range_from_mount_serve(client, home):
    """/api/fs/raw for a mount-backed path streams GETs from the mount's
    HTTP serve, not from the local filesystem. HEAD is the exception: it is
    answered from the mount's own stat — ranged clients (duckdb httpfs,
    fsspec/zarr) probe the length before every read, and proxying that probe
    costs a full remote round trip for headers the VFS getattr already
    knows; on a real mount the stat and the serve read the same remote, so
    the sizes agree (here they intentionally differ to prove the source)."""
    import functools
    import http.server
    import os

    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.bin")
    open(f, "wb").write(b"LOCAL-BYTES")  # what a (dead-mount) local read would see

    served = home / "served"
    served.mkdir()
    (served / "x.bin").write_bytes(b"REMOTE-BYTES")

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(H, directory=str(served)))
    import threading

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import fused_render.shell.storage as storage
        storage.write_json(mounts_mod.serves_path(),
                           {mp: f"http://127.0.0.1:{srv.server_address[1]}"})
        r = client.get("/api/fs/raw", params={"path": f})
        assert r.status_code == 200 and r.content == b"REMOTE-BYTES"
        r = client.head("/api/fs/raw", params={"path": f})
        assert r.status_code == 200
        assert r.headers["content-length"] == str(len(b"LOCAL-BYTES"))
        assert r.headers["accept-ranges"] == "bytes"
        assert "last-modified" in r.headers
        assert r.content == b""
    finally:
        srv.shutdown()


def test_fs_raw_falls_back_to_file_when_serve_dead(client, home):
    import os
    import socket

    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.bin")
    open(f, "wb").write(b"LOCAL-BYTES")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    import fused_render.shell.storage as storage
    storage.write_json(mounts_mod.serves_path(), {mp: f"http://127.0.0.1:{dead}"})
    r = client.get("/api/fs/raw", params={"path": f})
    assert r.status_code == 200 and r.content == b"LOCAL-BYTES"


def test_fs_raw_proxy_error_keeps_range_headers(client, home):
    """An HTTP error from a live serve passes through WITH its protocol
    headers — a 416's `Content-Range: bytes */<size>` is how a range client
    (DuckDB httpfs) learns the file length."""
    import http.server
    import os
    import threading

    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.bin")
    open(f, "wb").write(b"LOCAL-BYTES")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(416)
            self.send_header("Content-Range", "bytes */12345")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import fused_render.shell.storage as storage
        storage.write_json(mounts_mod.serves_path(),
                           {mp: f"http://127.0.0.1:{srv.server_address[1]}"})
        r = client.get("/api/fs/raw", params={"path": f},
                       headers={"Range": "bytes=99999999-"})
        assert r.status_code == 416
        assert r.headers["content-range"] == "bytes */12345"
    finally:
        srv.shutdown()


# -- upstream_url_for (direct-to-store bypass) -------------------------------


@pytest.fixture()
def fresh_upstream():
    """The bypass memoizes per-remote capability and per-object links in
    module globals; clear around each test so results don't leak."""
    mounts_mod._upstream_links.clear()
    mounts_mod._upstream_mode.clear()
    yield
    mounts_mod._upstream_links.clear()
    mounts_mod._upstream_mode.clear()


def test_upstream_url_prefers_presigned_link(home, rcd, fresh_upstream):
    import os

    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x?sig=1"}
    c = mounts_mod.add_mount("data", "remote:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "a", "b.parquet")
    assert mounts_mod.upstream_url_for(f) == "https://signed.example/x?sig=1"
    [(_, body)] = [x for x in rcd.calls if x[0] == "operations/publiclink"]
    assert body["fs"] == "remote:bucket" and body["remote"] == "a/b.parquet"
    # Second ask answers from the link cache — no extra rc round-trip.
    assert mounts_mod.upstream_url_for(f) == "https://signed.example/x?sig=1"
    assert len([x for x in rcd.calls if x[0] == "operations/publiclink"]) == 1


def test_upstream_url_public_s3_when_link_unsupported(home, rcd, fresh_upstream):
    import os

    # Anonymous S3 remotes can't presign ("unsupported signer type noAuth")
    # but don't need to — the plain public object URL works.
    rcd.responses["operations/publiclink"] = (
        500, {"error": 'unsupported signer type "smithy.api#noAuth"'})
    rcd.responses["config/get"] = {
        "type": "s3", "provider": "AWS", "env_auth": "false",
        "region": "us-west-2"}
    c = mounts_mod.add_mount("data", "aws-open:bucket/pre fix")
    f = os.path.join(mounts_mod.mountpoint(c), "k ey.parquet")
    assert mounts_mod.upstream_url_for(f) == (
        "https://bucket.s3.us-west-2.amazonaws.com/pre%20fix/k%20ey.parquet")


def test_upstream_url_none_is_remembered(home, rcd, fresh_upstream):
    import os

    # A credentialed remote that can't presign has no reachable direct URL;
    # the verdict is cached so the raw hot path doesn't re-ask rcd per read.
    rcd.responses["operations/publiclink"] = (500, {"error": "boom"})
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    c = mounts_mod.add_mount("data", "corp:bucket")
    f = os.path.join(mounts_mod.mountpoint(c), "x.parquet")
    assert mounts_mod.upstream_url_for(f) is None
    assert mounts_mod.upstream_url_for(f) is None
    assert len([x for x in rcd.calls if x[0] == "operations/publiclink"]) == 1


def test_upstream_url_none_outside_mounts(home, rcd, fresh_upstream):
    assert mounts_mod.upstream_url_for("/somewhere/else.parquet") is None


def test_fs_raw_redirects_cold_native_range_reads(client, home, rcd, fresh_upstream):
    """A ranged GET from a native client (no Sec-Fetch-Mode) on a cold
    mount-backed file 307s to the store's direct URL; browser requests and
    HEADs keep today's serve proxy (a redirect would die on CORS / fail a
    GET-signed link)."""
    import os

    import fused_render.shell.storage as storage

    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x"}
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.bin")
    open(f, "wb").write(b"LOCAL-BYTES")
    # A live-looking serve entry; the serve itself is never reached by the
    # redirect path, and browser/HEAD fall back to the file when it's dead.
    storage.write_json(mounts_mod.serves_path(), {mp: "http://127.0.0.1:1"})

    r = client.get("/api/fs/raw", params={"path": f},
                   headers={"Range": "bytes=0-3"}, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "https://signed.example/x"

    r = client.get("/api/fs/raw", params={"path": f},
                   headers={"Range": "bytes=0-3", "Sec-Fetch-Mode": "cors"},
                   follow_redirects=False)
    assert r.status_code != 307
    r = client.head("/api/fs/raw", params={"path": f}, follow_redirects=False)
    assert r.status_code != 307


def test_api_run_rewrites_raw_source_url_to_direct(
        client, home, rcd, fresh_upstream, tmp_path):
    """/api/run swaps a raw-proxy source_url for the store's direct URL on a
    cold mount-backed file (redirects defeat httpfs connection pooling — the
    reader must get the direct URL up front), and leaves everything else
    alone: non-mount paths, warm (prefetched) files, and foreign URLs."""
    import os
    from urllib.parse import quote

    import fused_render.shell.prefetch as prefetch_mod
    import fused_render.shell.storage as storage

    rcd.responses["operations/publiclink"] = {"url": "https://signed.example/x"}
    c = mounts_mod.add_mount("data", "remote:bucket")
    mp = mounts_mod.mountpoint(c)
    os.makedirs(mp)
    f = os.path.join(mp, "x.parquet")
    open(f, "wb").write(b"PAR1")
    storage.write_json(mounts_mod.serves_path(), {mp: "http://127.0.0.1:1"})

    echo = tmp_path / "echo.py"
    echo.write_text("def main(source_url=None):\n"
                    "    return {'source_url': source_url}\n",
                    encoding="utf-8")

    def run(src):
        r = client.post("/api/run", json={
            "py": str(echo), "params": {"source_url": src}},
            headers={"X-Fused": "1"})
        return r.json()["result"]["source_url"]

    raw = f"http://testserver/api/fs/raw?path={quote(f)}"
    # Cold mount-backed file: rewritten to the store URL.
    assert run(raw) == "https://signed.example/x"
    # Warm file (prefetch landed): the raw proxy stays, so reads replay from
    # the serve's local cache.
    with prefetch_mod._lock:
        prefetch_mod._jobs[f] = {"status": "done", "size": 4, "done": 4,
                                 "at": 0.0}
    try:
        assert run(raw) == raw
    finally:
        with prefetch_mod._lock:
            prefetch_mod._jobs.pop(f, None)
    # Non-mount path and foreign URLs: untouched.
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"x")
    unmounted = f"http://testserver/api/fs/raw?path={quote(str(plain))}"
    assert run(unmounted) == unmounted
    assert run("https://elsewhere.example/data.parquet") == \
        "https://elsewhere.example/data.parquet"


# -- read-only remotes -----------------------------------------------------
# A mount's writability is a property of the REMOTE (anonymous S3 can never
# take a write; an :http: backend has no write verbs at all), but the kernel
# mount can't say so: with CacheMode=full a write "succeeds" into the local
# VFS cache and only fails at async upload. So every attach re-detects
# read-onlyness non-mutatingly via rc (fsinfo features + remote config) and
# persists it on the mount record — unless the user chose the flag at create
# (read_only_user), which detection never overrides. Inconclusive probes
# persist nothing. server._writable consults the flag via mount_read_only().

FSINFO_RW = {"Features": {"Put": True, "PutStream": True, "Copy": True}}
FSINFO_RO = {"Features": {"Put": False, "PutStream": False, "Copy": False}}


def _attached(name, remote):
    m = mounts_mod.add_mount(name, remote)
    assert mounts_mod.attach_mount(m) is None
    return mounts_mod.get_mount(m["id"])


def test_attach_marks_anonymous_s3_read_only(home, rcd):
    rcd.responses["operations/fsinfo"] = FSINFO_RW
    rcd.responses["config/get"] = {"type": "s3", "provider": "AWS",
                                   "env_auth": "false"}
    assert _attached("pub", "aws-open:bucket")["read_only"] is True


def test_attach_marks_credentialed_s3_writable(home, rcd):
    rcd.responses["operations/fsinfo"] = FSINFO_RW
    rcd.responses["config/get"] = {"type": "s3", "provider": "AWS",
                                   "env_auth": "true", "profile": "default"}
    assert _attached("data", "aws:bucket")["read_only"] is False


def test_attach_marks_putless_backend_read_only(home, rcd):
    # An :http:-style backend advertises no write features at all — read-only
    # regardless of its config.
    rcd.responses["operations/fsinfo"] = FSINFO_RO
    rcd.responses["config/get"] = {"type": "http"}
    assert _attached("web", "web:")["read_only"] is True


def test_attach_persists_nothing_when_probe_inconclusive(home, rcd):
    # Neither probe answers (stub 404s both) — persist NO verdict: the mount
    # stays rw (the pre-flag behavior) and the next attach re-probes, so a
    # transient rcd hiccup at first attach can't freeze a wrong answer.
    m = _attached("data", "remote:bucket")
    assert "read_only" not in m
    # The probes come back — the same mount converges on the next attach.
    rcd.responses["operations/fsinfo"] = FSINFO_RO
    rcd.responses["config/get"] = {"type": "http"}
    assert mounts_mod.attach_mount(m) is None
    assert mounts_mod.get_mount(m["id"])["read_only"] is True


def test_attach_treats_missing_features_as_inconclusive(home, rcd):
    # fsinfo answering WITHOUT a Features map is version skew, not evidence
    # of read-onlyness — a writable remote must not get locked by it.
    rcd.responses["operations/fsinfo"] = {"Name": "remote"}
    rcd.responses["config/get"] = (404, {"error": "config not found"})
    assert "read_only" not in _attached("data", "remote:bucket")


def test_reattach_redetects_when_credentials_change(home, rcd):
    # Detected (not user-set) flags follow the remote: an anonymous S3 mount
    # flagged read-only flips back once credentials appear in its config.
    rcd.responses["operations/fsinfo"] = FSINFO_RW
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "false"}
    m = _attached("pub", "aws-open:bucket")
    assert m["read_only"] is True
    rcd.responses["config/get"] = {"type": "s3", "env_auth": "true"}
    assert mounts_mod.attach_mount(m) is None
    assert mounts_mod.get_mount(m["id"])["read_only"] is False


def test_explicit_read_only_flag_never_redetected(home, rcd):
    rcd.responses["operations/fsinfo"] = FSINFO_RO  # would detect read-only
    rcd.responses["config/get"] = {"type": "http"}
    m = mounts_mod.add_mount("web", "web:", read_only=False)
    assert mounts_mod.attach_mount(m) is None
    assert mounts_mod.get_mount(m["id"])["read_only"] is False


def test_add_mount_rejects_non_bool_read_only(home):
    # Straight off a JSON body: bool("false") is True, so anything but a real
    # boolean (or None) must be refused, not coerced.
    with pytest.raises(ValueError):
        mounts_mod.add_mount("pub", "aws-open:bucket", read_only="false")


def test_mount_read_only_path_lookup(home):
    ro = mounts_mod.add_mount("pub", "aws-open:bucket", read_only=True)
    rw = mounts_mod.add_mount("data", "aws:bucket", read_only=False)
    legacy = mounts_mod.add_mount("old", "old:bucket")  # pre-flag record
    assert mounts_mod.mount_read_only(
        _os.path.join(mounts_mod.mountpoint(ro), "f.parquet")) is True
    assert mounts_mod.mount_read_only(
        _os.path.join(mounts_mod.mountpoint(rw), "f.parquet")) is False
    assert mounts_mod.mount_read_only(
        _os.path.join(mounts_mod.mountpoint(legacy), "f.parquet")) is False
    assert mounts_mod.mount_read_only("/somewhere/else.parquet") is False


def test_mount_view_exposes_read_only(home):
    m = mounts_mod.add_mount("pub", "aws-open:bucket", read_only=True)
    assert mounts_mod.mount_view(m, rcd_mounts=set())["read_only"] is True
    m2 = mounts_mod.add_mount("data", "aws:bucket")
    assert mounts_mod.mount_view(m2, rcd_mounts=set())["read_only"] is False


def test_fix_dotted_bucket_url():
    fix = mounts_mod._fix_dotted_bucket_url
    # dotted bucket in the TLS hostname can never pass the wildcard cert:
    # unsigned URLs are rewritten to path-style
    assert fix("https://us-west-2.opendata.source.coop.s3.us-west-2"
               ".amazonaws.com/mindearth/a%20b/zarr.json") == \
        ("https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop"
         "/mindearth/a%20b/zarr.json")
    # a signed one can't be (SigV4 covers Host) — dropped, caller stays on
    # the serve proxy
    assert fix("https://buck.et.s3.us-east-1.amazonaws.com/k"
               "?X-Amz-Signature=abc") is None
    # dot-free buckets and non-AWS hosts pass through untouched
    for u in ("https://plain.s3.us-west-2.amazonaws.com/key",
              "https://plain.s3.us-west-2.amazonaws.com/key?X-Amz-Sig=x",
              "https://minio.example.com/bucket/key"):
        assert fix(u) == u

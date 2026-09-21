"""Task 5 — public vs temporary (30-minute, team-scoped) share links.

Against a stubbed shim (`share_app._run_shim` monkeypatched, same pattern as
tests/test_share_file_routes.py): publishing `temporary` records a
session_expires ~30 min out and reports `expired: False` right after
publish; publishing `public` records no session fields; re-publishing a file
in the other mode returns a different share token — the UI-facing fact of a
mode switch (share-any-file-plan.md's "Decisions & risks").

Also drives `is_expired`/`_parse_expiry` directly for the pure date-math,
and `_fused_share_app.publish`'s mode branching against a stubbed `fused`
SDK, the same stubbing approach tests/test_share_shim.py uses.
"""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

import fused_render.share_app as share_app_mod
import fused_render.share_file as share_file_mod
from fused_render.server import create_app

GUARD = {"X-Fused": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(share_app_mod, "STATE_DIR", str(tmp_path / "home"))
    creds = tmp_path / "credentials"
    creds.write_text("{}")
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(creds))
    return TestClient(create_app(start_dir=str(tmp_path)))


def make_file(tmp_path, name="demo.fused"):
    p = tmp_path / name
    p.write_bytes(b"not a real app, just bytes")
    return str(p)


def fake_shim_for(mode_seen: list):
    """A stand-in for _fused_share_app.publish: records the mode it was
    asked for and returns a response shaped the way the real shim would for
    that mode (session fields only when temporary)."""

    def fake_run_shim(request, timeout):
        mode = request.get("mode", "public")
        mode_seen.append(mode)
        out = {"url": f"https://udf.fused.ai/tok-{mode}/demo.html", "canvas_id": "c1",
               "canvas_name": "demo_abc123", "share_token": f"tok-{mode}", "slug": "demo_abc123",
               "remote": "fd://h/x/demo.fused", "workbench_url": "https://x", "mode": mode}
        if mode == "temporary":
            out["url"] += "?fused_session_token=sess-1"
            out["session_token"] = "sess-1"
            out["session_expires"] = time.time() + 30 * 60
        return out, None

    return fake_run_shim


def test_publish_public_records_no_session_fields(client, tmp_path, monkeypatch):
    path = make_file(tmp_path)
    monkeypatch.setattr(share_app_mod, "_run_shim", fake_shim_for([]))
    resp = client.post("/api/share/file/publish", json={"path": path, "mode": "public"},
                       headers=GUARD)
    assert resp.status_code == 200
    shared = resp.json()["shared"]
    assert shared["mode"] == "public"
    assert shared["session_token"] is None
    assert shared["session_expires"] is None
    assert shared["expired"] is False


def test_publish_defaults_to_public_when_mode_is_omitted(client, tmp_path, monkeypatch):
    path = make_file(tmp_path)
    seen = []
    monkeypatch.setattr(share_app_mod, "_run_shim", fake_shim_for(seen))
    resp = client.post("/api/share/file/publish", json={"path": path}, headers=GUARD)
    assert resp.status_code == 200
    assert seen == ["public"]


def test_publish_temporary_records_an_expiry_about_30_minutes_out(client, tmp_path, monkeypatch):
    path = make_file(tmp_path)
    monkeypatch.setattr(share_app_mod, "_run_shim", fake_shim_for([]))
    before = time.time()
    resp = client.post("/api/share/file/publish", json={"path": path, "mode": "temporary"},
                       headers=GUARD)
    assert resp.status_code == 200
    shared = resp.json()["shared"]
    assert shared["mode"] == "temporary"
    assert shared["session_token"] == "sess-1"
    assert shared["expired"] is False
    # ~30 minutes out, generously bounded either side of the mint.
    assert before + 29 * 60 < shared["session_expires"] < before + 31 * 60


def test_publish_unknown_mode_is_rejected(client, tmp_path):
    path = make_file(tmp_path)
    resp = client.post("/api/share/file/publish", json={"path": path, "mode": "private"},
                       headers=GUARD)
    assert resp.status_code == 400
    assert "share mode" in resp.json()["error"]


def test_republishing_in_the_other_mode_returns_a_different_share_token(client, tmp_path,
                                                                          monkeypatch):
    path = make_file(tmp_path)
    seen = []
    monkeypatch.setattr(share_app_mod, "_run_shim", fake_shim_for(seen))
    first = client.post("/api/share/file/publish", json={"path": path, "mode": "public"},
                        headers=GUARD).json()["shared"]
    second = client.post("/api/share/file/publish", json={"path": path, "mode": "temporary"},
                         headers=GUARD).json()["shared"]
    assert first["url"] != second["url"]
    assert seen == ["public", "temporary"]


def test_status_reports_expired_for_a_past_session(client, tmp_path, monkeypatch):
    path = make_file(tmp_path)

    def expired_shim(request, timeout):
        return {"url": "https://udf.fused.ai/tok/demo.html?fused_session_token=sess-old",
                "canvas_id": "c1", "canvas_name": "demo_abc123", "share_token": "tok",
                "slug": "demo_abc123", "remote": "fd://h/x/demo.fused", "workbench_url": "https://x",
                "mode": "temporary", "session_token": "sess-old",
                "session_expires": time.time() - 5}, None

    monkeypatch.setattr(share_app_mod, "_run_shim", expired_shim)
    client.post("/api/share/file/publish", json={"path": path, "mode": "temporary"},
               headers=GUARD)
    status = client.get("/api/share/file/status", params={"path": path}).json()
    assert status["shared"]["expired"] is True


# -- pure helpers --------------------------------------------------------------


def test_is_expired_false_for_public_mode_regardless_of_fields():
    assert share_file_mod.is_expired({"mode": "public", "session_expires": 0}) is False


def test_is_expired_false_with_no_session_expires():
    assert share_file_mod.is_expired({"mode": "temporary"}) is False


def test_is_expired_true_for_a_past_epoch_seconds():
    assert share_file_mod.is_expired({"mode": "temporary", "session_expires": time.time() - 1})


def test_is_expired_false_for_a_future_epoch_seconds():
    assert not share_file_mod.is_expired({"mode": "temporary", "session_expires": time.time() + 60})


def test_is_expired_parses_an_iso8601_string():
    import datetime

    past = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    assert share_file_mod.is_expired({"mode": "temporary", "session_expires": past})

    future = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(minutes=25)).isoformat().replace("+00:00", "Z")
    assert not share_file_mod.is_expired({"mode": "temporary", "session_expires": future})


def test_is_expired_treats_a_naive_iso8601_string_as_utc(monkeypatch):
    # Finding 7: the SDK returns a NAIVE iso string (no "Z", no offset) in
    # practice. `.timestamp()` on a naive datetime reads it in the SERVER'S
    # local zone, but the SDK means UTC — on a server set to UTC+2 a fresh
    # 30-minute token was read as already ~90 minutes expired. Pin the
    # server to a non-UTC zone so this genuinely exercises the bug rather
    # than accidentally passing on a UTC test machine.
    import datetime
    import time as time_mod

    monkeypatch.setenv("TZ", "Etc/GMT-2")  # UTC+2, POSIX sign is inverted
    time_mod.tzset()
    try:
        # A session that expires in 25 minutes if read as UTC (correct) but
        # would already look ~95 minutes expired if misread as local time.
        naive_future = (datetime.datetime.utcnow()
                        + datetime.timedelta(minutes=25)).isoformat()
        assert "Z" not in naive_future and "+" not in naive_future
        assert not share_file_mod.is_expired(
            {"mode": "temporary", "session_expires": naive_future})

        naive_past = (datetime.datetime.utcnow()
                      - datetime.timedelta(seconds=5)).isoformat()
        assert share_file_mod.is_expired(
            {"mode": "temporary", "session_expires": naive_past})
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time_mod.tzset()


# -- finding 8: lookup must not clobber a temporary share's session URL -------


def test_lookup_preserves_the_stored_session_url_for_a_temporary_share(client, tmp_path,
                                                                        monkeypatch):
    path = make_file(tmp_path)
    monkeypatch.setattr(share_app_mod, "_run_shim", fake_shim_for([]))
    published = client.post("/api/share/file/publish",
                            json={"path": path, "mode": "temporary"},
                            headers=GUARD).json()["shared"]
    assert "fused_session_token=" in published["url"]

    # `lookup`'s shim call only ever returns the bare, session-less
    # _share_url() (unlike `publish`/`status`) — it must not silently swap
    # the stored session URL out for that dead link.
    def bare_lookup_shim(request, timeout):
        return {"found": True, "url": "https://udf.fused.ai/tok-temporary/demo.html",
                "canvas_id": "c1", "canvas_name": "demo_abc123", "share_token": "tok-temporary",
                "slug": "demo_abc123"}, None

    monkeypatch.setattr(share_app_mod, "_run_shim", bare_lookup_shim)
    looked_up = client.post("/api/share/file/lookup", json={"path": path},
                            headers=GUARD).json()
    assert looked_up["found"] is True
    assert looked_up["shared"]["url"] == published["url"]
    assert "fused_session_token=" in looked_up["shared"]["url"]


# -- shim's mode branching (stubbed fused SDK) -----------------------------------


@pytest.fixture
def shim(monkeypatch):
    """Stub the real `fused` SDK the shim imports at module level, the same
    approach tests/test_share_shim.py uses, then import the module fresh."""
    import sys
    import types

    fused_mod = types.ModuleType("fused")
    fused_mod.api = types.SimpleNamespace()
    fused_mod._env = lambda name: None

    global_api_mod = types.ModuleType("fused._global_api")

    class _FakeApi:
        def __init__(self):
            self.collections: dict[str, dict] = {}
            self.session_calls: list[tuple[str, int]] = []
            self._next_id = 1
            self._next_token = 1

        def whoami(self):
            return {"handle": "someone"}

        def get_collection_by_name(self, name):
            info = self.collections.get(name)
            if info is None:
                raise Exception(f"Collection {name!r} not found")
            return dict(info, id=info["id"], access_scope=info["access_scope"])

        def create_collection(self, name, access_scope):
            cid = f"c{self._next_id}"
            self._next_id += 1
            self.collections[name] = {"id": cid, "access_scope": access_scope,
                                      "share_token": None}
            import types as _t
            return _t.SimpleNamespace(id=cid, access_scope=access_scope)

        def import_collection_toml_zip(self, collection_id, data):
            return None

        def update_collection(self, collection_id, name, access_scope):
            self.collections[name]["access_scope"] = access_scope

        def share_collection(self, collection_id, new_token=False):
            import types as _t
            for info in self.collections.values():
                if info["id"] == collection_id:
                    if new_token or not info["share_token"]:
                        info["share_token"] = f"tok-{collection_id}-{self._next_token}"
                        self._next_token += 1
                    return _t.SimpleNamespace(share_token=info["share_token"])
            raise Exception("collection not found")

        def _session_token(self, token, session_max_age):
            self.session_calls.append((token, session_max_age))
            import types as _t
            return _t.SimpleNamespace(session_token=f"sess-{token}", expires_at=time.time() + session_max_age)

    api = _FakeApi()
    global_api_mod.get_api = lambda: api

    options_mod = types.ModuleType("fused._options")
    options_mod.options = types.SimpleNamespace(
        shared_udf_base_url="https://udf.fused.ai", base_web_url="https://fused.io")

    monkeypatch.setitem(sys.modules, "fused", fused_mod)
    monkeypatch.setitem(sys.modules, "fused._global_api", global_api_mod)
    monkeypatch.setitem(sys.modules, "fused._options", options_mod)
    sys.modules.pop("fused_render._fused_share_app", None)
    import fused_render._fused_share_app as mod

    monkeypatch.setattr(mod, "_resolve_remote", lambda remote: "s3://bucket/" + remote.split("/", 3)[-1])
    mod.fused.api.upload = lambda local, remote: None
    mod.fused.api.delete = lambda *a, **k: None
    mod.fused.api.whoami = lambda: {"handle": "someone"}

    mod._FAKE_API = api
    yield mod
    sys.modules.pop("fused_render._fused_share_app", None)


def test_shim_publish_public_creates_a_public_collection_with_no_session(shim, tmp_path):
    f = tmp_path / "demo.fused"
    f.write_bytes(b"x")
    out = shim.publish({"share_id": "demo-abc123", "viewer_token": "UDF_Fused_App_File",
                        "name": "demo.fused", "file": str(f), "mode": "public"})
    assert out["mode"] == "public"
    assert out["session_token"] is None
    assert "fused_session_token" not in out["url"]
    assert shim._FAKE_API.collections["demo_abc123"]["access_scope"] == "public"


def test_shim_publish_temporary_sets_team_scope_and_mints_a_session(shim, tmp_path):
    f = tmp_path / "demo.fused"
    f.write_bytes(b"x")
    out = shim.publish({"share_id": "demo-abc123", "viewer_token": "UDF_Fused_App_File",
                        "name": "demo.fused", "file": str(f), "mode": "temporary"})
    assert out["mode"] == "temporary"
    assert out["session_token"]
    assert "fused_session_token=" in out["url"]
    assert shim._FAKE_API.collections["demo_abc123"]["access_scope"] == "team"
    assert shim._FAKE_API.session_calls[-1][1] == shim.SESSION_MAX_AGE_S


def test_shim_publish_mode_switch_reissues_the_share_token(shim, tmp_path):
    f = tmp_path / "demo.fused"
    f.write_bytes(b"x")
    req = {"share_id": "demo-abc123", "viewer_token": "UDF_Fused_App_File", "name": "demo.fused",
          "file": str(f)}
    first = shim.publish({**req, "mode": "public"})
    second = shim.publish({**req, "mode": "temporary"})
    assert first["share_token"] != second["share_token"]


def test_shim_publish_rejects_an_unknown_mode(shim, tmp_path):
    f = tmp_path / "demo.fused"
    f.write_bytes(b"x")
    with pytest.raises(shim.ShareError):
        shim.publish({"share_id": "demo-abc123", "viewer_token": "UDF_Fused_App_File",
                     "name": "demo.fused", "file": str(f), "mode": "private"})

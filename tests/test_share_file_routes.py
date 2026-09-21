"""share_file.py's routes: /api/share/file/status, /publish, /lookup,
/remove. Mirrors share_app.py's own guards (401/409 not_logged_in, 409 busy)
against a plain file instead of an app folder.

No network and no real SDK: `.fused` is used as the shareable extension
throughout because its viewer is a BUILT-IN rule (share_file_rules.py) that
resolves with no catalog cache at all, so these tests never need to touch
share_file_rules' disk cache or the shim subprocess for viewer resolution.
share_app._run_shim is monkeypatched directly wherever a route would reach
it, following the TestClient setup in tests/test_canvases.py.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import fused_render.share_app as share_app_mod
import fused_render.share_file as share_file_mod
from fused_render.server import create_app

GUARD = {"X-Fused": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    # STATE_DIR is a module-level constant computed at import time, so a
    # later FUSED_RENDER_HOME env change would not move it — patch directly,
    # the same reasoning test_appfile.py's isolated_home fixture documents.
    monkeypatch.setattr(share_app_mod, "STATE_DIR", str(tmp_path / "home"))
    # No credentials file by default — not signed in.
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(tmp_path / "no-credentials"))
    return TestClient(create_app(start_dir=str(tmp_path)))


def _sign_in(tmp_path, monkeypatch):
    creds = tmp_path / "credentials"
    creds.write_text("{}")
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(creds))


def make_file(tmp_path, name="demo.fused", subdir=None):
    d = tmp_path if subdir is None else (tmp_path / subdir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"not a real app, just bytes")
    return str(p)


# -- status -------------------------------------------------------------------


def test_status_on_unshareable_extension_refuses(client, tmp_path):
    path = make_file(tmp_path, "notebook.ipynb")
    resp = client.get("/api/share/file/status", params={"path": path})
    assert resp.status_code == 200
    body = resp.json()
    assert body["can_share"] is False
    assert body["refusal"] and ".ipynb" in body["refusal"]
    assert body["viewer"] is None


def test_status_on_shareable_extension_returns_the_viewer(client, tmp_path):
    path = make_file(tmp_path, "demo.fused")
    resp = client.get("/api/share/file/status", params={"path": path})
    assert resp.status_code == 200
    body = resp.json()
    assert body["can_share"] is True
    assert body["refusal"] is None
    assert body["viewer"] == "Fused_App_File"
    assert body["file_id"]


def test_identity_stable_across_two_calls(client, tmp_path):
    path = make_file(tmp_path, "demo.fused")
    id1 = client.get("/api/share/file/status", params={"path": path}).json()["file_id"]
    id2 = client.get("/api/share/file/status", params={"path": path}).json()["file_id"]
    assert id1 == id2


def test_identity_differs_for_same_named_files_in_different_folders(client, tmp_path):
    path_a = make_file(tmp_path, "demo.fused", subdir="a")
    path_b = make_file(tmp_path, "demo.fused", subdir="b")
    id_a = client.get("/api/share/file/status", params={"path": path_a}).json()["file_id"]
    id_b = client.get("/api/share/file/status", params={"path": path_b}).json()["file_id"]
    assert id_a != id_b


# -- publish --------------------------------------------------------------------


def test_publish_without_credentials_returns_409_not_logged_in(client, tmp_path):
    path = make_file(tmp_path, "demo.fused")
    resp = client.post("/api/share/file/publish", json={"path": path}, headers=GUARD)
    assert resp.status_code == 409
    assert resp.json()["code"] == "not_logged_in"


def test_publish_missing_guard_header_is_refused(client, tmp_path):
    path = make_file(tmp_path, "demo.fused")
    resp = client.post("/api/share/file/publish", json={"path": path})
    assert resp.status_code == 403


def test_publish_second_concurrent_call_gets_409_busy(client, tmp_path, monkeypatch):
    _sign_in(tmp_path, monkeypatch)
    path = make_file(tmp_path, "demo.fused")
    lock = share_app_mod._app_lock(os.path.abspath(path))
    assert lock.acquire(blocking=False)
    try:
        resp = client.post("/api/share/file/publish", json={"path": path}, headers=GUARD)
        assert resp.status_code == 409
        assert resp.json()["code"] == "busy"
    finally:
        lock.release()


def test_publish_unshareable_extension_is_refused_before_signin_check(client, tmp_path,
                                                                       monkeypatch):
    # Refusal fires even signed out, since it never depends on being signed in.
    path = make_file(tmp_path, "notebook.ipynb")
    resp = client.post("/api/share/file/publish", json={"path": path}, headers=GUARD)
    assert resp.status_code == 400
    assert ".ipynb" in resp.json()["error"]


def test_publish_calls_the_shim_with_the_resolved_viewer_token(client, tmp_path, monkeypatch):
    _sign_in(tmp_path, monkeypatch)
    path = make_file(tmp_path, "demo.fused")
    captured = {}

    def fake_run_shim(request, timeout):
        captured["request"] = request
        return {"url": "https://udf.fused.ai/tok/demo.html", "canvas_id": "c1",
                "canvas_name": "demo_abc123", "share_token": "tok", "slug": "demo_abc123",
                "remote": "fd://h/x/demo.fused", "workbench_url": "https://x"}, None

    monkeypatch.setattr(share_app_mod, "_run_shim", fake_run_shim)
    resp = client.post("/api/share/file/publish", json={"path": path}, headers=GUARD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["shared"]["viewer"] == "Fused_App_File"
    assert captured["request"]["viewer_token"] == "UDF_Fused_App_File"
    assert captured["request"]["action"] == "publish"

    # Re-fetching status now reports the share from the local record.
    status = client.get("/api/share/file/status", params={"path": path}).json()
    assert status["shared"]["url"] == "https://udf.fused.ai/tok/demo.html"


# -- lookup / remove --------------------------------------------------------------


def test_lookup_without_credentials_returns_409_not_logged_in(client, tmp_path):
    path = make_file(tmp_path, "demo.fused")
    resp = client.post("/api/share/file/lookup", json={"path": path}, headers=GUARD)
    assert resp.status_code == 409
    assert resp.json()["code"] == "not_logged_in"


def test_remove_without_credentials_returns_409_not_logged_in(client, tmp_path):
    path = make_file(tmp_path, "demo.fused")
    resp = client.post("/api/share/file/remove", json={"path": path}, headers=GUARD)
    assert resp.status_code == 409
    assert resp.json()["code"] == "not_logged_in"

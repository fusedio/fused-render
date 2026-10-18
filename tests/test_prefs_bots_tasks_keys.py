"""The four prefs keys that moved off localStorage (D1377): bots_autostage,
bots_sounds, bots_clear_enabled, tasks_view_tabs."""
import json
import os

from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.shell import prefs, storage

FUSED = {"X-Fused": "1"}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    return TestClient(create_app(start_dir=str(tmp_path)))


def _stored() -> dict:
    with open(os.path.join(storage.home_dir(), "prefs.json")) as f:
        return json.load(f)


def test_defaults(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/prefs").json()
    assert body["bots"] == {"autostage": True, "sounds": True, "clear_enabled": False}
    assert body["tasks_view_tabs"] == []
    assert prefs.bots_autostage() is True and prefs.bots_sounds() is True
    assert prefs.bots_clear_enabled() is False and prefs.tasks_view_tabs() == []


def test_put_roundtrips(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.put("/api/prefs", headers=FUSED, json={
        "bots_autostage": False, "bots_sounds": False, "bots_clear_enabled": True,
        "tasks_view_tabs": ["board", "list", "board", "calendar"]})
    assert r.status_code == 200
    body = r.json()
    assert body["bots"] == {"autostage": False, "sounds": False, "clear_enabled": True}
    # Order kept, repeats dropped.
    assert body["tasks_view_tabs"] == ["board", "list", "calendar"]
    assert client.get("/api/prefs").json()["bots"]["clear_enabled"] is True
    # [] is a real value: back to the client's defaults.
    r = client.put("/api/prefs", headers=FUSED, json={"tasks_view_tabs": []})
    assert r.json()["tasks_view_tabs"] == [] and _stored()["tasks_view_tabs"] == []


def test_put_one_key_leaves_the_others(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.put("/api/prefs", headers=FUSED, json={"bots_sounds": False})
    body = client.put("/api/prefs", headers=FUSED, json={"bots_autostage": False}).json()
    assert body["bots"] == {"autostage": False, "sounds": False, "clear_enabled": False}


def test_put_invalid_is_400_without_a_write(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    bad = [
        {"bots_autostage": "yes"},
        {"bots_sounds": 1},
        {"bots_clear_enabled": None},
        {"tasks_view_tabs": "board"},
        {"tasks_view_tabs": ["Board"]},
        {"tasks_view_tabs": ["board-view"]},
        {"tasks_view_tabs": ["x" * 25]},
        {"tasks_view_tabs": [""]},
        {"tasks_view_tabs": [1]},
        {"tasks_view_tabs": [f"t{c}" for c in "abcdefghi"]},
    ]
    for patch in bad:
        r = client.put("/api/prefs", headers=FUSED, json=patch)
        assert r.status_code == 400, patch
    assert not os.path.exists(os.path.join(storage.home_dir(), "prefs.json"))


def test_put_requires_fused_header(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.put("/api/prefs", json={"bots_sounds": False}).status_code == 403


def test_readers_tolerate_a_hand_edited_file(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    path = os.path.join(storage.home_dir(), "prefs.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"bots_autostage": "no", "bots_clear_enabled": 1,
                   "tasks_view_tabs": ["list", "Bad", 3, "list", "board"]}, f)
    # Only a stored boolean moves a switch off its default.
    assert prefs.bots_autostage() is True and prefs.bots_clear_enabled() is False
    assert prefs.tasks_view_tabs() == ["list", "board"]

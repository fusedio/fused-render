"""GET/POST /api/notifications/state (fused_render/shell/notifications.py, D1377)."""
import time

from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.shell import notifications

FUSED = {"X-Fused": "1"}
DAY_MS = 24 * 60 * 60 * 1000


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_get_absent_is_empty(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/notifications/state").json() == {
        "seen": [], "firstSeenAt": {}, "repoDismissed": {}, "attentionDismissed": {}}


def test_post_merges(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    now = time.time() * 1000
    r = client.post("/api/notifications/state", headers=FUSED, json={
        "seen": ["a"], "firstSeenAt": {"a": now - 10, "b": now - 5},
        "repoDismissed": {"/r1": "sig1", "/r2": "sig2"}})
    assert r.status_code == 200
    # A second window: union seen, keep the EARLIER stamp, posted dismissal
    # wins, "" / null deletes.
    r = client.post("/api/notifications/state", headers=FUSED, json={
        "seen": ["b", "a"], "firstSeenAt": {"a": now - 1, "b": now - 50},
        "repoDismissed": {"/r1": "sig1b", "/r2": ""},
        "attentionDismissed": {"t": "s", "gone": None}})
    body = r.json()
    assert body["seen"] == ["a", "b"]
    assert body["firstSeenAt"] == {"a": now - 10, "b": now - 50}
    assert body["repoDismissed"] == {"/r1": "sig1b"}
    assert body["attentionDismissed"] == {"t": "s"}
    assert client.get("/api/notifications/state").json() == body


def test_prune_keeps_never_stamped_seen(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    now = 1_000 * DAY_MS
    state = notifications.merge({
        "seen": ["old", "fresh", "unstamped"],
        "firstSeenAt": {"old": now - 31 * DAY_MS, "fresh": now - DAY_MS},
    }, now_ms=now)
    assert state["firstSeenAt"] == {"fresh": now - DAY_MS}
    assert state["seen"] == ["fresh", "unstamped"]


def test_prune_caps_newest_500(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    now = 1_000 * DAY_MS
    stamps = {f"k{i}": now - i for i in range(510)}
    state = notifications.merge({"seen": list(stamps), "firstSeenAt": stamps}, now_ms=now)
    assert len(state["firstSeenAt"]) == 500
    assert "k0" in state["firstSeenAt"] and "k509" not in state["firstSeenAt"]
    assert "k509" not in state["seen"] and len(state["seen"]) == 500


def test_post_guard_and_400s(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/notifications/state", json={"seen": []}).status_code == 403
    bad = [
        [1],
        {"seen": "a"},
        {"seen": [1]},
        {"seen": ["message:1"]},
        {"firstSeenAt": {"message:1": 1}},
        {"firstSeenAt": {"a": "1"}},
        {"firstSeenAt": {"a": True}},
        {"repoDismissed": {"message:x": "s"}},
        {"attentionDismissed": {"a": 1}},
    ]
    for patch in bad:
        r = client.post("/api/notifications/state", json=patch, headers=FUSED)
        assert r.status_code == 400, patch

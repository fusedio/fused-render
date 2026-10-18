"""The desk's order lives on the server (D1377): new apps insert at the top,
POST /api/current-apps/order rewrites it, and a legacy oldest-first file
reads reversed until its first write stamps it."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import current_apps
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server import create_app
from fused_render.shell import storage

FUSED = {"X-Fused": "1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    dirs = []
    for name in ("a", "b", "c"):
        d = tmp_path / "apps" / name
        d.mkdir(parents=True)
        dirs.append(canonical_fs_path(str(d)).rstrip("/"))
    return tmp_path, dirs


def _paths():
    return [a["path"] for a in current_apps.list_apps()]


def test_add_inserts_at_top(env):
    _, (a, b, c) = env
    for p in (a, b, c):
        assert current_apps.add(p) is True
    assert _paths() == [c, b, a]
    assert current_apps.add(a) is False  # already there: keeps its place
    assert _paths() == [c, b, a]
    raw = json.loads(open(os.path.join(storage.home_dir(), "current_apps.json")).read())
    assert raw["order"] == current_apps.ORDER_STAMP


def test_observe_inserts_newest_on_top(env, monkeypatch):
    _, (a, b, c) = env
    current_apps.add(a)
    folders = {"pb": b, "pc": c}
    monkeypatch.setattr(current_apps, "app_dir_for", lambda p: folders.get(p))
    # The listing arrives newest first: c's task is newer than b's.
    rows = [{"key": "k2", "project": "pc", "status": "done"},
            {"key": "k1", "project": "pb", "status": "done"}]
    current_apps.observe(rows)
    assert _paths() == [c, b, a]


def test_legacy_file_reads_reversed_then_is_stamped(env):
    _, (a, b, c) = env
    path = os.path.join(storage.home_dir(), "current_apps.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:  # today's added order: oldest first, no stamp
        json.dump({"apps": [{"path": a, "addedAt": "t", "openedAt": 1.0},
                            {"path": b, "addedAt": "t", "openedAt": 1.0}], "seen": []}, f)
    assert _paths() == [b, a]
    current_apps.add(c)
    assert _paths() == [c, b, a]
    raw = json.loads(open(path).read())
    assert raw["order"] == current_apps.ORDER_STAMP
    assert [x["path"] for x in raw["apps"]] == [c, b, a]


def test_order_route(env):
    tmp_path, (a, b, c) = env
    for p in (a, b, c):
        current_apps.add(p)
    current_apps.mark_opened(b, 42.0)
    client = TestClient(create_app(start_dir=str(tmp_path)))

    assert client.post("/api/current-apps/order", json={"paths": [a, b, c]}).status_code == 403

    resp = client.post("/api/current-apps/order", json={"paths": [a, b, c]}, headers=FUSED)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and [x["path"] for x in body["apps"]] == [a, b, c]
    # Row fields ride along untouched — the dicts are reordered, not rebuilt.
    assert next(x for x in body["apps"] if x["path"] == b)["opened_at"] == 42.0

    # Unnamed apps keep their relative order and go first; unknown, relative
    # and repeated paths are dropped; a trailing slash is the same app.
    resp = client.post("/api/current-apps/order", headers=FUSED,
                       json={"paths": [c + "/", "/nope", "rel", c, a]})
    assert [x["path"] for x in resp.json()["apps"]] == [b, c, a]
    assert _paths() == [b, c, a]

    for bad in ({"paths": "x"}, {"paths": [1]}, {}, {"paths": None}):
        r = client.post("/api/current-apps/order", json=bad, headers=FUSED)
        assert r.status_code == 400, bad

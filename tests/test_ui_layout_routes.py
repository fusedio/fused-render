"""GET/PUT /api/ui-layout (fused_render/shell/ui_layout.py, D1386)."""
import json
import os

from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.shell import storage

FUSED = {"X-Fused": "1"}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return TestClient(create_app(start_dir=str(tmp_path)))


def _doc():
    return {
        "v": 1,
        "sidebar": {"width": 240, "collapsed": False},
        "bookmarksCollapsed": True,
        "currentAppsCollapsed": False,
        "canvasesCollapsed": True,
        "explorerSide": {"hidden": False, "width": None, "maximized": True},
        "viewState": {"/Users/me/Fused": "?view=grid", "/tmp": ""},
        "tasks": {"view": "board", "calRange": "week"},
        "recentPaths": ["/Users/me/Fused/local/foo"],
        "rememberLastFolder": True,
        "canvasRailPeek": {"width": 520, "height": 720},
        "canvasRailFav": {"width": 400, "height": 480},
        "chatTextScale": 1.25,
    }


def test_get_absent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/ui-layout").json() == {"exists": False, "layout": None}


def test_put_roundtrips_and_drops_unknown_keys(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    doc = _doc()
    resp = client.put("/api/ui-layout", headers=FUSED,
                      json={**doc, "futureKey": 1, "tasks": {**doc["tasks"], "x": 2}})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    stored = json.loads(open(os.path.join(storage.home_dir(), "ui_layout.json")).read())
    assert stored == doc
    assert client.get("/api/ui-layout").json() == {"exists": True, "layout": doc}
    # Every sub-object is optional; the doc is replaced whole — except the
    # pinned rail card's size, which an older bundle's document never names
    # and so is carried forward (test below).
    assert client.put("/api/ui-layout", json={"v": 1}, headers=FUSED).status_code == 200
    assert client.get("/api/ui-layout").json()["layout"] == {
        "v": 1,
        "canvasRailPeek": doc["canvasRailPeek"],
        "canvasRailFav": doc["canvasRailFav"],
        "chatTextScale": doc["chatTextScale"],
    }


def test_put_without_canvas_rail_peek_keeps_the_stored_size(tmp_path, monkeypatch):
    """An older bundle's whole-document PUT omits `canvasRailPeek`: the size a
    newer one saved is carried forward; a PUT that names it still replaces it."""
    client = _client(tmp_path, monkeypatch)
    doc = _doc()
    assert client.put("/api/ui-layout", json=doc, headers=FUSED).status_code == 200
    older = {k: v for k, v in doc.items() if k not in ("canvasRailPeek", "canvasRailFav")}
    older["sidebar"] = {"width": 300, "collapsed": True}
    assert client.put("/api/ui-layout", json=older, headers=FUSED).status_code == 200
    got = client.get("/api/ui-layout").json()["layout"]
    assert got["sidebar"] == {"width": 300, "collapsed": True}
    assert got["canvasRailPeek"] == {"width": 520, "height": 720}
    assert got["canvasRailFav"] == {"width": 400, "height": 480}
    newer = {**older, "canvasRailPeek": {"width": 600, "height": 500}}
    assert client.put("/api/ui-layout", json=newer, headers=FUSED).status_code == 200
    assert client.get("/api/ui-layout").json()["layout"]["canvasRailPeek"] == {"width": 600, "height": 500}


def test_put_requires_fused_header(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.put("/api/ui-layout", json=_doc()).status_code == 403


def test_put_invalid_is_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    base = _doc()
    bad = [
        [1],
        {"v": 2},
        {**base, "v": True},
        {**base, "sidebar": {"width": 179, "collapsed": False}},
        {**base, "sidebar": {"width": 401, "collapsed": False}},
        {**base, "sidebar": {"width": 240.5, "collapsed": False}},
        {**base, "sidebar": {"width": 240, "collapsed": 0}},
        {**base, "bookmarksCollapsed": "yes"},
        {**base, "explorerSide": {"width": -1}},
        {**base, "explorerSide": {"hidden": 1}},
        {**base, "viewState": {"/a": "view=grid"}},
        {**base, "viewState": {"/a": "?" + "x" * 512}},
        {**base, "viewState": {f"/p{i}": "" for i in range(501)}},
        {**base, "tasks": {"view": "Board"}},
        {**base, "tasks": {"calRange": "x" * 33}},
        {**base, "recentPaths": ["/a"] * 9},
        {**base, "recentPaths": [1]},
        {**base, "rememberLastFolder": "yes"},
        {**base, "rememberLastFolder": 1},
        {**base, "canvasRailPeek": {"width": 520}},
        {**base, "canvasRailPeek": {"width": 119, "height": 720}},
        {**base, "canvasRailPeek": {"width": 520, "height": 10001}},
        {**base, "canvasRailPeek": {"width": 520.5, "height": 720}},
        {**base, "canvasRailPeek": {"width": True, "height": 720}},
        {**base, "canvasRailPeek": [520, 720]},
        {**base, "canvasRailFav": {"height": 480}},
        {**base, "canvasRailFav": {"width": 400, "height": 119}},
        {**base, "canvasRailFav": "400x480"},
        {**base, "chatTextScale": "1.25"},
        {**base, "chatTextScale": True},
        {**base, "chatTextScale": None},
    ]
    for doc in bad:
        assert client.put("/api/ui-layout", json=doc, headers=FUSED).status_code == 400, doc


def test_chat_text_scale_clamps_and_snaps_to_a_step(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    for sent, kept in [(1, 1), (1.3, 1.25), (0.5, 0.8), (9, 2), (1.04, 1)]:
        assert client.put("/api/ui-layout", json={"v": 1, "chatTextScale": sent},
                          headers=FUSED).status_code == 200
        assert client.get("/api/ui-layout").json()["layout"]["chatTextScale"] == kept, sent


def test_put_without_chat_text_scale_keeps_the_stored_zoom(tmp_path, monkeypatch):
    """An older bundle's PUT never names `chatTextScale`: the zoom survives it."""
    client = _client(tmp_path, monkeypatch)
    client.put("/api/ui-layout", json={"v": 1, "chatTextScale": 1.5}, headers=FUSED)
    assert client.put("/api/ui-layout", json={"v": 1, "bookmarksCollapsed": True},
                      headers=FUSED).status_code == 200
    assert client.get("/api/ui-layout").json()["layout"]["chatTextScale"] == 1.5


def test_corrupt_stored_doc_reads_absent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    path = os.path.join(storage.home_dir(), "ui_layout.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"v": 1, "sidebar": {"width": 9999, "collapsed": False}}, f)
    assert client.get("/api/ui-layout").json() == {"exists": False, "layout": None}

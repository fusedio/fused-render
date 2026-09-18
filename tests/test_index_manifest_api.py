"""The confirm/refuse HTTP surface over `fused_render.index.manifest`'s
propose/confirm/refuse store (SPEC-index-plugins.md decision #8, "the app
proposes, the user confirms — never silent"). Mirrors
`routers/background_apps.py`'s html-to-folder derivation and X-Fused guard
conventions: `propose` takes `html` (the calling page's own entry file),
never a raw folder path, so a caller gains no new path-typed API surface;
`confirm`/`refuse` take `folder` directly, which stays safe because
`manifest.confirm_index`/`refuse_index` only ever act on a folder already
recorded in the pending/confirmed lists by a genuine `propose` call.
"""
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app

HDRS = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # The proposal store lives in the shell home (manifest._store_path),
    # isolated per test the same way test_background_apps.py does.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _make_manifest_app(tmp_path, name="widget_app", kind="widgets"):
    """A folder with a valid [tool.fused-render.index] manifest and an
    entry html file, returning (folder, html) — mirrors
    tests/test_index_manifest.py's own fixture-writing helpers."""
    folder = tmp_path / name
    folder.mkdir()
    (folder / "pyproject.toml").write_text(
        f'[tool.fused-render.index]\nmodule = "indexer.py"\nkind = "{kind}"\n',
        encoding="utf-8")
    (folder / "indexer.py").write_text("def register():\n    pass\n", encoding="utf-8")
    html = folder / "index.html"
    html.write_text("<html></html>", encoding="utf-8")
    return str(folder), str(html)


# ------------------------------------------------------------------ propose


def test_propose_requires_x_fused_header(client, tmp_path):
    _, html = _make_manifest_app(tmp_path)
    resp = client.post("/api/index/proposals/propose", json={"html": html})
    assert resp.status_code == 403


def test_propose_requires_html_in_body(client):
    resp = client.post("/api/index/proposals/propose", json={}, headers=HDRS)
    assert resp.status_code == 400


def test_propose_without_a_manifest_reports_not_ok(client, tmp_path):
    folder = tmp_path / "no_manifest"
    folder.mkdir()
    html = folder / "index.html"
    html.write_text("<html></html>", encoding="utf-8")
    resp = client.post("/api/index/proposals/propose",
                       json={"html": str(html)}, headers=HDRS)
    assert resp.status_code == 200
    assert resp.json() == {"ok": False}


def test_propose_a_valid_manifest_lands_it_in_pending(client, tmp_path):
    folder, html = _make_manifest_app(tmp_path)
    resp = client.post("/api/index/proposals/propose",
                       json={"html": html}, headers=HDRS)
    assert resp.json() == {"ok": True}
    listing = client.get("/api/index/proposals").json()
    assert [p["folder"] for p in listing["pending"]] == [os.path.realpath(folder)]
    assert listing["confirmed"] == []


# ------------------------------------------------------------------ confirm


def test_confirm_requires_x_fused_header(client, tmp_path):
    folder, _ = _make_manifest_app(tmp_path)
    resp = client.post("/api/index/proposals/confirm", json={"folder": folder})
    assert resp.status_code == 403


def test_confirm_an_unknown_folder_reports_not_ok(client, tmp_path):
    resp = client.post("/api/index/proposals/confirm",
                       json={"folder": str(tmp_path / "nope")}, headers=HDRS)
    assert resp.json() == {"ok": False}


def test_confirm_moves_a_pending_folder_to_confirmed(client, tmp_path):
    folder, html = _make_manifest_app(tmp_path)
    client.post("/api/index/proposals/propose", json={"html": html}, headers=HDRS)
    resp = client.post("/api/index/proposals/confirm",
                       json={"folder": folder}, headers=HDRS)
    assert resp.json() == {"ok": True}
    listing = client.get("/api/index/proposals").json()
    assert listing["pending"] == []
    assert [p["folder"] for p in listing["confirmed"]] == [os.path.realpath(folder)]
    assert listing["confirmed"][0]["kind"] == "widgets"


def test_confirming_a_proposal_registers_its_kind_for_api_index_kinds(client, tmp_path):
    """Bugbot finding against a7aef9472: confirming a proposal used to only
    rewrite `index_proposals.json` — nothing imported the folder's module or
    called `register`, so a confirmed third-party kind never showed up in
    `GET /api/index/kinds` (and could never be scanned) until the process
    happened to restart. The confirm route must actually import+register."""
    from fused_render.index import kinds as kinds_mod

    kind_name = "_test_api_widgets"
    folder = tmp_path / "widget_app2"
    folder.mkdir()
    (folder / "pyproject.toml").write_text(
        f'[tool.fused-render.index]\nmodule = "indexer.py"\nkind = "{kind_name}"\n',
        encoding="utf-8")
    (folder / "indexer.py").write_text(
        "from fused_render.index.kinds import Column, IndexKind, register\n"
        "\n"
        "def _extract(path, st):\n"
        "    return None\n"
        "\n"
        f"KIND = IndexKind(name={kind_name!r}, columns=(Column('name', 'string'),),\n"
        "                  extract=_extract, text_column='name')\n"
        "\n"
        "def register(replace=False):\n"
        "    from fused_render.index import kinds as _kinds\n"
        "    _kinds.register(KIND, replace=replace)\n",
        encoding="utf-8")
    html = folder / "index.html"
    html.write_text("<html></html>", encoding="utf-8")

    try:
        client.post("/api/index/proposals/propose",
                    json={"html": str(html)}, headers=HDRS)
        assert kind_name not in client.get("/api/index/kinds").json()["kinds"]

        client.post("/api/index/proposals/confirm",
                    json={"folder": str(folder)}, headers=HDRS)

        assert kind_name in client.get("/api/index/kinds").json()["kinds"]
    finally:
        kinds_mod._REGISTRY.pop(kind_name, None)


# ------------------------------------------------------------------- refuse


def test_refuse_requires_x_fused_header(client, tmp_path):
    folder, _ = _make_manifest_app(tmp_path)
    resp = client.post("/api/index/proposals/refuse", json={"folder": folder})
    assert resp.status_code == 403


def test_refuse_removes_a_pending_proposal(client, tmp_path):
    folder, html = _make_manifest_app(tmp_path)
    client.post("/api/index/proposals/propose", json={"html": html}, headers=HDRS)
    resp = client.post("/api/index/proposals/refuse",
                       json={"folder": folder}, headers=HDRS)
    assert resp.json() == {"ok": True}
    listing = client.get("/api/index/proposals").json()
    assert listing["pending"] == []
    assert listing["confirmed"] == []


def test_refuse_revokes_an_already_confirmed_folder(client, tmp_path):
    folder, html = _make_manifest_app(tmp_path)
    client.post("/api/index/proposals/propose", json={"html": html}, headers=HDRS)
    client.post("/api/index/proposals/confirm", json={"folder": folder}, headers=HDRS)
    client.post("/api/index/proposals/refuse", json={"folder": folder}, headers=HDRS)
    listing = client.get("/api/index/proposals").json()
    assert listing["pending"] == []
    assert listing["confirmed"] == []

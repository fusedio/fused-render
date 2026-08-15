"""Registered apps (fused_render/registered_apps.py): external folders opened
through the explorer's "Open app" button, listed on /apps under the virtual
"linked" tag via ~/.fused-render/registered_apps.json.

Registration is passive — POST /api/apps/recents/open with an
outside-workspace absolute path registers the folder (or refreshes its
openedAt), so the store doubles as those apps' recents. Inside-workspace paths
keep going to app_recents.json exactly as before; these tests pin the split.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import registered_apps
from fused_render.server import create_app


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # The registry lives in the shell home; the conftest default is one shared
    # dir for the whole session, which would leak registrations across tests.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _folder(tmp_path, name, htmls=("index.html",), title=None):
    d = tmp_path / "elsewhere" / name
    d.mkdir(parents=True)
    for i, h in enumerate(htmls):
        body = "<html><body>hi</body></html>"
        if title is not None and i == 0:
            body = f"<html><head><title>{title}</title></head></html>"
        (d / h).write_text(body)
    return d


HDRS = {"X-Fused": "1"}


def _open(client, path) -> bool:
    return client.post(
        "/api/apps/recents/open", json={"path": str(path)}, headers=HDRS
    ).json()["recorded"]


# -------------------------------------------------------------- registration


def test_opening_an_external_folder_registers_it(client, tmp_path):
    d = _folder(tmp_path, "notes", title="My Notes")
    assert _open(client, d) is True

    (app,) = client.get("/api/apps").json()["apps"]
    assert app["tag"] == registered_apps.REGISTERED_TAG
    assert app["name"] == "notes"
    assert app["path"] == str(d)
    # Same app_dict shape as a workspace app — the registry reuses it wholesale.
    assert app["entry"] == app["entry_html"] == str(d / "index.html")
    assert app["title"] == "My Notes"
    assert isinstance(app["opened_at"], float)


def test_reopen_updates_opened_at_in_place(client, tmp_path):
    d = _folder(tmp_path, "notes")
    assert _open(client, d)
    first = client.get("/api/apps").json()["apps"][0]["opened_at"]
    assert _open(client, d)
    apps = client.get("/api/apps").json()["apps"]
    assert len(apps) == 1  # deduped, not appended
    assert apps[0]["opened_at"] >= first


def test_pageless_relative_or_missing_paths_do_not_register(client, tmp_path):
    no_page = tmp_path / "elsewhere" / "empty"
    no_page.mkdir(parents=True)
    assert _open(client, no_page) is False
    assert _open(client, "relative/notes") is False
    assert _open(client, tmp_path / "gone") is False
    assert client.get("/api/apps").json()["apps"] == []


def test_workspace_paths_still_go_to_recents_not_the_registry(
    client, tmp_path, workspace
):
    d = workspace / "local" / "demo"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html></html>")
    assert _open(client, d) is True

    assert registered_apps.read_entries() == []
    (app,) = client.get("/api/apps").json()["apps"]
    assert app["tag"] == "local"
    assert isinstance(app["opened_at"], float)


def test_workspace_and_ancestor_entries_are_refused_and_filtered(
    client, tmp_path, workspace
):
    (workspace / "index.html").write_text("<html></html>")
    (tmp_path / "index.html").write_text("<html></html>")
    # An ancestor of the workspace would be a card for the whole disk.
    assert _open(client, tmp_path) is False
    # A hand-edited registry can't smuggle either shape past the read filter.
    registered_apps.write_entries(
        [
            {"path": str(tmp_path), "openedAt": "2026-01-01T00:00:00+00:00"},
            {"path": str(workspace / "sub"), "openedAt": "2026-01-01T00:00:00+00:00"},
        ]
    )
    assert registered_apps.read_entries() == []


# ------------------------------------------------------------------- listing


def test_missing_folder_is_skipped_not_deleted(client, tmp_path):
    d = _folder(tmp_path, "notes")
    assert _open(client, d)
    import shutil

    shutil.rmtree(d)
    assert client.get("/api/apps").json()["apps"] == []
    # Read-only skip: the entry survives for when the folder comes back.
    assert len(registered_apps.read_entries()) == 1


def test_folder_that_lost_its_page_is_skipped(client, tmp_path):
    d = _folder(tmp_path, "notes")
    assert _open(client, d)
    (d / "index.html").unlink()
    assert client.get("/api/apps").json()["apps"] == []


def test_corrupt_registry_reads_as_empty(client, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "registered_apps.json").write_text("{not json")
    assert registered_apps.read_entries() == []
    assert client.get("/api/apps").json()["apps"] == []


def test_malformed_opened_at_drops_to_never_opened(client, tmp_path):
    d = _folder(tmp_path, "notes")
    registered_apps.write_entries([{"path": str(d), "openedAt": "not-a-time"}])
    (app,) = client.get("/api/apps").json()["apps"]
    assert app["opened_at"] is None


def test_category_from_metadata_json(client, tmp_path):
    d = _folder(tmp_path, "notes")
    (d / "metadata.json").write_text(json.dumps({"category": "Tools"}))
    assert _open(client, d)
    (app,) = client.get("/api/apps").json()["apps"]
    assert app["category"] == "Tools"


def test_external_and_workspace_apps_merge_in_one_listing(
    client, tmp_path, workspace
):
    ws = workspace / "local" / "demo"
    ws.mkdir(parents=True)
    (ws / "index.html").write_text("<html></html>")
    ext = _folder(tmp_path, "notes")
    assert _open(client, ext)

    listed = {(a["tag"], a["name"]) for a in client.get("/api/apps").json()["apps"]}
    assert listed == {("local", "demo"), ("linked", "notes")}


def test_registry_is_capped(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(registered_apps, "REGISTERED_APPS_CAP", 2)
    dirs = [_folder(tmp_path, f"app{i}") for i in range(3)]
    for d in dirs:
        assert registered_apps.record_open(str(d))
    entries = registered_apps.read_entries()
    assert [os.path.basename(e["path"]) for e in entries] == ["app2", "app1"]

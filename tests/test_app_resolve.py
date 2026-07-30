"""GET /api/app/resolve — nearest enclosing fused_app (fused.navigate)."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _make_app(root, name="app"):
    d = root / name
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html></html>", encoding="utf-8")
    (d / "fused_app.json").write_text(
        json.dumps({"fused_app": 1, "title": name,
                    "pages": [{"path": "/", "file": "index.html"}]}),
        encoding="utf-8")
    return d


def _resolve(client, path):
    r = client.get("/api/app/resolve", params={"path": str(path)})
    assert r.status_code == 200
    return r.json()


def test_file_inside_app_resolves_to_app_dir(client, tmp_path):
    d = _make_app(tmp_path)
    body = _resolve(client, d / "index.html")
    assert body["app_dir"] == str(d)
    assert body["manifest"]["title"] == "app"


def test_dir_input_resolves_itself(client, tmp_path):
    d = _make_app(tmp_path)
    assert _resolve(client, d)["app_dir"] == str(d)


def test_nested_file_walks_up_to_nearest_app(client, tmp_path):
    d = _make_app(tmp_path)
    sub = d / "pages" / "deep"
    sub.mkdir(parents=True)
    page = sub / "x.html"
    page.write_text("<html></html>", encoding="utf-8")
    assert _resolve(client, page)["app_dir"] == str(d)


def test_nearest_app_wins_over_ancestor_app(client, tmp_path):
    outer = _make_app(tmp_path, "outer")
    inner = _make_app(outer, "inner")
    assert _resolve(client, inner / "index.html")["app_dir"] == str(inner)


def test_invalid_manifest_skipped_valid_grandparent_found(client, tmp_path):
    outer = _make_app(tmp_path, "outer")
    broken = outer / "broken"
    broken.mkdir()
    (broken / "fused_app.json").write_text("{not json", encoding="utf-8")
    page = broken / "page.html"
    page.write_text("<html></html>", encoding="utf-8")
    assert _resolve(client, page)["app_dir"] == str(outer)


def test_no_manifest_anywhere_is_null(client, tmp_path):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    page = sub / "x.html"
    page.write_text("<html></html>", encoding="utf-8")
    assert _resolve(client, page)["app_dir"] is None


def test_walk_stops_at_root(client):
    # Nonexistent deep path under root: every gate denies, dirname fixpoint
    # terminates the walk — a clean null, no hang, no error.
    assert _resolve(client, "/no/such/dir/anywhere/x.html")["app_dir"] is None


def test_relative_path_rejected(client):
    r = client.get("/api/app/resolve", params={"path": "relative/x.html"})
    assert r.status_code == 400

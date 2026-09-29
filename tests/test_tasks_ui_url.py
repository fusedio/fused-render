"""`GET /api/tasks/ui` — the iframe URL behind `fused.tasks.ui()`.

The server builds it so a page never learns or guesses its app folder: with
`scope=app` the folder comes from `X-Fused-Page`, the same resolution the
scoped listing uses. The shell's param names (`embed`, `project`, `view`,
`peek`) are pinned here because runtime.js and the shell both depend on them.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _query(url: str) -> dict:
    parts = urlsplit(url)
    assert parts.path == "/tasks"
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_all_scope_board_with_task(client):
    r = client.get("/api/tasks/ui", params={"scope": "all", "view": "board", "task": "abc"})
    assert r.status_code == 200
    q = _query(r.json()["url"])
    assert q == {"embed": "1", "view": "board", "peek": "abc"}


def test_app_scope_resolves_project_from_page_header(client, tmp_path):
    app_dir = tmp_path / "some app"
    app_dir.mkdir()
    page = app_dir / "index.html"
    page.write_text('<meta name="fused-app" content="1">', encoding="utf-8")
    r = client.get("/api/tasks/ui", headers={"X-Fused-Page": str(page)})
    assert r.status_code == 200
    q = _query(r.json()["url"])
    assert q["embed"] == "1"
    assert q["project"] == str(app_dir)
    # "list" is written even though it is the page's default: a bare /tasks
    # falls back to the shell's REMEMBERED view, and the URL must outrank it
    assert q["view"] == "list"
    assert "peek" not in q


def test_app_scope_without_page_header_is_400(client):
    assert client.get("/api/tasks/ui").status_code == 400


def test_unknown_view_and_scope_are_400(client):
    assert client.get("/api/tasks/ui", params={"scope": "all", "view": "nope"}).status_code == 400
    assert client.get("/api/tasks/ui", params={"scope": "everything"}).status_code == 400

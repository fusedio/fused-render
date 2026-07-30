"""GET /call/<app dir>?route=... — fused_app .py pages as REST endpoints."""
import json

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture
def app_dir(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "index.html").write_text("<html></html>", encoding="utf-8")
    (d / "data.py").write_text(
        "def main(year: int = 2024):\n"
        "    return {\"year\": year, \"next\": year + 1}\n",
        encoding="utf-8")
    (d / "fused_app.json").write_text(json.dumps({
        "fused_app": 1,
        "title": "app",
        "pages": [
            {"path": "/", "file": "index.html"},
            {"path": "/api/data", "file": "data.py"},
            {"path": "/about", "file": "index.html"},
        ],
    }), encoding="utf-8")
    return d


def _call(client, d, **params):
    return client.get("/call" + str(d), params=params)


def test_happy_path_returns_result_json(client, app_dir):
    r = _call(client, app_dir, route="api/data")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"year": 2024, "next": 2025}


def test_annotated_param_is_coerced(client, app_dir):
    r = _call(client, app_dir, route="api/data", year="1999")
    assert r.status_code == 200
    assert r.json() == {"year": 1999, "next": 2000}


def test_reserved_and_route_params_not_forwarded(client, app_dir):
    # `route` and `_`-prefixed keys never reach main(); an unexpected kwarg
    # would be a python error, so a 200 proves they were stripped.
    r = _call(client, app_dir, route="api/data", _mode="x", year="5")
    assert r.status_code == 200
    assert r.json()["year"] == 5


def test_missing_route_param_is_400(client, app_dir):
    r = _call(client, app_dir)
    assert r.status_code == 400


def test_unknown_route_is_404(client, app_dir):
    assert _call(client, app_dir, route="nope").status_code == 404


def test_html_route_is_404(client, app_dir):
    # /about points at an .html file — a page, not an endpoint.
    assert _call(client, app_dir, route="about").status_code == 404


def test_listed_but_missing_file_is_404(client, app_dir):
    (app_dir / "fused_app.json").write_text(json.dumps({
        "pages": [
            {"path": "/", "file": "index.html"},
            {"path": "/gone", "file": "gone.py"},
        ],
    }), encoding="utf-8")
    assert _call(client, app_dir, route="gone").status_code == 404


def test_traversal_file_rejected(client, app_dir, tmp_path):
    (tmp_path / "outside.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (app_dir / "fused_app.json").write_text(json.dumps({
        "pages": [
            {"path": "/", "file": "index.html"},
            {"path": "/esc", "file": "../outside.py"},
        ],
    }), encoding="utf-8")
    assert _call(client, app_dir, route="esc").status_code == 404


def test_non_app_dir_is_404(client, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "data.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    assert _call(client, d, route="data").status_code == 404


def test_python_exception_is_500_with_error_payload(client, app_dir):
    (app_dir / "boom.py").write_text(
        "def main():\n    raise ValueError('kaboom')\n", encoding="utf-8")
    manifest = json.loads((app_dir / "fused_app.json").read_text())
    manifest["pages"].append({"path": "/boom", "file": "boom.py"})
    (app_dir / "fused_app.json").write_text(json.dumps(manifest), encoding="utf-8")
    r = _call(client, app_dir, route="boom")
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["type"] == "ValueError"
    assert "kaboom" in err["message"]


def test_view_and_api_namespaces_untouched(client, app_dir):
    # /call is its own namespace — the shell and API routes still resolve.
    assert client.get("/view" + str(app_dir)).status_code == 200
    assert client.get(
        "/api/app/resolve", params={"path": str(app_dir)}).status_code == 200

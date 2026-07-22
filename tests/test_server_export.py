"""Tests for POST /api/export (fused_render/server.py) — the HTTP wrapper
around export_page(), replacing the earlier `fused-render export` CLI form."""

import os

from fastapi.testclient import TestClient

from fused_render.server import create_app


def _client(tmp_path):
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app)


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_export_requires_x_fused_header(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/export", json={"page": "x", "out": "y"})
    assert resp.status_code == 403


def test_export_rejects_relative_paths(tmp_path):
    client = _client(tmp_path)
    headers = {"X-Fused": "1"}
    resp = client.post(
        "/api/export", json={"page": "page.html", "out": str(tmp_path / "out")}, headers=headers
    )
    assert resp.status_code == 400
    assert "absolute" in resp.json()["error"]

    resp = client.post(
        "/api/export", json={"page": str(tmp_path / "page.html"), "out": "out"}, headers=headers
    )
    assert resp.status_code == 400
    assert "absolute" in resp.json()["error"]


def test_export_success(tmp_path):
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "page.html", html)
    _write(tmp_path, "sine.py", "def main():\n    return 1\n")
    out_dir = tmp_path / "bundle"

    client = _client(tmp_path)
    resp = client.post(
        "/api/export",
        json={"page": str(tmp_path / "page.html"), "out": str(out_dir)},
        headers={"X-Fused": "1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["out"] == os.path.abspath(str(out_dir))
    assert [e["path"] for e in data["entrypoints"]] == ["./sine.py"]
    assert data["warnings"] == []
    assert (out_dir / "files" / "page.html").is_file()
    assert (out_dir / "manifest.json").is_file()


def test_export_honors_include_and_exclude(tmp_path):
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "page.html", html)
    _write(tmp_path, "sine.py", "def main():\n    return 1\n")
    _write(tmp_path, "data.csv", "a,b\n1,2\n")
    out_dir = tmp_path / "bundle"

    client = _client(tmp_path)
    resp = client.post(
        "/api/export",
        json={
            "page": str(tmp_path / "page.html"),
            "out": str(out_dir),
            "include": ["data.csv"],
            "exclude": ["./sine.py"],
        },
        headers={"X-Fused": "1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["entrypoints"] == []  # sine.py excluded
    assert [a["path"] for a in data["assets"]] == ["data.csv"]  # include bundled
    assert (out_dir / "files" / "data.csv").is_file()
    assert any("sine.py" in w for w in data["warnings"])


def test_export_rejects_bad_include_type(tmp_path):
    _write(tmp_path, "page.html", "<html></html>")
    client = _client(tmp_path)
    resp = client.post(
        "/api/export",
        json={"page": str(tmp_path / "page.html"), "out": str(tmp_path / "out"), "include": "x"},
        headers={"X-Fused": "1"},
    )
    assert resp.status_code == 400
    assert "include" in resp.json()["error"]


def test_export_forwards_cache_max_age_to_manifest(tmp_path):
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "page.html", html)
    _write(tmp_path, "sine.py", "def main():\n    return 1\n")
    out_dir = tmp_path / "bundle"

    client = _client(tmp_path)
    resp = client.post(
        "/api/export",
        json={
            "page": str(tmp_path / "page.html"),
            "out": str(out_dir),
            "cache_max_age": "15m",
        },
        headers={"X-Fused": "1"},
    )
    assert resp.status_code == 200, resp.text
    import json

    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["cache_max_age"] == "15m"


def test_export_rejects_bad_cache_max_age(tmp_path):
    _write(tmp_path, "page.html", "<html></html>")
    client = _client(tmp_path)
    resp = client.post(
        "/api/export",
        json={
            "page": str(tmp_path / "page.html"),
            "out": str(tmp_path / "out"),
            "cache_max_age": "5x",
        },
        headers={"X-Fused": "1"},
    )
    assert resp.status_code == 400
    assert "cache_max_age" in resp.json()["error"]


def test_export_error_is_400(tmp_path):
    html = "<script>const p = './x.py'; fused.runPython(p, {});</script>"
    _write(tmp_path, "page.html", html)

    client = _client(tmp_path)
    resp = client.post(
        "/api/export",
        json={"page": str(tmp_path / "page.html"), "out": str(tmp_path / "out")},
        headers={"X-Fused": "1"},
    )
    assert resp.status_code == 400
    assert "non-literal" in resp.json()["error"]

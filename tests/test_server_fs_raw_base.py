"""GET /api/fs/raw page-relative resolution (SPEC RH-1).

A relative `path` is resolved against the directory of `base` (the page's own absolute
path, sent by the runtime's fused.rawUrl); an absolute `path` is served verbatim. This is
what lets one `fused.rawUrl("data/x.json")` call resolve both locally and, when hosted,
against the bundle's _asset route by the same key.
"""
from fastapi.testclient import TestClient

from fused_render.server import create_app


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_relative_path_resolved_against_base_dir(tmp_path):
    page = _write(tmp_path, "proj/index.html", "<html></html>")
    _write(tmp_path, "proj/data/a.json", '{"v": 1}')
    client = _client(tmp_path)

    resp = client.get("/api/fs/raw", params={"path": "data/a.json", "base": str(page)})
    assert resp.status_code == 200
    assert resp.json() == {"v": 1}


def test_relative_path_without_base_is_not_page_resolved(tmp_path):
    # Without base, a relative path is not page-resolved (resolves against cwd) — so the
    # page-local file is not found. base is what makes the relative form meaningful.
    _write(tmp_path, "proj/index.html", "<html></html>")
    _write(tmp_path, "proj/data/a.json", '{"v": 1}')
    client = _client(tmp_path)

    resp = client.get("/api/fs/raw", params={"path": "data/a.json"})
    assert resp.status_code == 404


def test_absolute_path_ignores_base(tmp_path):
    page = _write(tmp_path, "proj/index.html", "<html></html>")
    target = _write(tmp_path, "elsewhere/b.json", '{"v": 2}')
    client = _client(tmp_path)

    # An absolute path is served as-is even when a base is supplied.
    resp = client.get("/api/fs/raw", params={"path": str(target), "base": str(page)})
    assert resp.status_code == 200
    assert resp.json() == {"v": 2}


def test_dot_slash_relative_resolved(tmp_path):
    page = _write(tmp_path, "proj/index.html", "<html></html>")
    _write(tmp_path, "proj/logo.svg", "<svg/>")
    client = _client(tmp_path)

    resp = client.get("/api/fs/raw", params={"path": "./logo.svg", "base": str(page)})
    assert resp.status_code == 200
    assert resp.text == "<svg/>"

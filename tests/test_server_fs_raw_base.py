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


def test_head_reports_size_and_a_missing_file_is_404(tmp_path):
    f = _write(tmp_path, "a.txt", "hello")
    client = _client(tmp_path)
    ok = client.head("/api/fs/raw", params={"path": str(f)})
    assert ok.status_code == 200
    assert ok.headers["content-length"] == "5"
    gone = client.head("/api/fs/raw", params={"path": str(tmp_path / "nope")})
    assert gone.status_code == 404


def test_head_on_a_tcc_denied_stat_is_the_fda_403_not_a_404(tmp_path, monkeypatch):
    """HEAD shares the GET path's stat handling: EPERM is a refusal (and feeds
    the Full Disk Access nudge), never folded into "missing"."""
    from fused_render.server.routers import fs_read

    f = _write(tmp_path, "a.txt", "hello")
    seen = []
    real_stat = fs_read.os.stat

    def stat(p, *a, **k):
        if str(p) == str(f):
            raise PermissionError(1, "Operation not permitted")
        return real_stat(p, *a, **k)

    monkeypatch.setattr(fs_read.os, "stat", stat)
    monkeypatch.setattr(fs_read.shell_fda, "refused",
                        lambda path, e: seen.append(path) or
                        fs_read._error("fda", status=403))
    resp = _client(tmp_path).head("/api/fs/raw", params={"path": str(f)})
    assert resp.status_code == 403
    assert seen == [str(f)]

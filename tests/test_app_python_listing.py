"""`GET /api/apps/python` + `pyinspect` (SPEC §49): the AST-only listing a bot
reads before calling an app's `.py` through `/api/run`."""
import os
import sys

from fused_render import pyinspect


def _write(folder, name, text):
    with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def test_file_report_main_signature_and_doc(tmp_path):
    _write(tmp_path, "summary.py", (
        '"""Module doc line."""\n'
        "def main(month: str = '2026-09', top: int = 10, *, csv: str = ''):\n"
        '    """Totals per category. Reads csv; writes nothing."""\n'
        "    return {}\n"
        "def helper():\n    pass\n"
        "def _private():\n    pass\n"))
    rep = pyinspect.file_report(str(tmp_path), "summary.py")
    assert rep["callable"] is True
    assert rep["doc"] == "Totals per category. Reads csv; writes nothing."
    assert rep["signature"].startswith("main(month: str=")
    assert [p["name"] for p in rep["params"]] == ["month", "top", "csv"]
    assert rep["params"][0] == {"name": "month", "type": "str", "default": "'2026-09'", "required": False}
    assert rep["functions"] == ["main", "helper"]
    assert rep["error"] == ""


def test_file_report_without_main_is_not_callable(tmp_path):
    _write(tmp_path, "helpers.py", '"""Helpers."""\ndef parse(x):\n    return x\n')
    rep = pyinspect.file_report(str(tmp_path), "helpers.py")
    assert rep["callable"] is False
    assert rep["reason"] == "no top-level main()"
    assert rep["doc"] == "Helpers."
    assert rep["functions"] == ["parse"]


def test_file_report_syntax_error_is_reported_not_raised(tmp_path):
    _write(tmp_path, "broken.py", "def main(:\n")
    rep = pyinspect.file_report(str(tmp_path), "broken.py")
    assert rep["callable"] is False
    assert rep["error"].startswith("SyntaxError")


def test_listing_never_imports_the_file(tmp_path):
    marker = tmp_path / "ran.txt"
    _write(tmp_path, "trap.py", f"open({str(marker)!r}, 'w').write('x')\ndef main():\n    pass\n")
    rep = pyinspect.folder_report(str(tmp_path))
    assert rep[0]["callable"] is True
    assert not marker.exists()


def test_async_main_is_listed_not_callable(tmp_path):
    _write(tmp_path, "a.py", "async def main(x: int = 1):\n    return x\n")
    rep = pyinspect.file_report(str(tmp_path), "a.py")
    assert rep["callable"] is False and rep["reason"].startswith("async def main")
    assert rep["functions"] == ["main"]


def test_python_files_skips_hidden_and_non_py(tmp_path):
    for n in ("a.py", ".hidden.py", "b.txt", "c.py"):
        _write(tmp_path, n, "")
    assert pyinspect.python_files(str(tmp_path)) == ["a.py", "c.py"]


def test_route_requires_x_fused_and_lists(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from fused_render.server.app import create_app

    _write(tmp_path, "index.html", "<meta name='fused-app'>")
    _write(tmp_path, "x.py", "def main(a: int = 1):\n    return a\n")
    _write(tmp_path, "mcp.toml", '[[tool]]\nname = "run_x"\ndescription = "d"\nfile = "x.py"\n')
    c = TestClient(create_app())
    r = c.get("/api/apps/python", params={"html": str(tmp_path / "index.html")})
    assert r.status_code == 403
    r = c.get("/api/apps/python", params={"html": str(tmp_path / "index.html")}, headers={"X-Fused": "1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["app"] == os.path.realpath(str(tmp_path))
    assert body["entrypoint"] == "main" and body["timeout_s"] == 60
    assert [f["file"] for f in body["files"]] == ["x.py"]
    assert body["files"][0]["callable"] is True
    assert body["tools"] == [{"name": "run_x", "description": "d", "file": "x.py", "entrypoint": "main", "curated": True}]
    assert body["background"] is None
    r = c.get("/api/apps/python", params={"dir": str(tmp_path)}, headers={"X-Fused": "1"})
    assert r.status_code == 200 and r.json()["html"] == os.path.join(os.path.realpath(str(tmp_path)), "index.html")
    r = c.get("/api/apps/python", params={"dir": str(tmp_path / "nope")}, headers={"X-Fused": "1"})
    assert r.status_code == 404

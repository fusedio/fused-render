"""`fused.listDir(path)` — the page-side directory listing helper.

Pages had stat / readFile / writeFile / mkdir / uploadFile / rawUrl but no way
to list a folder, although the server has served GET /api/fs/list all along.
The helper is flat beside `stat` (no fused.fs.* namespace), runs the path
through the same snapshot rewrite gate, and rejects with the server's message
on a non-ok response. String-contract checks over the shipped runtime.js, in
the style of test_runtime_cancellation.py.
"""
from pathlib import Path

import fused_render

_STATIC = Path(fused_render.__file__).parent / "static"
RUNTIME = (_STATIC / "runtime.js").read_text(encoding="utf-8")


def test_runtime_defines_listdir():
    assert "function listDir(path)" in RUNTIME


def test_runtime_listdir_calls_fs_list():
    assert '"/api/fs/list?path=" + encodeURIComponent(target)' in RUNTIME


def test_runtime_listdir_is_exported_beside_stat():
    # Flat member of window.fused, right after stat — no fs.* namespace.
    assert "    stat,\n    listDir,\n" in RUNTIME


def test_runtime_listdir_rejects_with_server_error():
    body = RUNTIME[RUNTIME.index("function listDir(path)"):]
    body = body[: body.index("function readFile(path)")]
    assert 'throw new Error((data && data.error) || "HTTP " + res.status)' in body
    assert "snapshotReady.then" in body
    assert "rewritePath(path)" in body


def test_authoring_skill_documents_listdir():
    skill = (Path(__file__).resolve().parents[1]
             / "skills" / "fused-render-authoring" / "SKILL.md")
    assert "`await fused.listDir(path)`" in skill.read_text(encoding="utf-8")

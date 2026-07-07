"""Tests for `fused-render export` (fused_render/export.py)."""
import json
import os

import pytest

from fused_render.export import ExportError, export_page, plan_export


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_plan_collects_runpython_and_assets(tmp_path):
    html = """<!DOCTYPE html><html><head></head><body><script>
      fused.runPython("./sine.py", {n: "10"});
      const u = fused.rawUrl("./logo.png");
      const t = await fused.readFile('./notes.txt');
    </script></body></html>"""
    _write(tmp_path, "page.html", html)
    _write(tmp_path, "sine.py", "def main(n=1):\n    return {'n': n}\n")
    _write(tmp_path, "logo.png", "PNG")
    _write(tmp_path, "notes.txt", "hello")

    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert [(e.path, e.name, e.file) for e in plan.entrypoints] == [
        ("./sine.py", "sine", "code/sine.py")
    ]
    assert {a.name for a in plan.assets} == {"logo.png", "notes.txt"}


def test_dynamic_path_is_an_error(tmp_path):
    html = "<script>const p = './x.py'; fused.runPython(p, {});</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("non-literal" in e for e in plan.errors)


def test_unsupported_api_is_an_error(tmp_path):
    html = "<script>fused.writeFile('./x.txt', 'hi'); fused.stat('./y');</script>"
    plan = plan_export(html, str(tmp_path))
    assert sum("not supported on a hosted page" in e for e in plan.errors) == 2


def test_missing_target_is_an_error(tmp_path):
    html = "<script>fused.runPython('./missing.py', {});</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("not found" in e for e in plan.errors)


def test_absolute_and_escaping_paths_rejected(tmp_path):
    html = "<script>fused.rawUrl('/etc/passwd'); fused.runPython('../secret.py', {});</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("absolute" in e for e in plan.errors)
    assert any("escapes" in e for e in plan.errors)


def test_reserved_route_name_is_prefixed(tmp_path):
    html = "<script>fused.runPython('./data.py', {});</script>"
    _write(tmp_path, "data.py", "def main():\n    return 1\n")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    # "data" is a reserved serve route → prefixed so it can't collide.
    assert plan.entrypoints[0].name == "run-data"


def test_duplicate_stems_get_distinct_names(tmp_path):
    html = "<script>fused.runPython('./a/run.py',{}); fused.runPython('./b/run.py',{});</script>"
    _write(tmp_path, "a/run.py", "def main():\n    return 1\n")
    _write(tmp_path, "b/run.py", "def main():\n    return 2\n")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    names = [e.name for e in plan.entrypoints]
    assert names == ["run", "run-2"]


def test_export_page_writes_bundle(tmp_path):
    html = """<html><head></head><body><script>
      fused.runPython("./sine.py", {n: "10"});
      fused.rawUrl("./data/logo.png");
    </script></body></html>"""
    _write(tmp_path, "src/page.html", html)
    _write(tmp_path, "src/sine.py", "def main(n=1):\n    return {'n': n}\n")
    _write(tmp_path, "src/data/logo.png", "PNG")

    out = tmp_path / "bundle"
    plan = export_page(str(tmp_path / "src" / "page.html"), str(out))
    assert not plan.errors

    assert (out / "page.html").is_file()
    assert (out / "code" / "sine.py").is_file()
    assert (out / "assets" / "data/logo.png").is_file()

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["fused_render_bundle"] == 1
    assert manifest["page"] == "page.html"
    assert manifest["entrypoints"][0]["name"] == "sine"
    assert manifest["assets"][0]["name"] == "data/logo.png"


def test_export_page_raises_on_error(tmp_path):
    _write(tmp_path, "page.html", "<script>fused.runPython('./missing.py', {});</script>")
    with pytest.raises(ExportError) as ei:
        export_page(str(tmp_path / "page.html"), str(tmp_path / "out"))
    assert "not found" in str(ei.value)
    assert not (tmp_path / "out").exists()


def test_non_html_input_rejected(tmp_path):
    p = _write(tmp_path, "notes.txt", "hi")
    with pytest.raises(ExportError):
        export_page(str(p), str(tmp_path / "out"))


def test_equivalent_asset_literals_both_mapped(tmp_path):
    # `./logo.png` and `logo.png` normalize to the same key but are distinct literals —
    # both must appear in the manifest (the served runtime looks up by exact string).
    html = "<script>fused.rawUrl('./logo.png'); fused.rawUrl('logo.png');</script>"
    _write(tmp_path, "logo.png", "PNG")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    by_path = {a.path: a.name for a in plan.assets}
    assert by_path == {"./logo.png": "logo.png", "logo.png": "logo.png"}


def test_same_asset_literal_across_methods_deduped(tmp_path):
    # The same literal via rawUrl and readFile is one asset, not two.
    html = "<script>fused.rawUrl('./x.csv'); fused.readFile('./x.csv');</script>"
    _write(tmp_path, "x.csv", "a,b")
    plan = plan_export(html, str(tmp_path))
    assert [a.path for a in plan.assets] == ["./x.csv"]


def test_symlink_escaping_page_dir_rejected(tmp_path):
    import os

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("def main():\n    return 'leak'\n")
    page = tmp_path / "page"
    page.mkdir()
    # A symlink beside the page that lexically stays local but points outside the tree.
    try:
        os.symlink(outside / "secret.py", page / "linked.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    html = "<script>fused.runPython('./linked.py', {});</script>"
    plan = plan_export(html, str(page))
    assert any("outside the page directory" in e for e in plan.errors)

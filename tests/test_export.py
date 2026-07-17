"""Tests for the export logic (fused_render/export.py), served via POST /api/export."""
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
        ("./sine.py", "sine", "files/sine.py")
    ]
    assert {a.name for a in plan.assets} == {"logo.png", "notes.txt"}


def test_dynamic_path_is_an_error(tmp_path):
    html = "<script>const p = './x.py'; fused.runPython(p, {});</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("non-literal" in e for e in plan.errors)


def test_dynamic_asset_path_is_a_warning_not_an_error(tmp_path):
    # A computed rawUrl/readFile path can't be resolved, but the user can bundle
    # its target via include — so it warns rather than blocking the deploy.
    html = "<script>const z = 2; fused.rawUrl(`./tiles/${z}.png`); fused.readFile(u);</script>"
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert any("computed path" in w for w in plan.warnings)


def test_include_bundles_an_unreferenced_file(tmp_path):
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "sine.py", "def main():\n    return 1\n")
    _write(tmp_path, "data/points.csv", "a,b\n1,2\n")
    plan = plan_export(html, str(tmp_path), include=["data/points.csv"])
    assert not plan.errors
    assert [(a.path, a.name, a.file) for a in plan.assets] == [
        ("data/points.csv", "data/points.csv", "files/data/points.csv")
    ]


def test_include_missing_or_unsafe_file_is_an_error(tmp_path):
    plan = plan_export("<html></html>", str(tmp_path), include=["nope.csv", "/etc/passwd"])
    assert any("not found" in e for e in plan.errors)
    assert any("absolute" in e for e in plan.errors)


def test_include_of_runpython_target_is_not_duplicated(tmp_path):
    # A persisted include that names a file the page ALSO runs via runPython must
    # not bundle it twice (once as code/<name>.py, once as assets/<key>).
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "sine.py", "def main():\n    return 1\n")
    plan = plan_export(html, str(tmp_path), include=["sine.py"])
    assert not plan.errors
    assert [e.path for e in plan.entrypoints] == ["./sine.py"]
    assert plan.assets == []  # not also added as an asset


def test_include_of_referenced_file_dedups(tmp_path):
    html = "<script>fused.rawUrl('./logo.png');</script>"
    _write(tmp_path, "logo.png", "PNG")
    # Same file reached by both the scan and an include (spelled without "./").
    plan = plan_export(html, str(tmp_path), include=["logo.png"])
    assert not plan.errors
    assert [a.name for a in plan.assets] == ["logo.png"]  # bundled once


def test_exclude_drops_asset_and_warns_when_referenced(tmp_path):
    html = "<script>fused.rawUrl('./logo.png'); fused.rawUrl('./keep.png');</script>"
    _write(tmp_path, "logo.png", "PNG")
    _write(tmp_path, "keep.png", "PNG")
    plan = plan_export(html, str(tmp_path), exclude=["./logo.png"])
    assert not plan.errors
    assert [a.name for a in plan.assets] == ["keep.png"]
    assert any("logo.png" in w for w in plan.warnings)


def test_exclude_drops_entrypoint_and_warns(tmp_path):
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "sine.py", "def main():\n    return 1\n")
    plan = plan_export(html, str(tmp_path), exclude=["./sine.py"])
    assert plan.entrypoints == []
    assert any("sine.py" in w and "runPython" in w for w in plan.warnings)


def test_exclude_drops_manual_include_silently(tmp_path):
    _write(tmp_path, "data.csv", "a,b\n1,2\n")
    plan = plan_export(
        "<html></html>", str(tmp_path), include=["data.csv"], exclude=["data.csv"]
    )
    assert not plan.errors
    assert plan.assets == []
    assert plan.warnings == []  # dropping an unreferenced include is not warned


def test_unsupported_api_is_an_error(tmp_path):
    html = "<script>fused.writeFile('./x.txt', 'hi'); fused.stat('./y');</script>"
    plan = plan_export(html, str(tmp_path))
    assert sum("not supported on a hosted page" in e for e in plan.errors) == 2


def test_space_before_call_parens_is_still_scanned(tmp_path):
    # `fused.runPython (...)` is valid JS a page author could write — it must
    # not silently vanish from the export (no bundle entry, no error either).
    html = "<script>fused.runPython (\"./sine.py\", {});</script>"
    _write(tmp_path, "sine.py", "def main():\n    return 1\n")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert [e.path for e in plan.entrypoints] == ["./sine.py"]


def test_space_before_call_parens_dynamic_path_still_an_error(tmp_path):
    html = "<script>const p = './x.py'; fused.runPython (p, {});</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("non-literal" in e for e in plan.errors)


def test_space_before_call_parens_unsupported_api_still_an_error(tmp_path):
    html = "<script>fused.writeFile ('./x.txt', 'hi');</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("not supported on a hosted page" in e for e in plan.errors)


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


def test_host_internal_stem_is_prefixed(tmp_path):
    # "_shell.py" slugifies to "shell" (leading "_" is stripped, not preserved),
    # so the reserved check must match slugified reserved names, not the
    # literal "_shell" string, or this host-internal route name leaks through.
    html = "<script>fused.runPython('./_shell.py', {});</script>"
    _write(tmp_path, "_shell.py", "def main():\n    return 1\n")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert plan.entrypoints[0].name == "run-shell"


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

    # v2: one payload dir mirroring the page's folder; no code/ /assets/ category dirs.
    assert (out / "files" / "page.html").is_file()
    assert (out / "files" / "sine.py").is_file()
    assert (out / "files" / "data/logo.png").is_file()

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["fused_render_bundle"] == 2
    assert manifest["root"] == "files"
    assert manifest["page"] == "page.html"
    assert manifest["entrypoints"][0] == {"path": "./sine.py", "name": "sine", "key": "sine.py"}
    assert manifest["assets"][0] == {"path": "./data/logo.png", "name": "data/logo.png"}


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


def test_reexport_clears_stale_bundle_files(tmp_path):
    # Re-exporting after removing a dependency must not leave the old files
    # in code/ or assets/ — a stale orphan beside a fresh manifest reads as
    # part of the bundle.
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("def main():\n    return 1\n")
    (src / "b.py").write_text("def main():\n    return 2\n")
    (src / "page.html").write_text(
        "<script>fused.runPython('./a.py',{}); fused.runPython('./b.py',{});</script>"
    )
    out = tmp_path / "bundle"
    export_page(str(src / "page.html"), str(out))
    assert (out / "files" / "b.py").is_file()

    (src / "page.html").write_text("<script>fused.runPython('./a.py',{});</script>")
    export_page(str(src / "page.html"), str(out))
    assert (out / "files" / "a.py").is_file()
    assert not (out / "files" / "b.py").exists()  # stale entry cleared
    manifest = json.loads((out / "manifest.json").read_text())
    assert [e["name"] for e in manifest["entrypoints"]] == ["a"]


def test_dotfile_asset_key_not_mangled(tmp_path):
    # lstrip("./") would strip the leading dot of a dotfile; keys must keep it.
    html = "<script>fused.rawUrl('./.data.bin');</script>"
    _write(tmp_path, ".data.bin", "X")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert plan.assets[0].name == ".data.bin"
    assert plan.assets[0].file == "files/.data.bin"


def test_discovers_imported_sibling_module(tmp_path):
    # A first-party module a bundled entrypoint imports is auto-bundled as a resource, so
    # `import helpers` resolves on the hosted page without the author hand-listing it.
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "sine.py", "import helpers\n\ndef main():\n    return helpers.go()\n")
    _write(tmp_path, "helpers.py", "def go():\n    return 1\n")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert [(r.key, r.file) for r in plan.resources] == [("helpers.py", "files/helpers.py")]


def test_transitive_module_discovery(tmp_path):
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "sine.py", "from a import x\n\ndef main():\n    return x\n")
    _write(tmp_path, "a.py", "import b\nx = b.y\n")
    _write(tmp_path, "b.py", "y = 2\n")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert {r.key for r in plan.resources} == {"a.py", "b.py"}


def test_stdlib_and_thirdparty_imports_not_bundled(tmp_path):
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "sine.py", "import os\nimport pandas\n\ndef main():\n    return os.getpid()\n")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert plan.resources == []  # no os.py / pandas.py beside the page — nothing to bundle


def test_relative_import_not_bundled(tmp_path):
    # A hosted entrypoint runs flattened with no package context, so a relative import
    # cannot resolve and is not bundled (only absolute sibling imports are).
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "sine.py", "from . import helpers\n\ndef main():\n    return 1\n")
    _write(tmp_path, "helpers.py", "def go():\n    return 1\n")
    plan = plan_export(html, str(tmp_path))
    assert plan.resources == []


def test_module_already_an_asset_not_duplicated(tmp_path):
    # A file both imported AND fetched (rawUrl) ships once, as an asset — assets already
    # land at the real key, so the import resolves without a second resource copy.
    html = "<script>fused.runPython('./sine.py', {}); fused.rawUrl('./helpers.py');</script>"
    _write(tmp_path, "sine.py", "import helpers\n\ndef main():\n    return 1\n")
    _write(tmp_path, "helpers.py", "def go():\n    return 1\n")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert [a.name for a in plan.assets] == ["helpers.py"]
    assert plan.resources == []


def test_exclude_module_warns(tmp_path):
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "sine.py", "import helpers\n\ndef main():\n    return 1\n")
    _write(tmp_path, "helpers.py", "def go():\n    return 1\n")
    plan = plan_export(html, str(tmp_path), exclude=["helpers.py"])
    assert plan.resources == []
    assert any("helpers.py" in w and "import" in w for w in plan.warnings)


def test_syntax_error_in_entrypoint_yields_no_resources(tmp_path):
    # A page .py that does not parse yields no discovered imports (its own error surfaces
    # at run time, not during the export scan).
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "sine.py", "def main(:\n    return 1\n")
    _write(tmp_path, "helpers.py", "x = 1\n")
    plan = plan_export(html, str(tmp_path))
    assert plan.resources == []


def test_export_page_writes_resources(tmp_path):
    html = "<script>fused.runPython('./sine.py', {});</script>"
    _write(tmp_path, "src/page.html", html)
    _write(tmp_path, "src/sine.py", "import helpers\n\ndef main():\n    return helpers.go()\n")
    _write(tmp_path, "src/helpers.py", "def go():\n    return 1\n")
    out = tmp_path / "bundle"
    plan = export_page(str(tmp_path / "src" / "page.html"), str(out))
    assert not plan.errors
    assert (out / "files" / "helpers.py").is_file()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["resources"] == [{"key": "helpers.py"}]


def test_reexport_sweeps_stale_v1_layout(tmp_path):
    # Re-exporting into an out dir that already holds a v1 bundle (root page.html +
    # code/ /assets/ /resources/) must sweep those away, not leave a mixed v1+v2 tree.
    src = tmp_path / "src"
    src.mkdir()
    (src / "page.html").write_text("<script>fused.runPython('./a.py',{});</script>")
    (src / "a.py").write_text("def main():\n    return 1\n")
    out = tmp_path / "bundle"
    # Simulate a stale v1 bundle already in the out dir.
    out.mkdir()
    (out / "page.html").write_text("stale")
    for d in ("code", "assets", "resources"):
        (out / d).mkdir()
        (out / d / "stale.txt").write_text("stale")

    export_page(str(src / "page.html"), str(out))

    assert (out / "files" / "page.html").is_file()  # v2 payload written
    assert not (out / "page.html").exists()  # stale v1 page swept
    for d in ("code", "assets", "resources"):
        assert not (out / d).exists()  # stale v1 category dirs swept


def test_reexport_clears_stale_resources(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "page.html").write_text("<script>fused.runPython('./sine.py', {});</script>")
    (src / "sine.py").write_text("import helpers\n\ndef main():\n    return 1\n")
    (src / "helpers.py").write_text("def go():\n    return 1\n")
    out = tmp_path / "bundle"
    export_page(str(src / "page.html"), str(out))
    assert (out / "files" / "helpers.py").is_file()

    # Drop the import; the stale resource must not linger beside the fresh manifest.
    (src / "sine.py").write_text("def main():\n    return 1\n")
    export_page(str(src / "page.html"), str(out))
    assert not (out / "files" / "helpers.py").exists()
    assert json.loads((out / "manifest.json").read_text())["resources"] == []


def test_symlink_escaping_page_dir_rejected(tmp_path):
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

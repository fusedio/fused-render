"""Tests for the export logic (fused_render/export.py), served via POST /api/export."""
import json
import os
import shutil

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
    # A literal rawUrl/readFile target is exposed via rawUrl/readFile — source "reference".
    assert {a.source for a in plan.assets} == {"reference"}


def test_asset_source_reflects_how_the_file_entered_the_bundle(tmp_path):
    # Three provenances land as three distinct `source` values so the Deploy modal's
    # list can say whether the page is known to fetch a file via rawUrl/readFile.
    html = (
        _manifest_block('{"include": ["data/*.geojson"]}')
        + "<script>fused.rawUrl('./logo.png'); const u = fused.rawUrl('data/' + n);</script>"
    )
    _write(tmp_path, "logo.png", "PNG")
    _write(tmp_path, "data/a.geojson", "{}")
    _write(tmp_path, "extra.csv", "a,b\n1,2\n")
    plan = plan_export(html, str(tmp_path), include=["extra.csv"])
    assert not plan.errors
    by_name = {a.name: a.source for a in plan.assets}
    assert by_name == {
        "logo.png": "reference",  # literal fused.rawUrl() target
        "data/a.geojson": "manifest",  # declared in the page's fused-bundle manifest
        "extra.csv": "include",  # added out-of-band via the caller's include
    }


def test_literal_reference_wins_source_over_manifest_and_include(tmp_path):
    # A file reachable more than one way is attributed to the strongest claim
    # (reference > manifest > include) and bundled once.
    html = (
        _manifest_block('{"include": ["logo.png"]}')
        + "<script>fused.rawUrl('./logo.png');</script>"
    )
    _write(tmp_path, "logo.png", "PNG")
    plan = plan_export(html, str(tmp_path), include=["logo.png"])
    assert not plan.errors
    assert [(a.name, a.source) for a in plan.assets] == [("logo.png", "reference")]


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


def test_export_into_page_dir_rejected(tmp_path):
    # Export is non-destructive → the out dir must be empty. The page's OWN folder is
    # non-empty, so exporting into it is refused (never delete the author's files).
    _write(tmp_path, "page.html", "<script>fused.runPython('./a.py', {});</script>")
    _write(tmp_path, "a.py", "def main():\n    return 1\n")
    with pytest.raises(ExportError, match="must be empty"):
        export_page(str(tmp_path / "page.html"), str(tmp_path))
    assert (tmp_path / "a.py").read_text() == "def main():\n    return 1\n"  # untouched


def test_export_into_ancestor_dir_rejected(tmp_path):
    # An ANCESTOR of the page folder is non-empty (it holds the page subfolder + sibling
    # author files), so it is refused — nothing outside the bundle is ever deleted.
    _write(tmp_path, "sub/page.html", "<script>fused.runPython('./a.py', {});</script>")
    _write(tmp_path, "sub/a.py", "def main():\n    return 1\n")
    _write(tmp_path, "files/keep.txt", "author data")  # a sibling that must not be touched
    with pytest.raises(ExportError, match="must be empty"):
        export_page(str(tmp_path / "sub" / "page.html"), str(tmp_path))
    assert (tmp_path / "files" / "keep.txt").read_text() == "author data"  # untouched


def test_export_into_nonempty_dir_rejected(tmp_path):
    # ANY non-empty directory is refused (even one that looks like a prior bundle) — export
    # never clobbers existing content.
    _write(tmp_path, "src/page.html", "<html></html>")
    out = tmp_path / "out"
    _write(out, "important.txt", "do not delete")
    with pytest.raises(ExportError, match="must be empty"):
        export_page(str(tmp_path / "src" / "page.html"), str(out))
    assert (out / "important.txt").read_text() == "do not delete"


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


def test_reexport_into_same_dir_rejected(tmp_path):
    # Export is non-destructive: a second export into the same (now non-empty) dir is
    # refused, rather than clearing/overwriting the prior bundle. Re-export targets a fresh
    # dir (Deploy always uses a new temp dir).
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("def main():\n    return 1\n")
    (src / "page.html").write_text("<script>fused.runPython('./a.py',{});</script>")
    out = tmp_path / "bundle"
    export_page(str(src / "page.html"), str(out))
    assert (out / "files" / "a.py").is_file()

    with pytest.raises(ExportError, match="must be empty"):
        export_page(str(src / "page.html"), str(out))


def test_failed_export_leaves_out_clean_so_retry_works(tmp_path, monkeypatch):
    # The bundle is built in a temp dir and swapped in with one atomic rename, so a failure
    # mid-build never leaves a partial out dir — out_dir stays absent/empty and a retry to the
    # same path is not blocked by the empty-dir check.
    _write(tmp_path, "src/page.html", "<script>fused.runPython('./a.py', {});</script>")
    _write(tmp_path, "src/a.py", "def main():\n    return 1\n")
    out = tmp_path / "bundle"

    real_copyfile = shutil.copyfile

    def boom(src, dst):  # fail the very first file copy (simulates a disk-full / crash)
        raise OSError("simulated mid-export failure")

    monkeypatch.setattr("fused_render.export.shutil.copyfile", boom)
    with pytest.raises(OSError, match="simulated mid-export failure"):
        export_page(str(tmp_path / "src" / "page.html"), str(out))
    # out_dir was never populated — absent, or empty if the caller pre-created it.
    assert not out.exists() or not any(out.iterdir())

    monkeypatch.setattr("fused_render.export.shutil.copyfile", real_copyfile)
    plan = export_page(str(tmp_path / "src" / "page.html"), str(out))  # retry succeeds
    assert not plan.errors
    assert (out / "files" / "a.py").is_file()


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


def test_computed_prefix_rawurl_is_dynamic_not_a_literal(tmp_path):
    # `fused.rawUrl("data/" + name)` must NOT be collected as a literal asset named
    # "data/" — the string is only a prefix of a computed expression, so it counts as a
    # (warned) computed path instead.
    html = "<script>const u = fused.rawUrl('data/' + name);</script>"
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors  # no bogus "data/ not found"
    assert not plan.assets
    assert any("computed path" in w for w in plan.warnings)


def test_computed_prefix_runpython_is_dynamic_error(tmp_path):
    # The same for runPython, where a computed target is a hard error (route name derives
    # from the literal path).
    html = "<script>fused.runPython('./run_' + kind + '.py', {});</script>"
    plan = plan_export(html, str(tmp_path))
    assert any("computed" in e for e in plan.errors)


def test_computed_path_with_trailing_literal_not_miscollected(tmp_path):
    # The scanner body must not stretch across an inner quote to a LATER one: forms like
    # "data/" + name + ".json" or "data/" + foo("x") are computed, not a garbage literal
    # ('data/" + name + ".json') that would 404 as a missing file (Bugbot, fused-render#184).
    for expr in ('"data/" + name + ".json"', '"data/" + foo("x")', '"a" + b + "c"'):
        html = f"<script>const u = fused.rawUrl({expr});</script>"
        plan = plan_export(html, str(tmp_path))
        assert not plan.errors, f"{expr} produced errors: {plan.errors}"
        assert not plan.assets, f"{expr} was mis-collected as an asset: {plan.assets}"
        assert any("computed path" in w for w in plan.warnings)


def test_computed_prefix_runpython_with_trailing_literal_is_error(tmp_path):
    # runPython variant of the same over-match: computed, so a hard error — never a
    # bogus literal target that quietly resolves to the wrong route.
    html = '<script>fused.runPython("./run_" + kind + ".py", {});</script>'
    plan = plan_export(html, str(tmp_path))
    assert any("computed" in e for e in plan.errors)
    assert not plan.entrypoints


def test_literal_path_may_contain_opposite_quote(tmp_path):
    # A double-quoted literal may contain an apostrophe (and vice-versa) — the body only
    # excludes its own delimiter, so this stays a real literal, not a computed path.
    html = "<script>fused.rawUrl(\"it's.png\");</script>"
    _write(tmp_path, "it's.png", "PNG")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert [a.name for a in plan.assets] == ["it's.png"]


# --- embedded bundle manifest (<script type="application/fused-bundle">) --------------


def _manifest_block(obj_json):
    return f'<script type="application/fused-bundle">{obj_json}</script>'


def test_manifest_glob_bundles_matching_files(tmp_path):
    # A glob in the embedded manifest bundles every match as a read-only asset, even
    # though no literal rawUrl names them — this is what lets a computed rawUrl resolve.
    html = _manifest_block('{"include": ["data/*.json"]}') + "<script>const u = fused.rawUrl('data/' + name);</script>"
    _write(tmp_path, "data/a.json", "1")
    _write(tmp_path, "data/b.json", "2")
    _write(tmp_path, "data/notes.txt", "skip")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert {a.name for a in plan.assets} == {"data/a.json", "data/b.json"}
    # The computed rawUrl call is still an advisory warning, now mentioning the manifest.
    assert any("computed path" in w for w in plan.warnings)


def test_manifest_recursive_glob(tmp_path):
    html = _manifest_block('{"include": ["tiles/**/*.png"]}')
    _write(tmp_path, "tiles/0/0.png", "P")
    _write(tmp_path, "tiles/1/2/3.png", "P")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert {a.name for a in plan.assets} == {"tiles/0/0.png", "tiles/1/2/3.png"}


def test_manifest_literal_include_missing_is_error(tmp_path):
    # A literal (non-glob) manifest entry that isn't on disk is a blocking error,
    # exactly like an explicit /api/export include.
    html = _manifest_block('{"include": ["data/only.json"]}')
    plan = plan_export(html, str(tmp_path))
    assert any("not found" in e for e in plan.errors)


def test_manifest_zero_match_glob_warns_not_errors(tmp_path):
    html = _manifest_block('{"include": ["data/*.json"]}')
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert any("matched no files" in w for w in plan.warnings)


def test_manifest_bracket_filename_is_literal_not_glob(tmp_path):
    # A real filename with brackets (e.g. a browser "file[1].json" download) has no */?,
    # so it's treated as a literal include and bundled — not globbed as a character class
    # (which would match nothing and silently drop the file).
    html = _manifest_block('{"include": ["data/file[1].json"]}')
    _write(tmp_path, "data/file[1].json", "1")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert [a.name for a in plan.assets] == ["data/file[1].json"]


def test_manifest_missing_bracket_literal_is_error_not_warning(tmp_path):
    # And when that literal is absent, it's the blocking missing-literal error (a literal),
    # not a zero-match glob warning.
    html = _manifest_block('{"include": ["data/file[1].json"]}')
    plan = plan_export(html, str(tmp_path))
    assert any("not found" in e for e in plan.errors)
    assert not any("matched no files" in w for w in plan.warnings)


def test_manifest_glob_does_not_follow_directory_symlinks(tmp_path):
    # A `**` glob must not traverse a directory symlink out of the page tree: it would
    # scan/hang on an external tree and turn an out-of-tree file into a blocking error.
    # Only the in-tree file matches; the symlinked subtree is not walked.
    page = tmp_path / "page"
    (page / "data").mkdir(parents=True)
    (page / "data" / "real.json").write_text("1")
    external = tmp_path / "external" / "deep"
    external.mkdir(parents=True)
    (external / "secret.json").write_text("leak")
    try:
        os.symlink(tmp_path / "external", page / "data" / "link")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    html = _manifest_block('{"include": ["data/**/*.json"]}')
    plan = plan_export(html, str(page))
    assert not plan.errors  # no "escapes the page directory" from a symlink-traversed file
    assert {a.name for a in plan.assets} == {"data/real.json"}


def test_manifest_block_stripped_before_scan(tmp_path):
    # A value inside the manifest that LOOKS like an unsupported call must not trip the
    # dependency scan — the block is removed before scanning.
    html = _manifest_block('{"include": ["writeFile-samples/*.json"], "note": "fused.writeFile( decoy"}')
    _write(tmp_path, "writeFile-samples/x.json", "1")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors  # the decoy text inside the manifest did not become an error
    assert {a.name for a in plan.assets} == {"writeFile-samples/x.json"}


def test_manifest_exclude_key_is_ignored_with_warning(tmp_path):
    html = _manifest_block('{"include": ["data/*.json"], "exclude": ["data/secret.json"]}')
    _write(tmp_path, "data/a.json", "1")
    _write(tmp_path, "data/secret.json", "2")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    # exclude is NOT applied (both files bundled) and the user is warned it was ignored.
    assert {a.name for a in plan.assets} == {"data/a.json", "data/secret.json"}
    assert any("'exclude' is ignored" in w for w in plan.warnings)


def test_manifest_unknown_key_ignored_forward_compat(tmp_path):
    # Forward-lenient: an unknown future directive does not break an older exporter.
    html = _manifest_block('{"include": ["data/a.json"], "futureThing": {"x": 1}}')
    _write(tmp_path, "data/a.json", "1")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert {a.name for a in plan.assets} == {"data/a.json"}


def test_manifest_malformed_json_is_error(tmp_path):
    html = _manifest_block('{"include": [')
    plan = plan_export(html, str(tmp_path))
    assert any("not valid JSON" in e for e in plan.errors)


def test_manifest_multiple_blocks_is_error(tmp_path):
    html = _manifest_block('{"include": []}') + _manifest_block('{"include": []}')
    plan = plan_export(html, str(tmp_path))
    assert any("at most one" in e for e in plan.errors)


def test_manifest_absolute_glob_rejected_before_expansion(tmp_path):
    # An absolute glob pattern must be rejected with the same "absolute" error a literal
    # gets — and NOT handed to glob.glob (which would walk from the filesystem root).
    html = _manifest_block('{"include": ["/etc/*.conf"]}')
    plan = plan_export(html, str(tmp_path))
    assert any("absolute" in e for e in plan.errors)
    assert not plan.assets


def test_manifest_escaping_glob_rejected_before_expansion(tmp_path):
    # A `..` glob pattern must be rejected up front (not walked outside the page tree).
    (tmp_path / "page").mkdir()
    (tmp_path / "sibling.json").write_text("leak")
    html = _manifest_block('{"include": ["../*.json"]}')
    plan = plan_export(html, str(tmp_path / "page"))
    assert any("escapes" in e for e in plan.errors)
    assert not plan.assets


def test_manifest_glob_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text("leak")
    page = tmp_path / "page"
    (page / "data").mkdir(parents=True)
    try:
        os.symlink(outside / "secret.json", page / "data" / "linked.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    html = _manifest_block('{"include": ["data/*.json"]}')
    plan = plan_export(html, str(page))
    # The glob matched the symlink, but the safety gauntlet rejects the escape.
    assert any("outside the page directory" in e for e in plan.errors)


def test_manifest_include_deduped_with_literal_asset(tmp_path):
    # A file the manifest globs AND the page references literally is bundled once.
    html = _manifest_block('{"include": ["data/a.json"]}') + "<script>fused.rawUrl('data/a.json');</script>"
    _write(tmp_path, "data/a.json", "1")
    plan = plan_export(html, str(tmp_path))
    assert not plan.errors
    assert [a.name for a in plan.assets] == ["data/a.json"]


def test_manifest_include_bundled_through_export_page(tmp_path):
    html = _manifest_block('{"include": ["data/*.json"]}') + "<script>const u = fused.rawUrl('data/' + n);</script>"
    _write(tmp_path, "src/page.html", html)
    _write(tmp_path, "src/data/a.json", "1")
    _write(tmp_path, "src/data/b.json", "2")
    out = tmp_path / "bundle"
    plan = export_page(str(tmp_path / "src" / "page.html"), str(out))
    assert not plan.errors
    assert (out / "files" / "data/a.json").is_file()  # v2 payload dir
    assert (out / "files" / "data/b.json").is_file()
    manifest = json.loads((out / "manifest.json").read_text())
    assert {a["name"] for a in manifest["assets"]} == {"data/a.json", "data/b.json"}

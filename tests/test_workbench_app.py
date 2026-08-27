from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

from fused_render.export import ExportError
from fused_render.workbench_app import compile_workbench_app, write_compiled_canvas


# How the generated UDFs are exercised here, and why.
#
# The plane runs one whole source file per UDF: `canvas push` zips the canvas
# directory, the server stores the ENTIRE file as the UDF's `code`
# (server/svc/code_proxy/shared.py::hydrate_udf_code_and_headers_from_files),
# and execution exec()s all of it and then calls the `udf` entrypoint
# (fused/_udf/compile_v2.py::compile_udf_and_run_v3).  Module-level constants
# are in scope; whatever the entrypoint returns goes to job2's serializer.
#
# `_load` reproduces exactly that: exec the whole file, call the entrypoint,
# look at the raw return value.  `fused` is stubbed during the exec because the
# real decorator would hand back a Udf object whose call goes over the network
# to a live realtime instance — a unit test must not do that.  The stub defines
# `udf` and NOTHING else, which is the point: an earlier revision faked
# `fused.HTMLResponse`, `fused.Response` and `fused._registered_udfs`, so the
# suite stayed green over generated code that could not run on Workbench at
# all.  test_real_fused_wheel_has_no_response_helpers pins that the stub is not
# lying about the wheel's surface, and _load asserts the module never reaches
# for a `fused` attribute the wheel lacks.


class _StubFused(types.ModuleType):
    """`fused` as the wheel really is, for the two names generated code uses."""

    def __init__(self) -> None:
        super().__init__("fused")

    @staticmethod
    def udf(fn):
        # The real decorator returns a Udf object; generated code only ever
        # uses the decorator as a marker, and never calls back into it.
        return fn

    def __getattr__(self, name: str):
        # AttributeError, not a louder failure: it is what the real module
        # raises, so a defensive getattr(fused, name, default) in generated
        # code behaves here exactly as it does on the plane.
        raise AttributeError(f"module 'fused' has no attribute {name!r}")


def _app(tmp_path: Path) -> Path:
    (tmp_path / "calc_helpers.py").write_text("def twice(x):\n    return x * 2\n")
    (tmp_path / "calc.py").write_text(
        "from calc_helpers import twice\n\n"
        "def main(value: int = 2):\n"
        "    return {'answer': twice(value)}\n"
    )
    (tmp_path / "note.txt").write_text("hello from an asset\n")
    page = tmp_path / "index.html"
    page.write_text(
        """<!doctype html><html><head><meta name="fused-app"><title>Demo</title></head>
<body><script>
fused.runPython("./calc.py", {value: 4});
fused.readFile("./note.txt");
</script></body></html>"""
    )
    return page


def _load(compiled, name: str, monkeypatch: pytest.MonkeyPatch):
    """Exec one generated UDF file whole and hand back its entrypoint."""
    monkeypatch.setitem(sys.modules, "fused", _StubFused())
    namespace: dict = {}
    exec(compile(compiled.files[name], f"<{name}>", "exec"), namespace)
    return namespace["udf"]


def test_real_fused_wheel_has_no_response_helpers():
    """The guard behind _StubFused: generated code may rely on none of these.

    `fused.HTMLResponse`/`Response`/`PlainTextResponse` and
    `fused._registered_udfs` live only in the openfused serve-plane shim
    (fused/agent_core/backends/aws/handler/fused.py), which is the plane this
    deployment path deliberately bypasses.
    """
    import fused

    for name in ("HTMLResponse", "Response", "PlainTextResponse", "_registered_udfs"):
        assert not hasattr(fused, name), f"the wheel now has fused.{name}"


def test_compile_generates_valid_dedicated_canvas(tmp_path):
    page = _app(tmp_path)
    compiled = compile_workbench_app(str(page), "My_app", cache_max_age="5m")
    output = tmp_path / "generated"
    write_compiled_canvas(compiled, str(output))

    from fused.workbench.cli.canvas_validate import validate_canvas_dir

    errors = validate_canvas_dir(output)
    assert [error for error in errors if error.severity == "error"] == []
    manifest = (output / "canvas.toml").read_text()
    assert f'udfName = "{compiled.shell_slug}"' in manifest
    assert manifest.count("visible = false") == 2
    assert set(compiled.entrypoints) == {"./calc.py"}
    assert compiled.assets == {"./note.txt": "note.txt"}
    assert "cacheMaxAge\":300" in (output / f"{compiled.shell_slug}.py").read_text()


def test_generated_run_route_answers_json(tmp_path, monkeypatch):
    """job2 zips a bare dict return, so the wrapper must hand back real JSON."""
    compiled = compile_workbench_app(str(_app(tmp_path)), "Executable")
    run_slug = compiled.entrypoints["./calc.py"]
    udf = _load(compiled, f"{run_slug}.py", monkeypatch)

    response = udf(value="7")
    assert response.media_type == "application/json"
    assert json.loads(response.body) == {"answer": 14}


def test_generated_shell_route_returns_html_as_a_str(tmp_path, monkeypatch):
    """A str return is what job2 serializes as text/html; charset=utf-8."""
    compiled = compile_workbench_app(str(_app(tmp_path)), "Executable")
    udf = _load(compiled, f"{compiled.shell_slug}.py", monkeypatch)

    body = udf()
    assert isinstance(body, str)
    assert "window.__FUSED_RENDER_WORKBENCH__" in body
    assert "window.fused" in body


def test_generated_asset_route_serves_bytes(tmp_path, monkeypatch):
    compiled = compile_workbench_app(str(_app(tmp_path)), "Executable")
    asset_name = next(name for name in compiled.files if "_asset_" in name)
    udf = _load(compiled, asset_name, monkeypatch)

    asset = udf(name="note.txt")
    assert asset.body == b"hello from an asset\n"
    assert asset.media_type == "text/plain"
    # job2 forwards body + media_type only, so a miss cannot answer 404.
    missing = udf(name="missing")
    assert missing.body == b"asset not found"
    assert missing.media_type == "text/plain"


def test_materialize_publishes_atomically_and_leaves_no_staging(tmp_path, monkeypatch):
    """Concurrent cold starts must never read a half-extracted tree.

    Every route of an app shares one digest and one archive, and each route is
    its own process, so the first load of an app with two Python routes has
    them racing on the same directory. Extraction happens in a private staging
    dir that is renamed into place whole; the published path is never written
    into.
    """
    import tempfile
    import threading

    scratch = tmp_path / "tmpdir"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    compiled = compile_workbench_app(str(_app(tmp_path)), "Concurrent")
    run_slug = compiled.entrypoints["./calc.py"]
    udf = _load(compiled, f"{run_slug}.py", monkeypatch)

    results: list = []
    errors: list = []

    def call(value: int) -> None:
        try:
            results.append(json.loads(udf(value=str(value)).body))
        except Exception as exc:  # pragma: no cover - only on a real race
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(item["answer"] for item in results) == [0, 2, 4, 6, 8, 10, 12, 14]

    # One published tree, and nothing half-built left beside it.
    published = scratch / "fused-render-workbench"
    assert [entry.name for entry in published.iterdir()] == [compiled.digest]


def test_materialize_yields_to_a_tree_that_is_already_published(tmp_path, monkeypatch):
    """Losing the rename race is not an error: the trees are identical."""
    import tempfile

    scratch = tmp_path / "tmpdir"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    compiled = compile_workbench_app(str(_app(tmp_path)), "Winner")
    run_slug = compiled.entrypoints["./calc.py"]
    udf = _load(compiled, f"{run_slug}.py", monkeypatch)

    # Stand in for the process that got there first.
    published = scratch / "fused-render-workbench" / compiled.digest
    published.mkdir(parents=True)
    (published / "calc.py").write_text("def main(value: int = 2):\n    return {'answer': 99}\n")

    assert json.loads(udf(value="7").body) == {"answer": 99}
    assert [entry.name for entry in published.parent.iterdir()] == [compiled.digest]


def test_compile_is_deterministic(tmp_path):
    page = _app(tmp_path)
    first = compile_workbench_app(str(page), "Stable")
    second = compile_workbench_app(str(page), "Stable")
    assert first.digest == second.digest
    assert first.files == second.files


def test_generated_route_dispatches_decorated_udf(tmp_path, monkeypatch):
    """A decorated function carries the app's own name, never `udf`.

    The plane's entrypoint must be named `udf` and the generated wrapper is;
    the app's own .py is loaded by that wrapper, so its function is named
    whatever the author called it — which is what local accepts too.
    """
    (tmp_path / "decorated.py").write_text(
        "import fused\n\n"
        "@fused.udf\n"
        "def calculate(value: int):\n"
        "    return value + 1\n"
    )
    page = tmp_path / "index.html"
    page.write_text('<script>fused.runPython("./decorated.py", {})</script>')
    compiled = compile_workbench_app(str(page), "Decorated")
    run_slug = compiled.entrypoints["./decorated.py"]
    udf = _load(compiled, f"{run_slug}.py", monkeypatch)

    response = udf(value="8")
    assert json.loads(response.body) == 9


def test_long_route_names_do_not_collide_on_one_udf(tmp_path):
    """The readable tail is truncated; the slug must stay unique regardless."""
    stem = "a" * 80
    for suffix in ("alpha", "beta"):
        (tmp_path / f"{stem}_{suffix}.py").write_text("def main():\n    return 1\n")
    page = tmp_path / "index.html"
    page.write_text(
        "<script>"
        f'fused.runPython("./{stem}_alpha.py", {{}});'
        f'fused.runPython("./{stem}_beta.py", {{}});'
        "</script>"
    )
    compiled = compile_workbench_app(str(page), "Collide")

    slugs = list(compiled.entrypoints.values())
    assert len(slugs) == 2
    assert len(set(slugs)) == 2, f"two entrypoints share one UDF name: {slugs}"
    assert all(len(slug) <= 96 for slug in slugs)
    for slug in slugs:
        assert f"{slug}.py" in compiled.files


def test_compile_keeps_export_blockers(tmp_path):
    page = tmp_path / "index.html"
    page.write_text('<meta name="fused-app"><script>fused.writeFile("x", "y")</script>')
    with pytest.raises(ExportError, match=r"fused\.writeFile"):
        compile_workbench_app(str(page), "Unsupported")


@pytest.mark.skipif(
    os.environ.get("FUSED_RENDER_LIVE_WORKBENCH") != "1",
    reason="runs generated UDFs on a live realtime instance; set FUSED_RENDER_LIVE_WORKBENCH=1",
)
def test_generated_routes_run_on_a_live_realtime_instance(tmp_path):
    """End-to-end against the real plane — no canvas is pushed or shared.

    fused.load() carries the whole file as the UDF code, which is what the
    plane stores, so calling it exercises the real executor and job2's real
    serializer.  Note the Python client cannot deserialize a text/plain body
    (it falls back to parquet), so the asset route is not checked here; a
    browser reads those bytes directly.
    """
    import fused

    compiled = compile_workbench_app(str(_app(tmp_path)), "LiveCheck")
    staged = tmp_path / "live"
    write_compiled_canvas(compiled, str(staged))

    shell = fused.load(str(staged / f"{compiled.shell_slug}.py"))()
    assert isinstance(shell, str) and "window.__FUSED_RENDER_WORKBENCH__" in shell

    run_slug = compiled.entrypoints["./calc.py"]
    assert fused.load(str(staged / f"{run_slug}.py"))(value="7") == {"answer": 14}

    # The decorated-UDF path against the REAL wheel, where @fused.udf returns a
    # Udf recorded in no registry — the case the capture decorator exists for.
    (tmp_path / "decorated.py").write_text(
        "import fused\n\n"
        "@fused.udf\n"
        "def calculate(value: int):\n"
        "    return value + 1\n"
    )
    decorated_page = tmp_path / "decorated.html"
    decorated_page.write_text('<script>fused.runPython("./decorated.py", {})</script>')
    decorated = compile_workbench_app(str(decorated_page), "LiveDecorated")
    staged_decorated = tmp_path / "live_decorated"
    write_compiled_canvas(decorated, str(staged_decorated))
    slug = decorated.entrypoints["./decorated.py"]
    assert fused.load(str(staged_decorated / f"{slug}.py"))(value="8") == 9

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from fused_render.export import ExportError
from fused_render.workbench_app import compile_workbench_app, write_compiled_canvas


class _Udf:
    def __init__(self, fn, registry):
        self._fn = fn
        registry.append(self)

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _Response:
    def __init__(self, body, *, media_type, status_code=200, headers=None):
        self.body = body
        self.media_type = media_type
        self.status_code = status_code
        self.headers = headers


def _fake_fused() -> types.ModuleType:
    module = types.ModuleType("fused")
    module._registered_udfs = []

    def udf(fn):
        return _Udf(fn, module._registered_udfs)

    module.udf = udf
    module.Response = _Response
    module.HTMLResponse = lambda body, **kwargs: _Response(body, media_type="text/html", **kwargs)
    module.PlainTextResponse = lambda body, **kwargs: _Response(
        body, media_type="text/plain", **kwargs
    )
    return module


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


def _exec_source(source: bytes, monkeypatch: pytest.MonkeyPatch):
    fake = _fake_fused()
    monkeypatch.setitem(sys.modules, "fused", fake)
    namespace: dict = {}
    exec(compile(source, "<generated>", "exec"), namespace)
    return namespace, fake


def test_compile_generates_valid_dedicated_canvas(tmp_path, monkeypatch):
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


def test_generated_routes_execute_main_shell_and_asset(tmp_path, monkeypatch):
    compiled = compile_workbench_app(str(_app(tmp_path)), "Executable")

    run_slug = compiled.entrypoints["./calc.py"]
    run_ns, fake = _exec_source(compiled.files[f"{run_slug}.py"], monkeypatch)
    assert run_ns["udf"](value="7") == {"answer": 14}
    # The nested page module must not leak its decorated UDF registrations into a warm worker.
    assert len(fake._registered_udfs) == 1

    shell_ns, _ = _exec_source(compiled.files[f"{compiled.shell_slug}.py"], monkeypatch)
    shell = shell_ns["udf"]()
    assert shell.media_type == "text/html"
    assert "window.__FUSED_RENDER_WORKBENCH__" in shell.body
    assert "window.fused" in shell.body

    asset_name = next(name for name in compiled.files if "_asset_" in name)
    asset_ns, _ = _exec_source(compiled.files[asset_name], monkeypatch)
    asset = asset_ns["udf"](name="note.txt")
    assert asset.body == b"hello from an asset\n"
    assert asset.media_type == "text/plain"
    missing = asset_ns["udf"](name="missing")
    assert missing.status_code == 404


def test_compile_is_deterministic(tmp_path):
    page = _app(tmp_path)
    first = compile_workbench_app(str(page), "Stable")
    second = compile_workbench_app(str(page), "Stable")
    assert first.digest == second.digest
    assert first.files == second.files


def test_generated_route_dispatches_decorated_udf(tmp_path, monkeypatch):
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
    namespace, fake = _exec_source(compiled.files[f"{run_slug}.py"], monkeypatch)

    assert namespace["udf"](value="8") == 9
    assert len(fake._registered_udfs) == 1


def test_compile_keeps_export_blockers(tmp_path):
    page = tmp_path / "index.html"
    page.write_text('<meta name="fused-app"><script>fused.writeFile("x", "y")</script>')
    with pytest.raises(ExportError, match=r"fused\.writeFile"):
        compile_workbench_app(str(page), "Unsupported")

"""Tests for the api/template.html inspector (static AST parsing only)."""
import importlib.util
import os

import pytest

_PATH = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "api", "inspector.py"
)
_spec = importlib.util.spec_from_file_location("inspector", _PATH)
inspector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inspector)


def _write(tmp_path, src):
    p = tmp_path / "page.py"
    p.write_text(src)
    return str(p)


def test_builtin_engine_finds_main(tmp_path):
    path = _write(tmp_path, "def main(n: int = 1):\n    return n\n")
    info = inspector.main(path, engine="builtin")
    assert info["function"]["name"] == "main"
    assert info["static_result"] is False


def test_fused_engine_prefers_decorated_function(tmp_path):
    path = _write(
        tmp_path,
        "def main():\n    return 1\n"
        "@fused.udf\n"
        "def other(n: int = 1):\n    return n\n",
    )
    info = inspector.main(path, engine="fused")
    assert info["function"]["name"] == "other"


def test_fused_engine_static_result_script(tmp_path):
    # No main(), no @fused.udf — just a module-level `result` assignment.
    # engine.py's compat bridge leaves this untouched and it's a valid,
    # parameterless entrypoint under the fused engine.
    path = _write(tmp_path, "result = {'x': 1}\n")
    info = inspector.main(path, engine="fused")
    assert info["function"] is None
    assert info["static_result"] is True


def test_builtin_engine_static_result_script_is_not_runnable(tmp_path):
    # The builtin executor only ever calls main() — a bare `result = ...`
    # script has no entrypoint under it, so static_result must stay False.
    path = _write(tmp_path, "result = {'x': 1}\n")
    info = inspector.main(path, engine="builtin")
    assert info["function"] is None
    assert info["static_result"] is False


def test_no_entrypoint_at_all(tmp_path):
    path = _write(tmp_path, "x = 1\n")
    info = inspector.main(path, engine="fused")
    assert info["function"] is None
    assert info["static_result"] is False

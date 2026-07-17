"""Tests for the api/template.html inspector (static AST parsing only)."""

import importlib.util
import os
import sys

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
        "def main():\n    return 1\n@fused.udf\ndef other(n: int = 1):\n    return n\n",
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


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="PEP 723 dependency parsing needs tomllib (Python 3.11+)"
)
def test_fused_engine_reports_pep723_dependencies(tmp_path):
    src = (
        '# /// script\n# dependencies = ["pyarrow", "requests"]\n# ///\ndef main():\n    return 1\n'
    )
    path = _write(tmp_path, src)
    info = inspector.main(path, engine="fused")
    assert info["dependencies"] == ["pyarrow", "requests"]


def test_builtin_engine_never_reports_dependencies(tmp_path):
    # The builtin executor never resolves PEP 723 deps — showing them would
    # imply an install that never happens.
    src = '# /// script\n# dependencies = ["pyarrow"]\n# ///\ndef main():\n    return 1\n'
    path = _write(tmp_path, src)
    info = inspector.main(path, engine="builtin")
    assert info["dependencies"] == []


def test_malformed_pep723_block_yields_no_dependencies(tmp_path):
    # Informational display only — a malformed block must not crash the
    # inspector, unlike engine.py's script_requirements() which raises.
    src = "# /// script\n# dependencies = [oops\n# ///\ndef main():\n    return 1\n"
    path = _write(tmp_path, src)
    info = inspector.main(path, engine="fused")
    assert info["dependencies"] == []
    assert info["function"]["name"] == "main"

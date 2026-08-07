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


def _declare(tmp_path, deps='"pyarrow", "requests"'):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\nversion = '0.1.0'\n"
        f"dependencies = [{deps}]\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="reading pyproject.toml needs tomllib (Python 3.11+)"
)
def test_fused_engine_reports_the_projects_dependencies(tmp_path):
    """The FOLDER declares them (SPEC PY-16), so that is what the form shows."""
    _declare(tmp_path)
    path = _write(tmp_path, "def main():\n    return 1\n")
    info = inspector.main(path, engine="fused")
    assert info["dependencies"] == ["pyarrow", "requests"]
    assert info["project"] == str(tmp_path)


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="reading pyproject.toml needs tomllib (Python 3.11+)"
)
def test_a_script_header_is_no_longer_reported_as_dependencies(tmp_path):
    """Headers are not read any more — reporting them would describe an
    environment that will never be built."""
    src = (
        "# /// script\n"
        '# dependencies = ["pyarrow", "requests"]\n'
        "# ///\n"
        "def main():\n    return 1\n"
    )
    path = _write(tmp_path, src)
    info = inspector.main(path, engine="fused")
    assert info["dependencies"] == []
    assert info["project"] is None


def test_builtin_engine_never_reports_dependencies(tmp_path):
    # The builtin executor never builds a venv from the declaration — showing it
    # would imply an install that never happens.
    _declare(tmp_path, '"pyarrow"')
    path = _write(tmp_path, "def main():\n    return 1\n")
    info = inspector.main(path, engine="builtin")
    assert info["dependencies"] == []


def test_a_malformed_manifest_yields_no_dependencies(tmp_path):
    # Informational display only — a broken pyproject.toml must not crash the
    # inspector view.
    (tmp_path / "pyproject.toml").write_text("this is not [ toml", encoding="utf-8")
    path = _write(tmp_path, "def main():\n    return 1\n")
    info = inspector.main(path, engine="fused")
    assert info["dependencies"] == []
    assert info["function"]["name"] == "main"


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="reading pyproject.toml needs tomllib (Python 3.11+)"
)
def test_a_nested_manifest_is_reported_as_ignored(tmp_path):
    """An inert file that looks correct is the exact failure D177 warns about.

    The environment is the project root's, so a `pyproject.toml` in a subfolder
    declares nothing. Surfaced here so a user who edits one and sees no change
    has something connecting the two.
    """
    _declare(tmp_path)
    sub = tmp_path / "readers"
    sub.mkdir()
    (sub / "pyproject.toml").write_text(
        "[project]\nname = 'inner'\nversion = '0.1.0'\ndependencies = [\"altair\"]\n",
        encoding="utf-8",
    )
    path = _write(sub, "def main():\n    return 1\n")

    info = inspector.main(path, engine="fused")
    assert info["project"] == str(tmp_path)
    assert info["dependencies"] == ["pyarrow", "requests"]
    assert info["ignored_manifests"] == [str(sub / "pyproject.toml")]

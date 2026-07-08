"""Tests for the built-in executor (fused_render/executor.py + _child.py, D72).

Runs real subprocesses via run_python — no mocking, since the worker's own
process boundary (and the traceback frames it produces) is exactly what's
under test.
"""
from fused_render.executor import run_python


def _write(tmp_path, name, src):
    p = tmp_path / name
    p.write_text(src)
    return str(p)


def test_error_where_points_at_deepest_user_frame(tmp_path):
    path = _write(
        tmp_path,
        "t.py",
        "def helper(x):\n"
        "    return 1 / x\n"
        "\n"
        "def main(divisor: int = 0):\n"
        "    return helper(divisor)\n",
    )
    out = run_python(path, {})
    assert out["ok"] is False
    assert out["error"]["type"] == "ZeroDivisionError"
    assert out["error"]["where"] == {
        "file": path,
        "line": 2,
        "func": "helper",
        "source": "return 1 / x",
    }
    # No runner internals (_child.py's own frame, frozen importlib bootstrap).
    assert "_child.py" not in out["error"]["traceback"]
    assert "<frozen" not in out["error"]["traceback"]


def test_error_in_library_blames_calling_user_line(tmp_path):
    path = _write(
        tmp_path,
        "t.py",
        "import json\n\n"
        "def main():\n"
        '    return json.loads("{not valid json}")\n',
    )
    out = run_python(path, {})
    assert out["ok"] is False
    where = out["error"]["where"]
    assert where["file"] == path
    assert where["line"] == 4
    assert where["func"] == "main"


def test_syntax_error_where_from_exception(tmp_path):
    path = _write(tmp_path, "t.py", "def main(:\n    return 1\n")
    out = run_python(path, {})
    assert out["ok"] is False
    assert out["error"]["type"] == "SyntaxError"
    assert out["error"]["where"] == {
        "file": path,
        "line": 1,
        "func": None,
        "source": "def main(:",
    }


def test_harness_error_has_no_where_and_no_stack(tmp_path):
    path = _write(tmp_path, "t.py", "def main(count: int):\n    return count\n")
    out = run_python(path, {})
    assert out["ok"] is False
    assert out["error"]["where"] is None
    # One-liner: no "Traceback (most recent call last):" header.
    assert out["error"]["traceback"].strip() == "ParamError: missing required param: 'count'"


def test_missing_file_where_is_none(tmp_path):
    out = run_python(str(tmp_path / "nope.py"), {})
    assert out["ok"] is False
    assert out["error"]["type"] == "FileNotFoundError"
    assert out["error"]["where"] is None


def test_success_path_unaffected(tmp_path):
    path = _write(tmp_path, "t.py", "def main():\n    return {'ok': 1}\n")
    out = run_python(path, {})
    assert out["ok"] is True
    assert out["result"] == {"ok": 1}

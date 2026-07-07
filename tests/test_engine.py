"""Tests for the optional fused execution engine (fused_render/engine.py, D68).

The pure parts (PEP 723 parsing, code wrapping + the bare-main compat bridge,
traceback cleaning, wire-shape adaptation) run everywhere — the wrapped code is
exec()'d directly, standing in for the backend's runner. The real-backend
integration tests run only when the `fused` package is importable (CI without
it skips them; the engine itself falls back the same way).
"""
import asyncio
import json
import os
import sys
import types

import pytest

from fused_render import engine


# --- script_requirements (PEP 723) ------------------------------------------

# tomllib is 3.11+; the engine itself is unreachable on 3.10 (the fused package
# needs 3.11, so available() is False), but requires-python is >=3.10 — keep a
# 3.10 dev `pytest` green by skipping the parser tests there.
requires_tomllib = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib (PEP 723 parsing) needs Python 3.11+"
)


@requires_tomllib
def test_requirements_absent_is_empty():
    assert engine.script_requirements("def main():\n    return 1\n") == []


@requires_tomllib
def test_requirements_parsed():
    src = (
        "# /// script\n"
        '# dependencies = ["pyarrow", "requests"]\n'
        "# ///\n"
        "def main():\n    return 1\n"
    )
    assert engine.script_requirements(src) == ["pyarrow", "requests"]


@requires_tomllib
def test_requirements_malformed_toml_raises():
    src = "# /// script\n# dependencies = [oops\n# ///\n"
    with pytest.raises(ValueError, match="PEP 723"):
        engine.script_requirements(src)


# --- build_code: the compat bridge, exec()'d directly ------------------------


def _run_wrapped(tmp_path, user_code, params, fake_fused=None):
    """Exec build_code's output the way the backend's runner does: cwd = a
    fresh exec dir holding _params.json, fresh globals, `result` read back."""
    script_dir = tmp_path / "page"
    script_dir.mkdir(exist_ok=True)
    exec_dir = tmp_path / "exec"
    exec_dir.mkdir(exist_ok=True)
    (exec_dir / "_params.json").write_text(json.dumps(params))

    code = engine.build_code(user_code, str(script_dir), str(script_dir / "target.py"))
    g = {}
    cwd = os.getcwd()
    had_fused = "fused" in sys.modules
    prior = sys.modules.get("fused")
    if fake_fused is not None:
        sys.modules["fused"] = fake_fused
    try:
        os.chdir(exec_dir)
        exec(compile(code, "<lambda_exec>", "exec"), g)
    finally:
        os.chdir(cwd)
        if fake_fused is not None:
            if had_fused:
                sys.modules["fused"] = prior
            else:
                sys.modules.pop("fused", None)
    return g


def test_bare_main_bridge_coerces_and_chdirs(tmp_path):
    src = (
        "import os\n"
        "def main(n: int = 1, freq: float = 1.0):\n"
        "    return {'sum': n + freq, 'tn': type(n).__name__, 'cwd': os.getcwd()}\n"
    )
    g = _run_wrapped(tmp_path, src, {"n": "160", "freq": "2.5"})
    assert g["result"]["sum"] == 162.5
    assert g["result"]["tn"] == "int"
    # main() runs with cwd on the script's own dir (relative data paths).
    assert g["result"]["cwd"] == str(tmp_path / "page")


def test_bare_main_bridge_handles_future_annotations(tmp_path):
    src = (
        "from __future__ import annotations\n"
        "def main(n: int = 1):\n"
        "    return type(n).__name__\n"
    )
    g = _run_wrapped(tmp_path, src, {"n": "7"})
    assert g["result"] == "int"


def test_result_script_untouched(tmp_path):
    g = _run_wrapped(tmp_path, "result = {'x': 1}\n", {"ignored": "1"})
    assert g["result"] == {"x": 1}


def test_no_entrypoint_raises(tmp_path):
    with pytest.raises(AttributeError, match="target.py"):
        _run_wrapped(tmp_path, "x = 1\n", {})


def test_registered_function_wrapped_not_bridged(tmp_path):
    # A fake `fused` module with a registered decorated function: the epilogue
    # must wrap it (chdir) and NOT set `result` (the runner dispatches it).
    class _Registered:
        def __init__(self):
            self._fn = lambda **kw: kw

    reg = _Registered()
    fake = types.ModuleType("fused")
    fake._registered_udfs = [reg]

    g = _run_wrapped(tmp_path, "x = 1\n", {}, fake_fused=fake)
    assert "result" not in g
    # The wrapped callable chdirs to the script dir then runs the original.
    cwd = os.getcwd()
    try:
        out = reg._fn(a=1)
        assert out == {"a": 1}
        assert os.getcwd() == str(tmp_path / "page")  # chdir happened inside the call
    finally:
        os.chdir(cwd)


# --- _clean_error -------------------------------------------------------------


def test_clean_error_keeps_user_frames_and_drops_plumbing():
    # User frames carry the real path already (own compile unit); the cleaner
    # only drops the backend/wrapper plumbing around them.
    raw = (
        "Traceback (most recent call last):\n"
        '  File "/backend/_runner.py", line 63, in main\n'
        "    exec(code)\n"
        '  File "<lambda_exec>", line 3, in <module>\n'
        "    exec(compile(...))\n"
        '  File "/pages/sine.py", line 2, in main\n'
        "    raise ValueError('boom')\n"
        "ValueError: boom\n"
    )
    cleaned = engine._clean_error(raw, "/pages/sine.py")
    assert "_runner.py" not in cleaned
    assert "<lambda_exec>" not in cleaned
    assert '  File "/pages/sine.py", line 2, in main' in cleaned
    assert cleaned.rstrip().endswith("ValueError: boom")


def test_clean_error_drops_bridge_helper_frames():
    # The bare-main bridge's helper frames live in <lambda_exec> — dropped even
    # when they appear BETWEEN user frames' plumbing and the user call.
    raw = (
        "Traceback (most recent call last):\n"
        '  File "<lambda_exec>", line 40, in <module>\n'
        "    result = _fused_run_main()\n"
        '  File "<lambda_exec>", line 38, in _fused_run_main\n'
        "    return _fn(**_fused_bind(_fn, _params))\n"
        '  File "/pages/sine.py", line 2, in main\n'
        "    raise ValueError('boom')\n"
        "ValueError: boom\n"
    )
    cleaned = engine._clean_error(raw, "/pages/sine.py")
    assert "_fused_run_main" not in cleaned
    assert '  File "/pages/sine.py", line 2, in main' in cleaned


def test_clean_error_passthrough_without_lambda_exec():
    raw = "execution exceeded 30s and was killed"
    assert engine._clean_error(raw, "/x.py") == raw


def test_split_error():
    assert engine._split_error("...\nValueError: nope\n") == ("ValueError", "nope")
    assert engine._split_error("killed by timeout") == ("Error", "killed by timeout")


# --- run_python wire-shape adaptation (fake backend) --------------------------


class _FakeResult:
    def __init__(self, *, return_value=None, error=None, stdout="", stderr=""):
        self.return_value = return_value
        self.error = error
        self.stdout = stdout
        self.stderr = stderr
        self.duration_ms = 5
        self.response = None


class _FakeBackend:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def execute(self, **kw):
        self.calls.append(kw)
        return self._result


def _adapt(monkeypatch, tmp_path, fake_result, params=None):
    target = tmp_path / "t.py"
    target.write_text("def main():\n    return 1\n")
    backend = _FakeBackend(fake_result)
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    out = asyncio.run(engine.run_python(str(target), params or {}))
    return out, backend


def test_success_maps_to_legacy_shape(monkeypatch, tmp_path):
    out, backend = _adapt(
        monkeypatch, tmp_path, _FakeResult(return_value='{"x": 1}', stdout="hi\n")
    )
    assert out["ok"] is True
    assert out["result"] == {"x": 1}
    assert out["stdout"] == "hi\n"
    assert out["duration_ms"] == 5
    # Params travel as _params.json; requirements include the defaults set.
    call = backend.calls[0]
    assert "_params.json" in call["input_files"]
    assert "pyarrow" in call["requirements"]


def test_error_maps_to_legacy_error_object(monkeypatch, tmp_path):
    target = str(tmp_path / "t.py")  # _adapt writes this exact file
    raw = (
        "Traceback (most recent call last):\n"
        '  File "<lambda_exec>", line 3, in <module>\n'
        "    exec(compile(...))\n"
        f'  File "{target}", line 2, in main\n'
        "    raise ValueError('nope')\n"
        "ValueError: nope\n"
    )
    out, _ = _adapt(monkeypatch, tmp_path, _FakeResult(error=raw))
    assert out["ok"] is False
    assert out["error"]["type"] == "ValueError"
    assert out["error"]["message"] == "nope"
    assert "t.py" in out["error"]["traceback"]
    assert "<lambda_exec>" not in out["error"]["traceback"]


def test_missing_file_is_legacy_error(monkeypatch, tmp_path):
    out = asyncio.run(engine.run_python(str(tmp_path / "nope.py"), {}))
    assert out["ok"] is False and out["error"]["type"] == "FileNotFoundError"


# --- real-backend integration (runs only when `fused` is importable) ----------

requires_fused = pytest.mark.skipif(
    not engine.available(), reason="fused package not installed (engine falls back)"
)


@requires_fused
def test_real_backend_runs_bare_main(monkeypatch, tmp_path):
    # Bare venv (no default data stack) so the test is fast and offline-safe.
    monkeypatch.setattr(engine, "DEFAULT_REQUIREMENTS", [])
    monkeypatch.setattr(engine, "_backend", None)
    target = tmp_path / "sine.py"
    target.write_text(
        "import math\n"
        "def main(n: int = 4, freq: float = 1.0):\n"
        "    return {'n': n, 'y0': math.sin(0.0) * freq}\n"
    )
    out = asyncio.run(engine.run_python(str(target), {"n": "8", "freq": "2.0"}))
    assert out["ok"] is True, out
    assert out["result"] == {"n": 8, "y0": 0.0}


@requires_fused
def test_real_backend_error_points_at_user_file(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "DEFAULT_REQUIREMENTS", [])
    monkeypatch.setattr(engine, "_backend", None)
    target = tmp_path / "boom.py"
    target.write_text("def main():\n    raise ValueError('nope')\n")
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    assert out["error"]["type"] == "ValueError"
    assert str(target) in out["error"]["traceback"]
    assert "_fused_run_main" not in out["error"]["traceback"]

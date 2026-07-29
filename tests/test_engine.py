"""Tests for the optional fused execution engine (fused_render/engine.py, D69).

The pure parts (PEP 723 parsing, code wrapping + the bare-main compat bridge,
traceback cleaning, wire-shape adaptation) run everywhere — the wrapped code is
exec()'d directly, standing in for the backend's runner. The real-backend
integration tests run only when the `fused` package is importable (CI without
it skips them; the engine itself falls back the same way).
"""
import asyncio
import json
import os
import stat
import subprocess
import sys
import types

import pytest

import conftest
from fused_render import engine


@pytest.fixture(autouse=True)
def _fresh_interpreter_probe():
    """The resolved app interpreter is cached per process; clear it per test.

    Every test that monkeypatches `sys.executable` or FUSED_RENDER_APP_PYTHON
    would otherwise be answered from (or poison) another test's cache.
    """
    engine.reset_app_interpreter_cache()
    yield
    engine.reset_app_interpreter_cache()


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


def test_binding_logic_comes_from_binding_py_not_a_copy(tmp_path, monkeypatch):
    """The wrapper must *obtain* the binder from `_binding.py`, not restate it.

    The behavioural comparison lives in tests/test_engine_parity.py; this is the
    structural half — patch the source the wrapper reads and the patch must show
    up in what the child runs. If someone re-inlines a hand-copy, this fails.
    """
    patched = engine._binding_source().replace(
        "missing required param:", "sentinel straight from _binding.py:"
    )
    monkeypatch.setattr(engine, "_binding_source", lambda: patched)
    with pytest.raises(TypeError, match="sentinel straight from _binding.py"):
        _run_wrapped(tmp_path, "def main(x: int):\n    return x\n", {})


def test_result_script_untouched(tmp_path):
    g = _run_wrapped(tmp_path, "result = {'x': 1}\n", {"ignored": "1"})
    assert g["result"] == {"x": 1}


def test_main_wins_over_stale_result(tmp_path):
    # The built-in executor's worker always calls main(**params), overwriting
    # any module-level `result` the script also assigned — the fused engine
    # must match, not silently keep the stale `result`.
    src = "result = {'x': 'stale'}\ndef main():\n    return {'x': 'fresh'}\n"
    g = _run_wrapped(tmp_path, src, {})
    assert g["result"] == {"x": "fresh"}


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
    """Both halves of the real backend's contract, so `calls` shows WHICH ran.

    A fake with only `execute()` would make every header-less run look like the
    venv path (engine.py falls back when `_execute_sync` is absent), which is
    exactly the distinction these tests are about.
    """

    def __init__(self, result):
        self._result = result
        self.calls = []

    async def execute(self, **kw):
        self.calls.append({"via": "execute", **kw})
        return self._result

    def _execute_sync(self, **kw):
        self.calls.append({"via": "_execute_sync", **kw})
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
    # Params travel as _params.json. The script declares no header, so it runs
    # on the app's interpreter with no venv and no requirements at all (D172):
    # a baseline set is exactly what this engine stopped installing.
    call = backend.calls[0]
    assert "_params.json" in call["input_files"]
    assert call["via"] == "_execute_sync"
    assert call["interpreter"] == engine.app_interpreter()
    assert "requirements" not in call


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


def test_unbuildable_wrapper_is_an_engine_error_not_a_500(monkeypatch, tmp_path):
    # build_code reads _binding.py's source off the package, so it can fail on a
    # broken/partial install. Every other failure in run_python returns the house
    # wire shape; this one used to be raised outside the guard and would have
    # reached the request handler as a 500 with no error overlay (D17).
    target = tmp_path / "t.py"
    target.write_text("def main():\n    return 1\n")

    def _boom():
        raise OSError("no such resource: _binding.py")

    monkeypatch.setattr(engine, "_binding_source", _boom)
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    assert out["error"]["type"] == "EngineError"
    assert "no such resource" in out["error"]["traceback"]


def test_missing_file_is_legacy_error(monkeypatch, tmp_path):
    out = asyncio.run(engine.run_python(str(tmp_path / "nope.py"), {}))
    assert out["ok"] is False and out["error"]["type"] == "FileNotFoundError"


# --- real-backend integration (runs only when `fused` is importable) ----------

requires_fused = pytest.mark.skipif(
    not engine.available(), reason="fused package not installed (engine falls back)"
)


def test_ci_claiming_to_cover_this_engine_actually_runs_it():
    """`engine.available()` must not be allowed to silently switch CI off.

    Everything below gates on it, which is right for a dev machine or a matrix
    entry without the extra — and wrong for the job that exists to run this
    engine (.github/workflows/test.yml's `fused-engine`): a job that installs
    `[fused]`, skips every test, and reports green is worse than no job. That
    job sets FUSED_RENDER_REQUIRE_FUSED_ENGINE=1, which turns the skip
    condition into an assertion here. Unset, this is a no-op.
    """
    if os.environ.get("FUSED_RENDER_REQUIRE_FUSED_ENGINE") != "1":
        pytest.skip("only meaningful where the [fused] extra is expected")
    assert engine.available(), (
        "FUSED_RENDER_REQUIRE_FUSED_ENGINE=1 but the fused local backend is not "
        "importable — the `[fused]` extra did not take effect (a direct-URL "
        "wheel marked python_version >= '3.11': check the interpreter), so every "
        "engine test would have skipped while the job reported success"
    )


@requires_fused
def test_real_backend_runs_bare_main(monkeypatch, tmp_path):
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
    monkeypatch.setattr(engine, "_backend", None)
    target = tmp_path / "boom.py"
    target.write_text("def main():\n    raise ValueError('nope')\n")
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    assert out["error"]["type"] == "ValueError"
    assert str(target) in out["error"]["traceback"]
    assert "_fused_run_main" not in out["error"]["traceback"]


# --- the app's own interpreter (PY-17 / D172) ---------------------------------
#
# A header-less script runs with `interpreter=<the app's real python>` and gets
# no venv. Everything here is about the one way that ships broken: `interpreter=`
# is spawned verbatim as argv[0], so handing it something that is not a genuine
# python (a py2app launcher stub, a `pythonw` with no console, an interpreter
# that cannot self-locate its stdlib once the backend strips PYTHONHOME) would
# spawn the wrong process instead of running the script — and `requirements` is
# silently ignored on that branch, so there is no fallback to notice.


def test_app_interpreter_is_this_installation_s_python():
    """The resolved interpreter really runs, and is the SAME install as ours.

    Same `sys.prefix` is the whole point: that is what makes the app's own
    site-packages (`[bundled]` + core `dependencies`) visible to a header-less
    script without installing anything.
    """
    exe = engine.app_interpreter()
    assert exe is not None, "a dev checkout's sys.executable is a real python"
    proc = subprocess.run(
        [exe, "-c", "import sys; print(sys.prefix)"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == sys.prefix


def test_app_interpreter_probe_rejects_a_launcher_stub(monkeypatch, tmp_path):
    """A py2app-style launcher stub must never be returned as an interpreter.

    The stub is executable and exits 0 — the exact shape that would pass a
    naive `os.path.isfile` / `os.access(X_OK)` check and then spawn the whole
    app as a subprocess on every run.
    """
    stub = tmp_path / "FusedRender"
    stub.write_text("#!/bin/sh\necho 'launching the app'\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FUSED_RENDER_APP_PYTHON", str(stub))
    assert engine.app_interpreter() is None


def test_app_interpreter_probe_rejects_a_foreign_python(monkeypatch, tmp_path):
    """A real python that is a DIFFERENT installation is rejected too.

    It would run the script — with someone else's site-packages, so every
    `[bundled]` import a header-less template makes would fail for reasons the
    traceback cannot explain. Simulated by a stub that reports a prefix which
    is not ours.
    """
    fake = tmp_path / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '{\"prefix\": \"/somewhere/else\", \"executable\": \"%s\"}\\n' \"$0\"\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FUSED_RENDER_APP_PYTHON", str(fake))
    assert engine.app_interpreter() is None


def test_app_interpreter_probe_survives_the_stripped_child_env(monkeypatch):
    """The probe must run under the env the BACKEND will use, not ours.

    `python_compute` strips PYTHONHOME/PYTHONPATH/VIRTUAL_ENV/PYTHONSTARTUP
    from the child. A packaged interpreter that only self-locates its stdlib
    *because* the app process exports PYTHONHOME would pass a probe run with
    our env and then die in the real child — so the probe drops the same vars.
    """
    seen = {}
    real_run = subprocess.run

    def spy(cmd, **kw):
        seen.update(kw.get("env") or {})
        seen["_had_env"] = kw.get("env") is not None
        return real_run(cmd, **kw)

    monkeypatch.setattr(engine.subprocess, "run", spy)
    engine.app_interpreter()
    assert seen["_had_env"], "the probe must pass an explicit env, not inherit"
    for var in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONSTARTUP"):
        assert var not in seen, f"{var} must be stripped from the probe env"


def test_windows_launcher_pythonw_resolves_to_the_console_python(monkeypatch, tmp_path):
    """On Windows the app runs under `pythonw.exe`; prefer `python.exe`.

    Same install (so same site-packages and the same `sys.prefix`), but with a
    usable standard-stream setup — `pythonw` is the windowless build the
    launcher stub and the AppRun analog exec, and the backend captures the
    child's stdout/stderr.
    """
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert engine._interpreter_candidate() == (str(tmp_path / "python.exe"), True)


def test_pythonw_without_a_console_sibling_keeps_itself(monkeypatch, tmp_path):
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert engine._interpreter_candidate() == (str(tmp_path / "pythonw.exe"), True)


def test_an_autodetected_non_python_name_is_never_spawned(monkeypatch, tmp_path):
    """A launcher-stub `sys.executable` is rejected WITHOUT creating a process.

    Being wrong here is not merely a failed probe: spawning a py2app launcher
    could start a second copy of the whole app. The name check is what means we
    never have to know what that stub does with `-c`.
    """
    stub = tmp_path / "FusedRender"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(sys, "executable", str(stub))
    spawned = []
    monkeypatch.setattr(
        engine.subprocess, "run",
        lambda cmd, **kw: spawned.append(cmd) or pytest.fail("must not spawn"),
    )
    assert engine.app_interpreter() is None
    assert spawned == []


def test_an_explicit_override_is_probed_even_with_an_odd_name(monkeypatch, tmp_path):
    """FUSED_RENDER_APP_PYTHON is deliberate config: a wrapper name is allowed.

    It still has to pass the probe — the escape hatch relaxes the name guard,
    not the verification.
    """
    wrapper = tmp_path / "app-python-wrapper"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FUSED_RENDER_APP_PYTHON", str(wrapper))
    assert engine.app_interpreter() == str(wrapper)


def test_the_probe_timeout_is_short_enough_to_not_read_as_a_hang():
    """It is paid on the request path in the case it exists to catch."""
    assert engine._PROBE_TIMEOUT_S <= 10


@requires_fused
def test_a_headerless_script_runs_on_the_app_interpreter(monkeypatch, tmp_path):
    """The end-to-end assertion: no venv, and the child IS the app's python.

    Checked from inside the child (its own `sys.executable`/`sys.prefix`)
    rather than from the requirements we passed — `interpreter=` silently
    ignores `requirements`, so only the child can say which python ran.
    """
    monkeypatch.setattr(engine, "_backend", None)
    target = tmp_path / "who.py"
    target.write_text(
        "import sys\n"
        "def main():\n"
        "    return {'exe': sys.executable, 'prefix': sys.prefix}\n"
    )
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is True, out
    assert out["result"]["prefix"] == sys.prefix
    assert out["result"]["exe"] == engine.app_interpreter()


@requires_fused
def test_a_headerless_script_sees_the_app_s_own_packages(monkeypatch, tmp_path):
    """The point of the switch: `[bundled]` works with no header and no install."""
    monkeypatch.setattr(engine, "_backend", None)
    target = tmp_path / "reads.py"
    target.write_text(
        "def main():\n"
        "    import pandas, pyarrow\n"
        "    return sorted(pandas.DataFrame({'a': [1, 2]}).a.tolist())\n"
    )
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is True, out
    assert out["result"] == [1, 2]


@requires_fused
def test_a_declared_header_still_gets_its_own_venv(
    monkeypatch, tmp_path, warm_fused_backend_venv
):
    """A header keeps today's venv path — it must NOT land on the app python."""
    monkeypatch.setattr(engine, "_backend", None)
    target = tmp_path / "declared.py"
    target.write_text(
        conftest.WARM_HEADER
        + "import sys\n"
        "def main():\n"
        "    return {'prefix': sys.prefix}\n"
    )
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is True, out
    assert out["result"]["prefix"] != sys.prefix
    assert "venvs" in out["result"]["prefix"]


def test_a_header_is_the_complete_requirement_list(monkeypatch, tmp_path):
    """A header goes to the venv path, and its venv gets EXACTLY the header.

    No baseline is unioned in (D172), so a header means what PEP 723 says it
    means. This is the assertion that stops a baseline being reintroduced.
    """
    target = tmp_path / "declared.py"
    target.write_text(
        "# /// script\n"
        '# dependencies = ["imagecodecs", "pyproj"]\n'
        "# ///\n"
        "def main():\n    return 1\n"
    )
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    # Past the install-loader pre-flight (PY-18), which would otherwise answer
    # `needs_install` for these two: this test is about what reaches the backend.
    monkeypatch.setattr("fused_render.envinstall.is_installed", lambda reqs: True)
    asyncio.run(engine.run_python(str(target), {}))
    call = backend.calls[0]
    assert call["via"] == "execute"
    assert call["requirements"] == ["imagecodecs", "pyproj"]


def test_no_resolvable_interpreter_falls_back_to_a_venv(monkeypatch, tmp_path):
    """When the probe fails we use the venv path — never a non-interpreter.

    The fallback loses the app's packages, which is a real degradation; the
    alternative is spawning something that is not python at all, which fails
    every run with a message about neither the script nor the cause.
    """
    stub = tmp_path / "not-python"
    stub.write_text("#!/bin/sh\nexit 3\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FUSED_RENDER_APP_PYTHON", str(stub))

    backend = _FakeBackend(_FakeResult(return_value="null"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    target = tmp_path / "plain.py"
    target.write_text("def main():\n    return None\n")
    asyncio.run(engine.run_python(str(target), {}))
    call = backend.calls[0]
    assert call["via"] == "execute"
    assert call["requirements"] == []

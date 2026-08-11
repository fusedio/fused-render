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
import shlex
import stat
import subprocess
import sys
import threading
import time
import types

import pytest

import conftest
from fused_render import engine, envinstall


def _toml_available() -> bool:
    """Can a `pyproject.toml` be parsed in this environment?"""
    try:
        import tomllib  # noqa: F401
    except ImportError:
        try:
            import tomli  # noqa: F401
        except ImportError:
            return False
    return True


@pytest.fixture(autouse=True)
def _fresh_interpreter_probe():
    """The resolved app interpreter is cached per process; clear it per test.

    Every test that monkeypatches `sys.executable` or FUSED_RENDER_APP_PYTHON
    would otherwise be answered from (or poison) another test's cache.
    """
    engine.reset_app_interpreter_cache()
    yield
    engine.reset_app_interpreter_cache()


@pytest.fixture(autouse=True)
def _fresh_availability_cache():
    """warm()'s cached availability is process-global; clear it per test."""
    engine._available_cached = None
    yield
    engine._available_cached = None


# --- engine warm-up + non-blocking availability (PY cold-start) --------------
#
# The first /api/config resolves the engine, which imports the fused backend. On
# a fresh install that cold import is ~a minute (bytecode compile + the OS
# scanning every native module on first load); it must NEVER run on the request
# thread. warm() pays it once in a startup thread; available_nonblocking() reads
# the result (or a cheap importability check) and never triggers the import.


def test_available_nonblocking_never_triggers_the_cold_import(monkeypatch):
    def _boom():
        raise AssertionError("the cold engine import must not run on the request path")

    monkeypatch.setattr(engine, "available", _boom)
    # Whatever it answers, it is computed WITHOUT the cold import (cache /
    # sys.modules / find_spec) — _boom must never fire.
    assert isinstance(engine.available_nonblocking(), bool)


def test_available_nonblocking_reads_find_spec_not_the_import(monkeypatch):
    monkeypatch.setattr(engine, "available",
                        lambda: (_ for _ in ()).throw(AssertionError("no cold import")))
    monkeypatch.delitem(sys.modules,
                        "fused.agent_core.backends.local.python_compute", raising=False)
    monkeypatch.setattr(engine.importlib.util, "find_spec", lambda _n: object())
    assert engine.available_nonblocking() is True
    monkeypatch.setattr(engine.importlib.util, "find_spec", lambda _n: None)
    assert engine.available_nonblocking() is False


def test_warm_caches_a_positive_and_short_circuits(monkeypatch):
    monkeypatch.setattr(engine, "available", lambda: True)
    engine.warm()
    assert engine._available_cached is True

    def _no_find_spec(_name):
        raise AssertionError("cached result must be used, not a fresh probe")

    monkeypatch.setattr(engine.importlib.util, "find_spec", _no_find_spec)
    assert engine.available_nonblocking() is True


def test_warm_does_not_cache_a_negative(monkeypatch):
    # A negative stays uncached so a mid-session `fused` install is still seen
    # live (fused_engine_available's original per-call contract).
    monkeypatch.setattr(engine, "available", lambda: False)
    engine.warm()
    assert engine._available_cached is None


def test_warm_logs_the_duration(monkeypatch, caplog):
    monkeypatch.setattr(engine, "available", lambda: True)
    with caplog.at_level("INFO", logger="fused_render.engine"):
        engine.warm()
    assert "engine warm-up" in caplog.text


# --- the folder rule (SPEC PY-16) --------------------------------------------

# Reading a `pyproject.toml` needs `tomllib` (3.11+ stdlib) or the `tomli`
# dependency that covers 3.10. Gated on AVAILABILITY rather than on the version:
# with tomli installed these tests run on 3.10 too, and they should — that is the
# whole point of shipping the fallback. A version check would have kept them
# silently skipped on the one interpreter where the bug lived.
requires_tomllib = pytest.mark.skipif(
    not _toml_available(), reason="needs tomllib (3.11+) or the tomli package"
)


def _declare(folder, deps='"pyarrow", "requests"'):
    """Give `folder` the pyproject.toml that declares its environment."""
    os.makedirs(str(folder), exist_ok=True)
    with open(os.path.join(str(folder), "pyproject.toml"), "w", encoding="utf-8") as fh:
        fh.write("[project]\nname = 't'\nversion = '0.1.0'\n"
                 f"dependencies = [{deps}]\n")
    return str(folder)


@requires_tomllib
def test_a_leftover_script_header_is_an_ordinary_comment(monkeypatch, tmp_path):
    """A `# /// script` block neither supplies an environment nor blocks a run.

    Headers were briefly REFUSED here, so that a file whose declaration had
    stopped being read could not fail later on a confusing ImportError. That
    guard is gone: this is a pre-release product with no installed base to
    migrate, and the refusal cost more than it bought — it broke files whose
    header declared nothing applicable on this platform, and it only fired when
    the folder had no manifest, so the case it was written for (a half-migrated
    folder that HAS one) slipped through it anyway.

    So the block is what it looks like: comment lines. The folder decides the
    environment (PY-16), and a file with no folder declaration runs on the app's
    own interpreter (PY-17) exactly as it would without the block.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))
    target = tmp_path / "orphan.py"
    target.write_text(
        '# /// script\n# dependencies = ["cowsay"]\n# ///\ndef main():\n    return 7\n',
        encoding="utf-8",
    )
    # Stubbed rather than run for real: the assertion is about what run_python
    # DECIDES, and a real backend would make this test require the `[fused]`
    # extra — which the 3.10-3.13 matrix does not install, so it would fail
    # there rather than skip. `_FakeBackend` carries both halves of the
    # contract, so `via` below proves which path was taken.
    backend = _FakeBackend(_FakeResult(return_value="7"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)

    out = asyncio.run(engine.run_python(str(target), {}))

    assert out["ok"] is True, out.get("error")
    assert not out.get("needs_install"), "a header must not ask for an install"
    assert backend.calls, "the run never reached the backend — it was refused"
    assert backend.calls[0]["via"] == "_execute_sync", (
        "a header must not route the file down the venv path"
    )
    assert backend.calls[0].get("interpreter"), (
        "a folder with no manifest runs on the app interpreter (PY-17)"
    )


@requires_tomllib
def test_a_header_does_not_override_the_folders_declaration(monkeypatch, tmp_path):
    """The folder wins outright — the header is not merged, read, or reported.

    The half-migrated case: the manifest is written but a stale block survives
    in one file. What the block names must have no bearing on the environment.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))
    proj = _declare(tmp_path / "proj", deps='"pyarrow"')
    target = os.path.join(proj, "s.py")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write('# /// script\n# dependencies = ["cowsay"]\n# ///\n'
                 'def main():\n    return 7\n')

    from fused_render import projectenv

    assert projectenv.applicable_dependencies_of(proj) == ["pyarrow"], (
        "the header leaked into the folder's declaration"
    )


@requires_tomllib
def test_a_header_inside_a_declared_project_is_simply_inert(monkeypatch, tmp_path):
    """The refusal is for ORPHANS only.

    A folder that has migrated has a working environment; a leftover block in one
    of its files declares nothing and must not break a project that is already
    correct. `tests/test_engine_requirements.py` is what keeps the core templates
    clean of them.
    """
    _declare(tmp_path, '"pyarrow"')
    target = tmp_path / "leftover.py"
    target.write_text(
        '# /// script\n# dependencies = ["cowsay"]\n# ///\ndef main():\n    return 1\n',
        encoding="utf-8",
    )
    # The escape hatch, so the run takes the venv path deterministically rather
    # than depending on whether this machine's app interpreter happens to have
    # pyarrow — the assertion is about WHICH declaration was read.
    monkeypatch.setenv(engine._FORCE_VENV_ENV, "1")
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    seen = _preflight_spy(monkeypatch)

    out = asyncio.run(engine.run_python(str(target), {}))

    assert out["ok"] is True, out
    assert seen == [["pyarrow"]], "the header was read instead of the pyproject"
    assert backend.calls[0]["interpreter"] == envinstall.venv_python_for(str(tmp_path))


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


def test_the_script_gets_its_own_path_as_dunder_file(tmp_path):
    """`__file__` must be the script's real absolute path, at module level.

    Passing the path as `compile()`'s filename — which the wrapper already did —
    only labels code objects for tracebacks; it does not define the *name*
    `__file__` in the exec globals. So `os.path.dirname(__file__)`, the ordinary
    way a page finds a data file next to its `.py`, raised NameError under this
    engine while working under the built-in one.
    """
    g = _run_wrapped(tmp_path, "_F = __file__\ndef main():\n    return _F\n", {})
    assert g["result"] == str(tmp_path / "page" / "target.py")


def test_the_script_gets_the_same_dunder_name_as_the_builtin_worker(tmp_path):
    # _child.py loads the file as spec_from_file_location("__fused_module__"),
    # so that is the name to match. Before this it was inherited from the
    # backend's runner namespace and came out as "builtins" — which no script
    # could sensibly branch on, and which differed from the other engine.
    g = _run_wrapped(tmp_path, "_N = __name__\ndef main():\n    return _N\n", {})
    assert g["result"] == "__fused_module__"


def test_neither_dunder_makes_a_script_look_like_main(tmp_path):
    # `if __name__ == "__main__":` blocks stay dormant under both engines —
    # templates like geotiff/tile_server.py use that guard for the *subprocess*
    # they spawn of themselves, and it must not fire on the exec'd entry.
    src = "_RAN = False\nif __name__ == '__main__':\n    _RAN = True\ndef main():\n    return _RAN\n"
    g = _run_wrapped(tmp_path, src, {})
    assert g["result"] is False


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


def test_the_interpreter_probe_does_not_block_the_event_loop(monkeypatch, tmp_path):
    """`app_interpreter` runs up to two 5s subprocess probes, synchronously.

    `/api/run` awaits `run_python` directly (no `to_thread`), so calling the
    probe inline stalls the WHOLE server for its duration — the /api/fs/events
    websocket, the file watcher, every other in-flight request — on the first
    header-less run of a process. Asserted by counting how many times a
    concurrent 10ms task gets to run while the probe is "in progress": inline,
    the answer is zero.
    """
    target = tmp_path / "t.py"
    target.write_text("def main():\n    return 1\n")

    def _slow_probe():
        time.sleep(0.5)
        return None  # -> InterpreterUnavailable; this test is about the stall

    monkeypatch.setattr(engine, "app_interpreter", _slow_probe)

    async def _drive():
        ticks = 0

        async def _tick():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(_tick())
        out = await engine.run_python(str(target), {})
        beat.cancel()
        return ticks, out

    ticks, out = asyncio.run(_drive())
    assert out["error"]["type"] == "InterpreterUnavailable"  # probe really ran
    assert ticks > 5, (
        f"the event loop only ticked {ticks} times during a 0.5s interpreter "
        "probe — run_python is calling app_interpreter inline"
    )


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
        "importable — the `[fused]` extra did not take effect (a pre-release pin "
        "marked python_version >= '3.11': check the interpreter), so every "
        "engine test would have skipped while the job reported success"
    )


@requires_fused
def test_the_backend_is_built_from_the_RESOLVED_script_interpreter(monkeypatch):
    """D214: the pin only exists if it reaches the backend.

    `envinstall._python_executable()` reads this attribute straight back off the
    live backend, and `venv_key_for` folds it into the venv key — so the resolution
    and the key are one value with one source. A resolver whose answer never
    reached the constructor would leave every venv still keyed on whatever
    interpreter happened to run the server, which is the bug D214 is about.
    """
    monkeypatch.setattr(engine, "_backend", None)
    monkeypatch.setattr(envinstall, "script_python", lambda: "/pinned/python3.12")
    backend = engine.get_backend()
    assert backend._python_executable == "/pinned/python3.12"
    # And read back through the loader, which is what actually keys the venv.
    assert envinstall._python_executable() == "/pinned/python3.12"


@requires_fused
def test_a_server_already_on_312_leaves_the_backend_default_untouched(monkeypatch):
    """None must stay None all the way to the constructor.

    This is the packaged-build path, and `None` is what makes it a no-op: it is
    the value the backend has always been given, so `python_identity` produces the
    identical key and no existing venv is orphaned. Passing something merely
    equivalent (`sys.executable`) instead would re-key every venv on every
    installed app for no behavioural gain.
    """
    monkeypatch.setattr(engine, "_backend", None)
    monkeypatch.setattr(envinstall, "script_python", lambda: None)
    assert engine.get_backend()._python_executable is None


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
def test_a_declared_project_runs_in_its_own_venv(
    monkeypatch, tmp_path, warm_fused_backend_venv
):
    """The venv path, end to end: a real venv is built and the script runs IN it.

    `_FORCE_VENV_ENV` is set because this project would otherwise no longer reach
    the venv path at all, and for an honest reason rather than a convenient one:
    the warm project declares `pip`, chosen because it is the cheapest possible
    warm venv, and `pip` is present on the app interpreter (the dev-env setup
    seeds it into `.venv` on purpose, so `test_deploy.py` can exercise a real
    `_pip_available()`). A declaration every member of which is already installed
    is precisely what `app_satisfies` claims, so leaving the fast path enabled
    here would silently convert this into a second no-project test.

    The hatch keeps the assertion this test is FOR — build a venv, run the script
    inside it, land under `venvs` — rather than trading it for a weaker one. Which
    declaration routes where is covered separately, on the fake backend, by
    `test_one_missing_package_sends_the_whole_project_to_the_venv_path` and
    `test_a_declaration_the_app_already_satisfies_builds_no_venv`.
    """
    monkeypatch.setenv(engine._FORCE_VENV_ENV, "1")
    monkeypatch.setattr(engine, "_backend", None)
    target = os.path.join(warm_fused_backend_venv, "declared.py")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("import sys\n"
                 "def main():\n"
                 "    return {'prefix': sys.prefix}\n")
    out = asyncio.run(engine.run_python(target, {}))
    assert out["ok"] is True, out
    assert out["result"]["prefix"] != sys.prefix
    assert "venvs" in out["result"]["prefix"]


@requires_tomllib
def test_the_declaration_is_the_complete_requirement_list(monkeypatch, tmp_path):
    """The folder's declaration is the whole environment; no baseline is added.

    No baseline is unioned in (D172, which survives the move from headers to
    pyproject), so what the folder declares is what its venv contains. This is
    the assertion that stops a baseline being reintroduced.
    """
    _declare(tmp_path, '"imagecodecs", "pyproj"')
    target = tmp_path / "declared.py"
    target.write_text("def main():\n    return 1\n")
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    # Past the install-loader pre-flight (PY-18), which would otherwise answer
    # `needs_install`: this test is about what reaches the backend.
    monkeypatch.setattr("fused_render.envinstall.is_installed", lambda project: True)
    asyncio.run(engine.run_python(str(target), {}))
    call = backend.calls[0]
    # The environment lives in our home dir, so the backend is TOLD which
    # interpreter to run on rather than resolving a requirement set of its own.
    assert call["via"] == "_execute_sync"
    assert call["interpreter"] == envinstall.venv_python_for(str(tmp_path))
    assert "requirements" not in call, (
        "upstream ignores requirements once interpreter is set; passing both misleads"
    )


@requires_tomllib
def test_the_install_preflight_does_not_run_on_the_event_loop(monkeypatch, tmp_path):
    """`is_installed` can spawn a subprocess, so it must not block the loop.

    Since D212 the pre-flight is not a single `os.path.exists` any more: the first
    call for a venv probes its interpreter with `subprocess.run(..., timeout=5)`.
    `/api/run` awaits this coroutine directly (`routers/run.py`), so running that
    inline stalls the ENTIRE server — websockets, the file watcher, every other
    request — for up to five seconds. The header-LESS branch of the same `if` was
    moved off the loop for exactly this reason (`await asyncio.to_thread(
    app_interpreter)`); this pins that the header branch matches it.

    Asserted by thread identity rather than by timing: no sleeps, no flakiness.
    """
    _declare(tmp_path, '"imagecodecs"')
    target = tmp_path / "declared.py"
    target.write_text("def main():\n    return 1\n")
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    seen = {}

    def _where_am_i(project):
        seen["ident"] = threading.get_ident()
        return True

    monkeypatch.setattr("fused_render.envinstall.is_installed", _where_am_i)

    async def _drive():
        return threading.get_ident(), await engine.run_python(str(target), {})

    loop_ident, out = asyncio.run(_drive())
    assert out["ok"] is True
    assert seen["ident"] != loop_ident, "the pre-flight probe ran ON the event loop"


@requires_tomllib
@pytest.mark.parametrize("exc", [ImportError("no fused"), RuntimeError("no attr")])
def test_a_preflight_that_raises_is_still_contained_off_thread(
    monkeypatch, tmp_path, exc
):
    """Moving the pre-flight to a thread must not widen what escapes /api/run.

    `is_installed` -> `venv_key_for` reaches into `fused.agent_core...` unguarded,
    and `_backend_attr` raises RuntimeError BY DESIGN when an upstream private
    attribute disappears. Uncontained, that made /api/run an unhandled 500 whose
    body is `{"error": "<string>"}`, which runtime.js renders as the literal word
    `undefined`. `asyncio.to_thread` re-raises in the awaiting frame, so the
    existing `try` still covers it — verified here rather than assumed, because
    "the exception surfaces somewhere else now" is exactly the kind of regression
    a thread hop hides.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "workspace"))
    proj = _declare(tmp_path / "proj", deps='"imagecodecs"')
    target = os.path.join(proj, "declared.py")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("def main():\n    return 1\n")
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)

    def _raise(reqs):
        raise exc

    monkeypatch.setattr("fused_render.envinstall.is_installed", _raise)
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    assert out["error"]["message"], out
    assert "traceback" in out["error"]


def test_no_resolvable_interpreter_is_a_loud_error_not_a_venv(monkeypatch, tmp_path):
    """A header-less script must NEVER silently fall back to a venv.

    With no baseline requirements (D172) that venv is stdlib-only, so a template
    that works today would die on `import numpy` — an error about the wrong thing
    entirely, on a path the user can't see. It would also mean a header-less core
    template hitting PyPI, which must never happen. So: run nothing, and return a
    configuration error that names its own fix.
    """
    stub = tmp_path / "not-python"
    stub.write_text("#!/bin/sh\nexit 3\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FUSED_RENDER_APP_PYTHON", str(stub))
    # No wrapper rescue either.
    monkeypatch.setattr(engine, "_wrapper_interpreter", lambda c: (None, "stub"))

    backend = _FakeBackend(_FakeResult(return_value="null"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    target = tmp_path / "plain.py"
    target.write_text("def main():\n    return None\n")
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    assert out["error"]["type"] == "InterpreterUnavailable"
    assert "FUSED_RENDER_APP_PYTHON" in out["error"]["message"]
    assert backend.calls == [], "nothing may be executed"


def test_a_missing_execute_sync_is_loud_too(monkeypatch, tmp_path):
    """Same rule for the other way the interpreter path can be unavailable."""

    class NoSyncBackend:
        def __init__(self):
            self.calls = []

        async def execute(self, **kw):
            self.calls.append(kw)
            raise AssertionError("must not run in a venv")

    backend = NoSyncBackend()
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    target = tmp_path / "plain.py"
    target.write_text("def main():\n    return None\n")
    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    assert out["error"]["type"] == "EngineError"
    assert "_execute_sync" in out["error"]["traceback"]
    assert backend.calls == []


# --- a header the app interpreter ALREADY satisfies ----------------------------
#
# The `[bundled]` extra puts pandas/duckdb/pyarrow/numpy/geopandas/rasterio/zarr/
# pyproj/keyring/yaml/cryptography on the app interpreter, and D172 stops a header
# being EXTENDED with a baseline — but nothing checked whether a header was already
# SATISFIED. So a header naming `pandas` built a multi-GB venv beside the pandas the
# app already ships. Measured on one machine's venv store: the set
# ['duckdb>=1.5.0','keyring>=24','pandas>=2.0.0','pyarrow>=14.0.0','pyyaml>=6.0.0']
# — every one already present — under FIVE different keys; 33 venvs, 4.9GB.
#
# These tests pin both directions: the fast path when every requirement is met, and
# an unchanged venv path the moment anything cannot be PROVEN met.


def _preflight_spy(monkeypatch):
    """Records whether the venv pre-flight was consulted, and for what.

    The pre-flight is keyed on the PROJECT now, so the spy reads the folder's
    declaration back out — the assertions below are about which dependencies
    routed the run, and that is still the interesting fact.
    """
    from fused_render import projectenv

    seen = []

    def _spy(project):
        seen.append(projectenv.dependencies_of(project))
        return True

    monkeypatch.setattr("fused_render.envinstall.is_installed", _spy)
    return seen


@requires_tomllib
@requires_fused
def test_a_declaration_the_app_already_satisfies_builds_no_venv(monkeypatch, tmp_path):
    """pandas is on the app interpreter, so a header naming it needs no venv."""
    _declare(tmp_path, '"pandas>=2.0.0"')
    target = tmp_path / "declared.py"
    target.write_text("def main():\n    return 1\n")
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    seen = _preflight_spy(monkeypatch)

    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is True, out
    assert "needs_install" not in out
    assert seen == [], "the venv pre-flight ran for a header nothing needs to install"
    call = backend.calls[0]
    assert call["via"] == "_execute_sync"
    assert call["interpreter"] == engine.app_interpreter()
    assert "requirements" not in call, (
        "upstream ignores requirements once interpreter is set; passing both misleads"
    )


@requires_tomllib
@requires_fused
def test_one_missing_package_sends_the_whole_project_to_the_venv_path(
    monkeypatch, tmp_path
):
    """All-or-nothing: the venv is what makes the MISSING one importable.

    Splitting the set — interpreter for the satisfied part, venv for the rest — is
    not expressible: one script runs on one interpreter. So one miss means the
    header goes to the venv exactly as before, pandas rebuilt and all.
    """
    _declare(tmp_path, '"pandas>=2.0.0", "imagecodecs"')
    target = tmp_path / "declared.py"
    target.write_text("def main():\n    return 1\n")
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    seen = _preflight_spy(monkeypatch)

    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is True, out
    assert seen == [["pandas>=2.0.0", "imagecodecs"]]
    assert backend.calls[0]["via"] == "_execute_sync"
    assert backend.calls[0]["interpreter"] == envinstall.venv_python_for(str(tmp_path))


@requires_tomllib
@requires_fused
def test_a_version_the_app_cannot_meet_is_not_treated_as_satisfied(
    monkeypatch, tmp_path
):
    """The check is on the SPECIFIER, not on importability.

    "pandas is importable" would have said yes to `pandas>=99`, run the script on
    an interpreter with 2.x, and produced whatever error a version-sensitive script
    produces — an error about the code, for a dependency problem.
    """
    _declare(tmp_path, '"pandas>=99"')
    target = tmp_path / "declared.py"
    target.write_text("def main():\n    return 1\n")
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    seen = _preflight_spy(monkeypatch)

    asyncio.run(engine.run_python(str(target), {}))
    assert seen == [["pandas>=99"]], "an unmeetable pin must still get its own venv"
    assert backend.calls[0]["interpreter"] == envinstall.venv_python_for(str(tmp_path))


@requires_tomllib
@requires_fused
def test_extras_are_never_treated_as_satisfied(monkeypatch, tmp_path):
    """`pandas[performance]` asks for packages a version number cannot vouch for.

    `importlib.metadata.version` says nothing about whether an extra's transitive
    dependencies are installed, so "cannot tell" has to mean "not satisfied" —
    otherwise the script runs and fails on the first import of the extra's package.
    """
    _declare(tmp_path, '"pandas[performance]>=2.0.0"')
    target = tmp_path / "declared.py"
    target.write_text("def main():\n    return 1\n")
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    seen = _preflight_spy(monkeypatch)

    asyncio.run(engine.run_python(str(target), {}))
    assert seen == [["pandas[performance]>=2.0.0"]]
    assert backend.calls[0]["interpreter"] == envinstall.venv_python_for(str(tmp_path))


@requires_tomllib
@requires_fused
def test_the_escape_hatch_forces_the_venv_path(monkeypatch, tmp_path):
    """One env var puts a satisfied header back on the old, isolated venv path."""
    monkeypatch.setenv(engine._FORCE_VENV_ENV, "1")
    _declare(tmp_path, '"pandas>=2.0.0"')
    target = tmp_path / "declared.py"
    target.write_text("def main():\n    return 1\n")
    backend = _FakeBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    seen = _preflight_spy(monkeypatch)

    asyncio.run(engine.run_python(str(target), {}))
    assert seen == [["pandas>=2.0.0"]]
    assert backend.calls[0]["interpreter"] == envinstall.venv_python_for(str(tmp_path))


@requires_fused
def test_the_app_package_probe_runs_at_most_once_per_process(monkeypatch):
    """One subprocess per server process, not one per /api/run.

    The probe spawns the app interpreter and imports importlib.metadata over every
    distribution on its path — tens of milliseconds, and it is asked on the request
    path. Uncached it would be paid by every run of every header, which is the cost
    this whole fast path exists to remove.
    """
    engine.reset_app_packages_cache()
    spawns = []
    real = engine._probe_app_packages
    monkeypatch.setattr(engine, "_probe_app_packages",
                        lambda exe: spawns.append(exe) or real(exe))
    first = engine.app_packages()
    assert engine.app_packages() is first
    assert engine.app_packages() is first
    assert len(spawns) == 1, spawns
    assert "pandas" in first, first


class _NoSyncBackend:
    """A backend with only the async half — a `fused` lacking `_execute_sync`."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    async def execute(self, **kw):
        self.calls.append({"via": "execute", **kw})
        return self._result


@requires_tomllib
@requires_fused
def test_no_execute_sync_is_a_named_configuration_error_not_an_inline_build(
    monkeypatch, tmp_path
):
    """Without `_execute_sync` a project venv cannot be used, and that is SAID.

    `_execute_sync` is the only way to run on an interpreter WE pick, and since
    the environment now lives under our own home dir rather than in the backend's
    store, being told the interpreter is the ONLY way the backend can reach it —
    there is no requirement set it could resolve to the same place.

    So the old fallback (`execute(requirements=…)`) is not available and must not
    be resurrected: it would build a SECOND, different venv inline, a blocking
    download inside /api/run, which is the exact thing PY-18 moved out of it. The
    honest answer is the configuration error `_execute` already raises, surfaced
    through the house wire shape.

    The pre-flight still runs first, so a genuinely missing venv is still reported
    as `needs_install` rather than as this error.
    """
    _declare(tmp_path, '"pandas>=2.0.0"')
    target = tmp_path / "declared.py"
    target.write_text("def main():\n    return 1\n")
    backend = _NoSyncBackend(_FakeResult(return_value="1"))
    monkeypatch.setattr(engine, "get_backend", lambda: backend)
    seen = _preflight_spy(monkeypatch)

    out = asyncio.run(engine.run_python(str(target), {}))
    assert out["ok"] is False
    assert seen == [["pandas>=2.0.0"]], (
        "the pre-flight was skipped, so a missing venv would be built inline"
    )
    assert backend.calls == [], "a second venv was built inline"
    assert "_execute_sync" in out["error"]["traceback"]


@requires_fused
def test_the_fast_path_is_not_taken_when_the_backend_cannot_run_on_an_interpreter(
    monkeypatch,
):
    """The gate itself, independent of any script."""
    monkeypatch.setattr(engine, "get_backend",
                        lambda: _NoSyncBackend(_FakeResult(return_value="1")))
    assert engine._interpreter_path_available() is False
    monkeypatch.setattr(engine, "get_backend",
                        lambda: _FakeBackend(_FakeResult(return_value="1")))
    assert engine._interpreter_path_available() is True


@requires_fused
def test_a_direct_url_requirement_is_never_treated_as_satisfied(monkeypatch):
    """`pandas @ https://…` names a SOURCE, which a version cannot vouch for.

    The extras hole in a second shape, and the more dangerous one: the app happens
    to have a `pandas`, so the header cleared and the wheel the author deliberately
    pinned was never fetched — the script ran against different code than it asked
    for, silently. Local paths and VCS URLs are the same case (`req.url` covers all
    three). This repo pinned its own `fused` as a direct-URL wheel for months, so it
    is an idiom a template author has every reason to copy.
    """
    monkeypatch.setattr(engine, "app_packages", lambda: {"pandas": "2.3.3"})
    assert engine.app_satisfies(["pandas"]) is True, "control: the plain name is met"
    for spec in (
        "pandas @ https://example.com/pandas-9.9.9-py3-none-any.whl",
        "pandas @ file:///tmp/pandas-9.9.9-py3-none-any.whl",
        "pandas @ git+https://github.com/pandas-dev/pandas@main",
    ):
        assert engine.app_satisfies([spec]) is False, spec


@requires_fused
def test_an_unparseable_requirement_is_not_satisfied():
    """"I could not read it" must never read as "it is already there"."""
    assert engine.app_satisfies(["not a requirement at all!!"]) is False


def test_a_failed_probe_means_not_satisfied(monkeypatch):
    """A probe that could not answer leaves the venv path in charge.

    Same discipline as `envinstall._venv_is_usable` and `engine._probe`: a timeout
    or a spawn failure is not evidence about what is installed, and acting on it as
    if it were would run a script against an interpreter we know nothing about.
    """
    monkeypatch.setattr(engine, "app_packages", lambda: None)
    assert engine.app_satisfies(["pandas"]) is False


def test_an_empty_requirement_list_is_not_the_fast_path(monkeypatch):
    """No header is PY-17's business, not this check's — and vacuous truth here
    would make `app_satisfies([])` answer True for a script that never asked."""
    monkeypatch.setattr(engine, "app_packages", lambda: {"pandas": "2.3.3"})
    assert engine.app_satisfies([]) is False


def test_a_backend_that_cannot_be_resolved_is_not_an_interpreter_path(monkeypatch):
    """`get_backend()` raising is a "no", not an error to propagate.

    The gate is the FIRST thing /api/run touches for a header, and `get_backend`
    raises outright when `fused` is absent — so letting that through made every
    more specific failure downstream unreachable. CI caught it: the plain
    `test-python` jobs install no `fused`, and
    `test_a_preflight_that_cannot_answer_returns_the_house_error_shape` saw a bare
    ModuleNotFoundError from this probe instead of the pre-flight's own diagnosis.
    """
    def _no_fused():
        raise ModuleNotFoundError("No module named 'fused'")

    monkeypatch.setattr(engine, "get_backend", _no_fused)
    assert engine._interpreter_path_available() is False


def test_a_missing_packaging_warns_instead_of_probing(monkeypatch):
    """The ImportError handler must be reachable — it sits above the probe.

    `_probe_app_packages` canonicalises with `packaging` too, so probing first let
    the very ImportError this handler exists for escape from underneath it. What
    the user got was not "the fast path is off" but an EngineError on every header
    script, for a dependency problem that has a graceful answer.
    """
    probed = []
    monkeypatch.setattr(engine, "app_packages",
                        lambda: probed.append(True) or {"pandas": "2.3.3"})
    # A None entry in sys.modules is what the import system treats as "this module
    # is not importable" — closer to a broken install than deleting the real one.
    monkeypatch.setitem(sys.modules, "packaging.requirements", None)
    assert engine.app_satisfies(["pandas"]) is False
    assert probed == [], "the probe ran, so it would have raised first"


# --- the PYTHONHOME wrapper: making the packaged macOS app work ---------------
#
# Measured against a real DMG (FusedRender-0.3.12), because every earlier guess
# about this bundle turned out wrong:
#
#   * stripped of PYTHONHOME, `Contents/MacOS/python` reports the BUILD
#     MACHINE's Homebrew framework as `sys.prefix` — a path absent on a user's
#     machine. So rung 1 cannot work there.
#   * the bundle ships NO `venv` module (not in Resources/lib/python3.12, not in
#     lib/python312.zip) and the embedded Python.framework holds only the dylib,
#     no second interpreter. A venv-based rescue is impossible, not just awkward.
#   * with PYTHONHOME restored, `sys.prefix` IS Contents/Resources and pandas /
#     geopandas / rasterio all import.
#
# CI cannot mount a DMG, so the stand-in below reproduces the property that
# matters: an interpreter whose stdlib AND site-packages live in one directory
# reachable only via PYTHONHOME. `test_the_stand_in_really_needs_pythonhome`
# keeps it honest.


def _bundle_like_python(tmp_path):
    """A python that can only find its stdlib AND one package via PYTHONHOME.

    py2app's shape, built with symlinks: ONE directory holding the stdlib and
    site-packages flattened together. Returns (base_interpreter, home, sentinel).

    The package is a sentinel this function WRITES, not a real distribution.
    Borrowing `pandas` made the whole file environment-dependent and it failed
    both ways in CI: on the runner where pandas is importable from the base
    interpreter the bare stand-in found it (so the guard below fired, correctly);
    on the runners where pandas is not installed at all there was nothing for the
    wrapper to find (so the positive assertions could not pass). A sentinel that
    exists ONLY inside the stand-in's lib dir makes both halves true by
    construction, on every runner, with no dependency on what is installed.
    """
    import sysconfig

    tag = "python3.%d" % sys.version_info[1]
    home = tmp_path / "fakebundle"
    libdir = home / "lib" / tag
    libdir.mkdir(parents=True)
    stdlib = sysconfig.get_paths()["stdlib"]
    if not os.path.isdir(stdlib):
        pytest.skip("no stdlib directory to model a bundle from")
    for name in os.listdir(stdlib):
        dst = libdir / name
        if not dst.exists():
            try:
                os.symlink(os.path.join(stdlib, name), dst)
            except OSError:
                pass
    # The app's "own package": importable only when PYTHONHOME points here.
    sentinel = "fused_render_bundle_sentinel"
    (libdir / f"{sentinel}.py").write_text("MARKER = 'from-the-bundle'\n")
    base = os.path.join(sys.base_prefix, "bin", "python3")
    if not os.path.exists(base):
        pytest.skip("no base interpreter to build a bundle-like stand-in from")
    return base, str(home), sentinel


def _stripped():
    """The env a backend child gets."""
    return {k: v for k, v in os.environ.items()
            if k not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONSTARTUP")}


@pytest.fixture
def bundle_like(tmp_path, monkeypatch):
    """This process as it is inside the .app: PYTHONHOME set, foreign raw prefix."""
    if os.name == "nt":
        pytest.skip("the wrapper is POSIX-only by design")
    base, home, sentinel = _bundle_like_python(tmp_path)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("FUSED_RENDER_APP_PYTHON", base)
    # The app claims the bundle's prefix, as py2app's launcher arranges.
    monkeypatch.setenv("PYTHONHOME", home)
    monkeypatch.setattr(sys, "prefix", home)
    return base, home, sentinel


def test_the_stand_in_really_needs_pythonhome(tmp_path):
    """Guard the guard: if the stand-in stops modelling the bundle, say so.

    A stand-in that worked WITHOUT PYTHONHOME would make every test below pass
    while testing nothing — the failure mode of any simulated environment.
    """
    if os.name == "nt":
        pytest.skip("POSIX-only")
    base, home, sentinel = _bundle_like_python(tmp_path)
    env = _stripped()
    bare = subprocess.run([base, "-c", f"import {sentinel}"], capture_output=True,
                          text=True, env=env, timeout=120)
    assert bare.returncode != 0, (
        "the stand-in must NOT find the app's package without PYTHONHOME, or every "
        "test below it passes while testing nothing"
    )
    with_home = subprocess.run(
        [base, "-c", f"import {sentinel},sys; print(sys.prefix, {sentinel}.MARKER)"],
        capture_output=True, text=True, env=dict(env, PYTHONHOME=home), timeout=120,
    )
    assert with_home.returncode == 0, with_home.stderr
    assert with_home.stdout.split() == [home, "from-the-bundle"]


def test_a_bundle_like_interpreter_is_rescued_by_the_wrapper(bundle_like):
    """The macOS path end to end: rung 1 rejected, rung 2 accepted."""
    base, home, sentinel = bundle_like
    resolved = engine.app_interpreter()
    assert resolved is not None, "the wrapper should have rescued this"
    assert resolved != base, "it must be the wrapper, not the rejected raw python"
    assert resolved == engine._wrapper_path()

    proc = subprocess.run(
        [resolved, "-c", f"import {sentinel},sys; print(sys.prefix)"],
        capture_output=True, text=True, env=_stripped(), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == home


def test_the_child_sees_the_WRAPPER_as_its_sys_executable(bundle_like):
    """`exec -a` is load-bearing, and this is why.

    geotiff/tile_server.py and zarr_aoi/tile_server.py spawn their daemons as
    `[sys.executable, …]` with PYTHONHOME **scrubbed** from the child env (their
    own comments explain why — a bundle-scoped PYTHONHOME would poison a uv
    venv). With the raw python as `sys.executable` that spawn loses the app's
    packages; measured on the real DMG as `ModuleNotFoundError: No module named
    'pandas'`. Pointing it at the wrapper makes the re-spawn immune to the scrub.
    """
    resolved = engine.app_interpreter()
    assert resolved is not None
    proc = subprocess.run(
        [resolved, "-c", "import sys; print(sys.executable)"],
        capture_output=True, text=True, env=_stripped(), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == resolved


def test_a_daemon_respawned_through_sys_executable_still_works(bundle_like):
    """The exact pattern geotiff/zarr/usd use, with the exact env they use.

    This is the difference between the feature working and a confusing second
    failure one level down, so it is asserted against the real spawn shape rather
    than inferred from sys.executable alone.
    """
    _base, _home, sentinel = bundle_like
    resolved = engine.app_interpreter()
    assert resolved is not None
    grandchild = (
        "import os, subprocess, sys\n"
        "denv = {k: v for k, v in os.environ.items() "
        "if k not in ('PYTHONPATH', 'PYTHONHOME')}\n"
        "r = subprocess.run([sys.executable, '-c', "
        f"'import {sentinel}; print(\"deep ok\")'], "
        "capture_output=True, text=True, env=denv)\n"
        "print(r.returncode, (r.stdout or r.stderr).strip().splitlines()[-1])\n"
    )
    proc = subprocess.run(
        [resolved, "-c", grandchild],
        capture_output=True, text=True, env=_stripped(), timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("0 deep ok"), proc.stdout


def test_the_wrapper_is_probed_not_trusted(bundle_like, monkeypatch):
    """It must earn its place by running, like any other candidate."""
    real_probe = engine._probe
    probed = []

    def spy(exe):
        probed.append(exe)
        return real_probe(exe)

    monkeypatch.setattr(engine, "_probe", spy)
    resolved = engine.app_interpreter()
    assert resolved in probed, "the wrapper was accepted without being run"


def test_a_wrapper_that_does_not_work_is_rejected(bundle_like, monkeypatch):
    """A wrapper pointing somewhere useless must not be returned."""
    monkeypatch.setattr(engine, "_wrapper_interpreter", lambda c: ("/bin/false", ""))
    assert engine.app_interpreter() is None


def test_the_wrapper_quotes_paths_with_spaces(tmp_path, monkeypatch):
    """A DMG can be mounted at `/Volumes/Fused Render`; nothing may split on it."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "a state dir"))
    home = tmp_path / "home with spaces"
    (home / "lib").mkdir(parents=True)
    monkeypatch.setenv("PYTHONHOME", str(home))
    weird = tmp_path / "py dir" / "python"
    weird.parent.mkdir()
    weird.write_text("")
    path, detail = engine._wrapper_interpreter(str(weird))
    assert path is not None, detail
    body = open(path, encoding="utf-8").read()
    assert shlex.quote(str(home)) in body
    assert shlex.quote(str(weird)) in body
    # Tokenising the exec line must recover the paths intact, not split them.
    exec_line = [ln for ln in body.splitlines() if ln.startswith("exec ")][0]
    assert str(weird) in shlex.split(exec_line)
    assert '"$@"' in exec_line, "argv must be forwarded quoted"


def test_the_wrapper_is_private_and_regenerated_only_when_it_changes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "state"))
    home = tmp_path / "home"
    (home / "lib").mkdir(parents=True)
    monkeypatch.setenv("PYTHONHOME", str(home))

    path, _ = engine._wrapper_interpreter("/usr/bin/python3")
    assert path is not None
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700, "derived state, owner-only"
    first = os.stat(path).st_mtime_ns
    body = open(path, encoding="utf-8").read()

    # Same inputs -> untouched, not rewritten with identical content.
    again, _ = engine._wrapper_interpreter("/usr/bin/python3")
    assert again == path
    assert os.stat(path).st_mtime_ns == first
    assert open(path, encoding="utf-8").read() == body

    # Different candidate -> regenerated.
    engine._wrapper_interpreter("/usr/bin/python3.11")
    assert open(path, encoding="utf-8").read() != body


def test_no_wrapper_without_a_pythonhome_to_restore(tmp_path, monkeypatch):
    """Gated to the case that needs it: a self-locating interpreter gets none.

    This is what keeps Windows and the Linux AppImage on rung 1.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PYTHONHOME", raising=False)
    path, detail = engine._wrapper_interpreter(sys.executable)
    assert path is None
    assert "PYTHONHOME" in detail
    assert not os.path.exists(engine._wrapper_path())


def test_a_bogus_pythonhome_is_not_wrapped(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "does-not-exist"))
    path, _detail = engine._wrapper_interpreter(sys.executable)
    assert path is None


def test_the_wrapper_lives_outside_the_script_venv_path(tmp_path, monkeypatch):
    """It must not be mistakable for a requirements venv.

    `fused`'s venvs_path is keyed by requirement set, and
    `ensure_requirements_venv` deletes directories there lacking its ready marker.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "state"))
    p = engine._wrapper_path()
    assert "_app_interpreter" in p
    assert "openfused" not in p
    assert os.path.basename(p) == "python", "should read as an interpreter in logs/ps"


# --- concurrency: the probe runs off the event loop, so it runs in parallel ----
#
# `/api/run` does `await asyncio.to_thread(app_interpreter)` (so a slow probe
# cannot stall the loop — test_the_interpreter_probe_does_not_block_the_event_loop
# pins that). The cost is that two header-less runs starting together are now
# genuinely concurrent, and both halves of this used to race: an unlocked
# read-then-write cache whose losing thread could store `None` over a working
# path (terminal, per process — every header-less script broken until restart),
# and a wrapper temp file named by pid alone, which two threads of ONE process
# share so the first `os.replace` steals it from the second.


def test_concurrent_probes_resolve_once_and_never_cache_the_loser(monkeypatch):
    """Many threads, ONE probe, and no caller left holding None.

    The probe is deliberately made to succeed only the first time: without
    serialization the extra probes are not merely wasteful, one of them caches
    its failure over the answer that worked.
    """
    candidate, _autodetected = engine._interpreter_candidate()
    seen = []
    counter = threading.Lock()

    def probe_once(exe):
        with counter:
            first = not seen
            seen.append(exe)
        time.sleep(0.2)  # wide enough that every thread is inside at once
        if first:
            return {"prefix": sys.prefix, "executable": exe}, ""
        return None, "a second probe of the same candidate deliberately fails"

    monkeypatch.setattr(engine, "_probe", probe_once)
    # No rung-2 rescue: a failed direct probe is a cached None, which is the
    # poisoning this test is about.
    monkeypatch.setattr(engine, "_wrapper_interpreter", lambda c: (None, "stub"))

    results = []
    ready = threading.Barrier(8)

    def call():
        ready.wait(timeout=30)
        results.append(engine.app_interpreter())

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "app_interpreter deadlocked"

    assert len(seen) == 1, f"the candidate was probed {len(seen)} times, not once"
    assert results == [candidate] * 8, (
        f"concurrent callers disagreed about the interpreter: {results}"
    )
    # And the cache kept the working answer, not a loser's None.
    assert engine.app_interpreter() == candidate
    assert len(seen) == 1, "the cached answer was re-probed"


def test_two_threads_writing_the_wrapper_both_succeed(tmp_path, monkeypatch):
    """The temp file must be per-THREAD, not per-process.

    `os.chmod` is where both threads are held until both are past the write, so
    the overlap is deterministic rather than a matter of timing: with one shared
    temp name the first `os.replace` consumes it and the second raises
    FileNotFoundError, reported as "could not write the interpreter wrapper" for a
    wrapper that is perfectly fine — and that spurious failure is then cached.
    """
    if os.name == "nt":
        pytest.skip("the wrapper is POSIX-only by design")
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "state"))
    home = tmp_path / "home"
    (home / "lib").mkdir(parents=True)
    monkeypatch.setenv("PYTHONHOME", str(home))
    wrapper_path = engine._wrapper_path()

    both_written = threading.Barrier(2)
    real_chmod = os.chmod

    def chmod_holding_both(path, mode, *a, **kw):
        real_chmod(path, mode, *a, **kw)
        if str(path).startswith(wrapper_path) and str(path).endswith(".tmp"):
            both_written.wait(timeout=30)

    monkeypatch.setattr(os, "chmod", chmod_holding_both)

    out = []
    threads = [
        threading.Thread(
            target=lambda: out.append(engine._wrapper_interpreter("/usr/bin/python3"))
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a wrapper write hung"

    assert out == [(wrapper_path, "")] * 2, f"a wrapper write lost its temp file: {out}"
    body = open(wrapper_path, encoding="utf-8").read()
    assert f"PYTHONHOME={shlex.quote(str(home))}" in body
    assert shlex.quote("/usr/bin/python3") in body
    assert not [
        n for n in os.listdir(os.path.dirname(wrapper_path)) if n.endswith(".tmp")
    ], "a temp file was left behind"

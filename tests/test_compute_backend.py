"""The vendored compute backend: one script, one subprocess, one parsed result."""

import json
import sys

import pytest

from fused_render.compute.backend import (
    _HANDLER_FILES,
    _STRIPPED_ENV_VARS,
    LocalPythonComputeBackend,
)
from fused_render.compute.result import ExecutionResult, ResponseMeta

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix_spawn path")

MAIN = "result = {'x': 1}\n"


@pytest.fixture
def backend():
    b = LocalPythonComputeBackend(timeout_seconds=60, python_executable=sys.executable)
    yield b
    if b._dispatcher is not None:
        b._dispatcher.stop()


def test_every_handler_file_ships_beside_the_backend():
    from fused_render.compute.backend import _HANDLER_DIR

    for name in _HANDLER_FILES:
        assert (_HANDLER_DIR / name).is_file(), name


def test_a_real_call_returns_the_scripts_value(backend):
    r = backend._execute_sync(MAIN + "print('hello')\n")
    assert r.error is None, r
    assert json.loads(r.return_value) == {"x": 1}
    assert "hello" in r.stdout


def test_input_files_reach_the_script(backend):
    code = "import json\nresult = json.load(open('x.json'))\n"
    r = backend._execute_sync(code, input_files={"x.json": b'{"a": 2}'})
    assert r.error is None, r
    assert json.loads(r.return_value) == {"a": 2}


def test_a_raising_script_is_an_error_not_an_exception(backend):
    r = backend._execute_sync("raise ValueError('boom')\n")
    assert r.error
    assert "boom" in r.error


def test_the_interpreter_argument_wins_over_the_default(tmp_path, monkeypatch, backend):
    spawned = []

    class Stop(BaseException):
        pass

    def _run(cmd, **kw):
        spawned.append(cmd[0])
        raise Stop()

    monkeypatch.setattr("fused_render.compute.backend.subprocess.run", _run)
    with pytest.raises(Stop):
        backend._execute_sync(MAIN, interpreter="/explicit/python")
    with pytest.raises(Stop):
        backend._execute_sync(MAIN)
    assert spawned == ["/explicit/python", sys.executable]


def test_a_timeout_is_reported_as_the_error():
    b = LocalPythonComputeBackend(timeout_seconds=1, python_executable=sys.executable)
    try:
        r = b._execute_sync("import time\ntime.sleep(30)\n")
    finally:
        if b._dispatcher is not None:
            b._dispatcher.stop()
    assert r.error == "Execution timed out after 1s"


def test_make_event_writes_the_context_dir(tmp_path, backend):
    event = backend._make_event("x=1", {"sub/a.txt": b"hi"}, tmp_path)
    assert event["code"] == "x=1"
    assert event["call_dir_root"] == str(tmp_path)
    assert (tmp_path / "context" / "sub" / "a.txt").read_bytes() == b"hi"
    assert event["context_path"] == str(tmp_path / "context")
    assert "context_path" not in backend._make_event("x=1", None, tmp_path)


def test_child_env_strips_the_interpreter_distorting_vars(monkeypatch, backend):
    for var in _STRIPPED_ENV_VARS:
        monkeypatch.setenv(var, "/evil")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = backend._child_env("/inv", 2, "/the/python", "/proj")
    assert not set(_STRIPPED_ENV_VARS) & set(env)
    assert env["KEEP_ME"] == "yes"
    assert env["OPENFUSED_LOCAL_INVOKE_DIR"] == "/inv"
    assert env["OPENFUSED_INVOKE_DEPTH"] == "2"
    assert env["OPENFUSED_LOCAL_WORKFLOW_INTERPRETER"] == "/the/python"
    assert env["OPENFUSED_PROJECT_ROOT"] == "/proj"


def test_child_env_clears_a_stale_inherited_interpreter(monkeypatch, backend):
    monkeypatch.setenv("OPENFUSED_LOCAL_WORKFLOW_INTERPRETER", "/stale")
    env = backend._child_env("/inv", 0)
    assert "OPENFUSED_LOCAL_WORKFLOW_INTERPRETER" not in env


@pytest.mark.parametrize(
    "rc,out,err,expect",
    [
        (1, "", "Traceback...", "Traceback..."),
        (3, "", "", "Runner exited with code 3"),
        (0, "", "", "runner produced no result line"),
        (0, "not json\n", "", "runner result line was not valid JSON"),
    ],
)
def test_result_from_run_turns_every_failure_into_an_error(backend, rc, out, err, expect):
    r = backend._result_from_run(rc, out, err, 5)
    assert isinstance(r, ExecutionResult)
    assert r.error == expect
    assert r.return_value is None


def test_result_from_run_folds_the_handler_error_into_stderr(backend):
    line = json.dumps({"stdout": "o", "stderr": "", "return_value": None, "error": "bad"})
    r = backend._result_from_run(0, line + "\n", "", 5)
    assert r.error == "bad"
    assert r.stderr == "bad"
    assert r.stdout == "o"


def test_result_from_run_carries_the_response_meta(backend):
    line = json.dumps(
        {"stdout": "", "stderr": "", "return_value": "x", "response": {"media_type": "text/csv"}}
    )
    r = backend._result_from_run(0, line, "", 5)
    assert r.error is None
    assert r.response == ResponseMeta(media_type="text/csv")
    assert ResponseMeta.from_wire(r.response.to_wire()) == r.response


def test_the_dispatcher_runs_a_brokered_child(backend):
    import time
    from pathlib import Path

    root = Path(backend._ensure_dispatcher().host_dir)
    req = {"id": "t1", "code": "result = 41 + 1\n", "depth": 1, "files": {}}
    (root / "requests" / "t1.json").write_text(json.dumps(req))
    out = root / "results" / "t1.json"
    deadline = time.monotonic() + 30
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    body = json.loads(out.read_text())
    assert body["error"] is None, body
    assert json.loads(body["return_value"]) == 42

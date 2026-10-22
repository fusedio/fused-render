# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see NOTICE.
"""Local compute: run one script in a host subprocess on a given interpreter.

`LocalPythonComputeBackend` writes the handler files (`handler/`) into a private
per-call temp directory and executes `<interpreter> run.py` there:

- The interpreter is the project's own venv python (passed per call). A call
  with no interpreter falls back to `python_executable`, then `sys.executable`.
- Per-call isolation comes from the handler itself: each call runs user code in
  a fresh child process inside a unique directory under the per-call temp dir,
  handed to the handler as `event["call_dir_root"]`. It is NOT a machine-wide
  `/tmp/exec`: `/tmp` is shared by every account on a host, so the first account
  to run a call would own it and every other account then fail every call with
  `PermissionError`.
- The child inherits the host environment (minus `PYTHONPATH`/`PYTHONHOME`/
  `VIRTUAL_ENV`/`PYTHONSTARTUP`, stripped so the venv stays hermetic).
- Local fan-out works through the dispatcher directory, passed by host path via
  `OPENFUSED_LOCAL_INVOKE_DIR`.

There is no container boundary: user code runs as the invoking user.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fused_render.compute.result import ExecutionResult, ResponseMeta

if TYPE_CHECKING:
    from fused_render.compute.invoke import LocalInvokeDispatcher

logger = logging.getLogger(__name__)

_HANDLER_DIR = Path(__file__).resolve().parent / "handler"

# The files the run script imports, written into every call's directory.
_HANDLER_FILES = ("handler.py", "_runner.py", "_openfused.py", "fused.py", "openfused.py")

# Wrapper script written to the temp dir and executed by the interpreter. The
# wrapper resolves paths against the script's own directory; the workspace is
# wherever the script itself lives on the host. It chdirs there itself rather
# than being handed `cwd=` by the parent, so the parent's spawn qualifies for
# posix_spawn (see the spawn in `_execute_sync`).
_RUN_SCRIPT = """\
import json
import os
import sys

workspace = os.path.dirname(os.path.abspath(__file__))
os.chdir(workspace)
sys.path.insert(0, workspace)
from handler import lambda_handler

with open(os.path.join(workspace, "event.json")) as fh:
    event = json.load(fh)

result = lambda_handler(event, None)
print(json.dumps(result))
"""

# Host env vars stripped from the child so the interpreter's module resolution is
# not distorted by the parent's setup. Everything else (cloud creds, proxies,
# locale) is inherited.
_STRIPPED_ENV_VARS = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP")


class LocalPythonComputeBackend:
    """Executes Python code on the host with a caller-supplied interpreter."""

    def __init__(
        self,
        timeout_seconds: int = 300,
        python_executable: str | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        # Interpreter used when a call names none. An embedding application whose
        # own interpreter is not a usable runner (a py2app-packaged .app) passes
        # a real standalone python here.
        self.python_executable = python_executable
        # Host broker for local fan-out (_openfused.invoke); created lazily so a
        # backend that never executes never spawns a watcher thread. The lock
        # makes first use under concurrent executes create exactly one dispatcher
        # (no orphaned watcher thread or leaked temp dir from a check-then-set race).
        self._dispatcher: LocalInvokeDispatcher | None = None
        self._dispatcher_lock = threading.Lock()

    def _ensure_dispatcher(self) -> LocalInvokeDispatcher:
        """Lazily create and start the host fan-out dispatcher; return it."""
        with self._dispatcher_lock:
            if self._dispatcher is None:
                from fused_render.compute.invoke import LocalInvokeDispatcher

                self._dispatcher = LocalInvokeDispatcher(self)
        self._dispatcher.ensure_started()
        return self._dispatcher

    def _write_handler_files(self, tmppath: Path) -> None:
        """Write the handler files the call's run script imports."""
        for name in _HANDLER_FILES:
            (tmppath / name).write_bytes((_HANDLER_DIR / name).read_bytes())

    def _child_env(
        self,
        invoke_dir: str,
        invoke_depth: int,
        interpreter: str | None = None,
        project_root: str | None = None,
    ) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV_VARS}
        env["OPENFUSED_LOCAL_INVOKE_DIR"] = invoke_dir
        env["OPENFUSED_INVOKE_DEPTH"] = str(invoke_depth)
        # Propagate the interpreter so fan-out children (which write requests the
        # dispatcher runs) inherit it; clear any inherited value when this call
        # named none so children don't pick up a stale one.
        if interpreter is not None:
            env["OPENFUSED_LOCAL_WORKFLOW_INTERPRETER"] = interpreter
        else:
            env.pop("OPENFUSED_LOCAL_WORKFLOW_INTERPRETER", None)
        # Expose the project root so a project script can read its bundled data
        # files portably via ``os.environ["OPENFUSED_PROJECT_ROOT"]``: the child
        # always runs in an isolated per-call temp cwd, so a relative
        # ``./assets/...`` path can only resolve through this.
        if project_root is not None:
            env["OPENFUSED_PROJECT_ROOT"] = project_root
        elif os.environ.get("OPENFUSED_PROJECT"):
            env["OPENFUSED_PROJECT_ROOT"] = os.getcwd()
        return env

    def _make_event(
        self,
        code: str,
        input_files: dict[str, bytes] | None,
        tmppath: Path,
        entrypoint: str | None = None,
    ) -> dict:
        """Build the handler event."""
        # Per-call working directory: hand the handler the private per-call temp
        # dir so no shared directory is involved. It also means the call dir is
        # cleaned up by the `TemporaryDirectory` even when the handler's own
        # `finally` never runs (a hard kill).
        event: dict = {"code": code, "call_dir_root": str(tmppath)}
        if entrypoint:
            event["entrypoint"] = entrypoint
        if input_files:
            context_dir = tmppath / "context"
            context_dir.mkdir()
            for name, content in input_files.items():
                dest = context_dir / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
            event["context_path"] = str(context_dir)
        return event

    def _result_from_run(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
        duration_ms: int,
    ) -> ExecutionResult:
        """Parse the runner's wire protocol (one JSON line on stdout) into a result.

        A non-zero exit, a missing result line, or an unparseable line each
        become a structured error rather than an exception.
        """
        if returncode != 0:
            exit_msg = stderr or f"Runner exited with code {returncode}"
            return ExecutionResult(
                stdout=stdout,
                stderr=exit_msg,
                return_value=None,
                duration_ms=duration_ms,
                error=exit_msg,
            )

        lines = [ln for ln in stdout.strip().splitlines() if ln]
        if not lines:
            no_output_msg = stderr or "runner produced no result line"
            return ExecutionResult(
                stdout="",
                stderr=no_output_msg,
                return_value=None,
                duration_ms=duration_ms,
                error=no_output_msg,
            )

        try:
            body = json.loads(lines[-1])
        except json.JSONDecodeError:
            unparseable_msg = stderr or "runner result line was not valid JSON"
            return ExecutionResult(
                stdout=stdout,
                stderr=unparseable_msg,
                return_value=None,
                duration_ms=duration_ms,
                error=unparseable_msg,
            )

        # The handler reports failures in a separate `error` field (which can
        # carry no stderr). Fold it into stderr so callers surface it.
        body_stderr = body.get("stderr", "")
        error = body.get("error")
        if error and error not in body_stderr:
            body_stderr = f"{body_stderr}\n{error}".strip() if body_stderr else error

        return ExecutionResult(
            stdout=body.get("stdout", ""),
            stderr=body_stderr,
            return_value=body.get("return_value"),
            duration_ms=duration_ms,
            response=ResponseMeta.from_wire(body.get("response")),
            error=error or None,
            warnings=body.get("warnings"),
        )

    def _execute_sync(
        self,
        code: str,
        input_files: dict[str, bytes] | None = None,
        invoke_depth: int = 0,
        interpreter: str | None = None,
        project_root: str | None = None,
        entrypoint: str | None = None,
    ) -> ExecutionResult:
        """Run one call synchronously and return its result."""
        venv_python = interpreter or self.python_executable or sys.executable
        dispatcher = self._ensure_dispatcher()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            self._write_handler_files(tmppath)
            event = self._make_event(code, input_files, tmppath, entrypoint)

            (tmppath / "event.json").write_text(json.dumps(event))
            run_py = tmppath / "run.py"
            run_py.write_text(_RUN_SCRIPT)

            t0 = time.monotonic()
            try:
                proc = subprocess.run(
                    [venv_python, str(run_py)],
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    env=self._child_env(
                        dispatcher.host_dir, invoke_depth, interpreter, project_root
                    ),
                    # Force CPython onto posix_spawn instead of fork()+exec. A
                    # forked child runs pthread_atfork handlers, and PROJ's
                    # (live whenever the parent has touched pyproj / rasterio /
                    # geopandas / fiona) closes its inherited-but-invalid
                    # proj.db sqlite handle and takes a SIGSEGV *before* exec,
                    # surfacing as a bare returncode -11 with no traceback and
                    # no child stderr. posix_spawn runs no atfork handlers.
                    # `subprocess._execute_child` requires BOTH `close_fds`
                    # falsey AND `cwd is None` to take that path, so neither is
                    # sufficient alone: `_RUN_SCRIPT` chdirs to its own
                    # directory in the child, which is why no `cwd=` is passed
                    # here and the child's working directory is unchanged.
                    # The cost is that this child inherits our open descriptors.
                    # Acceptable because it execs a first-party wrapper, lives
                    # for one call, and (the load-bearing part) the handler's
                    # own spawn of USER code (handler/handler.py, the runner
                    # subprocess) leaves `close_fds` at its default True, so the
                    # inherited descriptors are dropped at that boundary and
                    # never reach untrusted code. That default MUST stay.
                    close_fds=False,
                )
            except subprocess.TimeoutExpired:
                timeout_msg = f"Execution timed out after {self._timeout_seconds}s"
                return ExecutionResult(
                    stdout="",
                    stderr=timeout_msg,
                    return_value=None,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    error=timeout_msg,
                )

            duration_ms = int((time.monotonic() - t0) * 1000)
            return self._result_from_run(proc.returncode, proc.stdout, proc.stderr, duration_ms)

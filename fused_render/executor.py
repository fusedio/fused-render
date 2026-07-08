"""Runs a Python file's main() and returns its JSON result.

Two execution paths (D72):

- **User code** — a script the user is running (the `api` template's Run
  button) or a user-authored template's reader — runs in a **fresh isolated
  subprocess** per call (SPEC PY-6, D5): always-fresh code, no stale state, and
  a crash or `sys.exit` can't take down the server.
- **First-party helpers we ship under `templates/`** — the table/csv/xlsx
  readers and the `api` inspector — run **in-process**. They are trusted and,
  crucially, none of them import or execute user code (the readers open a data
  file; the inspector `ast`-parses a .py without importing it). Running them in
  the server (= app) process means the Downloads/Desktop/Documents access they
  perform is attributed to the app the user already granted, instead of to a
  freshly-spawned interpreter that macOS TCC re-prompts for on *every* call.
  That repeated prompting — one per preview/pagination/slider tick on a file
  under a protected folder — was the bug this split fixes; it also drops the
  per-call pandas/pyarrow re-import cost, since those stay warm in the server.
"""
import importlib.util
import json
import logging
import os
import subprocess
import sys
import traceback

from ._binding import bind_params

logger = logging.getLogger(__name__)

CHILD = os.path.join(os.path.dirname(__file__), "_child.py")
# Realpath so a symlinked helper can't smuggle a user path past the check.
BUILTIN_TEMPLATES_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "templates"))
DEFAULT_TIMEOUT = 30.0


def _error(err_type: str, message: str, detail: str = "") -> dict:
    return {
        "ok": False,
        "error": {"type": err_type, "message": message, "traceback": detail},
        "stdout": "",
    }


def _is_builtin_helper(path: str) -> bool:
    """True when `path` is one of the first-party helper scripts we ship under
    templates/. Those are trusted and never import/exec user code, so they run
    in-process (D72). A user template's reader lives under ~/.fused-render/, and
    a user script runs from wherever it is — neither is under here, so both stay
    subprocess-isolated.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return os.path.commonpath([real, BUILTIN_TEMPLATES_DIR]) == BUILTIN_TEMPLATES_DIR


def _run_inprocess(path: str, params: dict) -> dict:
    """Execute a first-party helper's main() in this process. Same result shape
    and param binding as the subprocess path; catches BaseException so a helper
    error (or a stray SystemExit) surfaces as a normal error dict instead of
    tearing down the server thread. No timeout: these are bounded local-file
    reads / ast parses, not arbitrary user code.

    Thread-safe under FastAPI's threadpool (RH-4): it mutates no process-global
    state. The helper module is built with `module_from_spec` + `exec_module`
    and is *never* inserted into `sys.modules`, so the fixed spec name is inert
    and concurrent calls get independent module objects. `sys.path` is left
    untouched — built-in helpers are self-contained (stdlib + the data stack,
    never a sibling imported by name), so there is nothing to add, and mutating
    the shared path would race concurrent imports. stdout is likewise NOT
    captured: helpers don't print, and redirecting the process-global
    `sys.stdout` would race concurrent calls.
    """
    spec = importlib.util.spec_from_file_location("__fused_builtin__", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        fn = getattr(mod, "main", None)
        if not callable(fn):
            raise AttributeError(
                f"{os.path.basename(path)} does not define a callable 'main' function"
            )
        result = fn(**bind_params(fn, params))
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            raise TypeError(
                f"main() returned {type(result).__name__}, which is not JSON-serializable; "
                "return dict/list/str/number/bool/None (e.g. df.to_dict('records'))"
            ) from None
        return {"ok": True, "result": result, "stdout": ""}
    except BaseException as e:  # noqa: BLE001 — mirror the child's catch-all
        return {
            "ok": False,
            "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            },
            "stdout": "",
        }


def run_python(path: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    result = _run_python(path, params, timeout)
    if not result.get("ok"):
        # A failed run is the common "something wrong with right-click open"
        # symptom, and the browser only flashes it in an error overlay. Record
        # it here — with the worker's traceback in `detail` — so the log file
        # explains a failure the user has since clicked away from.
        err = result.get("error") or {}
        logger.warning(
            "run failed for %s: %s: %s\n%s",
            path,
            err.get("type", "Error"),
            err.get("message", ""),
            err.get("traceback", ""),
        )
    return result


def _run_python(path: str, params: dict, timeout: float) -> dict:
    if not os.path.isfile(path):
        return _error("FileNotFoundError", f"no such Python file: {path}")

    # First-party helper -> in-process so its protected-folder access reuses
    # the app's TCC grant (D72). Everything else is user code -> subprocess.
    if _is_builtin_helper(path):
        return _run_inprocess(path, params or {})

    request = json.dumps({"path": path, "params": params or {}})
    try:
        proc = subprocess.run(
            [sys.executable, CHILD],
            input=request,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _error("TimeoutError", f"execution exceeded {timeout:g}s and was killed")
    except OSError as e:
        # Couldn't even spawn the worker (bad interpreter path, out of fds, …).
        # Return the normal wire shape rather than letting it 500 unlabeled.
        return _error("ExecutorError", f"could not start worker process: {e}")

    lines = proc.stdout.strip().splitlines()
    if lines:
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError:
            pass
    return _error(
        "ExecutorError",
        f"worker exited with code {proc.returncode} without producing a result",
        proc.stderr[-4000:],
    )

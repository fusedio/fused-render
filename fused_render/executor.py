"""Runs a Python file's main() in an isolated subprocess with a timeout."""
import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

CHILD = os.path.join(os.path.dirname(__file__), "_child.py")
DEFAULT_TIMEOUT = 30.0


def _error(err_type: str, message: str, detail: str = "") -> dict:
    return {
        "ok": False,
        "error": {"type": err_type, "message": message, "traceback": detail},
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

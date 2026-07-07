"""Runs a Python file's main() in an isolated subprocess with a timeout."""
import json
import os
import subprocess
import sys

CHILD = os.path.join(os.path.dirname(__file__), "_child.py")
DEFAULT_TIMEOUT = 30.0


def _error(err_type: str, message: str, detail: str = "") -> dict:
    return {
        "ok": False,
        "error": {"type": err_type, "message": message, "traceback": detail},
        "stdout": "",
    }


def run_python(path: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
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

"""Run one UDF node's file in Fused and print the result as JSON on stdout.

Run as ``[sys.executable, _fused_node_run.py]`` by canvases.py (same
in-interpreter spawn pattern as _fused_canvas_favourites.py: the real fused
SDK is importable here and never in the server process), with the request as
a JSON object on STDIN:

  {"path": "<abs path of a node's .py>", "max_bytes": N}

What comes back is Fused's own rendering of the result, not ours. The run asks
for the formats a shared UDF's link serves — ``png`` for a raster, ``html``
for everything else (a table comes back as a self-contained page, an HTML
string as itself) — and this shim hands the bytes on untouched:

  {"ok": true, "content_type": "...", "body_b64": "...", "stdout": "...",
   "stderr": "...", "cached": bool, "seconds": float}
  {"ok": false, "error": "...", "line": int | null, "stdout": "...",
   "stderr": "..."}

A run that RAISED is an answer (``ok: false``, exit 0): the page shows the
error under the node's name. Only a failure to run at all (no login, an
unreadable file, a helper bug) exits non-zero with the reason on stderr.

The code is the file as it stands on disk, so an edit shows before it is
pushed. What the UDF loads from OTHER nodes (`fused.load`) is the canvas as
Fused has it saved.

FUSED_ENV is honored the same way the CLI does it (fused._env), so the run
goes to the environment the canvas was cloned from.
"""
import base64
import json
import os
import sys

import fused

_env_name = os.environ.get("FUSED_ENV")
if _env_name:
    fused._env(_env_name)

# Raster first, then everything else: the pair a shared UDF link's `.png` and
# `.html` endings ask for, one each.
RESULT_FORMAT = "png,html"


def _fail(message: str) -> None:
    sys.stderr.write(message)
    sys.exit(1)


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def result_payload(result, max_bytes: int) -> dict:
    """The JSON answer for one `fused.run(..., _return_response=True)` result."""
    stdout, stderr = _text(getattr(result, "stdout", None)), _text(getattr(result, "stderr", None))
    error = getattr(result, "error_message", None)
    if error or getattr(result, "has_exception", False):
        line = getattr(result, "error_lineno", None)
        return {
            "ok": False,
            "error": _text(error) or "the UDF raised an error",
            "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
            "stdout": stdout,
            "stderr": stderr,
        }
    content = getattr(result, "original_content", None) or b""
    headers = getattr(result, "original_headers", None) or {}
    content_type = ""
    for key in ("content-type", "Content-Type"):
        try:
            content_type = headers.get(key) or content_type
        except Exception:  # noqa: BLE001 — headers of an unexpected shape
            pass
    if len(content) > max_bytes:
        return {
            "ok": False,
            "error": (f"the result is {len(content) / 1e6:.1f} MB, too large to show here "
                      f"(the limit is {max_bytes / 1e6:.0f} MB)"),
            "line": None,
            "stdout": stdout,
            "stderr": stderr,
        }
    seconds = getattr(result, "time_taken_seconds", None)
    return {
        "ok": True,
        "content_type": _text(content_type),
        "body_b64": base64.b64encode(content).decode("ascii"),
        "stdout": stdout,
        "stderr": stderr,
        "cached": bool(getattr(result, "is_cached", False)),
        "seconds": float(seconds) if isinstance(seconds, (int, float)) else None,
    }


def main() -> None:
    try:
        req = json.load(sys.stdin)
    except ValueError:
        req = None
    if not isinstance(req, dict):
        _fail("node run helper: expected a JSON object on stdin")
        return
    path = req.get("path")
    if not isinstance(path, str) or not os.path.isabs(path) or not path.endswith(".py"):
        _fail("node run helper: 'path' must be the absolute path of a .py file")
        return
    max_bytes = req.get("max_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        _fail("node run helper: 'max_bytes' must be a positive integer")
        return
    try:
        udf = fused.load(path)
    except Exception as exc:  # noqa: BLE001 — a file that is no UDF is an answer
        json.dump({"ok": False, "error": f"could not load this file as a UDF: {exc}",
                   "line": None, "stdout": "", "stderr": ""}, sys.stdout)
        return
    try:
        result = fused.run(udf, engine="remote", _format=RESULT_FORMAT,
                           _return_response=True)
    except Exception as exc:  # noqa: BLE001 — the SDK raises some run errors
        json.dump({"ok": False, "error": str(exc) or type(exc).__name__,
                   "line": None, "stdout": "", "stderr": ""}, sys.stdout)
        return
    json.dump(result_payload(result, max_bytes), sys.stdout)


if __name__ == "__main__":
    main()

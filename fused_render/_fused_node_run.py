"""Run one UDF node's file in Fused and print the result as JSON on stdout.

Run as ``[sys.executable, _fused_node_run.py]`` by canvases.py (same
in-interpreter spawn pattern as _fused_canvas_favourites.py: the real fused
SDK is importable here and never in the server process), with the request as
a JSON object on STDIN:

  {"path": "<abs path of a node's .py>", "ask_bytes": N | null, "max_bytes": N}

What comes back is Fused's own rendering of the result, not ours. The run asks
for the formats a shared UDF's link serves — ``png`` for a raster, ``html``
for everything else (a table comes back as a self-contained page, an HTML
string as itself) — and this shim hands the bytes on untouched:

  {"ok": true, "content_type": "...", "body_b64": "...", "stdout": "...",
   "stderr": "...", "cached": bool, "seconds": float}
  {"ok": false, "error": "...", "line": int | null, "stdout": "...",
   "stderr": "..."}
  {"ok": false, "too_large": true, "size": int, "limit": int, "stdout": "...",
   "stderr": "..."}        a result past `ask_bytes`: the page asks first

A BATCH JOB (a decorator that names an engine) is waited for by the SDK and
comes back as the value itself, with no response bytes to hand on: a table is
then written out here as a plain HTML table, anything else as its text.

Whatever the SDK prints while it runs ("Batch job started: job://…") is kept
off this process's stdout, which carries the one JSON answer, and joins the
run's own output.

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
import contextlib
import io
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


# Rows of a batch job's table written out here (`data_page`): a page to look
# at, not an export.
DATA_PAGE_MAX_ROWS = 1000


def data_page(data) -> tuple[bytes, str]:
    """(body, content type) for a result that came back as a VALUE (a batch
    job's): a table as an HTML table, bytes as they are, the rest as text."""
    if data is None:
        return b"", ""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data), "application/octet-stream"
    if isinstance(data, str):
        return data.encode("utf-8"), "text/plain; charset=utf-8"
    to_html = getattr(data, "to_html", None)
    if callable(to_html) and hasattr(data, "columns"):
        frame = data
        to_wkt = getattr(frame, "to_wkt", None)
        if callable(to_wkt):  # geometry as text, as Fused's own table page shows it
            frame = to_wkt()
        total = len(frame)
        html = frame.head(DATA_PAGE_MAX_ROWS).to_html(border=0)
        note = (f"<p>Showing the first {DATA_PAGE_MAX_ROWS} of {total} rows.</p>"
                if total > DATA_PAGE_MAX_ROWS else "")
        page = ("<html><head><meta charset='utf-8'><style>"
                "body{font:13px/1.4 -apple-system,'Segoe UI',Roboto,sans-serif;margin:12px}"
                "table{border-collapse:collapse}th,td{padding:4px 10px;text-align:left;"
                "border-bottom:1px solid #ddd;white-space:nowrap}th{position:sticky;top:0;"
                "background:#fff}</style></head><body>" + note + html + "</body></html>")
        return page.encode("utf-8"), "text/html; charset=utf-8"
    if isinstance(data, (dict, list, int, float, bool)):
        try:
            return json.dumps(data, default=str).encode("utf-8"), "application/json"
        except (TypeError, ValueError):
            pass
    return repr(data).encode("utf-8"), "text/plain; charset=utf-8"


def result_payload(result, max_bytes: int, ask_bytes: int | None = None,
                   printed: str = "") -> dict:
    """The JSON answer for one `fused.run(..., _return_response=True)` result.
    `printed` is what the SDK wrote to stdout around the run."""
    stdout, stderr = _text(getattr(result, "stdout", None)), _text(getattr(result, "stderr", None))
    if printed:
        stdout = printed + ("" if printed.endswith("\n") or not stdout else "\n") + stdout
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
    if not content and getattr(result, "data", None) is not None:
        # No response bytes, but a value: a batch job's result.
        content, content_type = data_page(result.data)
    if ask_bytes is not None and ask_bytes < len(content) <= max_bytes:
        return {"ok": False, "too_large": True, "size": len(content), "limit": ask_bytes,
                "stdout": stdout, "stderr": stderr}
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
    ask_bytes = req.get("ask_bytes")
    if ask_bytes is not None and (not isinstance(ask_bytes, int) or isinstance(ask_bytes, bool)
                                  or ask_bytes <= 0):
        _fail("node run helper: 'ask_bytes' must be a positive integer or null")
        return
    # stdout is this helper's answer: nothing the SDK prints may reach it.
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            udf = fused.load(path)
    except Exception as exc:  # noqa: BLE001 — a file that is no UDF is an answer
        json.dump({"ok": False, "error": f"could not load this file as a UDF: {exc}",
                   "line": None, "stdout": printed.getvalue(), "stderr": ""}, sys.stdout)
        return
    try:
        with contextlib.redirect_stdout(printed):
            result = fused.run(udf, engine="remote", _format=RESULT_FORMAT,
                               _return_response=True)
    except Exception as exc:  # noqa: BLE001 — the SDK raises some run errors
        json.dump({"ok": False, "error": str(exc) or type(exc).__name__,
                   "line": None, "stdout": printed.getvalue(), "stderr": ""}, sys.stdout)
        return
    json.dump(result_payload(result, max_bytes, ask_bytes, printed.getvalue()), sys.stdout)


if __name__ == "__main__":
    main()

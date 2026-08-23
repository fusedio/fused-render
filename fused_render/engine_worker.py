"""Warm-worker entry point for /api/engine (docs/ENGINE_HOST_APPS_DESIGN.md).

`executor._child.py` promoted from run-once to a serve loop: imports the target
module once, then answers many `POST /call` requests in the same interpreter so
module-level imports and globals persist. Fits the engine_host child contract
(--status/--cache/--version plus --module), returns _child.py's exact envelope
plus resolved_py, and re-imports on the module's mtime change.

Calls run in parallel; only the import / mtime-check / `main` lookup is
serialized, so I/O-bound handlers overlap. print() is captured per call via a
contextvar-routed stdout.
"""
import argparse
import contextvars
import importlib.util
import io
import json
import os
import secrets
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# The StringIO the current call captures print() into, or None for the real
# stdout. A contextvar so concurrent calls, each in its own thread, capture only
# their own output; a thread that never set() one routes to the real stdout.
_call_stdout = contextvars.ContextVar("engine_call_stdout", default=None)


class _RoutedStdout:
    """Installed once as `sys.stdout`; routes each write to the current call's
    buffer when one is set, else to the real stdout."""

    def __init__(self, real):
        self._real = real

    def write(self, s):
        buf = _call_stdout.get()
        return (buf if buf is not None else self._real).write(s)

    def flush(self):
        buf = _call_stdout.get()
        (buf if buf is not None else self._real).flush()

    def __getattr__(self, name):
        return getattr(self._real, name)

# Top-level import (not `fused_render._binding`), as _child.py does it: invoked as
# a standalone script, this file's own directory is sys.path[0] so `_binding.py`
# beside it resolves even when the package is not pip-installed. Runs before a
# module load below mutates sys.path, so a user module dir can't shadow it.
from _binding import bind_params

#: The per-call budget, enforced parent-side by the proxy (this worker does not
#: kill its own thread); kept here for parity with executor's timeout.
CALL_TIMEOUT_S = 60.0


class _Target:
    """The one module this worker serves, imported once and re-imported on edit.

    Only the import / mtime-check / `main` lookup is serialized by `_lock`;
    `main()` runs outside it, so concurrent I/O-bound calls overlap. Each call
    holds its own bound `main`, so a reload can't disturb one in flight.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._module = None
        self._mtime = None

    def _load_locked(self) -> None:
        """Import (or re-import) the target module. Caller holds `_lock`."""
        module_dir = os.path.dirname(self.path)
        # cwd + sys.path so relative data paths and sibling imports in user code
        # resolve next to the .py, as _child.py does. Set once on first load.
        if self._module is None:
            os.chdir(module_dir)
            if module_dir not in sys.path:
                sys.path.insert(0, module_dir)
        spec = importlib.util.spec_from_file_location("__fused_module__", self.path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module

    def _resolve_main(self):
        """Under the lock: re-import on mtime change, return a bound `main`."""
        with self._lock:
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                mtime = None
            if self._module is None or mtime != self._mtime:
                self._load_locked()
                self._mtime = mtime
            fn = getattr(self._module, "main", None)
            if not callable(fn):
                raise AttributeError(
                    f"{os.path.basename(self.path)} does not define a callable "
                    "'main' function"
                )
            return fn

    def call(self, params: dict) -> dict:
        """Run `main(**params)` in the warm process, returning _child.py's
        envelope (re-importing first on an mtime change)."""
        captured = io.StringIO()
        token = _call_stdout.set(captured)
        out = {"ok": False}
        try:
            fn = self._resolve_main()
            result = fn(**bind_params(fn, params or {}))
            try:
                json.dumps(result)
            except (TypeError, ValueError):
                raise TypeError(
                    f"main() returned {type(result).__name__}, which is not "
                    "JSON-serializable; return dict/list/str/number/bool/None "
                    "(e.g. df.to_dict('records'))"
                ) from None
            out = {"ok": True, "result": result}
        except BaseException as e:  # noqa: BLE001 — includes SystemExit, as _child.py
            message = str(e)
            # Same bootstrap diagnostic _child.py attaches when the worker can't
            # see fused_render: name the interpreter. Exact name match, so a
            # missing submodule (full dotted name) doesn't trip it.
            if isinstance(e, ImportError) and e.name == "fused_render":
                message += (
                    f" [worker could not see the fused_render package: "
                    f"executable={sys.executable}, "
                    f"PYTHONPATH={os.environ.get('PYTHONPATH') or '(unset)'}, "
                    f"sys.path[:3]={sys.path[:3]}]"
                )
            out = {
                "ok": False,
                "error": {
                    "type": type(e).__name__,
                    "message": message,
                    "traceback": traceback.format_exc(),
                },
            }
        finally:
            _call_stdout.reset(token)
        out["stdout"] = captured.getvalue()
        # The file that ran, for the runtime's auto-reload watch (LR-2).
        out["resolved_py"] = self.path
        return out


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"
    timeout = 65

    def log_message(self, _format, *_args):
        return

    def _authorized(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        supplied = query.get("t", [""])[0]
        expected = self.server.token  # type: ignore[attr-defined]
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        if path == "/ping":
            self._json(200, {"ok": True,
                             "version": self.server.version,  # type: ignore[attr-defined]
                             "pid": os.getpid()})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        if path != "/call":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 64 << 20:
                raise ValueError("request body is too large")
            params = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(params, dict):
                raise ValueError("params must be a JSON object")
        except (ValueError, json.JSONDecodeError) as e:
            self._json(400, {"error": f"invalid /call body: {e}"})
            return
        # call() always returns the envelope, so a user-code failure is a normal
        # 200 — never a 500 that would trip the parent's heal-on-failure.
        self._json(200, self.server.target.call(params))  # type: ignore[attr-defined]


def _write_status(path: str, payload: dict) -> None:
    """Publish {port, token, pid, version} atomically (temp file + os.replace),
    so the parent never reads a half-written status file while polling."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--version", required=True)
    # The one addition to the child contract: the absolute module to serve,
    # resolved parent-side from the app's `py`.
    parser.add_argument("--module", required=True)
    args = parser.parse_args()

    sys.stdout = _RoutedStdout(sys.stdout)

    token = secrets.token_urlsafe(32)
    server = _Server(("127.0.0.1", 0), _Handler)
    server.token = token  # type: ignore[attr-defined]
    server.version = args.version  # type: ignore[attr-defined]
    server.target = _Target(args.module)  # type: ignore[attr-defined]
    port = int(server.server_address[1])

    # Status file lands just before serve_forever, closing the gap before the
    # parent's first proxied /call.
    _write_status(args.status, {"version": args.version, "port": port,
                                "token": token, "pid": os.getpid()})
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

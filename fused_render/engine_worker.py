"""Warm-worker entry point for /api/engine (docs/ENGINE_HOST_APPS_DESIGN.md).

`executor._child.py` promoted from run-once to a serve loop. Where the child
imports the target module, calls `main(**params)` once, prints one JSON line and
exits, this process imports the module ONCE and then answers many `POST /call`
requests in the SAME interpreter — so a module-level `import botocore` runs once
and module globals (a warm client cache) persist across calls. That reuse is the
entire point of the warm variant; it is opt-in (`/api/run` stays always-fresh).

The process fits the engine_host child contract (server/engine_host.py): parse
`--status/--cache/--version` plus `--module <abs .py>`, bind 127.0.0.1:0,
generate a token, publish {port, token, pid, version} to the status file
ATOMICALLY before serving, answer `GET /ping?t=…` and `POST /call?t=…`, and
validate the token on every request. The result envelope on /call is byte-for-
byte the one `_child.py` produces ({ok, result, error:{type,message,traceback},
stdout}) so a page never sees which variant ran; `resolved_py` is added for the
runtime's auto-reload watch (LR-2), exactly as routers/run.py sets it.

Refresh guardrail: the module's mtime is checked on every call and the module is
re-imported when it changed on disk, so editing the `.py` takes effect the way it
does for `/api/run` (which gets fresh code for free every call).

Phase 1 is a single warm worker per script: calls are serialized on one lock, so
module state and the stdout capture below can never be corrupted by a concurrent
call. The per-script POOL that would let a handful of concurrent calls run in
parallel is Phase 2 (design §6) — TODO.
"""
import argparse
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

# Top-level (not `fused_render._binding`) import on purpose, exactly as _child.py
# does it: this file is invoked as a standalone script
# (`python .../fused_render/engine_worker.py`), so its own directory is
# sys.path[0] and `_binding.py` next to it always resolves — even when the
# package is not pip-installed. The import runs before a module load below
# mutates sys.path, so a user module dir can't shadow it.
from _binding import bind_params

#: How long a /call may run before it is abandoned. Mirrors executor.DEFAULT_
#: TIMEOUT — the per-call budget is unchanged by warmth; only the PROCESS
#: outlives the call. Enforced by the proxy/engine host on the parent side
#: (routers/engines.POST_TIMEOUT_S); this worker does not kill its own thread,
#: so the value lives here only for parity/documentation. Phase-2 TODO: a
#: hard per-call watchdog inside the worker.
CALL_TIMEOUT_S = 60.0


class _Target:
    """The one module this worker serves, imported once and re-imported on edit.

    All access is serialized by `_lock`: Phase 1 runs a single warm worker per
    script, so one call at a time keeps module state and the process-global
    stdout redirect (below) race-free. Phase 2's pool relaxes this.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._module = None
        self._mtime = None

    def _load_locked(self) -> None:
        """Import (or re-import) the target module. Caller holds `_lock`."""
        module_dir = os.path.dirname(self.path)
        # Relative data paths in user code resolve next to the .py, and a sibling
        # imported by name is importable — same as _child.py. Done once on the
        # first load; a re-import keeps the cwd/path already in place.
        if self._module is None:
            os.chdir(module_dir)
            if module_dir not in sys.path:
                sys.path.insert(0, module_dir)
        spec = importlib.util.spec_from_file_location("__fused_module__", self.path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module

    def call(self, params: dict) -> dict:
        """Run `main(**params)` in the warm process, returning _child.py's
        envelope. Re-imports first when the file changed on disk (mtime)."""
        captured = io.StringIO()
        real_stdout = sys.stdout
        with self._lock:
            out = {"ok": False}
            sys.stdout = captured
            try:
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
                # Same worker-bootstrap diagnostic _child.py attaches: a helper
                # that cannot see `fused_render` is naming the interpreter, not a
                # bug in the helper. EXACT name match, for the reason _child.py
                # documents (a missing submodule reports its full dotted path).
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
                sys.stdout = real_stdout
        out["stdout"] = captured.getvalue()
        # The absolute file that actually ran, so the runtime can watch it for
        # auto-reload (LR-2) exactly as routers/run.py sets it on /api/run.
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
        # call() catches BaseException itself and always returns the envelope, so
        # a user-code failure is a normal 200 result — never a 500 that would
        # trip the parent's heal-on-failure and needlessly recycle a warm worker.
        self._json(200, self.server.target.call(params))  # type: ignore[attr-defined]


def _write_status(path: str, payload: dict) -> None:
    """Publish {port, token, pid, version} atomically, as the map daemon does:
    write a temp file and os.replace it, so the parent never reads a half-written
    status file while polling for the port."""
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
    # The one addition to the engine_host child contract: which module this warm
    # worker serves. The parent resolves it from the app's `py` and hands over an
    # absolute path (no allowlist — the same trust as /api/run, which runs the
    # very same file).
    parser.add_argument("--module", required=True)
    args = parser.parse_args()

    token = secrets.token_urlsafe(32)
    server = _Server(("127.0.0.1", 0), _Handler)
    server.token = token  # type: ignore[attr-defined]
    server.version = args.version  # type: ignore[attr-defined]
    server.target = _Target(args.module)  # type: ignore[attr-defined]
    port = int(server.server_address[1])

    # Status file lands just before serve_forever, so the parent's _ping wait
    # closes the last gap before the first proxied /call.
    _write_status(args.status, {"version": args.version, "port": port,
                                "token": token, "pid": os.getpid()})
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

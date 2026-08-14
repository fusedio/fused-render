"""Persistent localhost HTTP daemon for the latex template.

Under the "fused" engine each `fused.runPython` pays 5–25 s of backend overhead,
brutal for this chatty template (compile → outline+bib, ~1.5 s status polls, a
synctex probe per cursor settle). So the page spawns this daemon once (one
runPython → `{port, token}`) and makes every later engine call over direct HTTP,
dispatched straight to `engine.main()` — no second copy of the domain logic (the
geotiff/netcdf/zarr pattern). Runs in the folder's project venv, so export's
`import pypandoc` resolves here. A random per-run token gates every request; the
server self-exits after 30 min idle and respawns when daemon.py/engine.py/
pyproject.toml change.

Run detached:  python daemon.py --serve
"""
import hashlib
import inspect
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import engine  # noqa: E402 — the existing main(action=…) dispatcher, reused verbatim
from procutil import clean_env, file_lock, spawn_python  # noqa: E402 — engine put templates/shared on the path

STATE = os.path.join(engine.CACHE_ROOT, "daemon.json")
LOCK = os.path.join(engine.CACHE_ROOT, "daemon.spawn.lock")
IDLE_EXIT_S = 30 * 60
_SPAWN_WAIT_S = 8.0

_last_hit = time.time()
_hit_lock = threading.Lock()


def _version() -> str:
    """Content hash of the code + manifest (NOT the interpreter path — that was
    the db_console churn bug). An edit or a dependency change respawns a fresh
    daemon onto the rebuilt venv."""
    h = hashlib.sha1()
    for f in ("daemon.py", "engine.py", "pyproject.toml"):
        try:
            with open(os.path.join(HERE, f), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:16]


def _read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(port, token, version):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid(), "version": version}, f)
    os.replace(tmp, STATE)


def _alive(port, version, token) -> bool:
    if not port:
        return False
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/ping?_token={token}", timeout=1.5) as r:
            return json.load(r).get("version") == version
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _quit(port, token):
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/quit?_token={token}", timeout=1).read()
    except (urllib.error.URLError, OSError):
        pass


# --- annotated-type coercion (query values arrive as strings) --------------
def _coerce(value, ann):
    if ann is bool:
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    if ann is int:
        return int(value)
    if ann is float:
        return float(value)
    return value


# {param name: annotation} for engine.main, resolved once — the daemon serves
# this on its hot path, so the reflection cost is paid at import, not per request.
_MAIN_ANNOTATIONS = {
    name: (None if p.annotation is inspect.Parameter.empty else p.annotation)
    for name, p in inspect.signature(engine.main).parameters.items()
}


def _dispatch(params: dict):
    """Call `engine.main` with the same string→type coercion the runPython
    binding does, so the HTTP path and the direct path behave identically."""
    kwargs = {name: (params[name] if ann is None else _coerce(params[name], ann))
              for name, ann in _MAIN_ANNOTATIONS.items() if name in params}
    return engine.main(**kwargs)


def _await_daemon(version):
    deadline = time.time() + _SPAWN_WAIT_S
    while time.time() < deadline:
        st = _read_state()
        if st and st.get("version") == version and _alive(st.get("port"), version, st.get("token")):
            return {"port": st["port"], "token": st["token"], "reused": False}
        time.sleep(0.15)
    return {"error": "latex daemon did not start in time"}


def _spawn():
    log = os.path.join(engine.CACHE_ROOT, "daemon.log")
    # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP (NOT CREATE_NO_WINDOW — the two
    # combined fail to spawn on Windows); start_new_session is the POSIX equal.
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
              if os.name == "nt" else {"start_new_session": True})
    with open(log, "ab") as lf:
        subprocess.Popen([spawn_python(), os.path.join(HERE, "daemon.py"), "--serve"],
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, cwd=HERE,
                         env=clean_env(), **detach)


def main():
    """runPython entrypoint: ensure the daemon is up, return {port, token}. The
    spawn is serialized on a kernel `file_lock` so concurrent ensures can't
    orphan a second server; a waiter blocks, then finds the daemon the winner
    already started."""
    os.makedirs(engine.CACHE_ROOT, exist_ok=True)   # LOCK/STATE/log all live under it
    version = _version()
    st = _read_state()
    if st and _alive(st.get("port"), version, st.get("token")):
        return {"port": st["port"], "token": st["token"], "reused": True}
    with file_lock(LOCK, timeout=_SPAWN_WAIT_S + 10):
        st = _read_state()   # recheck: a racing ensure may have spawned while we waited
        if st and _alive(st.get("port"), version, st.get("token")):
            return {"port": st["port"], "token": st["token"], "reused": True}
        if st and st.get("port"):
            _quit(st["port"], st.get("token", ""))   # stale version / wrong code — retire it
        _spawn()
        return _await_daemon(version)


# --- the server ------------------------------------------------------------
def _make_server():
    """Build the HTTP server + write the state file. Split from `_serve` so a
    test can drive it without `serve_forever`/`os._exit`."""
    version = _version()
    token = secrets.token_urlsafe(24)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # Poll/compile GETs reuse the same URL (warm_status, force=0 autosave
            # compiles); without no-store a memory cache (esp. WKWebView) would
            # replay the first response and warming/compiles would look stuck.
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self._cors()
            self.end_headers()

        def do_GET(self):
            global _last_hit
            u = urlsplit(self.path)
            # keep_blank_values so an empty param ("target=") arrives as "" (as the
            # runPython fallback passes it), not dropped to engine.main's default.
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            if q.get("_token") != token:
                self._json({"error": "forbidden"}, 403)
                return
            with _hit_lock:
                _last_hit = time.time()
            if u.path == "/ping":
                self._json({"version": version, "ok": True})
            elif u.path == "/quit":
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.1), os._exit(0)),
                                 daemon=True).start()
            elif u.path == "/run":
                q.pop("_token", None)
                try:
                    self._json(_dispatch(q))
                except Exception as e:  # noqa: BLE001
                    # A RAISED exception under a sentinel, so the page rejects like
                    # runPython instead of treating it as a normal {error} return.
                    import traceback
                    self._json({"__exc__": {"type": type(e).__name__,
                                            "message": f"{type(e).__name__}: {e}",
                                            "traceback": traceback.format_exc()}})
            else:
                self._json({"error": "not found"}, 404)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    _write_state(srv.server_address[1], token, version)
    return srv, srv.server_address[1], token


def _serve():
    srv, _port, _token = _make_server()

    def _reaper():
        while True:
            time.sleep(60)
            with _hit_lock:
                idle = time.time() - _last_hit
            if idle > IDLE_EXIT_S:
                os._exit(0)

    threading.Thread(target=_reaper, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        _serve()
    else:
        print(json.dumps(main()))

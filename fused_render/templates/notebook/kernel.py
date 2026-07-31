"""Kernel-manager daemon for the notebook template.

runPython spawns a fresh subprocess per call (no state, 60 s cap), so cell
execution lives in one long-lived localhost daemon instead. This module is
both:

  1. a runPython entrypoint `main(action="ensure")` — starts (or reuses) the
     daemon and returns {port, token}; and
  2. the daemon itself (`python kernel.py --serve`) — holds one kernel_body.py
     subprocess per (notebook, interpreter) pair, tags its stdout events with
     cell_id/execution_count/seq, and buffers them for the polling UI.

Endpoints (all token-gated via ?t= except /ping, CORS *, loopback only):
  GET  /ping                                   -> {"ok", "version"}
  GET  /quit?t=
  POST /kernel/ensure {nb_path, python}        -> {kernel_id, state, info}
  POST /kernel/execute {kernel_id, cell_id, code} -> {exec_id, execution_count}
  GET  /kernel/events?kernel_id&since=N        -> {events, next, state}
  POST /kernel/interrupt|restart|shutdown {kernel_id}
  GET  /envs?nb_path=                          -> {envs: [{label, path}]}

Kernels idle 30 min are reaped; the daemon exits after 30 min with no kernels
and no requests. The state file embeds a source hash of this file +
kernel_body.py, so editing either respawns a fresh daemon on the next ensure.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

def _cache_dir():
    """Daemon state nests under the app home (FUSED_RENDER_HOME_DIR is the
    server's branch-resolved home, exported to every child) so servers with
    isolated homes — tests, branches — get independent daemons. The ~/.cache
    fallback only applies standalone, with no server env around."""
    home = os.environ.get("FUSED_RENDER_HOME_DIR") or os.environ.get("FUSED_RENDER_HOME")
    if home:
        return os.path.join(home, "cache", "notebook-daemon")
    return os.path.expanduser("~/.cache/fused-render-notebook")


CACHE_DIR = _cache_dir()
STATE = os.path.join(CACHE_DIR, "daemon.json")
START_LOCK = os.path.join(CACHE_DIR, "start.lock")
LOCK_STALE_S = 30
IDLE_EXIT_S = 30 * 60
KERNEL_IDLE_S = 30 * 60
EVENT_BUFFER_MAX = 10000


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    if os.name != "nt":
        os.chmod(CACHE_DIR, 0o700)


def _me():
    if "__file__" in globals():
        return os.path.abspath(__file__)
    return os.path.join(os.path.abspath(sys.path[0]), "kernel.py")


def _body():
    return os.path.join(os.path.dirname(_me()), "kernel_body.py")


def _canon_python(exe):
    # the daemon runs under pythonw.exe (see spawn below); kernels and the
    # version string always use the console python.exe next to it
    if os.name == "nt" and os.path.basename(exe).lower() == "pythonw.exe":
        py = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(py):
            return py
    return exe


def _version():
    h = hashlib.sha256()
    for p in (_me(), _body()):
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12] + "|" + _canon_python(sys.executable)


# ================================================================ ensure()
def _alive(port, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version
    except (OSError, ValueError):
        return False


def _read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _claim_lock():
    _ensure_cache_dir()
    try:
        fd = os.open(START_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(START_LOCK) > LOCK_STALE_S:
                os.unlink(START_LOCK)
                return _claim_lock()
        except OSError:
            return False
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def _wait_alive(version, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.05)
        st = _read_state()
        if st and st.get("version") == version and _alive(st.get("port"), version):
            return st
    return None


def _server_url(src, endpoint, path):
    import urllib.parse
    u = urllib.parse.urlsplit(src)
    return f"{u.scheme}://{u.netloc}{endpoint}?path=" + urllib.parse.quote(path)


def _remote_meta(src, path):
    import urllib.request
    if not src:
        return None
    with urllib.request.urlopen(_server_url(src, "/api/fs/stat", path),
                                timeout=10) as r:
        return json.load(r)


def _list_remote(src, path, cap=5000):
    import urllib.parse
    import urllib.request
    entries, cursor = [], ""
    while True:
        url = _server_url(src, "/api/fs/list", path)
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor)
        with urllib.request.urlopen(url, timeout=30) as r:
            payload = json.load(r)
        entries.extend(payload.get("entries") or [])
        cursor = payload.get("cursor") or ""
        if len(entries) >= cap or not payload.get("truncated") or not cursor:
            break
    return entries


def _listdir(path, src):
    """Folder-picker listing for the new-notebook / save-a-copy modal. A
    mount-backed dir lists via the server's /api/fs/list, never a kernel scan
    that could wedge the mount (mirrors docs.py's listdir)."""
    base = os.path.abspath(os.path.expanduser(path)) if path else os.path.expanduser("~")
    dirs, files = [], []
    meta = _remote_meta(src, base)
    if meta and meta.get("remote"):
        if not meta.get("is_dir"):
            base = os.path.dirname(base) or os.path.expanduser("~")
        entries = _list_remote(src, base)
        for ent in entries:
            nm = ent["name"]
            if nm.startswith("."):
                continue
            if ent.get("is_dir"):
                dirs.append(nm)
            elif nm.lower().endswith(".ipynb"):
                files.append(nm)
    else:
        if not os.path.isdir(base):
            base = os.path.dirname(base) or os.path.expanduser("~")
        try:
            names = os.listdir(base)
        except OSError as e:
            return {"error": str(e)}
        for nm in names:
            if nm.startswith("."):
                continue
            if os.path.isdir(os.path.join(base, nm)):
                dirs.append(nm)
            elif nm.lower().endswith(".ipynb"):
                files.append(nm)
    dirs.sort(key=str.lower)
    files.sort(key=str.lower)
    parent = os.path.dirname(base) or base
    return {"path": base.replace(os.sep, "/"),
            "parent": parent.replace(os.sep, "/"),
            "dirs": dirs, "files": files}


def _resolve_dest(directory, name):
    """Destination for the new-notebook / save-a-copy modal. `name` may be a
    plain name, a relative subpath, or an absolute path that overrides the
    browsed `directory`; os.path keeps the semantics platform-correct (both
    separators on Windows; a backslash stays an ordinary filename character
    on POSIX). Appends .ipynb unless already present."""
    name = (name or "").strip()
    if not name:
        return {"error": "Enter a name."}
    name = os.path.expanduser(name)
    if os.path.isabs(name):
        full = os.path.abspath(name)
    else:
        full = os.path.abspath(
            os.path.join(os.path.expanduser(directory or "~"), name))
    if not full.lower().endswith(".ipynb"):
        full += ".ipynb"
    parent = os.path.dirname(full)
    if not os.path.isdir(parent):
        return {"error": "Folder does not exist: " + parent.replace(os.sep, "/")}
    return {"path": full.replace(os.sep, "/"),
            "dir": parent.replace(os.sep, "/"),
            "name": os.path.basename(full)}


def main(action: str = "ensure", path: str = "", src: str = "", name: str = ""):
    """runPython entrypoint: ensure the daemon (default), or folder listings /
    destination resolution for the save/new modal."""
    if action == "listdir":
        return _listdir(path, src)
    if action == "resolve":
        return _resolve_dest(path, name)
    version = _version()
    st = _read_state()
    if st and _alive(st.get("port"), version):
        return {"port": st["port"], "token": st.get("token"),
                "reused": True, "version": version}
    if st:
        # stale daemon (old version or dead) — ask it to quit, then respawn
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit?t={st.get('token', '')}",
                timeout=1).read()
        except OSError:
            pass

    if not _claim_lock():
        # another opener is spawning — wait for its daemon instead
        st = _wait_alive(version, 10)
        if st:
            return {"port": st["port"], "token": st.get("token"),
                    "reused": True, "version": version}
        if not _claim_lock():
            return {"error": "notebook daemon startup already in progress"}
    try:
        log = os.path.join(CACHE_DIR, "daemon.log")
        # scrub the app's interpreter env so a user-picked venv python never
        # imports the app bundle's packages
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONPATH", "PYTHONHOME")}
        # a console-subsystem python.exe spawned DETACHED_PROCESS gets a fresh
        # console window on Windows (the popup) — run pythonw.exe instead, the
        # same fix db_console/webhook_debugger shipped
        exe = sys.executable
        if os.name == "nt":
            pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(pyw):
                exe = pyw
        detach = (
            {"creationflags": subprocess.DETACHED_PROCESS
             | subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        with open(log, "ab") as lf:
            subprocess.Popen([exe, _me(), "--serve"],
                             stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                             env=env, cwd=os.path.dirname(_me()), **detach)
        st = _wait_alive(version, 15)
        if st:
            return {"port": st["port"], "token": st.get("token"),
                    "reused": False, "version": version}
        return {"error": f"notebook daemon did not start — see {log}"}
    finally:
        try:
            os.unlink(START_LOCK)
        except FileNotFoundError:
            pass


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass


# ================================================================ daemon
def _serve():
    import hmac
    import secrets
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    TOKEN = secrets.token_urlsafe(32)
    VERSION = _version()
    last_hit = [time.time()]
    kernels = {}
    klock = threading.Lock()

    class Kernel:
        def __init__(self, nb_path, python):
            self.nb_path = nb_path
            self.python = python
            self.proc = None
            self.state = "starting"
            self.info = {}
            self.ready = threading.Event()
            self.events = []
            self.seq = 0
            self.exec_count = 0
            self.cells = {}   # exec_id -> cell_id
            self.counts = {}  # exec_id -> execution_count
            self.pending = []
            self.last_used = time.time()
            self.lock = threading.Lock()

        def _spawn(self):
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTHONPATH", "PYTHONHOME")}
            env["MPLBACKEND"] = "Agg"
            kw = ({"creationflags": subprocess.CREATE_NO_WINDOW}
                  if os.name == "nt" else {})
            cwd = os.path.dirname(self.nb_path)
            proc = subprocess.Popen(
                [self.python, _body()],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=cwd if os.path.isdir(cwd) else None, env=env,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
            self.proc = proc
            threading.Thread(target=self._read, args=(proc,), daemon=True).start()

        def _read(self, proc):
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                self._handle(ev, proc)
            proc.wait()
            with self.lock:
                if self.proc is proc and self.state != "dead":
                    self.state = "dead"
                    self.pending = []
                    self._push({"type": "dead", "returncode": proc.returncode})

        def _handle(self, ev, proc):
            with self.lock:
                if proc is not self.proc:
                    # buffered output from a process restart already killed —
                    # after restart resets exec_count, its reused ids would
                    # attach these events to the wrong cells
                    return
                if ev.get("type") == "ready":
                    self.info = {"python": ev.get("python"),
                                 "version": ev.get("version")}
                    self.state = "busy" if self.pending else "idle"
                    self._push(ev)
                    self.ready.set()
                    return
                eid = ev.get("id")
                ev["cell_id"] = self.cells.get(eid)
                ev["execution_count"] = self.counts.get(eid)
                if ev.get("type") == "done":
                    if eid in self.pending:
                        self.pending.remove(eid)
                    if not self.pending and self.state == "busy":
                        self.state = "idle"
                self._push(ev)

        def _push(self, ev):
            # caller holds self.lock
            self.seq += 1
            ev["seq"] = self.seq
            self.events.append(ev)
            if len(self.events) > EVENT_BUFFER_MAX:
                del self.events[:len(self.events) - EVENT_BUFFER_MAX]

        def _send_locked(self, req):
            proc = self.proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise RuntimeError("kernel is dead — restart it")
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()

        def execute(self, cell_id, code):
            with self.lock:
                if self.state == "dead":
                    raise RuntimeError("kernel is dead — restart it")
                proc = self.proc
                self.exec_count += 1
                eid = f"e{self.exec_count}"
                count = self.exec_count
                self.cells[eid] = cell_id
                self.counts[eid] = count
                self.pending.append(eid)
                if self.ready.is_set():
                    self.state = "busy"
                self.last_used = time.time()
                try:
                    self._send_locked({"op": "execute", "id": eid, "code": code})
                except (BrokenPipeError, OSError, RuntimeError, ValueError) as exc:
                    self.pending.remove(eid)
                    self.cells.pop(eid, None)
                    self.counts.pop(eid, None)
                    self.state = "dead"
                    self._push({"type": "dead", "returncode": None})
                    raise RuntimeError("kernel is dead — restart it") from exc
            return eid, count

        def interrupt(self):
            with self.lock:
                self.last_used = time.time()
                self._send_locked({"op": "interrupt"})

        def restart(self):
            with self.lock:
                old, self.proc = self.proc, None
                self.ready.clear()
                _kill(old)
                self.pending = []
                self.cells = {}
                self.counts = {}
                self.exec_count = 0
                self.state = "starting"
                self.last_used = time.time()
                self._push({"type": "restarted"})
                try:
                    self._spawn()
                except OSError:
                    self.state = "dead"
                    self._push({"type": "dead", "returncode": None})
                    raise

        def shutdown(self):
            with self.lock:
                old, self.proc = self.proc, None
                self.state = "dead"
                self.pending = []
                self.cells = {}
                self.counts = {}
                _kill(old)

    def _kill(proc):
        if proc is None or proc.poll() is not None:
            return
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def kernel_id(nb_path, python):
        key = os.path.normcase(nb_path) + "|" + os.path.normcase(python)
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def get_kernel(body):
        k = kernels.get(body.get("kernel_id") or "")
        if k is None:
            raise KeyError("unknown kernel_id")
        return k

    def do_ensure(body):
        nb_path = os.path.abspath(os.path.expanduser(body.get("nb_path") or ""))
        python = body.get("python") or _canon_python(sys.executable)
        kid = kernel_id(nb_path, python)
        with klock:
            k = kernels.get(kid)
            fresh = k is None or k.state == "dead"
            if fresh:
                k = Kernel(nb_path, python)
                kernels[kid] = k
        if fresh:
            # spawn outside klock: Popen and the cwd isdir probe can hang on a
            # wedged mount, and that must not stall every other kernel request
            try:
                k._spawn()
            except OSError as e:
                with klock:
                    if kernels.get(kid) is k:
                        del kernels[kid]
                raise RuntimeError(f"could not start {python}: {e}")
        if not k.ready.wait(timeout=20) or k.state == "dead":
            with klock:
                if kernels.get(kid) is k:
                    del kernels[kid]
            k.shutdown()
            raise RuntimeError(f"kernel did not start under {python}")
        # seq lets a reconnecting client poll from "now" instead of replaying
        # a warm kernel's whole event buffer over its cells
        with k.lock:
            seq = k.seq
        return {"kernel_id": kid, "state": k.state, "python": python,
                "info": k.info, "seq": seq}

    def do_execute(body):
        k = get_kernel(body)
        eid, count = k.execute(body.get("cell_id") or "", body.get("code") or "")
        return {"exec_id": eid, "execution_count": count}

    def do_events(q):
        k = kernels.get(q1(q, "kernel_id", ""))
        if k is None:
            raise KeyError("unknown kernel_id")
        since = int(q1(q, "since", "0"))
        with k.lock:
            events = [e for e in k.events if e["seq"] > since]
            return {"events": events, "next": k.seq, "state": k.state}

    def do_envs(q):
        # the app env travels as "" so the saved metadata stays portable;
        # /kernel/ensure resolves "" to sys.executable
        envs = [{"label": "App environment", "path": ""}]
        seen = {os.path.normcase(_canon_python(sys.executable))}
        nb = q1(q, "nb_path", "")
        d = os.path.dirname(os.path.abspath(os.path.expanduser(nb))) if nb else ""
        for _ in range(4):
            if not d:
                break
            cand = (os.path.join(d, ".venv", "Scripts", "python.exe")
                    if os.name == "nt" else os.path.join(d, ".venv", "bin", "python"))
            if os.path.isfile(cand) and os.path.normcase(cand) not in seen:
                envs.append({"label": f".venv — {os.path.basename(d)}", "path": cand})
                seen.add(os.path.normcase(cand))
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        return {"envs": envs}

    def q1(q, k, dflt=None):
        v = q.get(k)
        return v[0] if v else dflt

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _gate(self, q):
            if not (hmac.compare_digest(q.get("t", [""])[0], TOKEN) or
                    hmac.compare_digest(self.headers.get("X-Token") or "", TOKEN)):
                self._send(403, {"error": "forbidden"})
                return False
            return True

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
            self.end_headers()

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/ping":
                self._send(200, {"ok": True, "version": VERSION})
                return
            if not self._gate(q):
                return
            try:
                if u.path == "/quit":
                    self._send(200, {"ok": True})
                    threading.Thread(target=srv.shutdown, daemon=True).start()
                elif u.path == "/kernel/events":
                    self._send(200, do_events(q))
                elif u.path == "/envs":
                    self._send(200, do_envs(q))
                else:
                    self._send(404, {"error": "not found"})
            except KeyError as e:
                self._send(404, {"error": str(e).strip("'")})
            except (OSError, RuntimeError, TypeError, ValueError) as e:
                self._send(500, {"error": str(e)})

        def do_POST(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._gate(q):
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("request body must be a JSON object")
                if u.path == "/kernel/ensure":
                    self._send(200, do_ensure(body))
                elif u.path == "/kernel/execute":
                    self._send(200, do_execute(body))
                elif u.path == "/kernel/interrupt":
                    get_kernel(body).interrupt()
                    self._send(200, {"ok": True})
                elif u.path == "/kernel/restart":
                    get_kernel(body).restart()
                    self._send(200, {"ok": True})
                elif u.path == "/kernel/shutdown":
                    # idempotent: shutting down a reaped kernel is a no-op
                    with klock:
                        k = kernels.pop(body.get("kernel_id") or "", None)
                    if k is not None:
                        k.shutdown()  # proc.wait can take seconds — not under klock
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "not found"})
            except KeyError as e:
                self._send(404, {"error": str(e).strip("'")})
            except (OSError, RuntimeError, TypeError, ValueError) as e:
                self._send(500, {"error": str(e)})

        def _send(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    _ensure_cache_dir()
    tmp_state = f"{STATE}.{os.getpid()}.tmp"
    fd = os.open(tmp_state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"port": port, "token": TOKEN,
                   "pid": os.getpid(), "version": VERSION}, fh)
    os.replace(tmp_state, STATE)

    def reaper():
        while True:
            time.sleep(60)
            now = time.time()
            with klock:
                idle = [(kid, k) for kid, k in kernels.items()
                        if now - k.last_used > KERNEL_IDLE_S]
                for kid, _ in idle:
                    kernels.pop(kid, None)
                empty = not kernels
            for _, k in idle:
                k.shutdown()  # proc.wait can take seconds — not under klock
            if empty and now - last_hit[0] > IDLE_EXIT_S:
                srv.shutdown()
                return

    threading.Thread(target=reaper, daemon=True).start()
    print(f"notebook daemon on 127.0.0.1:{port} (v{VERSION})", flush=True)
    srv.serve_forever()
    with klock:
        for k in kernels.values():
            k.shutdown()


if __name__ == "__main__" and "--serve" in sys.argv:
    _serve()

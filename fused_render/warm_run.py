"""Pre-spawned idle interpreters for `/api/run` (SPEC PY-20, D1411).

A run still gets a FRESH process (SPEC PY-6, D5): this module only starts that
process a little earlier than the request, so the interpreter start-up (about 11
ms, and TWO of them on the fused engine) is already paid when the call arrives.

Rules this file keeps, and a change must not break:

- **Stdlib only, pre-imported.** A spare never imports anything third-party and
  never sees user source, a script path, params or a working directory. Those
  arrive after it is claimed. Heavy imports (pandas, ...) are deliberately NOT
  warmed: they read env/config at import and would not be semantics-preserving.
- **A spare is claimed at most once and never reused.** A process that has run
  something is never returned to the pool.
- **One spawn site (`_spawn`)**, with the D277/D292 flags: `close_fds=False`, an
  absolute executable, no `cwd=`, no `start_new_session`, no `preexec_fn`, so
  CPython takes posix_spawn and PROJ's pthread_atfork handler never runs in the
  forked child. The fused spare detaches itself with `os.setsid()` as its first
  statement instead.
- **Never queue.** If no spare is ready the caller runs the cold path at once.
- **POSIX only.** Windows has no setsid/killpg here, so `enabled()` is False
  there and the cold path is the only path.
- **Every failure degrades to cold.** No exception from this module may fail a
  run that the cold path would have served.

Two kinds of spare:

``builtin`` — `_child.py` (via a one-line `runpy` boot that first closes inherited
descriptors), parked in `json.load(stdin)`. Claiming it is `communicate(request)`;
it is exactly the cold child, born early.

``fused`` — process A' (`A_BOOT`) which pre-imports the stdlib, starts the
user-code runner B' (`B_BOOT`) with the same default `close_fds=True` the upstream
handler uses, reports `ready`, and waits for one line naming the per-call
`run.py`. A' then runs that upstream `run.py` unmodified; the only difference is
that the handler's `subprocess.run([sys.executable, _runner.py, ...])` is routed,
by a narrow shim, to the already-booted B'. Any call the shim does not recognise
goes to the real `subprocess.run`.
"""
from __future__ import annotations

import atexit
import hashlib
import inspect
import json
import logging
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

DEPTH = 2            # idle spares per key
MAX_KEYS = 4         # keys (interpreter x kind) kept warm; LRU beyond that
TTL_S = 120.0        # an unclaimed spare is killed this long after its birth
REAP_INTERVAL_S = 30.0
READY_TIMEOUT_S = 5.0   # a spare not ready after this is broken
BOOT_WAIT_S = 0.25      # claim waits at most this for a spare that is still booting
FAIL_LIMIT = 3          # consecutive failures before a key is parked ...
FAIL_PARK_S = 60.0      # ... for this long

# The one `_execute_sync` this module mirrors. Checked once per backend class so
# an upstream signature change degrades quietly to the cold path (and loudly in
# tests/test_warm_run.py).
_EXPECTED_EXECUTE_SYNC_PARAMS = (
    "self", "code", "input_files", "test_file", "monitoring", "monitor_interval",
    "invoke_depth", "interpreter", "requirements", "project_root", "entrypoint",
)
_EXPECTED_CHILD_ENV_PARAMS = ("self", "invoke_dir", "invoke_depth", "interpreter", "project_root")
_REQUIRED_BACKEND_ATTRS = (
    "_ensure_dispatcher", "_write_handler_files", "_make_event", "_child_env",
    "_result_from_run", "_timeout_seconds", "_execute_sync",
)


def enabled() -> bool:
    """The warm-run switch (`shell.prefs.warm_run_enabled`), imported lazily:
    executor must not import shell prefs at module import (cycle risk)."""
    if os.name != "posix":
        return False
    try:
        from .shell import prefs
        return bool(prefs.warm_run_enabled())
    except Exception:  # noqa: BLE001 - a broken pref read must not fail a run
        return False


# ---- child-side boot scripts (stdlib only) ----------------------------------

# First thing every spare runs. The server spawns spares with close_fds=False
# (posix_spawn, D277), and on macOS `os.pipe()` is pipe()+fcntl(CLOEXEC), not
# atomic: a pipe another thread is creating at that instant is inherited by the
# spare and then held for its whole parked life, so that other child never sees
# EOF on its stdout and its caller hangs until the timeout (reproduced; a cold
# child dies with its call, a parked spare does not). Closing every inherited
# descriptor above stderr before doing anything else shrinks the hold from
# "until the spare is claimed" to the interpreter's start-up.
_CLOSE_FDS = r'''
import os
try:
    _fds = [int(_n) for _n in os.listdir("/dev/fd")]
except OSError:
    _fds = list(range(3, 1024))
for _fd in _fds:
    if _fd > 2:
        try:
            os.close(_fd)
        except OSError:
            pass
'''

BUILTIN_BOOT = _CLOSE_FDS + r'''
import sys, runpy
_path = sys.argv[1]
sys.argv = [_path]
sys.path[0] = os.path.dirname(_path)
runpy.run_path(_path, run_name="__main__")
'''

B_BOOT = r'''
import sys
if sys.path and sys.path[0] == "":
    del sys.path[0]
import os, json, io, base64, contextlib, traceback, inspect, runpy
_line = sys.stdin.readline()
if not _line:
    sys.exit(0)
_req = json.loads(_line)
os.chdir(_req["cwd"])
os.environ.clear()
os.environ.update(_req["env"])
_fd = os.open(os.devnull, os.O_RDONLY)
os.dup2(_fd, 0)
os.close(_fd)
_argv = _req["argv"]
sys.argv = list(_argv)
sys.path.insert(0, os.path.dirname(_argv[0]))
runpy.run_path(_argv[0], run_name="__main__")
'''

_A_BODY = _CLOSE_FDS + r'''
import sys
if sys.path and sys.path[0] == "":
    del sys.path[0]
import os, json, io, shutil, subprocess, tempfile, uuid, logging, traceback, time
import datetime, base64, contextlib, inspect, runpy
try:
    os.setsid()
except OSError:
    pass
# Default close_fds=True on purpose: B' (and anything user code spawns) must not
# inherit the parent's pipes.
_b = subprocess.Popen([sys.executable, "-c", B_BOOT], stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
_real_run = subprocess.run
_ALLOWED = {"capture_output", "text", "timeout", "cwd", "env"}


def _run(args, **kw):
    global _b
    b = _b
    if (b is not None and isinstance(args, (list, tuple)) and len(args) >= 2
            and args[0] == sys.executable and os.path.basename(str(args[1])) == "_runner.py"
            and set(kw) <= _ALLOWED and kw.get("capture_output") and kw.get("text")
            and kw.get("cwd") and kw.get("env") is not None and b.poll() is None):
        _b = None
        a = list(args)
        msg = json.dumps({"argv": a[1:], "cwd": kw["cwd"], "env": dict(kw["env"])}) + "\n"
        timeout = kw.get("timeout")
        try:
            out, err = b.communicate(msg, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            b.kill()
            b.communicate()
            raise subprocess.TimeoutExpired(a, timeout, output=exc.stdout, stderr=exc.stderr) from None
        except BaseException:
            b.kill()
            raise
        return subprocess.CompletedProcess(a, b.poll(), out, err)
    return _real_run(args, **kw)


subprocess.run = _run
os.write(1, b"ready\n")
_line = sys.stdin.readline()
try:
    if _line:
        _run_py = json.loads(_line)["run_py"]
        sys.argv = [_run_py]
        runpy.run_path(_run_py, run_name="__main__")
finally:
    if _b is not None:
        try:
            _b.kill()
            _b.wait()
        except Exception:
            pass
'''

A_BOOT = "B_BOOT = " + repr(B_BOOT) + "\n" + _A_BODY


# ---- spares and the pool -----------------------------------------------------

class _Spare:
    """One idle child. `ready()` is the handshake for the fused kind; a builtin
    spare is ready the moment it exists (it simply blocks reading stdin)."""

    def __init__(self, kind: str, proc: subprocess.Popen):
        self.kind = kind
        self.proc = proc
        self.born = time.monotonic()
        self._ready = kind == "builtin"
        self._buf = b""
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def age(self) -> float:
        return time.monotonic() - self.born

    def is_ready(self, wait: float = 0.0) -> bool:
        """True once the `ready\\n` line is read. Reads with `os.read` on the
        raw binary pipe (never a buffered readline) so the later `communicate()`
        sees every remaining byte."""
        if self._ready:
            return True
        deadline = time.monotonic() + max(0.0, wait)
        with self._lock:
            fd = self.proc.stdout.fileno()
            while True:
                left = max(0.0, deadline - time.monotonic())
                try:
                    r, _, _ = select.select([fd], [], [], left)
                except (OSError, ValueError):
                    return False
                if not r:
                    return False
                try:
                    chunk = os.read(fd, 6 - len(self._buf))
                except OSError:
                    return False
                if not chunk:  # EOF: the spare died before it was ready
                    return False
                self._buf += chunk
                if len(self._buf) >= 6:
                    self._ready = self._buf == b"ready\n"
                    return self._ready


def _kill(spare: _Spare) -> None:
    """Kill a spare and everything under it. Only ever on timeout, discard or
    shutdown, never on a successful call (a script's daemon children survive, as
    they do on the cold path)."""
    proc = spare.proc
    if spare.kind == "fused":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    _reap(proc)


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        pass
    for f in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if f is not None:
                f.close()
        except Exception:  # noqa: BLE001
            pass


class _Entry:
    __slots__ = ("fingerprint", "kind", "argv", "env", "spares", "spawning",
                 "failures", "disabled_until", "warned")

    def __init__(self, kind: str, fingerprint):
        self.kind = kind
        self.fingerprint = fingerprint
        self.argv: list[str] = []
        self.env: dict | None = None
        self.spares: list[_Spare] = []
        self.spawning = 0
        self.failures = 0
        self.disabled_until = 0.0
        self.warned = False


def _spawn(argv: list[str], env: dict | None) -> subprocess.Popen:
    """THE spawn site. posix_spawn needs: close_fds falsey, cwd None, no
    start_new_session/preexec_fn, and a dirname on argv[0] (an absolute path).
    Binary, unbuffered pipes: the handshake is read with os.read."""
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, close_fds=False, bufsize=0)


class Pool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: "OrderedDict[tuple, _Entry]" = OrderedDict()
        self._refuse = False
        self._reaper: threading.Thread | None = None

    # -- claim ---------------------------------------------------------------

    def claim(self, kind: str, interpreter: str, fingerprint, argv: list[str],
              env: dict | None, seed_only: bool = False) -> _Spare | None:
        """A ready spare for this (kind, interpreter) whose fingerprint still
        matches, or None (the caller runs cold, never waits). Always tops the
        key back up in the background (which also seeds it on the first call)."""
        ident = (kind, interpreter)
        dead: list[_Spare] = []
        with self._lock:
            if self._refuse:
                return None
            entry = self._entries.get(ident)
            if entry is None:
                entry = _Entry(kind, fingerprint)
                self._entries[ident] = entry
                while len(self._entries) > MAX_KEYS:
                    _, old = self._entries.popitem(last=False)
                    dead.extend(old.spares)
                    old.spares = []
            self._entries.move_to_end(ident)
            if entry.fingerprint != fingerprint:
                dead.extend(entry.spares)
                entry.spares = []
                entry.fingerprint = fingerprint
            entry.argv, entry.env = list(argv), env
            parked = time.monotonic() < entry.disabled_until
            cands = [] if parked or seed_only else list(entry.spares)
        self._ensure_reaper()
        for s in dead:
            _kill(s)
        chosen = self._pick(entry, cands)
        if not parked:
            self._replenish(ident, entry)
        return chosen

    def _pick(self, entry: _Entry, cands: list[_Spare]) -> _Spare | None:
        bad: list[_Spare] = []
        chosen: _Spare | None = None
        for s in cands:
            if not s.alive():
                bad.append(s)
            elif s.is_ready(0):
                chosen = s
                break
        if chosen is None:
            for s in cands:
                if s in bad or not s.alive():
                    continue
                age = s.age()
                if age > READY_TIMEOUT_S:
                    bad.append(s)
                elif age <= BOOT_WAIT_S and s.is_ready(BOOT_WAIT_S - age):
                    chosen = s
                    break
        with self._lock:
            for s in bad:
                if s in entry.spares:
                    entry.spares.remove(s)
            if chosen is not None:
                if chosen in entry.spares:
                    entry.spares.remove(chosen)
                    entry.failures = 0
                else:
                    chosen = None  # another thread took it first
            if bad:
                self._fail(entry, len(bad))
        for s in bad:
            _kill(s)
        return chosen

    def _fail(self, entry: _Entry, n: int = 1) -> None:  # caller holds the lock
        entry.failures += n
        if entry.failures >= FAIL_LIMIT:
            entry.disabled_until = time.monotonic() + FAIL_PARK_S
            entry.failures = 0
            if not entry.warned:
                entry.warned = True
                logger.warning("warm run: %s spares keep failing; parked for %ds",
                               entry.kind, int(FAIL_PARK_S))

    # -- replenish -----------------------------------------------------------

    def _replenish(self, ident: tuple, entry: _Entry) -> None:
        with self._lock:
            if self._refuse or self._entries.get(ident) is not entry:
                return
            need = DEPTH - len(entry.spares) - entry.spawning
            if need <= 0 or time.monotonic() < entry.disabled_until:
                return
            entry.spawning += need
            argv, env, fp = list(entry.argv), entry.env, entry.fingerprint
        threading.Thread(target=self._spawn_many, args=(ident, entry, fp, argv, env, need),
                         daemon=True, name="warm-run-spawn").start()

    def _spawn_many(self, ident, entry, fp, argv, env, need) -> None:
        for _ in range(need):
            spare = None
            try:
                spare = _Spare(entry.kind, _spawn(argv, env))
            except Exception as e:  # noqa: BLE001
                logger.debug("warm run: spare spawn failed: %s", e)
            with self._lock:
                entry.spawning -= 1
                if spare is None:
                    self._fail(entry)
                    continue
                keep = (not self._refuse and self._entries.get(ident) is entry
                        and entry.fingerprint == fp)
                if keep:
                    entry.spares.append(spare)
            if not keep:
                _kill(spare)

    # -- lifetime ------------------------------------------------------------

    def _ensure_reaper(self) -> None:
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap_loop, daemon=True,
                                            name="warm-run-reaper")
            self._reaper.start()

    def _reap_loop(self) -> None:
        while True:
            time.sleep(REAP_INTERVAL_S)
            self.reap()

    def reap(self) -> None:
        """Kill TTL-expired and dead spares. A key emptied this way is NOT
        refilled until it is next used."""
        victims: list[_Spare] = []
        with self._lock:
            for entry in self._entries.values():
                keep = []
                for s in entry.spares:
                    (keep if s.alive() and s.age() <= TTL_S else victims).append(s)
                entry.spares = keep
        for s in victims:
            _kill(s)

    def discard_all(self, reason: str = "") -> None:
        victims: list[_Spare] = []
        with self._lock:
            for entry in self._entries.values():
                victims.extend(entry.spares)
                entry.spares = []
        for s in victims:
            _kill(s)
        if victims:
            logger.debug("warm run: discarded %d spares (%s)", len(victims), reason)

    def refuse_new(self) -> None:
        with self._lock:
            self._refuse = True

    def shutdown(self) -> None:
        self.refuse_new()
        self.discard_all("shutdown")

    def reset(self) -> None:
        """Test seam: shut down, forget every key, accept spares again."""
        self.shutdown()
        with self._lock:
            self._entries.clear()
            self._refuse = False

    def spare_pids(self) -> list[int]:
        with self._lock:
            return [s.pid for e in self._entries.values() for s in e.spares if s.alive()]

    def spare_count(self) -> int:
        with self._lock:
            return sum(len(e.spares) for e in self._entries.values())


_POOL = Pool()
atexit.register(_POOL.shutdown)


def discard_all(reason: str = "") -> None:
    _POOL.discard_all(reason)


def refuse_new() -> None:
    _POOL.refuse_new()


def shutdown() -> None:
    _POOL.shutdown()


def reset() -> None:
    _POOL.reset()


def spare_pids() -> list[int]:
    return _POOL.spare_pids()


# ---- fingerprints -------------------------------------------------------------

def _env_digest(env) -> str:
    h = hashlib.sha1()
    for k, v in sorted(env.items()):
        h.update(f"{k}={v}\0".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _site_dirs(interpreter: str) -> list[str]:
    """Site-packages directories whose change a pre-started interpreter could
    miss (a `.pth` or package added after it booted)."""
    dirs: list[str] = []
    root = os.path.dirname(os.path.dirname(interpreter))
    if os.path.isfile(os.path.join(root, "pyvenv.cfg")):
        import glob
        dirs.extend(sorted(glob.glob(os.path.join(root, "lib", "python*", "site-packages"))))
    elif interpreter == sys.executable:
        import sysconfig
        for k in ("purelib", "platlib"):
            p = sysconfig.get_paths().get(k)
            if p and p not in dirs:
                dirs.append(p)
    return dirs


def _site_stamp(interpreter: str):
    try:
        stamp = [os.stat(interpreter).st_mtime_ns]
    except OSError:
        return None
    for d in _site_dirs(interpreter):
        try:
            stamp.append(os.stat(d).st_mtime_ns)
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def _fingerprint(interpreter: str, env) -> tuple:
    return (_env_digest(env), _site_stamp(interpreter))


# ---- builtin kind ---------------------------------------------------------------

def claim_builtin(exe: str) -> subprocess.Popen | None:
    """A parked `_child.py` for `exe`, or None (run cold). The caller owns the
    returned process: it writes the request to stdin and reads the result."""
    if not enabled() or not exe or not os.path.isabs(exe):
        return None
    try:
        from . import executor
        env = dict(os.environ)
        spare = _POOL.claim("builtin", exe, _fingerprint(exe, env),
                            [exe, "-c", BUILTIN_BOOT, executor.CHILD], env)
    except Exception as e:  # noqa: BLE001
        logger.debug("warm run: builtin claim failed: %s", e)
        return None
    return spare.proc if spare is not None else None


def prime(backend, app_interpreter) -> None:
    """Seed the builtin key and, when the fused backend is up, the app
    interpreter's fused key, so the first run after startup is warm (best
    effort, called from `engine.warm()`'s background thread)."""
    if not enabled():
        return
    exe = sys.executable
    if exe and os.path.isabs(exe):
        from . import executor
        env = dict(os.environ)
        _POOL.claim("builtin", exe, _fingerprint(exe, env),
                    [exe, "-c", BUILTIN_BOOT, executor.CHILD], env, seed_only=True)
    if backend is None or not _backend_ok(backend):
        return
    interpreter = app_interpreter()
    if not interpreter or not os.path.isabs(interpreter):
        return
    dispatcher = backend._ensure_dispatcher()
    env = backend._child_env(dispatcher.host_dir, 0, interpreter, None)
    _POOL.claim("fused", interpreter, _fingerprint(interpreter, env),
                [interpreter, "-c", A_BOOT], env, seed_only=True)


# ---- fused kind -------------------------------------------------------------------

_shape_cache: dict[type, bool] = {}


def _backend_ok(backend) -> bool:
    cls = type(backend)
    cached = _shape_cache.get(cls)
    if cached is not None:
        return cached
    ok = all(hasattr(backend, a) for a in _REQUIRED_BACKEND_ATTRS)
    if ok:
        try:
            ok = (tuple(inspect.signature(backend._execute_sync).parameters)
                  == _EXPECTED_EXECUTE_SYNC_PARAMS[1:]
                  and tuple(inspect.signature(backend._child_env).parameters)
                  == _EXPECTED_CHILD_ENV_PARAMS[1:])
        except (TypeError, ValueError):
            ok = False
    _shape_cache[cls] = ok
    return ok


def execute_fused(backend, code: str, input_files: dict | None, interpreter: str):
    """`backend._execute_sync(code=, input_files=, interpreter=)` with the
    interpreter start-up already paid, or None when no spare is ready / the
    backend's shape is not the one pinned here (the caller then runs the cold
    `_execute_sync`). Mirrors `_execute_sync` step for step except the spawn."""
    try:
        if not enabled() or not interpreter or not os.path.isabs(interpreter):
            return None
        if not _backend_ok(backend):
            return None
        from fused.agent_core.backends.base import ExecutionResult
        from fused.agent_core.backends.local import python_compute

        dispatcher = backend._ensure_dispatcher()
        env = backend._child_env(dispatcher.host_dir, 0, interpreter, None)
        spare = _POOL.claim("fused", interpreter, _fingerprint(interpreter, env),
                            [interpreter, "-c", A_BOOT], env)
    except Exception as e:  # noqa: BLE001
        logger.debug("warm run: fused claim failed: %s", e)
        return None
    if spare is None:
        return None

    proc = spare.proc
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            backend._write_handler_files(tmppath)
            event = backend._make_event(code, input_files, None, tmppath, None)
            (tmppath / "event.json").write_text(json.dumps(event))
            run_py = tmppath / "run.py"
            run_py.write_text(python_compute._RUN_SCRIPT)

            go = (json.dumps({"run_py": str(run_py)}) + "\n").encode()
            t0 = time.monotonic()
            try:
                out, err = proc.communicate(go, timeout=backend._timeout_seconds)
            except subprocess.TimeoutExpired:
                _kill(spare)
                msg = f"Execution timed out after {backend._timeout_seconds}s"
                return ExecutionResult(
                    stdout="", stderr=msg, return_value=None,
                    duration_ms=int((time.monotonic() - t0) * 1000), error=msg)
            duration_ms = int((time.monotonic() - t0) * 1000)
            return backend._result_from_run(
                proc.poll(), out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                duration_ms, None)
    except BaseException:
        _kill(spare)
        raise
    finally:
        _reap(proc)

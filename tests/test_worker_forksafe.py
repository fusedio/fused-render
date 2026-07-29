"""Fork-safety of the worker/child spawns (native-SIGSEGV regression).

Opening the pyramid view of a large mount-backed COG could crash the whole run
with `worker exited with code -11 without producing a result` (-11 = SIGSEGV).
The faulting thread was always a PROJ pthread_atfork *child* handler:

    subprocess_fork_exec -> fork -> _pthread_atfork_child_handlers
      -> osgeo::proj::io::SQLiteHandleCache::getHandle(...)::$_2
      -> ...Cache::clear() -> sqlite3Close -> osgeo::proj::VFSClose
      -> EXC_BAD_ACCESS (SIGSEGV)

The forking process (the server thread running executor.run_python, and the
executor's _child.py running the pyramid main()) has libproj resident with a
live proj.db SQLite handle in PROJ's handle cache (pyproj is pulled in
transitively, e.g. via `fused`/geopandas). `fork()` runs every registered
pthread_atfork child handler in the forked child before exec; PROJ's handler
sqlite3_close()es that now-invalid handle and segfaults before the worker can
even exec.

The fix makes every worker spawn use `posix_spawn` instead of `fork()+exec`
(via `close_fds=False`, or `os.posix_spawn` directly for the detached build
job). posix_spawn runs NO atfork handlers, so the crash path is gone. These
tests lock that in: the spawns must not take the fork path.
"""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from fused_render import executor, server
from fused_render import _server_ai
from fused_render.templates.pyramid import overview_pyramid as op

_POSIX = os.name == "posix"


# --------------------------------------------------------------------------
# executor._child.py spawn — the site in the crash report (server thread)
# --------------------------------------------------------------------------

def test_executor_child_spawn_disables_fork(tmp_path, monkeypatch):
    """run_python must spawn _child.py with close_fds=False, which is what
    forces CPython onto the posix_spawn path (no atfork handlers) instead of
    fork()+exec. A regression that drops this reopens the PROJ atfork SIGSEGV."""
    script = tmp_path / "trivial.py"
    script.write_text("def main():\n    return {'ok': 1}\n")

    captured = {}

    real_run = subprocess.run

    def fake_run(argv, **kw):
        captured.update(kw)
        return real_run(argv, **kw)

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    executor.run_python(str(script), {})
    assert captured.get("close_fds") is False, (
        "executor must pass close_fds=False so the child is spawned via "
        "posix_spawn, not fork() (which runs PROJ's crashing atfork handler)"
    )


@pytest.mark.skipif(not _POSIX, reason="pthread_atfork is POSIX-only")
def test_executor_spawn_runs_no_atfork_child_handler(tmp_path, monkeypatch):
    """Behavioral guard: register a C-level pthread_atfork CHILD handler (the
    same layer PROJ's crashing handler lives at) and prove it does NOT run when
    the executor spawns the child. It runs on fork() (the buggy path) and does
    NOT run on posix_spawn (the fix). This test FAILS before the fix."""
    import ctypes

    libc = ctypes.CDLL(None)
    # pthread_atfork is a plain exported symbol on macOS libSystem, but on glibc
    # it is a static-only wrapper around __register_atfork and is NOT resolvable
    # through CDLL(None) ("undefined symbol: pthread_atfork"). Where we can't
    # register a probe handler, skip — the close_fds=False assertion in
    # test_executor_child_spawn_disables_fork already covers the fix portably,
    # and the crash this behavioral guard mirrors is macOS-specific anyway.
    try:
        register_atfork = libc.pthread_atfork
    except AttributeError:
        pytest.skip("pthread_atfork not dynamically resolvable here (e.g. glibc)")

    marker = tmp_path / "atfork_child_ran"
    monkeypatch.setenv("FR_ATFORK_MARKER", str(marker))

    HANDLER = ctypes.CFUNCTYPE(None)

    def _child_handler():
        # gated on the env var so this process-global handler stays inert for
        # the rest of the session once the test clears the var
        p = os.environ.get("FR_ATFORK_MARKER")
        if not p:
            return
        try:
            fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o644)
            os.write(fd, b"x")
            os.close(fd)
        except Exception:
            pass

    _cb = HANDLER(_child_handler)
    # keep a ref alive for the life of the process (handler is never unregistered)
    globals().setdefault("_atfork_cbs", []).append(_cb)
    register_atfork(None, None, _cb)

    script = tmp_path / "trivial.py"
    script.write_text("def main():\n    return {'ok': 1}\n")

    res = executor.run_python(str(script), {})
    assert res.get("ok"), res
    assert not marker.exists(), (
        "a pthread_atfork child handler ran during the child spawn -> the "
        "executor took the fork() path; PROJ's atfork handler would SIGSEGV here"
    )


# --------------------------------------------------------------------------
# pyramid template worker spawns (_child.py running main() forks the worker)
# --------------------------------------------------------------------------

def test_pyramid_analyze_worker_spawn_disables_fork(monkeypatch):
    """The analyze/predict worker spawn (subprocess.run) must pass
    close_fds=False -> posix_spawn, not fork()+exec."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps({"ok": True, "levels": []})
        stderr = ""

    def fake_run(argv, **kw):
        captured.update(kw)
        return _Proc()

    monkeypatch.setattr(op, "_venv_python", lambda: "/fake/python")
    monkeypatch.setattr(subprocess, "run", fake_run)

    # local file, no src -> analyze spawns the worker straight away
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    op.main(file="/tmp/x.tif", action="analyze")
    assert captured.get("close_fds") is False


@pytest.mark.skipif(not _POSIX, reason="posix_spawn is POSIX-only")
def test_pyramid_build_worker_uses_posix_spawn(tmp_path, monkeypatch):
    """The detached build/cogify spawn must go through os.posix_spawn with
    setsid=True (session detachment, no atfork handlers), NOT subprocess.Popen
    (fork()+exec)."""
    tif = tmp_path / "x.tif"
    tif.write_bytes(b"II*\x00" + b"0" * 100)

    # redirect ~/.cache job dir into tmp so the test writes nothing under $HOME
    real_expand = os.path.expanduser

    def fake_expand(p):
        if p.startswith("~"):
            return str(tmp_path / "home") + p[1:]
        return real_expand(p)

    monkeypatch.setattr(os.path, "expanduser", fake_expand)
    monkeypatch.setattr(op, "_venv_python", lambda: "/fake/python")
    # build is local-only; no src -> local kernel isfile probe
    monkeypatch.setattr(os.path, "isfile", lambda p: True)

    captured = {}

    def fake_spawn(path, argv, env, **kw):
        captured["path"] = path
        captured["kw"] = kw
        return 424242

    # Popen must NOT be used for this spawn; blow up if it is.
    def boom_popen(*a, **k):
        raise AssertionError("build spawn used subprocess.Popen (fork), not posix_spawn")

    monkeypatch.setattr(os, "posix_spawn", fake_spawn)
    monkeypatch.setattr(subprocess, "Popen", boom_popen)

    res = op.main(file=str(tif), action="build")
    assert res.get("started") is True
    assert res.get("status_key")
    assert captured["kw"].get("setsid") is True
    assert captured["path"] == "/fake/python"


# --------------------------------------------------------------------------
# /api/ai claude CLI spawn (server._spawn_claude_stream, async)
# --------------------------------------------------------------------------

def test_ai_claude_spawn_disables_fork(monkeypatch):
    """_spawn_claude_stream must pass close_fds=False -> posix_spawn, not
    fork()+exec, with all three stdio streams piped (the prompt travels over
    stdin as a stream-json message — argv has an OS size cap)."""
    captured = {}

    class _Proc:
        returncode = 0

    async def fake_exec(*argv, **kw):
        captured.update(kw)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(_server_ai._spawn_claude_stream(
        ["claude", "-p"], dict(os.environ)))
    assert captured.get("close_fds") is False, (
        "the ai spawn must pass close_fds=False so the claude CLI is spawned "
        "via posix_spawn (no atfork handlers), not fork()+exec")
    assert captured.get("stdin") is asyncio.subprocess.PIPE
    assert captured.get("stdout") is asyncio.subprocess.PIPE
    assert captured.get("stderr") is asyncio.subprocess.PIPE


def test_ai_reap_kills_the_windows_process_tree(monkeypatch):
    """On win32 a .cmd shim runs through cmd.exe, so kill() alone orphans the
    node/claude child (it keeps running and billing after we answer timeout).
    Reaping a live claude process must taskkill /T the tree; POSIX keeps
    plain kill()."""
    killed = {}

    class _Proc:
        pid = 4242
        returncode = None

        async def wait(self):
            return 1

        def kill(self):
            killed["kill"] = True

    def fake_taskkill(argv, **kw):
        killed["taskkill"] = argv

    monkeypatch.setattr(subprocess, "run", fake_taskkill)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000,
                        raising=False)

    def run(coro):
        # the loop must exist before sys.platform reads "win32", or asyncio
        # tries to build the real Windows proactor loop on this box
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(sys, "platform","win32")
    run(_server_ai._ai_reap(_Proc()))
    assert killed.get("taskkill") == ["taskkill", "/T", "/F", "/PID", "4242"]
    assert killed.get("kill") is True  # fallback still fires

    killed.clear()
    monkeypatch.setattr(sys, "platform","darwin")
    run(_server_ai._ai_reap(_Proc()))
    assert "taskkill" not in killed
    assert killed.get("kill") is True

    killed.clear()
    # an already-exited process is only waited on, never re-killed
    dead = _Proc()
    dead.returncode = 0
    run(_server_ai._ai_reap(dead))
    assert killed == {}

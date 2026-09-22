"""Regression guard for "finding 0" (code review, PR #1290): the server must
still IMPORT on Windows, not merely degrade gracefully once running.

`fused_render/pty_session.py` used to `import fcntl`/`import termios` at
module scope. Both are POSIX-only. `fused_render/server/app.py` does an
unconditional top-level `from fused_render.server.routers.terminal import
router`, and that router imports `pty_session` — so on Windows, importing
`fused_render.server.app` raised ImportError before `resolve_profile()`'s
`os.name == "nt"` guard ever got a chance to run, taking the WHOLE app down,
not just the terminal. The fix moved the two POSIX-only imports into the one
function that uses them (`PtySession.resize`), which is never reached on
Windows (no session is ever constructed there — `PtySessionRegistry.create`
raises before touching `PtySession` once `resolve_profile()` returns None).

Run as a subprocess with `fcntl`/`termios` import-blocked, rather than
monkeypatching `sys.modules` in-process: this module is already imported by
every other test file in this suite by the time this one runs, and reload
games on a module with live daemon threads/fds elsewhere would be fragile.
A real Windows machine has no `fcntl`/`termios` to import in the first
place, which this reproduces exactly.
"""
import subprocess
import sys

_SCRIPT = """
import builtins
import sys

_BLOCKED = {"fcntl", "termios"}
_real_import = builtins.__import__


def _fake_import(name, *args, **kwargs):
    if name in _BLOCKED:
        raise ImportError(f"simulated: no module named {name!r} on Windows")
    return _real_import(name, *args, **kwargs)


builtins.__import__ = _fake_import

import fused_render.pty_session  # noqa: F401
import fused_render.server.app as app_mod

app = app_mod.create_app(start_dir=".")
print("OK")
"""


def test_server_app_imports_without_fcntl_or_termios():
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout

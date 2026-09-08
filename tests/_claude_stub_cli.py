"""Writes a fake `claude` CLI binary that the session-host tests can launch.

Shared by test_claude_session_host.py, test_claude_send_action.py and
test_claude_control_requests.py — each defines its own stub protocol (a
`_STUB.format(python=...)` script body) but needs the same thing done with it:
a file that `subprocess.Popen([path, ...])` can actually launch, handed back
as the string those tests point FUSED_RENDER_CLAUDE_BIN at.

POSIX launches an extensionless file with a `#!<python>` shebang directly, so
the body is written as-is and given the execute bit. Windows has neither
shebang handling nor an execute bit: CreateProcess cannot run an extensionless
file at all, so the body goes into a sibling `.py` file and `path` instead
names a `.bat` that invokes `sys.executable` on it — `subprocess.Popen`
launches a `.bat` directly on Windows (CreateProcess special-cases `.bat`/
`.cmd` through `cmd.exe` even without `shell=True`), and the interpreter that
runs the `.py` is the same one running the tests, matching the POSIX shebang.
"""
import os
import stat
import sys


def write_stub_cli(bin_dir, script_body):
    """Write `script_body` (a `#!{python}`-shebang Python script, already
    `.format(python=...)`-filled) as a `claude` stub under `bin_dir` and
    return the path to hand to FUSED_RENDER_CLAUDE_BIN."""
    bin_dir.mkdir()
    if os.name == "nt":
        script = bin_dir / "claude_stub.py"
        # Strip the POSIX shebang line — irrelevant on Windows, and Windows
        # Python module resolution doesn't need it.
        body = script_body.split("\n", 1)[1] if script_body.startswith("#!") else script_body
        script.write_text(body)
        launcher = bin_dir / "claude.bat"
        launcher.write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script))
        return str(launcher)
    path = bin_dir / "claude"
    path.write_text(script_body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)

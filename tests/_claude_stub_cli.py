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


def reap_host(run_dir, timeout=5.0):
    """Tear down the detached session host (and the stub CLI it spawned)
    that a test's `agent._start` left running under `run_dir`.

    The host is spawned `start_new_session` on purpose so a real chat
    survives a server restart, which means nothing reaps it when the test
    process exits: pytest's tmp_path is rmtree'd, and the host and stub stay
    up until reboot. A stub that never closes its turn (no `result` row, or
    a `control_response` row after the `result`, which agent._turn_state
    reads as the turn reopening) also defeats the host's own idle reap, so
    every test that starts a real host must call this in teardown.

    Both pids are session leaders (agent._DETACH), so each pid is its own
    process group: `killpg` on the host's pid takes the host, `killpg` on the
    CLI's pid takes the stub and whatever it forked. Waits up to `timeout`
    for the host to have written its pid files first, so a test that ends
    before the host finished starting still gets it reaped."""
    import signal
    import time

    if os.name == "nt":
        return
    host_json = os.path.join(run_dir, "host.json")
    pid_file = os.path.join(run_dir, "pid")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not (
            os.path.exists(host_json) and os.path.exists(pid_file)):
        time.sleep(0.05)
    pids = set()
    for path, key in ((host_json, "pid"), (pid_file, None)):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            pid = int(__import__("json").loads(raw)[key] if key else raw.strip())
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pid > 1:
            pids.add(pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in list(pids):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                pids.discard(pid)
            except PermissionError:
                pids.discard(pid)
        deadline = time.monotonic() + 2.0
        while pids and time.monotonic() < deadline:
            for pid in list(pids):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pids.discard(pid)
            time.sleep(0.05)
        if not pids:
            break

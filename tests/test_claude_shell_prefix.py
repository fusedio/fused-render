"""The CLAUDE_CODE_SHELL_PREFIX wrapper (fused_render/claude_shell_prefix.sh),
run for real under /bin/sh. Contract: transparent for anything that is not a
Bash-tool command; for a Bash-tool-shaped string, same stdout/stderr/exit
status as without it plus a per-command log."""
import os
import shlex
import subprocess
import tempfile
import time

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX wrapper")

WRAPPER = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "fused_render",
    "claude_shell_prefix.sh"))


# `source` of a missing file is fatal in POSIX-mode sh (it is in zsh's real
# use not), so the fake snapshot must exist.
_SNAPDIR = tempfile.mkdtemp(prefix="fr-snap-")
os.makedirs(os.path.join(_SNAPDIR, "shell-snapshots"))
SNAPSHOT = os.path.join(_SNAPDIR, "shell-snapshots", "snapshot-zsh-1700-abc.sh")
open(SNAPSHOT, "w").close()


def bash_tool_string(user_cmd: str, cwdfile: str) -> str:
    """The shape claude 2.1.291 hands the prefix (probed)."""
    return ("source " + SNAPSHOT + " "
            "2>/dev/null || true && { shopt -u extglob || setopt NO_EXTENDED_GLOB; } "
            ">/dev/null 2>&1 || true && eval " + shlex.quote(user_cmd)
            + " < /dev/null && pwd -P >| " + cwdfile)


def run(arg, logdir, stdin=b"", extra_env=None, timeout=30):
    env = dict(os.environ, SHELL="/bin/sh", FUSED_CLAUDE_CMD_LOG=str(logdir))
    env.pop("CLAUDE_CODE_SHELL", None)
    env.update(extra_env or {})
    return subprocess.run([WRAPPER, arg], input=stdin, capture_output=True,
                          env=env, timeout=timeout)


def entries(logdir):
    return sorted({n.split(".")[0] for n in os.listdir(logdir)
                   if not n.endswith((".fo", ".fe"))})


def read(logdir, ident, ext):
    with open(os.path.join(logdir, ident + "." + ext), "rb") as f:
        return f.read()


def test_wrapper_is_executable():
    assert os.access(WRAPPER, os.X_OK)


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/bash"])
def test_real_shells_exit_cwd_and_streams(tmp_path, shell):
    # The CLI's real shell is zsh/bash (the snapshot is theirs); same contract.
    if not os.access(shell, os.X_OK):
        pytest.skip(shell + " missing")
    sub = tmp_path / "d"
    sub.mkdir()
    cwdf = tmp_path / "w-cwd"
    r = run(bash_tool_string(f"cd {shlex.quote(str(sub))}; echo o; echo e >&2; false",
                             str(cwdf)), tmp_path / "log", extra_env={"SHELL": shell})
    assert (r.stdout, r.stderr, r.returncode) == (b"o\n", b"e\n", 1)
    r = run(bash_tool_string("exit 7", str(cwdf)), tmp_path / "log2",
            extra_env={"CLAUDE_CODE_SHELL": shell})
    assert r.returncode == 7
    r = run(bash_tool_string(f"cd {shlex.quote(str(sub))}", str(cwdf)),
            tmp_path / "log3", extra_env={"SHELL": shell})
    assert cwdf.read_text().strip() == os.path.realpath(sub)


def test_non_bash_tool_is_byte_transparent_with_stdin(tmp_path):
    # An MCP-style stdio server: reads stdin, echoes it, writes stderr too.
    payload = b'{"jsonrpc":"2.0","id":1}\n\x00\xff binary \n'
    r = run("cat; echo err >&2; exit 3", tmp_path, stdin=payload)
    assert r.stdout == payload
    assert r.stderr == b"err\n"
    assert r.returncode == 3
    assert os.listdir(tmp_path) == []  # nothing logged, nothing created


def test_hook_shaped_command_is_transparent(tmp_path):
    r = run('printf \'{"ok":true}\'', tmp_path)
    assert r.stdout == b'{"ok":true}' and r.stderr == b"" and r.returncode == 0
    assert os.listdir(tmp_path) == []


def test_bash_tool_output_and_log(tmp_path):
    log = tmp_path / "log"
    cwdf = tmp_path / "claude-1-cwd"
    r = run(bash_tool_string("echo out; echo err >&2", str(cwdf)), log)
    assert r.stdout == b"out\n" and r.stderr == b"err\n" and r.returncode == 0
    (ident,) = entries(log)
    assert b"echo out" in read(log, ident, "cmd")
    out = read(log, ident, "out")
    assert b"out\n" in out and b"err\n" in out
    meta = read(log, ident, "meta").decode()
    assert "pid=" in meta and "pgid=" in meta and "start=" in meta
    assert read(log, ident, "exit").strip() == b"0"
    assert cwdf.exists()


@pytest.mark.parametrize("cmd,code", [("false", 1), ("exit 7", 7), ("true", 0)])
def test_exit_code_preserved_and_logged(tmp_path, cmd, code):
    log = tmp_path / "log"
    r = run(bash_tool_string(cmd, str(tmp_path / "c-cwd")), log)
    assert r.returncode == code
    (ident,) = entries(log)
    assert read(log, ident, "exit").strip() == str(code).encode()


def test_cd_still_writes_cwd_file(tmp_path):
    target = tmp_path / "sub"
    target.mkdir()
    cwdf = tmp_path / "claude-9-cwd"
    r = run(bash_tool_string(f"cd {shlex.quote(str(target))}", str(cwdf)),
            tmp_path / "log")
    assert r.returncode == 0
    assert cwdf.read_text().strip() == os.path.realpath(target)


def test_background_shaped_command_logged(tmp_path):
    # run_in_background goes through the same shape.
    log = tmp_path / "log"
    r = run(bash_tool_string("sleep 0.2; echo later", str(tmp_path / "b-cwd")), log)
    assert r.stdout == b"later\n"
    (ident,) = entries(log)
    assert b"later" in read(log, ident, "out")


def test_stdin_reaches_bash_tool_command(tmp_path):
    cwdf = tmp_path / "s-cwd"
    s = ("source " + SNAPSHOT + " 2>/dev/null || true && "
         "cat && pwd -P >| " + str(cwdf))
    r = run(s, tmp_path / "log", stdin=b"piped")
    assert r.stdout == b"piped"


def test_no_log_env_falls_back_to_transparent(tmp_path):
    env = {"FUSED_CLAUDE_CMD_LOG": ""}
    r = run(bash_tool_string("echo hi", str(tmp_path / "x-cwd")), tmp_path, extra_env=env)
    assert r.stdout == b"hi\n" and r.returncode == 0
    assert os.listdir(tmp_path) == ["x-cwd"]  # the cwd file only, no log


def test_sigterm_to_wrapper_reaches_command(tmp_path):
    log = tmp_path / "log"
    env = dict(os.environ, SHELL="/bin/sh", FUSED_CLAUDE_CMD_LOG=str(log))
    p = subprocess.Popen([WRAPPER, bash_tool_string("sleep 30", str(tmp_path / "k-cwd"))],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if os.path.isdir(log) and any(n.endswith(".meta") for n in os.listdir(log)):
            break
        time.sleep(0.05)
    time.sleep(0.3)
    p.terminate()
    p.wait(timeout=10)
    assert p.returncode != 0
    (ident,) = entries(log)
    assert read(log, ident, "exit").strip() != b"0"


SIGCHECK = ("python3 -c 'import signal,sys; sys.exit(0 if "
            "signal.getsignal(signal.SIGINT) is not signal.SIG_IGN and "
            "signal.getsignal(signal.SIGQUIT) is not signal.SIG_IGN else 3)'")


@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash", "/bin/zsh"])
def test_logged_command_does_not_start_with_sigint_ignored(tmp_path, shell):
    # A non-interactive sh starts `cmd &` with SIGINT/SIGQUIT ignored, and a
    # child cannot undo an inherited SIG_IGN: KeyboardInterrupt would be dead.
    if not os.access(shell, os.X_OK):
        pytest.skip(shell + " missing")
    r = run(bash_tool_string(SIGCHECK, str(tmp_path / "c-cwd")), tmp_path / "log",
            extra_env={"CLAUDE_CODE_SHELL": shell})
    assert r.returncode == 0, r.stderr
    assert r.stderr == b""


@pytest.fixture
def fish_path(tmp_path):
    d = tmp_path / "fishbin"
    d.mkdir()
    f = d / "fish"
    f.write_text("#!/bin/sh\nexit 99\n")
    f.chmod(0o755)
    return str(d)


def test_transparent_command_ignores_non_posix_shell(tmp_path, fish_path):
    for shell in ("/nonexistent/fish", os.path.join(fish_path, "fish")):
        r = run("echo ok", tmp_path / "log", extra_env={
            "SHELL": shell, "PATH": fish_path + os.pathsep + os.environ["PATH"]})
        assert (r.stdout, r.returncode) == (b"ok\n", 0)


@pytest.mark.skipif(not os.access("/bin/bash", os.X_OK), reason="no bash")
def test_bash_snapshot_command_runs_under_bash(tmp_path, fish_path):
    snap = os.path.join(_SNAPDIR, "shell-snapshots", "snapshot-bash-1700-abc.sh")
    open(snap, "w").close()
    cwdf = str(tmp_path / "b-cwd")
    s = ("source " + snap + " 2>/dev/null || true && eval "
         + shlex.quote('echo "v=$BASH_VERSION"') + " < /dev/null && pwd -P >| " + cwdf)
    r = run(s, tmp_path / "log", extra_env={
        "SHELL": "/nonexistent/fish", "PATH": fish_path + os.pathsep + os.environ["PATH"]})
    assert r.returncode == 0 and r.stdout.startswith(b"v=") and len(r.stdout.strip()) > 2


def test_exit_codes_and_stderr_are_exact(tmp_path):
    for code in (0, 3, 42):
        r = run(bash_tool_string(f"echo e >&2; exit {code}", str(tmp_path / "e-cwd")),
                tmp_path / "log")
        assert (r.stdout, r.stderr, r.returncode) == (b"", b"e\n", code)
    r = run(bash_tool_string("true", str(tmp_path / "e-cwd")), tmp_path / "log")
    assert (r.stdout, r.stderr, r.returncode) == (b"", b"", 0)

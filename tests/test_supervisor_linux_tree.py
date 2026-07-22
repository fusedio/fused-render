"""Linux process-tree keeper — the no-orphans-on-crash gate (gate (a) of
docs/LINUX_DESKTOP_SPEC.md) in CI form.

Linux-only: PDEATHSIG/killpg/pid-namespaces are Linux kernel features. The
suite skips cleanly elsewhere (mirrors tests/test_supervisor_job.py skipping
off-Windows).

Liveness is checked via `flock`, not pids: inside the "namespace" mechanism the
server runs as pid 1 of a private pid namespace, so its host pid is not
`os.getpid()` and pid-based `os.kill(pid, 0)` checks would be meaningless. A
process that holds an exclusive lock on a shared file is alive; once the kernel
releases that lock (because the process died), the test can take it — this works
identically across pid namespaces because the mount namespace is shared.
"""
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux process-tree primitives (PDEATHSIG / killpg / pid namespaces)",
)

if sys.platform.startswith("linux"):
    from fused_render.supervisor._linux.tree import Job

_DEADLINE_S = 10.0

# The "server" stand-in: hold an exclusive lock, spawn one grandchild that does
# the same (optionally in its own session — the escape case), then park until a
# stop file appears. Stdlib only, so it runs under the child's stripped env.
_LEAF = r"""
import os, sys, fcntl, subprocess, time
leaf_lock, gc_lock, gc_ready, ready, stop, new_session = sys.argv[1:7]
_fd = os.open(leaf_lock, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(_fd, fcntl.LOCK_EX)
_gc = r'''
import os, sys, fcntl, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
while not os.path.exists(sys.argv[3]):
    time.sleep(0.1)
'''
gc = subprocess.Popen(
    [sys.executable, "-c", _gc, gc_lock, gc_ready, stop],
    start_new_session=(new_session == "1"),
)
while not os.path.exists(gc_ready):
    time.sleep(0.05)
open(ready, "w").close()
while not os.path.exists(stop):
    time.sleep(0.1)
"""

# The "supervisor" stand-in for the crash test: build a Job, spawn the server
# under it, then park holding the Job reference alive until killed.
_SUPERVISOR = r"""
import os, sys, time
from pathlib import Path
os.environ["FUSED_RENDER_LINUX_TREE_KILL"] = sys.argv[1]
from fused_render.supervisor._linux.tree import Job
leaf_script, leaf_lock, gc_lock, gc_ready, ready, stop, new_session = sys.argv[2:9]
job = Job()
job.spawn(
    Path(sys.executable),
    ["-I", leaf_script, leaf_lock, gc_lock, gc_ready, ready, stop, new_session],
)
while not os.path.exists(stop):
    time.sleep(0.2)
"""


def _userns_available() -> bool:
    unshare = shutil.which("unshare")
    if not unshare:
        return False
    try:
        result = subprocess.run(
            [unshare, "--user", "--map-root-user", "true"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _mechanisms() -> list[str]:
    mechs = ["pgroup"]
    if _userns_available():
        mechs.append("namespace")
    return mechs


def _lock_held(path: Path) -> bool:
    """True iff some (living) process still holds the exclusive lock."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def _wait_released(path: Path, deadline_s: float = _DEADLINE_S) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if not _lock_held(path):
            return True
        time.sleep(0.05)
    return not _lock_held(path)


def _wait_file(path: Path, deadline_s: float = _DEADLINE_S) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def _write_leaf_script(tmp_path: Path) -> Path:
    script = tmp_path / "leaf.py"
    script.write_text(_LEAF)
    return script


def _paths(tmp_path: Path):
    return (
        tmp_path / "leaf.lock",
        tmp_path / "gc.lock",
        tmp_path / "gc.ready",
        tmp_path / "ready",
        tmp_path / "stop",
    )


@pytest.mark.parametrize("mechanism", _mechanisms())
def test_close_kills_the_whole_process_group(tmp_path, monkeypatch, mechanism):
    """Deliberate teardown: job.close() reaps the server and its grandchild
    (grandchild in the SAME session — both mechanisms must handle this)."""
    monkeypatch.setenv("FUSED_RENDER_LINUX_TREE_KILL", mechanism)
    leaf_script = _write_leaf_script(tmp_path)
    leaf_lock, gc_lock, gc_ready, ready, stop = _paths(tmp_path)

    job = Job()
    job.spawn(
        Path(sys.executable),
        ["-I", str(leaf_script), str(leaf_lock), str(gc_lock), str(gc_ready),
         str(ready), str(stop), "0"],
    )
    try:
        _wait_file(ready)
        assert _lock_held(leaf_lock)
        assert _lock_held(gc_lock)
        job.close()
        assert _wait_released(leaf_lock), "server survived job.close()"
        assert _wait_released(gc_lock), "grandchild survived job.close()"
    finally:
        stop.write_text("")  # backstop cleanup for any survivor
        job.close()


@pytest.mark.parametrize("mechanism", _mechanisms())
def test_no_orphans_when_supervisor_is_killed(tmp_path, mechanism):
    """gate (a): SIGKILL the supervisor and the server must die with it.

    - pgroup: PDEATHSIG guarantees the DIRECT child (server) dies. A grandchild
      in a NEW session escapes — the documented baseline gap — so it is not
      asserted here (it is cleaned up via the stop file).
    - namespace: the kernel tears down the entire pid namespace, so the escaped
      grandchild dies too.
    """
    supervisor_script = tmp_path / "supervisor.py"
    supervisor_script.write_text(_SUPERVISOR)
    leaf_script = _write_leaf_script(tmp_path)
    leaf_lock, gc_lock, gc_ready, ready, stop = _paths(tmp_path)

    outer = subprocess.Popen(
        [sys.executable, "-I", str(supervisor_script), mechanism, str(leaf_script),
         str(leaf_lock), str(gc_lock), str(gc_ready), str(ready), str(stop), "1"],
    )
    try:
        _wait_file(ready)
        assert _lock_held(leaf_lock)
        assert _lock_held(gc_lock)

        outer.send_signal(signal.SIGKILL)
        outer.wait(timeout=_DEADLINE_S)

        assert _wait_released(leaf_lock), "server orphaned after supervisor SIGKILL"
        if mechanism == "namespace":
            assert _wait_released(gc_lock), (
                "grandchild orphaned despite pid-namespace teardown"
            )
    finally:
        stop.write_text("")
        if outer.poll() is None:
            outer.send_signal(signal.SIGKILL)
            outer.wait(timeout=_DEADLINE_S)

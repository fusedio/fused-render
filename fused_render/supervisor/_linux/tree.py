"""Linux process-tree keeper — the Job-Object counterpart.

This holds the one guarantee the whole desktop supervisor hinges on
(docs/LINUX_DESKTOP_SPEC.md gate (a), the "no-orphans-on-crash" analog of the
Windows Job Object's KILL_ON_JOB_CLOSE): when the supervisor dies — including a
hard `kill -9` — the server it spawned and that server's descendants must die
too, not linger as orphans holding a port and FUSE mounts.

Same surface `core.py` consumes via `_backend`:
  Job()              — a supervised process tree
  Job.spawn(...)     — launch the server, returns a waitable process
                       (`.wait(timeout_ms) -> bool`, `.id`)
  Job.close()        — tree-kill everything the job owns

Two mechanisms, both stdlib-only, selected by `FUSED_RENDER_LINUX_TREE_KILL`
(the gate in docs/LINUX_DESKTOP_SPEC.md decides which ships by default):

  "pgroup"  (default) — spawn the child with `start_new_session=True` (setsid:
      the child becomes a session/process-group leader) and set
      `PR_SET_PDEATHSIG(SIGKILL)` via prctl in a preexec_fn so the *direct*
      child is killed the instant the supervisor dies. `close()` does
      `killpg(SIGKILL)` on the group for deliberate teardown. A grandchild that
      calls setsid *itself* escapes the group — the known baseline gap.

  "namespace" (opt-in) — wrap the server in an unprivileged user+pid namespace
      via `unshare --user --map-root-user --pid --fork --kill-child`. The server
      becomes pid 1 of a private pid namespace; the kernel reaps the entire
      namespace when that pid 1 dies, and `--kill-child` propagates a supervisor
      kill down to it. `--kill-child` is child-side PR_SET_PDEATHSIG (util-linux
      sys-utils/unshare.c arms it in the forked child right after fork), i.e.
      kernel-enforced: it fires however `unshare` dies, including SIGKILL — no
      userspace signal handler is involved. Airtight (the true kill-on-crash
      analog), but depends on unprivileged user namespaces being enabled on the
      host. `unshare` is exec'd, not linked.

This module is import-safe on non-Linux (prctl is only referenced inside the
preexec_fn, which only runs when the backend is actually live on Linux).
"""
from __future__ import annotations

import ctypes
import os
import signal
import shutil
import subprocess
from pathlib import Path

# prctl(2) option; PR_SET_PDEATHSIG delivers a signal to the caller when its
# parent thread dies. 1 is stable ABI (linux/prctl.h), safe to hardcode.
_PR_SET_PDEATHSIG = 1

_MECHANISM_ENV = "FUSED_RENDER_LINUX_TREE_KILL"
_DEFAULT_MECHANISM = "pgroup"

_STRIPPED_ENV_VARS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONINSPECT",
)


def _mechanism() -> str:
    value = (os.environ.get(_MECHANISM_ENV) or _DEFAULT_MECHANISM).strip().lower()
    if value not in ("pgroup", "namespace"):
        return _DEFAULT_MECHANISM
    return value


def environment_block(overrides: dict[str, str] | None) -> dict[str, str]:
    """Complete child environment: the current process env minus the Python
    interpreter-identity vars (a bundled child interpreter must not inherit its
    parent's home/path), plus `overrides`. Same contract as the Windows
    backend's environment_block, minus the case-folding Windows env needs."""
    env = {name: value for name, value in os.environ.items() if name not in _STRIPPED_ENV_VARS}
    env.update(overrides or {})
    return env


def _parent_changed(expected_ppid: int, current_ppid: int) -> bool:
    """Whether our parent has already changed from the supervisor that forked
    us. NOT `current_ppid == 1`: on systemd an orphan reparents to a session
    subreaper (systemd --user, a login session leader), not pid 1, so a `== 1`
    test would miss the death entirely and leave the child armed against a
    parent that is already gone."""
    return current_ppid != expected_ppid


def _pdeathsig_preexec(expected_ppid: int) -> None:
    """Runs in the forked child between fork() and exec(). Arm PDEATHSIG so the
    child dies with the supervisor, then close the race where the supervisor
    already died in the fork/exec window — in which case PDEATHSIG (armed
    against the now-dead original parent) will never fire, so self-kill if our
    parent has already changed."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    if _parent_changed(expected_ppid, os.getppid()):
        os.kill(os.getpid(), signal.SIGKILL)


def _launch_argv(mechanism: str, application: Path, arguments: list[str]) -> list[str]:
    if mechanism == "namespace":
        unshare = shutil.which("unshare")
        if unshare is None:
            raise FileNotFoundError(
                "unshare not found; cannot use the namespace tree-kill mechanism"
            )
        return [
            unshare,
            "--user",
            "--map-root-user",
            "--pid",
            "--fork",
            "--kill-child",
            str(application),
            *arguments,
        ]
    return [str(application), *arguments]


class SupervisedProcess:
    """Waitable handle over the spawned child, matching the Windows
    SupervisedProcess surface core.py depends on (`.wait(ms)`, `.id`)."""

    def __init__(self, popen: "subprocess.Popen"):
        self._popen = popen
        self.id = popen.pid

    def wait(self, timeout_ms: int) -> bool:
        """True iff the process has exited within `timeout_ms`. `timeout_ms=0`
        is a non-blocking poll (mirrors the Win32 WaitForSingleObject(0)
        idiom the run loop uses)."""
        try:
            self._popen.wait(timeout=timeout_ms / 1000.0)
            return True
        except subprocess.TimeoutExpired:
            return False


class Job:
    """Owns one supervised process tree. Kept alive for the supervisor's whole
    lifetime by run()'s top-level frame; `close()` tree-kills deterministically
    (never rely on GC)."""

    def __init__(self):
        self._process: SupervisedProcess | None = None
        self._pgid: int | None = None
        self._closed = False

    def spawn(
        self,
        application: Path,
        arguments: list[str],
        environment: dict[str, str] | None = None,
        output: Path | None = None,
    ) -> SupervisedProcess:
        mechanism = _mechanism()
        argv = _launch_argv(mechanism, application, arguments)
        env = environment_block(environment)
        # Captured BEFORE fork so the child's preexec can tell whether the
        # supervisor already died in the fork/exec window (see _parent_changed).
        expected_ppid = os.getpid()

        stdout = subprocess.DEVNULL
        log_handle = None
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(output, "a", buffering=1)  # noqa: SIM115 - closed below
            stdout = log_handle

        try:
            popen = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT if log_handle is not None else subprocess.DEVNULL,
                # setsid: the child leads its own session/process group so
                # killpg reaches the whole group in deliberate teardown, and so
                # a grandchild inherits the group unless it setsids away.
                start_new_session=True,
                # PDEATHSIG requires the classic fork+exec path (preexec_fn
                # forces it off posix_spawn). This is the supervisor spawning
                # the server before any native libs (PROJ etc.) are loaded, so
                # the fork-safety concern that pushes the server's own workers
                # to posix_spawn does not apply here.
                preexec_fn=lambda: _pdeathsig_preexec(expected_ppid),
                close_fds=True,
            )
        finally:
            if log_handle is not None:
                # The child has inherited the fd; the parent's copy is not
                # needed and must not keep the log file's write end open.
                log_handle.close()

        self._process = SupervisedProcess(popen)
        try:
            self._pgid = os.getpgid(popen.pid)
        except ProcessLookupError:
            # Already gone (spawned and died instantly); killpg in close() will
            # simply no-op.
            self._pgid = popen.pid
        return self._process

    def close(self) -> None:
        """Tree-kill: SIGKILL the whole process group, then reap. Idempotent.

        For the "namespace" mechanism, killing the group kills the `unshare`
        keeper, which `--kill-child` turns into a SIGKILL of pid 1 of the pid
        namespace, which the kernel turns into teardown of the entire
        namespace. For "pgroup" it kills every member still in the group."""
        if self._closed:
            return
        self._closed = True
        if self._pgid is not None:
            try:
                os.killpg(self._pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if self._process is not None:
            try:
                self._process._popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

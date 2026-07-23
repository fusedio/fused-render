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

This module is import-safe on non-Linux (the libc handle backing prctl is only
resolved on Linux — see _LIBC — and the preexec_fn only runs when the backend
is actually live on Linux).
"""
from __future__ import annotations

import ctypes
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The child env-block contract (strip interpreter-identity vars, merge
# overrides) is platform-neutral and owned by supervisor.paths; import it
# rather than duplicate the merge and the stripped-var tuple here.
from fused_render.supervisor.paths import environment_block

# prctl(2) option; PR_SET_PDEATHSIG delivers a signal to the caller when its
# parent thread dies. 1 is stable ABI (linux/prctl.h), safe to hardcode.
_PR_SET_PDEATHSIG = 1

# libc handle resolved AT IMPORT, in the parent, precisely so the preexec_fn
# never has to: ctypes.CDLL(None) dlopens post-fork otherwise, and dlopen takes
# internal locks — in a threaded parent (the supervisor runs a tray thread and
# open workers) the fork child can inherit one of those locks mid-held and
# deadlock before exec. Only the async-signal-safe prctl/getppid/kill calls are
# allowed between fork and exec. Linux-gated so the module stays import-safe
# elsewhere (see module docstring).
_LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None

_MECHANISM_ENV = "FUSED_RENDER_LINUX_TREE_KILL"
_DEFAULT_MECHANISM = "pgroup"

# How long close() gives the SIGTERM'd group to exit before escalating to
# SIGKILL. rclone unmounts its FUSE mounts cleanly on SIGTERM; an immediate
# SIGKILL strands any mount rcd was serving as a wedged FUSE endpoint, so
# deliberate teardown always offers the graceful path first.
_TERM_GRACE_S = 5.0


def _mechanism() -> str:
    value = (os.environ.get(_MECHANISM_ENV) or _DEFAULT_MECHANISM).strip().lower()
    if value not in ("pgroup", "namespace"):
        return _DEFAULT_MECHANISM
    return value


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
    parent has already changed. Uses only the pre-resolved _LIBC handle plus
    async-signal-safe syscalls — no dlopen/allocation between fork and exec."""
    _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
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
                # forces it off posix_spawn). The preexec body stays fork-safe
                # even in this threaded parent (tray thread, open workers):
                # libc is pre-resolved at import (_LIBC — no post-fork dlopen,
                # which takes locks another thread could hold mid-fork) and it
                # otherwise makes only async-signal-safe calls.
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

    def _group_alive(self) -> bool:
        """Probe whether any member of the group is still around (signal 0
        delivers nothing). ProcessLookupError means the group is empty; any
        other failure (PermissionError on an unexpected uid change, ...) is
        treated as alive so close() still escalates rather than assuming."""
        try:
            os.killpg(self._pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True

    def close(self) -> None:
        """Tree-kill, gracefully first: SIGTERM the whole process group, wait a
        bounded grace (_TERM_GRACE_S) for it to exit, then SIGKILL whatever is
        left, then reap. Idempotent.

        SIGTERM-first matters because the group contains rclone's rcd: it
        unmounts its FUSE mounts cleanly on SIGTERM, whereas an immediate
        SIGKILL strands every mount as a wedged FUSE endpoint.

        For the "namespace" mechanism, killing the group kills the `unshare`
        keeper, which `--kill-child` turns into a SIGKILL of pid 1 of the pid
        namespace, which the kernel turns into teardown of the entire
        namespace. For "pgroup" it kills every member still in the group."""
        if self._closed:
            return
        self._closed = True
        if self._pgid is not None:
            try:
                os.killpg(self._pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            else:
                deadline = time.monotonic() + _TERM_GRACE_S
                while time.monotonic() < deadline:
                    if self._process is not None:
                        # Reap the direct child so its zombie doesn't keep the
                        # group looking alive to the probe below.
                        try:
                            self._process._popen.wait(timeout=0.05)
                        except subprocess.TimeoutExpired:
                            pass
                    if not self._group_alive():
                        break
                    time.sleep(0.05)
            if self._group_alive():
                # Backstop: whatever ignored (or outlived) the SIGTERM.
                try:
                    os.killpg(self._pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        if self._process is not None:
            try:
                self._process._popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

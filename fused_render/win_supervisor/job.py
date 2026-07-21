"""Job Object process-tree kill + suspended-create/resume child launch — port
of windows/supervisor/src/job.rs (feat/windows-desktop-foundation, PR #162).

This is the one guarantee the whole Python-supervisor experiment hinges on
(docs/PYTHON_SUPERVISOR_SPEC.md's "no-orphans-on-crash" acceptance gate):
closing the last handle to the Job Object (including the kernel closing it
for us when this process dies, e.g. `taskkill /F`) must kill the entire
process tree it owns.

Two pywin32-specific gotchas that would silently break that guarantee if
gotten wrong (see the design doc / PYTHON_SUPERVISOR_SPEC.md):

1. PyHANDLE auto-closes on garbage collection. The `Job` returned by `Job()`
   must be kept alive for the supervisor's entire process lifetime (owned by
   `supervisor.run()`'s top-level frame) — if it were a short-lived local, its
   GC would fire KILL_ON_JOB_CLOSE against a healthy running server.
2. The job handle itself must stay non-inheritable. `CreateJobObject(None,
   "")` gives a non-inheritable handle; do not change that, even though the
   child process is created with `bInheritHandles=True` (required for the
   redirected stdio handles) — an inherited job handle would let the child
   keep the job alive past the supervisor's own death.
"""
from __future__ import annotations

import os
from pathlib import Path

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32job
import win32process

CREATE_NO_WINDOW = 0x08000000
FILE_APPEND_DATA = 0x0004

_STRIPPED_ENV_VARS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONINSPECT",
)


def quote_argument(argument: str) -> str:
    """Windows command-line argument quoting (CommandLineToArgvW rules) —
    byte-for-byte port of job.rs::quote_argument."""
    if argument and not any(ch.isspace() or ch == '"' for ch in argument):
        return argument

    quoted = ['"']
    backslashes = 0
    for ch in argument:
        if ch == "\\":
            backslashes += 1
        elif ch == '"':
            quoted.append("\\" * (backslashes * 2 + 1))
            quoted.append('"')
            backslashes = 0
        else:
            quoted.append("\\" * backslashes)
            quoted.append(ch)
            backslashes = 0
    quoted.append("\\" * (backslashes * 2))
    quoted.append('"')
    return "".join(quoted)


def command_line(application: str, arguments: list[str]) -> str:
    parts = [quote_argument(application)]
    parts.extend(quote_argument(a) for a in arguments)
    return " ".join(parts)


def environment_block(overrides: dict[str, str] | None) -> dict[str, str]:
    """Complete child environment: current process env, minus the Python
    interpreter-identity vars (a child pythonw must not inherit ITS parent
    interpreter's home/path), plus `overrides` (case-preserving, but
    case-insensitively deduped against the base — matches job.rs)."""
    values: dict[str, tuple[str, str]] = {
        name.upper(): (name, value) for name, value in os.environ.items()
    }
    for name in _STRIPPED_ENV_VARS:
        values.pop(name, None)
    for name, value in (overrides or {}).items():
        values[name.upper()] = (name, value)
    return {name: value for name, value in values.values()}


class SupervisedProcess:
    def __init__(self, h_process, pid: int):
        self._h_process = h_process
        self.id = pid

    def wait(self, timeout_ms: int) -> bool:
        return win32event.WaitForSingleObject(self._h_process, timeout_ms) == win32event.WAIT_OBJECT_0

    @property
    def handle(self):
        return self._h_process


class Job:
    def __init__(self):
        handle = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation, info)
        self._handle = handle

    def spawn(
        self,
        application: Path,
        arguments: list[str],
        environment: dict[str, str] | None = None,
        output: Path | None = None,
    ) -> SupervisedProcess:
        cmdline = command_line(str(application), arguments)
        env = environment_block(environment)

        si = win32process.STARTUPINFO()
        si.dwFlags = win32con.STARTF_USESHOWWINDOW
        si.wShowWindow = win32con.SW_HIDE

        h_in = h_out = None
        inherit_handles = False
        if output is not None:
            sa = pywintypes.SECURITY_ATTRIBUTES()
            sa.bInheritHandle = True
            h_in = win32file.CreateFile(
                "NUL", win32con.GENERIC_READ, 0, sa, win32con.OPEN_EXISTING, 0, None
            )
            h_out = win32file.CreateFile(
                str(output),
                FILE_APPEND_DATA,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                sa,
                win32con.OPEN_ALWAYS,
                0,
                None,
            )
            si.dwFlags |= win32con.STARTF_USESTDHANDLES
            si.hStdInput = h_in
            si.hStdOutput = h_out
            si.hStdError = h_out
            inherit_handles = True

        flags = win32con.CREATE_SUSPENDED | CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT
        try:
            h_process, h_thread, pid, _tid = win32process.CreateProcess(
                str(application), cmdline, None, None, inherit_handles, flags, env, None, si
            )
        finally:
            if h_in is not None:
                h_in.Close()
            if h_out is not None:
                h_out.Close()

        try:
            win32job.AssignProcessToJobObject(self._handle, h_process)
        except pywintypes.error:
            win32process.TerminateProcess(h_process, 1)
            h_thread.Close()
            raise
        try:
            win32process.ResumeThread(h_thread)
        except pywintypes.error:
            win32process.TerminateProcess(h_process, 1)
            raise
        finally:
            h_thread.Close()

        return SupervisedProcess(h_process, pid)

    def contains(self, process: SupervisedProcess) -> bool:
        return bool(win32job.IsProcessInJob(process.handle, self._handle))

    def close(self) -> None:
        """Deterministically close the job handle, firing
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE against everything the job owns.
        Do not rely on PyHANDLE garbage collection for this (see module
        docstring) — always call this explicitly."""
        self._handle.Close()

    @property
    def handle(self):
        return self._handle

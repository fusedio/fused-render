"""Job Object identity/lifecycle tests — the no-orphans-on-crash guarantee
depends on each Job() being its own private kernel object (not aliasing a
named object another Job() instance could open), and on failure paths never
leaking process handles instead of closing them explicitly."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("win32job")

import pywintypes
import win32job

from fused_render.win_supervisor.job import Job


def test_each_job_is_a_private_kernel_object():
    # If Job() ever created/opened a *named* job (e.g. the old empty-string
    # name), two Jobs would alias one kernel object and limits set through
    # one would be visible through the other.
    a, b = Job(), Job()
    try:
        info = a.handle and win32job.QueryInformationJobObject(
            a.handle, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info["BasicLimitInformation"]["ActiveProcessLimit"] = 1
        win32job.SetInformationJobObject(
            a.handle, win32job.JobObjectExtendedLimitInformation, info
        )
        info_b = win32job.QueryInformationJobObject(
            b.handle, win32job.JobObjectExtendedLimitInformation
        )
        assert not (
            info_b["BasicLimitInformation"]["LimitFlags"]
            & win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
    finally:
        a.close()
        b.close()


def test_spawn_failure_closes_process_handle(monkeypatch):
    import win32job as win32job_module

    captured = []

    def failing_assign(job_handle, h_process):
        captured.append(h_process)
        raise pywintypes.error(5, "AssignProcessToJobObject", "denied")

    monkeypatch.setattr(win32job_module, "AssignProcessToJobObject", failing_assign)
    job = Job()
    try:
        with pytest.raises(pywintypes.error):
            job.spawn(Path(sys.executable), ["-c", "pass"])
    finally:
        job.close()
    [h] = captured
    assert int(h) == 0  # h_process.Close() ran in the failure branch

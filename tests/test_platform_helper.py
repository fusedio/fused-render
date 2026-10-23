"""`fused_render._platform`: advisory file locks, tree kill and detach kwargs
behave the same through the POSIX and the Windows branch."""
import os
import subprocess
import sys
import time

import pytest

from fused_render import _platform


def test_flock_nonblocking_reports_contention(tmp_path):
    p = tmp_path / "l"
    a = open(p, "a")
    b = open(p, "a")
    try:
        assert _platform.flock(a, blocking=False) is True
        assert _platform.flock(b, blocking=False) is False
        _platform.funlock(a)
        assert _platform.flock(b, blocking=False) is True
        _platform.funlock(b)
    finally:
        a.close()
        b.close()


def test_flock_accepts_raw_fd(tmp_path):
    fd = os.open(tmp_path / "l", os.O_CREAT | os.O_RDWR)
    try:
        assert _platform.flock(fd, blocking=False) is True
        _platform.funlock(fd)
    finally:
        os.close(fd)


def test_flock_blocking_waits_for_release(tmp_path):
    import threading
    p = tmp_path / "l"
    a = open(p, "a")
    b = open(p, "a")
    got = []
    try:
        _platform.flock(a)
        t = threading.Thread(target=lambda: got.append(_platform.flock(b)))
        t.start()
        time.sleep(0.3)
        assert got == []
        _platform.funlock(a)
        t.join(5)
        assert got == [True]
    finally:
        a.close()
        b.close()


def test_kill_tree_kills_group_and_swallows_missing():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            **_platform.detach_kwargs())
    assert _platform.kill_tree(proc.pid) is True
    assert proc.wait(10) != 0
    assert _platform.kill_tree(proc.pid) is False  # already gone: no raise


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX detach kwargs")
def test_detach_kwargs_posix():
    assert _platform.detach_kwargs() == {"start_new_session": True}


def test_import_never_raises_without_posix_modules():
    code = (
        "import sys\n"
        "for n in ('fcntl','pwd','termios','pty','resource'): sys.modules[n] = None\n"
        "from fused_render import _platform\n"
        "print(_platform.IS_WINDOWS)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_probe_pid_alive_and_gone():
    _platform.probe_pid(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    with pytest.raises(ProcessLookupError):
        _platform.probe_pid(proc.pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_kill_tree_spares_the_callers_own_group():
    """A pid that leads no group of its own (the caller's own pid, here) is
    signalled alone, never through `killpg`, which would take the whole group."""
    import signal
    seen = []
    orig = os.killpg
    os.killpg = lambda *a: seen.append(a)
    try:
        assert _platform.kill_tree(os.getpid(), 0) is True
    finally:
        os.killpg = orig
    leader = os.getpgid(os.getpid()) == os.getpid()
    assert bool(seen) == leader


def _as_windows(monkeypatch):
    # What os.path / os.sep ARE on Windows, so the drive-root and case rules run
    # on any host.
    import ntpath

    monkeypatch.setattr(os, "path", ntpath)
    monkeypatch.setattr(os, "sep", "\\")


def test_is_within_posix_is_case_sensitive_and_segment_aware():
    assert _platform.is_within("/a/b/c", "/a/b")
    assert _platform.is_within("/a/b", "/a/b")
    assert not _platform.is_within("/a/b", "/a/b", strict=True)
    assert not _platform.is_within("/a/bc", "/a/b")
    assert not _platform.is_within("/A/b/c", "/a/b")
    assert _platform.is_within("/a", "/")
    assert _platform.is_within("/", "/")


def test_is_within_windows_drive_root_and_case(monkeypatch):
    _as_windows(monkeypatch)
    # A drive root already ends in the separator: root + os.sep is "C:\\\\".
    assert _platform.is_within("C:\\Users\\me", "C:\\")
    assert _platform.is_within("c:\\users\\me", "C:\\")
    assert _platform.is_within("C:\\", "C:\\")
    assert not _platform.is_within("C:\\", "C:\\", strict=True)
    assert _platform.is_within("C:\\Users\\me\\a", "c:\\USERS")
    assert not _platform.is_within("C:\\Users2", "C:\\Users")
    assert not _platform.is_within("D:\\Users", "C:\\")

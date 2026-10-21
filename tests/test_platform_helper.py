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
    _platform.kill_tree(proc.pid)
    assert proc.wait(10) != 0
    _platform.kill_tree(proc.pid)  # already gone: no raise


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

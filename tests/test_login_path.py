"""login_path: merge the user's login-shell PATH into the server's environ.

REAL subprocesses against tiny fake shells in tmp_path — a faked
subprocess.run hid lifecycle bugs in this repo before.
"""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix shells")

from fused_render import login_path  # noqa: E402


def _shell(tmp_path, body, name="fakesh"):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


@pytest.fixture(autouse=True)
def _reset():
    login_path._reset_for_tests()
    yield
    login_path._reset_for_tests()


def test_probe_parses_between_markers_ignoring_rc_noise(tmp_path):
    sh = _shell(tmp_path, 'echo "motd noise"\n'
                          'echo "rc: loaded nvm"\n'
                          'printf %s __FR_PATH_BEGIN__\n'
                          'printf %s "/opt/a:/opt/b"\n'
                          'printf %s __FR_PATH_END__\n'
                          'echo "trailing noise"\n')
    assert login_path.probe(shell=sh) == ["/opt/a", "/opt/b"]


def test_probe_falls_through_flag_sets(tmp_path):
    # Only the -ic form works; -ilc prints nothing, -lc exits non-zero.
    sh = _shell(tmp_path, 'case "$1" in\n'
                          '  -ilc) exit 0;;\n'
                          '  -lc) exit 3;;\n'
                          '  -ic) printf %s __FR_PATH_BEGIN__/x/bin__FR_PATH_END__;;\n'
                          'esac\n')
    assert login_path.probe(shell=sh) == ["/x/bin"]


def test_probe_none_on_timeout(tmp_path):
    sh = _shell(tmp_path, "sleep 5\n")
    t0 = time.monotonic()
    assert login_path.probe(shell=sh, timeout=0.3) is None
    assert time.monotonic() - t0 < 4


def test_probe_none_when_shell_not_executable(tmp_path):
    p = tmp_path / "notexec"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o644)
    assert login_path.probe(shell=str(p)) is None
    assert login_path.probe(shell=str(tmp_path / "missing")) is None


def test_probe_drops_relative_empty_and_dedupes(tmp_path):
    sh = _shell(tmp_path, 'printf %s "__FR_PATH_BEGIN__/a::rel/dir:./x:/b:/a:/b__FR_PATH_END__"\n')
    assert login_path.probe(shell=sh) == ["/a", "/b"]


def test_merge_current_first_then_new():
    assert login_path.merge("/app/bin:/usr/bin", ["/usr/bin", "/n/bin", "/app/bin"]) \
        == "/app/bin:/usr/bin:/n/bin"


def test_merge_empty_inputs():
    assert login_path.merge("", []) == ""
    assert login_path.merge("", ["/a"]) == "/a"
    assert login_path.merge("/a::/b", []) == "/a:/b"


def test_start_opt_out_leaves_path_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_NO_LOGIN_PATH", "1")
    monkeypatch.setenv("SHELL", _shell(tmp_path, "exit 1\n"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    login_path.start()
    t0 = time.monotonic()
    assert login_path.wait(5) is True
    assert time.monotonic() - t0 < 1
    assert os.environ["PATH"] == "/usr/bin:/bin"


def test_wait_without_start_returns_immediately():
    t0 = time.monotonic()
    assert login_path.wait(5) is True
    assert time.monotonic() - t0 < 1


def test_start_merges_fake_shell_dirs(monkeypatch, tmp_path):
    sh = _shell(tmp_path, 'printf %s "__FR_PATH_BEGIN__/fnm/bin:/usr/bin__FR_PATH_END__"\n')
    monkeypatch.delenv("FUSED_RENDER_NO_LOGIN_PATH", raising=False)
    monkeypatch.setenv("SHELL", sh)
    monkeypatch.setenv("PATH", "/app/bin:/usr/bin")
    login_path.start()
    login_path.start()  # idempotent
    assert login_path.wait(10) is True
    assert os.environ["PATH"] == "/app/bin:/usr/bin:/fnm/bin"

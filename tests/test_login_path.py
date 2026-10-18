"""login_path: merge the user's login-shell PATH into the server's environ.

REAL subprocesses against tiny fake shells in tmp_path — a faked
subprocess.run hid lifecycle bugs in this repo before.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix shells")

from fused_render import login_path  # noqa: E402


def _shell(tmp_path, body, name="fakesh"):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    h = tmp_path / "frhome"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    return h


def _cache_file():
    from fused_render.shell.storage import home_dir
    return os.path.join(home_dir(), "login-path.json")


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
    # /app/bin is not in login's list: it is the leading app-owned run and
    # stays first; login's own order follows.
    assert login_path.merge("/app/bin:/usr/bin", ["/usr/bin", "/n/bin"]) \
        == "/app/bin:/usr/bin:/n/bin"


def test_merge_version_manager_shims_beat_system_dirs():
    assert login_path.merge("/app/bin:/usr/bin:/bin",
                            ["/home/u/.pyenv/shims", "/usr/bin", "/bin"]) \
        == "/app/bin:/home/u/.pyenv/shims:/usr/bin:/bin"


def test_merge_current_entry_in_login_keeps_login_position():
    assert login_path.merge("/app/bin:/usr/bin:/x", ["/a", "/x", "/usr/bin"]) \
        == "/app/bin:/a:/x:/usr/bin"


def test_merge_extras_after_login():
    assert login_path.merge("/app/bin:/usr/bin:/extra", ["/a", "/usr/bin"]) \
        == "/app/bin:/a:/usr/bin:/extra"


def test_merge_idempotent():
    once = login_path.merge("/app/bin:/usr/bin:/bin:/extra", ["/p", "/usr/bin", "/bin"])
    assert login_path.merge(once, ["/p", "/usr/bin", "/bin"]) == once


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
    assert os.environ["PATH"] == "/app/bin:/fnm/bin:/usr/bin"


def test_spawn_env_waits_for_probe_and_keeps_homebrew(monkeypatch, tmp_path):
    import importlib.util
    agent_py = Path(__file__).resolve().parents[1] / "fused_render" / "claude_agent" / "agent.py"
    spec = importlib.util.spec_from_file_location("claude_agent_login_path", agent_py)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    sh = _shell(tmp_path, 'sleep 0.5\nprintf %s "__FR_PATH_BEGIN__/fnm/bin__FR_PATH_END__"\n')
    monkeypatch.delenv("FUSED_RENDER_NO_LOGIN_PATH", raising=False)
    monkeypatch.setenv("SHELL", sh)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    login_path.start()
    parts = agent._spawn_env("chat-1")["PATH"].split(os.pathsep)
    assert "/fnm/bin" in parts  # waited for the probe
    assert "/opt/homebrew/bin" in parts  # fallback still there
    assert parts.index("/usr/bin") < parts.index("/fnm/bin")


def test_probe_salvages_markers_when_background_process_holds_stdout(tmp_path):
    # An rc file that backgrounds a daemon inheriting stdout (ssh-agent,
    # zsh-async): the shell exits but the pipe stays open until the timeout.
    sh = _shell(tmp_path, 'printf %s "__FR_PATH_BEGIN__/held/bin__FR_PATH_END__"\n'
                          '( sleep 30 ) &\n'
                          'exit 0\n')
    t0 = time.monotonic()
    assert login_path.probe(shell=sh, timeout=1) == ["/held/bin"]
    assert time.monotonic() - t0 < 5


def test_probe_stops_after_first_timeout(tmp_path):
    sh = _shell(tmp_path, 'case "$1" in\n'
                          '  -ilc) sleep 30;;\n'
                          '  *) printf %s __FR_PATH_BEGIN__/x/bin__FR_PATH_END__;;\n'
                          'esac\n')
    t0 = time.monotonic()
    assert login_path.probe(shell=sh, timeout=1) is None
    assert time.monotonic() - t0 < 2 * 1 + 1.5


def test_user_shell_falls_back_to_account_shell(monkeypatch, tmp_path):
    import pwd
    fake = _shell(tmp_path, "exit 0\n", name="acctsh")
    monkeypatch.delenv("SHELL", raising=False)

    class _Pw:
        pw_shell = fake
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: _Pw())
    assert login_path._user_shell() == fake
    monkeypatch.setenv("SHELL", "")
    assert login_path._user_shell() == fake


def test_user_shell_prefers_env_then_bash(monkeypatch):
    import pwd
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert login_path._user_shell() == "/bin/zsh"
    monkeypatch.delenv("SHELL", raising=False)

    class _Pw:
        pw_shell = ""
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: _Pw())
    assert login_path._user_shell() == "/bin/bash"


def test_start_applies_cache_synchronously_before_slow_probe(monkeypatch, tmp_path):
    sh = _shell(tmp_path, 'sleep 1\nprintf %s "__FR_PATH_BEGIN__/fresh/bin:/usr/bin__FR_PATH_END__"\n')
    with open(_cache_file(), "w") as f:
        json.dump({"shell": sh, "path": ["/cached/bin", "/usr/bin"]}, f)
    monkeypatch.delenv("FUSED_RENDER_NO_LOGIN_PATH", raising=False)
    monkeypatch.setenv("SHELL", sh)
    monkeypatch.setenv("PATH", "/app/bin:/usr/bin")
    login_path.start()
    assert os.environ["PATH"] == "/app/bin:/cached/bin:/usr/bin"  # before wait()
    assert login_path.wait(10) is True
    assert os.environ["PATH"] == "/app/bin:/cached/bin:/fresh/bin:/usr/bin"  # re-merged on current
    with open(_cache_file()) as f:
        assert json.load(f) == {"shell": sh, "path": ["/fresh/bin", "/usr/bin"]}


def test_start_ignores_cache_for_other_shell(monkeypatch, tmp_path):
    sh = _shell(tmp_path, 'sleep 0.5\nprintf %s "__FR_PATH_BEGIN__/fresh__FR_PATH_END__"\n')
    with open(_cache_file(), "w") as f:
        json.dump({"shell": "/some/other/sh", "path": ["/cached/bin"]}, f)
    monkeypatch.delenv("FUSED_RENDER_NO_LOGIN_PATH", raising=False)
    monkeypatch.setenv("SHELL", sh)
    monkeypatch.setenv("PATH", "/usr/bin")
    login_path.start()
    assert "/cached/bin" not in os.environ["PATH"]
    login_path.wait(10)


def test_successful_probe_writes_cache(monkeypatch, tmp_path):
    sh = _shell(tmp_path, 'printf %s "__FR_PATH_BEGIN__/n/bin__FR_PATH_END__"\n')
    monkeypatch.delenv("FUSED_RENDER_NO_LOGIN_PATH", raising=False)
    monkeypatch.setenv("SHELL", sh)
    monkeypatch.setenv("PATH", "/usr/bin")
    login_path.start()
    assert login_path.wait(10) is True
    with open(_cache_file()) as f:
        assert json.load(f) == {"shell": sh, "path": ["/n/bin"]}


def test_corrupt_cache_is_ignored(monkeypatch, tmp_path):
    sh = _shell(tmp_path, 'printf %s "__FR_PATH_BEGIN__/n/bin__FR_PATH_END__"\n')
    with open(_cache_file(), "w") as f:
        f.write("{not json")
    monkeypatch.delenv("FUSED_RENDER_NO_LOGIN_PATH", raising=False)
    monkeypatch.setenv("SHELL", sh)
    monkeypatch.setenv("PATH", "/usr/bin")
    login_path.start()
    assert login_path.wait(10) is True
    assert os.environ["PATH"] == "/usr/bin:/n/bin"


def test_opt_out_touches_no_cache(monkeypatch, tmp_path):
    with open(_cache_file(), "w") as f:
        json.dump({"shell": "/bin/sh", "path": ["/cached"]}, f)
    monkeypatch.setenv("FUSED_RENDER_NO_LOGIN_PATH", "1")
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("PATH", "/usr/bin")
    login_path.start()
    assert os.environ["PATH"] == "/usr/bin"


def test_export_app_env_starts_probe_last_and_prepends_survive(monkeypatch, tmp_path):
    # Real behaviour: run export_app_env with the probe enabled against a fake
    # shell; the dirs it prepends itself must still lead PATH afterwards.
    from fused_render.server import app as server_app
    sh = _shell(tmp_path, 'sleep 0.5\nprintf %s "__FR_PATH_BEGIN__/fnm/bin:/usr/bin__FR_PATH_END__"\n')
    monkeypatch.delenv("FUSED_RENDER_NO_LOGIN_PATH", raising=False)
    monkeypatch.setenv("SHELL", sh)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    server_app.export_app_env()
    assert login_path.wait(10) is True
    parts = os.environ["PATH"].split(os.pathsep)
    fused_bin = os.path.join(os.environ["FUSED_RENDER_HOME"], "fused-bin")
    assert "/fnm/bin" in parts
    if fused_bin in parts:
        assert parts.index(fused_bin) < parts.index("/fnm/bin")
    src = Path(server_app.__file__).read_text()
    body = src.split("def export_app_env", 1)[1].split("\ndef ", 1)[0]
    assert body.rindex("login_path.start()") > body.rindex("_export_bundled_uv_path()")

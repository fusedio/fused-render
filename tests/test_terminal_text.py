"""Coverage for the "Claude can read the user's terminal" read path:
`pty_session.render_screen_text` (VT-emulated screen text), the live shell
integration state (OSC 133 / 7 / 633;E), and `GET /api/terminal/{sid}/text`.

Unix-only like the rest of the pty suite. Real-shell tests skip when the shell
binary is absent.
"""
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.name == "nt", reason="pty is unix-only")

if os.name != "nt":
    from fused_render import pty_session
    from fused_render.server import create_app
    from fused_render.shell_integration import ShellEventParser, integrate
    from fused_render.terminal_profiles import TerminalProfile

_HEADERS = {"X-Fused": "1"}


def _wait_until(predicate, timeout=8.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# -- render_screen_text -------------------------------------------------------

def test_carriage_return_progress_renders_the_final_state():
    out = pty_session.render_screen_text(
        b"progress 10%\rprogress 55%\rprogress 100%\r\n$ ", rows=24, cols=80, lines=50)
    assert out == "progress 100%\n$"


def test_cursor_moves_and_erase_are_applied():
    # Write XXXX, move left 2, overwrite with "ab" -> "XXab"; then erase line.
    out = pty_session.render_screen_text(
        b"XXXX\x1b[2Dab\r\nsecond\x1b[2K\rthird\r\n", rows=24, cols=80, lines=50)
    assert out == "XXab\nthird"


def test_history_plus_screen_and_last_n_lines():
    data = b"".join(b"line %d\r\n" % i for i in range(100))
    out = pty_session.render_screen_text(data, rows=10, cols=40, lines=5)
    assert out.splitlines() == [f"line {i}" for i in range(95, 100)]
    # More lines than the screen holds are reachable through the history.
    big = pty_session.render_screen_text(data, rows=10, cols=40, lines=2000)
    assert big.splitlines()[0] == "line 0"
    assert big.splitlines()[-1] == "line 99"


def test_ansi_colour_is_dropped_and_trailing_blank_lines_trimmed():
    out = pty_session.render_screen_text(b"\x1b[31mred\x1b[0m text\r\n\r\n\r\n", rows=5, cols=40, lines=50)
    assert out == "red text"


def test_empty_scrollback_is_empty_text():
    assert pty_session.render_screen_text(b"", rows=24, cols=80, lines=10) == ""


# -- ShellEventParser ---------------------------------------------------------

def test_parser_tracks_command_exit_and_cwd():
    p = ShellEventParser()
    p.feed(b"\x1b]133;D;0\x07\x1b]7;file://host/tmp/a%20b\x07\x1b]133;A\x07$ ")
    assert p.last_command is None and p.last_exit is None
    assert p.cwd == "/tmp/a b"
    p.feed(b"\x1b]633;E;false \\x3b echo hi\x07\x1b]133;C\x07")
    assert p.last_command == "false ; echo hi"
    assert p.running is True
    p.feed(b"\x1b]133;D;1\x07")
    assert p.last_exit == 1 and p.running is False


def test_parser_handles_sequences_split_across_chunks():
    p = ShellEventParser()
    stream = b"\x1b]633;E;ls -l\x07\x1b]133;C\x07out\x1b]133;D;7\x1b\\"
    for i in range(len(stream)):
        p.feed(stream[i:i + 1])
    assert p.last_command == "ls -l"
    assert p.last_exit == 7


def test_parser_ignores_unknown_osc_and_garbage():
    p = ShellEventParser()
    p.feed(b"\x1b]0;window title\x07\x1b]999;zzz\x07\x1b]")
    p.feed(b"plain text \x1b[31m")
    assert p.last_command is None and p.cwd is None


# -- integrate() --------------------------------------------------------------

def test_integrate_leaves_unknown_shells_alone():
    prof = TerminalProfile(shell="/bin/sh", argv=["/bin/sh"], env={"A": "1"}, cwd="/tmp")
    assert integrate(prof) is prof


def test_integrate_zsh_points_zdotdir_at_a_shim_and_keeps_the_users():
    prof = TerminalProfile(shell="/bin/zsh", argv=["/bin/zsh", "-l"],
                           env={"ZDOTDIR": "/custom"}, cwd="/tmp")
    out = integrate(prof)
    assert out.argv == ["/bin/zsh", "-l"]
    assert out.env["FUSED_USER_ZDOTDIR"] == "/custom"
    assert os.path.isfile(os.path.join(out.env["ZDOTDIR"], ".zshrc"))
    assert out.env["ZDOTDIR"] == out.env["FUSED_SHIM_DIR"]


def test_integrate_bash_uses_init_file_and_login_flag_env():
    prof = TerminalProfile(shell="/bin/bash", argv=["/bin/bash", "-l"], env={}, cwd="/tmp")
    out = integrate(prof)
    assert "-l" not in out.argv and "--init-file" in out.argv
    assert out.env["FUSED_SHELL_LOGIN"] == "1"
    assert os.path.isfile(out.argv[out.argv.index("--init-file") + 1])


# -- real shells --------------------------------------------------------------

def _session_for(shell, tmp_path, monkeypatch, home=None, login=False):
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} not installed")
    home = home or tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "TERM": "xterm-256color"}
    prof = integrate(TerminalProfile(shell=exe, argv=[exe, "-l"] if login else [exe], env=env,
                                        cwd=str(tmp_path)))
    monkeypatch.setattr(pty_session, "resolve_profile", lambda cwd=None: prof)
    reg = pty_session.PtySessionRegistry()
    session = reg.create()
    return reg, session


@pytest.mark.parametrize("shell", ["zsh", "bash"])
def test_real_shell_reports_command_exit_and_cwd(shell, tmp_path, monkeypatch):
    reg, s = _session_for(shell, tmp_path, monkeypatch)
    try:
        sub = tmp_path / "sub dir"
        sub.mkdir()
        assert _wait_until(lambda: s.shell_state.cwd is not None and s.shell_is_foreground())
        s.write(b"false\n")
        assert _wait_until(lambda: s.shell_state.last_exit == 1), s.scrollback()
        assert s.shell_state.last_command == "false"
        s.write(f"cd '{sub}'\n".encode())
        assert _wait_until(lambda: s.shell_state.cwd == os.path.realpath(str(sub))
                           or s.shell_state.cwd == str(sub)), s.shell_state.cwd
        s.write(b"true\n")
        assert _wait_until(lambda: s.shell_state.last_command == "true"
                           and s.shell_state.last_exit == 0)
    finally:
        reg.shutdown_all()


@pytest.mark.parametrize("shell", ["zsh", "bash"])
def test_real_shell_sources_the_users_rc_files(shell, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    rc = ".zshrc" if shell == "zsh" else ".bashrc"
    (home / rc).write_text("export FR_RC_MARK=loaded-from-user-rc\n")
    reg, s = _session_for(shell, tmp_path, monkeypatch, home=home)
    try:
        assert _wait_until(lambda: s.shell_is_foreground() and s.shell_state.cwd is not None)
        s.write(b"echo mark=$FR_RC_MARK\n")
        assert _wait_until(lambda: b"mark=loaded-from-user-rc" in s.scrollback())
    finally:
        reg.shutdown_all()


def test_zsh_without_user_zdotdir_still_finds_zlogin_in_home(tmp_path, monkeypatch):
    # No ZDOTDIR, no ~/.zshenv, no ~/.zshrc: the shim must unset ZDOTDIR (not
    # leave it empty, which makes zsh look for .zlogin in "/").
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zlogin").write_text("export FR_ZLOGIN_MARK=zlogin-ran\n")
    reg, s = _session_for("zsh", tmp_path, monkeypatch, home=home, login=True)
    try:
        assert _wait_until(lambda: s.shell_is_foreground() and s.shell_state.cwd is not None)
        s.write(b"echo \"mark=$FR_ZLOGIN_MARK zd=[${ZDOTDIR-unset}]\"\n")
        assert _wait_until(lambda: b"mark=zlogin-ran zd=[unset]" in s.scrollback()), s.scrollback()[-400:]
    finally:
        reg.shutdown_all()


def test_bash_unsaved_command_is_not_reported_as_the_previous_one(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("HISTCONTROL=ignorespace\n")
    reg, s = _session_for("bash", tmp_path, monkeypatch, home=home)
    try:
        assert _wait_until(lambda: s.shell_is_foreground() and s.shell_state.cwd is not None)
        s.write(b"echo first\n")
        assert _wait_until(lambda: s.shell_state.last_command == "echo first")
        s.write(b" echo second\n")
        assert _wait_until(lambda: s.shell_state.last_command is not None
                           and s.shell_state.last_command.strip() == "echo second"), \
            s.shell_state.last_command
    finally:
        reg.shutdown_all()


# -- /text route + list fields ------------------------------------------------

@pytest.fixture
def scratch_registry(monkeypatch, tmp_path):
    reg = pty_session.PtySessionRegistry()
    monkeypatch.setattr(pty_session, "REGISTRY", reg)
    monkeypatch.setattr(
        pty_session, "resolve_profile",
        lambda cwd=None: TerminalProfile(
            shell="/bin/sh", argv=["/bin/sh"], env=dict(os.environ), cwd=str(tmp_path)))
    yield reg
    reg.shutdown_all()


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_text_route_returns_screen_text_and_metadata(client, scratch_registry, tmp_path):
    sid = client.post("/api/terminal", headers=_HEADERS, json={}).json()["id"]
    s = scratch_registry.get(sid)
    assert _wait_until(lambda: s.shell_is_foreground())
    # `$((1+1))` so the marker only exists in the OUTPUT, never in the echoed
    # command line (which would satisfy the wait before the command ran).
    s.write(b"printf 'abc\\rXY\\n'; echo done-$((1+1))\n")
    assert _wait_until(lambda: b"done-2" in s.scrollback())
    assert _wait_until(lambda: s.shell_is_foreground())
    body = client.get(f"/api/terminal/{sid}/text").json()
    assert body["id"] == sid
    assert "XYc" in body["text"]
    assert "done-2" in body["text"]
    assert body["alive"] is True
    assert body["foreground"] is None
    assert body["cwd"] == str(tmp_path)
    assert set(body) >= {"lastCommand", "lastExit", "lastActivity", "exitCode"}


def test_text_route_lines_param_clamps_and_limits(client, scratch_registry):
    sid = client.post("/api/terminal", headers=_HEADERS, json={}).json()["id"]
    s = scratch_registry.get(sid)
    assert _wait_until(lambda: s.shell_is_foreground())
    s.write(b"for i in 1 2 3 4 5 6 7 8; do echo row$i; done\n")
    assert _wait_until(lambda: b"row8" in s.scrollback().replace(b"echo", b""))
    one = client.get(f"/api/terminal/{sid}/text?lines=1").json()["text"]
    assert len(one.splitlines()) == 1
    assert client.get(f"/api/terminal/{sid}/text?lines=0").status_code == 200
    assert client.get(f"/api/terminal/{sid}/text?lines=999999").status_code == 200


def test_text_route_404_for_unknown_session(client, scratch_registry):
    assert client.get("/api/terminal/nope/text").status_code == 404


def test_text_route_names_the_foreground_program(client, scratch_registry):
    sid = client.post("/api/terminal", headers=_HEADERS, json={}).json()["id"]
    s = scratch_registry.get(sid)
    assert _wait_until(lambda: s.shell_is_foreground())
    s.write(b"sleep 30\n")
    assert _wait_until(lambda: not s.shell_is_foreground())
    body = client.get(f"/api/terminal/{sid}/text").json()
    assert body["foreground"] == "sleep"
    s.write(b"\x03")


def test_list_entries_carry_the_new_fields(client, scratch_registry):
    client.post("/api/terminal", headers=_HEADERS, json={})
    entry = client.get("/api/terminal").json()["sessions"][0]
    assert {"foreground", "lastCommand", "lastExit", "lastActivity"} <= set(entry)
    assert entry["lastCommand"] is None and entry["foreground"] is None

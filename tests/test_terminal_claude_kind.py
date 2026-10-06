"""The read-only "claude" session kind on /api/terminal: listed, streamed over
the same WS protocol from the wrapper's per-chat log, readable as text, and
stoppable (D1326)."""
import json
import os
import shlex
import subprocess
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_cmd_log, pty_session
from fused_render.server import create_app

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX only")

H = {"X-Fused": "1"}
WRAPPER = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                       "fused_render", "claude_shell_prefix.sh"))

_SNAPDIR = tempfile.mkdtemp(prefix="fr-snap-")
os.makedirs(os.path.join(_SNAPDIR, "shell-snapshots"))
SNAP = os.path.join(_SNAPDIR, "shell-snapshots", "snapshot-zsh-1-a.sh")
open(SNAP, "w").close()


def wrapped(user_cmd, cwdfile):
    return ("source " + SNAP + " 2>/dev/null || true && eval " + shlex.quote(user_cmd)
            + " < /dev/null && pwd -P >| " + cwdfile)


@pytest.fixture
def logroot(monkeypatch, tmp_path):
    root = tmp_path / "claude-cmds"
    root.mkdir()
    monkeypatch.setattr(claude_cmd_log, "root", lambda: str(root))
    monkeypatch.setattr(pty_session, "REGISTRY", pty_session.PtySessionRegistry())
    return root


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def fake_cmd(root, chat, ident, user_cmd, out=b"", code=None, pid=None):
    d = root / chat
    d.mkdir(exist_ok=True)
    raw = wrapped(user_cmd, "/tmp/x-cwd")
    (d / (ident + ".cmd")).write_text(raw)
    (d / (ident + ".meta")).write_text("pid=%d\npgid=1\nstart=1\n" % (pid or os.getpid()))
    (d / (ident + ".out")).write_bytes(out)
    if code is not None:
        (d / (ident + ".exit")).write_text("%d\n" % code)


def test_readable_command_unquotes_eval():
    raw = wrapped("echo 'it'\"'\"'s' && ls", "/tmp/c-cwd")
    assert claude_cmd_log.readable_command(raw) == "echo 'it'\"'\"'s' && ls"
    assert claude_cmd_log.readable_command("plain") == "plain"
    assert claude_cmd_log.readable_command(
        "source x || true && eval pwd < /dev/null && pwd -P >| /t/c-cwd") == "pwd"


def test_list_has_claude_entry_with_kind_and_chat(client, logroot):
    fake_cmd(logroot, "chat-a", "00000000001-1", "ls -la", b"total 0\n", code=0)
    sessions = client.get("/api/terminal").json()["sessions"]
    (entry,) = [s for s in sessions if s.get("kind") == "claude"]
    assert entry["id"] == "claude:chat-a" and entry["chat"] == "chat-a"
    assert entry["alive"] is True and entry["lastCommand"] == "ls -la"


def test_list_empty_without_logs(client, logroot):
    assert client.get("/api/terminal").json()["sessions"] == []


def test_stream_replays_header_output_and_nonzero_exit(client, logroot):
    fake_cmd(logroot, "c1", "00000000001-1", "make test", b"building\nboom\n", code=2)
    with client.websocket_connect("/api/terminal/claude:c1/stream") as ws:
        data = ws.receive_bytes()
    text = data.decode()
    assert "$ make test" in text
    assert "building\r\nboom\r\n" in text
    assert "[exit 2]" in text


def test_zero_exit_shows_no_exit_line(client, logroot):
    fake_cmd(logroot, "c1", "00000000001-1", "true", b"ok\n", code=0)
    with client.websocket_connect("/api/terminal/claude:c1/stream") as ws:
        text = ws.receive_bytes().decode()
    assert "[exit" not in text


def test_stream_follows_new_output_and_commands(client, logroot):
    fake_cmd(logroot, "c1", "00000000001-1", "sleep 5", b"one\n")  # running (our pid)
    with client.websocket_connect("/api/terminal/claude:c1/stream") as ws:
        first = ws.receive_bytes().decode()
        assert "$ sleep 5" in first and "one" in first
        with open(logroot / "c1" / "00000000001-1.out", "ab") as f:
            f.write(b"two\n")
        seen = ""
        for _ in range(50):
            seen += ws.receive_bytes().decode()
            if "two" in seen:
                break
        assert "two" in seen and "one" not in seen  # appended, not re-sent
        fake_cmd(logroot, "c1", "00000000002-1", "echo next", b"next\n", code=0)
        seen = ""
        for _ in range(50):
            seen += ws.receive_bytes().decode()
            if "next" in seen:
                break
        assert "$ echo next" in seen


def test_input_frames_are_ignored(client, logroot):
    fake_cmd(logroot, "c1", "00000000001-1", "true", b"x\n", code=0)
    with client.websocket_connect("/api/terminal/claude:c1/stream") as ws:
        ws.receive_bytes()
        ws.send_bytes(b"rm -rf /\n")
        ws.send_text(json.dumps({"resize": [10, 10]}))
    # nothing blew up and the log is untouched
    assert (logroot / "c1" / "00000000001-1.out").read_bytes() == b"x\n"


def test_unknown_claude_chat_gets_exit_null(client, logroot):
    with client.websocket_connect("/api/terminal/claude:nope/stream") as ws:
        assert json.loads(ws.receive_text()) == {"exit": None}


def test_text_endpoint_strips_ansi(client, logroot):
    fake_cmd(logroot, "c1", "00000000001-1", "echo hi", b"hi\n", code=0)
    body = client.get("/api/terminal/claude:c1/text").json()
    assert body["text"].splitlines() == ["$ echo hi", "hi"]
    assert client.get("/api/terminal/claude:zzz/text").status_code == 404


def test_input_and_delete_refused_for_claude_tab(client, logroot):
    fake_cmd(logroot, "c1", "00000000001-1", "true", b"", code=0)
    assert client.post("/api/terminal/claude:c1/input", json={"data": "x"},
                       headers=H).status_code == 404
    assert client.delete("/api/terminal/claude:c1", headers=H).status_code == 400


def test_stop_requires_guard_and_known_chat(client, logroot):
    assert client.post("/api/terminal/claude:c1/stop").status_code in (400, 403)
    assert client.post("/api/terminal/claude:none/stop", headers=H).status_code == 404


def test_stop_kills_running_command_tree(client, logroot, tmp_path):
    chat = "c-stop"
    env = dict(os.environ, SHELL="/bin/sh", FUSED_CLAUDE_CMD_LOG=str(logroot / chat))
    p = subprocess.Popen([WRAPPER, wrapped("sleep 60", str(tmp_path / "s-cwd"))],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            d = logroot / chat
            if d.is_dir() and any(n.endswith(".meta") for n in os.listdir(d)):
                break
            time.sleep(0.05)
        time.sleep(0.5)
        (entry,) = [s for s in client.get("/api/terminal").json()["sessions"]
                    if s.get("kind") == "claude"]
        assert entry["running"] is True
        r = client.post("/api/terminal/claude:%s/stop" % chat, headers=H)
        assert r.status_code == 200 and r.json()["stopped"] == 1
        assert p.wait(timeout=15) != 0
        (ident,) = {n.split(".")[0] for n in os.listdir(logroot / chat)
                    if n.endswith(".exit")}
        assert (logroot / chat / (ident + ".exit")).read_text().strip() != "0"
        with client.websocket_connect("/api/terminal/claude:%s/stream" % chat) as ws:
            assert "[exit" in ws.receive_bytes().decode()
    finally:
        if p.poll() is None:
            p.kill()


@pytest.mark.parametrize("call", [lambda: claude_cmd_log._descendants(1),
                                  lambda: claude_cmd_log._is_wrapper(1)])
def test_ps_spawns_are_posix_spawn_safe(monkeypatch, call):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(claude_cmd_log.subprocess, "run", fake_run)
    call()
    assert os.path.isabs(seen["argv"][0])
    assert seen["kw"].get("close_fds") is False
    assert "cwd" not in seen["kw"]


def test_pid_zero_without_exit_counts_as_running(logroot):
    # A .cmd with no readable .meta (older layout) must not look ended.
    d = logroot / "c1"
    d.mkdir()
    (d / "00000000001-1.cmd").write_text(wrapped("sleep 5", "/tmp/x-cwd"))
    (d / "00000000001-1.out").write_bytes(b"partial\n")
    cmd = claude_cmd_log.Cmd("c1", "00000000001-1")
    assert cmd.pid == 0 and cmd.running
    out = claude_cmd_log.Stream("c1").poll()
    assert b"partial" in out and b"[ended]" not in out
    (d / "00000000001-1.exit").write_text("0\n")
    assert not claude_cmd_log.Cmd("c1", "00000000001-1").running


def test_stream_poll_reads_only_new_bytes(logroot):
    fake_cmd(logroot, "c1", "00000000001-1", "sleep 5", b"x" * 1_000_000)  # running
    s = claude_cmd_log.Stream("c1")
    first = s.poll()
    assert first.count(b"x") == 1_000_000
    with open(logroot / "c1" / "00000000001-1.out", "ab") as f:
        f.write(b"0123456789")
    assert s.poll() == b"0123456789"
    assert s.poll() == b""

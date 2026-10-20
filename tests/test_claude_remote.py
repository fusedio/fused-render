import os
import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_agent, claude_health, claude_remote, claude_spawn, pty_session
from fused_render.server import create_app

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PTY only")

SID = "11111111-2222-3333-4444-555555555555"
URL = "https://claude.ai/code/session_TEST123"

OK_CLI = """#!%s
import os, sys, time, tty
open(sys.argv[0] + ".argv", "w").write(" ".join(sys.argv[1:]))
tty.setraw(0)
yes = False
def draw():
    sys.stdout.write(("  No, exit\\r\\n\u276f Yes, I trust this folder" if yes else
                      "\u276f No, exit\\r\\n  Yes, I trust this folder") + "\\r\\n")
    sys.stdout.flush()
draw()
while True:
    key = os.read(0, 8)
    if key == b"\\x1b[B":
        yes = True
        draw()
    elif key == b"\\r":
        break
if not yes:
    sys.exit(1)
print("/remote-control is active - Continue at %s", flush=True)
time.sleep(30)
""" % (sys.executable, URL)

DEAD_CLI = """#!/bin/sh
echo "boom: no remote control here"
exit 3
"""


def _cli(tmp_path, body):
    path = tmp_path / "fake-claude"
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    reg = pty_session.PtySessionRegistry()
    monkeypatch.setattr(pty_session, "REGISTRY", reg)
    monkeypatch.setattr(claude_remote, "_held", {})
    state = {"bin": "", "live": {}, "turn": (False, False), "backend": "claude",
             "killed": []}
    agent = types.SimpleNamespace(
        main=lambda action="", session_id="", file="": {"ok": True},
        RUNS=str(tmp_path),
        _bad_id=lambda sid: "/" in sid or ".." in sid,
        _workdir=lambda file: str(tmp_path),
        _session_backend=lambda sid: "claude",
        _backend_for_session=lambda sid, workdir, hint=None: state["backend"],
        _claude_bin=lambda: state["bin"],
        _plugin_argv=lambda file: [],
        _spawn_env=lambda *a: dict(os.environ),
        _fused_cli_dir=lambda: "",
        _live_host=lambda file, sid: state["live"],
        _turn_state=lambda path: state["turn"],
        _kill_tree=lambda run_dir: state["killed"].append(os.path.basename(run_dir)),
        _host_alive=lambda run_dir: False,
        _inbox_dir=lambda run_dir: os.path.join(run_dir, "inbox"),
    )
    monkeypatch.setattr(claude_agent, "agent_module", lambda: agent)
    monkeypatch.setattr(claude_health, "_auth_status",
                        lambda binary: {"loggedIn": True, "authMethod": "claude.ai"})
    state["file"] = str(tmp_path / "page.html")
    state["reg"] = reg
    yield state
    reg.shutdown_all()


def _poll(until, timeout=10):
    deadline = time.time() + timeout
    while True:
        out = claude_remote.status(SID)
        if until(out) or time.time() > deadline:
            return out
        time.sleep(0.05)


def test_start_trust_prompt_url_hold_and_stop(env, tmp_path):
    env["bin"] = _cli(tmp_path, OK_CLI)
    env["live"] = {"run_id": "run1"}
    assert claude_remote.start(env["file"], SID, "my chat")["state"] == "starting"
    out = _poll(lambda s: s["state"] == "active")
    assert out["state"] == "active" and out["url"] == URL
    assert env["killed"] == ["run1"]
    argv = open(env["bin"] + ".argv").read().split("\n")[0]
    assert argv == f"--resume {SID} --remote-control my chat"
    assert claude_remote.holds(SID)
    assert claude_spawn.spawn_helper(env["file"], "hi", "default", session_id=SID) == {"error": claude_remote.HELD}

    client = TestClient(create_app(start_dir=str(tmp_path)))
    resp = client.post("/api/claude/agent", headers={"X-Fused": "1"},
                       json={"action": "start", "session_id": SID, "file": env["file"]})
    assert resp.json() == {"error": claude_remote.HELD}
    resp = client.post("/api/claude/remote-control", headers={"X-Fused": "1"},
                       json={"action": "status", "session_id": SID})
    assert resp.json()["url"] == URL

    term = claude_remote._held[SID]["term"]
    assert claude_remote.stop(SID) == {"state": "off"}
    assert not claude_remote.holds(SID)
    assert claude_remote.status(SID) == {"state": "off"}
    deadline = time.time() + 5
    while term.alive and time.time() < deadline:
        time.sleep(0.05)
    assert not term.alive


def test_cli_exit_reports_ended_once(env, tmp_path):
    env["bin"] = _cli(tmp_path, DEAD_CLI)
    claude_remote.start(env["file"], SID)
    out = _poll(lambda s: s["state"] == "ended")
    assert out["state"] == "ended" and out["exit_code"] == 3
    assert "boom: no remote control here" in out["tail"]
    assert claude_remote.status(SID) == {"state": "off"}
    assert not claude_remote.holds(SID)


@pytest.mark.parametrize("case", ["logged_out", "api_key", "copilot", "bad_id", "busy", "inbox"])
def test_refusals_spawn_nothing(env, tmp_path, monkeypatch, case):
    env["bin"] = _cli(tmp_path, OK_CLI)
    sid = SID
    if case == "logged_out":
        monkeypatch.setattr(claude_health, "_auth_status",
                            lambda b: {"loggedIn": False})
    elif case == "api_key":
        monkeypatch.setattr(claude_health, "_auth_status",
                            lambda b: {"loggedIn": True, "authMethod": "api_key"})
    elif case == "copilot":
        env["backend"] = "copilot"
    elif case == "bad_id":
        sid = "../x"
    elif case == "busy":
        env["live"] = {"run_id": "run1"}
        env["turn"] = (True, False)
    else:
        env["live"] = {"run_id": "run1"}
        (tmp_path / "run1" / "inbox").mkdir(parents=True)
        (tmp_path / "run1" / "inbox" / "1.json").write_text("{}")
    out = claude_remote.start(env["file"], sid)
    assert set(out) == {"error"} and out["error"]
    if case in ("busy", "inbox"):
        assert out["error"] == claude_remote.BUSY
        assert env["killed"] == []
    assert not claude_remote._held
    assert not env["reg"].list()
    assert not os.path.exists(env["bin"] + ".argv")

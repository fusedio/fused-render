"""Persistent claude sessions: the FIFO-backed process that survives its own
turn, and the two new actions built on top of it (`steer`, and `_cancel`'s new
interrupt branch).

Background — see D496 for the full probe writeup. The stock `claude -p` CLI
has no true mid-turn injection: a message written to a live process's stdin
while a turn is running is QUEUED BY THE CLI ITSELF and starts as its own
separate turn the moment the current one's `result` row lands. What IS real
is (1) the process stays alive across turns as long as its stdin never hits
EOF, and (2) the control-protocol `interrupt` request truncates the running
turn without ending the process. Both are exercised here against a REAL
subprocess (`cat`, standing in for `claude`) rather than a fake Popen,
because the whole point under test is the FIFO plumbing itself — the fake
Popen used elsewhere in this template's tests (see `test_claude_app_state.py`
`_spawn`) never dupes a fd to a child, so it cannot prove a FIFO reader
survives.

Everything here is POSIX-only, matching the feature: `os.mkfifo` does not
exist on Windows, and `test_claude_agent_windows.py` pins that platform's
one-shot path untouched.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

if os.name == "nt":
    pytest.skip("persistent sessions are POSIX-only", allow_module_level=True)

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_persistent_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


def _spawn_fake_claude(agent, run_dir):
    """A REAL process standing in for a persistent `claude`: `cat` with its
    stdin wired to the run's FIFO (so it never sees EOF, exactly like the
    real CLI would not) and its stdout going to a file this test can read
    back to see what actually reached "claude"'s input."""
    fifo_path = os.path.join(run_dir, "stdin.fifo")
    os.mkfifo(fifo_path, 0o600)
    out_path = os.path.join(run_dir, "echoed.txt")
    fifo_fd = os.open(fifo_path, os.O_RDWR)
    out_fh = open(out_path, "wb")
    proc = subprocess.Popen(["cat"], stdin=fifo_fd, stdout=out_fh,
                            stderr=subprocess.DEVNULL)
    os.close(fifo_fd)
    out_fh.close()
    with agent._private_open(os.path.join(run_dir, "pid")) as f:
        f.write(str(proc.pid))
    return proc, out_path


def _read(path, tries=20, delay=0.1):
    for _ in range(tries):
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.read()
        if data:
            return data
        time.sleep(delay)
    return ""


# ------------------------------------------------------------- the flag

def test_persistent_is_off_by_default(agent, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_CLAUDE_PERSISTENT", raising=False)
    assert agent._persistent_ok() is False


def test_persistent_turns_on_via_env_var(agent, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_PERSISTENT", "1")
    assert agent._persistent_ok() is True


def test_persistent_is_never_on_for_windows_even_with_the_env_var(agent, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_PERSISTENT", "1")
    monkeypatch.setattr(agent.os, "name", "nt")
    assert agent._persistent_ok() is False


# ------------------------------------------------------------- _start

def _spawn(agent, monkeypatch, target, message="hi", persistent=True):
    """Run `_start` against a fake Popen (argv/spawn-shape only — no FIFO
    reader is attached, which is fine for the assertions below: `_start`
    itself never blocks on one, see its own O_RDWR comment)."""
    seen = {}

    class _Proc:
        pid = 4242

    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent, "_persistent_ok", lambda: persistent)
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: (seen.__setitem__("cmd", cmd),
                                          seen.__setitem__("kw", kw), _Proc())[2])
    out = agent._start(str(target), message, "", "", "")
    assert "error" not in out, out
    return seen["cmd"], seen["kw"], os.path.join(agent.RUNS, out["run_id"])


def test_a_persistent_start_creates_a_fifo_and_marks_meta(agent, tmp_path,
                                                          monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "f.py"
    target.write_text("x = 1")
    cmd, kw, run_dir = _spawn(agent, monkeypatch, target, message="hello there")

    fifo_path = os.path.join(run_dir, "stdin.fifo")
    assert os.path.exists(fifo_path), "no FIFO — the persistent path never ran"
    assert "--input-format" in cmd and "stream-json" in cmd
    # The message must never land in argv for a persistent run any more than
    # it does for the existing message_via_stdin path — it goes down the FIFO.
    assert "hello there" not in cmd

    meta = json.load(open(os.path.join(run_dir, "meta.json"), encoding="utf-8"))
    assert meta["persistent"] is True


def test_a_non_persistent_start_leaves_the_one_shot_path_untouched(agent, tmp_path,
                                                                    monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "f.py"
    target.write_text("x = 1")
    cmd, kw, run_dir = _spawn(agent, monkeypatch, target, message="hello there",
                              persistent=False)

    assert not os.path.exists(os.path.join(run_dir, "stdin.fifo"))
    # The historical shape: message on argv, no --input-format at all.
    assert "hello there" in cmd
    assert "--input-format" not in cmd
    meta = json.load(open(os.path.join(run_dir, "meta.json"), encoding="utf-8"))
    assert meta["persistent"] is False


# ------------------------------------------------------------- steer

def test_steer_writes_a_user_line_into_a_live_run(agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    proc, out_path = _spawn_fake_claude(agent, run_dir)
    try:
        result = agent._steer("run1", "second message")
        assert result == {"steered": True}
        data = _read(out_path)
        row = json.loads(data.strip().splitlines()[0])
        assert row["type"] == "user"
        assert row["message"]["content"][0]["text"] == "second message"
        # The whole point: writing into the FIFO must not close it out from
        # under the still-running process.
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_steer_refuses_a_run_with_no_fifo(agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    with agent._private_open(os.path.join(run_dir, "pid")) as f:
        f.write("999999")  # doesn't need to be alive, the fifo check wins first
    result = agent._steer("run1", "hi")
    assert result["steered"] is False
    assert "not persistent" in result["error"]


def test_steer_refuses_a_dead_run(agent, tmp_path, monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    os.mkfifo(os.path.join(run_dir, "stdin.fifo"), 0o600)
    monkeypatch.setattr(agent, "_alive", lambda _d: False)
    result = agent._steer("run1", "hi")
    assert result["steered"] is False
    assert "ended" in result["error"]


def test_steer_refuses_an_unknown_run(agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    result = agent._steer("no-such-run", "hi")
    assert result["steered"] is False


# ------------------------------------------------------------- cancel/interrupt

def test_cancel_interrupts_a_persistent_run_instead_of_killing_it(agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    agent._private_dir(agent._perm_dir(run_dir))
    proc, out_path = _spawn_fake_claude(agent, run_dir)
    try:
        result = agent._cancel("run1")
        assert result == {"cancelled": "run1"}
        # The marker is written exactly as it always was — every existing
        # reader (`_poll`'s "cancelled" field, `_history`'s _stopped_last)
        # keeps working with no change on their end.
        assert os.path.exists(os.path.join(run_dir, "cancelled"))
        # ...but the process itself is NOT dead: a persistent session's stop
        # button ends the TURN, not the SESSION.
        time.sleep(0.2)
        assert proc.poll() is None, "an interrupt must not kill the process"
        data = _read(out_path)
        row = json.loads(data.strip().splitlines()[0])
        assert row["type"] == "control_request"
        assert row["request"]["subtype"] == "interrupt"
    finally:
        proc.kill()
        proc.wait()


def test_cancel_still_kills_a_one_shot_run(agent, tmp_path):
    """No FIFO at all (persistence off, or Windows) -> the historical kill
    path, proven here against a REAL process rather than the monkeypatched
    `_alive` the existing test_claude_stop_run.py tests use."""
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    agent._private_dir(agent._perm_dir(run_dir))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=True)
    with agent._private_open(os.path.join(run_dir, "pid")) as f:
        f.write(str(proc.pid))

    agent._cancel("run1")
    proc.wait(timeout=5)
    assert proc.returncode is not None


# ------------------------------------------------------------- poll additions

def test_poll_reports_the_completed_turn_count_and_persistence(agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    with agent._private_open(os.path.join(run_dir, "meta.json")) as f:
        json.dump({"file": "x", "message": "hi", "persistent": True}, f)
    rows = [
        {"type": "result", "session_id": "s1", "result": "one"},
        {"type": "system", "session_id": "s1"},
        {"type": "result", "session_id": "s1", "result": "two"},
    ]
    with agent._private_open(os.path.join(run_dir, "out.jsonl")) as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    with agent._private_open(os.path.join(run_dir, "pid")) as f:
        f.write("999999999")  # not alive -> done regardless

    out = agent._poll("run1")
    assert out["turn"] == 2
    assert out["persistent"] is True


def test_poll_reports_persistent_false_and_zero_turns_for_a_fresh_one_shot_run(
        agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    with agent._private_open(os.path.join(run_dir, "meta.json")) as f:
        json.dump({"file": "x", "message": "hi"}, f)
    with agent._private_open(os.path.join(run_dir, "out.jsonl")) as f:
        f.write("")
    with agent._private_open(os.path.join(run_dir, "pid")) as f:
        f.write("999999999")

    out = agent._poll("run1")
    assert out["turn"] == 0
    assert out["persistent"] is False


# ------------------------------------------------------------- main() dispatch

def test_main_dispatches_the_steer_action(agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    out = agent.main(action="steer", run_id="no-such-run", message="hi")
    assert out["steered"] is False

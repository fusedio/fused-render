"""Persistent claude sessions: the FIFO-backed process that survives its own
turn, and the two new actions built on top of it (`steer`, and `_cancel`'s new
interrupt branch).

Background — see D497 for the full probe writeup. The stock `claude -p` CLI
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
                            stderr=subprocess.DEVNULL,
                            # matches the real spawn (_DETACH):
                            # `_kill_process` uses os.killpg, which
                            # needs pid to be a process GROUP leader.
                            start_new_session=True)
    os.close(fifo_fd)
    out_fh.close()
    with agent._private_open(os.path.join(run_dir, "pid")) as f:
        f.write(str(proc.pid))
    return proc, out_path


_FAKE_CLI = r"""
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if row.get("type") == "control_request":
        # A real `claude` interrupted mid-turn writes a `result` row and a
        # control_response for the request it just honoured. Order doesn't
        # matter to `_wait_for_turn_end` (it only counts `result` rows), but
        # both are written for realism.
        print(json.dumps({"type": "result", "result": None, "is_error": True}), flush=True)
        print(json.dumps({"type": "control_response",
                          "response": {"subtype": "success",
                                       "request_id": row["request_id"]}}), flush=True)
"""


def _spawn_responsive_fake_claude(agent, run_dir):
    """A fake `claude` that actually HONOURS an interrupt — writes a `result`
    row the moment one arrives — standing in for the confirmed-stop half of
    `_cancel`'s escalation logic. `_spawn_fake_claude`'s `cat` cannot do this
    (it only echoes), which is exactly right for testing the OTHER half:
    an interrupt that never gets confirmed."""
    fifo_path = os.path.join(run_dir, "stdin.fifo")
    os.mkfifo(fifo_path, 0o600)
    out_path = os.path.join(run_dir, "out.jsonl")
    fifo_fd = os.open(fifo_path, os.O_RDWR)
    out_fh = open(out_path, "wb")
    proc = subprocess.Popen([sys.executable, "-u", "-c", _FAKE_CLI],
                            stdin=fifo_fd, stdout=out_fh, stderr=subprocess.DEVNULL,
                            start_new_session=True)
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


def test_a_popen_failure_propagates_uninmasked(agent, tmp_path, monkeypatch):
    """Found in review: the FIFO write used to live in a `finally`, which
    runs even when `Popen` itself raised — no child was ever created, so
    writing there could itself fail (or, on a large message, hang) and
    REPLACE the real spawn error with an unrelated one. The write now only
    happens after a successful spawn, so the original exception must reach
    the caller exactly as raised."""
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "f.py"
    target.write_text("x = 1")
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent, "_persistent_ok", lambda: True)

    def boom(cmd, **kw):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(agent.subprocess, "Popen", boom)
    with pytest.raises(FileNotFoundError, match="no such binary"):
        agent._start(str(target), "hi", "", "", "")


def test_start_kills_and_errors_if_the_first_message_never_lands(agent, tmp_path,
                                                                  monkeypatch):
    """A process that spawned but never got its first message is a phantom
    run — `_alive` says it's going, `_poll` will wait forever for a `result`
    that can never arrive. `_start` must not hand back a run_id for one."""
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "f.py"
    target.write_text("x = 1")

    class _Proc:
        pid = 4242

    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent, "_persistent_ok", lambda: True)
    monkeypatch.setattr(agent.subprocess, "Popen", lambda cmd, **kw: _Proc())
    monkeypatch.setattr(agent, "_fifo_write_all",
                        lambda fd, payload, timeout=agent._FIFO_WRITE_TIMEOUT: False)
    killed = []
    monkeypatch.setattr(agent, "_kill_process", lambda run_dir: killed.append(run_dir))

    out = agent._start(str(target), "hi", "", "", "")
    assert out == {"error": "claude did not accept the first message"}
    assert killed, "a process that never got its message must be killed, not orphaned"


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

def test_a_confirmed_interrupt_leaves_the_process_alive_and_scopes_the_marker(
        agent, tmp_path):
    """The happy path: the FAKE claude actually honours the interrupt (writes
    a `result` row), `_wait_for_turn_end` sees it and stops waiting, and
    `_cancel` never escalates. The marker is TURN-SCOPED, not the old sticky
    whole-run file — a persistent run's process survives past the turn it
    just stopped, so `_history`/`_poll` must be able to tell that turn apart
    from a later one that finishes cleanly (see `_interrupted_indices`)."""
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    agent._private_dir(agent._perm_dir(run_dir))
    proc, out_path = _spawn_responsive_fake_claude(agent, run_dir)
    try:
        result = agent._cancel("run1")
        assert result == {"cancelled": "run1"}
        time.sleep(0.3)
        assert proc.poll() is None, "a confirmed interrupt must not kill the process"
        # The OLD sticky marker is NOT written for a persistent interrupt —
        # only the turn-scoped one.
        assert not os.path.exists(os.path.join(run_dir, "cancelled"))
        assert 0 in agent._interrupted_indices(run_dir), \
            "the first (0th) turn is the one that was in flight when stopped"
        data = _read(out_path)
        rows = [json.loads(line) for line in data.strip().splitlines()]
        assert any(r.get("type") == "result" for r in rows)
    finally:
        proc.kill()
        proc.wait()


def test_an_unconfirmed_interrupt_escalates_to_a_kill(agent, tmp_path, monkeypatch):
    """`cat` receives the interrupt (bytes land on the wire) but — like a
    turn wedged in an MCP permission wait, or a dropped control request —
    never actually ends the turn: no `result` row ever appears. `_cancel`
    must not report success for a process it never actually stopped; it has
    to escalate to the kill the one-shot path has always used."""
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    agent._private_dir(agent._perm_dir(run_dir))
    monkeypatch.setattr(agent, "_INTERRUPT_CONFIRM_TIMEOUT", 0.3)
    proc, _out_path = _spawn_fake_claude(agent, run_dir)
    try:
        result = agent._cancel("run1")
        assert result == {"cancelled": "run1"}
        proc.wait(timeout=5)
        assert proc.returncode is not None, \
            "an interrupt nobody confirmed must escalate to a real kill"
        # Still turn-scoped, even on the escalated path — the record of WHICH
        # turn this stopped does not depend on how the stop was achieved.
        assert 0 in agent._interrupted_indices(run_dir)
    finally:
        if proc.poll() is None:
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


# ------------------------------------------------------- reap / expected_turns

def test_a_persistent_run_that_is_truly_idle_gets_reaped(agent, tmp_path):
    """The core of the fix for the leak: once a persistent run's turn (and
    every turn `_steer` asked for) has landed, `_poll` itself ends the
    process — nothing external ever gets a chance to let it sit there
    forever. The FIFO's O_RDWR trick that keeps this process from ever
    seeing stdin EOF is exactly what stops it from exiting on its own."""
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    with agent._private_open(os.path.join(run_dir, "meta.json")) as f:
        json.dump({"file": "x", "message": "hi", "persistent": True}, f)
    proc, _out_path = _spawn_fake_claude(agent, run_dir)
    try:
        with agent._private_open(os.path.join(run_dir, "out.jsonl")) as f:
            f.write(json.dumps({"type": "result", "session_id": "s1",
                                "result": "one"}) + "\n")

        out = agent._poll("run1")
        assert out["done"] is True
        proc.wait(timeout=5)
        assert proc.returncode is not None, \
            "an idle persistent run with nothing outstanding must be reaped"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_poll_does_not_finish_while_a_steered_turn_is_still_outstanding(
        agent, tmp_path):
    """The other half of the same fix, and the one that keeps a steered
    message from silently vanishing (D497): if `_steer` has been told a
    SECOND turn is coming, `done` must not go True (and the process must not
    be reaped) on the FIRST turn's `result` alone — the page's poll loop
    would abandon the run one row too early and nothing would be left
    watching when the steered turn's reply actually starts."""
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    with agent._private_open(os.path.join(run_dir, "meta.json")) as f:
        json.dump({"file": "x", "message": "hi", "persistent": True}, f)
    proc, _out_path = _spawn_fake_claude(agent, run_dir)
    try:
        with agent._private_open(os.path.join(run_dir, "out.jsonl")) as f:
            f.write(json.dumps({"type": "result", "session_id": "s1",
                                "result": "one"}) + "\n")
        # One steer accepted -> expect a SECOND result before this can be done.
        agent._private_append(os.path.join(run_dir, "steered"), "1")

        out = agent._poll("run1")
        assert out["done"] is False, \
            "a steered turn has not landed yet — this must still be in flight"

        time.sleep(0.3)
        assert proc.poll() is None, "must not be reaped while a steer is outstanding"

        # Now the steered turn's own result lands...
        with agent._private_open(os.path.join(run_dir, "out.jsonl")) as f:
            f.write(json.dumps({"type": "result", "session_id": "s1",
                                "result": "one"}) + "\n")
            f.write(json.dumps({"type": "result", "session_id": "s1",
                                "result": "two"}) + "\n")
        out = agent._poll("run1")
        assert out["done"] is True
        proc.wait(timeout=5)
        assert proc.returncode is not None, "now it may be reaped"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_not_alive_is_done_regardless_of_outstanding_steers(agent, tmp_path):
    """The unconditional escape hatch: a `_steer` that raced a process which
    died in the same instant must not leave `done` stuck False forever
    waiting on a turn that will never come."""
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    with agent._private_open(os.path.join(run_dir, "meta.json")) as f:
        json.dump({"file": "x", "message": "hi", "persistent": True}, f)
    with agent._private_open(os.path.join(run_dir, "out.jsonl")) as f:
        f.write(json.dumps({"type": "result", "session_id": "s1",
                            "result": "one"}) + "\n")
    agent._private_append(os.path.join(run_dir, "steered"), "1")
    with agent._private_open(os.path.join(run_dir, "pid")) as f:
        f.write("999999999")  # dead

    out = agent._poll("run1")
    assert out["done"] is True


# ---------------------------------------------------------- _fifo_write_all

def test_fifo_write_all_loops_over_short_writes_until_complete(agent, monkeypatch):
    payload = b"x" * 100
    writes = []

    def fake_write(fd, data):
        n = min(10, len(data))
        writes.append(n)
        return n

    monkeypatch.setattr(agent.os, "write", fake_write)
    monkeypatch.setattr(agent.os, "set_blocking", lambda fd, flag: None)
    assert agent._fifo_write_all(999, payload) is True
    assert sum(writes) == 100
    assert len(writes) == 10, "a short-count write must not be read as done"


def test_fifo_write_all_gives_up_rather_than_hang_on_a_dead_reader(agent, monkeypatch):
    def raising_write(fd, data):
        raise BlockingIOError()

    monkeypatch.setattr(agent.os, "write", raising_write)
    monkeypatch.setattr(agent.os, "set_blocking", lambda fd, flag: None)
    monkeypatch.setattr(agent.select, "select", lambda r, w, x, t: ([], [], []))
    assert agent._fifo_write_all(999, b"x", timeout=0.05) is False


def test_steer_delivers_a_large_message_without_truncation(agent, tmp_path):
    """The realistic trigger the review called out: a pasted log bigger than
    the pipe's buffer (16-64KB). A single non-blocking write used to return a
    short count and be read as success anyway, leaving the CLI with half a
    JSON line and the next write's bytes glued onto its tail."""
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    run_dir = os.path.join(agent.RUNS, "run1")
    agent._private_dir(run_dir)
    proc, out_path = _spawn_fake_claude(agent, run_dir)
    try:
        big = "y" * 200_000  # comfortably bigger than any OS pipe buffer
        result = agent._steer("run1", big)
        assert result == {"steered": True}
        data = _read(out_path, tries=50, delay=0.1)
        row = json.loads(data.strip().splitlines()[0])
        assert row["message"]["content"][0]["text"] == big
    finally:
        proc.kill()
        proc.wait()


# ------------------------------------------------------------- main() dispatch

def test_main_dispatches_the_steer_action(agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    out = agent.main(action="steer", run_id="no-such-run", message="hi")
    assert out["steered"] is False

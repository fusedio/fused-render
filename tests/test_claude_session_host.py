"""session_host.py: one long-lived process owns the CLI's stdin for a whole
chat session, not just one turn.

`_start` now detaches `session_host.py` instead of the CLAUDE CLI directly.
This drives the REAL host against a small stub `claude` binary (a Python
script standing in for the CLI, wired in via FUSED_RENDER_CLAUDE_BIN) —
nothing here is mocked at the Popen level, because the whole point of this
task is the process tree `_start` now actually spawns.

The stub speaks a tiny protocol over stream-json: each line it reads is
echoed back as `{"type": "echo", "text": <the user text>}`, immediately
followed by a `result` row — except two magic strings, `BGON`/`BGOFF`, which
also emit a `background_tasks_changed` row (non-empty / empty) so the reap
timer's "tasks_pending holds the reap off" branch can be exercised without
a real background task ever running.
"""
import importlib.util
import json
import os
import sys
import time

import pytest

from _claude_stub_cli import write_stub_cli

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load_agent():
    path = os.path.join(TEMPLATE_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    mod = _load_agent()
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


def _load_host():
    path = os.path.join(TEMPLATE_DIR, "session_host.py")
    spec = importlib.util.spec_from_file_location("claude_session_host", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_STUB = '''#!{python}
import json
import sys

def send(row):
    sys.stdout.write(json.dumps(row) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    text = row["message"]["content"][0]["text"]
    send({{"type": "echo", "text": text}})
    if text == "BGON":
        send({{"type": "system", "subtype": "background_tasks_changed",
               "tasks": [{{"task_id": "t1", "description": "sleep"}}]}})
    elif text == "BGOFF":
        send({{"type": "system", "subtype": "background_tasks_changed",
               "tasks": []}})
    send({{"type": "result", "session_id": "sess-stub", "result": "ok"}})
'''


@pytest.fixture()
def stub_cli(tmp_path):
    return write_stub_cli(tmp_path / "bin", _STUB.format(python=sys.executable))


@pytest.fixture()
def target(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    return str(f)


def _out_rows(run_dir):
    path = os.path.join(run_dir, "out.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _start(agent, monkeypatch, stub_cli, target, message="m1", **extra_env):
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", stub_cli)
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)
    res = agent._start(target, message, "", "", "", has_pane=False)
    assert "run_id" in res, res
    return os.path.join(agent.RUNS, res["run_id"])


def test_two_inbox_entries_reach_the_stub_in_order(agent, monkeypatch, stub_cli,
                                                     target):
    run_dir = _start(agent, monkeypatch, stub_cli, target, message="first")
    # A follow-up written straight into the inbox, exactly the shape `_send`
    # (a later task) will write — the host does not care who wrote it.
    assert _wait_for(lambda: any(
        r.get("type") == "echo" for r in _out_rows(run_dir)))
    agent._write_inbox_entry(run_dir, "second")

    def both_echoed():
        texts = [r["text"] for r in _out_rows(run_dir) if r.get("type") == "echo"]
        return texts == ["first", "second"]

    assert _wait_for(both_echoed), _out_rows(run_dir)


def test_host_survives_a_result_row(agent, monkeypatch, stub_cli, target):
    """A trailing `result` ends the TURN, not the host — it must still be
    around (and its process group root, run_dir/pid, alive) to accept a
    later follow-up."""
    run_dir = _start(agent, monkeypatch, stub_cli, target, message="only")

    def turn_finished():
        return any(r.get("type") == "result" for r in _out_rows(run_dir))

    assert _wait_for(turn_finished)
    with open(os.path.join(run_dir, "pid"), encoding="utf-8") as f:
        pid = int(f.read().strip())
    assert agent._alive(run_dir), "the CLI's pid must still be alive right " \
        "after a result — only the idle-reap timer ends the host"
    assert pid != os.getpid()  # confirms the pid file WAS overwritten by the
    # host with the stub CLI's own pid, not left as the host's


def test_it_exits_after_the_idle_window(agent, monkeypatch, stub_cli, target):
    run_dir = _start(agent, monkeypatch, stub_cli, target, message="only",
                      FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS="0.3",
                      FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS="0.05")

    def reaped():
        return not agent._alive(run_dir)

    assert _wait_for(reaped, timeout=10.0), \
        "the host (and the CLI under it) should have exited once " \
        "_turn_state read idle-with-nothing-pending for the whole window"
    assert not os.path.exists(os.path.join(run_dir, "host.json"))


def test_pending_tasks_hold_the_reap_off(agent, monkeypatch, stub_cli, target):
    run_dir = _start(agent, monkeypatch, stub_cli, target, message="BGON",
                      FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS="0.3",
                      FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS="0.05")

    def tasks_seen():
        turn_open, tasks_pending = agent._turn_state(run_dir)
        return tasks_pending

    assert _wait_for(tasks_seen)
    # Well past the idle window — still up, because tasks_pending is True.
    time.sleep(0.6)
    assert agent._alive(run_dir), \
        "a non-empty background_tasks_changed must hold the reap off even " \
        "though the turn itself already ended in a result"
    assert os.path.exists(os.path.join(run_dir, "host.json"))

    agent._write_inbox_entry(run_dir, "BGOFF")

    def reaped():
        return not agent._alive(run_dir)

    assert _wait_for(reaped, timeout=10.0), \
        "emptying the tasks array should let the idle window run out again"


# ------------------------------------------------- B5: the reap decision is cheap

def test_turn_state_is_not_reread_while_out_jsonl_is_unchanged(tmp_path):
    """B5 regression: the reap loop calls this every _DRAIN_INTERVAL_SECONDS
    (0.2s) for the whole life of a session. `agent._turn_state` re-parses the
    ENTIRE out.jsonl on every call — fine once, ruinous 5 times a second
    against a multi-hour session's tens-of-MB transcript. Nothing new can
    have happened if the file has not grown, so a tick that sees the same
    size must not re-derive the answer at all."""
    host = _load_host()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "out.jsonl").write_text("")

    calls = []

    class _FakeAgent:
        @staticmethod
        def _turn_state(rd):
            calls.append(rd)
            return (True, False)

    cache = {}
    first = host._turn_state_if_grown(_FakeAgent, str(run_dir), cache)
    second = host._turn_state_if_grown(_FakeAgent, str(run_dir), cache)
    third = host._turn_state_if_grown(_FakeAgent, str(run_dir), cache)
    assert first == second == third == (True, False)
    assert len(calls) == 1, \
        "three ticks with no growth in out.jsonl must derive the state once"

    # The file actually grows: the next tick must re-derive, and only that one.
    (run_dir / "out.jsonl").write_text('{"type": "result"}\n')
    fourth = host._turn_state_if_grown(_FakeAgent, str(run_dir), cache)
    fifth = host._turn_state_if_grown(_FakeAgent, str(run_dir), cache)
    assert fourth == fifth == (True, False)
    assert len(calls) == 2, \
        "growth must trigger exactly one re-derive, not one per tick after it"


# ---------------------------------------------- B8: the pid-file write race

# --------------------------------------------- D686: the drain-before-reap race

def test_a_message_that_arrives_during_the_reap_decision_is_still_drained(
        tmp_path, monkeypatch):
    """A `_send` writes straight into `run_dir/inbox` and returns
    `{"sent": True}` without knowing anything about the reap loop's own
    timing. If that write lands in the gap between the reap loop's own last
    regular `_drain_inbox` call (top of the iteration that goes on to decide
    to `break`) and the loop actually tearing the session down, the message
    used to be silently orphaned — never handed to the CLI, and never
    findable again once `host.json` is gone. This drives the real
    `_reap_loop` with a fake CLI (never dies on its own — only the idle
    timer ends it) and a `_turn_state` stub that writes a fresh inbox entry
    on its second call, exactly where a `_send` landing mid-decision would:
    after this iteration's own `_drain_inbox` already ran, but before the
    loop notices anything changed and exits."""
    host = _load_host()
    agent = _load_agent()
    monkeypatch.setattr(host, "_IDLE_REAP_SECONDS", 0.001)
    monkeypatch.setattr(host, "_DRAIN_INTERVAL_SECONDS", 0.02)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "out.jsonl").write_text("")
    host_json = run_dir / "host.json"
    host_json.write_text("{}")

    calls = []

    # `_turn_state_if_grown` (not `agent._turn_state` itself — see B5's own
    # test above) only re-derives when `out.jsonl` has grown, so it, not
    # `_turn_state`, is the thing to fake here: this stub bypasses that
    # cache entirely and always looks like a fresh read.
    def fake_turn_state_if_grown(agent, rd, cache):
        calls.append(rd)
        if len(calls) == 2:
            # The race: a follow-up lands right after this iteration's own
            # `_drain_inbox` call already found the inbox empty.
            agent._write_inbox_entry(str(run_dir), "raced in")
        return (False, False)

    monkeypatch.setattr(host, "_turn_state_if_grown", fake_turn_state_if_grown)

    class _FakeStdin:
        def __init__(self):
            self.written = []
            self.closed = False

        def write(self, data):
            self.written.append(data)

        def flush(self):
            pass

        def close(self):
            self.closed = True

    class _FakeCli:
        def __init__(self):
            self.stdin = _FakeStdin()

        def poll(self):
            return None  # never dies on its own — only the idle timer ends this

        def wait(self, timeout=None):
            pass

    fake_cli = _FakeCli()
    host._reap_loop(agent, str(run_dir), fake_cli, str(host_json))

    assert len(calls) >= 2, "the race must actually have had a chance to occur"
    inbox_left = list((run_dir / "inbox").glob("*.json"))
    done_dir = run_dir / "inbox" / "done"
    drained = list(done_dir.glob("*.json")) if done_dir.is_dir() else []
    assert inbox_left == [], (
        "a follow-up that landed during the reap decision was left behind "
        "in the inbox, never handed to the CLI"
    )
    assert len(drained) == 1, \
        "the raced-in message must have reached the CLI via the final drain"
    assert not host_json.exists(), \
        "host.json must still be removed once teardown is done"


def test_start_never_clobbers_a_pid_the_host_already_wrote(agent, monkeypatch,
                                                            target):
    """`_start` used to write the host's OWN pid to run_dir/pid
    unconditionally, right after spawning session_host.py — which begins
    overwriting that same file with the CLI's own pid the moment IT spawns
    the CLI. If the host won that race, `_start`'s later write clobbered the
    CLI's pid back to the host's, and `_cancel`'s killpg (which only ever
    targets whatever pid this file holds) then reached just the host's
    process group, orphaning the very CLI it was supposed to kill."""

    class _FakeStdin:
        def write(self, data):
            pass

        def close(self):
            pass

    class _FakeProc:
        pid = 424242  # the HOST's own pid, distinct from the "CLI" pid below
        stdin = _FakeStdin()

    def fake_popen(cmd, **kwargs):
        # `_start` has already created run_dir (via _private_dir) by the time
        # it Popens the host, so it is the one new entry under RUNS.
        run_dir = os.path.join(agent.RUNS, os.listdir(agent.RUNS)[0])
        # Simulate the host WINNING the race: by the time _start gets around
        # to writing its own placeholder pid below, the real host has already
        # spawned the CLI and overwritten run_dir/pid with the CLI's pid.
        with open(os.path.join(run_dir, "pid"), "w", encoding="utf-8") as f:
            f.write("999999")
        return _FakeProc()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    res = agent._start(target, "hi", "", "", "", has_pane=False)
    run_dir = os.path.join(agent.RUNS, res["run_id"])
    with open(os.path.join(run_dir, "pid"), encoding="utf-8") as f:
        pid = f.read().strip()
    assert pid == "999999", \
        "the host's own write must survive _start's later placeholder write"

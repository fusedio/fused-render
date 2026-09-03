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
import stat
import sys
import time

import pytest

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
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(_STUB.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


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

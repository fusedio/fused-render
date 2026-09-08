"""_turn_state: one liveness answer read off out.jsonl, not a pid.

A session host (see the persistent-session plan) can hold a `claude` process
alive well past its last turn, so "the process exists" stopped meaning "a
turn is running". `_turn_state` is the D415 rule `_poll` already applies to
its own `idle` flag, lifted out so `_live_run`/`_live_sessions` can ask the
same question without a pid touch, plus whether background work is still
pending (which is what has to hold a session host's reap off).
"""
import importlib.util
import json
import os

import pytest


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
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


def _write_rows(run_dir, rows):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "out.jsonl"), "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _result(session_id="sess-A"):
    return {"type": "result", "session_id": session_id, "result": "done"}


def _system_init(session_id="sess-A"):
    return {"type": "system", "subtype": "init", "session_id": session_id}


def _bg_tasks(*ids):
    return {"type": "system", "subtype": "background_tasks_changed",
            "tasks": [{"task_id": i, "description": "sleep"} for i in ids]}


def test_no_out_jsonl_is_open(agent, tmp_path):
    """A freshly spawned run with no out.jsonl yet is not idle — same as
    `_poll` reads a run whose file does not exist yet."""
    run_dir = str(tmp_path / "run1")
    os.makedirs(run_dir)
    assert agent._turn_state(run_dir) == (True, False)


def test_trailing_result_is_idle(agent, tmp_path):
    run_dir = str(tmp_path / "run2")
    _write_rows(run_dir, [_system_init(), _result()])
    assert agent._turn_state(run_dir) == (False, False)


def test_rows_after_a_result_reopen_the_turn(agent, tmp_path):
    """The CLI waking itself for another turn — a hook, a fresh init, more
    text — after a `result` means a new turn is open, exactly like `_poll`'s
    `idle` flag resets on ANY row that follows a `result`."""
    run_dir = str(tmp_path / "run3")
    _write_rows(run_dir, [_result(), _system_init()])
    assert agent._turn_state(run_dir) == (True, False)


def test_nonempty_tasks_is_pending_even_when_idle(agent, tmp_path):
    run_dir = str(tmp_path / "run4")
    _write_rows(run_dir, [_system_init(), _bg_tasks("t1"), _result()])
    assert agent._turn_state(run_dir) == (False, True)


def test_a_subagents_result_row_does_not_close_the_turn(agent, tmp_path):
    """`_poll` `continue`s on any row carrying `parent_tool_use_id` before
    its own `idle` check — a subagent's `result` row must not read as the
    whole turn closing. The docstring here claims to lift that rule
    "exactly", so `_turn_state` must not diverge from it."""
    run_dir = str(tmp_path / "run5")
    _write_rows(run_dir, [
        _system_init(),
        {"type": "result", "parent_tool_use_id": "toolu_1",
         "session_id": "sess-A", "result": "sub done"},
    ])
    assert agent._turn_state(run_dir) == (True, False)


def test_emptied_tasks_array_clears_pending(agent, tmp_path):
    run_dir = str(tmp_path / "run5")
    _write_rows(run_dir, [_bg_tasks("t1"), _bg_tasks(), _result()])
    assert agent._turn_state(run_dir) == (False, False)


def test_garbage_line_is_skipped(agent, tmp_path):
    run_dir = str(tmp_path / "run6")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "out.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(_system_init()) + "\n")
        f.write("{not json\n")
    assert agent._turn_state(run_dir) == (True, False)


def test_live_sessions_ignores_a_live_pid_with_a_trailing_result(agent, tmp_path):
    """The whole point: a session host keeps the pid alive after its turn
    ends, so `_live_sessions` (and `_live_run`) must stop reading THAT as
    "still going" and read the transcript instead."""
    target = tmp_path / "proj" / "index.html"
    target.parent.mkdir()
    target.write_text("<html></html>")
    run_dir = os.path.join(agent.RUNS, "20260901-120000-aaa")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"file": str(target), "message": "hi",
                   "resumed_from": "sess-A", "mode": "prompt"}, f)
    with open(os.path.join(run_dir, "pid"), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))  # a real, alive pid
    _write_rows(run_dir, [_system_init("sess-A"), _result("sess-A")])

    assert agent._live_run(str(target), "sess-A") == {"run_id": ""}
    assert agent._live_sessions(str(target)) == set()

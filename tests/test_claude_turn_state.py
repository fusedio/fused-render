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
    path = os.path.join("fused_render", "claude_agent", "agent.py")
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


def test_a_command_lifecycle_row_after_the_result_does_not_reopen_the_turn(
        agent, tmp_path):
    """Claude Code 2.1.302 writes a `command_lifecycle` ledger row after the
    `result` when a message was queued behind the turn. Not a turn: the list
    said Done while the chat re-attached as still going (Akshil, 2026-10-20).
    A command that really starts a turn brings its own `init`."""
    run_dir = str(tmp_path / "run3b")
    _write_rows(run_dir, [_system_init(), _result(),
                          {"type": "command_lifecycle", "state": "queued",
                           "command_uuid": "c1", "uuid": "u1"}])
    assert agent._turn_state(run_dir) == (False, False)
    _write_rows(run_dir, [_system_init(), _result(),
                          {"type": "command_lifecycle", "state": "queued",
                           "command_uuid": "c1", "uuid": "u1"},
                          _system_init()])
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


# --------------------------------------------------- incremental fold (host RSS)


def _append(run_dir, text):
    with open(os.path.join(run_dir, "out.jsonl"), "a", encoding="utf-8") as f:
        f.write(text)


_FOLD_ROWS = [
    _system_init(),
    {"type": "assistant", "message": {"content": []}},
    _bg_tasks("t1"),
    _result(),
    {"type": "assistant", "parent_tool_use_id": "tu1", "message": {"content": []}},
    {"type": "system", "subtype": "task_notification", "status": "stopped"},
]


@pytest.mark.parametrize("n", range(len(_FOLD_ROWS) + 1))
def test_fold_over_prefix_equals_one_shot(agent, tmp_path, n):
    run_dir = str(tmp_path / "r")
    _write_rows(run_dir, _FOLD_ROWS[:n])
    assert agent._turn_state_fold(run_dir, {}) == agent._turn_state(run_dir)


@pytest.mark.parametrize("split", range(len(_FOLD_ROWS) + 1))
def test_fold_in_two_chunks_equals_one_shot(agent, tmp_path, split):
    run_dir = str(tmp_path / "r")
    _write_rows(run_dir, _FOLD_ROWS[:split])
    cursor = {}
    agent._turn_state_fold(run_dir, cursor)
    _append(run_dir, "".join(json.dumps(r) + "\n" for r in _FOLD_ROWS[split:]))
    got = agent._turn_state_fold(run_dir, cursor)
    _write_rows(str(tmp_path / "one"), _FOLD_ROWS)
    assert got == agent._turn_state(str(tmp_path / "one"))


def test_fold_does_not_consume_half_written_line(agent, tmp_path):
    run_dir = str(tmp_path / "r")
    _write_rows(run_dir, [_system_init()])
    cursor = {}
    assert agent._turn_state_fold(run_dir, cursor) == (True, False)
    offset = cursor["offset"]
    line = json.dumps(_result()) + "\n"
    _append(run_dir, line[:10])
    # Not arrived yet: still open, offset unmoved.
    assert agent._turn_state_fold(run_dir, cursor) == (True, False)
    assert cursor["offset"] == offset
    _append(run_dir, line[10:])
    assert agent._turn_state_fold(run_dir, cursor) == (False, False)


def test_one_shot_still_parses_unterminated_complete_last_line(agent, tmp_path):
    run_dir = str(tmp_path / "r")
    _write_rows(run_dir, [_system_init()])
    _append(run_dir, json.dumps(_result()))  # valid JSON, no newline
    assert agent._turn_state(run_dir) == (False, False)
    cursor = {}
    assert agent._turn_state_fold(run_dir, cursor) == (True, False)  # not consumed
    assert agent._turn_state_fold(run_dir, cursor, partial=True) == (False, False)


def test_fold_resets_on_truncation(agent, tmp_path):
    run_dir = str(tmp_path / "r")
    _write_rows(run_dir, [_system_init(), _bg_tasks("t1"), _result()])
    cursor = {}
    assert agent._turn_state_fold(run_dir, cursor) == (False, True)
    _write_rows(run_dir, [_system_init()])  # smaller: replaced
    assert agent._turn_state_fold(run_dir, cursor) == (True, False)


def test_fold_missing_file_is_open(agent, tmp_path):
    run_dir = str(tmp_path / "none")
    assert agent._turn_state_fold(run_dir, {}) == (True, False)


def test_fold_memory_stays_flat_while_file_grows(tmp_path):
    """The host called the one-shot read on every growth: +50MB on a 7MB file.
    The fold must not retain or rebuild the file."""
    import subprocess
    import sys
    code = r'''
import importlib.util, json, os, resource, sys
spec = importlib.util.spec_from_file_location("a", "fused_render/claude_agent/agent.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = sys.argv[1]
os.makedirs(d, exist_ok=True)
row = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 2000}]}}) + "\n"
path = os.path.join(d, "out.jsonl")
cursor = {}
def rss():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024
with open(path, "w") as f:
    f.write(json.dumps({"type": "system", "subtype": "init"}) + "\n")
m._turn_state_fold(d, cursor)
base = rss()
for _ in range(50):
    with open(path, "a") as f:
        f.write(row * 50)  # ~100KB per round, ~5MB total
    m._turn_state_fold(d, cursor)
print(rss() - base)
'''
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path / "mem")],
                         capture_output=True, text=True, check=True).stdout
    assert float(out.strip()) < 15

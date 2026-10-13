"""A `result` followed only by the CLI's bookkeeping rows is a finished turn.

Seen live on Claude Code 2.1.296: a turn that ran background work ends as
`post_turn_summary`, `result`, `task_summary`, and a backgrounded monitor's
`task_updated` / `background_tasks_changed` rows keep landing after the
reply. `_poll` used to read anything after `result` as the run waking for
another turn, so such a run never reported `done`; `_turn_state` had the same
rule and kept the host's reap loop thinking the turn was open.
"""
import json
import os

import pytest

from fused_render.claude_agent import agent


def _rows(*rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


RESULT = {"type": "result", "subtype": "success", "session_id": "s1",
          "result": "all done", "duration_ms": 10}


@pytest.mark.parametrize("trailer", [
    {"type": "system", "subtype": "task_summary", "detail": None},
    {"type": "system", "subtype": "task_updated", "task_id": "t1",
     "patch": {"status": "completed"}},
    {"type": "system", "subtype": "background_tasks_changed", "tasks": []},
    {"type": "system", "subtype": "task_progress", "task_id": "t1",
     "description": "x", "usage": {"total_tokens": 1, "tool_uses": 0,
                                   "duration_ms": 5}},
])
def test_turn_state_closes_on_bookkeeping_after_result(tmp_path, trailer):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "out.jsonl").write_text(_rows(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        RESULT, trailer))
    turn_open, _pending = agent._turn_state(str(run_dir))
    assert turn_open is False


def test_turn_state_still_opens_on_a_real_wake(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "out.jsonl").write_text(_rows(
        RESULT, {"type": "system", "subtype": "init", "session_id": "s1"}))
    turn_open, _pending = agent._turn_state(str(run_dir))
    assert turn_open is True


def test_background_tasks_changed_after_result_still_counts_pending(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "out.jsonl").write_text(_rows(
        RESULT, {"type": "system", "subtype": "background_tasks_changed",
                 "tasks": [{"task_id": "m1", "description": "monitor"}]}))
    turn_open, pending = agent._turn_state(str(run_dir))
    assert turn_open is False
    assert pending is True


def test_poll_reports_done_with_task_summary_after_result(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "RUNS", str(tmp_path))
    run_id = "20261013-000000-abcdef"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps(
        {"file": str(tmp_path), "message": "hi", "resumed_from": "",
         "mode": "prompt", "session_id": "s1"}))
    (run_dir / "out.jsonl").write_text(_rows(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "all done"}]}},
        {"type": "system", "subtype": "post_turn_summary"},
        RESULT,
        {"type": "system", "subtype": "task_summary", "detail": None}))
    # No pid file: the process is gone either way, so `done` is forced by
    # liveness — assert on the turn-open reading the page keys its verb on.
    out = agent._poll(run_id, str(tmp_path))
    assert out["done"] is True
    assert out["error"] == ""
    assert os.path.exists(run_dir / "out.jsonl")

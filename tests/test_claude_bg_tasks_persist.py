"""`background_tasks_changed` is the authoritative full list, and the CLI
only emits one on change. Once `_poll`'s cursor (see `_read_current_turn`)
advances past a turn boundary, a later poll's own row window can no longer
contain that row — so `bg_tasks` rebuilt from scratch, each poll, off only
what that poll's own window happens to hold, silently drops back to empty
even while the background work it described is still running. `activity`
(rendered by `w.setStats`) and `tasks_pending` (what a session host's reap
loop leans on — see `_turn_state`) both need to keep reporting the last
authoritative list across a cursor advance, not just within the one poll
that actually saw the row.
"""
import importlib.util
import json
import os

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    return d


def _write(run_dir, rows):
    body = "".join(json.dumps(r) + "\n" for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")


def _append(run_dir, rows):
    body = "".join(json.dumps(r) + "\n" for r in rows)
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(body)


def _user_row(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def _text_row(chunk):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": chunk}}}


def _result_row(session="s"):
    return {"type": "result", "session_id": session, "result": "done"}


def _bg_tasks_row(*ids):
    return {"type": "system", "subtype": "background_tasks_changed",
            "tasks": [{"task_id": i, "description": "sleep"} for i in ids]}


def _poll(agent, run_dir, alive=True):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    return agent._poll("run")


def test_pending_tasks_survive_a_cursor_advance_past_their_own_row(
        agent, run_dir):
    _write(run_dir, [_user_row("start"), _bg_tasks_row("t1"),
                      _text_row("working on it"), _result_row()])
    first = _poll(agent, run_dir)
    assert first["tasks_pending"] is True
    assert [t["id"] for t in first["activity"]["tasks"]] == ["t1"]

    # A genuine new turn (preceded by a `result`) is what makes the NEXT
    # poll's cursor advance past turn one's `background_tasks_changed` row —
    # `_read_current_turn` only applies an advance computed on one call
    # starting with the FOLLOWING call, so this poll still sees the full
    # window (same as `test_claude_poll_cursor.py`'s own cursor tests).
    _append(run_dir, [_user_row("continue"), _text_row("still going")])
    second = _poll(agent, run_dir)
    assert second["tasks_pending"] is True

    # THIS poll's window starts at "continue"'s own row — nothing in it says
    # anything about task t1 at all, since the cursor advance computed by the
    # previous call now takes effect.
    third = _poll(agent, run_dir)
    assert third["tasks_pending"] is True, (
        "a background task started in an earlier turn must still read as "
        "pending after the cursor advances past the row that announced it")
    assert [t["id"] for t in third["activity"]["tasks"]] == ["t1"]


def test_an_empty_tasks_row_clears_the_persisted_state(agent, run_dir):
    _write(run_dir, [_user_row("start"), _bg_tasks_row("t1"),
                      _text_row("working on it"), _result_row()])
    _poll(agent, run_dir)

    _append(run_dir, [_user_row("continue"), _bg_tasks_row(), _result_row()])
    second = _poll(agent, run_dir)
    assert second["tasks_pending"] is False
    assert second["activity"]["tasks"] == []

    _append(run_dir, [_user_row("once more"), _text_row("done now")])
    third = _poll(agent, run_dir)
    assert third["tasks_pending"] is False
    assert third["activity"]["tasks"] == []

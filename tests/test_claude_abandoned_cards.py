"""A permission card whose asking call the CLI walked away from closes itself.

Akshil, 2026-10-18: a backgrounded subagent asked for Bash a minute after the
main reply ended; nobody saw the card; the next message restarted the main
thread and the subagent carried on without a result for that call. The
request sat in `perm/` with no decision, so the Tasks row read "blocked" for
as long as the process lived. `_abandoned_requests` reads out.jsonl for the
walk-away and `_permissions` latches a `deny`/`abandoned` on disk; `_cards`
is the lighter poll the chat keeps up after the reply ends.
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
    # The grace window is its own test below; everywhere else the second
    # reading is taken to have happened.
    monkeypatch.setattr(mod, "_ABANDON_GRACE", 0.0)
    return mod


def _run(agent, name="20261018-000000-abc123"):
    run_dir = os.path.join(agent.RUNS, name)
    os.makedirs(os.path.join(run_dir, "perm"))
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"file": "/tmp/x", "mode": "prompt"}, fh)
    return run_dir


def _ask(run_dir, req_id, tool_use_id, tool="Bash"):
    with open(os.path.join(run_dir, "perm", req_id + ".req.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"id": req_id, "tool": tool, "input": {"command": "ls"},
                   "tool_use_id": tool_use_id, "created_at": 1.0}, fh)


def _rows(run_dir, rows):
    with open(os.path.join(run_dir, "out.jsonl"), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _use(tool_use_id, mid, parent=None):
    row = {"type": "assistant", "message": {"id": mid, "content": [
        {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {}}]}}
    if parent:
        row["parent_tool_use_id"] = parent
    return row


def _text(mid, parent=None, text="next"):
    row = {"type": "assistant", "message": {"id": mid, "content": [
        {"type": "text", "text": text}]}}
    if parent:
        row["parent_tool_use_id"] = parent
    return row


def _result(tool_use_id, parent=None):
    row = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}]}}
    if parent:
        row["parent_tool_use_id"] = parent
    return row


def _decision(run_dir, req_id):
    path = os.path.join(run_dir, "perm", req_id + ".res.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_a_subagent_that_spoke_again_without_a_result_abandons_its_card(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1"),
                    _text("m2", parent="task-1")])
    rows = agent._permissions(run_dir)
    assert rows[0]["decision"] == "deny"
    assert rows[0]["reason"] == "abandoned"
    # Latched on disk: the permission server's wait ends and every reader
    # (the Tasks row, the rail's count) sees the same verdict.
    assert _decision(run_dir, "r1") == {"decision": "deny", "reason": "abandoned"}


def test_a_sibling_of_one_parallel_tools_message_is_not_a_walk_away(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-a")
    _rows(run_dir, [_use("tu-a", "m1", parent="task-1"),
                    _use("tu-b", "m1", parent="task-1")])
    assert agent._permissions(run_dir)[0]["decision"] == ""
    assert _decision(run_dir, "r1") is None


def test_the_main_thread_talking_does_not_abandon_a_subagents_card(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1"), _text("m2")])
    assert agent._permissions(run_dir)[0]["decision"] == ""


def test_a_subagent_working_does_not_abandon_the_main_threads_card(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-main")
    _rows(run_dir, [_use("tu-main", "m1"), _text("m9", parent="task-1")])
    assert agent._permissions(run_dir)[0]["decision"] == ""


def test_the_main_thread_moving_on_abandons_its_own_card(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-main")
    _rows(run_dir, [_use("tu-main", "m1"), _text("m2")])
    assert agent._permissions(run_dir)[0]["reason"] == "abandoned"


def test_a_result_that_landed_means_the_call_was_answered(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1"),
                    _result("tu-bash", parent="task-1"),
                    _text("m2", parent="task-1")])
    assert agent._permissions(run_dir)[0]["decision"] == ""
    assert _decision(run_dir, "r1") is None


def test_a_click_that_beat_the_closer_stands(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1"),
                    _text("m2", parent="task-1")])
    agent._write_decision(os.path.join(run_dir, "perm"), "r1",
                          {"decision": "allow", "scope": "once"})
    rows = agent._permissions(run_dir)
    assert rows[0]["decision"] == "allow"
    assert rows[0]["reason"] == ""


def test_a_request_without_a_tool_use_id_is_never_judged(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "")
    _rows(run_dir, [_text("m1"), _text("m2")])
    assert agent._permissions(run_dir)[0]["decision"] == ""


def test_the_scan_is_memoised_on_the_file_size(agent, monkeypatch):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1")])
    assert agent._permissions(run_dir)[0]["decision"] == ""
    calls = []
    real = agent._abandoned_requests

    def counting(run_dir_, rows):
        out = real(run_dir_, rows)
        calls.append(agent._ABANDON_CACHE[run_dir_][0])
        return out
    monkeypatch.setattr(agent, "_abandoned_requests", counting)
    agent._permissions(run_dir)
    before = agent._ABANDON_CACHE[run_dir]
    agent._permissions(run_dir)  # same size, same ids: the memo answers
    assert agent._ABANDON_CACHE[run_dir] is before
    with open(os.path.join(run_dir, "out.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_text("m2", parent="task-1")) + "\n")
    assert agent._permissions(run_dir)[0]["reason"] == "abandoned"


def test_a_thinking_only_row_is_not_the_scope_speaking(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    thinking = {"type": "assistant", "parent_tool_use_id": "task-1",
                "message": {"id": "m2", "content": [{"type": "thinking", "thinking": "hm"}]}}
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1"), thinking])
    assert agent._permissions(run_dir)[0]["decision"] == ""


def test_an_api_error_row_is_no_evidence(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    err = dict(_text("m2", parent="task-1", text="API Error: overloaded"),
               isApiErrorMessage=True)
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1"), err])
    assert agent._permissions(run_dir)[0]["decision"] == ""


def test_rows_without_a_message_id_never_judge(agent):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    # The asking row has no id: unknown, never abandoned.
    _rows(run_dir, [_use("tu-bash", "", parent="task-1"),
                    _text("m2", parent="task-1")])
    assert agent._permissions(run_dir)[0]["decision"] == ""
    # The later row has no id: it does not count as the scope speaking.
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1"),
                    _text("", parent="task-1")])
    agent._ABANDON_CACHE.clear()
    assert agent._permissions(run_dir)[0]["decision"] == ""


def test_the_verdict_waits_out_a_grace_window(agent, monkeypatch):
    """A permanent deny is not latched off one reading: the walk-away has to
    still hold `_ABANDON_GRACE` later (and a result landing meanwhile clears
    the sighting)."""
    monkeypatch.setattr(agent, "_ABANDON_GRACE", 5.0)
    clock = [100.0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock[0])
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1"),
                    _text("m2", parent="task-1")])
    assert agent._permissions(run_dir)[0]["decision"] == ""
    assert _decision(run_dir, "r1") is None
    clock[0] += 2.0
    agent._ABANDON_CACHE.clear()
    assert agent._permissions(run_dir)[0]["decision"] == ""
    # The result landed after all: the sighting is dropped.
    with open(os.path.join(run_dir, "out.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_result("tu-bash", parent="task-1")) + "\n")
    assert agent._permissions(run_dir)[0]["decision"] == ""
    assert "r1" not in agent._ABANDON_SEEN
    # …and a walk-away that holds across the window is latched.
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1"),
                    _text("m2", parent="task-1")])
    agent._ABANDON_CACHE.clear()
    assert agent._permissions(run_dir)[0]["decision"] == ""
    clock[0] += 6.0
    agent._ABANDON_CACHE.clear()
    assert agent._permissions(run_dir)[0]["reason"] == "abandoned"
    assert "r1" not in agent._ABANDON_SEEN


def test_cards_reports_alive_pending_and_the_rows(agent, monkeypatch):
    run_dir = _run(agent)
    _ask(run_dir, "r1", "tu-bash")
    _rows(run_dir, [_use("tu-bash", "m1", parent="task-1")])
    monkeypatch.setattr(agent, "_alive", lambda d: True)
    monkeypatch.setattr(agent, "_host_alive", lambda d: False)
    monkeypatch.setattr(agent, "_read_bg_tasks", lambda d: {
        "t1": {"status": "running", "ended_at": None}})
    out = agent._cards(os.path.basename(run_dir))
    assert out["alive"] is True
    assert out["tasks_pending"] is True
    assert [r["id"] for r in out["permissions"]] == ["r1"]
    assert out["mode"] == "prompt"


def test_cards_on_a_dead_host_with_nothing_pending(agent, monkeypatch):
    run_dir = _run(agent)
    monkeypatch.setattr(agent, "_alive", lambda d: False)
    monkeypatch.setattr(agent, "_host_alive", lambda d: False)
    out = agent._cards(os.path.basename(run_dir))
    assert out == {"alive": False, "tasks_pending": False,
                   "permissions": [], "mode": "prompt"}


def test_cards_refuses_an_unknown_run(agent):
    out = agent._cards("nope")
    assert out["error"] == "unknown run_id"
    assert out["alive"] is False

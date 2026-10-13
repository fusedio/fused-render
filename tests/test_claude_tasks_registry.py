"""SPEC T-1 / T-3: the task registry `_poll` keeps in `bg_tasks.json`, and the
`task_output` action.

The registry is `{task_id: TaskRecord}` — every task the run has seen, ended
ones included — built from the CLI's `task_*` / `background_tasks_changed`
system rows, applied ONCE per row (the poll re-reads its window every call,
and a record that counts monitor lines must not count them twice).
`activity.tasks` / `tasks_pending` keep their old meaning: the tasks still
going, as `{id, description}`.
"""
import importlib.util
import json
import os

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")


def _load():
    path = os.path.join(AGENT_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_tasks_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load()


@pytest.fixture
def run_dir(tmp_path, agent):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "out.jsonl").write_text("", encoding="utf-8")
    agent.RUNS = str(d.parent)
    agent._alive = lambda _d: True
    return d


def _append(run_dir, rows):
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


def _sys(subtype, **fields):
    row = {"type": "system", "subtype": subtype, "session_id": "s"}
    row.update(fields)
    return row


def _tool(tool_id, name, inp=None):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": inp or {}}]}}


def _user(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def _result():
    return {"type": "result", "session_id": "s", "result": "done"}


def _rec(data, task_id):
    return next(t for t in data["tasks"] if t["id"] == task_id)


_KEYS = {"id", "description", "kind", "type", "subagent_type", "tool_use_id",
         "status", "started_at", "ended_at", "backgrounded", "usage", "error",
         "summary", "events"}


def test_a_background_shell_runs_then_completes(agent, run_dir):
    _append(run_dir, [_tool("toolu_b", "Bash", {"command": "make", "run_in_background": True}),
                      _sys("task_started", task_id="b1", tool_use_id="toolu_b",
                           description="make", is_backgrounded=True,
                           task_type="local_bash")])
    data = agent._poll("run")
    rec = _rec(data, "b1")
    assert set(rec) == _KEYS
    assert rec["kind"] == "shell" and rec["type"] == "local_bash"
    assert rec["status"] == "running" and rec["ended_at"] is None
    assert rec["backgrounded"] is True and rec["tool_use_id"] == "toolu_b"
    assert data["activity"]["tasks"] == [{"id": "b1", "description": "make"}]
    assert data["tasks_pending"] is True

    _append(run_dir, [_sys("task_notification", task_id="b1",
                           status="completed", summary="make finished")])
    data = agent._poll("run")
    rec = _rec(data, "b1")
    assert rec["status"] == "completed" and rec["ended_at"] is not None
    assert rec["summary"] == "make finished" and rec["events"] == 1
    assert data["activity"]["tasks"] == [] and data["tasks_pending"] is False
    seg = data["segments"][0]
    assert seg["name"] == "Bash" and seg["task_id"] == "b1"


def test_an_agent_task_tracks_usage_and_status(agent, run_dir):
    _append(run_dir, [_tool("toolu_a", "Agent", {"description": "map", "subagent_type": "Explore"}),
                      _sys("task_started", task_id="a1", tool_use_id="toolu_a",
                           description="map", task_type="local_agent")])
    data = agent._poll("run")
    rec = _rec(data, "a1")
    assert rec["kind"] == "agent" and rec["subagent_type"] == "Explore"
    assert rec["usage"] is None

    _append(run_dir, [_sys("task_progress", task_id="a1",
                           usage={"total_tokens": 1200, "tool_uses": 3,
                                  "duration_ms": 4500})])
    rec = _rec(agent._poll("run"), "a1")
    assert rec["usage"] == {"total_tokens": 1200, "tool_uses": 3,
                            "duration_ms": 4500}

    _append(run_dir, [_sys("task_updated", task_id="a1",
                           patch={"status": "failed", "error": "boom"})])
    rec = _rec(agent._poll("run"), "a1")
    assert rec["status"] == "failed" and rec["error"] == "boom"
    assert rec["ended_at"] is not None


def test_a_subagent_type_alone_makes_an_agent(agent, run_dir):
    _append(run_dir, [_sys("task_started", task_id="x", description="d",
                           subagent_type="Plan")])
    assert _rec(agent._poll("run"), "x")["kind"] == "agent"


def test_monitor_line_notifications_keep_it_running(agent, run_dir):
    _append(run_dir, [_tool("toolu_m", "Monitor", {"command": "tail -f log"}),
                      _sys("task_started", task_id="m1", tool_use_id="toolu_m",
                           description="watch log"),
                      _sys("task_notification", task_id="m1", summary="line 1"),
                      _sys("task_notification", task_id="m1", status="running",
                           summary="line 2")])
    data = agent._poll("run")
    rec = _rec(data, "m1")
    assert rec["kind"] == "monitor"
    assert rec["status"] == "running" and rec["events"] == 2
    assert rec["summary"] == "line 2"
    # Re-reading the same window must not count the lines again.
    assert _rec(agent._poll("run"), "m1")["events"] == 2
    assert _rec(agent._poll("run"), "m1")["events"] == 2

    _append(run_dir, [_sys("task_notification", task_id="m1", summary="line 3")])
    assert _rec(agent._poll("run"), "m1")["events"] == 3
    _append(run_dir, [_sys("task_updated", task_id="m1", patch={"status": "killed"})])
    data = agent._poll("run")
    assert _rec(data, "m1")["status"] == "killed"
    assert data["activity"]["tasks"] == []


def test_monitor_task_type_alone_makes_a_monitor(agent, run_dir):
    _append(run_dir, [_sys("task_started", task_id="w", description="ws",
                           task_type="monitor_ws")])
    assert _rec(agent._poll("run"), "w")["kind"] == "monitor"


def test_an_untyped_task_is_other_and_a_bare_notification_ends_it(agent, run_dir):
    _append(run_dir, [_sys("task_started", task_id="o", description="?")])
    assert _rec(agent._poll("run"), "o")["kind"] == "other"
    _append(run_dir, [_sys("task_notification", task_id="o")])
    assert _rec(agent._poll("run"), "o")["status"] == "completed"


def test_paused_and_pending_still_count_as_pending_work(agent, run_dir):
    _append(run_dir, [_sys("task_started", task_id="p", description="p"),
                      _sys("task_updated", task_id="p", patch={"status": "paused"})])
    data = agent._poll("run")
    assert _rec(data, "p")["status"] == "paused"
    assert data["tasks_pending"] is True
    assert data["activity"]["tasks"] == [{"id": "p", "description": "p"}]


def test_background_tasks_changed_reconciles(agent, run_dir):
    _append(run_dir, [_sys("task_started", task_id="b1", description="one"),
                      _sys("task_started", task_id="b2", description="two"),
                      _sys("background_tasks_changed", tasks=[
                          {"task_id": "b2", "description": "two"},
                          {"task_id": "b3", "description": "three",
                           "task_type": "local_bash"}])])
    data = agent._poll("run")
    assert _rec(data, "b1")["status"] == "completed"
    assert _rec(data, "b3")["kind"] == "shell"
    assert data["activity"]["tasks"] == [{"id": "b2", "description": "two"},
                                         {"id": "b3", "description": "three"}]
    # Running first, then by start time; ended records stay.
    assert [t["id"] for t in data["tasks"]] == ["b2", "b3", "b1"]


def test_the_registry_survives_cursor_advances(agent, run_dir):
    _append(run_dir, [_user("start"), _sys("task_started", task_id="m1",
                                            description="watch",
                                            task_type="monitor_mcp"),
                      _sys("task_notification", task_id="m1", summary="l1"),
                      _result()])
    agent._poll("run")
    _append(run_dir, [_user("continue")])
    agent._poll("run")   # computes the cursor advance
    data = agent._poll("run")  # window now starts at "continue"
    rec = _rec(data, "m1")
    assert rec["status"] == "running" and rec["events"] == 1
    assert data["tasks_pending"] is True
    _append(run_dir, [_sys("task_notification", task_id="m1", summary="l2")])
    assert _rec(agent._poll("run"), "m1")["events"] == 2


def test_a_half_written_tail_row_is_not_persisted(agent, run_dir):
    _append(run_dir, [_sys("task_started", task_id="m1", description="w",
                           task_type="monitor_ws")])
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_sys("task_notification", task_id="m1", summary="x")))
    assert _rec(agent._poll("run"), "m1")["events"] == 1
    assert _rec(agent._poll("run"), "m1")["events"] == 1
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write("\n")
    assert _rec(agent._poll("run"), "m1")["events"] == 1


def test_the_old_flat_file_is_upgraded(agent, run_dir):
    (run_dir / "bg_tasks.json").write_text(json.dumps({"t1": "sleep 30"}))
    data = agent._poll("run")
    rec = _rec(data, "t1")
    assert set(rec) == _KEYS
    assert rec["description"] == "sleep 30" and rec["status"] == "running"
    assert data["activity"]["tasks"] == [{"id": "t1", "description": "sleep 30"}]
    stored = json.loads((run_dir / "bg_tasks.json").read_text())
    assert stored["t1"]["status"] == "running"


def test_history_carries_the_last_runs_tasks(agent, run_dir, tmp_path, monkeypatch):
    target = tmp_path / "proj"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps(
        {"file": str(target), "session_id": "s"}))
    _append(run_dir, [_sys("task_started", task_id="b1", description="bg",
                           task_type="local_bash")])
    agent._poll("run")
    monkeypatch.setattr(agent, "PROJECTS", str(tmp_path / "projects"))
    hist = agent._history(str(target), "s")
    assert [t["id"] for t in hist["tasks"]] == ["b1"]
    assert agent._history(str(target), "other")["tasks"] == []


# ------------------------------------------------------------- task_output

def test_task_output_with_no_live_host_is_an_error(agent, run_dir):
    assert "error" in agent._task_output("run", "b1")
    assert "error" in agent._task_output("../x", "b1")
    assert "error" in agent._task_output("run", "")


def _fake_host(agent, monkeypatch, run_dir, response):
    seen = {}
    monkeypatch.setattr(agent, "_host_alive", lambda _d: True)

    def write(rd, subtype, **fields):
        seen.update(subtype=subtype, **fields)
        return "req-1"

    def await_(rd, request_id, timeout=5.0, start_offset=0):
        seen["timeout"] = timeout
        return response
    monkeypatch.setattr(agent, "_write_control_request", write)
    monkeypatch.setattr(agent, "_await_control_response", await_)
    return seen


def test_task_output_maps_the_response(agent, run_dir, monkeypatch):
    seen = _fake_host(agent, monkeypatch, run_dir,
                      {"content": "hello\n", "status": "running"})
    res = agent.main("task_output", run_id="run", task_id="b1")
    assert res == {"output": "hello\n", "status": "running", "truncated": False}
    assert seen == {"subtype": "get_task_output", "task_id": "b1", "timeout": 10.0}


def test_task_output_keeps_the_tail_of_a_long_output(agent, run_dir, monkeypatch):
    big = "a" * 70000 + "THE END"
    _fake_host(agent, monkeypatch, run_dir, {"output": big})
    res = agent._task_output("run", "b1")
    assert res["truncated"] is True and res["status"] == ""
    assert len(res["output"].encode()) == 64 * 1024
    assert res["output"].endswith("THE END")


def test_task_output_tolerates_an_unknown_shape(agent, run_dir, monkeypatch):
    _fake_host(agent, monkeypatch, run_dir, {"weird": 1})
    assert agent._task_output("run", "b1") == {"output": "", "status": "",
                                                "truncated": False}
    _fake_host(agent, monkeypatch, run_dir, None)
    assert "error" in agent._task_output("run", "b1")

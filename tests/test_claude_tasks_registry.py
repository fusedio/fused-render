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
    assert stored["tasks"]["t1"]["status"] == "running"
    assert isinstance(stored["offset"], int)


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

    def await_(rd, request_id, timeout=5.0, start_offset=0, errors=None):
        seen["timeout"] = timeout
        if isinstance(response, Exception):
            if errors is not None:
                errors.append(str(response))
            return None
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


def test_task_output_passes_the_clis_refusal_through(agent, run_dir, monkeypatch):
    _fake_host(agent, monkeypatch, run_dir, RuntimeError("No task found with ID: b9"))
    assert agent._task_output("run", "b9") == {"error": "No task found with ID: b9"}


def test_await_control_response_reports_an_error_answer(agent, run_dir):
    _append(run_dir, [{"type": "control_response", "response": {
        "request_id": "r1", "subtype": "error", "error": "No task found"}}])
    errors = []
    assert agent._await_control_response(str(run_dir), "r1", timeout=0.2,
                                         errors=errors) is None
    assert errors == ["No task found"]


def test_task_output_has_its_own_lane():
    import importlib
    pool = importlib.import_module("fused_render.claude_agent.pool")
    lane = pool.pool_for("task_output")
    assert lane is not pool.POOL and lane is not pool.CONTROL_POOL
    assert pool.budget("task_output") == 15.0


# ------------------------------------------- review fixes: storage and revival

def test_the_registry_and_its_offset_are_one_file(agent, run_dir):
    _append(run_dir, [_sys("task_started", task_id="b1", description="x")])
    agent._poll("run")
    stored = json.loads((run_dir / "bg_tasks.json").read_text())
    assert set(stored) == {"offset", "tasks"}
    assert stored["offset"] == (run_dir / "out.jsonl").stat().st_size
    assert not (run_dir / "bg_tasks.offset").exists()


def test_the_transitional_two_file_layout_is_read(agent, run_dir):
    _append(run_dir, [_sys("task_started", task_id="m1", description="w",
                           task_type="monitor_ws"),
                      _sys("task_notification", task_id="m1", summary="l1")])
    size = (run_dir / "out.jsonl").stat().st_size
    rec = agent._new_task_record("m1", "w")
    rec.update(kind="monitor", events=1, type="monitor_ws")
    (run_dir / "bg_tasks.json").write_text(json.dumps({"m1": rec}))
    (run_dir / "bg_tasks.offset").write_text(str(size))
    data = agent._poll("run")
    assert _rec(data, "m1")["events"] == 1, "rows behind the old offset are not re-applied"
    assert not (run_dir / "bg_tasks.offset").exists()


def test_interleaved_polls_keep_every_record(agent, run_dir):
    """Poll A read the window before poll B absorbed a newer row; A's write
    must not drop B's record (the registry is re-read under the lock)."""
    _append(run_dir, [_sys("task_started", task_id="a", description="first")])
    ends_a = []
    rows_a, _ = agent._read_current_turn(str(run_dir), ends_a)
    _append(run_dir, [_sys("task_started", task_id="b", description="second")])
    agent._poll("run")                                    # B: sees a and b
    reg, _ = agent._update_run_registry(str(run_dir), rows_a, ends_a, {}, {})  # A, late
    assert set(reg) == {"a", "b"}
    data = agent._poll("run")
    assert {t["id"] for t in data["tasks"]} == {"a", "b"}


def test_concurrent_polls_from_threads_keep_every_record(agent, run_dir):
    import threading
    _append(run_dir, [_sys("task_started", task_id="t%d" % i, description="d")
                      for i in range(20)])
    errs = []

    def go():
        try:
            for _ in range(5):
                agent._poll("run")
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    assert len(agent._read_bg_tasks(str(run_dir))) == 20


@pytest.mark.parametrize("body", ["", '{"offset": 5, "tas'])
def test_an_unreadable_file_rebuilds_rather_than_wipes(agent, run_dir, body):
    _append(run_dir, [_sys("task_started", task_id="b1", description="x")])
    agent._poll("run")
    (run_dir / "bg_tasks.json").write_text(body)
    data = agent._poll("run")
    assert [t["id"] for t in data["tasks"]] == ["b1"]
    assert _rec(data, "b1")["status"] == "running"
    assert json.loads((run_dir / "bg_tasks.json").read_text())["tasks"]["b1"]


def test_an_ended_task_is_not_revived_by_a_non_terminal_patch(agent, run_dir):
    _append(run_dir, [_sys("task_started", task_id="t3", description="three"),
                      _sys("task_started", task_id="t4", description="four"),
                      _sys("background_tasks_changed", tasks=[
                          {"task_id": "t4", "description": "four"}]),
                      _sys("task_updated", task_id="t3", patch={"status": "paused"}),
                      _sys("task_notification", task_id="t3", status="running")])
    data = agent._poll("run")
    rec = _rec(data, "t3")
    assert rec["status"] == "completed" and rec["ended_at"] is not None
    assert data["activity"]["tasks"] == [{"id": "t4", "description": "four"}]
    # A terminal patch may still correct how it ended…
    _append(run_dir, [_sys("task_updated", task_id="t3", patch={"status": "killed"})])
    assert _rec(agent._poll("run"), "t3")["status"] == "killed"
    # …and a fresh listing brings it back.
    _append(run_dir, [_sys("background_tasks_changed", tasks=[
        {"task_id": "t3", "description": "three"}])])
    assert _rec(agent._poll("run"), "t3")["status"] == "running"


def test_history_marks_a_dead_runs_tasks_unknown(agent, run_dir, tmp_path, monkeypatch):
    target = tmp_path / "proj2"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target)}))
    (run_dir / "session").write_text("s")
    (run_dir / "bg_tasks.json").write_text(json.dumps({"old": "sleep 99"}))
    os.utime(run_dir / "bg_tasks.json", (1000.0, 1000.0))
    monkeypatch.setattr(agent, "PROJECTS", str(tmp_path / "projects"))
    agent._alive = lambda _d: False
    tasks = agent._history(str(target), "s")["tasks"]
    assert tasks[0]["status"] == "unknown" and tasks[0]["ended_at"] is None
    assert tasks[0]["started_at"] == 1000.0, "an upgraded record keeps a stable start"
    assert agent._history(str(target), "s")["tasks"][0]["started_at"] == 1000.0


def test_a_contended_lock_skips_the_write_and_holds_the_cursor(agent, run_dir, monkeypatch):
    """CodeRabbit, PR #1635: when another poll holds the registry lock past
    the retry budget, this poll answers from memory but writes neither the
    registry nor the poll cursor, so the next poll re-reads the same rows."""
    _append(run_dir, [_user("one"), {"type": "result", "result": "ok"},
                      _user("two"),
                      _sys("task_started", task_id="t1", description="d")])
    cursor_file = run_dir / "cursor"

    real = agent._RunLock

    class Contended(real):
        def __enter__(self):
            self.held_elsewhere = True
            return self

    monkeypatch.setattr(agent, "_RunLock", Contended)
    data = agent._poll("run")
    assert _rec(data, "t1")["status"] == "running", "this answer still applies the row"
    assert "t1" not in agent._read_bg_tasks(str(run_dir))
    assert not cursor_file.exists(), "the cursor is held back with the registry"

    monkeypatch.setattr(agent, "_RunLock", real)
    data = agent._poll("run")
    assert "t1" in agent._read_bg_tasks(str(run_dir))
    assert cursor_file.exists() and int(cursor_file.read_text()) > 0

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
         "summary", "events", "isolation", "model", "effort"}


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
    assert rec["isolation"] == ""
    assert rec["model"] == "" and rec["effort"] == ""
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


@pytest.mark.parametrize("host", [True, False])
def test_history_names_the_host_run_after_the_turn_ended(
        agent, run_dir, tmp_path, monkeypatch, host):
    """No open turn means no `live_run`, but a live host can still stop the
    task: history names its run (`tasks_run`) so Stop/Output can reach it."""
    target = tmp_path / "proj"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps(
        {"file": str(target), "session_id": "s"}))
    _append(run_dir, [_sys("task_started", task_id="b1", description="bg",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    if host:
        (run_dir / "host.json").write_text(json.dumps({"pid": os.getpid()}))
    monkeypatch.setattr(agent, "PROJECTS", str(tmp_path / "projects"))
    hist = agent._history(str(target), "s")
    assert hist["tasks"][0]["status"] == "running"
    assert hist["tasks_run"] == ("run" if host else "")


def _newer_run(agent, run_dir, meta, rows=(), host=False):
    """A second run dir for the same session, sorted after `run`."""
    d = run_dir.parent / "run2"
    (d / "perm").mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "out.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    if host:
        (d / "host.json").write_text(json.dumps({"pid": os.getpid()}))
    agent._poll("run2")
    return d


@pytest.mark.parametrize("newer_host", [True, False])
def test_history_finds_the_job_an_older_host_still_holds(
        agent, run_dir, tmp_path, monkeypatch, newer_host):
    """Akshil, 2026-10-19: one session, two hosts. The older one still runs
    the `sleep`/dev server; the newer one (the chat reopened on its folder)
    has nothing. The Tasks list counts the job — history must too, owned by
    the host that holds it so Stop/Output reach it."""
    target = tmp_path / "proj"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps(
        {"file": str(target / "index.html"), "session_id": "s"}))
    _append(run_dir, [_sys("task_started", task_id="b1", description="sleep 600",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    (run_dir / "host.json").write_text(json.dumps({"pid": os.getpid()}))
    newer = _newer_run(agent, run_dir, {"file": str(target), "session_id": "s"},
                       [_user("hi"), _result()], host=newer_host)
    monkeypatch.setattr(agent, "PROJECTS", str(tmp_path / "projects"))
    reg, host_run, last = agent._session_task_registry(str(target), "s")
    assert list(reg) == ["b1"] and reg["b1"]["status"] == "running"
    assert host_run == "run"
    assert last == str(newer), "the window size still comes off the newest run"
    hist = agent._history(str(target / "index.html"), "s")
    assert [t["id"] for t in hist["tasks"]] == ["b1"] and hist["tasks_run"] == "run"

    # The job ends and wakes that host's model: its open turn still answers.
    _append(run_dir, [_sys("task_notification", task_id="b1", status="killed")])
    reg, host_run, _last = agent._session_task_registry(str(target), "s")
    assert host_run == "run" and list(reg) == ["b1"]
    # Once that turn is over the older host holds nothing, so the newest run
    # answers again (the page's host watch on "run" carried the ending).
    _append(run_dir, [_result()])
    reg, host_run, _last = agent._session_task_registry(str(target), "s")
    assert reg == {} and host_run == ("run2" if newer_host else "")
    assert agent._read_bg_tasks(str(run_dir))["b1"]["status"] == "killed"


def test_a_held_job_past_the_scan_window_still_answers(agent, run_dir, tmp_path):
    """A dev server started hours ago sits behind `limit` newer run dirs; the
    Tasks list (120 deep) still counts it, so the chat looks twice as far."""
    target = tmp_path / "proj"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target), "session_id": "s"}))
    _append(run_dir, [_sys("task_started", task_id="b1", description="dev server",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    (run_dir / "host.json").write_text(json.dumps({"pid": os.getpid()}))
    other = tmp_path / "elsewhere"
    other.mkdir()
    _newer_run(agent, run_dir, {"file": str(other), "session_id": "x"})
    reg, host_run, last = agent._session_task_registry(str(target), "s", limit=1)
    assert list(reg) == ["b1"] and host_run == "run" and last == str(run_dir)
    run_dir.joinpath("host.json").unlink()
    assert agent._session_task_registry(str(target), "s", limit=1) == ({}, "", "")


def test_a_newest_run_whose_claude_died_yields_to_a_live_older_host(
        agent, run_dir, tmp_path, monkeypatch):
    target = tmp_path / "proj"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target), "session_id": "s"}))
    _append(run_dir, [_sys("task_started", task_id="old", description="o",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    (run_dir / "host.json").write_text(json.dumps({"pid": os.getpid()}))
    newer = _newer_run(agent, run_dir, {"file": str(target), "session_id": "s"},
                       [_sys("task_started", task_id="stale", description="n",
                             task_type="local_bash"), _result()], host=True)
    monkeypatch.setattr(agent, "_alive", lambda d: d != str(newer))
    reg, host_run, _last = agent._session_task_registry(str(target), "s")
    assert list(reg) == ["old"] and host_run == "run"


def test_an_older_host_with_nothing_running_does_not_answer(
        agent, run_dir, tmp_path, monkeypatch):
    target = tmp_path / "proj"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target), "session_id": "s"}))
    _append(run_dir, [_sys("task_started", task_id="old", description="done",
                           task_type="local_bash"),
                      _sys("task_notification", task_id="old", status="completed"),
                      _result()])
    agent._poll("run")
    (run_dir / "host.json").write_text(json.dumps({"pid": os.getpid()}))
    _newer_run(agent, run_dir, {"file": str(target), "session_id": "s"},
               [_sys("task_started", task_id="new", description="n",
                     task_type="local_bash"),
                _sys("task_notification", task_id="new", status="completed"),
                _result()])
    reg, host_run, _last = agent._session_task_registry(str(target), "s")
    assert list(reg) == ["new"] and host_run == ""


def test_the_newest_runs_own_live_work_wins(agent, run_dir, tmp_path, monkeypatch):
    target = tmp_path / "proj"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target), "session_id": "s"}))
    _append(run_dir, [_sys("task_started", task_id="old", description="o",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    (run_dir / "host.json").write_text(json.dumps({"pid": os.getpid()}))
    _newer_run(agent, run_dir, {"file": str(target), "session_id": "s"},
               [_sys("task_started", task_id="new", description="n",
                     task_type="local_bash"), _result()], host=True)
    reg, host_run, _last = agent._session_task_registry(str(target), "s")
    assert list(reg) == ["new"] and host_run == "run2"


def test_a_dead_older_claude_does_not_answer(agent, run_dir, tmp_path, monkeypatch):
    """A `host.json` whose pid was reused: `_alive` (the run's own `claude`)
    has to vouch too, as in `_run_tasks`."""
    target = tmp_path / "proj"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target), "session_id": "s"}))
    _append(run_dir, [_sys("task_started", task_id="old", description="o",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    (run_dir / "host.json").write_text(json.dumps({"pid": os.getpid()}))
    newer = _newer_run(agent, run_dir, {"file": str(target), "session_id": "s"},
                       [_user("hi"), _result()])
    monkeypatch.setattr(agent, "_alive", lambda d: d == str(newer))
    reg, host_run, _last = agent._session_task_registry(str(target), "s")
    assert reg == {} and host_run == ""


# ------------------------------------------- after the turn (Akshil 2026-10-18)

def test_tasks_action_catches_the_registry_up_while_the_host_runs_on(
        agent, run_dir, monkeypatch):
    """Nobody polls a run whose turn ended: `tasks` (and history) fold the
    rows the CLI wrote since, so a job that exited leaves the panel with its
    real status instead of reading `running` until the host dies."""
    _append(run_dir, [_sys("task_started", task_id="b1", description="sleep",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    monkeypatch.setattr(agent, "_host_alive", lambda _d: True)
    got = agent._run_tasks("run")
    assert got["host_alive"] is True and got["tasks_pending"] is True
    assert got["tasks"][0]["status"] == "running"
    _append(run_dir, [_sys("task_notification", task_id="b1", status="completed",
                           summary="done")])
    got = agent._run_tasks("run")
    assert got["tasks"][0]["status"] == "completed"
    assert got["tasks_pending"] is False
    # Persisted: the next reader (a poll, history) starts past those rows.
    assert agent._read_bg_tasks(str(run_dir))["b1"]["status"] == "completed"


def test_catch_up_stops_before_an_init_row_so_the_poll_still_reports_it(
        agent, run_dir, monkeypatch):
    _append(run_dir, [_sys("task_started", task_id="b1", description="sleep",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    _append(run_dir, [_sys("task_notification", task_id="b1", status="completed")])
    mid = os.path.getsize(run_dir / "out.jsonl")
    _append(run_dir, [_sys("init", slash_commands=["x"]),
                      _sys("task_started", task_id="b2", description="later")])
    agent._catch_up_registry(str(run_dir))
    reg, after, _cur = agent._load_registry(str(run_dir))
    # Absorbed up to the `init`, and not past it: the poll still gets that news.
    assert after == mid
    assert reg["b1"]["status"] == "completed" and "b2" not in reg


def test_history_never_catches_up_mid_turn(agent, run_dir, tmp_path, monkeypatch):
    """Mid-turn the poll owns the registry (only it has the tool inputs a
    fresh `task_started` takes its subagent fields from)."""
    target = tmp_path / "proj"
    target.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target), "session_id": "s"}))
    _append(run_dir, [_sys("task_started", task_id="b1", description="bg",
                           task_type="local_bash")])
    agent._poll("run")
    _reg, before, _cur = agent._load_registry(str(run_dir))
    monkeypatch.setattr(agent, "_host_alive", lambda _d: True)
    _append(run_dir, [_sys("task_notification", task_id="b1", status="completed")])
    agent._session_task_registry(str(target), "s")   # no `result` yet: turn open
    assert agent._load_registry(str(run_dir))[1] == before
    _append(run_dir, [_result()])
    reg, _host, _dir = agent._session_task_registry(str(target), "s")
    assert reg["b1"]["status"] == "completed"


def test_catch_up_folds_a_long_backlog_in_chunks(agent, run_dir, monkeypatch):
    _append(run_dir, [_sys("task_started", task_id="b1", description="sleep",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    monkeypatch.setattr(agent, "_REGISTRY_CATCH_UP_MAX", 200)
    _append(run_dir, [_user("x" * 300)] * 3
            + [_sys("task_notification", task_id="b1", status="completed")])
    for _ in range(10):
        agent._catch_up_registry(str(run_dir))
    assert agent._read_bg_tasks(str(run_dir))["b1"]["status"] == "completed"


def test_catch_up_never_wedges_behind_an_init_row(agent, run_dir, monkeypatch):
    """Bugbot (#1732): an `init` as the very next row used to stop every
    catch-up in front of it, so the job's end was never absorbed."""
    _append(run_dir, [_sys("task_started", task_id="b1", description="sleep",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    _append(run_dir, [_sys("init", slash_commands=["x"]),
                      _sys("task_notification", task_id="b1", status="completed")])
    monkeypatch.setattr(agent, "_host_alive", lambda _d: True)
    got = agent._run_tasks("run")
    assert got["tasks"][0]["status"] == "completed"
    assert got["tasks_pending"] is False


def test_tasks_action_with_the_host_gone_downgrades_and_says_so(agent, run_dir, monkeypatch):
    _append(run_dir, [_sys("task_started", task_id="b1", description="sleep",
                           task_type="local_bash"), _result()])
    agent._poll("run")
    monkeypatch.setattr(agent, "_host_alive", lambda _d: False)
    monkeypatch.setattr(agent, "_alive", lambda _d: False)
    got = agent._run_tasks("run")
    assert got["host_alive"] is False and got["tasks_pending"] is False
    assert got["tasks"][0]["status"] == "unknown"
    assert "error" in agent._run_tasks("../x")
    assert "error" in agent._run_tasks("nope")


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


# --------------------------------------------------------------- task_stop

def test_task_stop_rejects_bad_ids_and_a_dead_host(agent, run_dir):
    assert "error" in agent._task_stop("run", "b1")  # no live host
    assert "error" in agent._task_stop("../x", "b1")
    assert "error" in agent._task_stop("run", "")


def test_task_stop_writes_a_stop_task_request(agent, run_dir, monkeypatch):
    seen = _fake_host(agent, monkeypatch, run_dir, {})
    res = agent.main("task_stop", run_id="run", task_id="b1")
    assert res == {"ok": True}
    assert seen == {"subtype": "stop_task", "task_id": "b1", "timeout": 10.0}


def test_task_stop_passes_the_clis_refusal_through(agent, run_dir, monkeypatch):
    _fake_host(agent, monkeypatch, run_dir, RuntimeError("No task found with ID: b9"))
    assert agent._task_stop("run", "b9") == {"error": "No task found with ID: b9"}


def test_task_stop_has_its_own_lane():
    import importlib
    pool = importlib.import_module("fused_render.claude_agent.pool")
    assert pool.pool_for("task_stop") is pool.TASK_STOP_POOL
    assert pool.pool_for("task_stop") is not pool.pool_for("task_output")
    assert pool.budget("task_stop") == 15.0


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


def test_an_agent_rows_model_and_effort_come_from_its_call(agent, run_dir):
    _append(run_dir, [_tool("toolu_a", "Agent", {"description": "d", "model": "sonnet",
                                                 "effort": "medium", "subagent_type": "Explore"}),
                      _sys("task_started", task_id="a1", tool_use_id="toolu_a",
                           description="d", is_backgrounded=True,
                           task_type="local_agent")])
    rec = _rec(agent._poll("run"), "a1")
    assert rec["model"] == "sonnet" and rec["effort"] == "medium"

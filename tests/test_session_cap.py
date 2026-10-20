"""The global cap on concurrent Claude sessions (`fused_render/session_cap.py`)."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import schedule, session_cap, tasks_store
from fused_render.server import create_app
from fused_render.server.routers import tasks as tasks_router

FUSED = {"X-Fused": "1"}


class _Agent:
    """Just the three things `session_cap` asks of the agent module."""

    _LIVE_SCAN_LIMIT = 60

    def __init__(self, runs, hosts=None):
        self.RUNS = str(runs)
        self.hosts = hosts or {}

    @staticmethod
    def _pid_alive(pid):
        return pid.startswith("live")

    def _host_alive(self, run_dir):
        try:
            with open(os.path.join(run_dir, "host.json"), encoding="utf-8") as fh:
                return self._pid_alive(str(json.load(fh).get("pid")))
        except (OSError, ValueError):
            return False

    def _live_host(self, target, session_id=""):
        return {"run_id": self.hosts.get(session_id, "")}


@pytest.fixture()
def runs(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    root.mkdir()
    agent = _Agent(root)
    monkeypatch.setattr(session_cap, "_agent", lambda: agent)
    return agent


def _host(runs, name, pid):
    d = os.path.join(runs.RUNS, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "host.json"), "w", encoding="utf-8") as fh:
        json.dump({"pid": pid}, fh)
    return d


def _pid_only(runs, name, pid, age=0.0):
    d = os.path.join(runs.RUNS, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "pid"), "w", encoding="utf-8") as fh:
        fh.write(pid)
    old = time.time() - age
    os.utime(d, (old, old))
    return d


def _set_cap(monkeypatch, tmp_path, value):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    os.makedirs(tmp_path / "home", exist_ok=True)
    if value is not None:
        (tmp_path / "home" / "prefs.json").write_text(
            json.dumps({"claude_max_concurrent_sessions": value}))


def test_live_host_is_counted_and_dead_pid_is_not(runs):
    _host(runs, "20260101-000001-a", "live-1")
    _host(runs, "20260101-000002-b", "dead-2")
    assert session_cap.live_count() == 1


def test_young_pid_without_host_counts_but_an_old_one_does_not(runs):
    _pid_only(runs, "20260101-000003-c", "live-3", age=2)
    _pid_only(runs, "20260101-000004-d", "live-4", age=300)
    _pid_only(runs, "20260101-000005-e", "dead-5", age=2)
    assert session_cap.live_count() == 1


def test_no_agent_module_counts_nothing(monkeypatch):
    monkeypatch.setattr(session_cap, "_agent", lambda: None)
    assert session_cap.live_count() == 0


@pytest.mark.parametrize("stored,expected", [
    (None, 10), (0, 0), (4, 4), (64, 64), (65, 10), (-1, 10), (True, 10),
    ("8", 10)])
def test_cap_default_zero_and_junk(tmp_path, monkeypatch, stored, expected):
    _set_cap(monkeypatch, tmp_path, stored)
    assert session_cap.cap() == expected


def _entry(session="", due_in=-5, **extra):
    when = datetime.now(timezone.utc) + timedelta(seconds=due_in)
    return {"id": f"e{abs(hash((session, due_in, tuple(extra))))}",
            "state": schedule.PENDING, "due": when.isoformat(),
            "target": "/tmp/x/index.html", "session_id": session, **extra}


def test_waiting_lists_due_pending_entries_oldest_first(runs, tmp_path, monkeypatch):
    _set_cap(monkeypatch, tmp_path, 1)
    _host(runs, "20260101-000001-a", "live-1")
    late = _entry("s-late", due_in=-10)
    early = _entry("s-early", due_in=-60)
    future = _entry("s-future", due_in=600)
    sent = dict(_entry("s-sent"), state=schedule.SENT)
    now = datetime.now(timezone.utc)
    assert session_cap.waiting([late, early, future, sent], now) == [
        "s-early", "s-late"]


def test_waiting_skips_an_entry_with_a_live_host_and_is_empty_under_cap(
        runs, tmp_path, monkeypatch):
    _set_cap(monkeypatch, tmp_path, 1)
    _host(runs, "20260101-000001-a", "live-1")
    runs.hosts["s-has-host"] = "run-9"
    now = datetime.now(timezone.utc)
    assert session_cap.waiting([_entry("s-has-host")], now) == []
    _set_cap(monkeypatch, tmp_path, 5)
    assert session_cap.waiting([_entry("s-new")], now) == []


def _follower(leader_session="sess-L", **extra):
    """A queued follow-up: empty session_id, `follow_of` names its leader."""
    leader = dict(_entry("", due_in=-90), id="leader-1", state=schedule.SENT,
                  claude_session_id=leader_session)
    follower = dict(_entry("", due_in=-5), id="follower-1", follow_of="leader-1",
                    **extra)
    return leader, follower


def test_waiting_skips_a_follower_whose_conversation_has_a_live_host(
        runs, tmp_path, monkeypatch):
    _set_cap(monkeypatch, tmp_path, 1)
    _host(runs, "20260101-000001-a", "live-1")
    runs.hosts["sess-L"] = "run-9"
    leader, follower = _follower()
    now = datetime.now(timezone.utc)
    assert session_cap.waiting([leader, follower], now) == []
    runs.hosts.clear()
    assert session_cap.waiting([leader, follower], now) == ["sess-L"]


def test_held_follows_the_resolved_session_not_the_entry_field(
        runs, tmp_path, monkeypatch):
    _set_cap(monkeypatch, tmp_path, 1)
    _host(runs, "20260101-000001-a", "live-1")
    _, follower = _follower()
    assert session_cap.held(follower) is True
    runs.hosts["sess-L"] = "run-9"
    assert session_cap.held(follower) is True  # entry has no session_id of its own
    assert session_cap.held(follower, "sess-L") is False
    assert session_cap.has_live_host(follower, "sess-L") is True


def test_unlimited_never_holds(runs, tmp_path, monkeypatch):
    _set_cap(monkeypatch, tmp_path, 0)
    for i in range(5):
        _host(runs, f"20260101-00000{i}-a", f"live-{i}")
    assert session_cap.held(_entry("s-new")) is False


# ------------------------------------------------------ the scheduler seam


@pytest.fixture()
def sched(tmp_path, monkeypatch, runs):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(state))
    from fused_render import project_queue

    project_queue.reset_cache()
    (home / "prefs.json").write_text(json.dumps(
        {"claude_max_concurrent_sessions": 1}))
    page = tmp_path / "w"
    page.mkdir()
    (page / "index.html").write_text("<html></html>")
    sent = []
    monkeypatch.setattr(schedule, "_send", lambda entry: sent.append(entry))
    monkeypatch.setattr(schedule, "_watch_turn", lambda e, r: None)
    _host(runs, "20260101-000001-a", "live-1")
    return {"dir": str(page), "sent": sent, "runs": runs, "home": home}


def _create(sched, session="", **kw):
    when = datetime.now(timezone.utc) - timedelta(seconds=30)
    return schedule.create(sched["dir"], "go", when, session_id=session, **kw)


def test_dispatch_raises_busy_at_the_cap(sched):
    entry = _create(sched)
    with pytest.raises(schedule.SpawnBusy):
        schedule.dispatch_entry(entry["id"])
    assert sched["sent"] == []
    stored = {e["id"]: e for e in schedule.list_entries()}
    assert stored[entry["id"]]["state"] == schedule.PENDING


def test_dispatch_lets_a_follower_through_when_its_conversation_has_a_host(sched):
    leader = _create(sched)
    follower = _create(sched, follow_of=leader["id"])
    with schedule._lock:
        entries = schedule._read()
        for e in entries:
            if e["id"] == leader["id"]:
                e.update(state=schedule.SENT, turn="done", claude_session_id="sess-L")
        schedule._write(entries)
    with pytest.raises(schedule.SpawnBusy):
        schedule.dispatch_entry(follower["id"])
    sched["runs"].hosts["sess-L"] = "run-1"
    assert schedule.dispatch_entry(follower["id"]) is not None


def test_dispatch_proceeds_with_a_live_host_or_no_cap(sched, tmp_path, monkeypatch):
    sched["runs"].hosts["sess-1"] = "run-1"
    entry = _create(sched, session="sess-1")
    assert schedule.dispatch_entry(entry["id"]) is not None
    assert len(sched["sent"]) == 1
    (sched["home"] / "prefs.json").write_text(json.dumps(
        {"claude_max_concurrent_sessions": 0}))
    other = _create(sched)
    assert schedule.dispatch_entry(other["id"]) is not None
    assert len(sched["sent"]) == 2


def test_flag_off_tick_leaves_the_entry_pending_then_sends_when_a_slot_frees(sched):
    entry = _create(sched)
    assert schedule.tick() == []
    stored = {e["id"]: e for e in schedule.list_entries()}
    assert stored[entry["id"]]["state"] == schedule.PENDING
    os.remove(os.path.join(sched["runs"].RUNS, "20260101-000001-a", "host.json"))
    sent = schedule.tick()
    assert [e["id"] for e in sent] == [entry["id"]]


def test_cap_held_entry_lists_as_queued_with_a_position(sched):
    first = _create(sched)
    second = _create(sched)
    entries = schedule.list_entries()
    lines = tasks_router._queue_lines({}, time.time(),
                                      tasks_router._by_entry_id(entries))
    k1, k2 = (tasks_store.pending_key(first["id"]),
              tasks_store.pending_key(second["id"]))
    assert lines[k1]["position"] == 1 and lines[k1]["ahead_key"] == ""
    assert lines[k2]["position"] == 2 and lines[k2]["ahead_key"] == k1


def test_queue_lines_untouched_with_no_cap(sched):
    _create(sched)
    (sched["home"] / "prefs.json").write_text(json.dumps(
        {"claude_max_concurrent_sessions": 0}))
    entries = schedule.list_entries()
    assert tasks_router._queue_lines({}, time.time(),
                                     tasks_router._by_entry_id(entries)) == {}


# ------------------------------------------------------------- the free slot


def test_exited_wakes_the_scheduler_with_the_project_queue_off(
        tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    rung = []
    monkeypatch.setattr(schedule, "wake", lambda: rung.append(1))
    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.post("/api/tasks/queue/event", headers=FUSED,
                    json={"kind": "exited", "run_id": "r1", "session_id": "s1"})
    assert r.status_code == 200
    assert rung == [1]
    client.post("/api/tasks/queue/event", headers=FUSED,
                json={"kind": "turn_ended", "run_id": "r1", "session_id": "s1"})
    assert rung == [1]


def test_prefs_round_trip_for_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert client.get("/api/prefs").json()["claude_sessions"] == {"max_concurrent": 10}
    for bad in (65, -1, True, "8", 2.5):
        r = client.put("/api/prefs", json={"claude_max_concurrent_sessions": bad},
                       headers=FUSED)
        assert r.status_code == 400, bad
    r = client.put("/api/prefs", json={"claude_max_concurrent_sessions": 0},
                   headers=FUSED)
    assert r.status_code == 200
    assert client.get("/api/prefs").json()["claude_sessions"] == {"max_concurrent": 0}

"""tasks_watch: the Tasks page's change signal, and the endpoint that serves it.

Everything runs against a tmp `~/.claude`: `tick()` is called by hand with files
this test writes, never the thread. The signals are exercised one at a time — a
registry file, a live transcript growing, a send's own "a turn started here"
mark — and then the long-poll endpoint over them.
"""
import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import tasks_store, tasks_watch
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod

SID = "11111111-1111-1111-1111-111111111111"
SID2 = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    root = tmp_path / "claude"
    (root / "projects" / "-proj").mkdir(parents=True)
    (root / "sessions").mkdir()
    monkeypatch.setattr(tasks_watch, "SESSIONS_DIR", str(root / "sessions"))
    monkeypatch.setattr(tasks_store, "PROJECTS_DIR", str(root / "projects"))
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(state))
    monkeypatch.setattr(sessions_mod, "STATE_DIR", str(state))
    tasks_mod.reset_cache()
    yield root
    tasks_mod.reset_cache()


def _transcript(root, sid, lines=1, cwd="/proj"):
    path = root / "projects" / "-proj" / f"{sid}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for i in range(lines):
            f.write(json.dumps({
                "type": "user", "cwd": cwd,
                "timestamp": "2026-08-27T10:00:0%dZ" % (i % 10),
                "message": {"role": "user", "content": f"prompt {i}"},
            }) + "\n")
    return path


_STAMP = [time.time() + 10]


def _registry(root, sid, pid=None, status="busy", name="p"):
    row = {"pid": pid if pid is not None else os.getpid(), "sessionId": sid,
           "cwd": "/proj", "status": status, "updatedAt": 1787824059664}
    path = root / "sessions" / f"{name}.json"
    path.write_text(json.dumps(row), encoding="utf-8")
    # Two rewrites can land inside the clock's granularity (Windows time.time()
    # is ~15ms before 3.13), and the watcher would rightly see no change: stamp
    # each write one whole second later than the last.
    _STAMP[0] += 1
    os.utime(path, (_STAMP[0], _STAMP[0]))
    return path


# ------------------------------------------------------------------ the tick

def test_first_tick_is_a_baseline(claude_home):
    _registry(claude_home, SID)
    assert tasks_watch.tick() == set()
    assert tasks_watch.generation() == 0


def test_registry_file_appearing_changing_and_going(claude_home):
    tasks_watch.tick()
    path = _registry(claude_home, SID, status="busy")
    assert tasks_watch.tick() == {SID}
    assert tasks_watch.live_from_registry(SID) == (True, 1787824059.664)
    _registry(claude_home, SID, status="idle")
    assert tasks_watch.tick() == {SID}
    assert tasks_watch.live_from_registry(SID) == (False, 1787824059.664)
    path.unlink()
    assert tasks_watch.tick() == {SID}
    assert tasks_watch.live_from_registry(SID) == (False, 0.0)  # departed: known idle


def test_a_departed_session_is_known_idle_until_its_transcript_moves(claude_home):
    tasks_watch.tick()
    path = _registry(claude_home, SID, status="busy")
    tasks_watch.tick()
    path.unlink()
    tasks_watch.tick()
    # No process: not running, however fresh the tail looks.
    assert tasks_watch.live_from_registry(SID, transcript_mtime=0.0) == (False, 0.0)
    assert tasks_watch.live_from_registry(SID) == (False, 0.0)
    # Something appended after the process left: no opinion, let the tail rule.
    assert tasks_watch.live_from_registry(SID, transcript_mtime=time.time() + 5) is None
    # It comes back: the departure is forgotten.
    _registry(claude_home, SID, status="busy")
    tasks_watch.tick()
    assert tasks_watch.live_from_registry(SID, transcript_mtime=0.0) == (True, 1787824059.664)
    # Never seen at all: no opinion.
    assert tasks_watch.live_from_registry(SID2) is None


def test_registry_row_with_dead_pid_is_not_live(claude_home):
    tasks_watch.tick()
    _registry(claude_home, SID, pid=2 ** 22 + 12345)  # nobody's pid
    tasks_watch.tick()
    assert tasks_watch.live_from_registry(SID) is None  # never held, never departed
    # And it is not re-read (and re-announced) every tick.
    assert tasks_watch.tick() == set()


def test_a_crashed_claude_is_noticed_without_its_file_changing(claude_home, monkeypatch):
    tasks_watch.tick()
    _registry(claude_home, SID, status="busy")
    tasks_watch.tick()
    assert tasks_watch.live_from_registry(SID) == (True, 1787824059.664)
    # The process dies; the file is left exactly as it was.
    monkeypatch.setattr(tasks_watch, "_pid_alive", lambda pid: False)
    assert tasks_watch.tick() == {SID}
    assert tasks_watch.live_from_registry(SID) == (False, 0.0)
    assert tasks_watch.tick() == set()  # said once


def test_changes_listing_does_not_prune_current_apps(claude_home, monkeypatch):
    from fused_render import current_apps
    calls = []
    monkeypatch.setattr(current_apps, "observe", lambda rows: calls.append(len(rows)))
    _transcript(claude_home, SID)
    _transcript(claude_home, SID2)
    with TestClient(create_app(str(claude_home))) as client:
        # Startup warms the listing on a thread (app.py `_startup_tasks_warm`),
        # and that warm is a full listing, so it tells the desk once too. Let it
        # finish before counting, or the count depends on a race.
        client.app.state.tasks_warm.join(10)
        assert calls == [2]
        gen = client.get("/api/tasks").json()["generation"]
        assert calls == [2, 2]
        tasks_watch.notify({SID})
        client.get(f"/api/tasks/changes?since={gen}&wait=0")
        assert calls == [2, 2]  # the partial listing told the desk nothing


def test_registry_row_without_status_has_no_opinion(claude_home):
    tasks_watch.tick()
    (claude_home / "sessions" / "p.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": SID}), encoding="utf-8")
    tasks_watch.tick()
    assert tasks_watch.registry_row(SID) is not None
    assert tasks_watch.live_from_registry(SID) is None


def test_live_transcript_growth_is_news_and_dead_ones_are_not_watched(claude_home):
    live = _transcript(claude_home, SID)
    dead = _transcript(claude_home, SID2)
    _registry(claude_home, SID)
    tasks_watch.tick()
    _transcript(claude_home, SID2)  # nobody holds SID2: grows unseen
    assert tasks_watch.tick() == set()
    _transcript(claude_home, SID)
    assert tasks_watch.tick() == {SID}
    assert live.exists() and dead.exists()


def test_transcript_born_under_a_watched_session_is_news(claude_home):
    _registry(claude_home, SID)
    tasks_watch.tick()  # registry seen, no transcript yet
    _transcript(claude_home, SID)
    assert tasks_watch.tick() == {SID}


def test_notify_bumps_from_outside(claude_home):
    tasks_watch.tick()
    tasks_watch.notify({SID})
    assert tasks_watch.wait(0, 0) == (1, frozenset({SID}))


# ------------------------------------------------------------------ wait()

def test_wait_returns_union_since_and_full_when_too_far_back(monkeypatch):
    tasks_watch.tick()
    tasks_watch.notify({"a"})
    tasks_watch.notify({"b"})
    assert tasks_watch.wait(0, 0) == (2, frozenset({"a", "b"}))
    assert tasks_watch.wait(1, 0) == (2, frozenset({"b"}))
    assert tasks_watch.wait(2, 0.05) == (2, frozenset())  # timed out, nothing
    assert tasks_watch.wait(-1, 0) == (2, frozenset())  # handshake: here we are, no keys
    monkeypatch.setattr(tasks_watch, "RING", 2)
    for _ in range(3):
        tasks_watch.notify({"c"})
    # The deque was built at import with the real RING; emulate an overflow.
    while len(tasks_watch._changed) > 2:
        tasks_watch._changed.popleft()
    assert tasks_watch.wait(0, 0) == (5, None)


def test_wait_with_a_since_from_a_previous_process_asks_for_a_full_reload():
    tasks_watch.tick()
    tasks_watch.notify({"a"})
    assert tasks_watch.wait(99, 0) == (1, None)  # client ahead: reload now, no wait


def test_changes_names_the_pending_key_a_run_message_left_behind(claude_home, monkeypatch):
    from fused_render import schedule
    entry_id = "e1"
    entry = {"id": entry_id, "state": schedule.SENT, "session_id": SID,
             "created": "2026-08-27T09:00:00Z", "due": "2026-08-27T09:00:00Z",
             "target": "/proj", "message": "hi"}
    monkeypatch.setattr(schedule, "list_entries", lambda: [entry])
    monkeypatch.setattr(tasks_mod, "_entry_session", lambda e: SID)
    _transcript(claude_home, SID)
    with TestClient(create_app(str(claude_home))) as client:
        gen = client.get("/api/tasks").json()["generation"]
        tasks_watch.notify({SID})
        r = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
        assert [t["key"] for t in r["rows"]] == [SID]
        assert r["gone"] == [tasks_store.pending_key(entry_id)]


def test_wait_wakes_on_bump():
    tasks_watch.tick()
    out = {}

    def waiter():
        out["r"] = tasks_watch.wait(0, 5)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    tasks_watch.notify({SID})
    t.join(2)
    assert out["r"] == (1, frozenset({SID}))


# -------------------------------------------------------------- the endpoint

def test_changes_endpoint_returns_only_the_moved_rows(claude_home):
    _transcript(claude_home, SID)
    _transcript(claude_home, SID2)
    with TestClient(create_app(str(claude_home))) as client:
        full = client.get("/api/tasks").json()
        assert {t["key"] for t in full["tasks"]} == {SID, SID2}
        gen = full["generation"]
        tasks_watch.notify({SID})
        r = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
        assert r["generation"] == gen + 1
        assert [t["key"] for t in r["rows"]] == [SID]
        assert r["gone"] == []
        # Nothing since: an empty answer, same generation.
        r = client.get(f"/api/tasks/changes?since={gen + 1}&wait=0").json()
        assert r == {"generation": gen + 1, "rows": [], "gone": []}
        # Handshake: the generation, nothing else, no wait.
        r = client.get("/api/tasks/changes?since=-1&wait=0").json()
        assert r == {"generation": gen + 1, "rows": [], "gone": []}


def test_changes_endpoint_reports_a_deleted_task_as_gone(claude_home):
    _transcript(claude_home, SID)
    with TestClient(create_app(str(claude_home))) as client:
        gen = client.get("/api/tasks").json()["generation"]
        assert client.post("/api/tasks/delete", json={"key": SID}).json()["ok"]
        r = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
        assert r["rows"] == []
        assert r["gone"] == [SID]


def test_registry_status_decides_the_running_badge(claude_home):
    _transcript(claude_home, SID)  # timestamps in 2026: the tail says idle
    _registry(claude_home, SID, status="busy")
    tasks_watch.tick()
    with TestClient(create_app(str(claude_home))) as client:
        row = client.get("/api/tasks").json()["tasks"][0]
        assert row["live"] is True
        _registry(claude_home, SID, status="idle")
        tasks_watch.tick()
        row = client.get("/api/tasks").json()["tasks"][0]
        assert row["live"] is False


# ------------------------------------------------- the send's own running mark

def test_mark_running_announces_at_once_and_expires_by_itself(claude_home):
    tasks_watch.tick()
    tasks_watch.mark_running(SID, ttl_sec=60)
    assert tasks_watch.is_marked_running(SID)
    # The send is news the instant it is made: no tick in between.
    assert tasks_watch.wait(0, 0) == (1, frozenset({SID}))
    assert tasks_watch.tick() == set()  # nothing on disk moved
    # The window closing is news too — no byte on disk records it — and it is
    # said exactly once.
    tasks_watch._marks[SID] = time.time() - 1
    assert tasks_watch.tick() == {SID}
    assert not tasks_watch.is_marked_running(SID)
    assert tasks_watch.tick() == set()
    assert tasks_watch.is_marked_running("") is False
    tasks_watch.mark_running("")  # no id, no mark, no bump
    assert tasks_watch.wait(2, 0) == (2, frozenset())


def test_a_marked_send_reads_live_until_the_registry_disagrees(claude_home):
    """`claude -p` publishes its registry row two to four seconds after the
    send, so the listing called our own turns done for their first seconds
    (Akshil, 2026-09-15). The send's mark is the floor under that window; only
    a registry that says `busy` outranks it."""
    _transcript(claude_home, SID)  # rows stamped 2026: the tail rule says idle
    tasks_watch.tick()
    with TestClient(create_app(str(claude_home))) as client:
        was = client.get("/api/tasks").json()["tasks"][0]
        assert (was["live"], was["status"]) == (False, "done")

        tasks_watch.mark_running(SID, ttl_sec=60)
        now = client.get("/api/tasks").json()["tasks"][0]
        assert (now["live"], now["status"]) == (True, "in_progress")

        # A stale `idle` row from the turn before does NOT settle the row while
        # the mark is alive — that is the exact shape the bug wore.
        _registry(claude_home, SID, status="idle")
        tasks_watch.tick()
        assert client.get("/api/tasks").json()["tasks"][0]["live"] is True

        # `busy` is the same answer from a better source, and it outlives the
        # mark: the row stays live once the window closes.
        _registry(claude_home, SID, status="busy")
        tasks_watch.tick()
        tasks_watch._marks.pop(SID, None)
        assert client.get("/api/tasks").json()["tasks"][0]["live"] is True

        # …and with the mark gone, `idle` is authoritative again.
        _registry(claude_home, SID, status="idle")
        tasks_watch.tick()
        assert client.get("/api/tasks").json()["tasks"][0]["live"] is False


def test_running_endpoint_marks_the_session_and_wakes_the_long_poll(claude_home):
    _transcript(claude_home, SID)
    with TestClient(create_app(str(claude_home))) as client:
        gen = client.get("/api/tasks").json()["generation"]
        assert client.post("/api/tasks/running", json={"session_id": SID}).json() == {
            "ok": True, "session_id": SID}
        r = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
        assert [t["key"] for t in r["rows"]] == [SID]
        # THE WHOLE POINT: the row the poll hands back already wears the lane,
        # not just the flag under it.
        assert r["rows"][0]["live"] is True
        assert r["rows"][0]["status"] == "in_progress"
        assert client.post("/api/tasks/running", json={"session_id": "  "}).status_code == 400


# ------------------------------------------------ the turn ENDING, said aloud

def test_mark_idle_retires_the_mark_and_announces_it(claude_home):
    """The other half of the send. A mark is a fifteen-second floor, and a turn
    that ends in three seconds spends the other twelve wearing a running ring
    nothing on disk contradicts fast enough — so the page that sent it says
    when it ended, the same way it said when it started."""
    tasks_watch.tick()
    tasks_watch.mark_running(SID, ttl_sec=60)
    assert tasks_watch.is_marked_running(SID)
    gen, _keys = tasks_watch.wait(0, 0)

    tasks_watch.mark_idle(SID)
    assert not tasks_watch.is_marked_running(SID)
    assert tasks_watch.idle_at(SID) is not None
    # News at once, like the mark: no tick in between, and nothing left for the
    # next tick to expire.
    assert tasks_watch.wait(gen, 0) == (gen + 1, frozenset({SID}))
    assert tasks_watch.tick() == set()

    # A send after a stand-down is a new turn; the stand-down goes with it.
    tasks_watch.mark_running(SID, ttl_sec=60)
    assert tasks_watch.idle_at(SID) is None
    assert tasks_watch.is_marked_running(SID)

    tasks_watch.mark_idle("")  # no id, no stand-down, no bump
    assert tasks_watch.idle_at("") is None


def test_a_stand_down_runs_out_once_the_tail_is_stale_anyway(claude_home):
    tasks_watch.mark_idle(SID)
    assert tasks_watch.idle_at(SID) is not None
    tasks_watch._idle[SID] = time.time() - tasks_watch.IDLE_TTL_SEC - 1
    assert tasks_watch.idle_at(SID) is None


def test_the_registry_going_idle_after_busy_retires_the_mark(claude_home):
    """The safety net for a turn this app did not send, or a page that closed
    before it could say the turn ended: a registry row that was `busy` and is
    now not, AFTER the mark was placed, has said everything the mark was
    standing in for."""
    _transcript(claude_home, SID)
    tasks_watch.tick()
    tasks_watch.mark_running(SID, ttl_sec=60)
    _registry(claude_home, SID, status="busy")
    tasks_watch.tick()
    assert tasks_watch.is_marked_running(SID), "busy corroborates the mark"
    _registry(claude_home, SID, status="idle")
    assert SID in tasks_watch.tick()
    assert not tasks_watch.is_marked_running(SID)


def test_an_older_turns_row_departing_does_not_retire_a_fresh_mark(claude_home):
    """The flicker this rule must not cause. The previous turn's `claude -p` is
    still being reaped when the next send lands — its row goes away AFTER the
    new mark, and it was never busy on this mark's watch, so it has nothing to
    say about it."""
    _transcript(claude_home, SID)
    _registry(claude_home, SID, status="busy")
    tasks_watch.tick()
    _registry(claude_home, SID, status="idle")
    tasks_watch.tick()
    # …now the user sends again, before the new process has published anything.
    tasks_watch.mark_running(SID, ttl_sec=60)
    os.remove(os.path.join(tasks_watch.SESSIONS_DIR, "p.json"))
    tasks_watch.tick()
    assert tasks_watch.is_marked_running(SID), "a stale row's exit is not news"

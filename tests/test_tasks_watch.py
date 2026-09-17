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


def test_the_registry_answers_which_session_a_pid_is_holding(claude_home):
    """The reverse lookup the project queue needs to name a run that has not
    written its session down yet: the run dir carries the CLI's pid from the
    instant it spawns, and `sessions/<pid>.json` carries the id."""
    tasks_watch.reset()
    tasks_watch.tick()
    _registry(claude_home, SID, pid=os.getpid())
    tasks_watch.tick()
    assert tasks_watch.session_for_pid(os.getpid()) == SID
    assert tasks_watch.session_for_pid(str(os.getpid())) == SID
    assert tasks_watch.session_for_pid(2 ** 22 + 12345) == ""
    assert tasks_watch.session_for_pid("") == ""
    assert tasks_watch.session_for_pid("not-a-pid") == ""


def test_a_pid_is_looked_up_in_the_file_before_the_first_tick(claude_home):
    """A server seconds old has not ticked yet, and the answer is already on
    disk — the very file the tick would have read."""
    tasks_watch.reset()
    pid = os.getpid()
    (claude_home / "sessions" / f"{pid}.json").write_text(
        json.dumps({"pid": pid, "sessionId": SID2, "cwd": "/proj"}),
        encoding="utf-8")
    assert tasks_watch.session_for_pid(pid) == SID2


def test_a_registry_file_for_a_dead_pid_names_nobody(claude_home):
    """A crashed `claude` leaves its row behind; a pid nothing answers to is
    not a session, whatever the file says."""
    tasks_watch.reset()
    dead = 2 ** 22 + 12345
    (claude_home / "sessions" / f"{dead}.json").write_text(
        json.dumps({"pid": dead, "sessionId": SID2, "cwd": "/proj"}),
        encoding="utf-8")
    assert tasks_watch.session_for_pid(dead) == ""


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
    monkeypatch.setattr(tasks_mod, "_entry_session", lambda e, by_id=None: SID)
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
        # ...and the drafts half of the same answer: the key that moved holds
        # no draft, so it is reported gone rather than left unmentioned (see
        # `routers/tasks._draft_changes` — this list is noisy by construction).
        assert r["drafts"] == {"changed": [], "gone": [SID]}
        # Nothing since: an empty answer, same generation.
        empty = {"generation": gen + 1, "rows": [], "gone": [],
                 "drafts": {"changed": [], "gone": []}}
        r = client.get(f"/api/tasks/changes?since={gen + 1}&wait=0").json()
        assert r == empty
        # Handshake: the generation, nothing else, no wait.
        r = client.get("/api/tasks/changes?since=-1&wait=0").json()
        assert r == empty


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
    tasks_watch._marks[SID]["until"] = time.time() - 1
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
    # News at once, like the mark: no tick in between, and nothing left for the
    # next tick to expire.
    assert tasks_watch.wait(gen, 0) == (gen + 1, frozenset({SID}))
    assert tasks_watch.tick() == set()

    # A send after a stand-down is a new turn.
    tasks_watch.mark_running(SID, ttl_sec=60)
    assert tasks_watch.is_marked_running(SID)

    gen2 = tasks_watch.generation()
    tasks_watch.mark_idle("")  # no id, no stand-down, no bump
    assert tasks_watch.generation() == gen2


def test_last_idle_turn_is_capped_so_it_cannot_grow_forever(claude_home):
    """Nothing ever pops a `_last_idle_turn` entry outright — a session can
    always send one more `mark_running` to race against it — so without a cap
    it would keep one float per session ever seen for the server's whole
    lifetime (bugbot #4019069906). `_expire_marks` (run every `tick`) evicts
    anything old enough that nothing could still plausibly be racing against
    it."""
    now_ms = time.time() * 1000.0
    tasks_watch.mark_idle(SID, turn=now_ms)
    assert SID in tasks_watch._last_idle_turn

    # Still fresh: an expire pass leaves it alone.
    tasks_watch._expire_marks(time.time())
    assert SID in tasks_watch._last_idle_turn

    # Old enough that nothing could still be racing against it.
    tasks_watch._last_idle_turn[SID] = (
        now_ms - tasks_watch._LAST_IDLE_TURN_TTL_SEC * 1000.0 - 1000.0
    )
    tasks_watch._expire_marks(time.time())
    assert SID not in tasks_watch._last_idle_turn


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


# --------------------------------------- running and idle can race (bugbot #1163)

def test_a_running_mark_that_loses_the_race_to_its_own_idle_is_ignored(claude_home):
    """`noteTurnRunning` and `noteTurnIdle` are two independent fetches — nothing
    here orders their arrival — so a short turn's idle POST can reach the
    server before its own running POST does. Without the `turn` check that late
    running mark would read as a FRESH send and clear the stand-down `mark_idle`
    just made, leaving the row `in_progress` for the rest of `MARK_TTL_SEC`: the
    exact leftover ring `mark_idle` exists to prevent."""
    tasks_watch.mark_running(SID, ttl_sec=60, turn=100)
    assert tasks_watch.is_marked_running(SID)

    tasks_watch.mark_idle(SID, turn=200)
    assert not tasks_watch.is_marked_running(SID)

    # The running POST for THAT SAME turn (turn=100) finally lands, having lost
    # the race to the idle POST (turn=200) that already closed it. Its turn is
    # not newer than the idle it trails, so it is dropped whole: no mark, no
    # bump — the row stays idle.
    gen = tasks_watch.generation()
    tasks_watch.mark_running(SID, ttl_sec=60, turn=100)
    assert not tasks_watch.is_marked_running(SID)
    assert tasks_watch.generation() == gen, "a stale running mark must not bump"

    # A running mark for turn 200 itself — the idle's own value — is just as
    # stale (a turn already reported ended cannot also be reported starting).
    tasks_watch.mark_running(SID, ttl_sec=60, turn=200)
    assert not tasks_watch.is_marked_running(SID)

    # A GENUINELY NEW turn (a `turn` newer than the idle it follows) is not
    # stale, and marks running exactly as it would have with no `turn` at all.
    tasks_watch.mark_running(SID, ttl_sec=60, turn=201)
    assert tasks_watch.is_marked_running(SID)


def test_stale_idle_does_not_retire_a_newer_turns_mark(claude_home):
    """The reverse shape of the race above (bugbot #1163, round two):
    `noteTurnIdle` now AWAITS its own seat's running POST before firing, which
    delays the stand-down rather than ordering it — a FOLLOW-UP turn's
    `mark_running` can still land first. Without the `_mark_turns` check,
    turn 1's late idle would retire turn 2's mark, and a row a NEWER turn is
    still driving would read `done` until the registry caught up."""
    _transcript(claude_home, SID)  # rows stamped 2026: the tail rule says idle
    tasks_watch.tick()
    with TestClient(create_app(str(claude_home))) as client:
        tasks_watch.mark_running(SID, ttl_sec=60, turn=1)
        tasks_watch.mark_running(SID, ttl_sec=60, turn=2)
        assert tasks_watch.is_marked_running(SID)

        # Turn 1's idle finally lands, having lost the race to turn 2's own
        # running POST. It must not touch turn 2's still-live mark.
        gen = tasks_watch.generation()
        tasks_watch.mark_idle(SID, turn=1)
        assert tasks_watch.is_marked_running(SID), "turn 2's mark must outlive turn 1's idle"
        assert tasks_watch.generation() == gen, "a stale idle must not bump"
        row = client.get("/api/tasks").json()["tasks"][0]
        assert (row["live"], row["status"]) == (True, "in_progress")

        # Turn 2's own idle is not stale against itself and retires the mark.
        tasks_watch.mark_idle(SID, turn=2)
        assert not tasks_watch.is_marked_running(SID)
        row = client.get("/api/tasks").json()["tasks"][0]
        assert (row["live"], row["status"]) == (False, "done")


# --------------------------------- the manager's own word: the TURN has ended

def test_mark_turn_ended_retires_the_mark_and_outranks_a_stale_busy_row(claude_home):
    """The queue manager's word off `turn_ended`/`exited` (a `result` row it
    tailed) is sooner and more certain than anything this module infers on its
    own: a registry row already sitting at `busy` from BEFORE this call has
    nothing to say about a turn that has already ended."""
    tasks_watch.reset()
    tasks_watch.tick()
    tasks_watch.mark_running(SID, ttl_sec=60)
    _registry(claude_home, SID, status="busy")
    tasks_watch.tick()
    assert tasks_watch.is_marked_running(SID)
    assert tasks_watch.live_from_registry(SID) == (True, 1787824059.664)

    gen = tasks_watch.generation()
    tasks_watch.mark_turn_ended(SID, "run-1")
    assert not tasks_watch.is_marked_running(SID)
    # Announced at once, like every other write here — no tick in between.
    assert tasks_watch.wait(gen, 0) == (gen + 1, frozenset({SID}))

    # `_registry()` above deliberately stamps its files ahead of real time (see
    # its own comment — it is dodging the clock's granularity across rapid
    # successive writes, not modelling a real clock), so the real
    # `mark_turn_ended` call just above almost certainly landed at a `time.time()`
    # BEFORE that stamp. Pin the ended stamp to what it would be on a real
    # machine — strictly after the row it must outrank — so this test is about
    # the ordering rule and not a race against that fixture's own trick.
    tasks_watch._ended[SID] = tasks_watch._registry_mtime[SID] + 1.0
    assert tasks_watch.is_turn_ended(SID)
    assert tasks_watch.live_from_registry(SID) == (False, 1787824059.664)


def test_a_registry_row_rewritten_busy_after_the_end_stamp_re_enables(claude_home):
    """Only a REWRITE after the manager's word counts as new information — a
    genuinely later turn started on the same session."""
    tasks_watch.reset()
    tasks_watch._registry[SID] = {"pid": 1, "sessionId": SID, "status": "busy",
                                  "updatedAt": 1787824059664}
    tasks_watch._registry_mtime[SID] = 1000.0
    tasks_watch.mark_turn_ended(SID)
    assert tasks_watch.is_turn_ended(SID)
    assert tasks_watch.live_from_registry(SID)[0] is False

    ended_at = tasks_watch._ended[SID]
    tasks_watch._registry_mtime[SID] = ended_at + 5.0
    assert not tasks_watch.is_turn_ended(SID)
    assert tasks_watch.live_from_registry(SID) == (True, 1787824059.664)


def test_an_older_or_equal_registry_mtime_does_not_re_enable(claude_home):
    """The SAME turn's row, re-read unchanged or re-asserting the same status
    with no fresh write behind it, must not resurrect the ended stamp."""
    tasks_watch.reset()
    tasks_watch._registry[SID] = {"pid": 1, "sessionId": SID, "status": "busy",
                                  "updatedAt": 1787824059664}
    tasks_watch._registry_mtime[SID] = 1000.0
    tasks_watch.mark_turn_ended(SID)
    ended_at = tasks_watch._ended[SID]

    tasks_watch._registry_mtime[SID] = ended_at  # equal: not a rewrite
    assert tasks_watch.is_turn_ended(SID)
    assert tasks_watch.live_from_registry(SID)[0] is False

    tasks_watch._registry_mtime[SID] = ended_at - 1.0  # older still
    assert tasks_watch.is_turn_ended(SID)
    assert tasks_watch.live_from_registry(SID)[0] is False


# ------------------------- bugbot: ended mark clobbers overlapping turns


def test_a_newer_marks_own_turn_survives_an_older_turns_ended_event(claude_home):
    """A follow-up's `mark_running` (a NEWER turn) can land before an OLDER
    turn's `turn_ended` event does — the two are independent HTTP deliveries
    with no shared clock beyond the `at` each one carries. The newer mark must
    not be popped by news that, in truth, describes a turn before it."""
    tasks_watch.reset()
    tasks_watch.tick()
    t0 = time.time()
    tasks_watch.mark_running(SID, ttl_sec=60)  # the newer turn's own mark
    assert tasks_watch.is_marked_running(SID)

    # The OLDER turn's ended event, timestamped well before the mark above,
    # arrives late.
    gen = tasks_watch.generation()
    tasks_watch.mark_turn_ended(SID, "run-1", at=t0 - 5.0)
    assert tasks_watch.is_marked_running(SID), "the newer mark must survive"
    assert not tasks_watch.is_turn_ended(SID)
    assert tasks_watch.generation() == gen + 1, "still announced: `_ended` moved"


def test_a_registry_row_rewritten_busy_before_a_slow_ended_event_lands(claude_home):
    """The other overlap the bugbot named: the registry file itself was
    rewritten `busy` again — a genuinely later turn — strictly AFTER the
    moment the `turn_ended` event describes but before that (slower) HTTP
    request reaches this process. `is_turn_ended`'s own mtime check already
    guards this once `_ended` is stamped to the EVENT's `at` instead of
    arrival time; this pins the ordering down end to end."""
    tasks_watch.reset()
    tasks_watch._registry[SID] = {"pid": 1, "sessionId": SID, "status": "busy",
                                  "updatedAt": 1787824059664}
    t0 = time.time()
    tasks_watch._registry_mtime[SID] = t0 + 0.5  # rewritten AFTER the turn ended
    tasks_watch.mark_turn_ended(SID, at=t0)
    assert not tasks_watch.is_turn_ended(SID)
    assert tasks_watch.live_from_registry(SID) == (True, 1787824059.664)


def test_the_plain_case_still_ends(claude_home):
    """No overlap at all: the mark predates the ended event, no fresher
    registry row exists, and the turn reads ended, same as ever."""
    tasks_watch.reset()
    tasks_watch.tick()
    tasks_watch.mark_running(SID, ttl_sec=60)
    assert tasks_watch.is_marked_running(SID)

    tasks_watch.mark_turn_ended(SID, at=time.time() + 1.0)
    assert not tasks_watch.is_marked_running(SID)
    assert tasks_watch.is_turn_ended(SID)


def test_an_older_or_duplicate_turn_ended_event_never_moves_ended_backwards(
        claude_home):
    """Idempotent: a retried POST, or an `exited` describing the same edge a
    `turn_ended` already reported, must change nothing — not the stamp, not
    the generation."""
    tasks_watch.reset()
    t0 = time.time()
    tasks_watch.mark_turn_ended(SID, at=t0)
    assert tasks_watch._ended[SID] == t0

    gen = tasks_watch.generation()
    tasks_watch.mark_turn_ended(SID, at=t0 - 5.0)  # an older duplicate/retry
    assert tasks_watch._ended[SID] == t0, "must not move backwards"
    assert tasks_watch.generation() == gen, "an ignored event must not bump"

    tasks_watch.mark_turn_ended(SID, at=t0)  # an exact duplicate
    assert tasks_watch._ended[SID] == t0
    assert tasks_watch.generation() == gen


def test_mark_turn_ended_with_no_at_falls_back_to_arrival_time(claude_home):
    """A caller with nothing better — an old host, or a plain unit test —
    keeps the pre-`at` behaviour: the call's own `time.time()`."""
    tasks_watch.reset()
    before = time.time()
    tasks_watch.mark_turn_ended(SID)
    after = time.time()
    assert before <= tasks_watch._ended[SID] <= after


# --------------------------------------------- the card rings the queue


class _CardAgent:
    """The parts of agent.py the card watch reads: where the runs live, and
    where a run keeps its cards."""

    def __init__(self, runs):
        self.RUNS = str(runs)

    def _perm_dir(self, run_dir):
        return os.path.join(run_dir, "perm")

    def _session_from_out(self, run_dir):
        return ""


@pytest.fixture()
def carded(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    run_dir = runs / "20260916-120000-abcdef"
    (run_dir / "perm").mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"file": "/proj", "resumed_from": "", "session_id": SID}),
        encoding="utf-8")
    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: _CardAgent(runs))
    return run_dir


@pytest.fixture()
def rings(monkeypatch):
    """Every `schedule.wake` the tick fires. `_wake_schedule` imports the
    scheduler inside the call, so patching the module's attribute is enough."""
    from fused_render import schedule

    calls = []
    monkeypatch.setattr(schedule, "wake", lambda: calls.append(1))
    return calls


def test_a_card_rings_the_scheduler_instead_of_waiting_out_its_poll(
        claude_home, carded, rings):
    """Akshil's QA, 2026-09-16: a task queued behind a BLOCKED one took ~30s to
    start. A raised card parks the run, which frees the folder for whatever is
    queued behind it — but the only thing that knew was this loop, and it said
    nothing, so the queued task waited out the scheduler's 30-second poll.
    Answering the card is the same event in reverse."""
    tasks_watch.reset()
    _registry(claude_home, SID, status="busy")
    tasks_watch.tick()
    assert rings == [], "the baseline sighting of a run dir names nobody"

    tasks_watch.tick()
    assert rings == [], "nothing moved: no card, no ring"

    (carded / "perm" / "p1.req.json").write_text(
        json.dumps({"id": "p1", "tool": "Bash", "input": {}}), encoding="utf-8")
    assert tasks_watch.tick() == {SID}
    assert rings == [1], "raised: the folder is free, ring once"

    tasks_watch.tick()
    assert rings == [1], "the card is old news; a ring is not repeated"

    (carded / "perm" / "p1.decision.json").write_text(
        json.dumps({"decision": "allow"}), encoding="utf-8")
    tasks_watch.tick()
    assert rings == [1, 1], "answered: the folder is taken back, ring again"


def test_a_tick_with_no_card_does_not_ring(claude_home, carded, rings):
    """The ring is a hint the scheduler pays an early pass for, so it is only
    worth firing on the event itself: a transcript growing under a run that is
    still holding its folder changes nothing about the queue."""
    tasks_watch.reset()
    _registry(claude_home, SID, status="busy")
    _transcript(claude_home, SID)
    tasks_watch.tick()
    _transcript(claude_home, SID, lines=2)
    assert tasks_watch.tick() == {SID}, "the transcript grew"
    assert rings == []

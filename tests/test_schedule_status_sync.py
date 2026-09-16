"""The scheduler's per-session hold ends on the EVENT, not on the 30-second poll.

Three sync improvements that arrived with the project queue and are not the
queue (Akshil, 2026-09-16 — they run with the pref off, and there is no pref in
this file):

* `tasks_watch.tick` rings `schedule.wake()` the moment a session leaves busy,
  its registry row departs, or its pid dies.
* `schedule._verdict_echo`: a transcript whose newest rows are the closing rows
  of a turn THIS module already filed a verdict for is not a live turn, so a
  follow-up behind it goes on the next pass instead of waiting out the
  45-second window.
* `schedule._turn_ended` rings the loop and the Tasks long-poll when a watched
  turn closes.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from fused_render import claude_spawn, schedule, session_liveness, tasks_watch

_REAL_SESSION_LIVE = schedule._session_live
SID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A per-test store, as every other schedule test file has. Without it the
    xdist workers share one suite-wide FUSED_RENDER_HOME and one worker's
    "second" entry comes due in another's `tick`."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def no_timer_left_behind():
    """Holds on a live session arm a real `threading.Timer`; leave none behind
    for a later test to be rung by."""
    yield
    schedule._cancel_rearm()


@pytest.fixture()
def sid():
    """A session id of this test's own: `session_liveness` remembers where a
    session's transcript lives, so two tests sharing one id would read each
    other's files."""
    return str(uuid.uuid4())


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id="", **kw):
        calls.append({"target": target, "message": prompt,
                      "permission_mode": permission_mode,
                      "session_id": session_id})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


@pytest.fixture()
def folder(tmp_path):
    d = tmp_path / "alpha"
    d.mkdir()
    (d / "index.html").write_text("<html></html>")
    return d


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(session_liveness, "PROJECTS_DIR", str(d))
    monkeypatch.setattr(schedule, "_session_live", _REAL_SESSION_LIVE)
    return d


def _transcript(root, session_id, seconds_ago, last="assistant"):
    """Newest real record `seconds_ago` old. `last="user"` is a turn somebody
    opened; `"assistant"` is a reply that finished."""
    d = root / ("-encoded-" + session_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    role = "user" if last == "user" else "assistant"
    path.write_text(json.dumps({
        "type": role,
        "timestamp": _ago(seconds_ago).isoformat().replace("+00:00", "Z"),
        "message": {"role": role,
                    "content": [{"type": "text", "text": "words"}]}}) + "\n")
    return path


# ---- the echo rule ------------------------------------------------------------


def test_a_finished_turns_echo_does_not_hold_the_next_message(
        folder, spawned, transcripts, sid):
    """The transcript is 5 s warm — inside the 45 s window — but the newest rows
    are the closing rows of a turn this module already filed `ok` for. That is
    not a live turn, and the follow-up goes now."""
    done = schedule.create(str(folder), "first", _ago(60))
    schedule._update(done["id"], state=schedule.SENT, claude_session_id=sid,
                     turn="ok", turn_at=_ago(5).isoformat())
    _transcript(transcripts, sid, 5)
    later = schedule.create(str(folder), "second", _ago(30), session_id=sid)

    assert [e["id"] for e in schedule.tick()] == [later["id"]]


def test_a_turn_somebody_opened_is_never_an_echo(folder, spawned, transcripts, sid):
    """Same stamps, but the last word in the file is a USER row: a human started
    a turn after the verdict. Held, exactly as before."""
    done = schedule.create(str(folder), "first", _ago(60))
    schedule._update(done["id"], state=schedule.SENT, claude_session_id=sid,
                     turn="ok", turn_at=_ago(5).isoformat())
    _transcript(transcripts, sid, 2, last="user")
    schedule.create(str(folder), "second", _ago(30), session_id=sid)

    assert schedule.tick() == []


def test_only_a_watched_verdict_silences_the_transcript(folder, spawned, transcripts, sid):
    """`unknown` is stamped on every open turn at a restart — on a `claude` that
    is still writing — and `cancelled` lands while the process is closing its
    rows. Neither may call a live transcript an echo."""
    for verdict in ("unknown", "cancelled"):
        sid = str(uuid.uuid4())
        for e in schedule.list_entries():
            schedule._update(e["id"], state=schedule.CANCELLED)
        done = schedule.create(str(folder), "first", _ago(60))
        schedule._update(done["id"], state=schedule.SENT, claude_session_id=sid,
                         turn=verdict, turn_at=_ago(5).isoformat())
        _transcript(transcripts, sid, 5)
        schedule.create(str(folder), "second", _ago(30), session_id=sid)
        assert schedule.tick() == [], verdict


def test_a_held_live_turn_arms_a_timer_for_when_its_window_lapses(
        folder, spawned, transcripts, sid, monkeypatch):
    """A hold that ends on a CLOCK asks to be woken when the clock runs out,
    rather than on the 30-second poll."""
    # The store outlives a test (`home` is per run): nothing pending from an
    # earlier case may be what this tick sends.
    for e in schedule.list_entries():
        if e.get("state") == schedule.PENDING:
            schedule._update(e["id"], state=schedule.CANCELLED)
    armed = []
    monkeypatch.setattr(schedule, "_rearm", lambda delays: armed.append(list(delays)))
    _transcript(transcripts, sid, 5)          # live for another ~40 s
    schedule.create(str(folder), "second", _ago(30), session_id=sid)

    assert schedule.tick() == []
    assert armed and armed[-1] and 0 < armed[-1][0] <= session_liveness.RUNNING_WINDOW_SEC


# ---- the watcher rings the loop -----------------------------------------------


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    sessions = tmp_path / "claude" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(tasks_watch, "SESSIONS_DIR", str(sessions))
    tasks_watch.reset()
    yield sessions
    tasks_watch.reset()


@pytest.fixture()
def woke(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule, "wake", lambda: calls.append(1))
    return calls


def _row(sessions, status, stamp, name="p.json"):
    path = sessions / name
    path.write_text(json.dumps({"pid": os.getpid(), "sessionId": SID,
                                "cwd": "/proj", "status": status,
                                "updatedAt": int(stamp * 1000)}))
    os.utime(path, (stamp, stamp))
    tasks_watch.tick()


def test_a_session_leaving_busy_rings_the_scheduler(registry, woke):
    stamp = 1_800_000_000.0
    _row(registry, "busy", stamp)
    assert woke == []          # arriving busy frees nothing
    _row(registry, "idle", stamp + 1)
    assert woke


def test_a_departed_row_rings_the_scheduler(registry, woke):
    stamp = 1_800_000_000.0
    _row(registry, "busy", stamp)
    woke.clear()
    (registry / "p.json").unlink()
    tasks_watch.tick()
    assert woke


def test_a_dead_pid_rings_the_scheduler(registry, woke, monkeypatch):
    stamp = 1_800_000_000.0
    _row(registry, "busy", stamp)
    woke.clear()
    monkeypatch.setattr(tasks_watch, "_pid_alive", lambda pid: False)
    tasks_watch.tick()
    assert woke


# ---- a turn ending rings both bells ---------------------------------------------


def test_a_closed_turn_wakes_the_loop_and_rings_the_tasks_watcher(monkeypatch):
    woke, rung = [], []
    monkeypatch.setattr(schedule, "wake", lambda: woke.append(1))
    monkeypatch.setattr(tasks_watch, "notify", lambda keys: rung.append(set(keys)))
    schedule._turn_ended({"id": "e1", "session_id": SID})
    assert woke == [1]
    # Every key the listing might file this row under, since the ring cannot
    # know which one it chose: the input session and the not-yet-run row.
    assert rung == [{SID, "pending:e1"}]


def test_a_closed_turn_rings_the_key_the_listing_actually_used(monkeypatch):
    rung = []
    monkeypatch.setattr(schedule, "wake", lambda: None)
    monkeypatch.setattr(tasks_watch, "notify", lambda keys: rung.append(set(keys)))
    # A resume that forked: the listing files it under the ANSWER, not the input.
    schedule._turn_ended({"id": "e2", "session_id": SID, "claude_session_id": "forked"})
    assert rung == [{SID, "forked", "pending:e2"}]
    # A turn that ended before any session was captured: still a `pending:` row.
    rung.clear()
    schedule._turn_ended({"id": "e3"})
    assert rung == [{"pending:e3"}]


# ---- a send rings the page before the CLI does -----------------------------------


def test_a_claimed_send_rings_the_tasks_watcher_before_the_spawn(
        folder, spawned, monkeypatch):
    """The row is in progress from the claim; the page must not wait for the
    CLI's registry row (2-4 s) to hear so. Rung under every key the listing
    might file it by, since a fresh task has no session yet (`pending:`)."""
    rung = []
    monkeypatch.setattr(tasks_watch, "notify", lambda keys: rung.append(set(keys)))
    entry = schedule.create(str(folder), "go", _ago(30))
    assert schedule.tick() != []
    assert rung and rung[0] == {"pending:" + entry["id"]}
    assert spawned  # …and the ring came with the send, not instead of it


def test_a_spawn_failure_rings_the_tasks_watcher(folder, monkeypatch):
    rung = []
    monkeypatch.setattr(tasks_watch, "notify", lambda keys: rung.append(set(keys)))
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **k: {"error": "no claude"})
    entry = schedule.create(str(folder), "go", _ago(30))
    schedule.tick()
    assert {"pending:" + entry["id"]} in rung
    assert [e for e in schedule.list_entries() if e["id"] == entry["id"]][0]["state"] == "error"


def test_the_session_id_stamp_rings_the_old_and_new_keys(folder, monkeypatch):
    """First reporting tick names the session: the `pending:` row is now the
    session's row, and the page swaps them on one long-poll answer."""
    rung = []
    monkeypatch.setattr(tasks_watch, "notify", lambda keys: rung.append(set(keys)))
    monkeypatch.setattr(schedule, "_report", lambda *a, **k: None)
    entry = schedule.create(str(folder), "go", _ago(30))
    schedule._update(entry["id"], state=schedule.SENT, run_id="r-1")
    entry = [e for e in schedule.list_entries() if e["id"] == entry["id"]][0]

    class Agent:
        def _cancel(self, *a, **k): return None

    schedule._turn_tick(entry, "r-1", Agent(), {"session_id": SID})
    assert {SID, "pending:" + entry["id"]} in rung

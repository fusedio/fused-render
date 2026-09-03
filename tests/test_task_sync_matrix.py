"""The task lifecycle, watched from all three surfaces at once.

The Tasks board (`/api/tasks` → `_status`), the dock's queue half
(`schedule.queue()`), and the dock's job half (`jobs.list_jobs()`,
`sys:schedule:<id>` rows) describe the same run. These tests pin that they
AGREE at every stage of the lifecycle — create → queue → claim → spawn →
turn → verdict — because each reported desync (board Done while the dock
still says thinking, board stuck In Progress while the dock says finished)
is one of these stages answering differently on two surfaces.

Nothing here runs a real claude: the spawn is stubbed at claude_spawn's
`spawn_helper` seam, the watcher thread is stubbed out, and the watcher's
observations are driven by hand through `schedule._turn_tick`.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_spawn, jobs, schedule, schedule_wake
from fused_render import session_liveness, tasks_store
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod


# --------------------------------------------------------------- the harness


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(tasks_store, "PROJECTS_DIR", str(d))
    # The scheduler's own liveness read walks the same tree.
    monkeypatch.setattr(session_liveness, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "STATE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def clean_registries(monkeypatch):
    jobs.reset()
    schedule._events.clear()
    monkeypatch.setattr(schedule, "_event_seq", 0)
    monkeypatch.setattr(schedule, "_delivered", 0)
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)
    schedule._watched.clear()
    tasks_mod.reset_cache()
    sessions_mod._HEAD_CACHE.clear()
    yield
    jobs.reset()
    schedule._events.clear()
    schedule._watched.clear()
    tasks_mod.reset_cache()
    sessions_mod._HEAD_CACHE.clear()


@pytest.fixture()
def spawned(monkeypatch):
    """Sends that reached the helper. Watcher thread stubbed — the tests drive
    `_turn_tick` by hand (see test_schedule.py's `spawned` for why the thread
    body is the right seam)."""
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id="", **kw):
        calls.append({"target": target, "message": prompt,
                      "session_id": session_id})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path, state_dir):
    # Day-one baseline pre-stamped so unread math cannot mask a row.
    (state_dir / "read.json").write_text(json.dumps({tasks_store.INIT_KEY: 0.0}))
    return TestClient(create_app(start_dir=str(tmp_path)))


class DummyAgent:
    def __init__(self):
        self.cancelled = []

    def _cancel(self, run_id):
        self.cancelled.append(run_id)


def _now():
    return datetime.now(timezone.utc)


def _in(seconds):
    return _now() + timedelta(seconds=seconds)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _entry(entry_id=None):
    entries = schedule._read()
    if entry_id is None:
        assert len(entries) == 1, [e["id"] for e in entries]
        return entries[0]
    return next(e for e in entries if e["id"] == entry_id)


def _job(entry_id):
    rows = [j for j in jobs.list_jobs(mark_read=True) if j["id"] == "sys:schedule:" + entry_id]
    return rows[0] if rows else None


def _board(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    return r.json()["tasks"]


def _board_status(client, key):
    tasks_mod.reset_cache()
    rows = {t["key"]: t for t in _board(client)}
    assert key in rows, f"{key!r} not in {list(rows)}"
    return rows[key]["status"]


def _user_rec(text, ts, session_id, cwd):
    return {"type": "user", "timestamp": ts, "cwd": cwd,
            "sessionId": session_id,
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def _assistant_rec(text, ts, session_id, cwd):
    return {"type": "assistant", "timestamp": ts, "cwd": cwd,
            "sessionId": session_id,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _write_transcript(projects_dir, session_id, cwd, records, mtime=None):
    d = projects_dir / ("-enc-" + session_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _tick(now=None):
    return schedule.tick(now or _now())


# ============================================================ S1/S2 — create + queue


def test_pending_future_agrees_everywhere(client, target, spawned):
    """A message scheduled for later: board Upcoming, queue empty, no job."""
    entry = schedule.create(str(target), "future work", _in(3600))
    q = schedule.queue()
    assert q["queued"] == [] and q["running"] == []
    assert _job(entry["id"]) is None
    key = tasks_store.pending_key(entry["id"])
    assert _board_status(client, key) == "upcoming"


def test_pending_past_is_queued_and_still_upcoming_on_board(client, target, spawned):
    entry = schedule.create(str(target), "overdue work", _in(-120))
    q = schedule.queue()
    assert [e["id"] for e in q["queued"]] == [entry["id"]]
    key = tasks_store.pending_key(entry["id"])
    assert _board_status(client, key) == "upcoming"
    assert _job(entry["id"]) is None  # nothing runs until the tick


def test_tick_claims_past_due_within_one_pass(target, spawned):
    """The pickup guarantee: one tick at/after the due time sends it. Any
    latency past POLL_INTERVAL_S is a hold, and holds are separately tested."""
    schedule.create(str(target), "overdue", _in(-5))
    sent = _tick()
    assert len(sent) == 1
    assert _entry()["state"] == schedule.SENT
    assert len(spawned) == 1


def test_creating_past_due_work_rings_the_loop(target, spawned):
    """The pickup fix. `create` still does not send anything itself — the tick
    owns every state change — but it RINGS, so the loop's wait ends now instead
    of up to POLL_INTERVAL_S (30s) later. That plus the board's own poll was
    the minute a message asked for "now" spent reading Upcoming."""
    schedule._wake.clear()
    schedule.create(str(target), "overdue", _in(-5))
    assert schedule._wake.is_set()
    assert _entry()["state"] == schedule.PENDING  # the tick still does the work
    assert spawned == []


def test_scheduling_for_later_does_not_ring(target, spawned):
    """A ring is "something is due NOW". A future message must not wake the
    loop — it would tick, find nothing, and sleep again."""
    schedule._wake.clear()
    schedule.create(str(target), "tomorrow", _in(3600))
    assert not schedule._wake.is_set()


def test_a_past_anchored_repeat_rings_for_its_catch_up_run(target, spawned):
    """The ring is read AFTER materialization, so a repeat anchored in the past
    rings for the catch-up occurrence that pass just created — the template
    itself never fires and would ring nothing."""
    schedule._wake.clear()
    schedule.create(str(target), "daily, starting last week",
                    _in(-7 * 86400), rule={"freq": "day"})
    assert schedule._wake.is_set()


def test_queue_order_is_claim_order(target, spawned):
    a = schedule.create(str(target), "later overdue", _in(-10))
    b = schedule.create(str(target), "older overdue", _in(-600))
    q = schedule.queue()
    assert [e["id"] for e in q["queued"]] == [b["id"], a["id"]]
    _tick()
    # both fresh-session → both go, oldest first
    assert [c["message"] for c in spawned] == ["older overdue", "later overdue"]


def test_live_list_is_sent_without_turn_exactly(client, target, spawned):
    schedule.create(str(target), "will run", _in(-5))
    _tick()
    entry = _entry()
    live = [e for e in schedule.list_entries()
            if e.get("state") == schedule.SENT and not e.get("turn")]
    assert [e["id"] for e in live] == [entry["id"]]
    schedule._update(entry["id"], turn="ok", turn_at=_now().isoformat())
    live = [e for e in schedule.list_entries()
            if e.get("state") == schedule.SENT and not e.get("turn")]
    assert live == []


# ============================================================ S3 — holds


def test_same_session_pair_serializes_not_drops(target, spawned):
    schedule.create(str(target), "first", _in(-60), session_id="sess-1")
    schedule.create(str(target), "second", _in(-30), session_id="sess-1")
    _tick()
    assert [c["message"] for c in spawned] == ["first"]
    second = next(e for e in schedule._read() if e["message"] == "second")
    assert second["state"] == schedule.PENDING  # deferred, never dropped
    # verdict lands → next tick releases it
    first = next(e for e in schedule._read() if e["message"] == "first")
    schedule._update(first["id"], turn="ok", turn_at=_now().isoformat())
    _tick()
    assert [c["message"] for c in spawned] == ["first", "second"]


def test_hold_by_learned_session_of_a_fresh_send(target, spawned):
    """A fresh send occupies the session it GOT: once the watcher records
    claude_session_id, a follow-up naming that session must wait."""
    schedule.create(str(target), "first", _in(-60))
    _tick()
    first = _entry()
    schedule._update(first["id"], claude_session_id="sess-real")
    schedule.create(str(target), "second", _in(-30), session_id="sess-real")
    _tick()
    assert [c["message"] for c in spawned] == ["first"]


def test_hold_by_live_transcript_then_release(target, spawned, projects_dir):
    """The user typing in the conversation holds a scheduled send; the send
    goes once the transcript is quiet."""
    now = _now()
    _write_transcript(projects_dir, "sess-live", str(target),
                      [_user_rec("typing away", _iso(now), "sess-live",
                                 str(target))],
                      mtime=time.time())
    schedule.create(str(target), "queued behind human", _in(-60),
                    session_id="sess-live")
    _tick()
    assert spawned == []
    assert _entry()["state"] == schedule.PENDING
    # quiet: activity outside the 45s window
    old = time.time() - 300
    _write_transcript(projects_dir, "sess-live", str(target),
                      [_user_rec("typing away", _iso(now - timedelta(seconds=300)),
                                 "sess-live", str(target))],
                      mtime=old)
    _tick()
    assert [c["message"] for c in spawned] == ["queued behind human"]


def test_stuck_sending_is_reported_not_retried(target, spawned):
    schedule.create(str(target), "was mid-claim", _in(-60))
    now = _now()
    claimed = schedule._claim(_entry()["id"], now)
    assert claimed["state"] == schedule.SENDING
    # process "died" between claim and spawn; sweep past the stuck window
    _tick(now + timedelta(seconds=schedule._SENDING_STUCK_S + 30))
    entry = _entry()
    assert entry["state"] == schedule.ERROR
    assert "interrupted" in entry["error"]
    assert spawned == []  # never re-sent
    assert [e["kind"] for e in schedule.event_log()] == [schedule.EVENT_FAILED]


def test_unwatched_sent_is_closed_and_releases_the_session(target, spawned):
    """Process-death recovery: a `sent` entry nothing is watching gets
    `turn: unknown`, and the session it held goes free."""
    schedule.create(str(target), "orphaned", _in(-60), session_id="sess-9")
    _tick()
    assert _entry()["state"] == schedule.SENT
    schedule._watched.clear()  # simulate: the watching process died
    schedule.create(str(target), "follow-up", _in(-30), session_id="sess-9")
    _tick()  # sweep closes the orphan; follow-up still held this tick
    orphan = next(e for e in schedule._read() if e["message"] == "orphaned")
    assert orphan["turn"] == "unknown"
    _tick()
    assert [c["message"] for c in spawned] == ["orphaned", "follow-up"]


# ============================================================ S4 — execution


def test_spawn_failure_agrees_everywhere(client, target, monkeypatch):
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **k: {"error": "no claude installed"})
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    entry = schedule.create(str(target), "doomed", _in(-5))
    _tick()
    stored = _entry()
    assert stored["state"] == schedule.ERROR
    assert stored["error"] == "no claude installed"
    job = _job(entry["id"])
    assert job["state"] == "error"
    assert [e["kind"] for e in schedule.event_log()] == [schedule.EVENT_FAILED]
    key = tasks_store.pending_key(entry["id"])
    row = {t["key"]: t for t in _board(client)}[key]
    assert row["status"] == "failed" and row["failed"] is True


def test_sent_and_watched_job_row_is_running_and_cancellable(target, spawned):
    entry = schedule.create(str(target), "away", _in(-5))
    _tick()
    job = _job(entry["id"])
    assert job["state"] == "running"
    assert job["cancellable"] is True
    assert schedule._is_watched(entry["id"])


def test_turn_done_ok_all_surfaces(client, target, spawned):
    entry = schedule.create(str(target), "will finish", _in(-5))
    _tick()
    stored = _entry()
    schedule._turn_tick(dict(stored), "r-1", DummyAgent(),
                        {"session_id": "sess-ok", "done": True})
    stored = _entry()
    assert stored["turn"] == "ok" and stored["turn_at"]
    assert stored["claude_session_id"] == "sess-ok"
    job = _job(entry["id"])
    assert job["state"] == "done" and job["detail"] == "finished"
    assert [e["kind"] for e in schedule.event_log()] == [schedule.EVENT_DONE]
    q = schedule.queue()
    assert q["queued"] == [] and q["running"] == []
    assert _board_status(client, "sess-ok") == "done"


def test_turn_done_error_all_surfaces(client, target, spawned):
    entry = schedule.create(str(target), "will break", _in(-5))
    _tick()
    schedule._turn_tick(dict(_entry()), "r-1", DummyAgent(),
                        {"session_id": "sess-bad", "done": True,
                         "error": "tool exploded"})
    stored = _entry()
    assert stored["turn"] == "failed" and stored["error"] == "tool exploded"
    assert _job(entry["id"])["state"] == "error"
    assert [e["kind"] for e in schedule.event_log()] == [schedule.EVENT_FAILED]
    row = {t["key"]: t for t in _board(client)}["sess-bad"]
    assert row["status"] == "failed" and row["failed"] is True


def test_cancel_from_the_dock_all_surfaces(client, target, spawned):
    """The dock ✕: cancel_requested on the job row → agent._cancel → turn
    `cancelled`.

    A stop the user ASKED FOR is a settled outcome, so the lane is Done and
    nothing flies a red mark. What must not differ is the WORD: the dock and
    the schedule list say "Stopped", so the board's row carries the turn
    `cancelled` through rather than collapsing it into a plain finished run —
    the client's label reads "Stopped" off exactly this field."""
    entry = schedule.create(str(target), "stop me", _in(-5))
    _tick()
    jobs.request_cancel("sys:schedule:" + entry["id"])
    agent = DummyAgent()
    keep = schedule._turn_tick(dict(_entry()), "r-1", agent,
                               {"session_id": "sess-c", "phase": "working"})
    assert keep is False
    assert agent.cancelled == ["r-1"]
    stored = _entry()
    assert stored["turn"] == "cancelled"
    assert _job(entry["id"])["state"] == "cancelled"

    tasks_mod.reset_cache()
    row = {t["key"]: t for t in _board(client)}["sess-c"]
    assert row["status"] == "done"      # settled, not a fault
    assert row["failed"] is False
    # the word, which is what the two surfaces used to disagree about
    assert row["messages"][0]["turn"] == "cancelled"


def test_watch_ending_without_verdict_all_surfaces(client, target, spawned):
    entry = schedule.create(str(target), "goes dark", _in(-5))
    _tick()
    stored = _entry()
    schedule._update(stored["id"], claude_session_id="sess-dark")
    schedule._close_unwatched(dict(stored), "stopped reporting")
    stored = _entry()
    assert stored["turn"] == "unknown"
    assert _job(entry["id"])["state"] == "error"
    row = {t["key"]: t for t in _board(client)}["sess-dark"]
    assert row["status"] == "failed" and row["failed"] is True


def test_chain_writeback_teaches_template_and_successor(target, spawned):
    schedule.create(str(target), "daily", repeats="0 9 * * *")
    entries = schedule._read()
    template = next(e for e in entries if e["state"] == schedule.RECURRING)
    occurrence = next(e for e in entries if e.get("template_id"))
    schedule._chain_session(template["id"], "sess-learned")
    entries = schedule._read()
    template = next(e for e in entries if e["state"] == schedule.RECURRING)
    occurrence = next(e for e in entries if e.get("template_id"))
    assert template["session_id"] == "sess-learned"
    assert template["session_learned"] is True
    assert occurrence["session_id"] == "sess-learned"


# ============================================================ S5 — the sync matrix


def test_sent_before_first_report_is_not_done_on_board(client, target, spawned):
    """SYMPTOM 2's opening act. The window between the spawn and the watcher's
    first report: the entry is `sent`, `turn` is empty, but no
    claude_session_id has landed yet and no transcript exists. The turn is
    RUNNING — the board must say In Progress, not Done."""
    entry = schedule.create(str(target), "just spawned", _in(-5))
    _tick()
    assert _entry()["state"] == schedule.SENT
    assert _entry()["claude_session_id"] == ""
    assert _job(entry["id"])["state"] == "running"  # dock: thinking
    key = tasks_store.pending_key(entry["id"])
    status = _board_status(client, key)
    assert status == "in_progress", (
        f"board says {status!r} for a run the dock is showing as running — "
        "the spawn→first-report window reads as finished")


def test_sent_with_quiet_transcript_stays_in_progress(client, target, spawned,
                                                      projects_dir):
    """A long silent tool call: transcript stale (>90s), turn still open. The
    busy set (claude_session_id) must keep the board on In Progress."""
    schedule.create(str(target), "long tool call", _in(-5))
    _tick()
    stored = _entry()
    schedule._update(stored["id"], claude_session_id="sess-slow")
    old = time.time() - 300
    _write_transcript(projects_dir, "sess-slow", str(target),
                      [_user_rec("long tool call",
                                 _iso(_now() - timedelta(seconds=300)),
                                 "sess-slow", str(target))],
                      mtime=old)
    assert _board_status(client, "sess-slow") == "in_progress"


def test_verdict_suppresses_its_own_echo(client, target, spawned, projects_dir):
    """turn ok, transcript's newest records are the finished turn's own closing
    rows (within the echo window): board says Done immediately, not In
    Progress for the rest of the 45s liveness window."""
    schedule.create(str(target), "echo case", _in(-5))
    _tick()
    stored = _entry()
    now = _now()
    _write_transcript(projects_dir, "sess-echo", str(target),
                      [_user_rec("echo case", _iso(now - timedelta(seconds=8)),
                                 "sess-echo", str(target)),
                       _assistant_rec("did it", _iso(now - timedelta(seconds=2)),
                                      "sess-echo", str(target))],
                      mtime=time.time())
    schedule._turn_tick(dict(stored), "r-1", DummyAgent(),
                        {"session_id": "sess-echo", "done": True})
    assert _board_status(client, "sess-echo") == "done"


def test_transcript_past_echo_window_reads_in_progress(client, target, spawned,
                                                       projects_dir):
    """SYMPTOM 3's shape: verdict landed, dock shows finished, but the
    transcript got a real record >5s after turn_at — board flips back to
    In Progress for up to the 45s liveness window. Pinned as CURRENT behavior;
    whether 5s is wide enough for the CLI's real teardown is the question."""
    schedule.create(str(target), "late tail", _in(-5))
    _tick()
    stored = _entry()
    schedule._turn_tick(dict(stored), "r-1", DummyAgent(),
                        {"session_id": "sess-tail", "done": True})
    turn_at = tasks_store.epoch(_entry()["turn_at"])
    late = datetime.fromtimestamp(turn_at + 20, tz=timezone.utc)
    _write_transcript(projects_dir, "sess-tail", str(target),
                      [_user_rec("late tail", _iso(late - timedelta(seconds=30)),
                                 "sess-tail", str(target)),
                       _assistant_rec("closing rows", _iso(late),
                                      "sess-tail", str(target))],
                      mtime=late.timestamp())
    assert _job(_entry()["id"])["state"] == "done"  # dock: finished
    assert _board_status(client, "sess-tail") == "in_progress"


def test_busy_poisoning_by_an_orphan_holds_the_board(client, target, spawned,
                                                     projects_dir):
    """SYMPTOM 3, other route: an older `sent` entry with no verdict in the
    same conversation keeps the session in the busy set, so the board reads
    In Progress even after the newest run finished (dock: done)."""
    schedule.create(str(target), "old orphan", _in(-600), session_id="sess-p")
    _tick()
    orphan = _entry()
    schedule._update(orphan["id"], claude_session_id="sess-p")
    # its watcher is still alive (hung claude): sweep will not close it
    assert schedule._is_watched(orphan["id"])
    # a second entry into the same conversation, forced through run_now's seam:
    fresh = schedule.create(str(target), "fresh run", _in(-5),
                            session_id="sess-p")
    claimed = schedule._claim(fresh["id"], _now())
    assert claimed is not None
    schedule._send(claimed)
    schedule._turn_tick(dict(schedule._read()[1]), "r-2", DummyAgent(),
                        {"session_id": "sess-p", "done": True})
    fresh_stored = next(e for e in schedule._read() if e["message"] == "fresh run")
    assert fresh_stored["turn"] == "ok"
    assert _job(fresh["id"])["state"] == "done"  # dock: finished
    # board: sess-p still busy via the orphan → in_progress, indefinitely
    assert _board_status(client, "sess-p") == "in_progress"


def test_done_schedule_job_row_ages_out_after_read_not_kept_forever(target, spawned):
    """No dock draws a schedule row at all: `isScheduleJob`/`jobRows` excludes
    every `sys:schedule:*` id from what Activity draws, and `ActivityDock.tsx`
    applies `jobRows` before `terminalJobs`, so a schedule row never reaches
    Notifications either. D663's "every terminal state kept until dismissed"
    exemption only makes sense for a row some surface can show and let the
    user dismiss — a schedule row is neither, so it does not get that
    exemption: it ages out on the same read-gated `FINISHED_TTL_S` clock every
    terminal row used before D663, the readers of it being `fused.watchJob`
    and the Scheduled page's own poll, not a dock. Immediately after the
    first read the row is still there (the read is what starts the clock);
    `FINISHED_TTL_S` later it is gone."""
    entry = schedule.create(str(target), "watch me finish", _in(-5))
    _tick()
    schedule._turn_tick(dict(_entry()), "r-1", DummyAgent(),
                        {"session_id": "s", "done": True})
    assert _job(entry["id"])["state"] == "done"
    now = time.time()
    rows = jobs.list_jobs(now=now, mark_read=True)
    assert any(j["id"] == "sys:schedule:" + entry["id"]
               and j["state"] == "done" for j in rows)
    rows = jobs.list_jobs(now=now + jobs.FINISHED_TTL_S + 1)
    assert not any(j["id"] == "sys:schedule:" + entry["id"] for j in rows)


def test_error_schedule_job_row_ages_out_after_read_not_kept_forever(target, spawned):
    """Same rule as the `done` case above, for `error`: a schedule row's
    terminal state does not change whether some surface can show or dismiss
    it, so `error` gets no special exemption here either — only a
    page-reachable job's `error` row (D663) keeps the unconditional one."""
    entry = schedule.create(str(target), "fails loudly", _in(-5))
    _tick()
    schedule._turn_tick(dict(_entry()), "r-1", DummyAgent(),
                        {"session_id": "s", "done": True, "error": "boom"})
    now = time.time()
    rows = jobs.list_jobs(now=now, mark_read=True)
    assert any(j["id"] == "sys:schedule:" + entry["id"]
               and j["state"] == "error" for j in rows)
    rows = jobs.list_jobs(now=now + jobs.FINISHED_TTL_S + 1)
    assert not any(j["id"] == "sys:schedule:" + entry["id"] for j in rows)


def test_unread_schedule_job_row_has_the_longer_backstop(target, spawned):
    """A schedule row nobody has read yet is bounded by
    `FINISHED_UNREAD_DROP_S`, not the short `FINISHED_TTL_S` — the same
    unread-backstop rule every terminal row followed before D663, since
    nothing about a schedule row being excluded from every dock changes
    whether it might simply not have been read yet."""
    entry = schedule.create(str(target), "nobody reads me", _in(-5))
    _tick()
    schedule._turn_tick(dict(_entry()), "r-1", DummyAgent(),
                        {"session_id": "s", "done": True})
    # Read the raw record rather than `_job` (above), which itself calls
    # `list_jobs(mark_read=True)` and would start the very read-gated clock
    # this test means to leave un-started.
    finished = jobs._jobs["sys:schedule:" + entry["id"]].finished_at
    rows = jobs.list_jobs(now=finished + jobs.FINISHED_TTL_S + 1)
    assert any(j["id"] == "sys:schedule:" + entry["id"] for j in rows)
    rows = jobs.list_jobs(now=finished + jobs.FINISHED_UNREAD_DROP_S + 1)
    assert not any(j["id"] == "sys:schedule:" + entry["id"] for j in rows)


def test_parked_permission_is_named_on_the_job_row(target, spawned):
    entry = schedule.create(str(target), "needs approval", _in(-5))
    _tick()
    keep = schedule._turn_tick(dict(_entry()), "r-1", DummyAgent(),
                               {"permissions": [{"id": "p1", "decision": ""}],
                                "phase": "tooling", "tokens": 500})
    assert keep is True
    assert _job(entry["id"])["detail"] == "waiting for permission"


# ================================================= S5b — what must NOT change
#
# Trusting the store is scoped to SCHEDULED messages. A chat turn has no
# watcher behind it — nothing will ever write a verdict for it — so the
# transcript's own liveness is the only answer there is, and these pin that it
# still is.


def test_a_quiet_chat_task_is_not_running(client, projects_dir, target):
    """A pure chat task whose transcript has gone quiet is DONE, not running.
    If the store-authority rule leaked into chat messages, every conversation
    on the machine would read In Progress forever."""
    old = time.time() - 3600
    _write_transcript(projects_dir, "sess-chat", str(target),
                      [_user_rec("what is this",
                                 _iso(_now() - timedelta(seconds=3600)),
                                 "sess-chat", str(target))],
                      mtime=old)
    row = {t["key"]: t for t in _board(client)}["sess-chat"]
    assert row["live"] is False
    assert row["messages"][0]["turn"] == "idle"
    assert row["status"] == "done"


def test_a_live_chat_task_is_running(client, projects_dir, target):
    """And the other half: a transcript written to just now IS a live turn —
    the heuristic keeps its job where it is the only evidence available."""
    _write_transcript(projects_dir, "sess-chat", str(target),
                      [_user_rec("hello", _iso(_now()), "sess-chat",
                                 str(target))],
                      mtime=time.time())
    row = {t["key"]: t for t in _board(client)}["sess-chat"]
    assert row["status"] == "in_progress"
    assert row["messages"][0]["turn"] == ""


def test_a_scheduled_run_that_is_over_still_speaks(client, target, spawned,
                                                   projects_dir):
    """The verdict-None rule is for an OPEN turn only. A finished one still
    files the task — otherwise a task whose only run succeeded would fall
    through to `upcoming`/`done`-by-default rather than being reported."""
    schedule.create(str(target), "finished work", _in(-5))
    _tick()
    schedule._turn_tick(dict(_entry()), "r-1", DummyAgent(),
                        {"session_id": "sess-spoke", "done": True})
    row = {t["key"]: t for t in _board(client)}["sess-spoke"]
    assert row["status"] == "done"
    assert row["messages"][0]["turn"] == "done"


def test_deleting_a_task_mid_run_is_refused(client, target, spawned):
    """A CONSEQUENCE of trusting the store, named so it is not a surprise: a
    task holding a run the watcher has not closed reads In Progress, and
    delete refuses that (it would hide work that is still happening). The
    sweep resolves an abandoned run within one tick, so the refusal is
    bounded by the poll interval rather than permanent."""
    entry = schedule.create(str(target), "mid-flight", _in(-5))
    _tick()
    key = tasks_store.pending_key(entry["id"])
    tasks_mod.reset_cache()
    r = client.post("/api/tasks/delete", json={"key": key})
    assert r.status_code == 409
    assert "running" in r.json()["detail"]


# ============================================================ S6 — events


def test_events_emitted_once_and_ack_is_monotonic(target, spawned):
    schedule.create(str(target), "one event", _in(-5))
    _tick()
    schedule._turn_tick(dict(_entry()), "r-1", DummyAgent(),
                        {"session_id": "s", "done": True})
    events = schedule.undelivered_events()
    assert [e["kind"] for e in events] == [schedule.EVENT_DONE]
    schedule.ack_events(events[-1]["id"])
    assert schedule.undelivered_events() == []
    schedule.ack_events(0)  # replayed / out-of-order ack cannot re-arm
    assert schedule.undelivered_events() == []


def test_a_stop_from_the_chat_is_a_stop_on_the_board_too(client, target, spawned):
    """THE OTHER STOP BUTTON. The queue card's ✕ goes through the job registry,
    so the watcher knows the end was asked for and records `cancelled`. The
    CHAT's own Stop button calls agent._cancel directly — the scheduler never
    hears about it and only sees the kill's error on its next observation, which
    it files as a failed turn. Same act, same run, two verdicts: the chat says
    "Stopped." and the board flies a red Failed mark.

    The run's own cancel marker is what closes it: `_poll` reports `cancelled`
    (this branch added it for the chat), so the watcher can read the same fact
    the chat reads instead of inferring a crash from the error."""
    schedule.create(str(target), "stop me from the chat", _in(-5))
    _tick()
    stored = _entry()
    # what `_poll` returns for a run killed by agent._cancel: dead, with the
    # error the kill leaves behind, AND its own record of having been cancelled
    schedule._turn_tick(dict(stored), "r-1", DummyAgent(),
                        {"session_id": "sess-chatstop", "done": True,
                         "error": "claude exited before completing the reply",
                         "cancelled": True})
    entry = _entry()
    assert entry["turn"] == "cancelled", (
        f"a stop pressed in the chat is recorded as {entry['turn']!r} — the "
        "board reads that as Failed while the chat says Stopped")
    row = {t["key"]: t for t in _board(client)}["sess-chatstop"]
    assert row["status"] == "done" and row["failed"] is False
    assert row["messages"][0]["turn"] == "cancelled"

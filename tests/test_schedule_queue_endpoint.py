"""`GET /api/schedule/queue`: what is about to run or running now, server-side.

This used to also cover a frontend card built directly on top of this endpoint
(`QueueDock.tsx`), tested here by reading its source text. That card is gone —
folded into the Notifications/Activity merge and then deleted outright, "this
queue and notification thing should be same no? why duplicate popups? just
replace the queue -> thinking -> done" (Akshil, 2026-08-17) — but the endpoint
itself was not: `frontend/src/shell/Scheduled.tsx`'s calendar page still calls
`getScheduleQueue()` on its own poll, to mark queued/live entries on the thread
rows they ARE rather than draw a strip across the grid (see
`ScheduleCalendar.tsx`'s `queued`/`running` props). So this file now tests only
what is still live: the endpoint, against a real app.

What is in each of its three lists, and above all what is NOT: a message
scheduled for later today is not queued, it is scheduled ("show me the queued
that are like in the current time or past time, not future time"). The former
"component" half — source-text assertions against the deleted card — went with
the card; the row-level pure-function rules those tests also pinned
(`queue-dock-lib.ts`) were deleted the same way (D655). The general
structural/placement checks that used to sit in this same file (the fold is
never persisted, the bar's placement inside `#main`, always-present-not-gone)
outlived the card because they were never about the queue specifically — they
now live in `tests/test_activity_bar_structure.py`.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_spawn, jobs, schedule, schedule_wake
from fused_render.server import create_app

WRITE = {"X-Fused": "1"}


def _at(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """A store, a wake stub and a clean event log per test — the same isolation
    test_schedule_queue.py sets up, for the same process-global reasons."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE", raising=False)
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)
    schedule._events.clear()
    schedule._watched.clear()
    # The job registry is a process-global too, and a scheduled send writes to it —
    # so the half of the state this endpoint does NOT report has to be isolated too.
    jobs.reset()
    yield
    schedule._events.clear()
    schedule._watched.clear()
    jobs.reset()


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id="", **kw):
        calls.append({"message": prompt, "session_id": session_id})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


def test_the_dock_shows_past_due_only(client, target):
    """The line the whole feature turns on. Two messages, one overdue and one for
    later today: only the overdue one is queued, because "queued" means the
    scheduler is about to take it — not "scheduled at some point"."""
    overdue = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "overdue",
                                "due": _at(-600).isoformat()}).json()["entry"]
    client.post("/api/schedule", headers=WRITE,
                json={"target": str(target), "message": "this afternoon",
                      "due": _at(4 * 3600).isoformat()})

    body = client.get("/api/schedule/queue").json()
    assert [e["id"] for e in body["queued"]] == [overdue["id"]]
    assert [e["message"] for e in body["queued"]] == ["overdue"]


def test_a_live_turn_is_listed_with_what_a_link_needs(client, target, spawned):
    """`live` is what a client draws for a run already going, and it exists for
    one reason: a run parked on a permission prompt was visible in the job
    registry and unreachable, because a job row knows a title and a status line
    and not WHERE the session is. These entries carry the target and the session
    the turn landed in."""
    entry = schedule.create(str(target), "get me something", _at(-60))
    schedule.tick()

    body = client.get("/api/schedule/queue").json()
    live = body["live"]
    assert [e["id"] for e in live] == [entry["id"]]
    assert live[0]["target"] == str(target)
    assert live[0]["state"] == "sent"
    # and it is out of the queue: it has been claimed and spawned
    assert body["queued"] == []


def test_exactly_one_half_owns_the_run_at_each_step(client, target, spawned):
    """The queue and the job registry must not both report a run, and must not
    both skip it. The queue side is whatever `/api/schedule/queue` lists; the
    job side is `/api/jobs`. So the property to pin on the server side is which
    of the two even has a record at each step, and that they key on the same
    entry id.

        queued   — in `queued`, and NO job row yet (the row is written at spawn)
        live     — in `live`, and a `running` job row: the one overlap, and the
                   only step a consumer has to choose between the two. It
                   chooses by entry id, so the id in the queue list and the id
                   inside `sys:schedule:<id>` have to be the same string.
        finished — out of every queue list, and the job row is terminal: the
                   outcome report, and the only row left."""
    entry = schedule.create(str(target), "get me something", _at(-60))
    job_id = f"sys:schedule:{entry['id']}"

    # queued: the queue half alone
    body = client.get("/api/schedule/queue").json()
    assert [e["id"] for e in body["queued"]] == [entry["id"]]
    assert [j["id"] for j in client.get("/api/jobs").json()["jobs"]] == []

    # live: both have a record, and the id a consumer joins on is the same one
    schedule.tick()
    body = client.get("/api/schedule/queue").json()
    assert [e["id"] for e in body["live"]] == [entry["id"]]
    assert body["queued"] == [] and body["running"] == []
    live_job = [j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id]
    assert len(live_job) == 1
    assert live_job[0]["state"] == "running"
    # and it can really be stopped, which is what the row's ✕ promises
    assert live_job[0]["cancellable"] is True

    # finished: the queue half is out and the job row is the whole story
    schedule._update(entry["id"], turn="ok")
    schedule._report(entry["id"], state="done", detail="finished")
    body = client.get("/api/schedule/queue").json()
    assert (body["live"], body["queued"], body["running"]) == ([], [], [])
    done = [j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id]
    assert [j["state"] for j in done] == ["done"]


def test_a_finished_turn_leaves_the_dock(client, target, spawned):
    """`live` is work IN FLIGHT, not history. A turn that ended has a `turn`
    verdict written on it, and this endpoint reports what is about to happen —
    the job registry's row is where the outcome is reported."""
    entry = schedule.create(str(target), "already done", _at(-60))
    schedule.tick()
    schedule._update(entry["id"], turn="ok")

    assert client.get("/api/schedule/queue").json()["live"] == []


def test_the_only_overlap_the_server_leaves_is_the_mirror_image(client, target, spawned):
    """WHICH WAY the queue and the job registry can disagree, because it decides
    which side a consumer has to reconcile. `_watch_turn` writes the entry's
    `turn` verdict BEFORE reporting the job terminal, and `live` is "sent with
    no turn" — so between those two writes the entry is out of every queue list
    while its job row is still `running`. That is the mirror image, and it
    happens on every single run: a consumer sees the job row, not the queue
    entry, for that window — one row, with the ✕ that really stops the process.

    The other direction — a live queue entry whose job row is already terminal —
    is not a state the server passes through at all."""
    entry = schedule.create(str(target), "get me something", _at(-60))
    job_id = f"sys:schedule:{entry['id']}"
    schedule.tick()

    schedule._update(entry["id"], turn="ok")  # the verdict, before the job report
    body = client.get("/api/schedule/queue").json()
    assert (body["live"], body["queued"], body["running"]) == ([], [], [])
    mid = [j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id]
    assert [j["state"] for j in mid] == ["running"]
    assert mid[0]["cancellable"] is True  # and its stop is still real

    # and the job report lands second, which is when the row becomes the outcome
    schedule._report(entry["id"], state="done", detail="finished")
    after = [j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == job_id]
    assert [j["state"] for j in after] == ["done"]


def test_the_queue_read_stays_open_and_changes_nothing(client, target):
    """Merely LOOKING at the queue must not change what runs — the tick owns every
    state change. And the read is unguarded, like every other read."""
    client.post("/api/schedule", headers=WRITE,
                json={"target": str(target), "message": "overdue",
                      "due": _at(-600).isoformat()})
    before = schedule.list_entries()
    assert client.get("/api/schedule/queue").status_code == 200
    assert client.get("/api/schedule/queue").status_code == 200
    assert schedule.list_entries() == before

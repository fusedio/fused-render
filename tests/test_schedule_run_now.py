"""Run a pending scheduled message NOW (`POST /api/schedule/run-now`).

The Board lets a card be dragged from Upcoming to In Progress, and that gesture
has to mean something the store can do. What it means is: bring this one send
forward. Everything this file pins is about what run-now must NOT also do.

* **`due` does not move.** The obvious implementation rewrites it to now so the
  row "looks" consistent, and that destroys the only record of what was asked
  for. A message that ran early is a message that ran early: `due` then, `fired`
  now. It is the same split the Tasks side draws with `at` / `ran_at`, and it is
  what keeps the calendar chip on the day the user picked.
* **There is only one way to send.** Run-now reuses `_claim`, the single
  `pending -> sending` transition, so claim-before-spawn holds unchanged and a
  message cannot go twice however the two paths race.
* **A recurring rule is not disturbed.** Running one occurrence early leaves the
  template's `made`, its `due` and its arithmetic exactly where they were.
* **A refusal is honest.** Every non-pending state gets its own sentence,
  because "already sent" and "you cancelled this" are different news to someone
  who just dragged a card.

Frozen clocks; nothing here sleeps.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_spawn, schedule, schedule_wake
from fused_render.server import create_app

WRITE = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def unbounded(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE", raising=False)


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture(autouse=True)
def clean_event_log():
    schedule._events.clear()
    yield
    schedule._events.clear()


@pytest.fixture(autouse=True)
def fresh_process():
    schedule._watched.clear()
    yield
    schedule._watched.clear()


@pytest.fixture(autouse=True)
def nothing_is_live(monkeypatch):
    """No transcript on disk anywhere, so no session reads as mid-turn. The
    liveness hold has its own file (test_schedule_session_liveness.py); here it
    must not quietly decide anything."""
    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: False)


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id=""):
        calls.append({"message": prompt, "session_id": session_id})
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
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _at(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _entries():
    return {e["id"]: e for e in schedule.list_entries()}


# --------------------------------------------------------------- the model


def test_run_now_sends_a_pending_message_and_leaves_its_due_alone(target,
                                                                  spawned):
    """The headline contract. The message goes; the schedule time it was given
    is still the schedule time it has."""
    entry = schedule.create(str(target), "do it now", _at(3600))
    due = entry["due"]

    result = schedule.run_now(entry["id"])
    assert result["ok"] is True
    assert len(spawned) == 1
    assert spawned[0]["message"] == "do it now"

    stored = _entries()[entry["id"]]
    assert stored["state"] == schedule.SENT
    assert stored["due"] == due, "the ask is a fact and run-now does not edit it"
    assert stored["fired"], "when it ACTUALLY ran is the field that moved"
    assert schedule.parse_due(stored["fired"]) < schedule.parse_due(stored["due"])


def test_run_now_cannot_send_the_same_message_twice(target, spawned):
    """Claim-before-spawn, reached from the new door. The second call finds the
    entry already `sending`/`sent` and refuses; nothing spawns again."""
    entry = schedule.create(str(target), "once", _at(3600))
    assert schedule.run_now(entry["id"])["ok"] is True

    again = schedule.run_now(entry["id"])
    assert again["ok"] is False
    assert again["found"] is True
    assert "already sent" in again["reason"]
    assert len(spawned) == 1


def test_a_tick_after_run_now_does_not_send_it_again(target, spawned):
    """The other half of the same invariant: run-now leaves the entry in a state
    the sweep will not act on, so the two paths cannot both send it."""
    entry = schedule.create(str(target), "once", _at(-60))
    schedule.run_now(entry["id"])
    assert len(spawned) == 1

    assert schedule.tick(now=datetime.now(timezone.utc)) == []
    assert len(spawned) == 1


@pytest.mark.parametrize("state,expected", [
    (schedule.SENT, "already sent"),
    (schedule.SENDING, "already sending"),
    (schedule.CANCELLED, "cancelled"),
    (schedule.MISSED, "already missed"),
])
def test_run_now_refuses_a_non_pending_message_with_a_reason(target, spawned,
                                                             state, expected):
    """One sentence per state, not one "not pending" for all of them."""
    entry = schedule.create(str(target), "x", _at(3600))
    schedule._update(entry["id"], state=state)

    result = schedule.run_now(entry["id"])
    assert result["ok"] is False
    assert result["found"] is True
    assert expected in result["reason"]
    assert spawned == []
    assert _entries()[entry["id"]]["state"] == state


def test_run_now_refuses_a_recurring_template(target, spawned):
    """A template is never sent — its occurrences are. Saying so is more use
    than a bare "not pending"."""
    template = schedule.create(str(target), "daily", due=_at(3600),
                               rule={"freq": "day"})
    result = schedule.run_now(template["id"])
    assert result["ok"] is False
    assert "repeating schedule" in result["reason"]
    assert spawned == []


def test_run_now_on_an_unknown_id_is_not_found(target):
    result = schedule.run_now("no-such-entry")
    assert result["ok"] is False
    assert result["found"] is False
    assert "no scheduled message" in result["reason"]


def test_run_now_holds_off_a_conversation_that_is_mid_turn(target, spawned,
                                                           monkeypatch):
    """The one refusal that is not about state. Two `claude --resume` processes
    on one transcript is the hazard the liveness check exists for, and a drag
    gesture cannot consent to it — so the entry stays PENDING and the ordinary
    tick sends it when the conversation goes quiet."""
    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: session == "sess-live")
    entry = schedule.create(str(target), "later please", _at(3600),
                            session_id="sess-live")

    result = schedule.run_now(entry["id"])
    assert result["ok"] is False
    assert "turn running right now" in result["reason"]
    assert spawned == []
    assert _entries()[entry["id"]]["state"] == schedule.PENDING


# ------------------------------------------------- recurring: one run, early


def test_running_one_occurrence_early_does_not_move_the_rule(target, spawned):
    """The occurrence goes now; the template's budget, its `due` and the
    interval its successor is computed from are all untouched."""
    template = schedule.create(str(target), "daily", due=_at(3600),
                               rule={"freq": "day"})
    stored = _entries()
    occurrence = next(e for e in stored.values()
                      if str(e.get("template_id") or "") == template["id"])
    made_before = stored[template["id"]]["made"]
    due_before = stored[template["id"]]["due"]

    assert schedule.run_now(occurrence["id"])["ok"] is True
    assert len(spawned) == 1

    after = _entries()
    assert after[occurrence["id"]]["due"] == occurrence["due"]
    assert after[template["id"]]["made"] == made_before
    assert after[template["id"]]["due"] == due_before

    # The successor is a day after the occurrence's own (unmoved) due time —
    # exactly where it would have been had the run happened at its own minute.
    schedule.tick(now=datetime.now(timezone.utc))
    following = [e for e in _entries().values()
                 if str(e.get("template_id") or "") == template["id"]
                 and e["state"] == schedule.PENDING]
    assert len(following) == 1
    step = (schedule.parse_due(following[0]["due"])
            - schedule.parse_due(occurrence["due"]))
    assert step == timedelta(days=1)


# ----------------------------------------------------------------- the route


def test_the_route_sends_and_answers_with_the_entry(client, target, spawned):
    entry = schedule.create(str(target), "go", _at(3600))
    r = client.post("/api/schedule/run-now", json={"entry_id": entry["id"]},
                    headers=WRITE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["entry"]["id"] == entry["id"]
    assert body["entry"]["state"] == schedule.SENT
    assert body["entry"]["due"] == entry["due"]
    assert len(spawned) == 1


def test_the_route_answers_409_with_the_reason_for_a_non_pending_entry(
        client, target, spawned):
    """409, not 404: the entry exists and the user needs to be told which of the
    several ways it cannot run applies to it."""
    entry = schedule.create(str(target), "go", _at(3600))
    schedule.cancel(entry["id"])

    r = client.post("/api/schedule/run-now", json={"entry_id": entry["id"]},
                    headers=WRITE)
    assert r.status_code == 409, r.text
    assert "cancelled" in r.json()["error"]
    assert spawned == []


def test_the_route_answers_404_for_an_id_that_is_not_there(client, target):
    r = client.post("/api/schedule/run-now", json={"entry_id": "nope"},
                    headers=WRITE)
    assert r.status_code == 404
    assert "no scheduled message" in r.json()["error"]


def test_the_route_needs_an_entry_id(client):
    r = client.post("/api/schedule/run-now", json={}, headers=WRITE)
    assert r.status_code == 400
    assert "entry_id" in r.json()["error"]


def test_the_route_carries_the_d3_write_guard(client, target, spawned):
    """It starts an unattended agent turn on the spot — the most obvious member
    of the set the header guard exists for."""
    entry = schedule.create(str(target), "go", _at(3600))
    r = client.post("/api/schedule/run-now", json={"entry_id": entry["id"]})
    assert r.status_code == 403
    assert spawned == []
    assert _entries()[entry["id"]]["state"] == schedule.PENDING

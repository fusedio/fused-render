"""Recurring schedules — templates, materialization, and skip-not-catch-up.

The model under test is the occurrence-as-entry design: a `recurring` template
is never sent itself; `_materialize` keeps exactly one pending one-shot
occurrence ahead of it, and that occurrence flows through the ordinary firing
machinery. Spawns and the wake stub are stubbed exactly as in test_schedule.py.
"""
from datetime import datetime, timedelta, timezone

import pytest

from fused_render import claude_spawn, schedule, schedule_wake


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule_wake, "sync", lambda due: calls.append(list(due)))
    return calls


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id=""):
        calls.append({"target": target, "message": prompt,
                      "permission_mode": permission_mode,
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


def _entries():
    return {e["id"]: e for e in schedule.list_entries()}


def _occurrences(template_id):
    return [e for e in schedule.list_entries()
            if str(e.get("template_id") or "") == template_id]


# ----------------------------------------------------------------- creating

def test_create_with_repeats_stores_template_and_first_occurrence(target):
    template = schedule.create(str(target), "daily report", repeats="*/5 * * * *")

    assert template["state"] == schedule.RECURRING
    assert template["repeats"] == "*/5 * * * *"

    occurrences = _occurrences(template["id"])
    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence["state"] == schedule.PENDING
    assert occurrence["repeats"] == ""
    assert occurrence["max_late"] == schedule._OCCURRENCE_MAX_LATE_S
    # The occurrence is on the cron grid, in the future, and the template's own
    # `due` mirrors it.
    due = schedule.parse_due(occurrence["due"])
    assert due > datetime.now(timezone.utc)
    assert due.astimezone().minute % 5 == 0
    assert _entries()[template["id"]]["due"] == occurrence["due"]


def test_create_with_bad_cron_raises_and_stores_nothing(target):
    with pytest.raises(ValueError):
        schedule.create(str(target), "x", repeats="every day at nine")
    assert schedule.list_entries() == []


def test_repeats_ignores_due(target):
    # The cron line says when; a `due` alongside it is not an input.
    decoy = datetime(2030, 1, 1, tzinfo=timezone.utc)
    template = schedule.create(str(target), "x", due=decoy, repeats="0 * * * *")
    assert template["state"] == schedule.RECURRING
    # The stored due comes off the cron grid (the next hour), not the decoy.
    assert schedule.parse_due(template["due"]) < decoy
    assert schedule.parse_due(template["due"]).astimezone().minute == 0


# ------------------------------------------------------------------- firing

def test_fired_occurrence_is_followed_by_the_next_one(target, spawned):
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])

    fired = schedule.tick(now=first_due + timedelta(seconds=1))
    assert [e["id"] for e in fired] == [first["id"]]
    assert len(spawned) == 1

    # The successor appears on the NEXT pass (materialize runs before the
    # sweep, when `first` was still pending), due one grid step later.
    schedule.tick(now=first_due + timedelta(seconds=2))
    occurrences = _occurrences(template["id"])
    assert len(occurrences) == 2
    states = {o["id"]: o["state"] for o in occurrences}
    assert states[first["id"]] == schedule.SENT
    fresh = next(o for o in occurrences if o["id"] != first["id"])
    assert schedule.parse_due(fresh["due"]) == first_due + timedelta(minutes=5)


def test_overdue_occurrence_is_skipped_not_caught_up(target, spawned):
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])

    # Ten minutes late — far past the occurrence bound, well inside the global
    # day that would have SENT a one-shot.
    late = first_due + timedelta(minutes=10)
    fired = schedule.tick(now=late)
    assert fired == []
    assert spawned == []
    assert _entries()[first["id"]]["state"] == schedule.MISSED
    assert "skipped" in _entries()[first["id"]]["error"]

    # The replacement lands on the next pass, on the grid AFTER the gap —
    # nothing between first_due and `late` is offered again.
    schedule.tick(now=late)
    fresh = [o for o in _occurrences(template["id"])
             if o["state"] == schedule.PENDING]
    assert len(fresh) == 1
    assert schedule.parse_due(fresh[0]["due"]) > late


def test_one_shot_still_catches_up_a_day(target, spawned):
    # The tiny bound is the occurrence's alone; a plain message keeps the
    # day-long catch-up this feature shipped with.
    entry = schedule.create(str(target), "one shot",
                            datetime.now(timezone.utc) - timedelta(hours=2))
    fired = schedule.tick()
    assert [e["id"] for e in fired] == [entry["id"]]
    assert len(spawned) == 1


# ---------------------------------------------------------------- cancelling

def test_cancelling_template_cancels_its_pending_occurrence(target):
    template = schedule.create(str(target), "run", repeats="0 * * * *")
    occurrence = _occurrences(template["id"])[0]

    cancelled = schedule.cancel(template["id"])
    assert cancelled["id"] == template["id"]
    assert _entries()[template["id"]]["state"] == schedule.CANCELLED
    assert _entries()[occurrence["id"]]["state"] == schedule.CANCELLED

    # A cancelled template materializes nothing further.
    schedule.tick(now=schedule.parse_due(occurrence["due"]) + timedelta(hours=5))
    assert len(_occurrences(template["id"])) == 1


def test_cancelling_one_occurrence_skips_it_and_keeps_the_schedule(target):
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])

    schedule.cancel(first["id"])
    assert _entries()[template["id"]]["state"] == schedule.RECURRING

    schedule.tick(now=first_due - timedelta(minutes=1))
    fresh = [o for o in _occurrences(template["id"])
             if o["state"] == schedule.PENDING]
    assert len(fresh) == 1
    # The cancelled time itself is not re-offered.
    assert schedule.parse_due(fresh[0]["due"]) == first_due + timedelta(minutes=5)


# --------------------------------------------------------------- projections

def test_upcoming_projects_the_cron_grid(target):
    template = schedule.create(str(target), "run", repeats="0 * * * *")
    times = schedule.upcoming(template, horizon_days=1)
    assert 23 <= len(times) <= 25  # hourly over a day, DST edges allowed
    parsed = [schedule.parse_due(t) for t in times]
    assert parsed == sorted(parsed)
    assert all(t > datetime.now(timezone.utc) for t in parsed)
    assert all(t.astimezone().minute == 0 for t in parsed)


def test_upcoming_is_empty_for_non_recurring(target):
    entry = schedule.create(str(target), "x",
                            datetime.now(timezone.utc) + timedelta(hours=1))
    assert schedule.upcoming(entry) == []


# ------------------------------------------------------------- tampered store

def test_template_with_unreadable_cron_stops_loudly(target):
    template = schedule.create(str(target), "run", repeats="0 * * * *")
    occurrence = _occurrences(template["id"])[0]

    # A hand-edited store: the cron line no longer parses. Cancel the pending
    # occurrence so materialization has to consult the template again.
    schedule.cancel(occurrence["id"])
    import json
    path = schedule.store_path()
    with open(path) as fh:
        data = json.load(fh)
    for entry in data["entries"]:
        if entry["id"] == template["id"]:
            entry["repeats"] = "nonsense"
    with open(path, "w") as fh:
        json.dump(data, fh)

    schedule.tick()
    assert _entries()[template["id"]]["state"] == schedule.ERROR
    assert "recurring schedule stopped" in _entries()[template["id"]]["error"]
    events = schedule.event_log()
    assert any(e["kind"] == schedule.EVENT_FAILED
               and e["entry_id"] == template["id"] for e in events)


# ------------------------------------------------------------------ unskipping

def test_a_skipped_run_can_be_restored(target):
    template = schedule.create(str(target), "run", repeats="0 * * * *")
    occurrence = _occurrences(template["id"])[0]

    schedule.cancel(occurrence["id"])
    restored = schedule.restore(occurrence["id"])
    assert restored["id"] == occurrence["id"]
    assert _entries()[occurrence["id"]]["state"] == schedule.PENDING


def test_restore_refuses_what_is_not_a_future_skip(target):
    # A cancelled ONE-SHOT is not restorable — a skip names an exception to a
    # standing rule; a plain cancel has no rule to return to.
    one_shot = schedule.create(str(target), "x",
                               datetime.now(timezone.utc) + timedelta(hours=1))
    schedule.cancel(one_shot["id"])
    assert schedule.restore(one_shot["id"]) is None

    # Nor is a skip under a schedule that has since been cancelled outright.
    template = schedule.create(str(target), "run", repeats="0 * * * *")
    occurrence = _occurrences(template["id"])[0]
    schedule.cancel(occurrence["id"])
    schedule.cancel(template["id"])
    assert schedule.restore(occurrence["id"]) is None

    # Nor anything still pending.
    fresh = schedule.create(str(target), "run2", repeats="30 * * * *")
    assert schedule.restore(_occurrences(fresh["id"])[0]["id"]) is None


def test_restore_can_leave_two_pending_and_both_fire(target, spawned):
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])

    schedule.cancel(first["id"])
    # The materializer moves on to the next slot...
    schedule.tick(now=first_due - timedelta(minutes=1))
    # ...and an unskip then brings the first one back beside it.
    schedule.restore(first["id"])
    pending = [o for o in _occurrences(template["id"])
               if o["state"] == schedule.PENDING]
    assert len(pending) == 2

    schedule.tick(now=first_due + timedelta(seconds=1))
    schedule.tick(now=first_due + timedelta(minutes=5, seconds=1))
    states = {o["id"]: o["state"] for o in _occurrences(template["id"])}
    assert states[first["id"]] == schedule.SENT
    assert len(spawned) == 2


def test_upcoming_covers_the_full_horizon_for_hourly(target):
    # The cap must clear the horizon for the presets the form offers: hourly
    # over 14 days is 336 instants, and a 50-instant cap blanked the week
    # view two days out (Bugbot, PR #529).
    template = schedule.create(str(target), "run", repeats="0 * * * *")
    times = schedule.upcoming(template)
    assert len(times) >= 14 * 24 - 2
    last = schedule.parse_due(times[-1])
    assert last > datetime.now(timezone.utc) + timedelta(days=13)

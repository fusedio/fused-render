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
    # No per-occurrence late bound any more: the 120-second skip-not-catch-up
    # rule was replaced by coalescing (`_coalesce`), which sends the latest
    # missed run instead of discarding it.
    assert "max_late" not in occurrence
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


def test_an_overdue_backlog_coalesces_into_one_run(target, spawned):
    """UPDATED POLICY. This test used to assert that an overdue occurrence was
    marked `missed` and never sent — the 120-second `max_late` bound. That bound
    is gone; a missed recurring run is now COALESCED, so the latest one runs and
    the rest are counted.

    The half that survives unchanged is the important half: the backlog is never
    replayed. Three runs came due while the app was closed and exactly one send
    happens."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])

    # Ten minutes late: the grid holds runs at first_due, +5 and +10, of which
    # only ONE is in the store — the others exist in the cron line alone.
    late = first_due + timedelta(minutes=10)
    fired = schedule.tick(now=late)

    assert [e["id"] for e in fired] == [first["id"]]
    assert [c["message"] for c in spawned] == ["run"]
    ran = _entries()[first["id"]]
    assert ran["state"] == schedule.SENT
    # It ran as the LATEST missed occurrence, not as the one it was created for.
    assert schedule.parse_due(ran["due"]) == late
    assert ran["skipped"] == 2
    assert ran["skipped_note"] == "2 earlier runs skipped"

    # The successor lands on the next pass, ahead of now — nothing between
    # first_due and `late` is ever offered again.
    schedule.tick(now=late)
    fresh = [o for o in _occurrences(template["id"])
             if o["state"] == schedule.PENDING]
    assert len(fresh) == 1
    assert schedule.parse_due(fresh[0]["due"]) > late


def test_a_single_missed_occurrence_still_runs_with_nothing_reported_skipped(
        target, spawned):
    """Coalescing must not turn "one run, late" into a report about skipping.
    Nothing was skipped: the backlog is one deep."""
    template = schedule.create(str(target), "run", repeats="0 * * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])

    fired = schedule.tick(now=first_due + timedelta(minutes=20))

    assert [e["id"] for e in fired] == [first["id"]]
    assert len(spawned) == 1
    ran = _entries()[first["id"]]
    assert schedule.parse_due(ran["due"]) == first_due  # not moved
    assert "skipped" not in ran
    # ...and nothing was narrated about it. (The event ring is process-global,
    # so this asks about THIS entry rather than about the ring being empty.)
    assert [e for e in schedule.event_log()
            if e["entry_id"] == first["id"]] == []


def test_one_shot_catches_up_however_late(target, spawned):
    """A one-off is not coalesced with anything — there is nothing to coalesce
    it WITH — so it simply runs, however long the app was closed."""
    entry = schedule.create(str(target), "one shot",
                            datetime.now(timezone.utc) - timedelta(days=3))
    fired = schedule.tick()
    assert [e["id"] for e in fired] == [entry["id"]]
    assert len(spawned) == 1


# ------------------------------------------------ what an occurrence inherits
#
# An occurrence is the template's run, so what the user wrote about the work has
# to travel with it — a list of runs must be able to name one without going back
# to the template for the label. `session_id` is the exception that is really the
# feature: whether it travels is what "new task each run" means.


def test_an_occurrence_inherits_the_templates_title_and_description(target):
    template = schedule.create(str(target), "pull the news", repeats="0 9 * * *",
                               title="Morning digest",
                               description="Reads the feeds")
    occurrence = _occurrences(template["id"])[0]

    assert occurrence["title"] == "Morning digest"
    assert occurrence["description"] == "Reads the feeds"
    assert occurrence["message"] == "pull the news"


def test_by_default_every_run_appends_to_the_same_thread(target, spawned):
    """A task IS a Claude session, so chaining is the default by construction:
    the occurrence inherits the template's session id, and the send therefore
    resumes that conversation rather than opening a new one."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *",
                               session_id="sess-thread")
    first = _occurrences(template["id"])[0]
    assert first["session_id"] == "sess-thread"
    assert first["new_task_each_run"] is False

    schedule.tick(now=schedule.parse_due(first["due"]) + timedelta(seconds=1))
    assert spawned[0]["session_id"] == "sess-thread"


def test_new_task_each_run_gives_every_occurrence_a_fresh_session(target, spawned):
    """The opposite ask, and one line of `_materialize` is the whole feature:
    the occurrence is made with `session_id: ""`, which is how the rest of the
    module already spells "start a fresh session" — `_send` hands the empty
    string to the helper, and `_busy_sessions` treats it as colliding with
    nothing, so independent runs are not serialised against each other."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *",
                               session_id="sess-thread",
                               new_task_each_run=True)
    first = _occurrences(template["id"])[0]

    assert first["session_id"] == ""
    assert first["new_task_each_run"] is True
    # The TEMPLATE keeps its own id — the flag changes what occurrences get, not
    # what the schedule was created against.
    assert _entries()[template["id"]]["session_id"] == "sess-thread"

    first_due = schedule.parse_due(first["due"])
    schedule.tick(now=first_due + timedelta(seconds=1))
    schedule.tick(now=first_due + timedelta(seconds=2))
    second = next(o for o in _occurrences(template["id"])
                  if o["id"] != first["id"])

    assert spawned[0]["session_id"] == ""
    assert second["session_id"] == ""


def test_a_hand_edited_flag_degrades_rather_than_flipping_the_threading(target):
    """`bool("false")` is True, so reading the flag with it would silently turn a
    chained schedule into an unchained one on a store somebody edited by hand."""
    import json
    template = schedule.create(str(target), "run", repeats="0 * * * *",
                               session_id="sess-thread")
    occurrence = _occurrences(template["id"])[0]
    schedule.cancel(occurrence["id"])   # so the next tick has to materialize

    path = schedule.store_path()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for entry in data["entries"]:
        if entry["id"] == template["id"]:
            entry["new_task_each_run"] = "false"
            entry["title"] = 42
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    schedule.tick()

    fresh = [o for o in _occurrences(template["id"])
             if o["state"] == schedule.PENDING][0]
    assert fresh["new_task_each_run"] is False
    assert fresh["session_id"] == "sess-thread"     # still the same thread
    assert fresh["title"] == ""


# --------------------------------------------- how the thread gets STARTED
#
# The half of chaining that inheritance alone cannot supply, and the half that
# was missing. A template made from the Tasks page has no session id to hand
# down, so its first occurrence necessarily opens one — and with nothing
# carrying that answer back, every later occurrence inherited "" and opened
# another, which made leaving "new task each run" unticked behave exactly like
# ticking it. `_chain_session` is that writeback: run 1's ANSWER becomes run 2's
# INPUT, across two entries, while no entry's own `session_id` is ever rewritten
# to match its own answer.


class _FakeAgent:
    """The claude backend, as far as `_turn_tick` needs one."""

    def _cancel(self, run_id):  # pragma: no cover — nothing here cancels
        pass


def _turn_reports(entry_id, session_id, done=True):
    """Drive the watcher's seam for one entry: its turn says which session it
    ran in. The same seam test_schedule_reporting.py drives — no thread, no real
    claude, no sleeping."""
    entry = _entries()[entry_id]
    return schedule._turn_tick(
        entry, entry.get("run_id") or "r-1", _FakeAgent(),
        {"done": done, "session_id": session_id, "error": ""})


def _run_once(template_id, session_id):
    """One whole run of a template: fire its pending occurrence, let the tick
    after that materialize the successor (which is the real ordering — the
    successor usually exists before the turn has said anything), then have the
    turn report `session_id`. Returns the occurrence that ran."""
    pending = [o for o in _occurrences(template_id)
               if o["state"] == schedule.PENDING]
    assert len(pending) == 1, pending
    occurrence = pending[0]
    due = schedule.parse_due(occurrence["due"])
    schedule.tick(now=due + timedelta(seconds=1))
    schedule.tick(now=due + timedelta(seconds=2))
    _turn_reports(occurrence["id"], session_id)
    return _entries()[occurrence["id"]]


def test_the_first_run_teaches_the_template_which_thread_it_opened(target, spawned):
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    assert first["session_id"] == ""        # there was nothing to inherit

    ran = _run_once(template["id"], "sess-1")

    # The template now knows the conversation — the whole fix in one assertion.
    assert _entries()[template["id"]]["session_id"] == "sess-1"
    # …and the successor, materialized with "" before the turn spoke, is fixed
    # up rather than left to open a second thread.
    second = [o for o in _occurrences(template["id"])
              if o["state"] == schedule.PENDING][0]
    assert second["session_id"] == "sess-1"
    # The run itself keeps saying, truthfully, that it started fresh.
    assert ran["session_id"] == ""
    assert ran["claude_session_id"] == "sess-1"
    assert spawned[0]["session_id"] == ""


def test_a_successor_materialized_after_the_report_inherits_the_thread(target, spawned):
    """The other ordering — the turn reports before the next tick materializes
    anything — and it must land in the same place, through `_materialize`
    inheriting the template's now-filled id rather than through the fixup."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    due = schedule.parse_due(first["due"])

    schedule.tick(now=due + timedelta(seconds=1))
    _turn_reports(first["id"], "sess-1")
    schedule.tick(now=due + timedelta(seconds=2))

    second = [o for o in _occurrences(template["id"])
              if o["state"] == schedule.PENDING][0]
    assert second["session_id"] == "sess-1"


def test_runs_two_and_three_continue_the_thread_run_one_opened(target, spawned):
    """The documented default, end to end: one task, one session, three runs.
    Run 3 also matters on its own — it is where a writeback that got undone, or
    re-decided every run, would show up."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    _run_once(template["id"], "sess-1")
    _run_once(template["id"], "sess-1")   # a resume reports the session it resumed
    _run_once(template["id"], "sess-1")

    # Only the first send opened a thread; every one after it resumed.
    assert [c["session_id"] for c in spawned] == ["", "sess-1", "sess-1"]
    assert _entries()[template["id"]]["session_id"] == "sess-1"
    # Three ran, one is queued ahead — and it carries the thread too.
    occurrences = _occurrences(template["id"])
    assert len(occurrences) == 4
    ahead = [o for o in occurrences if o["state"] == schedule.PENDING]
    assert [o["session_id"] for o in ahead] == ["sess-1"]


def test_new_task_each_run_never_learns_a_session(target, spawned):
    """The opt-out has to stay an opt-out for ever: a template that mints fresh
    tasks must never acquire a session id, however many its runs report."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *",
                               new_task_each_run=True)
    _run_once(template["id"], "sess-1")
    _run_once(template["id"], "sess-2")

    assert _entries()[template["id"]]["session_id"] == ""
    assert [c["session_id"] for c in spawned] == ["", ""]
    assert [o["session_id"] for o in _occurrences(template["id"])] == ["", "", ""]
    # The runs still record what they ran in — that is the row's link to the
    # Inbox, and it is orthogonal to threading.
    ran = sorted((o for o in _occurrences(template["id"]) if o["claude_session_id"]),
                 key=lambda o: o["claude_session_id"])
    assert [o["claude_session_id"] for o in ran] == ["sess-1", "sess-2"]


def test_a_session_the_user_chose_is_never_overwritten_by_a_run(target, spawned):
    """A task handed off from a chat carries the conversation it must continue.
    What a run reports is an answer; it does not get to redefine the input."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *",
                               session_id="sess-user")
    ran = _run_once(template["id"], "sess-other")

    assert _entries()[template["id"]]["session_id"] == "sess-user"
    assert ran["session_id"] == "sess-user"
    assert ran["claude_session_id"] == "sess-other"
    second = [o for o in _occurrences(template["id"])
              if o["state"] == schedule.PENDING][0]
    assert second["session_id"] == "sess-user"


def test_re_reporting_the_same_session_does_not_touch_the_store(target, spawned,
                                                                monkeypatch):
    """A watch reports every few seconds and re-reports the id it already gave.
    First run wins and the store then goes quiet."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    ran = _run_once(template["id"], "sess-1")

    writes = []
    real_write = schedule._write
    monkeypatch.setattr(schedule, "_write",
                        lambda entries: (writes.append(len(entries)),
                                         real_write(entries))[1])

    schedule._chain_session(template["id"], "sess-1")
    schedule._chain_session(template["id"], "sess-1")
    # A LATER run reporting a different id is refused by the same rule, which is
    # what keeps the thread stable rather than hopping to the newest session.
    schedule._chain_session(template["id"], "sess-9")
    # And through the watcher's own seam, which never even gets that far: the
    # entry already holds the answer.
    schedule._turn_tick(_entries()[ran["id"]], "r-1", _FakeAgent(),
                        {"done": False, "session_id": "sess-1", "phase": "working"})

    assert writes == []
    assert _entries()[template["id"]]["session_id"] == "sess-1"


def test_two_templates_reporting_each_learn_only_their_own_thread(target, spawned):
    """Two occurrences of two different templates can report at the same time,
    and the writeback is keyed off the occurrence's own `template_id`."""
    a = schedule.create(str(target), "a", repeats="*/5 * * * *")
    b = schedule.create(str(target), "b", repeats="*/5 * * * *")
    first_a = _occurrences(a["id"])[0]
    first_b = _occurrences(b["id"])[0]
    when = max(schedule.parse_due(first_a["due"]),
               schedule.parse_due(first_b["due"]))

    schedule.tick(now=when + timedelta(seconds=1))
    schedule.tick(now=when + timedelta(seconds=2))
    _turn_reports(first_b["id"], "sess-b")
    _turn_reports(first_a["id"], "sess-a")

    assert _entries()[a["id"]]["session_id"] == "sess-a"
    assert _entries()[b["id"]]["session_id"] == "sess-b"
    assert [o["session_id"] for o in _occurrences(a["id"])
            if o["state"] == schedule.PENDING] == ["sess-a"]
    assert [o["session_id"] for o in _occurrences(b["id"])
            if o["state"] == schedule.PENDING] == ["sess-b"]


def test_the_next_run_is_held_while_the_thread_it_learned_is_still_busy(
        target, spawned):
    """The hold `_busy_sessions` exists for, reached from the direction this fix
    opened: run 1 asked for a fresh session, so the ONLY record that its thread
    is in flight is the id its turn reported. Without reading that, run 2 would
    resume a conversation mid-turn."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    due = schedule.parse_due(first["due"])

    schedule.tick(now=due + timedelta(seconds=1))
    _turn_reports(first["id"], "sess-1", done=False)   # still running
    schedule.tick(now=due + timedelta(seconds=2))       # successor exists

    assert schedule.tick(now=due + timedelta(minutes=5, seconds=1)) == []
    assert len(spawned) == 1

    # It goes as soon as the turn has a verdict.
    _turn_reports(first["id"], "sess-1")
    fired = schedule.tick(now=due + timedelta(minutes=5, seconds=2))
    assert [c["session_id"] for c in spawned] == ["", "sess-1"]
    assert len(fired) == 1


def test_a_one_off_reporting_a_session_chains_nothing(target, spawned):
    """No template, nothing to write back to — and its own two fields keep
    meaning what they meant."""
    entry = schedule.create(str(target), "one shot",
                            datetime.now(timezone.utc) - timedelta(seconds=5))
    schedule.tick()
    _turn_reports(entry["id"], "sess-1")

    stored = _entries()[entry["id"]]
    assert stored["claude_session_id"] == "sess-1"
    assert stored["session_id"] == ""      # the input still says "start fresh"
    assert stored.get("template_id", "") == ""
    assert len(schedule.list_entries()) == 1


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

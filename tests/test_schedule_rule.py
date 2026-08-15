"""Structured repeats — the calendar's vocabulary (recur.py), and the store and
route that carry it.

The feature under test is the second way to say "repeatedly": beside the cron
line that `test_schedule_recurring.py` covers, a RULE — freq/interval/byday/
monthly, ending on a date or after a number of runs. The two share one
lifecycle on purpose (a `recurring` template, one materialized occurrence ahead
of it, occurrences flowing through the ordinary firing machinery), so what is
tested here is only what differs: the arithmetic, the ends, and the wire.

Three groups, in the order the value flows:

* **recur** — pure, fixed datetimes, no store. The interesting cases are all
  about what is SKIPPED (a month with no 31st, no fifth Friday, a year with no
  Feb 29) and where a week begins.
* **the store** — a template that counts, and stops counting.
* **the route** — the object arriving as JSON, and the four ways to send one
  that cannot mean anything.

Spawns and the wake stub are stubbed exactly as in test_schedule_recurring.py.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_spawn, recur, schedule, schedule_wake
from fused_render.server import create_app

WRITE = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture(autouse=True)
def clean_event_log(monkeypatch):
    """The event log is process-global and in memory, so it would otherwise
    carry one test's events into the next one's assertions."""
    schedule._events.clear()
    monkeypatch.setattr(schedule, "_delivered", 0)
    yield
    schedule._events.clear()


class _Clock:
    """A settable `now` for the store.

    Not a convenience: a series with an END can only be observed by living
    through it, and `upcoming` projects from the wall clock rather than from
    whatever `now` a `tick` was handed. Passing `now=` to `tick` alone would
    therefore fire three runs while the projection still called all three
    future — which is what the first cut of these tests asserted, wrongly."""

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def set(self, when: datetime) -> datetime:
        self.now = when
        return when


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    ticker = _Clock()
    monkeypatch.setattr(schedule, "_now", lambda: ticker.now)
    return ticker


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id=""):
        calls.append({"target": target, "message": prompt})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    (d / "index.html").write_text("<html></html>")
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _entries():
    return {e["id"]: e for e in schedule.list_entries()}


def _occurrences(template_id):
    return [e for e in schedule.list_entries()
            if str(e.get("template_id") or "") == template_id]


def _pending(template_id):
    return [o for o in _occurrences(template_id) if o["state"] == schedule.PENDING]


def _local(entry_or_iso) -> datetime:
    """A stored UTC instant back as the naive local wall-clock time the user
    picked — the only frame in which "9am on the second Wednesday" is a fact."""
    iso = entry_or_iso if isinstance(entry_or_iso, str) else entry_or_iso["due"]
    return schedule.parse_due(iso).astimezone().replace(tzinfo=None)


def _anchor(**offset) -> datetime:
    """An anchor `offset` from the store's now, as an aware instant. Relative
    rather than a fixed date so these tests do not quietly expire."""
    return schedule._now() + timedelta(**offset)


# =========================================================== recur: the walk
#
# 2026-08-12 is a Wednesday and 2026-08-15 a Saturday; every fixed date below
# hangs off one of those two.

WED = datetime(2026, 8, 12, 9, 0)
SAT = datetime(2026, 8, 15, 9, 0)


def _walk(rule, anchor, n=5, after=None):
    return recur.occurrences(rule, anchor, after, n)


def test_the_series_includes_its_anchor():
    """The anchor is the first RUN, not a marker before the first run — the
    form's date-and-time picker is choosing occurrence one."""
    assert _walk({"freq": "day"}, SAT, 1) == [SAT]
    assert _walk({"freq": "month"}, SAT, 1) == [SAT]
    assert _walk({"freq": "year"}, SAT, 1) == [SAT]
    assert recur.next_occurrence({"freq": "day"}, SAT,
                                 SAT - timedelta(days=99)) == SAT


def test_daily_steps_by_its_interval_and_keeps_the_time_of_day():
    assert _walk({"freq": "day", "interval": 3}, SAT, 3) == [
        SAT, datetime(2026, 8, 18, 9, 0), datetime(2026, 8, 21, 9, 0)]
    # Strictly after, so the anchor's own instant is not offered twice.
    assert recur.next_occurrence({"freq": "day"}, SAT, SAT) == \
        datetime(2026, 8, 16, 9, 0)


def test_weekly_defaults_to_the_anchors_own_weekday():
    """No `byday` is not "no days" — it is the day the user picked."""
    assert _walk({"freq": "week"}, WED, 3) == [
        WED, datetime(2026, 8, 19, 9, 0), datetime(2026, 8, 26, 9, 0)]


def test_weekly_runs_every_chosen_day_and_the_first_week_is_partial():
    """Mon+Wed from a Wednesday anchor: the Monday of the anchor's own week has
    already gone by, so the series starts at the anchor and picks up Mondays
    from the following week — the "first matching instant >= anchor" rule."""
    times = _walk({"freq": "week", "byday": [1, 3]}, WED, 5)
    assert times == [
        WED,                             # Wed 12th, the anchor itself
        datetime(2026, 8, 17, 9, 0),     # Mon 17th
        datetime(2026, 8, 19, 9, 0),     # Wed 19th
        datetime(2026, 8, 24, 9, 0),     # Mon 24th
        datetime(2026, 8, 26, 9, 0),     # Wed 26th
    ]


def test_a_fortnight_counts_sunday_anchored_weeks_not_fourteen_days():
    """The unit that repeats is the WEEK the anchor falls in, and weeks start on
    Sunday. From a Saturday anchor, "every 2 weeks on Sunday" therefore skips
    the Sunday five days LATER — that one opens the very next week, which is an
    off week. Counting Mon-anchored weeks (or just adding 14 days to the anchor)
    would land on the 16th instead, which is why this test names the date."""
    times = _walk({"freq": "week", "interval": 2, "byday": [0]}, SAT, 3)
    assert times == [datetime(2026, 8, 23, 9, 0),
                     datetime(2026, 9, 6, 9, 0),
                     datetime(2026, 9, 20, 9, 0)]


def test_a_fortnight_keeps_both_days_in_the_same_week():
    """Mon+Wed every 2 weeks: the pair travels together. Counting 14 days from
    each RUN instead would put the Mondays and the Wednesdays on opposite
    fortnights within two steps."""
    times = _walk({"freq": "week", "interval": 2, "byday": [1, 3]}, SAT, 4)
    assert times == [datetime(2026, 8, 24, 9, 0), datetime(2026, 8, 26, 9, 0),
                     datetime(2026, 9, 7, 9, 0), datetime(2026, 9, 9, 9, 0)]
    assert [t.isocalendar()[1] for t in times[:2]] == \
           [times[0].isocalendar()[1]] * 2


def test_monthly_on_a_day_skips_the_months_that_lack_it():
    """The 31st, never clamped to the 30th or the 28th: a user who picked the
    31st asked for the 31st, and February is not a near miss (RFC 5545)."""
    times = _walk({"freq": "month"}, datetime(2026, 1, 31, 9, 0), 5)
    assert [t.month for t in times] == [1, 3, 5, 7, 8]  # no Feb, Apr, Jun
    assert all(t.day == 31 for t in times)


def test_monthly_on_a_day_steps_by_its_interval():
    times = _walk({"freq": "month", "interval": 3}, datetime(2026, 1, 15, 9, 0), 3)
    assert times == [datetime(2026, 1, 15, 9, 0), datetime(2026, 4, 15, 9, 0),
                     datetime(2026, 7, 15, 9, 0)]


def test_monthly_on_the_nth_weekday_reads_the_nth_off_the_anchor():
    """Aug 15 2026 is the third Saturday, so "monthly on the third Saturday" is
    a thing the anchor already says — the rule never restates it."""
    times = _walk({"freq": "month", "monthly": "nth-weekday"}, SAT, 4)
    assert times == [SAT, datetime(2026, 9, 19, 9, 0),
                     datetime(2026, 10, 17, 9, 0), datetime(2026, 11, 21, 9, 0)]
    assert all(recur.weekday(t.date()) == 6 for t in times)      # Saturday
    assert all((t.day - 1) // 7 + 1 == 3 for t in times)         # the third one


def test_the_fifth_friday_simply_misses_most_months():
    """The case cron cannot express at all, and the one that proves skipping:
    only five months in 2026 have a fifth Friday, and the rule fires in exactly
    those. Jan 30 2026 is the fifth Friday of January."""
    times = _walk({"freq": "month", "monthly": "nth-weekday"},
                  datetime(2026, 1, 30, 9, 0), 5)
    assert times == [datetime(2026, 1, 30, 9, 0), datetime(2026, 5, 29, 9, 0),
                     datetime(2026, 7, 31, 9, 0), datetime(2026, 10, 30, 9, 0),
                     datetime(2027, 1, 29, 9, 0)]


def test_a_leap_day_rule_waits_for_the_leap_years():
    times = _walk({"freq": "year"}, datetime(2024, 2, 29, 9, 0), 3)
    assert [t.year for t in times] == [2024, 2028, 2032]
    # And an interval lands only on the leap years it actually reaches: every
    # 2 years from 2024 is 2026, 2028, ... — 2026 has no Feb 29, so it is
    # skipped rather than moved to the 28th.
    every_two = _walk({"freq": "year", "interval": 2},
                      datetime(2024, 2, 29, 9, 0), 3)
    assert [t.year for t in every_two] == [2024, 2028, 2032]


def test_yearly_steps_by_its_interval():
    assert _walk({"freq": "year", "interval": 5}, SAT, 3) == [
        SAT, datetime(2031, 8, 15, 9, 0), datetime(2036, 8, 15, 9, 0)]


# ------------------------------------------------------------------ the ends

def test_until_includes_the_day_it_names():
    """"Ends on Nov 11" means the 11th can still run — it is the last day, not
    the first excluded one. The comparison is on the DATE, so a run later in the
    day than the anchor's own time is not cut off by the boundary either."""
    rule = {"freq": "day", "until": "2026-08-17"}
    assert _walk(rule, SAT, 9) == [SAT, datetime(2026, 8, 16, 9, 0),
                                   datetime(2026, 8, 17, 9, 0)]
    # The last run of the last day is reachable...
    assert recur.next_occurrence(rule, SAT, datetime(2026, 8, 16, 23, 59)) == \
        datetime(2026, 8, 17, 9, 0)
    # ...and nothing follows it.
    assert recur.next_occurrence(rule, SAT, datetime(2026, 8, 17, 9, 0)) is None


def test_an_exhausted_rule_answers_none_rather_than_raising():
    """None is an answer the store acts on (stop materializing); an exception
    would be a template that breaks instead of one that finishes."""
    spent = {"freq": "week", "until": "2026-08-15"}
    assert recur.next_occurrence(spent, SAT, SAT) is None
    assert recur.occurrences(spent, SAT, SAT, 10) == []
    # An `until` before the anchor is a series with nothing in it at all.
    assert recur.next_occurrence({"freq": "day", "until": "2020-01-01"},
                                 SAT, SAT - timedelta(days=9999)) is None


def test_count_is_not_the_walks_business():
    """`count` is a fact about how many runs a STORE has made, not about the
    calendar, so the walk keeps going and the store stops asking."""
    times = _walk({"freq": "day", "count": 2}, SAT, 5)
    assert len(times) == 5


# =========================================================== recur: validation
#
# Every message below is read by a person: it comes back verbatim in a 400.

def test_a_valid_rule_normalises_to_what_it_actually_says():
    assert recur.validate_rule({"freq": "day"}) == {"freq": "day", "interval": 1}
    # byday deduped and sorted; monthly defaulted only where it means something.
    assert recur.validate_rule({"freq": "week", "byday": [3, 1, 3]}) == \
        {"freq": "week", "interval": 1, "byday": [1, 3]}
    assert recur.validate_rule({"freq": "month"}) == \
        {"freq": "month", "interval": 1, "monthly": "day"}
    assert recur.validate_rule({"freq": "month", "monthly": "nth-weekday",
                                "interval": 2, "count": 13}) == \
        {"freq": "month", "interval": 2, "monthly": "nth-weekday", "count": 13}
    assert recur.validate_rule({"freq": "year", "until": "2026-11-11"}) == \
        {"freq": "year", "interval": 1, "until": "2026-11-11"}
    # `byday` is NOT defaulted here — its default is the anchor's weekday, and
    # validation has no anchor.
    assert "byday" not in recur.validate_rule({"freq": "week"})


@pytest.mark.parametrize("rule, says", [
    ({}, "freq: required"),
    ({"freq": "hourly"}, "freq: expected one of"),
    ({"freq": "day", "interval": 0}, "interval: expected a whole number"),
    ({"freq": "day", "interval": 100}, "interval: expected a whole number"),
    ({"freq": "day", "interval": "2"}, "interval: expected a whole number"),
    ({"freq": "day", "byday": [1]}, "only a weekly repeat"),
    ({"freq": "week", "byday": []}, "byday: cannot be empty"),
    ({"freq": "week", "byday": 3}, "byday: expected a list"),
    ({"freq": "week", "byday": [7]}, "is not a weekday"),
    ({"freq": "week", "byday": [-1]}, "is not a weekday"),
    ({"freq": "week", "monthly": "day"}, "only a monthly repeat"),
    ({"freq": "month", "monthly": "last"}, "monthly: expected"),
    ({"freq": "day", "until": "the 11th"}, "until: expected an end date"),
    ({"freq": "day", "count": 0}, "count: expected a whole number"),
    ({"freq": "day", "count": 1000}, "count: expected a whole number"),
    ({"freq": "day", "count": 3, "until": "2026-11-11"}, "pick one"),
    ({"freq": "day", "untill": "2026-11-11"}, "don't know what to do with"),
])
def test_a_bad_rule_says_what_is_wrong_with_it(rule, says):
    with pytest.raises(ValueError) as raised:
        recur.validate_rule(rule)
    assert says in str(raised.value)


def test_a_typo_is_refused_rather_than_ignored():
    """The specific reason unknown fields are fatal: a dropped `untill` is a
    repeat that never ends, discovered weeks later."""
    with pytest.raises(ValueError) as raised:
        recur.validate_rule({"freq": "day", "untill": "2026-11-11"})
    assert "'untill'" in str(raised.value)


def test_the_walk_refuses_aware_datetimes():
    """Same posture as cron.py: the arithmetic is a promise about a wall clock,
    so an instant with a zone attached is a caller that skipped the edge."""
    with pytest.raises(ValueError):
        recur.next_occurrence({"freq": "day"}, SAT.replace(tzinfo=timezone.utc),
                              SAT)


# ============================================================== the store
#
# From here on the frame is the store's: aware UTC instants in, occurrences out.

def test_create_with_a_rule_stores_a_template_and_its_first_run(target):
    when = _anchor(hours=1)
    template = schedule.create(str(target), "weekly review", due=when,
                               rule={"freq": "week", "byday": [1, 3]})

    assert template["state"] == schedule.RECURRING
    assert template["repeats"] == ""            # not a cron template
    assert template["rule"] == {"freq": "week", "interval": 1, "byday": [1, 3]}
    assert schedule.parse_due(template["anchor"]) == when

    occurrences = _occurrences(template["id"])
    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence["state"] == schedule.PENDING
    assert occurrence["template_id"] == template["id"]
    # A recurring run, so the tiny skip-not-catch-up bound, exactly as a cron
    # occurrence gets — the whole point of sharing the lifecycle.
    assert occurrence["max_late"] == schedule._OCCURRENCE_MAX_LATE_S
    assert _entries()[template["id"]]["made"] == 1
    # The template's `due` mirrors the run ahead of it; `anchor` does not move.
    assert _entries()[template["id"]]["due"] == occurrence["due"]


def test_a_rule_needs_a_due_and_refuses_a_second_schedule(target):
    with pytest.raises(ValueError) as no_due:
        schedule.create(str(target), "x", rule={"freq": "day"})
    assert "rule: needs `due`" in str(no_due.value)

    with pytest.raises(ValueError) as both:
        schedule.create(str(target), "x", due=_anchor(hours=1),
                        rule={"freq": "day"}, repeats="0 * * * *")
    assert "cannot be combined" in str(both.value)

    with pytest.raises(ValueError):
        schedule.create(str(target), "x", due=_anchor(hours=1),
                        rule={"freq": "fortnightly"})
    assert schedule.list_entries() == []


def test_an_anchor_in_the_past_is_a_phase_not_a_missed_message(target):
    """A one-shot further back than the catch-up bound is refused; an anchor is
    not. "Every other Monday, on the phase that started last Monday" is an
    ordinary thing to mean, and nothing about it fires late — the first run
    materialized is still in the future."""
    template = schedule.create(str(target), "standing", due=_anchor(days=-30),
                               rule={"freq": "day", "interval": 7})
    assert template["state"] == schedule.RECURRING
    first = _occurrences(template["id"])[0]
    assert schedule.parse_due(first["due"]) > datetime.now(timezone.utc)
    # Still on the anchor's phase: a whole number of weeks from it.
    step = schedule.parse_due(first["due"]) - schedule.parse_due(template["anchor"])
    assert step.total_seconds() % (7 * 86400) == 0


def test_a_fired_run_is_followed_by_the_next_one(target, spawned):
    when = _anchor(minutes=1)
    template = schedule.create(str(target), "run", due=when,
                               rule={"freq": "day", "interval": 2})
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])
    assert first_due == when

    fired = schedule.tick(now=first_due + timedelta(seconds=1))
    assert [e["id"] for e in fired] == [first["id"]]
    assert len(spawned) == 1

    # The successor appears on the next pass, two days on (the interval), and
    # the template has now made two.
    schedule.tick(now=first_due + timedelta(seconds=2))
    fresh = _pending(template["id"])
    assert len(fresh) == 1
    assert _local(fresh[0]) == _local(first) + timedelta(days=2)
    assert _entries()[template["id"]]["made"] == 2


def _run_out(template_id, clock, passes=8):
    """Live through the whole series: fire whatever is pending, over and over,
    until the template stops producing. Returns every occurrence it ever made.
    Moves the CLOCK rather than passing `now=`, so the projection agrees with
    the store about what is still to come.

    Materializes before looking for something to fire, because a template whose
    only run was SKIPPED has nothing pending until a tick gives it a successor —
    a loop that checked first would decide the series was over."""
    for _ in range(passes):
        schedule.tick()
        pending = _pending(template_id)
        if not pending:
            break
        due = schedule.parse_due(pending[0]["due"])
        clock.set(due + timedelta(seconds=1))
        schedule.tick()
    return sorted(_occurrences(template_id), key=lambda o: o["due"])


def test_count_stops_the_series_at_n_runs(target, spawned, clock):
    """"Ends after 3 occurrences" is a promise about how many runs reach the
    calendar, and the store is what keeps it — recur.py counts to nothing."""
    template = schedule.create(str(target), "run", due=_anchor(minutes=1),
                               rule={"freq": "day", "count": 3})
    made = _run_out(template["id"], clock)

    assert len(made) == 3
    assert len(spawned) == 3
    assert {o["state"] for o in made} == {schedule.SENT}
    stored = _entries()[template["id"]]
    assert stored["made"] == 3
    # Spent, not dead: it stays a recurring schedule with nothing ahead of it.
    assert stored["state"] == schedule.RECURRING
    assert schedule.upcoming(stored) == []


def test_a_skipped_run_still_counts_against_the_count(target, spawned, clock):
    """The user asked for three runs to be SCHEDULED. Deciding to skip one is a
    decision about a run that was scheduled, so it consumes one — otherwise a
    schedule would quietly extend itself every time a run was waved off."""
    template = schedule.create(str(target), "run", due=_anchor(minutes=1),
                               rule={"freq": "day", "count": 3})
    schedule.cancel(_pending(template["id"])[0]["id"])
    made = _run_out(template["id"], clock)

    assert len(made) == 3
    assert len(spawned) == 2                       # one of the three was skipped
    assert _entries()[template["id"]]["made"] == 3


def test_until_stops_the_series_on_the_day_it_names(target, spawned, clock):
    when = _anchor(minutes=1)
    last = (when.astimezone().replace(tzinfo=None) + timedelta(days=2)).date()
    template = schedule.create(str(target), "run", due=when,
                               rule={"freq": "day", "until": last.isoformat()})
    made = _run_out(template["id"], clock)

    # Today, tomorrow, and the named day itself — inclusive.
    assert len(made) == 3
    assert _local(made[-1]).date() == last
    stored = _entries()[template["id"]]
    assert stored["state"] == schedule.RECURRING
    assert schedule.upcoming(stored) == []


# ---------------------------------------------------------------- cancelling

def test_cancelling_a_rule_template_cascades_to_its_pending_run(target):
    template = schedule.create(str(target), "run", due=_anchor(hours=1),
                               rule={"freq": "day"})
    occurrence = _occurrences(template["id"])[0]

    schedule.cancel(template["id"])
    assert _entries()[template["id"]]["state"] == schedule.CANCELLED
    assert _entries()[occurrence["id"]]["state"] == schedule.CANCELLED

    # And nothing further is materialized under it.
    schedule.tick(now=schedule.parse_due(occurrence["due"]) + timedelta(days=3))
    assert len(_occurrences(template["id"])) == 1


def test_one_run_can_be_skipped_and_unskipped(target):
    template = schedule.create(str(target), "run", due=_anchor(hours=1),
                               rule={"freq": "day"})
    first = _occurrences(template["id"])[0]

    schedule.cancel(first["id"])
    assert _entries()[template["id"]]["state"] == schedule.RECURRING
    # The next run takes its place, a day on — the skipped time is not re-offered.
    schedule.tick(now=schedule.parse_due(first["due"]) - timedelta(minutes=1))
    fresh = _pending(template["id"])
    assert len(fresh) == 1
    assert _local(fresh[0]) == _local(first) + timedelta(days=1)

    restored = schedule.restore(first["id"])
    assert restored["id"] == first["id"]
    assert len(_pending(template["id"])) == 2


# --------------------------------------------------------------- projections

def test_upcoming_projects_what_recur_projects(target):
    template = schedule.create(str(target), "run", due=_anchor(hours=1),
                               rule={"freq": "week", "byday": [1, 3]})
    stored = _entries()[template["id"]]
    times = [_local(t) for t in schedule.upcoming(stored, horizon_days=28)]

    assert times == sorted(times)
    assert all(t > datetime.now() for t in times)
    assert {recur.weekday(t.date()) for t in times} <= {1, 3}
    # The already-materialized run is the first thing projected, not a gap.
    assert times[0] == _local(_occurrences(template["id"])[0])
    # And it agrees with the engine, asked directly.
    anchor = _local(stored["anchor"])
    assert times == [t for t in recur.occurrences(
        stored["rule"], anchor, datetime.now(), 500)
        if t <= datetime.now() + timedelta(days=28)]


def test_upcoming_stops_at_the_end_of_a_counted_series(target, spawned, clock):
    """The count case is projected by numbering the series FROM THE ANCHOR, so
    the run already materialized (and already counted in `made`) is not
    double-counted out of the projection's tail."""
    template = schedule.create(str(target), "run", due=_anchor(minutes=1),
                               rule={"freq": "day", "count": 3})
    stored = _entries()[template["id"]]
    assert stored["made"] == 1
    # Three runs promised, none yet fired: all three are still to come.
    assert len(schedule.upcoming(stored, horizon_days=28)) == 3

    first_due = schedule.parse_due(_pending(template["id"])[0]["due"])
    clock.set(first_due + timedelta(seconds=1))
    schedule.tick()          # fires the first
    schedule.tick()          # materializes the second
    # One is behind us, so two are left — and the third, which `made` has NOT
    # yet counted, is still projected. Subtracting `made` from a projection
    # starting at `now` would have dropped it.
    assert len(schedule.upcoming(_entries()[template["id"]],
                                 horizon_days=28)) == 2


def test_upcoming_stops_at_until(target):
    when = _anchor(minutes=1)
    last = (when.astimezone().replace(tzinfo=None) + timedelta(days=3)).date()
    template = schedule.create(str(target), "run", due=when,
                               rule={"freq": "day", "until": last.isoformat()})
    times = schedule.upcoming(_entries()[template["id"]], horizon_days=28)
    assert len(times) == 4
    assert _local(times[-1]).date() == last


def test_a_tampered_rule_stops_loudly(target):
    """Same contract as a cron line that no longer parses: `error`, announced.
    Silently never firing again is the one outcome this feature must not have."""
    import json

    template = schedule.create(str(target), "run", due=_anchor(hours=1),
                               rule={"freq": "day"})
    schedule.cancel(_occurrences(template["id"])[0]["id"])
    path = schedule.store_path()
    with open(path) as fh:
        data = json.load(fh)
    for entry in data["entries"]:
        if entry["id"] == template["id"]:
            entry["rule"] = {"freq": "sometimes"}
    with open(path, "w") as fh:
        json.dump(data, fh)

    schedule.tick()
    stored = _entries()[template["id"]]
    assert stored["state"] == schedule.ERROR
    assert "recurring schedule stopped" in stored["error"]
    assert any(e["kind"] == schedule.EVENT_FAILED and e["entry_id"] == template["id"]
               for e in schedule.event_log())
    # A projection off the same store never raises into the listing.
    assert schedule.upcoming(stored) == []


# ================================================================== the route

def _iso(when: datetime) -> str:
    return when.isoformat()


def test_post_a_rule_creates_a_template_the_listing_can_draw(client, target):
    res = client.post("/api/schedule", headers=WRITE, json={
        "target": str(target), "message": "second Wednesday review",
        "due": _iso(_anchor(hours=2)),
        "rule": {"freq": "month", "monthly": "nth-weekday", "interval": 2},
    })
    assert res.status_code == 200
    entry = res.json()["entry"]
    assert entry["state"] == schedule.RECURRING
    assert entry["rule"] == {"freq": "month", "interval": 2,
                             "monthly": "nth-weekday"}

    listed = client.get("/api/schedule").json()["entries"]
    template = next(e for e in listed if e["id"] == entry["id"])
    # The rule and its progress ride along, and the projection is server-side —
    # the client draws a calendar without a recurrence engine of its own.
    assert template["rule"]["monthly"] == "nth-weekday"
    assert template["made"] == 1
    assert len(template["upcoming"]) > 0
    assert any(e.get("template_id") == entry["id"] and e["state"] == "pending"
               for e in listed)


def test_post_a_rule_reports_the_ends_it_was_given(client, target):
    res = client.post("/api/schedule", headers=WRITE, json={
        "target": str(target), "message": "x", "due": _iso(_anchor(hours=2)),
        "rule": {"freq": "week", "byday": [3, 1], "count": 13},
    })
    assert res.status_code == 200
    assert res.json()["entry"]["rule"] == {"freq": "week", "interval": 1,
                                          "byday": [1, 3], "count": 13}


@pytest.mark.parametrize("body, says", [
    ({"rule": "weekly"}, "rule: expected an object"),
    ({"rule": {"freq": "day"}}, "rule: needs `due`"),
    ({"rule": {"freq": "day"}, "delay_seconds": 600}, "cannot be combined"),
    ({"rule": {"freq": "day"}, "repeats": "0 * * * *"}, "cannot be combined"),
])
def test_the_ways_a_rule_cannot_be_sent(client, target, body, says):
    res = client.post("/api/schedule", headers=WRITE,
                      json={"target": str(target), "message": "x", **body})
    assert res.status_code == 400
    assert says in res.json()["error"]


def test_a_bad_rule_comes_back_as_the_sentence_recur_wrote(client, target):
    """The human message is the payload: it is what the form shows, so it must
    survive the trip rather than being replaced by a generic 400."""
    res = client.post("/api/schedule", headers=WRITE, json={
        "target": str(target), "message": "x", "due": _iso(_anchor(hours=2)),
        "rule": {"freq": "month", "byday": [1]},
    })
    assert res.status_code == 400
    assert "only a weekly repeat" in res.json()["error"]

    ends = client.post("/api/schedule", headers=WRITE, json={
        "target": str(target), "message": "x", "due": _iso(_anchor(hours=2)),
        "rule": {"freq": "day", "count": 3, "until": "2026-11-11"},
    })
    assert ends.status_code == 400
    assert "pick one" in ends.json()["error"]
    # Nothing was stored by either refusal.
    assert client.get("/api/schedule").json()["entries"] == []


def test_a_rule_still_carries_the_d3_write_guard(client, target):
    res = client.post("/api/schedule", json={
        "target": str(target), "message": "x", "due": _iso(_anchor(hours=2)),
        "rule": {"freq": "day"}})
    assert res.status_code == 403


def test_cron_templates_are_untouched_by_any_of_this(client, target):
    """The parallel path stays parallel: a cron entry carries no rule, and its
    projection still comes off the cron grid."""
    entry = client.post("/api/schedule", headers=WRITE, json={
        "target": str(target), "message": "x", "repeats": "0 * * * *"
    }).json()["entry"]
    assert entry["rule"] is None
    assert "anchor" not in entry
    listed = client.get("/api/schedule").json()["entries"]
    template = next(e for e in listed if e["id"] == entry["id"])
    assert len(template["upcoming"]) > 100        # hourly over the 14-day horizon
    assert all(schedule.parse_due(t).astimezone().minute == 0
               for t in template["upcoming"])


def test_the_date_helpers_agree_with_the_calendar():
    """A guard on the one conversion everything else rests on: 0 is Sunday, and
    a week starts on the Sunday on or before the day."""
    assert recur.weekday(date(2026, 8, 16)) == 0          # Sunday
    assert recur.weekday(date(2026, 8, 15)) == 6          # Saturday
    assert recur.week_start(date(2026, 8, 15)) == date(2026, 8, 9)
    assert recur.week_start(date(2026, 8, 16)) == date(2026, 8, 16)

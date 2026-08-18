"""The queue: unbounded catch-up, coalesced recurrences, and cancel.

The policy this file pins, in one paragraph. Nothing fires while the app is not
running, so opening it after a week away finds work that came due in the dark.
That work is now QUEUED rather than expired: a missed one-off runs however old
it is, a recurring rule that missed N runs runs exactly ONCE (the latest, with
the rest counted and reported), and a message scheduled into the past is
accepted, recorded with the time the user picked, and sent on the next tick.
What makes that safe is not a constant — it is `schedule.queue()` and
`schedule.cancel_queued()`, the surface the shell's queue popover is built on.

Two invariants are load-bearing and each has its own section below:

* **claim-before-spawn** — an entry is written `sending` BEFORE the helper is
  spawned, so a process that dies in between leaves something that is never
  re-sent. Cancel had to be built without disturbing that order, so it is
  re-proved here rather than assumed from test_schedule.py.
* **nothing already terminal is resurrected** — removing the bound reaches only
  entries that are still `pending`. An entry an older build already swept to
  `missed` stays missed. This is what makes the change shippable with no
  migration, so it is guarded by a test.

Frozen clocks throughout (`tick(now=...)`, and `_at` for a store written "in the
past"); nothing here sleeps, and nothing counts ticks.
"""
import json
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
    """The default policy, made explicit. A developer with the env var set in
    their own shell would otherwise silently test the operator override."""
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE", raising=False)


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture(autouse=True)
def clean_event_log():
    """Process-global, so one test's events would land in the next one's
    assertions."""
    schedule._events.clear()
    yield
    schedule._events.clear()


@pytest.fixture(autouse=True)
def fresh_process():
    schedule._watched.clear()
    yield
    schedule._watched.clear()


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id=""):
        calls.append({"message": prompt, "session_id": session_id})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    # The thread BODY, not the function inside it — see test_schedule.py for why
    # stubbing `record_session_when_ready` instead quietly closes the turn.
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


def _occurrences(template_id):
    return [e for e in schedule.list_entries()
            if str(e.get("template_id") or "") == template_id]


def _pending(template_id):
    return [o for o in _occurrences(template_id) if o["state"] == schedule.PENDING]


# ------------------------------------------------- one-offs: no bound at all

def test_a_one_off_missed_by_three_days_fires(target, spawned):
    """The headline change. Under the old 24-hour bound this was swept to
    `missed` and never sent; an unsent one-shot is GONE, so it now waits."""
    entry = schedule.create(str(target), "three days stale", _at(-3 * 86400))

    fired = schedule.tick()

    assert [e["id"] for e in fired] == [entry["id"]]
    assert [c["message"] for c in spawned] == ["three days stale"]
    assert _entries()[entry["id"]]["state"] == schedule.SENT


def test_ten_one_offs_missed_over_two_weeks_all_fire(target, spawned):
    """One-offs are NOT coalesced — each is a different thing the user asked
    for, so all ten go. (Coalescing is only ever right for repeats, where the
    ten are ten copies of one instruction.)"""
    for day in range(1, 11):
        schedule.create(str(target), f"day -{day}", _at(-day * 86400))

    schedule.tick()

    assert len(spawned) == 10
    # ...and in DUE order, oldest first: the queue runs the way it reads.
    assert [c["message"] for c in spawned] == [f"day -{d}"
                                               for d in range(10, 0, -1)]


def test_an_operator_bound_still_expires_a_stale_one_off(target, spawned,
                                                          monkeypatch):
    """The escape hatch. An install that sets FUSED_RENDER_SCHEDULE_MAX_LATE
    explicitly keeps the old shape — the bound is a decision someone MADE, where
    the 24-hour default was a decision a constant made for them."""
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", str(3600))
    entry = schedule.create(str(target), "three days stale", _at(-3 * 86400))

    fired = schedule.tick()

    assert fired == []
    assert spawned == []
    stored = _entries()[entry["id"]]
    assert stored["state"] == schedule.MISSED
    assert "not running" in stored["error"]


def test_an_already_missed_entry_never_fires_once_the_bound_is_gone(target,
                                                                    spawned,
                                                                    monkeypatch):
    """THE reason this change ships without a migration.

    Removing the bound cannot resurrect old work, because the sweep does not
    re-derive its verdict on every tick — it WRITES `missed` to the store and
    persists it, and it only ever acts on entries that are still `pending`.
    Anything that outlived the old bound is already terminal, and terminal is
    invisible to every pass here.

    This test builds exactly that store — an entry swept under a bound — and
    then removes the bound, which is what upgrading looks like from the store's
    side."""
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "60")
    entry = schedule.create(str(target), "expired long ago", _at(-30))
    schedule.tick(now=_at(600))
    assert _entries()[entry["id"]]["state"] == schedule.MISSED

    # The upgrade: the bound goes away, and the app is opened again.
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE")
    assert schedule.max_late_seconds() is None
    assert schedule.tick() == []
    assert schedule.tick(now=_at(86400)) == []

    assert spawned == []
    assert _entries()[entry["id"]]["state"] == schedule.MISSED
    # and it is not in the queue either — a queue is what WILL run
    assert schedule.queue()["queued"] == []


# --------------------------------------------------- recurrences: coalescing

def test_five_missed_daily_runs_produce_one_send_and_a_four_skipped_report(
        target, spawned):
    """The rule the spec names. A week of "daily at 9am" replayed into one
    thread is not what the words meant; one late run is."""
    template = schedule.create(str(target), "daily digest", repeats="0 9 * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])

    # Four more 9am runs have come and gone: five in all, of which the store
    # holds exactly ONE (the materializer keeps one run ahead and no more, so
    # the other four exist only in the cron line). The extra hour keeps the
    # count exact across a DST shift in either direction.
    schedule.tick(now=first_due + timedelta(days=4, hours=1))

    assert len(spawned) == 1
    ran = _entries()[first["id"]]
    assert ran["state"] == schedule.SENT
    assert ran["skipped"] == 4
    assert ran["skipped_note"] == "4 earlier runs skipped"
    # It ran as the LATEST of the five, not as the oldest.
    assert schedule.parse_due(ran["due"]) == first_due + timedelta(days=4)


def test_the_collapse_is_announced_once_not_once_per_dropped_run(target, spawned):
    """A toast per skipped run is the storm coalescing exists to prevent, so the
    whole collapse is one event carrying the count."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first_due = schedule.parse_due(_occurrences(template["id"])[0]["due"])

    schedule.tick(now=first_due + timedelta(minutes=30))

    missed = [e for e in schedule.event_log() if e["kind"] == schedule.EVENT_MISSED]
    assert len(missed) == 1
    assert missed[0]["detail"] == "6 earlier runs skipped"


def test_several_stale_pending_occurrences_collapse_into_the_newest(target,
                                                                    spawned):
    """The other shape of backlog. `_materialize` keeps only one run ahead, so a
    second past-due pending occurrence never arrives from there — but `restore`
    can put one beside its successor, and a human can edit the store. Both are
    real entries, so the losers are marked `missed` rather than deleted."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])

    # Skip the first run, let the materializer move on, then unskip it — which
    # is exactly the sequence that leaves two pending under one template.
    schedule.cancel(first["id"])
    schedule.tick(now=first_due - timedelta(minutes=1))
    schedule.restore(first["id"])
    assert len(_pending(template["id"])) == 2
    second = next(o for o in _pending(template["id"]) if o["id"] != first["id"])

    # Now the app is closed until both are past due.
    schedule.tick(now=first_due + timedelta(minutes=6))

    assert len(spawned) == 1
    assert _entries()[first["id"]]["state"] == schedule.MISSED
    assert "only the latest" in _entries()[first["id"]]["error"]
    ran = _entries()[second["id"]]
    assert ran["state"] == schedule.SENT
    assert ran["skipped"] == 1
    assert ran["skipped_note"] == "1 earlier run skipped"


def test_a_rule_series_spends_its_count_budget_on_skipped_runs(target, spawned):
    """`made` counts what the template put on the calendar, and `create` is
    explicit that skipped runs count — "ends after N occurrences" is a promise
    about runs that were scheduled, not about runs that happened to fire. So a
    collapse spends the budget, and a series can end inside one."""
    template = schedule.create(str(target), "run", due=_at(60),
                               rule={"freq": "hour", "count": 3})
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])
    assert _entries()[template["id"]]["made"] == 1

    # Five hours later: the rule only ever had three runs in it.
    schedule.tick(now=first_due + timedelta(hours=5))

    assert len(spawned) == 1
    ran = _entries()[first["id"]]
    assert ran["skipped"] == 2                       # runs 2 and 3, collapsed
    assert schedule.parse_due(ran["due"]) == first_due + timedelta(hours=2)
    assert _entries()[template["id"]]["made"] == 3
    # Spent: nothing further is ever materialized, and the template stays
    # `recurring` with nothing ahead of it rather than acquiring a new state.
    schedule.tick(now=first_due + timedelta(hours=6))
    schedule.tick(now=first_due + timedelta(days=9))
    assert _pending(template["id"]) == []
    assert len(_occurrences(template["id"])) == 1
    assert _entries()[template["id"]]["state"] == schedule.RECURRING
    assert len(spawned) == 1


def test_a_legacy_occurrence_bound_is_cleared_rather_than_obeyed(target, spawned):
    """A store written by the previous build carries `max_late: 120` on every
    occurrence — the skip-not-catch-up bound coalescing replaced. Left in place
    it would sweep the very run the collapse just decided to send, so the
    coalescer clears it. Read defensively either way: the store is a JSON file a
    human may edit."""
    template = schedule.create(str(target), "run", repeats="0 * * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])
    schedule._update(first["id"], max_late=120)      # what the old build wrote

    schedule.tick(now=first_due + timedelta(minutes=45))

    assert len(spawned) == 1
    stored = _entries()[first["id"]]
    assert stored["state"] == schedule.SENT
    assert "max_late" not in stored


def test_a_nonsense_max_late_falls_back_instead_of_deciding(target, spawned):
    """The existing defensive-read example, re-pinned against the new default: a
    hand-edited `max_late` that is not a usable number must not be what decides
    whether a message is sent."""
    entry = schedule.create(str(target), "hand edited", _at(-3 * 86400))
    path = schedule.store_path()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data["entries"][0]["max_late"] = "quite a while"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    schedule.tick()

    assert [c["message"] for c in spawned] == ["hand edited"]
    assert _entries()[entry["id"]]["state"] == schedule.SENT


# ------------------------------------------------------ scheduling backwards

def test_scheduling_three_days_back_is_accepted_and_queued_at_the_head(target,
                                                                       spawned):
    """Picking a past date on the calendar now means "run this, and file it
    under then". Two halves, and both matter: the due time is stored AS GIVEN so
    history reads truthfully, and because the queue runs in due order a past due
    time sorts ahead of everything later — which is what "head of the queue"
    means here, and why it goes on the next tick."""
    ahead = schedule.create(str(target), "due in a minute", _at(-30))
    back = schedule.create(str(target), "backdated", _at(-3 * 86400))

    assert schedule.parse_due(back["due"]) < schedule._now() - timedelta(days=2)
    assert [e["message"] for e in schedule.queue()["queued"]] == [
        "backdated", "due in a minute"]

    schedule.tick()
    assert [c["message"] for c in spawned] == ["backdated", "due in a minute"]
    assert _entries()[ahead["id"]]["state"] == schedule.SENT


# -------------------------------------------------------------- the queue read

def test_the_queue_is_what_is_waiting_and_what_is_in_flight(target, spawned):
    """`queued` is past-due-and-pending in run order; `running` is `sending`.
    Deliberately narrow at both ends: a message due tomorrow is scheduled, not
    queued, and cancel-all must not reach it."""
    later = schedule.create(str(target), "tomorrow", _at(86400))
    soon = schedule.create(str(target), "overdue", _at(-60))
    older = schedule.create(str(target), "very overdue", _at(-600))
    schedule._update(later["id"], state=schedule.SENDING,
                     fired=schedule._now().isoformat())

    q = schedule.queue()

    assert [e["message"] for e in q["queued"]] == ["very overdue", "overdue"]
    assert [e["message"] for e in q["running"]] == ["tomorrow"]
    assert older["id"] == q["queued"][0]["id"]
    assert soon["id"] == q["queued"][1]["id"]


def test_reading_the_queue_changes_nothing(target, spawned):
    """A GET that materialized, coalesced, or claimed would make merely LOOKING
    at the queue decide what runs. The tick owns every state change."""
    schedule.create(str(target), "run", repeats="*/5 * * * *")
    schedule.create(str(target), "overdue", _at(-60))
    before = json.dumps(schedule.list_entries(), sort_keys=True)

    schedule.queue()

    assert json.dumps(schedule.list_entries(), sort_keys=True) == before
    assert spawned == []


# ------------------------------------------------------------------- cancel

def test_cancelling_a_queued_entry_takes_it_out_of_the_queue(target, spawned):
    entry = schedule.create(str(target), "never mind", _at(-3 * 86400))

    result = schedule.cancel_queued([entry["id"]])

    assert result["cancelled"] == [entry["id"]]
    assert result["refused"] == []
    assert schedule.queue()["queued"] == []
    assert schedule.tick() == []
    assert spawned == []
    assert _entries()[entry["id"]]["state"] == schedule.CANCELLED


def test_cancel_all_means_the_queue_now_not_everything_scheduled(target, spawned):
    """Recomputed under the lock rather than trusted from the client: "all" must
    mean the queue as it IS, so a message that came due while the popover was
    open is cancelled with the rest — and a message due tomorrow, which was
    never on the popover, is not."""
    a = schedule.create(str(target), "queued a", _at(-600))
    b = schedule.create(str(target), "queued b", _at(-60))
    future = schedule.create(str(target), "tomorrow", _at(86400))

    result = schedule.cancel_queued(all_queued=True)

    assert sorted(result["cancelled"]) == sorted([a["id"], b["id"]])
    assert _entries()[future["id"]]["state"] == schedule.PENDING
    schedule.tick()
    assert spawned == []


def test_cancelling_something_already_claimed_is_refused_not_forced(target,
                                                                    spawned):
    """The claim race, answered honestly.

    Between reading the queue and pressing Cancel, the tick can claim an entry
    and spawn its helper. Writing `cancelled` over `sending` would be a claim
    this module cannot make good on — the process is away — and it would also
    destroy the record the stuck sweep needs to report an interrupted send. So
    the entry is refused, with a reason the popover can show, and it is left
    EXACTLY as it was."""
    entry = schedule.create(str(target), "already away", _at(-60))
    schedule._update(entry["id"], state=schedule.SENDING,
                     fired=schedule._now().isoformat())
    before = dict(_entries()[entry["id"]])

    result = schedule.cancel_queued([entry["id"]])

    assert result["cancelled"] == []
    assert result["refused"] == [entry["id"]]
    assert "already running" in result["reasons"][entry["id"]]
    # untouched, down to the claim stamp the stuck sweep reads
    assert _entries()[entry["id"]] == before

    # ...and that record still does its job: the sweep reports the interrupted
    # send rather than finding a corrupted entry.
    schedule.tick(now=_at(schedule._SENDING_STUCK_S + 60))
    swept = _entries()[entry["id"]]
    assert swept["state"] == schedule.ERROR
    assert "interrupted" in swept["error"]
    assert spawned == []


def test_a_turn_already_running_is_refused_too(target, spawned):
    """`sent` with no verdict is a live turn. It has its own cancel — the job
    row's ✕, which really does stop the run — and it is past the point this
    surface can withdraw anything."""
    entry = schedule.create(str(target), "in flight", _at(-60))
    schedule.tick()
    assert _entries()[entry["id"]]["state"] == schedule.SENT

    result = schedule.cancel_queued([entry["id"]])

    assert result["refused"] == [entry["id"]]
    assert "already running" in result["reasons"][entry["id"]]
    assert _entries()[entry["id"]]["state"] == schedule.SENT


def test_cancel_reports_every_id_it_could_not_take(target, spawned):
    """Partial success is the normal outcome, so nothing is silently dropped: a
    request naming four entries where two got away is two cancellations and two
    named refusals."""
    done = schedule.create(str(target), "sent", _at(-120))
    schedule.tick()
    schedule._update(done["id"], turn="ok")
    queued = schedule.create(str(target), "queued", _at(-60))
    also = schedule.create(str(target), "also queued", _at(-90))

    result = schedule.cancel_queued([queued["id"], done["id"], also["id"],
                                     "no-such-id"])

    assert sorted(result["cancelled"]) == sorted([queued["id"], also["id"]])
    assert sorted(result["refused"]) == sorted([done["id"], "no-such-id"])
    assert result["reasons"][done["id"]] == f"already {schedule.SENT}"
    assert "no scheduled message" in result["reasons"]["no-such-id"]


def test_a_cancel_landing_between_the_sweep_and_the_claim_wins(target, spawned,
                                                               monkeypatch):
    """The other side of the same race. `_claim` re-reads under the lock, so a
    cancel that lands in the window between the sweep's verdict and the claim
    takes the entry and the tick simply skips it — no send, no corruption."""
    entry = schedule.create(str(target), "never mind", _at(-5))
    real_claim = schedule._claim

    def cancel_then_claim(entry_id, now):
        schedule.cancel_queued([entry_id])
        return real_claim(entry_id, now)

    monkeypatch.setattr(schedule, "_claim", cancel_then_claim)

    assert schedule.tick() == []
    assert spawned == []
    assert _entries()[entry["id"]]["state"] == schedule.CANCELLED


def test_cancelling_a_queued_occurrence_skips_it_and_keeps_the_schedule(target,
                                                                        spawned):
    """Cancel means the same thing here as everywhere else in the module: on an
    occurrence it is "skip this one", not "stop the schedule"."""
    template = schedule.create(str(target), "run", repeats="*/5 * * * *")
    first = _occurrences(template["id"])[0]
    first_due = schedule.parse_due(first["due"])

    schedule.cancel_queued([first["id"]])

    assert _entries()[template["id"]]["state"] == schedule.RECURRING
    schedule.tick(now=first_due + timedelta(seconds=1))
    assert spawned == []
    assert len(_pending(template["id"])) == 1


# ------------------------------------------------------- claim before spawn

def test_the_claim_is_still_written_before_the_spawn(target, monkeypatch):
    """Re-proved here because cancel touches the claim path. The order is what
    makes a crash safe: if the process dies inside the helper the entry is
    already out of `pending`."""
    seen = {}

    def spawn_and_look(target_, prompt, mode, session_id=""):
        seen["state"] = schedule.list_entries()[0]["state"]
        seen["queue"] = [e["id"] for e in schedule.queue()["queued"]]
        seen["running"] = [e["id"] for e in schedule.queue()["running"]]
        return {"run_id": "r-1"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", spawn_and_look)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    entry = schedule.create(str(target), "hi", _at(-5))

    schedule.tick()

    assert seen["state"] == schedule.SENDING
    # and the queue surface agrees with the store while the helper is away: off
    # the queue, on the running list. A row that read "queued" here would offer
    # a cancel that could not be honoured.
    assert seen["queue"] == []
    assert seen["running"] == [entry["id"]]


def test_a_crash_between_the_claim_and_the_spawn_can_never_re_send(target,
                                                                    monkeypatch,
                                                                    spawned):
    """What the order buys, stated as the failure it prevents.

    The process dies after `_claim` has written `sending` and before the helper
    is away — simulated by a `_send` that never returns to the store. The entry
    that survives is `sending`, NOT `pending`, so no later tick sends it: it is
    reported as interrupted instead. An unsent message is a disappointment; a
    message sent five times over five crash-restarts is an agent running
    unattended five times."""
    schedule.create(str(target), "interrupted", _at(-3 * 86400))
    real_send = schedule._send
    crashed = {"yes": True}
    # A `_send` that never reaches the helper and never touches the store: what
    # the process dying between the claim and the spawn leaves behind.
    monkeypatch.setattr(schedule, "_send",
                        lambda entry: None if crashed["yes"] else real_send(entry))

    schedule.tick()
    stored = schedule.list_entries()[0]
    assert stored["state"] == schedule.SENDING
    assert spawned == []

    # Restart: `_send` works again, and the store is swept many times over.
    crashed["yes"] = False
    for minutes in (1, 10, 60):
        schedule.tick(now=_at(60 * minutes))

    assert spawned == []
    assert schedule.list_entries()[0]["state"] == schedule.ERROR
    assert "interrupted" in schedule.list_entries()[0]["error"]


# --------------------------------------------------------------- the HTTP skin

def test_the_queue_endpoint_lists_queued_and_running(client, target):
    created = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "backdated",
                                "due": _at(-3 * 86400).isoformat()})
    assert created.status_code == 200
    entry = created.json()["entry"]

    body = client.get("/api/schedule/queue").json()

    assert [e["id"] for e in body["queued"]] == [entry["id"]]
    assert body["running"] == []
    # the read is open, like every other read endpoint
    assert client.get("/api/schedule/queue").status_code == 200


def test_the_listing_reports_the_bound_as_null_when_there_is_none(client):
    """The page cannot explain a `missed` entry without the bound, and it cannot
    explain the absence of one from a made-up number either."""
    assert client.get("/api/schedule").json()["max_late_seconds"] is None


def test_queue_cancel_carries_the_write_guard(client, target):
    unguarded = client.post("/api/schedule/queue/cancel", json={"all": True})
    assert unguarded.status_code == 403


def test_queue_cancel_takes_ids_or_all_and_refuses_neither(client, target):
    for i in range(2):
        client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": f"m{i}",
                          "due": _at(-600 * (i + 1)).isoformat()})
    ids = [e["id"] for e in client.get("/api/schedule/queue").json()["queued"]]
    assert len(ids) == 2

    bad = client.post("/api/schedule/queue/cancel", headers=WRITE, json={})
    assert bad.status_code == 400
    assert "entry_ids" in bad.json()["error"]

    one = client.post("/api/schedule/queue/cancel", headers=WRITE,
                      json={"entry_ids": [ids[0]]}).json()
    assert one == {"ok": True, "cancelled": [ids[0]], "refused": [],
                   "reasons": {}}

    rest = client.post("/api/schedule/queue/cancel", headers=WRITE,
                       json={"all": True}).json()
    assert rest["cancelled"] == [ids[1]]
    assert client.get("/api/schedule/queue").json()["queued"] == []


def test_queue_cancel_reports_an_already_running_entry_rather_than_failing(
        client, target):
    """200 with two lists, not a status code: partial success is the normal
    outcome of a cancel that races the claim."""
    created = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "away",
                                "due": _at(-60).isoformat()})
    entry_id = created.json()["entry"]["id"]
    schedule._update(entry_id, state=schedule.SENDING,
                     fired=schedule._now().isoformat())

    body = client.post("/api/schedule/queue/cancel", headers=WRITE,
                       json={"entry_ids": [entry_id]})

    assert body.status_code == 200
    assert body.json()["cancelled"] == []
    assert body.json()["refused"] == [entry_id]
    assert "already running" in body.json()["reasons"][entry_id]
    assert schedule.list_entries()[0]["state"] == schedule.SENDING

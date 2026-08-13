"""What a scheduled message tells you while it runs, and after (D276).

A scheduled message is the one kind of work in the app that nobody is looking at
when it happens, so these two surfaces are the feature, not decoration:

* the **job registry** (jobs.py, D244) — one `task` row per send, live in the
  shell's download manager, carrying the turn's phase and whether it is parked on
  a permission card nobody has answered;
* the **event log** — append-only, monotonically ided, what the shell polls and
  turns into toasts for a message that ran, failed, or was missed.

The turn watcher is driven through its `_turn_tick` seam with fabricated `_poll`
results: no test here runs a real claude, and none waits on a thread.
"""
import pytest

from fused_render import claude_spawn, jobs, schedule, schedule_wake


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def clean_registries(monkeypatch):
    """The job registry and the event log are process-global and in memory, so
    each test starts with both empty."""
    jobs.reset()
    schedule._events.clear()
    monkeypatch.setattr(schedule, "_event_seq", 0)
    monkeypatch.setattr(schedule, "_delivered", 0)
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)
    yield
    jobs.reset()
    schedule._events.clear()


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


@pytest.fixture()
def sent(monkeypatch):
    """A send that succeeds, with the turn watcher's thread stubbed out — the
    watcher is driven directly by the tests that care about it."""
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **k: {"run_id": "r-1"})
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)


def _overdue_time():
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(seconds=5)


def _overdue(target, message="do the thing", **kw):
    return schedule.create(str(target), message, _overdue_time(), **kw)


def _kinds():
    return [e["kind"] for e in schedule.event_log()]


def _no_watcher_thread(monkeypatch):
    """Stop `_send` from starting a watcher THREAD, and hand back the real
    `_watch_turn` to drive synchronously instead.

    A daemon thread that emits is a cross-test leak, not a detail: the event log
    is process-global and cleared per test, so a watcher still finishing when the
    next test starts appends into ITS log. That is exactly what turned two
    unrelated assertions red on CI while passing locally on timing."""
    real = schedule._watch_turn
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return real


class FakeAgent:
    """Stands in for the claude backend: records what was cancelled."""

    def __init__(self):
        self.cancelled = []

    def _cancel(self, run_id):
        self.cancelled.append(run_id)


# ------------------------------------------------------------------ the send

def test_a_send_opens_a_running_job_row(target, sent):
    entry = _overdue(target, "update the changelog")
    schedule.tick()

    row = jobs.list_jobs()[0]
    assert row["id"] == f"sys:schedule:{entry['id']}"
    assert row["state"] == "running"
    assert row["kind"] == "task"
    # the prompt's first line is what the user will recognise in a column of
    # unrelated work
    assert row["title"] == "update the changelog"
    assert row["detail"] == str(target)
    # this process owns the run, so the manager's ✕ is an action, not a request
    assert row["cancellable"] is True
    assert row["owner"] == "server"


def test_a_spawn_failure_reports_everywhere_at_once(target, monkeypatch):
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **k: {"error": "claude CLI not found"})
    _overdue(target)
    schedule.tick()

    assert schedule.list_entries()[0]["state"] == schedule.ERROR
    row = jobs.list_jobs()[0]
    assert row["state"] == "error"
    assert "claude CLI not found" in row["message"]
    assert _kinds() == [schedule.EVENT_FAILED]


def test_a_send_alone_announces_nothing(target, sent):
    """Starting is not news — the toast is for how it TURNED OUT. A message that
    is merely on its way would otherwise notify twice for one outcome."""
    _overdue(target)
    schedule.tick()

    assert _kinds() == []


# ------------------------------------------------------------- the live turn

def test_a_running_turn_reports_what_it_is_doing(target, sent):
    entry = _overdue(target)
    schedule.tick()
    agent = FakeAgent()

    assert schedule._turn_tick(entry, "r-1", agent,
                               {"done": False, "phase": "writing", "tokens": 1200}) is True

    row = jobs.list_jobs()[0]
    assert row["state"] == "running"
    assert row["detail"] == "writing · 1200 tokens"


def test_a_turn_parked_on_a_permission_card_says_so(target, sent):
    """The single most likely way an unattended session is stuck, and from the
    outside it looks identical to a slow one."""
    entry = _overdue(target)
    schedule.tick()

    schedule._turn_tick(entry, "r-1", FakeAgent(),
                        {"done": False, "phase": "thinking", "tokens": 40,
                         "permissions": [{"id": "p1", "tool": "Bash"}]})

    assert jobs.list_jobs()[0]["detail"] == "waiting for permission"


def test_the_session_the_turn_ran_in_is_captured(target, sent):
    """A FRESH scheduled send creates a session nothing else in the app knows the
    id of, and the Inbox addresses a session by exactly that id — so without this
    the row has nothing to link to."""
    entry = _overdue(target)
    schedule.tick()

    schedule._turn_tick(entry, "r-1", FakeAgent(),
                        {"done": False, "session_id": "sess-new", "phase": "thinking"})

    stored = schedule.list_entries()[0]
    assert stored["claude_session_id"] == "sess-new"
    # …and the INPUT is left alone, so a fresh send is not retroactively relabelled
    # as a continuation of something
    assert stored["session_id"] == ""


def test_a_resumed_send_records_the_same_session_it_continued(target, sent):
    entry = schedule.create(str(target), "and another thing",
                            _overdue_time(), session_id="sess-a")
    schedule.tick()
    schedule._turn_tick(entry, "r-1", FakeAgent(),
                        {"done": True, "session_id": "sess-a", "error": ""})

    stored = schedule.list_entries()[0]
    assert stored["session_id"] == "sess-a"
    assert stored["claude_session_id"] == "sess-a"


def test_a_run_that_never_reports_a_session_leaves_the_field_empty(target, sent):
    """No id, no link — the row simply does not offer one rather than pointing at
    a session that does not exist."""
    entry = _overdue(target)
    schedule.tick()
    schedule._turn_tick(entry, "r-1", FakeAgent(), {"done": True, "error": "died early"})

    assert schedule.list_entries()[0]["claude_session_id"] == ""


def test_a_finished_turn_lands_ok_on_all_three_surfaces(target, sent):
    entry = _overdue(target)
    schedule.tick()

    assert schedule._turn_tick(entry, "r-1", FakeAgent(),
                               {"done": True, "error": ""}) is False

    assert schedule.list_entries()[0]["turn"] == "ok"
    assert jobs.list_jobs()[0]["state"] == "done"
    assert _kinds() == [schedule.EVENT_DONE]


def test_a_turn_that_fails_after_a_clean_send_is_reported_as_the_TURN_failing(target, sent):
    """`state` says whether the message was SENT; `turn` says how the session
    went. Reporting a dead turn as a send failure would send the user looking in
    the wrong place."""
    entry = _overdue(target)
    schedule.tick()

    schedule._turn_tick(entry, "r-1", FakeAgent(),
                        {"done": True, "error": "the model gave up"})

    stored = schedule.list_entries()[0]
    assert stored["state"] == schedule.SENT      # the send was fine
    assert stored["turn"] == "failed"            # the turn was not
    assert "the model gave up" in stored["error"]
    assert jobs.list_jobs()[0]["state"] == "error"
    assert _kinds() == [schedule.EVENT_FAILED]


def test_the_managers_cancel_really_stops_the_run(target, sent):
    entry = _overdue(target)
    schedule.tick()
    agent = FakeAgent()

    jobs.request_cancel(f"sys:schedule:{entry['id']}")
    assert schedule._turn_tick(entry, "r-1", agent,
                               {"done": False, "phase": "writing"}) is False

    assert agent.cancelled == ["r-1"]  # an ACTION, not a request
    assert schedule.list_entries()[0]["turn"] == "cancelled"
    assert jobs.list_jobs()[0]["state"] == "cancelled"


def test_a_failed_cancel_still_stops_watching(target, sent, monkeypatch):
    """A run that cannot be signalled (already gone) must not leave the watcher
    looping over a job row the user has closed."""
    entry = _overdue(target)
    schedule.tick()

    class Stubborn:
        def _cancel(self, run_id):
            raise OSError("no such process")

    jobs.request_cancel(f"sys:schedule:{entry['id']}")
    assert schedule._turn_tick(entry, "r-1", Stubborn(),
                               {"done": False}) is False
    assert schedule.list_entries()[0]["turn"] == "cancelled"


# -------------------------------------------------------------- missed / swept

def test_a_missed_message_announces_itself(target, sent, monkeypatch):
    """The case the whole log exists for: nobody was there, and without this
    the only way to find out is to go looking."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "60")
    schedule.create(str(target), "stale", datetime.now(timezone.utc) - timedelta(seconds=30))
    schedule.tick(now=datetime.now(timezone.utc) + timedelta(seconds=120))

    assert _kinds() == [schedule.EVENT_MISSED]
    event = schedule.event_log()[0]
    assert event["message"] == "stale"      # identifies it without a hunt
    assert event["target"] == str(target)
    assert "not running" in event["detail"]


def test_an_interrupted_send_announces_itself(target, sent):
    from datetime import datetime, timedelta, timezone

    entry = _overdue(target)
    schedule._update(entry["id"], state=schedule.SENDING,
                     fired=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())

    schedule.tick()

    assert _kinds() == [schedule.EVENT_FAILED]
    assert "interrupted" in schedule.event_log()[0]["detail"]


# ------------------------------------------------------------------ the log

def test_event_ids_are_monotonic_so_a_poller_can_track_a_high_water_mark(target, sent):
    for i in range(3):
        entry = _overdue(target, f"msg {i}")
        schedule.tick()
        schedule._turn_tick(entry, "r-1", FakeAgent(), {"done": True, "error": ""})

    ids = [e["id"] for e in schedule.event_log()]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3


def test_the_log_is_bounded(target, sent, monkeypatch):
    """A running narration for the UI to toast, not history — the store is what
    holds every outcome durably."""
    monkeypatch.setattr(schedule, "_EVENTS_MAX", 5)
    for i in range(12):
        schedule._emit(schedule.EVENT_DONE, {"id": f"e{i}", "message": "x", "target": "/t"})

    log = schedule.event_log()
    assert len(log) == 5
    assert [e["entry_id"] for e in log] == [f"e{i}" for i in range(7, 12)]


def test_a_watch_that_ends_without_a_verdict_closes_the_row(target, monkeypatch):
    """Every way the watch can stop early — a `load_agent` that raises, a `_poll`
    that raises, the ~1h tick cap a long turn outruns — used to leave `turn` empty,
    which the page reads as "Running…" and the toast logic as nothing to say. A row
    that claims to be running with nobody watching is the frozen-progress-bar lie
    the job registry's `stalled` state exists to avoid."""
    monkeypatch.setattr(claude_spawn, "spawn_helper", lambda *a, **k: {"run_id": "r-1"})
    # the recorder returns having never seen `done` (the tick cap / a raised poll)
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: FakeAgent())
    monkeypatch.setattr(claude_spawn, "record_session_when_ready",
                        lambda agent, run_id, on_tick=None: None)
    # Drive the watcher HERE, not on the thread `_send` starts. Letting that
    # thread run made this test emit into whatever event log was current when it
    # got round to finishing — it leaked a `failed` into two other tests on CI.
    watch = _no_watcher_thread(monkeypatch)

    entry = _overdue(target)
    schedule.tick()
    watch(entry, "r-1")

    stored = schedule.list_entries()[0]
    assert stored["turn"] == "unknown"        # honest: the app stopped being able to say
    assert stored["run_id"] == "r-1"          # and this is how to go and read it
    assert "stopped reporting" in stored["error"]
    assert jobs.list_jobs()[0]["state"] == "error"
    assert _kinds() == [schedule.EVENT_FAILED]


def test_closing_an_unwatched_row_never_overwrites_a_real_outcome(target, sent):
    """`_turn_tick` may have resolved the entry seconds before the recorder
    returned, so the close re-reads instead of trusting the copy the watcher thread
    has held since the send."""
    entry = _overdue(target)
    schedule.tick()
    schedule._turn_tick(entry, "r-1", FakeAgent(), {"done": True, "error": ""})

    schedule._close_unwatched(entry, "stopped reporting")

    assert schedule.list_entries()[0]["turn"] == "ok"   # not clobbered to unknown
    assert _kinds() == [schedule.EVENT_DONE]            # and no second toast


def test_a_load_agent_failure_still_closes_the_row(target, monkeypatch):
    monkeypatch.setattr(claude_spawn, "spawn_helper", lambda *a, **k: {"run_id": "r-2"})
    monkeypatch.setattr(claude_spawn, "load_agent",
                        lambda: (_ for _ in ()).throw(RuntimeError("no backend")))
    watch = _no_watcher_thread(monkeypatch)

    entry = _overdue(target)
    schedule.tick()
    watch(entry, "r-2")

    assert schedule.list_entries()[0]["turn"] == "unknown"
    assert _kinds() == [schedule.EVENT_FAILED]


def test_startup_events_survive_until_a_shell_actually_narrates_them(target, sent,
                                                                    monkeypatch):
    """The bug the delivery mark exists for.

    The catch-up pass emits its `missed` verdicts on the scheduler's FIRST tick,
    which lands long before a shell has loaded. Under the original design (a
    client-side "first poll is a silent baseline", copied from the mount-health
    poller) that first poll marked them seen and never said a word — swallowing
    precisely the events the log was added to deliver."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "60")
    schedule.create(str(target), "missed while away",
                    datetime.now(timezone.utc) - timedelta(seconds=30))
    # the app was closed for the whole window; this is the first tick after launch
    schedule.tick(now=datetime.now(timezone.utc) + timedelta(seconds=120))

    # a shell that loads later still gets it
    pending = schedule.undelivered_events()
    assert [e["kind"] for e in pending] == [schedule.EVENT_MISSED]

    # ...and once it confirms, a reload is quiet
    schedule.ack_events(pending[-1]["id"])
    assert schedule.undelivered_events() == []
    # while the durable record is untouched — the store, not the ring, is history
    assert schedule.list_entries()[0]["state"] == schedule.MISSED
    assert len(schedule.event_log()) == 1


def test_acking_only_ever_moves_forward(target, sent):
    entry = _overdue(target)
    schedule.tick()
    schedule._turn_tick(entry, "r-1", FakeAgent(), {"done": True, "error": ""})
    latest = schedule.event_log()[-1]["id"]

    assert schedule.ack_events(latest) == latest
    # a replayed or out-of-order ack must not re-arm what was already shown
    assert schedule.ack_events(1) == latest
    assert schedule.ack_events(-5) == latest
    assert schedule.undelivered_events() == []


def test_a_read_alone_never_consumes_a_notification(target, sent):
    """Draining is the ack's job. A duplicate poll, a second window, or a
    speculative fetch must not cost the user a toast."""
    entry = _overdue(target)
    schedule.tick()
    schedule._turn_tick(entry, "r-1", FakeAgent(), {"done": True, "error": ""})

    first = schedule.undelivered_events()
    assert len(first) == 1
    assert schedule.undelivered_events() == first
    assert schedule.undelivered_events() == first


def test_reporting_failures_never_break_a_send(target, monkeypatch):
    """The registry is not authoritative: a jobs call that raises must not cost
    the message its send."""
    monkeypatch.setattr(claude_spawn, "spawn_helper", lambda *a, **k: {"run_id": "r-9"})
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    monkeypatch.setattr(jobs, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    _overdue(target)
    schedule.tick()

    assert schedule.list_entries()[0]["state"] == schedule.SENT


def test_the_watcher_wraps_the_recorder_rather_than_replacing_it(target, monkeypatch):
    """The sidecar write and the commit live in record_session_when_ready and
    must happen whether or not anything is observing, so the watcher goes
    THROUGH it and only adds the observer."""
    seen = {}

    def fake_record(agent, run_id, on_tick=None):
        seen["run_id"] = run_id
        seen["has_observer"] = on_tick is not None

    monkeypatch.setattr(claude_spawn, "load_agent", lambda: FakeAgent())
    monkeypatch.setattr(claude_spawn, "record_session_when_ready", fake_record)

    schedule._watch_turn({"id": "e1", "target": "/t", "message": "m"}, "r-7")

    assert seen == {"run_id": "r-7", "has_observer": True}


def test_an_observer_that_raises_does_not_abandon_the_run(monkeypatch):
    """record_session_when_ready owns the sidecar write; an observer's exception
    must not stop the poll before it happens."""
    ticks = []

    class Agent:
        def _poll(self, run_id):
            ticks.append(run_id)
            return {"done": len(ticks) >= 2}

    monkeypatch.setattr(claude_spawn, "_RECORD_POLL_INTERVAL", 0)

    claude_spawn.record_session_when_ready(
        Agent(), "r-1",
        on_tick=lambda data: (_ for _ in ()).throw(RuntimeError("observer boom")))

    assert len(ticks) == 2  # kept polling to `done` regardless

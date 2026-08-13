"""Scheduled Claude messages — the model (fused_render/schedule.py).

The store, the firing decision, and the two rules that make "the app owns the
send" survivable: wall-clock comparison (so a due time that passed while the app
was closed still fires) and a bound on how late that is still worth doing.

Nothing here spawns a real claude: the send is stubbed at claude_spawn's
`spawn_helper` seam, and the wake stub is stubbed everywhere so no test writes a
LaunchAgent on a developer's own macOS machine.
"""
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from fused_render import claude_spawn, schedule, schedule_wake


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A per-test store. FUSED_RENDER_HOME is already redirected suite-wide, but
    these tests assert on exact list contents, so they get a dir of their own."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    """The wake stub, recorded instead of installed. Without this, running the
    suite on macOS would write and load a real LaunchAgent."""
    calls = []
    monkeypatch.setattr(schedule_wake, "sync", lambda due: calls.append(list(due)))
    return calls


@pytest.fixture()
def spawned(monkeypatch):
    """Every send that gets as far as the helper, recorded. Returns a run_id so
    the entry lands in `sent`; a test wanting failure re-stubs this."""
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id=""):
        calls.append({"target": target, "message": prompt,
                      "permission_mode": permission_mode,
                      "session_id": session_id})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    # The sidecar-recording thread is bookkeeping; keep it out of the way rather
    # than let it exec_module the real agent backend per test.
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: None)
    monkeypatch.setattr(claude_spawn, "record_session_when_ready",
                        lambda agent, run_id: None)
    return calls


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    (d / "index.html").write_text("<html></html>")
    return d


def _in(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ------------------------------------------------------------------ creating

def test_create_stores_a_pending_entry(target):
    entry = schedule.create(str(target), "ship it", _in(600))

    assert entry["state"] == schedule.PENDING
    assert entry["target"] == str(target)
    assert entry["message"] == "ship it"
    # the default is the unattended mode, and it is RECORDED on the entry rather
    # than re-derived at send time
    assert entry["permission_mode"] == "auto"
    assert schedule.list_entries() == [entry]


@pytest.mark.parametrize("mode", ["prompt", "auto", "acceptEdits", "plan"])
def test_every_mode_the_composer_can_offer_is_accepted(target, mode):
    """The composer hands its approvals pill straight through, so a mode it can
    show but the store refuses is a 400 naming a value the user never chose.
    `acceptEdits` was exactly that bug — see PERMISSION_MODES. The two lists are
    held together by test_claude_schedule_pill.py; this end pins the values."""
    assert schedule.create(str(target), "hi", _in(600),
                           permission_mode=mode)["permission_mode"] == mode


def test_create_rejects_what_a_caller_can_get_wrong(target):
    with pytest.raises(ValueError, match="message"):
        schedule.create(str(target), "   ", _in(600))
    with pytest.raises(ValueError, match="no such file or directory"):
        schedule.create(str(target / "nope"), "hi", _in(600))
    with pytest.raises(ValueError, match="ISO 8601"):
        schedule.create(str(target), "hi", "next tuesday")
    with pytest.raises(ValueError, match="permission_mode"):
        schedule.create(str(target), "hi", _in(600),
                        permission_mode="bypassPermissions")


def test_create_refuses_a_due_time_already_past_the_catch_up_bound(target):
    """Storing it would be a lie: the next tick would sweep it straight to
    `missed`, so the caller is told now instead of never."""
    with pytest.raises(ValueError, match="catch-up bound"):
        schedule.create(str(target), "hi", _in(-schedule.max_late_seconds() - 60))


@pytest.mark.parametrize("due", [
    "2038-01-19T03:14",              # EXACTLY what a datetime-local input sends
    "2038-01-19T03:14:07",
    "2038-01-19T03:14:07Z",
    "2038-01-19T03:14:07+02:00",
    "2038-01-19T03:14:07.123456+00:00",
])
def test_every_shape_a_caller_actually_sends_parses(target, due):
    """Minute precision is first in this list because it is the one the page
    sends and the one the tests did not cover: every other case here was written
    with `.isoformat()`, which always emits seconds.

    Verified against 3.10 as well as the pinned 3.12 — `requires-python` is
    >=3.10 and CI runs it, and pre-3.11 `fromisoformat` accepts only what
    `isoformat()` emits, which is why `Z` is normalised before the parse."""
    assert schedule.create(str(target), "hi", due)["state"] == schedule.PENDING


def test_naive_due_time_is_read_as_local_not_utc(target, monkeypatch):
    """A human typing "09:00" means their own clock. Reading it as UTC would fire
    at the wrong hour for everyone not on UTC."""
    naive = (datetime.now() + timedelta(hours=3)).replace(microsecond=0)
    entry = schedule.create(str(target), "hi", naive.isoformat())

    stored = datetime.fromisoformat(entry["due"])
    assert stored == naive.astimezone(timezone.utc)


def test_creating_syncs_the_wake_stub_with_pending_due_times(target, no_real_wake):
    a = schedule.create(str(target), "one", _in(600))
    b = schedule.create(str(target), "two", _in(1200))

    assert no_real_wake[-1] == [a["due"], b["due"]]


# ------------------------------------------------------------------- firing

def test_tick_sends_only_what_is_due(target, spawned):
    soon = schedule.create(str(target), "now please", _in(-5))
    later = schedule.create(str(target), "not yet", _in(3600))

    schedule.tick()

    assert [c["message"] for c in spawned] == ["now please"]
    by_id = {e["id"]: e for e in schedule.list_entries()}
    assert by_id[soon["id"]]["state"] == schedule.SENT
    assert by_id[soon["id"]]["run_id"] == "r-1"
    assert by_id[later["id"]]["state"] == schedule.PENDING


def test_a_due_time_that_passed_while_the_app_was_closed_still_fires(target, spawned):
    """The catch-up guarantee, and the reason nothing counts ticks: this entry
    came due two hours ago with no loop running, and the first tick sends it."""
    schedule.create(str(target), "overdue", _in(-7200))

    schedule.tick()

    assert [c["message"] for c in spawned] == ["overdue"]


def test_past_the_bound_it_is_missed_not_sent(target, spawned, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "60")
    entry = schedule.create(str(target), "stale", _in(-30))
    # now walk the clock past the bound without ever running a tick — the app was
    # closed for the whole window
    schedule.tick(now=datetime.now(timezone.utc) + timedelta(seconds=120))

    assert spawned == []
    stored = schedule.list_entries()[0]
    assert stored["id"] == entry["id"]
    assert stored["state"] == schedule.MISSED
    assert "not running" in stored["error"]


def test_a_sent_message_is_never_sent_twice(target, spawned):
    schedule.create(str(target), "once", _in(-5))

    schedule.tick()
    schedule.tick()
    schedule.tick()

    assert len(spawned) == 1


def test_the_claim_is_written_before_the_spawn(target, monkeypatch):
    """The crash-safety order: if the process dies inside the helper, the entry
    must already be out of `pending` so the next boot does not send it again."""
    seen = {}

    def spawn_and_look(target_, prompt, mode, session_id=""):
        # what the store says WHILE the helper is away
        seen["state"] = schedule.list_entries()[0]["state"]
        return {"run_id": "r-1"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", spawn_and_look)
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: None)
    monkeypatch.setattr(claude_spawn, "record_session_when_ready", lambda a, r: None)

    schedule.create(str(target), "hi", _in(-5))
    schedule.tick()

    assert seen["state"] == schedule.SENDING


def test_only_the_message_in_flight_is_claimed(target, monkeypatch):
    """Claiming the whole due batch up front inverted the point of claiming: a
    tick sends sequentially, so a process dying inside the FIRST helper left its
    siblings persisted as `sending` with no spawn behind them, and the stuck
    sweep then reported messages that were never attempted as interrupted.

    Only the one actually in flight may be claimed; the rest stay `pending` and
    are still sendable on the next tick or the next launch."""
    seen = []

    def spawn_and_look(target_, prompt, mode, session_id=""):
        # what the store says about EVERY entry while this one is away
        seen.append({e["message"]: e["state"] for e in schedule.list_entries()})
        return {"run_id": f"r-{len(seen)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", spawn_and_look)
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: None)
    monkeypatch.setattr(claude_spawn, "record_session_when_ready", lambda *a, **k: None)

    schedule.create(str(target), "first", _in(-20))
    schedule.create(str(target), "second", _in(-10))
    schedule.tick()

    # while "first" is in the helper, "second" has not been touched
    assert seen[0] == {"first": schedule.SENDING, "second": schedule.PENDING}
    # and it is claimed only when its own turn comes
    assert seen[1]["second"] == schedule.SENDING
    assert {e["message"]: e["state"] for e in schedule.list_entries()} == {
        "first": schedule.SENT, "second": schedule.SENT}


def test_a_cancel_landing_between_the_sweep_and_the_claim_wins(target, spawned,
                                                               monkeypatch):
    """The sweep's verdict is a moment old by the time the claim runs, so the
    claim re-reads: a message cancelled in that window must not be sent."""
    entry = schedule.create(str(target), "never mind", _in(-5))
    real_claim = schedule._claim

    def cancel_then_claim(entry_id, now):
        schedule.cancel(entry_id)
        return real_claim(entry_id, now)

    monkeypatch.setattr(schedule, "_claim", cancel_then_claim)

    assert schedule.tick() == []
    assert schedule.list_entries()[0]["state"] == schedule.CANCELLED
    assert entry["id"] == schedule.list_entries()[0]["id"]


def test_an_entry_stuck_in_sending_is_reported_not_retried(target, spawned):
    """What the claim above costs: a process that died mid-spawn leaves a
    `sending` entry. It becomes an error the user can read — never a resend."""
    schedule.create(str(target), "interrupted", _in(-5))
    stuck = schedule.list_entries()[0]["id"]
    schedule._update(stuck, state=schedule.SENDING,
                     fired=_in(-3600).isoformat())

    schedule.tick()

    assert spawned == []
    stored = schedule.list_entries()[0]
    assert stored["state"] == schedule.ERROR
    assert "interrupted" in stored["error"]


def test_a_spawn_failure_lands_on_the_entry(target, monkeypatch):
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda *a, **k: {"error": "claude CLI not found"})
    schedule.create(str(target), "hi", _in(-5))

    schedule.tick()

    stored = schedule.list_entries()[0]
    assert stored["state"] == schedule.ERROR
    assert "claude CLI not found" in stored["error"]


def test_one_bad_entry_does_not_stop_the_rest_of_the_tick(target, monkeypatch):
    """A tick is a batch; a target that blows up in the helper must not cost the
    other messages their send."""
    calls = []

    def flaky(target_, prompt, mode, session_id=""):
        calls.append(prompt)
        if prompt == "boom":
            raise RuntimeError("helper exploded")
        return {"run_id": "r-ok"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", flaky)
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: None)
    monkeypatch.setattr(claude_spawn, "record_session_when_ready", lambda a, r: None)

    schedule.create(str(target), "boom", _in(-20))
    schedule.create(str(target), "fine", _in(-10))

    schedule.tick()

    assert calls == ["boom", "fine"]
    states = {e["message"]: e["state"] for e in schedule.list_entries()}
    assert states == {"boom": schedule.ERROR, "fine": schedule.SENT}


def test_two_messages_resuming_one_session_do_not_overlap(target, spawned):
    """A spawn returns when the process is away, not when the turn ends, so two
    sends that resume the SAME session would otherwise run concurrent
    `claude --resume` processes over one transcript. The second waits."""
    schedule.create(str(target), "first", _in(-20), session_id="sess-a")
    schedule.create(str(target), "second", _in(-10), session_id="sess-a")

    schedule.tick()

    assert [c["message"] for c in spawned] == ["first"]
    states = {e["message"]: e["state"] for e in schedule.list_entries()}
    assert states == {"first": schedule.SENT, "second": schedule.PENDING}

    # still busy: the first turn has no verdict yet
    schedule.tick()
    assert [c["message"] for c in spawned] == ["first"]

    # once it finishes, the follower goes
    first = next(e for e in schedule.list_entries() if e["message"] == "first")
    schedule._update(first["id"], turn="ok")
    schedule.tick()
    assert [c["message"] for c in spawned] == ["first", "second"]


def test_a_catch_up_batch_fires_in_DUE_order_not_creation_order(target, spawned):
    """The store is in creation order and the two disagree the moment a catch-up
    pass finds several overdue at once: something scheduled this morning for
    tonight would go before something scheduled at lunch for 2pm.

    It bites hardest on same-session sends, where the per-session hold turns
    "which goes first" into "which conversation TURN happens first" — so this
    creates them in the wrong order deliberately."""
    schedule.create(str(target), "later", _in(-60))    # created first, due last
    schedule.create(str(target), "sooner", _in(-600))  # created second, due first

    schedule.tick()

    assert [c["message"] for c in spawned] == ["sooner", "later"]


def test_a_held_same_session_follow_up_still_goes_in_due_order(target, spawned):
    schedule.create(str(target), "second turn", _in(-60), session_id="s")
    schedule.create(str(target), "first turn", _in(-600), session_id="s")

    schedule.tick()
    assert [c["message"] for c in spawned] == ["first turn"]

    first = next(e for e in schedule.list_entries() if e["message"] == "first turn")
    schedule._update(first["id"], turn="ok")
    schedule.tick()

    assert [c["message"] for c in spawned] == ["first turn", "second turn"]


def test_different_sessions_and_fresh_sends_never_block_each_other(target, spawned):
    """The hold is per-session, and a fresh-session entry ("" session_id) collides
    with nothing — otherwise one slow conversation would stall every message."""
    schedule.create(str(target), "in a", _in(-30), session_id="sess-a")
    schedule.create(str(target), "in b", _in(-20), session_id="sess-b")
    schedule.create(str(target), "fresh one", _in(-15))
    schedule.create(str(target), "fresh two", _in(-10))

    schedule.tick()

    assert sorted(c["message"] for c in spawned) == [
        "fresh one", "fresh two", "in a", "in b"]


def test_session_id_rides_through_to_the_helper(target, spawned):
    """A scheduled message can continue an existing conversation rather than
    always opening a new one."""
    schedule.create(str(target), "and another thing", _in(-5),
                    session_id="sess-abc")

    schedule.tick()

    assert spawned[0]["session_id"] == "sess-abc"


# ----------------------------------------------------------------- cancelling

def test_cancel_stops_a_pending_message(target, spawned):
    entry = schedule.create(str(target), "never mind", _in(-5))

    assert schedule.cancel(entry["id"])["state"] == schedule.CANCELLED
    schedule.tick()

    assert spawned == []
    assert schedule.list_entries()[0]["state"] == schedule.CANCELLED


def test_cancel_is_none_for_anything_not_pending(target, spawned):
    entry = schedule.create(str(target), "gone", _in(-5))
    schedule.tick()

    # already sent: there is no promise left to withdraw
    assert schedule.cancel(entry["id"]) is None
    assert schedule.cancel("no-such-id") is None


# --------------------------------------------------------------------- store

def test_a_corrupt_store_reads_as_nothing_scheduled(home):
    os.makedirs(home, exist_ok=True)
    with open(schedule.store_path(), "w", encoding="utf-8") as f:
        f.write("{not json")

    assert schedule.list_entries() == []


def test_listing_puts_live_entries_first_then_soonest_due(target, spawned):
    schedule.create(str(target), "sent one", _in(-5))
    schedule.tick()
    late = schedule.create(str(target), "later", _in(7200))
    soon = schedule.create(str(target), "sooner", _in(60))

    listed = schedule.list_entries()

    assert [e["message"] for e in listed[:2]] == [soon["message"], late["message"]]
    assert listed[2]["state"] == schedule.SENT


def test_the_wake_stub_is_never_synced_while_the_store_lock_is_held(target, spawned,
                                                                    monkeypatch):
    """On macOS `sync` shells out to launchctl twice. Holding the store lock
    across those subprocesses would let one tick stall a GET /api/schedule for as
    long as launchd takes to answer, so every caller snapshots under the lock and
    syncs after releasing it.

    Checked from ANOTHER thread on purpose: `_lock` is an RLock, so re-acquiring
    it on the calling thread would succeed whether or not it is held."""
    held = []

    def sync_and_probe(due):
        def probe():
            got = schedule._lock.acquire(timeout=0.5)
            held.append(not got)  # True == the lock was still held
            if got:
                schedule._lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()

    monkeypatch.setattr(schedule_wake, "sync", sync_and_probe)

    entry = schedule.create(str(target), "hi", _in(-5))   # create -> sync
    schedule.tick()                                        # claim + send -> sync
    schedule.create(str(target), "another", _in(600))
    schedule.cancel(schedule.list_entries()[0]["id"])      # cancel -> sync

    assert held, "the wake stub was never synced, so this proves nothing"
    assert not any(held), "sync ran while the store lock was held"
    assert entry["state"] == schedule.PENDING


def test_max_late_seconds_falls_back_on_nonsense(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "not-a-number")
    assert schedule.max_late_seconds() == 24 * 3600
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "0")
    assert schedule.max_late_seconds() == 24 * 3600
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "90")
    assert schedule.max_late_seconds() == 90

"""Scheduled Claude messages — the model (fused_render/schedule.py).

The store, the firing decision, and the two rules that make "the app owns the
send" survivable: wall-clock comparison (so a due time that passed while the app
was closed still fires) and a bound on how late that is still worth doing.

Nothing here spawns a real claude: the send is stubbed at claude_spawn's
`spawn_helper` seam, and the wake stub is stubbed everywhere so no test writes a
LaunchAgent on a developer's own macOS machine.
"""
import inspect
import os
import threading
import time
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


@pytest.fixture(autouse=True)
def fresh_process():
    """`schedule._watched` is process-global and says "this process is watching
    that turn", so a test inheriting another's entries would look like a process
    that never died. Cleared on the way in AND out: a test that leaves an id behind
    silently disables the sweep's abandoned-turn branch for everything after it."""
    schedule._watched.clear()
    yield schedule._watched
    schedule._watched.clear()


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
    # Stub the THREAD BODY, not the function inside it. Stubbing
    # `record_session_when_ready` used to look equivalent and was not, in two
    # ways that both bit: the stub's signature drifted from the real function's
    # (a TypeError raised inside a daemon thread is a warning, never a failure),
    # and a stub that RETURNS makes `_watch_turn` conclude the poll loop ended,
    # so it closes the turn as `unknown` — which quietly un-busies the session
    # these tests use to check that two sends on one session serialize. Nothing
    # in this file asserts on watcher output; every test drives `turn` by hand.
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
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


def test_create_target_makes_one_folder_and_only_one(target):
    """`create_target` is opt-in, and one level deep. Off, a missing target is
    the same refusal it always was — which is what the re-send path relies on:
    a target that has since been deleted is a fact its user needs told, not one
    to paper over with an empty directory (and re-making a deleted FILE's name
    as a folder would be the worst answer available)."""
    schedule.create(str(target / "ABC1"), "hi", _in(600), create_target=True)
    assert (target / "ABC1").is_dir()

    # Two levels is a tree, not a folder — and nothing is left behind.
    with pytest.raises(ValueError, match="only one new folder"):
        schedule.create(str(target / "new1" / "new2"), "hi", _in(600),
                        create_target=True)
    assert not (target / "new1").exists()

    # Off by default, so every other caller is unchanged.
    with pytest.raises(ValueError, match="no such file or directory"):
        schedule.create(str(target / "ABC2"), "hi", _in(600))
    assert not (target / "ABC2").exists()


def test_create_target_leaves_an_existing_target_alone(target):
    """The flag is permission to create, not an instruction to. Something that is
    already there is used as-is — including a FILE target, which stays legal
    (a task can run against one; the agent works in its parent)."""
    entry = schedule.create(str(target / "index.html"), "hi", _in(600),
                            create_target=True)
    assert entry["target"] == str(target / "index.html")
    assert (target / "index.html").is_file()


def test_a_due_time_days_in_the_past_is_accepted_and_recorded_as_given(target):
    """This used to be REFUSED, and the refusal was right while catch-up was
    bounded: an entry the next tick would sweep to `missed` is better refused
    than accepted and silently dropped.

    Unbounded, there is nothing to refuse it for. Picking a date days back on
    the calendar means "run this, and file it under then", so the due time is
    stored as given rather than rewritten to now — the history has to read
    truthfully — and the run happens on the next tick."""
    past = _in(-3 * 86400).replace(microsecond=0)
    entry = schedule.create(str(target), "backdated", past)

    assert entry["state"] == schedule.PENDING
    assert schedule.parse_due(entry["due"]) == past


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


# ------------------------------------------- the form's own three fields
#
# `title`, `description` and `new_task_each_run` come off the create form, and
# `/api/schedule` takes a raw dict — so a field the model does not name is
# silently DROPPED, which is what happened to all three. These pin them: they
# are stored, they round-trip under the exact names the form reads back, and a
# store a human has edited cannot raise on the way through.


def test_the_forms_three_fields_are_stored_and_read_back_by_name(target):
    entry = schedule.create(str(target), "pull the news", _in(600),
                            title="Morning digest",
                            description="Reads the feeds and summarises them",
                            new_task_each_run=True)

    assert entry["title"] == "Morning digest"
    assert entry["description"] == "Reads the feeds and summarises them"
    assert entry["new_task_each_run"] is True
    # ...and out of the store under the same names, which is what the form
    # reads when it opens on an existing task.
    stored = schedule.list_entries()[0]
    assert (stored["title"], stored["description"],
            stored["new_task_each_run"]) == (
        "Morning digest", "Reads the feeds and summarises them", True)


def test_omitted_and_blank_fields_default_the_same_way(target):
    """The form omits a field rather than sending a blank one, so "absent",
    "null" and "" must all mean the same thing.

    An empty title is NOT a hole for this module to fill: it is the first branch
    of a precedence the tasks endpoint owns (the user's title, else Claude
    Code's `ai-title`, else the message's first line), and inventing one here
    would pin the row to whatever the message happened to open with."""
    omitted = schedule.create(str(target), "hi", _in(600))
    blank = schedule.create(str(target), "hi", _in(600), title="   ",
                            description="", new_task_each_run=None)

    for entry in (omitted, blank):
        assert entry["title"] == ""
        assert entry["description"] == ""
        assert entry["new_task_each_run"] is False


@pytest.mark.parametrize("value", [42, ["a", "list"], {"an": "object"}, None])
def test_a_garbage_title_degrades_instead_of_raising(target, value):
    """The store is a JSON file a human may edit and the router passes the
    request body through unvalidated, so a title that is not a string must cost
    the entry nothing — never a 500 on the way in or an unreadable listing."""
    entry = schedule.create(str(target), "hi", _in(600), title=value,
                            description=value)
    assert entry["title"] == "" and entry["description"] == ""


@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), (None, False),
    # The truthy strings are the point: a hand-edited store carrying "false"
    # read through bool() would flip a schedule's threading model with nobody
    # asking, so only a real `true` counts.
    ("true", False), ("false", False), ("no", False), (1, False), (0, False),
])
def test_only_a_real_true_ticks_new_task_each_run(target, value, expected):
    entry = schedule.create(str(target), "hi", _in(600),
                            new_task_each_run=value)
    assert entry["new_task_each_run"] is expected


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
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)

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
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)

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
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)

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


def test_handled_entries_read_newest_first(target, spawned):
    """The two groups run in OPPOSITE directions, because "most relevant first"
    means opposite things about the future and the past. Ascending here was a
    straight bug (reported from use): it buried what just ran under every message
    ever scheduled, and got worse the longer the feature was used."""
    for i, ago in enumerate([-300, -200, -100]):
        schedule.create(str(target), f"ran {i}", _in(ago))
        schedule.tick()   # one at a time, so each gets its own `fired` stamp

    listed = schedule.list_entries()

    assert [e["message"] for e in listed] == ["ran 2", "ran 1", "ran 0"]


def test_the_two_groups_are_ordered_independently(target, spawned):
    """Live ascending (the next thing to happen, at the top) and handled
    descending (the latest news, at the top) in one listing."""
    schedule.create(str(target), "ran first", _in(-300))
    schedule.tick()
    schedule.create(str(target), "ran second", _in(-100))
    schedule.tick()
    schedule.create(str(target), "due later", _in(7200))
    schedule.create(str(target), "due sooner", _in(60))

    assert [e["message"] for e in schedule.list_entries()] == [
        "due sooner", "due later",      # ascending: soonest first
        "ran second", "ran first",      # descending: most recent first
    ]


def test_a_handled_entry_that_never_ran_still_sorts(target, spawned, monkeypatch):
    """`missed` and `cancelled` carry no `fired` stamp, so the fallback to `due` is
    what keeps them in the order at all rather than bunched at one end."""
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "60")
    # Created INSIDE the bound (create refuses anything already past it), then the
    # clock walks beyond it with no tick in between — the app was closed.
    schedule.create(str(target), "missed older", _in(-40))
    schedule.create(str(target), "missed newer", _in(-20))
    schedule.tick(now=datetime.now(timezone.utc) + timedelta(seconds=120))

    cancelled = schedule.create(str(target), "cancelled", _in(3600))
    schedule.cancel(cancelled["id"])

    listed = [e["message"] for e in schedule.list_entries()]
    # the cancelled one is due furthest ahead, so it leads; then the missed pair,
    # newest first
    assert listed == ["cancelled", "missed newer", "missed older"]


def test_the_wake_stub_is_never_synced_while_the_store_lock_is_held(target, spawned,
                                                                    monkeypatch):
    """On macOS `sync` shells out to launchctl twice. Holding the store lock
    across those subprocesses would let one tick stall a GET /api/schedule for as
    long as launchd takes to answer, so `_sync_wake` reads what it needs, releases
    the lock, and only then shells out.

    This is also the lock-ORDER guard. `_sync_wake` takes `_wake_lock` and then
    `_lock`; a caller that still held `_lock` when it got here would be taking them
    in the opposite order, and two such callers deadlock.

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


def test_max_late_seconds_is_unbounded_unless_an_operator_says_otherwise(monkeypatch):
    """The default is None — no bound — which is what makes missed work queue
    rather than expire. It was 24h; the env var is the escape hatch for an
    install that wants the old shape, and a value that is not a usable bound
    falls back to the default exactly as it always did."""
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE", raising=False)
    assert schedule.max_late_seconds() is None
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "not-a-number")
    assert schedule.max_late_seconds() is None
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "0")
    assert schedule.max_late_seconds() is None
    monkeypatch.setenv("FUSED_RENDER_SCHEDULE_MAX_LATE", "90")
    assert schedule.max_late_seconds() == 90


# ---------------------------------------------------------------------------
# A turn whose watcher died with the process
#
# `sent` with an empty `turn` means two completely different things depending on
# whether anything is still watching: a turn running normally, or one abandoned
# when the app was killed mid-turn. `_close_unwatched` is the floor under a watch
# that ENDS, and cannot cover a process that dies — the thread goes with it. So the
# sweep has to notice, and `schedule._watched` is how it tells the two apart.


def _abandon():
    """What a restart looks like from the store's side: the entries survive, the
    knowledge of which turns were being watched does not."""
    schedule._watched.clear()


def test_a_turn_abandoned_by_a_dead_process_is_closed_by_the_next_sweep(
        target, spawned):
    schedule.create(str(target), "hi", _in(-5))
    schedule.tick()
    sent = schedule.list_entries()[0]
    assert (sent["state"], sent["turn"]) == (schedule.SENT, "")

    _abandon()
    schedule.tick()

    stored = schedule.list_entries()[0]
    # The message DID go out, so `state` stays sent and only the turn is unknown —
    # the same verdict `_close_unwatched` reaches for a watch that ended silently.
    assert stored["state"] == schedule.SENT
    assert stored["turn"] == "unknown"
    assert "interrupted" in stored["error"]


def test_a_turn_this_process_is_watching_is_left_alone(target, spawned):
    """The other half of the same branch, and the one that makes it safe: while a
    watcher is registered, a sweep landing mid-turn must not touch the entry."""
    schedule.create(str(target), "hi", _in(-5))
    schedule.tick()

    schedule.tick()          # a second sweep, watcher still registered
    schedule.tick()

    stored = schedule.list_entries()[0]
    assert stored["turn"] == "", "a live turn was closed as abandoned"
    assert not stored["error"]


def test_closing_an_abandoned_turn_releases_its_session(target, spawned):
    """The consequence that costs the user a LATER message, not just a wrong label.

    A `sent` entry with no verdict keeps its session in `_busy_sessions`, so the
    next scheduled message to that conversation is held back tick after tick — for
    an abandoned turn, until the catch-up bound gives up and calls it missed. The
    hold is right while the turn is live and wrong once nothing is watching it."""
    schedule.create(str(target), "first", _in(-20), session_id="sess-a")
    schedule.create(str(target), "second", _in(-10), session_id="sess-a")

    schedule.tick()
    assert [c["message"] for c in spawned] == ["first"]

    schedule.tick()          # still watching: the follower correctly waits
    assert [c["message"] for c in spawned] == ["first"]

    _abandon()
    schedule.tick()

    assert [c["message"] for c in spawned] == ["first", "second"], (
        "the follower is still held by a session whose turn nobody is watching")


def test_an_abandoned_turn_is_announced_once_not_every_tick(target, spawned):
    """It writes a verdict, so the branch stops matching — but if it ever did not,
    the user would get one toast per tick for the rest of the session."""
    schedule.create(str(target), "hi", _in(-5))
    schedule.tick()
    _abandon()
    # The event log is process-global and this file does not clear it, so ack what
    # earlier tests left behind: otherwise the count below is of the whole worker.
    outstanding = schedule.undelivered_events()
    if outstanding:
        schedule.ack_events(outstanding[-1]["id"])

    schedule.tick()
    schedule.tick()
    schedule.tick()

    failures = [e for e in schedule.undelivered_events()
                if e["kind"] == schedule.EVENT_FAILED]
    assert len(failures) == 1, failures


def test_a_watcher_that_ends_deregisters_so_a_later_sweep_sees_the_truth(
        target, monkeypatch):
    """The mirror image: leaving an id registered after the watch ends would make a
    FINISHED turn permanently invisible to the sweep — the same stuck row, reached
    from the other side. Every exit path has to deregister, which is why the
    `finally` is there; this drives the raising one."""
    monkeypatch.setattr(claude_spawn, "spawn_helper", lambda *a, **k: {"run_id": "r-1"})

    def boom():
        raise RuntimeError("no agent backend")

    monkeypatch.setattr(claude_spawn, "load_agent", boom)

    schedule.create(str(target), "hi", _in(-5))
    schedule.tick()

    entry_id = schedule.list_entries()[0]["id"]
    # The watcher thread is a daemon; give it a moment to reach its `finally`.
    for _ in range(50):
        if not schedule._is_watched(entry_id):
            break
        time.sleep(0.02)
    assert not schedule._is_watched(entry_id), (
        "the watch ended but the entry is still marked as watched")


# ---------------------------------------------------------------------------
# The wake stub is told the store, not a caller's memory of it


def test_sync_wake_cannot_be_handed_a_stale_view():
    """Structural, because it is what makes the bug unrepresentable rather than
    merely absent: callers used to snapshot the pending times inside their own lock
    and pass them here, and two mutations racing could reach launchctl in the
    opposite order — the older snapshot then overwrote the plist and dropped the
    newer message's time, with nothing to resync until some later store write
    happened along. There is no argument to be stale now."""
    assert not inspect.signature(schedule._sync_wake).parameters


def test_the_wake_stub_is_given_the_pending_times_as_they_are_at_sync_time(
        target, no_real_wake):
    schedule.create(str(target), "first", _in(600))
    schedule.create(str(target), "second", _in(1200))

    assert no_real_wake, "nothing synced, so this proves nothing"
    due = {e["due"] for e in schedule.list_entries()
           if e["state"] == schedule.PENDING}
    assert set(no_real_wake[-1]) == due


def test_two_threads_never_shell_out_to_the_wake_stub_at_once(target, monkeypatch):
    """The serialisation half of the same fix. Overlapping launchctl pairs are how
    an older view of the store won the plist; one lock around the shell-out is what
    makes "last to write the plist is last to read the store" true."""
    live, peak = [], []

    def sync(due):
        live.append(1)
        peak.append(len(live))
        time.sleep(0.01)      # long enough for a racing thread to arrive
        live.pop()

    monkeypatch.setattr(schedule_wake, "sync", sync)

    threads = [threading.Thread(target=schedule.create,
                                args=(str(target), f"m{i}", _in(600 + i)))
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak, "nothing synced, so this proves nothing"
    assert max(peak) == 1, f"{max(peak)} syncs were inside launchctl at once"

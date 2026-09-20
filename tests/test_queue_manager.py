"""queue_manager: every event against every state, and the index that survives.

The manager owns no process and reads no registry — spawn, deliver and the two
status reads are injected — so the whole dispatcher runs here on plain Python
fakes and a tmp STATE_DIR (patched on `tasks_store`, the way
`tests/test_project_queue.py` isolates the same directory). What is under test is
the order the folder is handed out in and the index that is written; nothing
about Claude.

Two worlds, because `pump` behaves differently against each and both are real:
`World` spawns (the head of the line takes the folder), `idle_world` answers None
(the item names no work any more and is dropped).

2026-09-17.
"""
import json
import os
import threading
import time

import pytest

from fused_render import queue_manager as qm
from fused_render import tasks_store

F1 = "/tmp/proj-one"
F2 = "/tmp/proj-two"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """A state dir of our own. `tasks_store.STATE_DIR` is read on every call
    rather than captured at import, so patching the attribute is enough."""
    folder = tmp_path / "state"
    folder.mkdir()
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(folder))
    qm.reset_for_tests(None)
    qm.set_factory(None)
    yield folder
    qm.reset_for_tests(None)
    qm.set_factory(None)


class World:
    """The injected side of the manager: a spawn log, a delivery log, the two
    status answers, the due list and the notified keys."""

    def __init__(self, due=(), spawns=None):
        self.due = list(due)
        self.spawned: list[tuple[str, str]] = []
        self.delivered: list[dict] = []
        self.notified: list[set] = []
        self.running_keys: set[str] = set()
        self.blocked_keys: set[str] = set()
        self.spawns = dict(spawns or {})     # task_key -> what spawn answers
        self.default_spawn = {"run_id": "run", "session_id": "sess"}
        self.now = 1000.0

    def spawn(self, folder, task_key):
        self.spawned.append((folder, task_key))
        value = self.spawns.get(task_key, self.default_spawn)
        return dict(value) if isinstance(value, dict) else value

    def deliver(self, answer):
        self.delivered.append(answer)

    @staticmethod
    def names(task):
        """Every name one status question can arrive under. The manager asks
        with the whole RECORD — `{task, run_id, session_id}` — because half the
        index knows a conversation by a name the registry never heard: a
        `pending:` owner by the entry that made it, a new chat by its run."""
        if isinstance(task, dict):
            return {str(task.get(field) or "")
                    for field in ("task", "run_id", "session_id")} - {""}
        return {str(task or "")} - {""}

    def running(self, task):
        return bool(self.names(task) & self.running_keys)

    def blocked(self, task):
        return bool(self.names(task) & self.blocked_keys)

    def pending_due(self):
        return list(self.due)

    def notify(self, keys):
        self.notified.append(set(keys))

    def clock(self):
        self.now += 1.0
        return self.now

    def manager(self):
        """A manager over this world, reconciled — which is what the scheduler's
        first tick does to a freshly built one. Construction itself is
        deliberately inert (it can spawn, so it must not happen inside a read);
        every case here wants the loaded-and-settled state, so the helper says
        it once."""
        m = qm.QueueManager(spawn=self.spawn, deliver=self.deliver,
                            running=self.running, blocked=self.blocked,
                            pending_due=self.pending_due, notify=self.notify,
                            clock=self.clock)
        m.reconcile()
        return m


def idle_world(**kw):
    """A world that can start nothing. Ownership is declared explicitly with
    `started()` — which is what the chat's admit path does — and anything the
    pump reaches is dropped."""
    world = World(**kw)
    world.default_spawn = None
    return world


def loaded(world):
    """A manager as `_load` + the migration leave it, with NO `reconcile()`.

    What construction alone did is exactly what the migration tests are about:
    reconcile pumps, and a pump hands the head of the line its stored answer
    straight away — a true and wanted outcome, but one that erases the state
    under test."""
    return qm.QueueManager(spawn=world.spawn, deliver=world.deliver,
                           running=world.running, blocked=world.blocked,
                           pending_due=world.pending_due, notify=world.notify,
                           clock=world.clock)


def line_of(manager, folder=F1):
    return [i["task"] for i in manager.snapshot()["folders"][folder]["line"]]


def blocked_of(manager, folder=F1):
    return [i["task"] for i in manager.snapshot()["folders"][folder]["blocked"]]


def owner_key(manager, folder=F1):
    owner = manager.owner(folder)
    return owner["task"] if owner else None


# ------------------------------------------------------------------- enqueue


def test_enqueue_into_a_free_folder_spawns_and_owns():
    world = World()
    m = world.manager()
    place = m.enqueue(F1, "a", "e1")
    assert world.spawned == [(F1, "a")]
    assert owner_key(m) == "a"
    assert line_of(m) == []
    assert place == {"key": "", "position": 0, "ahead_key": ""}
    owner = m.owner(F1)
    assert (owner["run_id"], owner["session_id"]) == ("run", "sess")
    assert owner["since"] > 0
    assert owner["entry_id"] == "e1"


def test_enqueue_behind_an_owner_queues_in_order():
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    assert m.enqueue(F1, "b")["position"] == 1
    assert m.enqueue(F1, "c")["position"] == 2
    assert line_of(m) == ["b", "c"]
    assert world.spawned == [(F1, "a")]


def test_enqueue_is_idempotent_everywhere():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    m.card_raised("a")                       # a -> blocked, b takes the folder
    assert (owner_key(m), line_of(m), blocked_of(m)) == ("b", ["c"], ["a"])

    assert m.enqueue(F1, "b") == {"key": "", "position": 0, "ahead_key": ""}    # the owner
    assert m.enqueue(F1, "c") == {"key": F1, "position": 1, "ahead_key": "b"}   # in line
    assert m.enqueue(F1, "a") == {"key": "", "position": 0, "ahead_key": ""}    # blocked
    assert line_of(m) == ["c"]
    assert blocked_of(m) == ["a"]


def test_enqueue_ignores_a_blank_folder_or_key():
    m = World().manager()
    assert m.enqueue("", "a") == {"key": "", "position": 0, "ahead_key": ""}
    assert m.enqueue(F1, "") == {"key": "", "position": 0, "ahead_key": ""}
    assert m.snapshot()["folders"] == {}


def test_two_folders_are_independent():
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F2, "b")
    assert owner_key(m, F1) == "a"
    assert owner_key(m, F2) == "b"
    assert world.spawned == [(F1, "a"), (F2, "b")]


# ---------------------------------------------------------------------- skip


def test_skip_moves_to_the_head_newest_press_wins():
    m = World().manager()
    for key in ("owner", "a", "b", "c"):
        m.enqueue(F1, key)
    assert line_of(m) == ["a", "b", "c"]
    m.skip("b")
    assert line_of(m) == ["b", "a", "c"]
    m.skip("c")
    assert line_of(m) == ["c", "b", "a"]


def test_skip_pulls_a_blocked_task_back_into_the_line():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a")
    assert (owner_key(m), blocked_of(m)) == ("b", ["a"])
    assert m.skip("a") == {"key": F1, "position": 1, "ahead_key": "b",
                           "started": False}
    assert blocked_of(m) == []
    assert line_of(m) == ["a"]


def test_skip_of_a_blocked_task_on_a_free_folder_resumes_without_spawning():
    """Bugbot, PR #1194: skip of a task parked in `blocked` used to pump it as
    ordinary queued work — no held answer, `resumed` unset — which spawned a
    SECOND turn (or dropped the item) beside the run that is still alive,
    waiting on its card. The fix files it as a resume marker: the pump owns
    it without spawning, exactly like `card_cleared` does."""
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.card_raised("a", "run")                # the only task; folder frees, a parked
    assert (owner_key(m), blocked_of(m)) == (None, ["a"])
    world.spawned.clear()

    result = m.skip("a")

    assert world.spawned == [], "a second turn beside the parked run"
    assert owner_key(m) == "a"
    assert m.owner(F1)["run_id"] == "run"
    assert result["started"] is True
    assert m.snapshot()["folders"][F1]["owner"]["starting"] is False


def test_card_answered_on_a_skip_resumed_owner_delivers_directly():
    """The owner IS the blocked run once skip resumes it — `card_answered`
    matching the owner returns not-held so the decide endpoint delivers the
    verdict straight into the live turn, same as any other owner."""
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.card_raised("a", "run")
    m.skip("a")
    assert owner_key(m) == "a"
    assert m.card_answered("a", "run", "req", {"x": 1}) == {"held": False,
                                                             "position": 0}
    assert world.delivered == []             # the caller delivers it, not this call


def test_card_cleared_on_a_skip_resumed_owner_is_a_no_op():
    """The task is no longer in `blocked` once skip resumes it as owner, so
    `card_cleared` — somebody answering the same card elsewhere — is a no-op
    that keeps ownership exactly where it is."""
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.card_raised("a", "run")
    m.skip("a")
    before = m.snapshot()
    m.card_cleared("a", "run")
    assert m.snapshot() == before
    assert owner_key(m) == "a"


def test_reconcile_keeps_a_skip_resumed_owner_that_is_still_blocked():
    """A skip-resumed owner is still a parked card, not a live turn — only
    `blocked(record)` says it is worth keeping."""
    world = idle_world()
    m = world.manager()
    m.started(F1, "a", "run-a", "sess-a")
    m.card_raised("a", "run-a")
    m.skip("a")
    assert owner_key(m) == "a"
    world.blocked_keys.add("run-a")
    m.reconcile()
    assert owner_key(m) == "a"
    world.blocked_keys.discard("run-a")
    m.reconcile()
    assert owner_key(m) is None


def test_skip_of_the_owner_and_of_a_stranger_are_no_ops():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    assert m.skip("a") == {"key": "", "position": 0, "ahead_key": "",
                           "started": False}
    assert m.skip("nobody") == {"key": "", "position": 0, "ahead_key": "",
                                "started": False}
    assert owner_key(m) == "a"
    assert line_of(m) == ["b"]


def test_skip_into_a_free_folder_spawns_immediately():
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    world.spawned.clear()
    m.skip("c")
    m.turn_ended("a")
    assert owner_key(m) == "c"
    assert world.spawned == [(F1, "c")]


# -------------------------------------------------------------------- remove


def test_remove_drops_from_line_blocked_and_answers():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    m.card_raised("a")                       # b owns, c waits, a blocked
    m.card_answered("a", "run-a", "req-1", {"x": 1})
    assert m.held_answer("a") is not None
    assert line_of(m) == ["a", "c"]

    m.remove("a")
    assert m.held_answer("a") is None
    assert line_of(m) == ["c"]
    assert blocked_of(m) == []
    assert owner_key(m) == "b"


def test_remove_clears_a_resume_marker_no_entry_cancel_can_reach():
    """`forget_entry` names one MESSAGE and matches by entry id; the items the
    queue mints for itself carry none (`card_answered`, `card_cleared`), so
    only the keyed verb takes those out of the line. This is the half of a
    delete that cancelling the task's scheduled entries can never do."""
    m = World().manager()
    m.enqueue(F1, "a", "e1")
    m.enqueue(F1, "b", "e2")
    m.card_raised("a")                       # a parked, b takes the folder
    m.card_cleared("a")                      # answered elsewhere: a marker
    assert line_of(m) == ["a"]
    assert [i["entry_id"] for i in m.snapshot()["folders"][F1]["line"]] == [""]

    m.forget_entry("e1")                     # the marker names no message
    assert line_of(m) == ["a"]
    m.remove("a")
    assert line_of(m) == []


def test_remove_of_the_owner_ends_the_turn_and_pumps():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.remove("a")
    assert owner_key(m) == "b"
    assert line_of(m) == []


def test_remove_of_a_stranger_changes_nothing():
    m = World().manager()
    m.enqueue(F1, "a")
    before = m.snapshot()
    m.remove("nobody")
    assert m.snapshot() == before


def test_remove_of_a_stuck_line_head_pumps_the_next_task(monkeypatch):
    """Bugbot, PR #1194: after `SpawnBusy` the owner is already None with the
    item that failed sitting at line[0]. `remove` of exactly that task used to
    only pump when the removed task was the OWNER, leaving the rest of the
    line — "owner None, line [X, Y]" — waiting for ever."""
    class Busy(Exception):
        pass

    monkeypatch.setattr(qm, "_BUSY", (Busy,))
    monkeypatch.setattr(qm, "_BUSY_TRIED", True)
    world = World()
    attempts = []

    def spawn(folder, key):
        attempts.append(key)
        if key == "x":
            raise Busy()
        return {"run_id": "run-" + key, "session_id": "sess-" + key}

    world.spawn = spawn
    m = world.manager()
    m.started(F1, "owner")
    m.enqueue(F1, "x")
    m.enqueue(F1, "y")
    m.turn_ended("owner")                    # owner frees; x is busy, resigns
    assert owner_key(m) is None
    assert line_of(m) == ["x", "y"]

    attempts.clear()
    m.remove("x")
    assert line_of(m) == []
    assert owner_key(m) == "y"
    assert attempts == ["y"]


# ---------------------------------------------------------------- holds_live


def test_holds_live_for_the_owner_and_not_for_the_line():
    """The two answers the delete door needs told apart: the folder's owner is
    a process in flight, and everybody behind it is a message that has not
    started."""
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    world.running_keys.add("a")
    assert m.holds_live("a") is True
    assert m.holds_live("b") is False
    assert m.holds_live("") is False
    assert m.holds_live("nobody") is False


def test_holds_live_for_a_parked_run_and_for_its_held_answer():
    """Parked is live — a turn waiting on a human — and stays live once the
    user answers: the decision is held, the task moves into the line, and the
    run it belongs to is still sitting there waiting for the folder."""
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a")                       # b owns, a parked
    world.blocked_keys.add("a")
    assert m.holds_live("a") is True

    m.card_answered("a", "run-a", "req-1", {"answer": "allow"})
    assert blocked_of(m) == [] and line_of(m) == ["a"]
    world.blocked_keys.clear()
    world.running_keys.add("a")
    assert m.holds_live("a") is True


def test_holds_live_is_false_once_the_run_behind_the_answer_is_gone():
    """A held decision for a process that has died holds nothing: the status
    sync is what decides, so the row can be deleted rather than being refused
    until the next `reconcile` retires the answer."""
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a")
    m.card_answered("a", "run-a", "req-1", {})
    assert m.held_answer("a") is not None
    assert m.holds_live("a") is False


# ------------------------------------------------------------------- started


def test_started_overwrites_the_owner_and_clears_the_line_entry():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.started(F1, "b", "run-b", "sess-b")
    owner = m.owner(F1)
    assert (owner["task"], owner["run_id"], owner["session_id"]) == ("b", "run-b", "sess-b")
    assert line_of(m) == []


def test_started_is_idempotent():
    m = idle_world().manager()
    m.started(F1, "a", "run-a")
    m.started(F1, "a", "run-a")
    assert owner_key(m) == "a"
    assert line_of(m) == []


def test_started_ignores_a_blank_folder_or_key():
    m = World().manager()
    m.started("", "a")
    m.started(F1, "")
    assert m.snapshot()["folders"] == {}


def test_started_with_no_task_key_files_the_owner_under_its_run():
    """A brand-new chat's first send has no session — Claude Code mints one
    inside the spawn — so the run id is the only name it has. Filing nobody left
    the folder reading free and let a second nameless send straight in (T3's
    handoff, 2026-09-17)."""
    m = idle_world().manager()
    m.started(F1, "", run_id="run-1")
    assert owner_key(m) == "run-1"
    assert m.owner(F1)["run_id"] == "run-1"
    assert m.is_free(F1) is False
    assert m.is_free(F1, "run-1") is True
    assert m.is_free(F1, "somebody-else") is False


def test_is_free_answers_to_the_owners_task_run_or_session():
    """Three names for one conversation and any of them is enough — otherwise
    the chat that owns the folder is told it is standing behind itself."""
    m = idle_world().manager()
    m.started(F1, "task-a", run_id="run-1", session_id="sess-1")
    assert m.is_free(F1, "task-a") is True
    assert m.is_free(F1, "run-1") is True
    assert m.is_free(F1, "sess-1") is True
    assert m.is_free(F1, "") is False
    assert m.is_free(F1, "stranger") is False
    assert m.is_free(F2, "") is True


# --------------------------------------------------------------- card_raised


def test_card_raised_parks_the_owner_and_hands_the_folder_on():
    world = World(spawns={"a": {"run_id": "run-a", "session_id": "sess-a"}})
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    world.spawned.clear()
    m.card_raised("a", "run-a")
    assert blocked_of(m) == ["a"]
    assert owner_key(m) == "b"
    assert world.spawned == [(F1, "b")]


def test_card_raised_for_a_non_owner_is_a_no_op():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    before = m.snapshot()
    m.card_raised("b")
    m.card_raised("nobody")
    assert m.snapshot() == before


def test_card_raised_twice_parks_once():
    m = World().manager()
    m.enqueue(F1, "a")
    m.card_raised("a")
    m.card_raised("a")
    assert blocked_of(m) == ["a"]
    assert owner_key(m) is None


# -------------------------------------------------------------- card_answered


def test_card_answered_for_the_owner_is_not_held():
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    assert m.card_answered("a", "run-a", "req", {"ok": True}) == {"held": False, "position": 0}
    assert world.delivered == []
    assert m.held_answer("a") is None


def test_card_answered_with_a_free_folder_takes_the_folder_back():
    """The task is PARKED in that folder — a live turn waiting on a human — and
    the decision about to be delivered un-parks it, so the folder is its again.
    Leaving it unowned let the next tick start a second turn in the very tree
    this one was about to resume, and the re-owned record names the RUN the card
    was raised against, not just the label."""
    m = World().manager()
    m.enqueue(F1, "a")
    m.card_raised("a", "run")                # nothing behind it: the folder frees
    assert (owner_key(m), blocked_of(m)) == (None, ["a"])

    assert m.card_answered("a", "run", "req", {"ok": True}) == {"held": False,
                                                                "position": 0}
    assert m.held_answer("a") is None        # not held: the caller delivers it
    assert owner_key(m) == "a"
    assert blocked_of(m) == []
    assert m.owner(F1)["run_id"] == "run"


def test_card_answered_for_an_unknown_task_is_not_held():
    m = World().manager()
    assert m.card_answered("ghost", "run", "req", {}) == {"held": False, "position": 0}


def test_card_answered_while_busy_holds_and_promotes_to_the_head():
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    m.card_raised("a")                       # b owns, c waits, a blocked
    assert m.card_answered("a", "run-a", "req-1", {"answer": "allow"}) == {"held": True, "position": 1}
    assert blocked_of(m) == []
    assert line_of(m) == ["a", "c"]
    held = m.held_answer("a")
    assert (held["run_id"], held["request_id"]) == ("run-a", "req-1")
    assert held["raw"] == {"answer": "allow"}
    assert world.delivered == []             # the pump delivers it, not this call


def test_answered_then_skipped_puts_the_answer_second():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    m.card_raised("a")
    m.card_answered("a", "run-a", "req", {})
    assert line_of(m) == ["a", "c"]
    m.skip("c")
    assert line_of(m) == ["c", "a"]
    assert m.place("a") == {"key": F1, "position": 2, "ahead_key": "c"}


def test_pump_delivers_a_stored_answer_instead_of_spawning():
    world = World(spawns={"a": {"run_id": "run-a", "session_id": "sa"},
                          "b": {"run_id": "run-b", "session_id": "sb"}})
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a", "run-a")              # b owns, a blocked
    m.card_answered("a", "run-a", "req-1", {"answer": "allow"})
    world.spawned.clear()

    m.turn_ended("b", "run-b")
    assert owner_key(m) == "a"
    assert world.spawned == []               # delivered, never re-spawned
    assert [d["request_id"] for d in world.delivered] == ["req-1"]
    assert m.held_answer("a") is None        # consumed
    assert m.owner(F1)["run_id"] == "run-a"


# ------------------------------------------------------------ turn_ended/exit


def test_turn_ended_frees_the_owner_and_pumps():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.turn_ended("a", "run")
    assert owner_key(m) == "b"


def test_turn_ended_matches_on_run_id_when_the_key_rekeyed():
    m = idle_world().manager()
    m.started(F1, "pending:e1", "run-7", "sess-7")
    m.turn_ended("some-session-id", "run-7")
    assert owner_key(m) is None


def test_turn_ended_for_a_non_owner_is_a_no_op():
    m = World().manager()
    m.started(F1, "a", "run-a")
    m.enqueue(F1, "b")
    m.turn_ended("b", "run-b")
    m.turn_ended("nobody")
    assert owner_key(m) == "a"
    assert line_of(m) == ["b"]


def test_turn_ended_repeated_for_a_past_owner_is_a_no_op():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.turn_ended("a", "run")
    assert owner_key(m) == "b"
    m.turn_ended("a")                        # the host retries; b keeps the folder
    assert owner_key(m) == "b"


def test_exited_frees_the_owner_like_turn_ended():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.exited("a", "run", 0)
    assert owner_key(m) == "b"
    m.exited("a", "", 1)
    assert owner_key(m) == "b"


def test_turn_ended_with_an_empty_line_leaves_the_folder_free():
    m = World().manager()
    m.enqueue(F1, "a")
    m.turn_ended("a")
    assert owner_key(m) is None
    assert m.is_free(F1) is True


# ---------------------------------------------------------------------- pump


def test_spawn_returning_none_drops_the_item_and_keeps_pumping():
    world = World(spawns={"a": None, "b": None})
    m = world.manager()
    m.started(F1, "owner")
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    world.spawned.clear()

    m.turn_ended("owner")
    assert world.spawned == [(F1, "a"), (F1, "b"), (F1, "c")]
    assert owner_key(m) == "c"
    assert line_of(m) == []


def test_pump_with_nothing_startable_leaves_the_folder_free():
    m = World(spawns={"a": None}).manager()
    m.enqueue(F1, "a")
    assert owner_key(m) is None
    assert line_of(m) == []


def test_a_raising_spawn_drops_the_item_rather_than_the_line():
    world = World()

    def boom(folder, key):
        world.spawned.append((folder, key))
        if key == "a":
            raise RuntimeError("no")
        return {"run_id": "r", "session_id": "s"}

    world.spawn = boom
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    assert owner_key(m) == "b"


def test_a_raising_deliver_keeps_the_answer_and_the_place():
    """The user pressed Allow, the folder freed, the replay failed — and the
    verdict used to be GONE, because the answer was popped before the delivery
    was attempted. Now the task goes back to the head of its line, promoted, the
    folder is freed and the answer stays: the next event tries again."""
    world = World()
    fail = {"now": True}

    def boom(answer):
        world.delivered.append(answer)
        if fail["now"]:
            raise RuntimeError("the run went away")

    world.deliver = boom
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a", "run")
    m.card_answered("a", "run-a", "req", {})
    m.turn_ended("b", "run")

    assert owner_key(m) is None              # the folder is not held by a ghost
    assert line_of(m) == ["a"]
    assert m.positions()["a"]["priority"] is True
    assert m.held_answer("a")["request_id"] == "req"
    assert len(world.delivered) == 1

    fail["now"] = False                      # the next event retries it
    m.pump(F1)
    assert owner_key(m) == "a"
    assert m.held_answer("a") is None
    assert len(world.delivered) == 2


def test_a_busy_spawn_keeps_its_place_and_stops_the_pump(monkeypatch):
    """`SpawnBusy` is "not yet", not "nothing here": the conversation already
    has a send in flight. Dropping it would lose the user's message, and letting
    the next item through would jump the line."""
    class Busy(Exception):
        pass

    monkeypatch.setattr(qm, "_BUSY", (Busy,))
    monkeypatch.setattr(qm, "_BUSY_TRIED", True)
    world = World()
    attempts = []

    def spawn(folder, key):
        attempts.append(key)
        if key == "a":
            raise Busy()
        return {"run_id": "r", "session_id": "s"}

    world.spawn = spawn
    m = world.manager()
    m.started(F1, "owner")
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    attempts.clear()

    m.turn_ended("owner")
    assert attempts == ["a"]                 # b was never reached
    assert owner_key(m) is None
    assert line_of(m) == ["a", "b"]

    m.pump(F1)                               # the next event tries again
    assert attempts == ["a", "a"]


def test_the_busy_class_resolves_to_the_scheduler_s():
    """The name the manager imports is the one the scheduler raises."""
    from fused_render.schedule import SpawnBusy

    qm._BUSY, qm._BUSY_TRIED = (), False
    assert qm._busy_class() == (SpawnBusy,)


def test_pump_is_a_no_op_while_a_folder_is_owned():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.pump(F1)
    assert owner_key(m) == "a"
    assert line_of(m) == ["b"]


# ----------------------------------------------------------------- positions


def test_positions_shape_and_priority():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    assert m.positions() == {
        "b": {"key": F1, "position": 1, "ahead_key": "a",
              "ahead_names": ["a", "sess", "run"], "priority": False},
        "c": {"key": F1, "position": 2, "ahead_key": "b",
              "ahead_names": ["b"], "priority": False},
    }
    m.skip("c")
    assert m.positions() == {
        "c": {"key": F1, "position": 1, "ahead_key": "a",
              "ahead_names": ["a", "sess", "run"], "priority": True},
        "b": {"key": F1, "position": 2, "ahead_key": "c",
              "ahead_names": ["c"], "priority": False},
    }


def test_positions_key_is_the_folder_not_the_task():
    """`key` is the folder a queued task is WAITING ON, which is what the field
    meant before the manager existed and what `_queue_row` → `_row` reads it as
    (a row's own key is the dict key it is filed under). Two folders, so a bug
    that echoes the task back cannot pass by coincidence."""
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F2, "c")
    m.enqueue(F2, "d")
    places = m.positions()
    assert places["b"]["key"] == F1
    assert places["d"]["key"] == F2
    assert m.place("b")["key"] == F1
    assert m.place("d")["key"] == F2


def test_promotion_stops_showing_once_it_is_no_longer_the_head():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    m.skip("c")
    m.skip("b")
    assert m.positions()["c"]["priority"] is False
    assert m.positions()["b"]["priority"] is True


def test_positions_excludes_blocked_tasks_and_owners():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a")                       # a blocked, b owns
    m.enqueue(F1, "c")
    assert set(m.positions()) == {"c"}


def test_positions_of_an_ownerless_line_names_nobody_ahead():
    """A pump empties a line whose folder has no owner, so this state is only
    reachable from an index written by something else. It must still list the
    items — a row that hides because the file disagreed is worse than a row with
    no name ahead of it."""
    m = World().manager()
    m._state["folders"][F1] = {"owner": None, "blocked": [],
                               "line": [{"task": "b", "entry_id": "", "promoted": False}]}
    assert m.positions()["b"] == {"key": F1, "position": 1, "ahead_key": "",
                                  "ahead_names": [], "priority": False}


def test_place_and_is_free_and_owner_reads():
    m = World().manager()
    assert m.is_free(F1) is True
    assert m.owner(F1) is None
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    assert m.is_free(F1) is False
    assert m.is_free(F1, "a") is True
    assert m.is_free(F1, "b") is False
    assert m.place("a") == {"key": "", "position": 0, "ahead_key": ""}
    assert m.place("b") == {"key": F1, "position": 1, "ahead_key": "a"}
    assert m.place("nobody") == {"key": "", "position": 0, "ahead_key": ""}


def test_owner_read_is_a_copy():
    m = World().manager()
    m.enqueue(F1, "a")
    m.owner(F1)["task"] = "mutated"
    assert owner_key(m) == "a"


# ----------------------------------------------------------------- reconcile


def test_reconcile_rebuilds_an_empty_index_from_pending_due():
    world = World(due=[(F1, "pending:e1", "e1"), (F1, "pending:e2", "e2"),
                       (F2, "pending:e3", "e3")])
    m = world.manager()                      # built, reconciled, then pumped
    assert owner_key(m) == "pending:e1"
    assert line_of(m) == ["pending:e2"]
    assert owner_key(m, F2) == "pending:e3"
    assert world.spawned == [(F1, "pending:e1"), (F2, "pending:e3")]


def test_reconcile_leaves_the_rebuilt_line_alone_when_the_folder_is_owned():
    world = idle_world(due=[(F1, "pending:e1", "e1")])
    m = world.manager()
    m.started(F1, "live", "run-live")
    world.running_keys.add("live")
    m.reconcile()
    assert owner_key(m) == "live"
    assert line_of(m) == ["pending:e1"]


def test_reconcile_drops_a_dead_owner_and_pumps():
    world = World()
    m = world.manager()
    m.started(F1, "a", "run-a")
    m.enqueue(F1, "b")
    world.running_keys.add("b")              # b is live; a is gone
    world.spawned.clear()
    m.reconcile()
    assert owner_key(m) == "b"
    assert world.spawned == [(F1, "b")]


def test_reconcile_keeps_a_blocked_owner():
    world = idle_world()
    m = world.manager()
    m.started(F1, "a")
    world.blocked_keys.add("a")
    m.reconcile()
    assert owner_key(m) == "a"


def test_reconcile_drops_line_and_blocked_keys_the_world_forgot():
    world = idle_world()
    m = world.manager()
    m.started(F1, "parked")
    m.card_raised("parked")                  # the blocked list, populated
    m.started(F1, "keep-owner")
    m.enqueue(F1, "gone")
    m.enqueue(F1, "live")
    world.running_keys.update({"keep-owner", "live"})
    world.blocked_keys.add("parked")

    m.reconcile()
    assert owner_key(m) == "keep-owner"
    assert line_of(m) == ["live"]
    assert blocked_of(m) == ["parked"]


def test_reconcile_does_not_duplicate_a_key_it_already_holds():
    world = idle_world(due=[(F1, "a", "e1"), (F1, "b", "e2")])
    m = world.manager()
    m.started(F1, "a")
    world.running_keys.add("a")
    m.reconcile()
    m.reconcile()
    assert owner_key(m) == "a"
    assert line_of(m) == ["b"]


def test_reconcile_ignores_a_malformed_due_row():
    world = World(due=[("only-one",), (F1, "", "e"), ("", "a", "e"),
                       (F1, "good", "e1")])
    m = world.manager()
    assert owner_key(m) == "good"


def test_reconcile_keeps_the_index_when_pending_due_raises():
    world = idle_world()
    m = world.manager()
    m.started(F1, "a")
    world.running_keys.add("a")

    def boom():
        raise RuntimeError("the store is gone")

    m._pending_due = boom
    m.reconcile()
    assert owner_key(m) == "a"


def test_reconcile_keeps_an_item_whose_status_read_raises():
    world = idle_world()
    m = world.manager()
    m.started(F1, "owner")
    m.enqueue(F1, "b")

    def boom(key):
        raise RuntimeError("the registry is unreadable")

    m._running = boom
    m.reconcile()
    assert owner_key(m) == "owner"
    assert line_of(m) == ["b"]


# --------------------------------------------------------------- persistence


def test_the_index_persists_across_a_fresh_instance(state):
    world = idle_world()
    m = world.manager()
    m.started(F1, "a", "run-a", "sess-a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    m.skip("c")
    m.card_answered("c", "run-c", "req-c", {"answer": "allow"})
    assert os.path.exists(os.path.join(str(state), qm.INDEX_FILE))

    second = idle_world()
    second.running_keys.update({"a", "b", "c"})
    fresh = second.manager()
    assert owner_key(fresh) == "a"
    assert fresh.owner(F1)["run_id"] == "run-a"
    assert fresh.owner(F1)["session_id"] == "sess-a"
    assert line_of(fresh) == ["c", "b"]
    assert fresh.positions()["c"]["priority"] is True
    assert fresh.held_answer("c")["raw"] == {"answer": "allow"}


def test_blocked_list_round_trips(state):
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a")
    assert blocked_of(m) == ["a"]

    second = idle_world()
    second.running_keys.add("b")
    second.blocked_keys.add("a")
    fresh = second.manager()
    assert blocked_of(fresh) == ["a"]
    assert owner_key(fresh) == "b"


def test_a_line_written_as_bare_strings_still_loads(state):
    with open(os.path.join(str(state), qm.INDEX_FILE), "w", encoding="utf-8") as f:
        json.dump({"folders": {F1: {"owner": {"task": "a"}, "line": ["b", ""],
                                    "blocked": ["c"]}},
                   "answers": {"c": {"run_id": "r", "request_id": "q", "raw": {}},
                               "junk": {"raw": "not a dict"}}}, f)
    world = idle_world()
    world.running_keys.update({"a", "b"})
    world.blocked_keys.add("c")
    m = world.manager()
    assert owner_key(m) == "a"
    assert line_of(m) == ["b"]
    assert blocked_of(m) == ["c"]
    assert m.positions()["b"]["priority"] is False
    assert m.held_answer("junk") is None


def test_a_corrupt_index_is_not_an_error(state):
    with open(os.path.join(str(state), qm.INDEX_FILE), "w", encoding="utf-8") as f:
        f.write("{not json")
    m = idle_world().manager()
    assert m.snapshot() == {"folders": {}, "answers": {}}


def test_snapshot_is_a_copy():
    m = World().manager()
    m.enqueue(F1, "a")
    snap = m.snapshot()
    snap["folders"][F1]["owner"]["task"] = "mutated"
    assert owner_key(m) == "a"


# ------------------------------------------------- the held-answers migration


def _legacy_file(state, *records):
    """The derived-holder layer's `held_answers.json`, written the way
    `project_queue.hold_answer` wrote it (`STORE_VERSION` 1 — the reader refuses
    a shape it does not know)."""
    path = os.path.join(str(state), qm.LEGACY_ANSWERS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "answers": list(records)}, f)
    return path


def _legacy(session_id, queue_key=F1, at=1.0, run_id="", request_id="", raw=None):
    return {"queue_key": queue_key, "session_id": session_id,
            "run_id": run_id or ("run-" + session_id),
            "request_id": request_id or ("req-" + session_id),
            "payload": {"raw": raw if raw is not None else {"decision": "allow"}},
            "at": at}


def test_a_legacy_held_answer_is_migrated_into_the_index(state):
    """A parked card decision is the ONE piece of queue state the old layer kept
    that a rebuild cannot re-derive: nobody but the user knows what they clicked.
    It comes across as an answer plus a place at the HEAD of its folder's line —
    the task is a turn already running and parked, owed the decision it was
    promised, which is where `card_answered` puts one taken today."""
    path = _legacy_file(state, _legacy("sess-a", raw={"decision": "allow",
                                                      "scope": "once"}))
    world = idle_world()
    world.running_keys.add("sess-a")
    m = loaded(world)
    assert m.held_answer("sess-a") == {"run_id": "run-sess-a",
                                       "request_id": "req-sess-a",
                                       "raw": {"decision": "allow", "scope": "once"},
                                       "at": 1.0}
    assert line_of(m) == ["sess-a"]
    assert m.positions()["sess-a"] == {"key": F1, "position": 1, "ahead_key": "",
                                       "ahead_names": [], "priority": True}
    assert not os.path.exists(path)
    assert os.path.exists(path + qm.MIGRATED_SUFFIX)
    # And the first reconcile is what actually hands it over — construction
    # itself never delivers, because construction never pumps.
    assert world.delivered == []
    m.reconcile()
    assert [a["request_id"] for a in world.delivered] == ["req-sess-a"]
    assert owner_key(m) == "sess-a"


def test_the_migration_survives_a_restart_that_has_already_run_it(state):
    """The index is the store now: a second process reads the answer out of
    `queue_index.json` and never looks at the renamed file again."""
    _legacy_file(state, _legacy("sess-a"))
    first = idle_world()
    first.running_keys.add("sess-a")
    loaded(first)
    second = idle_world()
    second.running_keys.add("sess-a")
    again = loaded(second)
    assert again.held_answer("sess-a")["request_id"] == "req-sess-a"
    assert line_of(again) == ["sess-a"]


def test_the_migration_keeps_the_old_order_and_splits_by_folder(state):
    """Oldest `at` first — the order the old deliverer used — and each folder
    gets its own head, so two answers parked for two projects do not interleave."""
    _legacy_file(state,
                 _legacy("sess-c", queue_key=F2, at=30.0),
                 _legacy("sess-b", at=20.0),
                 _legacy("sess-a", at=10.0))
    world = idle_world()
    world.running_keys.update({"sess-a", "sess-b", "sess-c"})
    m = loaded(world)
    assert line_of(m, F1) == ["sess-a", "sess-b"]
    assert line_of(m, F2) == ["sess-c"]
    assert set(m.snapshot()["answers"]) == {"sess-a", "sess-b", "sess-c"}


def test_the_migration_does_not_queue_a_task_the_index_already_places(state):
    """The index is the newer truth. A session that already owns its folder, or
    already stands in a line, keeps the place it has and only gains the answer —
    two entries for one task is a line that runs a turn twice."""
    with open(os.path.join(str(state), qm.INDEX_FILE), "w", encoding="utf-8") as f:
        json.dump({"folders": {F1: {"owner": {"task": "sess-a", "run_id": "run-sess-a"},
                                    "line": [{"task": "sess-b"}], "blocked": []}},
                   "answers": {}}, f)
    _legacy_file(state, _legacy("sess-a", at=10.0), _legacy("sess-b", at=20.0))
    world = idle_world()
    world.running_keys.update({"sess-a", "sess-b"})
    m = loaded(world)
    assert owner_key(m) == "sess-a"
    assert line_of(m) == ["sess-b"]
    assert m.held_answer("sess-a")["request_id"] == "req-sess-a"
    assert m.held_answer("sess-b")["request_id"] == "req-sess-b"


def test_no_legacy_file_is_not_a_migration(state):
    """The ordinary case, and it must cost nothing: no file, no rename, no write
    of an index that had nothing to say."""
    loaded(idle_world())
    assert not os.path.exists(os.path.join(str(state), qm.INDEX_FILE))
    assert not os.path.exists(
        os.path.join(str(state), qm.LEGACY_ANSWERS_FILE + qm.MIGRATED_SUFFIX))


def test_a_legacy_record_with_no_session_is_skipped(state):
    """An answer nobody can be given is an answer nobody is owed. The file still
    moves aside — re-reading it every load would keep failing the same way."""
    path = _legacy_file(state, {"queue_key": F1, "session_id": "", "run_id": "r",
                                "request_id": "q", "payload": {}, "at": 1.0},
                        "not even a dict")
    m = loaded(idle_world())
    assert m.snapshot()["answers"] == {}
    assert m.snapshot()["folders"] == {}
    assert os.path.exists(path + qm.MIGRATED_SUFFIX)


def test_the_migration_survives_a_legacy_store_that_will_not_read(state):
    """A corrupt file holds nothing, the same answer the old store always gave,
    and construction still succeeds."""
    with open(os.path.join(str(state), qm.LEGACY_ANSWERS_FILE), "w",
              encoding="utf-8") as f:
        f.write("{not json")
    m = loaded(idle_world())
    assert m.snapshot()["answers"] == {}


# -------------------------------------------------------------------- notify


def test_notify_carries_the_keys_each_event_touched():
    world = World()
    m = world.manager()
    world.notified.clear()

    m.enqueue(F1, "a")
    assert world.notified == [{"a"}]
    world.notified.clear()

    m.enqueue(F1, "b")
    assert world.notified == [{"b"}]
    world.notified.clear()

    m.turn_ended("a")                        # a released, b pumped
    assert world.notified == [{"a", "b"}]


def test_notify_stays_quiet_when_nothing_moved():
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    world.notified.clear()
    m.turn_ended("nobody")
    m.skip("nobody")
    m.card_raised("nobody")
    m.remove("nobody")
    m.pump(F1)
    assert world.notified == []


def test_notify_is_optional():
    m = qm.QueueManager(spawn=lambda f, k: None, deliver=lambda a: None,
                        running=lambda k: False, blocked=lambda k: False,
                        pending_due=lambda: [])
    m.enqueue(F1, "a")                       # would raise if notify were required
    assert m.snapshot()["folders"][F1]["line"] == []


def test_skip_notifies_everyone_whose_line_position_moved():
    """User screenshot, PR #1194: after Skip on b the Tasks list showed BOTH
    "b 1st in line" and "a 1st in line" — `skip` only notified the task it
    was called on, so a's row (now second, behind b) kept its stale "1st in
    line" fact. Reordering the line moves EVERY task still in it, including
    c whose `ahead_key` flips from b to a."""
    world = World()
    m = world.manager()
    for key in ("owner", "a", "b", "c"):
        m.enqueue(F1, key)
    world.notified.clear()

    m.skip("b")

    assert line_of(m) == ["b", "a", "c"]
    assert world.notified == [{"owner", "a", "b", "c"}]


def test_turn_ended_notifies_the_new_owner_and_the_rest_of_the_line():
    """A pump handing the folder to the next task shifts every remaining line
    item's position/ahead_key too, not just the outgoing and incoming
    owner."""
    world = World()
    m = world.manager()
    for key in ("owner", "a", "b", "c"):
        m.enqueue(F1, key)
    world.notified.clear()

    m.turn_ended("owner")

    assert owner_key(m) == "a"
    assert line_of(m) == ["b", "c"]
    assert world.notified == [{"owner", "a", "b", "c"}]


def test_card_answered_inserting_at_head_notifies_the_whole_line():
    """`card_answered` while busy promotes the parked task to the head of the
    line — everybody already queued behind it (here, c) moves down one and
    needs a fresh row too."""
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    m.card_raised("a")                       # b owns, c waits, a blocked
    world.notified.clear()

    m.card_answered("a", "run-a", "req-1", {"answer": "allow"})

    assert line_of(m) == ["a", "c"]
    assert world.notified == [{"a", "c"}]


def test_remove_of_a_line_item_notifies_everyone_still_behind_it():
    """Removing a's entry moves b and c up a slot each even though their
    relative order to each other never changes."""
    world = World()
    m = world.manager()
    for key in ("owner", "a", "b", "c"):
        m.enqueue(F1, key)
    world.notified.clear()

    m.remove("a")

    assert line_of(m) == ["b", "c"]
    assert world.notified == [{"a", "b", "c"}]


# ----------------------------------------------------------------- claim
#
# `is_free` then `started` is two acquisitions of one lock with a gap in
# between, and two sends into one free folder both heard "free".


def test_claim_is_the_check_and_the_filing():
    m = idle_world().manager()
    assert m.claim(F1, "a", "run-a", "sess-a") is True
    assert owner_key(m) == "a"
    assert (m.owner(F1)["run_id"], m.owner(F1)["session_id"]) == ("run-a", "sess-a")
    assert m.claim(F1, "b", "run-b", "sess-b") is False
    assert owner_key(m) == "a"


def test_claim_by_the_owner_itself_is_still_true():
    """A second message typed into a conversation that is running is the
    inbox-absorb case the chat has always had — and any of the three names
    answers for it."""
    m = idle_world().manager()
    m.claim(F1, "a", "run-a", "sess-a")
    assert m.claim(F1, "a") is True
    assert m.claim(F1, "sess-a") is True
    assert m.claim(F1, "other", run_id="run-a") is True
    assert owner_key(m) == "a"


def test_claim_learns_the_better_name_the_second_message_carries():
    m = idle_world().manager()
    m.claim(F1, "run-1", run_id="run-1")
    assert m.claim(F1, "run-1", run_id="run-1", session_id="sess-1") is True
    assert m.owner(F1)["session_id"] == "sess-1"


def test_claim_ignores_a_blank_folder_or_key():
    m = idle_world().manager()
    assert m.claim("", "a") is False
    assert m.claim(F1, "") is False
    assert m.snapshot()["folders"] == {}


def test_claim_takes_a_task_out_of_whatever_line_it_stood_in():
    m = idle_world().manager()
    m.started(F1, "owner")
    m.enqueue(F1, "b")
    assert m.claim(F2, "b", "run-b") is True
    assert line_of(m, F1) == []
    assert owner_key(m, F2) == "b"


def test_claim_pumps_a_folder_it_pulled_a_stuck_line_head_out_of():
    """Bugbot, PR #1194: `claim` taking a task out of another folder's line
    used to never pump that folder — only `remove`/`started` pulling a task
    out of a folder it OWNED did. After `SpawnBusy` or a failed deliver a
    folder's owner can already be None with its line sitting on its own, and
    the item this call takes may be the only thing ever going to unstick it."""
    world = World()
    m = world.manager()
    m._state["folders"][F1] = {
        "owner": None, "blocked": [],
        "line": [{"task": "b", "entry_id": "", "promoted": False,
                 "run_id": "", "session_id": "", "resumed": False},
                {"task": "c", "entry_id": "", "promoted": False,
                 "run_id": "", "session_id": "", "resumed": False}]}
    world.spawned.clear()

    assert m.claim(F2, "b", "run-b", "sess-b") is True

    assert line_of(m, F1) == []
    assert owner_key(m, F1) == "c"
    assert world.spawned == [(F1, "c")]
    assert owner_key(m, F2) == "b"


def test_started_pumps_a_folder_it_pulled_a_stuck_line_head_out_of():
    world = World()
    m = world.manager()
    m._state["folders"][F1] = {
        "owner": None, "blocked": [],
        "line": [{"task": "b", "entry_id": "", "promoted": False,
                 "run_id": "", "session_id": "", "resumed": False},
                {"task": "c", "entry_id": "", "promoted": False,
                 "run_id": "", "session_id": "", "resumed": False}]}
    world.spawned.clear()

    m.started(F2, "b", "run-b", "sess-b")

    assert line_of(m, F1) == []
    assert owner_key(m, F1) == "c"
    assert world.spawned == [(F1, "c")]
    assert owner_key(m, F2) == "b"


def test_claim_took_separates_taking_a_folder_from_already_owning_it():
    """One bool could not tell "I have it now" from "I already had it", and
    run-now hands the tree back on a later refusal — which, in the `own` case,
    handed back the live turn of the chat this message follows (Bugbot #1194)."""
    m = idle_world().manager()
    assert m.claim_took(F1, "a", "run-a", "sess-a") == (True, True)
    assert m.claim_took(F1, "a", "run-a", "sess-a") == (True, False)
    assert m.claim_took(F1, "sess-a") == (True, False)
    assert m.claim_took(F1, "b", "run-b") == (False, False)
    assert m.claim_took("", "a") == (False, False)
    assert m.claim(F1, "a") is True, "the bool wrapper is what the doors read"


def test_a_follow_up_claim_absorbed_survives_one_turn_ended():
    """Bugbot, PR #1194: a follow-up `claim` that finds its own conversation
    already owns the folder used to return `(True, False)` without recording
    that a SECOND send is now in flight — so the EARLIER send's `turn_ended`
    released the owner while the absorbed send was still running in the same
    host. `turns` counts sends in flight; only the matching number of
    `turn_ended` calls releases the folder."""
    m = idle_world().manager()
    assert m.claim_took(F1, "a", "run-a", "sess-a") == (True, True)
    assert m.claim_took(F1, "a", "run-a", "sess-a") == (True, False)   # absorbed
    assert m.owner(F1)["turns"] == 2

    m.turn_ended("a", "run-a")               # the first send's result row
    assert owner_key(m) == "a", "the absorbed send is still running"

    m.turn_ended("a", "run-a")               # the second send's result row
    assert owner_key(m) is None


def test_started_never_counts_a_turn_even_declared_twice():
    """`started` is a REFILE, never a claim (Bugbot, PR #1194 second pass): the
    caller — admit, run-now — already counted this send with `claim`/
    `claim_took` before it ever reaches `started`, so `started` overwriting the
    owner unconditionally used to also increment `turns`, and one send crossing
    both doors (admit, then the spawn site's refile) turned into two turns that
    needed two `turn_ended`s to free — which never happened, because only one
    send was ever in flight."""
    m = idle_world().manager()
    m.started(F1, "a", "run-a", "sess-a")
    m.started(F1, "a", "run-a", "sess-a")
    assert m.owner(F1)["turns"] == 1
    m.turn_ended("a", "run-a")
    assert owner_key(m) is None


def test_claim_then_started_for_the_same_send_counts_one_turn():
    """The real sequence one send takes through `/api/run`: admit's `claim`
    takes the folder, then the spawn site's `started` refiles the run/session
    it returned. Bugbot, PR #1194: `started` used to count a second turn here
    on top of `claim`'s, and `_folder_busy`'s own now-removed claim counted a
    third — one send, three increments, and `turn_ended`'s single decrement
    never brought `turns` back to zero."""
    m = idle_world().manager()
    assert m.claim(F1, "a", "run-a", "sess-a") is True
    m.started(F1, "a", "run-a", "sess-a")
    assert m.owner(F1)["turns"] == 1
    m.turn_ended("a", "run-a")
    assert owner_key(m) is None


def test_started_on_a_folder_with_no_owner_files_one_turn():
    """The legacy/anonymous path: a spawn `claim` never saw. `started` files a
    fresh owner with `turns = 1`, exactly like any other first ownership."""
    m = idle_world().manager()
    m.started(F1, "a", "run-a", "sess-a")
    assert m.owner(F1)["turns"] == 1
    m.turn_ended("a", "run-a")
    assert owner_key(m) is None


def test_exited_releases_regardless_of_turns_in_flight():
    """The child process is gone; no send can still be running in it, so
    `exited` must not wait for as many calls as `turns` counts."""
    m = idle_world().manager()
    m.claim_took(F1, "a", "run-a", "sess-a")
    m.claim_took(F1, "a", "run-a", "sess-a")
    assert m.owner(F1)["turns"] == 2
    m.exited("a", "run-a", 0)
    assert owner_key(m) is None


def test_turns_persist_across_a_fresh_instance(state):
    world = idle_world()
    m = world.manager()
    m.claim_took(F1, "a", "run-a", "sess-a")
    m.claim_took(F1, "a", "run-a", "sess-a")

    second = idle_world()
    second.running_keys.add("a")
    fresh = second.manager()
    assert fresh.owner(F1)["turns"] == 2
    fresh.turn_ended("a", "run-a")
    assert owner_key(fresh) == "a"
    fresh.turn_ended("a", "run-a")
    assert owner_key(fresh) is None


def test_a_single_turn_still_releases_on_the_first_turn_ended():
    """No regression for the ordinary case: one send in, one `turn_ended`,
    released."""
    m = idle_world().manager()
    m.claim_took(F1, "a", "run-a", "sess-a")
    assert m.owner(F1)["turns"] == 1
    m.turn_ended("a", "run-a")
    assert owner_key(m) is None


def test_claim_took_calls_a_placeholder_a_take():
    world = idle_world()
    m = world.manager()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one")
    assert m.claim_took(F1, "sess-new", "run-1", "sess-new") == (True, True)

    m2 = idle_world().manager()
    m2.claim(F1, qm.PLACEHOLDER_PREFIX + "one")
    assert m2.claim_took(F1, qm.PLACEHOLDER_PREFIX + "two") == (False, False)


def test_claim_took_over_a_placeholder_drops_its_claims_and_turns():
    """CORRECTED 2026-09-17, Bugbot PR #1194, fourth round: the third round
    had `claim_took`'s NAMED-claim-gives-way-to-a-placeholder branch INHERIT
    the placeholder's claims and turns, the same way `started` does for its
    own rename. But every caller that reaches this branch is a DIFFERENT
    send from the one the placeholder was minted for — the SAME admission
    naming itself always goes through `started`, never back through
    `claim_took` — so inheriting here let an admitted nameless send's token
    keep consuming after a STRANGER took the folder: `admitted` alone then
    proved nothing, and that nameless send spawned into a tree the stranger
    now owned. The fix drops the claims (and, with them, the turn count)
    instead: the fresh owner starts clean, like any other first ownership,
    and the nameless send that lost its placeholder has to re-admit."""
    m = idle_world().manager()
    _ok, _took, first = m.claim_for_send(F1, qm.PLACEHOLDER_PREFIX + "one")
    _ok2, _took2, second = m.claim_for_send(F1, qm.PLACEHOLDER_PREFIX + "one")
    assert m.owner(F1)["turns"] == 2
    ok, took = m.claim_took(F1, "sess-new", "run-1", "sess-new")
    assert (ok, took) == (True, True)
    owner = m.owner(F1)
    assert owner["task"] == "sess-new"
    assert owner["turns"] == 1
    assert m.consume_claim(F1, first) is False
    assert m.consume_claim(F1, second) is False


# ----------------------------------------------------- the per-send claim token
#
# Bugbot, PR #1194, second round: a gate that only LOOKS (never claims) let a
# send that skipped admission through unchecked into a free folder — the
# older bug this whole feature exists to close. A gate that always CLAIMS
# double-counts the ordinary admit→run send. The token is what tells the two
# apart: admit mints one on every successful `claim_for_send`, the client
# echoes it back on the run request, and `consume_claim` finding it there is
# proof this send was already counted.


def test_claim_for_send_yields_a_token_and_consume_claim_spends_it_once():
    m = idle_world().manager()
    ok, took, token = m.claim_for_send(F1, "a", "run-a", "sess-a")
    assert (ok, took) == (True, True)
    assert token and isinstance(token, str)
    assert m.consume_claim(F1, token) is True
    assert m.consume_claim(F1, token) is False, "a token is spent once"


def test_claim_for_send_mints_no_token_on_a_refusal():
    """False `ok` means nothing was claimed, so there is nothing for admit to
    hand the client — a caller that redeemed a "" token would find nothing
    (`consume_claim` refuses an empty one outright)."""
    m = idle_world().manager()
    m.claim_took(F1, "a", "run-a", "sess-a")
    assert m.claim_for_send(F1, "b", "run-b", "sess-b") == (False, False, "")


def test_claim_for_send_absorbed_still_mints_its_own_token():
    """A follow-up absorbed into a turn already running is counted (`turns`)
    exactly as `claim_took` counts it, and it earns its OWN token — two sends
    in flight are two different claims to redeem, not one shared between
    them."""
    m = idle_world().manager()
    _ok, _took, first = m.claim_for_send(F1, "a", "run-a", "sess-a")
    ok, took, second = m.claim_for_send(F1, "a", "run-a", "sess-a")
    assert (ok, took) == (True, False)
    assert second and second != first
    assert m.owner(F1)["turns"] == 2
    assert m.consume_claim(F1, first) is True
    assert m.consume_claim(F1, second) is True


def test_claim_took_and_claim_still_answer_their_own_shape():
    """`schedule.py::_claim_folder` unpacks `claim_took` positionally and
    every door reads `claim` as a bool — a token is not added to either."""
    m = idle_world().manager()
    assert m.claim_took(F1, "a", "run-a", "sess-a") == (True, True)
    assert m.claim(F2, "b", "run-b", "sess-b") is True


def test_consume_claim_is_false_for_an_empty_token_or_unknown_folder():
    m = idle_world().manager()
    m.claim_for_send(F1, "a", "run-a", "sess-a")
    assert m.consume_claim(F1, "") is False
    assert m.consume_claim("", "whatever") is False
    assert m.consume_claim("/nowhere", "whatever") is False


def test_consume_claim_fails_once_a_different_owner_takes_the_folder():
    """A token only proves a send was admitted once — not that the owner it
    was minted for still holds the folder. Once the turn ends and somebody
    else takes it fresh, the old owner's unconsumed claims are gone with it."""
    m = idle_world().manager()
    _ok, _took, token = m.claim_for_send(F1, "a", "run-a", "sess-a")
    m.exited("a", "run-a")
    m.claim_took(F1, "b", "run-b", "sess-b")
    assert m.consume_claim(F1, token) is False


def test_claims_are_capped():
    """An admission a page never sent — a stale card, a reload — leaves an
    unconsumed token behind; `CLAIM_CAP` keeps a long-lived owner's list from
    growing without bound."""
    m = idle_world().manager()
    tokens = [m.claim_for_send(F1, "a", "run-a", "sess-a")[2]
              for _ in range(qm.CLAIM_CAP + 5)]
    assert len(m.snapshot()["folders"][F1]["owner"]["claims"]) == qm.CLAIM_CAP
    # The newest tokens survive; the oldest were pushed out.
    for token in tokens[-qm.CLAIM_CAP:]:
        assert m.consume_claim(F1, token) is True
    for token in tokens[:5]:
        assert m.consume_claim(F1, token) is False


# ------------------------------------------------------- the admit placeholder


def test_a_nameless_admission_owns_the_folder_under_a_placeholder():
    """A brand-new chat's first send has no session and no run — the spawn has
    not happened, because this is the call that says it may. Filing nobody left
    the folder reading free and let a second nameless send race into it."""
    m = idle_world().manager()
    assert m.claim(F1, qm.PLACEHOLDER_PREFIX + "one") is True
    assert m.claim(F1, qm.PLACEHOLDER_PREFIX + "two") is False
    assert owner_key(m) == qm.PLACEHOLDER_PREFIX + "one"


def test_a_placeholder_gives_way_to_a_real_name():
    """The chat that minted it, arriving with a run — or another one in the same
    window, which cannot be told apart from here. Refusing would re-open the bug
    the whole identity chase is about (a chat queued behind itself)."""
    m = idle_world().manager()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one")
    assert m.claim(F1, "sess-new", "run-1", "sess-new") is True
    assert owner_key(m) == "sess-new"


def test_started_from_the_real_spawn_retires_the_placeholder():
    """The normal path: admission claims the placeholder AND mints a token,
    the run gate `consume_claim`s it (proof this is the very send that was
    admitted — the mark `started` now checks, Bugbot PR #1194 third round),
    and only then is the spawn's own refile trusted to replace it."""
    m = idle_world().manager()
    _ok, _took, token = m.claim_for_send(F1, qm.PLACEHOLDER_PREFIX + "one")
    assert m.consume_claim(F1, token) is True
    m.started(F1, "sess-new", "run-1", "sess-new")
    owner = m.owner(F1)
    assert (owner["task"], owner["run_id"]) == ("sess-new", "run-1")


def test_started_never_overwrites_a_live_foreign_placeholder():
    """CORRECTED 2026-09-17, Bugbot PR #1194, third round: `started` used to
    trust ANY caller to retire a placeholder, so a stranger's spawn — one
    that skipped admission and reached the spawn site some other way — could
    clobber another admission's live reservation and run two brand-new chats
    in one tree. Without a consumed claim as proof, `started` cannot tell
    that caller apart from the admission that filed the placeholder, so it
    refuses (no-op) rather than guess; the real fix is the run gate refusing
    that stranger before it ever spawns (`is_free`, `test_run_folder_gate.py`)
    — this is the backstop for a caller that reaches `started` anyway."""
    m = idle_world().manager()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one")   # no token ever consumed
    m.started(F1, "stranger-sess", "run-x", "stranger-sess")
    owner = m.owner(F1)
    assert owner["task"] == qm.PLACEHOLDER_PREFIX + "one"


def test_started_overwrites_an_expired_placeholder_with_no_token():
    """An expired placeholder gives way unconditionally, same as `is_free` —
    nothing will ever post an event for a process that never started, so it
    cannot be expected to present a token either."""
    world = idle_world()
    m = world.manager()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one")
    world.now += qm.PLACEHOLDER_TTL + 5
    m.started(F1, "sess-new", "run-1", "sess-new")
    owner = m.owner(F1)
    assert (owner["task"], owner["run_id"]) == ("sess-new", "run-1")


def test_a_live_placeholder_is_free_only_to_itself():
    """CORRECTED 2026-09-17, Bugbot PR #1194, third round: `is_free` used to
    call a live placeholder free to EVERY caller, so the run gate's
    `is_free(folder, "")` on a nameless send let a STRANGER's send past a
    folder another admission had already reserved, and two brand-new chats
    spawned in one tree (see `test_run_folder_gate.py`, the real gate). A
    live placeholder is free only to the exact key it was minted under; a
    caller with a real name proves itself with a consumed claim token
    instead, which `is_free` cannot see (`routers/run.py::_folder_busy`)."""
    m = idle_world().manager()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one")
    assert m.is_free(F1) is False
    assert m.is_free(F1, "stranger") is False
    assert m.is_free(F1, qm.PLACEHOLDER_PREFIX + "one") is True


def test_a_placeholder_expires_and_never_refuses_a_send():
    """THE ONE CLOCK IN THE QUEUE: no process exists yet, so no event can ever
    free this record and it has to free itself. Once expired it is free to
    anybody, same as before — only a LIVE placeholder is now guarded."""
    world = idle_world()
    m = world.manager()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one")

    world.now += qm.PLACEHOLDER_TTL + 5
    assert m.is_free(F1) is True
    assert m.is_free(F1, "anybody") is True
    assert m.claim(F1, qm.PLACEHOLDER_PREFIX + "two") is True
    assert owner_key(m) == qm.PLACEHOLDER_PREFIX + "two"


def test_reconcile_drops_an_expired_placeholder_and_pumps():
    world = World()
    m = world.manager()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one")
    world.due = [(F1, "b", "e-b")]
    world.spawned.clear()
    m.reconcile()
    assert owner_key(m) == qm.PLACEHOLDER_PREFIX + "one"   # still inside the TTL
    assert line_of(m) == ["b"]

    world.now += qm.PLACEHOLDER_TTL + 5
    m.reconcile()
    assert owner_key(m) == "b"
    assert world.spawned == [(F1, "b")]


def test_a_placeholder_owner_is_nobody_to_name():
    """`admit:<token>` names no row on the Tasks page, and the client says
    "behind a run in this folder" for an empty `ahead_key` — where printing the
    token would put a uuid in the chip and link it to nothing."""
    m = idle_world().manager()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one")
    m.enqueue(F1, "b")
    assert m.positions()["b"]["ahead_key"] == ""
    assert m.place("b") == {"key": F1, "position": 1, "ahead_key": ""}


def test_an_owner_named_only_by_its_run_is_nobody_to_name_either():
    m = idle_world().manager()
    m.started(F1, "", run_id="run-1")
    m.enqueue(F1, "b")
    assert m.positions()["b"]["ahead_key"] == ""


def test_positions_names_the_owner_every_way_the_page_might_have_filed_it():
    """`ahead_key` IS WHAT TO PRINT; `ahead_names` IS WHAT TO LOOK UP WITH.

    A brand-new chat holds the folder under the only name it had when its turn
    started — its run id — and the Tasks page files its row under the session
    Claude Code minted a moment later. One chat, two names, and the row behind
    it read "1st in line" with nothing after it for as long as the reader was
    only offered the first (`tasks.py::_name_ahead`)."""
    m = idle_world().manager()
    m.started(F1, "", run_id="run-1", session_id="sess-1")
    m.enqueue(F1, "b")
    spot = m.positions()["b"]
    assert spot["ahead_key"] == "run-1"
    assert spot["ahead_names"] == ["run-1", "sess-1"]


def test_positions_never_offers_the_placeholder_token_but_keeps_its_other_names():
    """`admit:<token>` names nothing anywhere and is dropped from the lookup
    list too — but a placeholder with a run filed against it is a turn like any
    other, and that run is a name worth trying."""
    m = idle_world().manager()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one", run_id="run-9")
    m.enqueue(F1, "b")
    spot = m.positions()["b"]
    assert spot["ahead_key"] == ""
    assert spot["ahead_names"] == ["run-9"]


def test_positions_names_the_item_ahead_by_its_entry_as_well_as_its_key():
    """Position n > 1 stands behind another QUEUED task, and that one can be
    filed under a name of its own too — the entry it was stored as, which is the
    one name of a scheduled message that never moves while its key rekeys onto
    the session its run publishes."""
    m = idle_world().manager()
    m.started(F1, "a")
    m.enqueue(F1, "b", entry_id="e2")
    m.enqueue(F1, "c")
    assert m.positions()["c"]["ahead_key"] == "b"
    assert m.positions()["c"]["ahead_names"] == ["b", "pending:e2"]
    # …and the owner's own names are unchanged by any of it.
    assert m.positions()["b"]["ahead_names"] == ["a"]


def test_a_placeholder_never_rings_the_long_poll():
    world = idle_world()
    m = world.manager()
    world.notified.clear()
    m.claim(F1, qm.PLACEHOLDER_PREFIX + "one")
    assert world.notified == []


# ---------------------------------------------------- several cards, one run


def test_two_cards_of_one_run_are_both_held_and_both_delivered():
    """Keyed by `(task, run, request)`. A store keyed by the task alone let the
    second answer overwrite the first, and the first decision — given by a human
    who is waiting for it — was never delivered at all."""
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a", "run")
    assert m.card_answered("a", "run-a", "req-1", {"n": 1})["held"] is True
    assert m.card_answered("a", "run-a", "req-2", {"n": 2})["held"] is True
    assert [row["request_id"] for row in m.held_answers("a")] == ["req-1", "req-2"]

    m.turn_ended("b", "run")
    assert [d["request_id"] for d in world.delivered] == ["req-1", "req-2"]
    assert m.held_answers("a") == []
    assert owner_key(m) == "a"


def test_the_same_question_answered_twice_keeps_the_first_verdict():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a", "run")
    m.card_answered("a", "run-a", "req-1", {"answer": "allow"})
    assert m.card_answered("a", "run-a", "req-1", {"answer": "deny"}) == {
        "held": True, "position": 1}
    assert [row["raw"] for row in m.held_answers("a")] == [{"answer": "allow"}]


def test_a_second_card_delivered_after_a_failure_keeps_the_first_one_gone():
    """Idempotent: an answer is dropped the moment ITS delivery returns, so a
    retry replays only what is left."""
    world = World()
    seen = []

    def deliver(answer):
        seen.append(answer["request_id"])
        if answer["request_id"] == "req-2" and len(seen) == 2:
            raise RuntimeError("the run went away")

    world.deliver = deliver
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a", "run")
    m.card_answered("a", "run-a", "req-1", {})
    m.card_answered("a", "run-a", "req-2", {})
    m.turn_ended("b", "run")
    assert seen == ["req-1", "req-2"]
    assert [row["request_id"] for row in m.held_answers("a")] == ["req-2"]

    m.pump(F1)
    assert seen == ["req-1", "req-2", "req-2"]
    assert m.held_answers("a") == []


# ------------------------------------------------------------ run identity


def test_card_raised_matches_the_owner_by_its_run():
    """The permission server knows the RUN it is serving; the owner may be filed
    under a `pending:` key that card never heard of."""
    m = idle_world().manager()
    m.started(F1, "pending:e1", "run-7", "sess-7")
    m.card_raised("sess-7", "run-7")
    assert owner_key(m) is None
    assert blocked_of(m) == ["sess-7"]
    item = m.snapshot()["folders"][F1]["blocked"][0]
    assert (item["run_id"], item["session_id"]) == ("run-7", "sess-7")


def test_a_card_from_an_old_run_does_not_park_the_new_owner():
    m = idle_world().manager()
    m.started(F1, "sess-1", "run-2", "sess-1")
    m.card_raised("sess-1", "run-1")         # an older run of the same chat
    assert owner_key(m) == "sess-1"
    assert blocked_of(m) == []


def test_a_stale_exit_from_an_old_run_does_not_release_the_owner():
    """When both sides name a run, the runs decide and nothing else does: a
    session outlives many runs, and the host retries."""
    m = idle_world().manager()
    m.started(F1, "sess-1", "run-2", "sess-1")
    m.exited("sess-1", "run-1", 0)
    m.turn_ended("sess-1", "run-1")
    assert owner_key(m) == "sess-1"

    m.turn_ended("sess-1", "run-2")
    assert owner_key(m) is None


def test_an_event_with_no_run_id_still_matches_by_session():
    m = idle_world().manager()
    m.started(F1, "pending:e1", "run-7", "sess-7")
    m.turn_ended("sess-7")
    assert owner_key(m) is None


# ------------------------------------------------------------- card_cleared


def test_card_cleared_takes_a_free_folder_back():
    """Somebody answered it elsewhere — a terminal, a file. The task is a live
    turn that is no longer waiting on a human."""
    m = idle_world().manager()
    m.started(F1, "a", "run-a", "sess-a")
    m.card_raised("a", "run-a")
    assert (owner_key(m), blocked_of(m)) == (None, ["a"])

    m.card_cleared("a", "run-a", "req-1")
    assert owner_key(m) == "a"
    assert blocked_of(m) == []
    assert m.owner(F1)["run_id"] == "run-a"


def test_card_cleared_goes_to_the_head_when_somebody_else_has_the_folder():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.enqueue(F1, "c")
    m.card_raised("a", "run")                # b owns, c waits, a blocked
    m.card_cleared("a", "run")
    assert blocked_of(m) == []
    assert line_of(m) == ["a", "c"]
    assert m.positions()["a"]["priority"] is True


def test_card_cleared_leaves_a_verdict_we_are_already_holding_alone():
    """A task with a held answer stands in the LINE, not blocked, so this is a
    no-op for it — and that is the wanted answer: the held decision is what makes
    the pump hand the folder back to that live run instead of trying to spawn a
    turn for a session that is already running."""
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a", "run")
    m.card_answered("a", "run-a", "req-1", {"answer": "allow"})
    m.card_cleared("a", "run-a", "req-1")
    assert [row["request_id"] for row in m.held_answers("a")] == ["req-1"]
    assert line_of(m) == ["a"]


def test_card_cleared_is_idempotent_and_ignores_a_task_that_is_not_parked():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    before = m.snapshot()
    m.card_cleared("a", "run")               # the owner, not parked
    m.card_cleared("nobody", "run")
    assert m.snapshot() == before

    m.card_raised("a", "run")
    m.card_cleared("a", "run")
    after = m.snapshot()
    m.card_cleared("a", "run")
    assert m.snapshot() == after


# ------------------------------------------ card_cleared: the resume marker
#
# The card was answered by a route that does NOT hold the folder — a terminal,
# a file, another UI — while another task owns the tree. That run is resuming
# right now and waits for nobody, so what goes into the line is not work to
# start: it is a process to hand the folder to (Bugbot, PR #1194).

RUNS = {"a": {"run_id": "run-a", "session_id": "sess-a"},
        "b": {"run_id": "run-b", "session_id": "sess-b"},
        "c": {"run_id": "run-c", "session_id": "sess-c"}}


def parked_elsewhere(**kw):
    """a is a live run whose card was answered outside the queue; b holds the
    folder. Returns `(world, manager)` with the marker already filed."""
    world = World(spawns=RUNS, **kw)
    m = world.manager()
    m.enqueue(F1, "a", "e-a")
    m.enqueue(F1, "b", "e-b")
    m.card_raised("a", "run-a")              # b takes the folder, a is parked
    m.card_cleared("a", "run-a", "req-1")
    world.spawned.clear()
    return world, m


def test_card_cleared_files_a_resume_marker_when_somebody_else_holds():
    _world, m = parked_elsewhere()
    head = m.snapshot()["folders"][F1]["line"][0]
    assert head["task"] == "a"
    assert head["resumed"] is True
    assert head["promoted"] is True
    assert head["entry_id"] == ""
    assert head["run_id"] == "run-a"


def test_the_pump_hands_the_folder_to_a_resume_marker_without_spawning():
    """THE BUG: filed as ordinary queued work the marker named no pending entry,
    `spawn` answered None, the pump dropped it — and started the NEXT task in
    the tree the resuming process was already editing."""
    world, m = parked_elsewhere()
    m.enqueue(F1, "c", "e-c")

    m.turn_ended("b", "run-b")               # the owner finishes

    assert owner_key(m) == "a"
    assert world.spawned == [], "a second turn beside the run that is resuming"
    assert m.owner(F1)["run_id"] == "run-a"
    assert line_of(m) == ["c"]


def test_a_resume_marker_with_a_held_verdict_is_still_delivered():
    """The two can meet: this card was cleared elsewhere and a verdict of ours
    for another card of the same run is still waiting. Delivery wins — it is
    the one thing that run is owed, and it is a no-op on disk if it is late."""
    world, m = parked_elsewhere()
    m.card_answered("a", "run-a", "req-2", {"answer": "allow"})

    m.turn_ended("b", "run-b")

    assert owner_key(m) == "a"
    assert world.spawned == []
    assert [row["request_id"] for row in world.delivered] == ["req-2"]


def test_reconcile_keeps_a_live_resume_marker_and_drops_a_dead_one():
    """It names no pending entry — it never will — so the only thing that says
    whether it is still worth a folder is the status sync."""
    world, m = parked_elsewhere()
    world.running_keys.add("run-b")          # the owner is still going

    world.running_keys.add("run-a")
    m.reconcile()
    assert line_of(m) == ["a"]

    world.running_keys.discard("run-a")
    m.reconcile()
    assert line_of(m) == []


def test_forget_entry_leaves_a_resume_marker_alone():
    """Cancelling a message cannot un-queue a process that is already running."""
    _world, m = parked_elsewhere()
    m.forget_entry("e-a")
    assert line_of(m) == ["a"]
    assert m.snapshot()["folders"][F1]["line"][0]["resumed"] is True


def test_a_resume_marker_survives_a_restart():
    world, _m = parked_elsewhere()
    head = loaded(world).snapshot()["folders"][F1]["line"][0]
    assert (head["task"], head["resumed"]) == ("a", True)


# ------------------------------------------------------------- forget_entry


def test_forget_entry_drops_one_message_and_leaves_the_turn_alone():
    """Cancelling the second thing you typed used to take the turn that was
    running away from you: `remove` releases the folder, and a queued follow-up
    is filed under the very task that owns it."""
    m = idle_world().manager()
    m.started(F1, "a", "run-a", "sess-a")
    m.enqueue(F1, "b", "e-b")
    m.enqueue(F1, "c", "e-c")
    m.forget_entry("e-b")
    assert owner_key(m) == "a"
    assert line_of(m) == ["c"]


def test_forget_entry_frees_the_folder_for_the_next_message():
    world = World()
    m = world.manager()
    m.started(F1, "owner")
    m.enqueue(F1, "b", "e-b")
    m.enqueue(F1, "c", "e-c")
    world.spawned.clear()
    m.forget_entry("e-b")
    assert line_of(m) == ["c"]
    m.turn_ended("owner")
    assert owner_key(m) == "c"


def test_forget_entry_drops_the_answers_of_a_task_it_orphaned():
    """The message is gone and it was the only place that task stood: a verdict
    left behind would re-own a folder the day that key came round again."""
    m = World().manager()
    m.enqueue(F1, "a", "e-a")                # a owns, under entry e-a
    m.enqueue(F1, "b")
    m.card_raised("a", "run")                # b owns, a blocked
    m.card_answered("a", "run-a", "req-1", {})
    assert m.held_answer("a") is not None

    m.forget_entry("e-a")
    assert line_of(m) == []
    assert m.held_answer("a") is None
    assert owner_key(m) == "b"


def test_forget_entry_ignores_a_blank_id():
    m = World().manager()
    m.enqueue(F1, "a")
    before = m.snapshot()
    m.forget_entry("")
    assert m.snapshot() == before


# ------------------------------------------------- reconcile and identity


def test_reconcile_keeps_a_pending_owner_the_registry_knows_by_its_session():
    """The bug: every scheduled message the pump starts owns its folder under
    `pending:<entry id>`, which has no session and no registry row — so the
    status read answered "dead" and the index dropped a LIVE owner on the very
    next tick."""
    world = World(spawns={"pending:e1": {"run_id": "run-1",
                                         "session_id": "sess-1"}},
                  due=[(F1, "pending:e1", "e1")])
    m = world.manager()
    assert owner_key(m) == "pending:e1"
    world.due = []
    world.running_keys.add("sess-1")         # the registry knows the SESSION
    world.now += 60
    m.reconcile()
    assert owner_key(m) == "pending:e1"


def test_reconcile_never_pops_a_spawn_that_has_not_landed_yet():
    """An owner whose spawn has not come back with a run has nothing the status
    sync can be asked about — no run dir, no registry row, no mark — so every
    channel says "not running" about a process that is starting."""
    world = idle_world()
    m = world.manager()
    m.started(F1, "a")                       # no run id yet
    m.reconcile()
    assert owner_key(m) == "a"

    world.now += qm.SPAWN_GRACE + 5
    m.reconcile()
    assert owner_key(m) is None


def test_reconcile_never_pops_an_in_flight_spawn_past_the_grace():
    """Bugbot, PR #1194: `SPAWN_GRACE` is 10s, but `dispatch_entry`/`_send` can
    block up to 60s (the subprocess timeout). A reconcile that only looked at
    age popped a `starting` owner mid-spawn and started a SECOND task beside
    it. An owner whose spawn is known to still be in flight is never popped
    for age, however old it gets — only once the spawn actually lands does
    `reconcile` fall back to consulting `running` as usual."""
    world = World()
    inside = threading.Event()
    release = threading.Event()

    def slow(folder, key):
        inside.set()
        assert release.wait(5)
        return {"run_id": "run-" + key, "session_id": "sess-" + key}

    world.spawn = slow
    m = world.manager()
    thread = threading.Thread(target=m.enqueue, args=(F1, "slow"))
    thread.start()
    assert inside.wait(5)                    # the spawn is in flight

    world.now += qm.SPAWN_GRACE + 5          # far older than the grace
    m.reconcile()
    assert owner_key(m) == "slow"            # not popped: the spawn is still going
    assert line_of(m) == []

    release.set()
    thread.join(5)
    assert owner_key(m) == "slow"
    assert m.owner(F1)["run_id"] == "run-slow"

    world.running_keys.discard("run-slow")   # landed: the status sync decides now
    m.reconcile()
    assert owner_key(m) is None


def test_reconcile_trusts_the_status_sync_once_a_spawn_has_a_run():
    """With a run id there IS something to ask about — the run dir and the pid
    in it — so the grace would only delay the truth by ten seconds."""
    world = idle_world()
    m = world.manager()
    m.started(F1, "a", "run-a", "sess-a")
    m.reconcile()
    assert owner_key(m) is None


def test_reconcile_asks_about_a_blocked_item_by_its_run():
    world = idle_world()
    m = world.manager()
    m.started(F1, "pending:e1", "run-7", "sess-7")
    m.card_raised("sess-7", "run-7")
    world.blocked_keys.add("run-7")          # the CARDS are the run's
    world.now += 60
    m.reconcile()
    assert blocked_of(m) == ["sess-7"]


def test_reconcile_prunes_an_answer_nothing_can_deliver():
    """A verdict whose task the index no longer points at anywhere, and whose
    conversation nothing can find, would sit in the file for ever and re-own a
    folder the day that key came round again."""
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a", "run")
    m.card_answered("a", "run-a", "req-1", {})
    assert m.held_answer("a") is not None

    m._state["folders"][F1]["line"] = []     # the world forgot it
    world.now += 60
    m.reconcile()
    assert m.held_answer("a") is None


def test_reconcile_keeps_an_answer_whose_task_is_still_live():
    world = World()
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a", "run")
    m.card_answered("a", "run-a", "req-1", {})
    world.running_keys.update({"a", "b"})
    world.now += 60
    m.reconcile()
    assert m.held_answer("a") is not None


def test_a_legacy_answer_for_a_dead_run_is_not_migrated(state):
    """The old store outlived the runs it was about. An upgrade on a machine
    that has been off for a week must not re-own a folder for a process that
    died days ago."""
    _legacy_file(state, _legacy("sess-gone"), _legacy("sess-live", at=2.0))
    world = idle_world()
    world.running_keys.add("sess-live")
    m = loaded(world)
    assert m.held_answer("sess-gone") is None
    assert m.held_answer("sess-live") is not None
    assert line_of(m) == ["sess-live"]


# ------------------------------------------------------- the lock and the spawn


def test_an_event_never_waits_on_a_spawn():
    """Starting a turn talks to another process. Holding the queue lock across
    it made every event that arrived in that window — a card going up in another
    folder, a turn ending in a third — wait on a spawn it had nothing to do
    with. The pump DECIDES under the lock and does the work outside it."""
    world = World()
    inside = threading.Event()
    release = threading.Event()

    def slow(folder, key):
        inside.set()
        assert release.wait(5)
        return {"run_id": "run-" + key, "session_id": "sess-" + key}

    world.spawn = slow
    m = world.manager()
    m.started(F2, "other", "run-other", "sess-other")

    thread = threading.Thread(target=m.enqueue, args=(F1, "slow"))
    thread.start()
    assert inside.wait(5)                    # the spawn is in flight

    began = time.monotonic()
    m.card_raised("other", "run-other")      # another folder, the same lock
    m.turn_ended("nobody")
    assert time.monotonic() - began < 1.0
    assert blocked_of(m, F2) == ["other"]

    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert owner_key(m) == "slow"
    assert m.owner(F1)["run_id"] == "run-slow"


def test_a_turn_that_ends_mid_spawn_leaves_the_folder_to_whoever_has_it():
    """The world moves on while the lock is down; a job that comes back to a
    folder somebody else owns has nothing to patch and must not steal it."""
    world = World()
    inside = threading.Event()
    release = threading.Event()

    def slow(folder, key):
        inside.set()
        assert release.wait(5)
        return {"run_id": "run-" + key, "session_id": "sess-" + key}

    world.spawn = slow
    m = world.manager()
    thread = threading.Thread(target=m.enqueue, args=(F1, "slow"))
    thread.start()
    assert inside.wait(5)
    m.started(F1, "barged-in", "run-b", "sess-b")
    release.set()
    thread.join(5)
    assert owner_key(m) == "barged-in"


# ------------------------------------------------------------- the singleton


def test_get_without_a_factory_explains_itself():
    with pytest.raises(RuntimeError, match="set_factory"):
        qm.get()


def test_get_builds_once_from_the_factory():
    world = idle_world()
    built = []

    def factory():
        built.append(1)
        return world.manager()

    qm.set_factory(factory)
    first = qm.get()
    assert qm.get() is first
    assert built == [1]


def test_reset_for_tests_installs_a_manager():
    m = idle_world().manager()
    qm.reset_for_tests(m)
    assert qm.get() is m


def test_the_real_spawn_keeps_the_placeholders_other_claim_tokens():
    """Bugbot PR #1194: renaming an `admit:` placeholder to the spawned run
    used to write a fresh owner with no `claims`, so a SECOND send absorbed
    into the same placeholder (`claim_for_send` called again before the first
    spawn returns) found its own token gone the moment the run gate presented
    it — and the gate counted that send a second time.

    One of the two tokens is consumed first: the guard `started` now checks
    (Bugbot PR #1194, third round) needs proof THIS call is the admitted
    send, not a stranger's. The OTHER token belongs to the absorbed
    follow-up and must survive the rename untouched, same as `turns`."""
    manager = idle_world().manager()
    _ok, _took, first = manager.claim_for_send(F1, qm.PLACEHOLDER_PREFIX + "abc")
    _ok2, _took2, second = manager.claim_for_send(F1, qm.PLACEHOLDER_PREFIX + "abc")
    assert manager.owner(F1)["turns"] == 2
    assert manager.consume_claim(F1, first) is True
    manager.started(F1, "sess-1", run_id="run-1", session_id="sess-1")
    owner = manager.owner(F1)
    assert owner["task"] == "sess-1"
    assert owner["turns"] == 2
    assert manager.consume_claim(F1, second) is True
    assert manager.consume_claim(F1, second) is False


def test_restore_claim_puts_a_spent_token_back_once():
    """Bugbot PR #1194 (eighth round): the run gate gives a token back when
    the send it was spent on did not stick, so the fallback start reads as the
    admitted send. Only an owner that consumed a claim takes one back."""
    m = idle_world().manager()
    _ok, _took, token = m.claim_for_send(F1, "a", "run-a", "sess-a")
    assert m.restore_claim(F1, token) is False, "nothing consumed yet"
    assert m.consume_claim(F1, token) is True
    assert m.restore_claim(F1, token) is True
    assert m.restore_claim(F1, token) is True, "idempotent"
    assert m.owner(F1)["claims"].count(token) == 1
    assert m.consume_claim(F1, token) is True
    assert m.consume_claim(F1, token) is False
    assert m.restore_claim(F1, "") is False
    assert m.restore_claim("/nowhere", token) is False


def test_restore_claim_refuses_a_token_for_a_conversation_the_owner_is_not():
    """Review, 2026-09-18: `consumed` is stamped by ANY consume, so owner B
    consuming its own token must not let owner A's spent token land on B."""
    m = idle_world().manager()
    _ok, _took, t_a = m.claim_for_send(F1, "a", "run-a", "sess-a")
    m.consume_claim(F1, t_a)
    m.exited("a", "run-a")
    _ok, _took, t_b = m.claim_for_send(F1, "b", "run-b", "sess-b")
    m.consume_claim(F1, t_b)
    assert m.restore_claim(F1, t_a, run_id="run-a", session_id="sess-a") is False
    assert m.restore_claim(F1, t_b, run_id="run-b") is True
    assert m.restore_claim(F1, t_b, session_id="sess-b") is True
    assert m.owner(F1)["claims"] == [t_b]


def test_restore_claim_refuses_a_folder_whose_owner_changed():
    m = idle_world().manager()
    _ok, _took, token = m.claim_for_send(F1, "a", "run-a", "sess-a")
    m.consume_claim(F1, token)
    m.exited("a", "run-a")
    m.claim_took(F1, "b", "run-b", "sess-b")
    assert m.restore_claim(F1, token) is False
    assert m.consume_claim(F1, token) is False

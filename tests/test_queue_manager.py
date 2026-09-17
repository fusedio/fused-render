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

    def running(self, key):
        return key in self.running_keys

    def blocked(self, key):
        return key in self.blocked_keys

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
    assert place == {"position": 0, "ahead_key": ""}
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

    assert m.enqueue(F1, "b") == {"position": 0, "ahead_key": ""}    # the owner
    assert m.enqueue(F1, "c") == {"position": 1, "ahead_key": "b"}   # in line
    assert m.enqueue(F1, "a") == {"position": 0, "ahead_key": ""}    # blocked
    assert line_of(m) == ["c"]
    assert blocked_of(m) == ["a"]


def test_enqueue_ignores_a_blank_folder_or_key():
    m = World().manager()
    assert m.enqueue("", "a") == {"position": 0, "ahead_key": ""}
    assert m.enqueue(F1, "") == {"position": 0, "ahead_key": ""}
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
    assert m.skip("a") == {"position": 1, "ahead_key": "b"}
    assert blocked_of(m) == []
    assert line_of(m) == ["a"]


def test_skip_of_the_owner_and_of_a_stranger_are_no_ops():
    m = World().manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    assert m.skip("a") == {"position": 0, "ahead_key": ""}
    assert m.skip("nobody") == {"position": 0, "ahead_key": ""}
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


# --------------------------------------------------------------- card_raised


def test_card_raised_parks_the_owner_and_hands_the_folder_on():
    world = World()
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


def test_card_answered_with_a_free_folder_is_not_held():
    m = World().manager()
    m.enqueue(F1, "a")
    m.card_raised("a")                       # nothing behind it: the folder is free
    assert owner_key(m) is None
    assert m.card_answered("a", "run-a", "req", {"ok": True}) == {"held": False, "position": 0}
    assert m.held_answer("a") is None


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
    assert m.place("a") == {"position": 2, "ahead_key": "c"}


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


def test_a_raising_deliver_still_leaves_the_folder_owned():
    world = World()

    def boom(answer):
        world.delivered.append(answer)
        raise RuntimeError("the run went away")

    world.deliver = boom
    m = world.manager()
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    m.card_raised("a")
    m.card_answered("a", "run-a", "req", {})
    m.turn_ended("b")
    assert owner_key(m) == "a"
    assert len(world.delivered) == 1


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
        "b": {"key": "b", "position": 1, "ahead_key": "a", "priority": False},
        "c": {"key": "c", "position": 2, "ahead_key": "b", "priority": False},
    }
    m.skip("c")
    assert m.positions() == {
        "c": {"key": "c", "position": 1, "ahead_key": "a", "priority": True},
        "b": {"key": "b", "position": 2, "ahead_key": "c", "priority": False},
    }


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
    assert m.positions()["b"] == {"key": "b", "position": 1, "ahead_key": "",
                                  "priority": False}


def test_place_and_is_free_and_owner_reads():
    m = World().manager()
    assert m.is_free(F1) is True
    assert m.owner(F1) is None
    m.enqueue(F1, "a")
    m.enqueue(F1, "b")
    assert m.is_free(F1) is False
    assert m.is_free(F1, "a") is True
    assert m.is_free(F1, "b") is False
    assert m.place("a") == {"position": 0, "ahead_key": ""}
    assert m.place("b") == {"position": 1, "ahead_key": "a"}
    assert m.place("nobody") == {"position": 0, "ahead_key": ""}


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

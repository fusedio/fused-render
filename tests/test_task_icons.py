"""Task icons (`task_icons_enabled`, 2026-11-10): the slot a task wears is
arithmetic on its number, unique within a project, stable under deletion,
and a reroll persists an override no neighbour wears."""
import pytest

from fused_render import tasks_store


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(tmp_path))
    return tmp_path


P = "/Users/me/proj"
Q = "/Users/me/other"


def test_first_299_numbers_of_a_project_wear_299_different_faces():
    slots = [tasks_store.icon_slot(tasks_store.format_task_id(n), P) for n in range(1, 300)]
    assert len(set(slots)) == tasks_store.ICON_COUNT == 299
    assert all(0 <= s < 299 for s in slots)
    # The 300th wraps onto the first's face: the set is spent, it repeats.
    assert tasks_store.icon_slot("TASK-300", P) == slots[0]


def test_two_projects_start_at_different_places():
    assert tasks_store.icon_slot("TASK-001", P) != tasks_store.icon_slot("TASK-001", Q)


def test_no_number_no_slot():
    assert tasks_store.icon_slot("", P) is None
    assert tasks_store.icon_slot("TASK-", P) is None


def test_deleting_a_task_moves_nobody_elses_face(state):
    items = [(f"s{i}", P, float(i)) for i in range(1, 6)]
    numbers = tasks_store.ensure_ids(items)
    before = {k: tasks_store.icon_slot(numbers[k], P) for k, _, _ in items}
    # s3 goes away; the next listing numbers a newcomer from "max plus one".
    rest = [it for it in items if it[0] != "s3"] + [("s6", P, 6.0)]
    numbers2 = tasks_store.ensure_ids(rest)
    after = {k: tasks_store.icon_slot(numbers2[k], P) for k, _, _ in rest}
    for k in ("s1", "s2", "s4", "s5"):
        assert after[k] == before[k]
    # ...and the newcomer wears a face none of the survivors do.
    assert after["s6"] not in {after[k] for k in ("s1", "s2", "s4", "s5")}


def test_reroll_persists_and_avoids_every_face_in_the_project(state):
    items = [(f"s{i}", P, float(i)) for i in range(1, 4)] + [("t1", Q, 9.0)]
    numbers = tasks_store.ensure_ids(items)
    worn_by_others = {tasks_store.icon_slot(numbers[k], P) for k in ("s2", "s3")}
    old = tasks_store.icon_slot(numbers["s1"], P)
    for _ in range(12):
        new = tasks_store.reroll_icon("s1")
        assert new is not None and new != old
        assert new not in worn_by_others
        # Persisted: the store reads it back, and the listing's helpers honour it.
        rec = tasks_store.task_ids()["s1"]
        assert rec["icon_slot"] == new
        assert tasks_store.icon_overrides() == {"s1": new}
        assert tasks_store.icon_slot(numbers["s1"], P, new) == new
        old = new
    # A rekey carries the override with the number.
    tasks_store.ensure_ids([("sess", P, 1.0)], rekeys=(("s1", "sess"),))
    assert tasks_store.task_ids()["sess"]["icon_slot"] == old
    assert "s1" not in tasks_store.icon_overrides()


def test_reroll_of_an_unnumbered_task_is_refused(state):
    assert tasks_store.reroll_icon("nobody") is None


def test_a_hand_edited_slot_out_of_range_is_dropped(state):
    tasks_store.ensure_ids([("s1", P, 1.0)])
    tasks_store._update(tasks_store.TASK_IDS_FILE, lambda store: (store["s1"].__setitem__("icon_slot", 999), True))
    assert "icon_slot" not in tasks_store.task_ids()["s1"]
    assert tasks_store.icon_overrides() == {}


def test_reroll_counts_only_live_tasks_as_wearing_a_face(state):
    """A deleted task keeps its number record (so the number is never
    recycled) but its face is free again: with `live` given, only listed
    keys' slots are avoided, and a long-lived project cannot run dry."""
    items = [(f"s{i}", P, float(i)) for i in range(1, 300)]
    numbers = tasks_store.ensure_ids(items)
    # Every slot is worn by SOME record...
    assert len({tasks_store.icon_slot(numbers[k], P) for k, _, _ in items}) == 299
    # ...but only s1 and s2 still exist. A reroll of s1 must land on a face
    # neither s1 nor s2 wears, not on "anything but current".
    worn_live = {tasks_store.icon_slot(numbers["s2"], P)}
    current = tasks_store.icon_slot(numbers["s1"], P)
    for _ in range(20):
        new = tasks_store.reroll_icon("s1", live={"s1", "s2"})
        assert new != current and new not in worn_live
        current = new
    # Without `live`, every unspent record counts and the project reads full:
    # the fallback still never answers the current face.
    assert tasks_store.reroll_icon("s1") != current


def test_reroll_ignores_erased_tombstones(state):
    tasks_store.ensure_ids([("s1", P, 1.0), ("s2", P, 2.0)])
    s2_face = tasks_store.icon_slot_for(tasks_store.task_ids()["s2"]["n"], P)
    tasks_store._update(tasks_store.TASK_IDS_FILE, lambda store: (store["s2"].__setitem__("erased", True), True))
    # s2's face is free now: with only 2 numbers allocated the project is far
    # from full, so prove it the direct way — s2's slot is pickable.
    seen = {tasks_store.reroll_icon("s1") for _ in range(400)}
    assert s2_face in seen

"""Task icons (`task_icons_enabled`, 2026-11-10): the slot a task wears is
arithmetic on its number, unique within a project, stable under deletion;
a pick persists an override; and the listing hands a face out only to a
FAVOURITE (2026-11-11)."""
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


def test_a_pick_persists_and_reset_gives_the_numbers_face_back(state):
    numbers = tasks_store.ensure_ids([("s1", P, 1.0), ("s2", P, 2.0)])
    own = tasks_store.icon_slot(numbers["s1"], P)
    picked = (own + 7) % tasks_store.ICON_COUNT
    assert tasks_store.set_icon("s1", picked) == picked
    # Persisted: the store reads it back, and the listing's helpers honour it.
    assert tasks_store.task_ids()["s1"]["icon_slot"] == picked
    assert tasks_store.icon_overrides() == {"s1": picked}
    assert tasks_store.icon_slots()["s1"] == picked
    # A neighbour's face may be picked too: the choice is the user's.
    theirs = tasks_store.icon_slot(numbers["s2"], P)
    assert tasks_store.set_icon("s1", theirs) == theirs
    # A rekey carries the override with the number.
    tasks_store.ensure_ids([("sess", P, 1.0)], rekeys=(("s1", "sess"),))
    assert tasks_store.task_ids()["sess"]["icon_slot"] == theirs
    assert "s1" not in tasks_store.icon_overrides()
    # Reset: the override goes, the number's own face is back.
    assert tasks_store.set_icon("sess", None) == own
    assert tasks_store.icon_overrides() == {}
    assert tasks_store.icon_slots() == {"sess": own, "s2": theirs}


def test_a_pick_for_an_unnumbered_task_is_refused(state):
    assert tasks_store.set_icon("nobody", 3) is None
    assert tasks_store.set_icon("nobody", None) is None


def test_a_hand_edited_slot_out_of_range_is_dropped(state):
    tasks_store.ensure_ids([("s1", P, 1.0)])
    tasks_store._update(tasks_store.TASK_IDS_FILE, lambda store: (store["s1"].__setitem__("icon_slot", 999), True))
    assert "icon_slot" not in tasks_store.task_ids()["s1"]
    assert tasks_store.icon_overrides() == {}


def test_the_face_follows_the_number_record_not_the_rows_project_spelling(state):
    """A listing that later spells a task's project differently (a rekeyed
    `pending:` row, a cwd read from the transcript) must not move its face:
    `icon_slots` computes from the record's own project."""
    tasks_store.ensure_ids([("s1", "/Users/me/proj", 1.0)])
    slots = tasks_store.icon_slots()
    assert slots == {"s1": tasks_store.icon_slot_for(1, "/Users/me/proj")}
    # ...and NOT what the other spelling would give.
    # A rekey keeps the face on the new key and drops the spent old one.
    tasks_store.ensure_ids([("sess", "/somewhere/else", 2.0)], rekeys=(("s1", "sess"),))
    assert tasks_store.icon_slots() == {"sess": slots["s1"]}
    # A pick shows through.
    assert tasks_store.set_icon("sess", 42) == 42
    assert tasks_store.icon_slots() == {"sess": 42}


# ------------------------------------------------- only a favourite wears one


def test_the_listing_hands_a_face_only_to_a_starred_task(state):
    from fused_render.server.routers import tasks as tasks_router
    numbers = tasks_store.ensure_ids([("s1", P, 1.0), ("s2", P, 2.0)])
    assert tasks_router._icon_slots() == {}
    # Starred: its face, and nobody else's.
    tasks_store.set_favourite("s1", True)
    own = tasks_store.icon_slot(numbers["s1"], P)
    assert tasks_router._icon_slots() == {"s1": own}
    # A pick made while starred shows; it survives the star going and coming.
    tasks_store.set_icon("s1", 42)
    assert tasks_router._icon_slots() == {"s1": 42}
    tasks_store.set_favourite("s1", False)
    assert tasks_router._icon_slots() == {}
    tasks_store.set_favourite("s1", True)
    assert tasks_router._icon_slots() == {"s1": 42}


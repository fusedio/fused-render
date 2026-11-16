"""Task icons (`task_icons_enabled`, 2026-11-10): the slot a task wears is
arithmetic on its number, unique within a project, stable under deletion;
a pick persists an override; and — the icon and the star being two things
since 2026-11-16 — a task wears a face when it was picked by hand, or while
starred unless it was removed by hand (`tasks_store.worn_icons`)."""
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


def test_a_pick_persists_and_rides_a_rekey(state):
    numbers = tasks_store.ensure_ids([("s1", P, 1.0), ("s2", P, 2.0)])
    own = tasks_store.icon_slot(numbers["s1"], P)
    picked = (own + 7) % tasks_store.ICON_COUNT
    assert tasks_store.set_icon("s1", picked) == {"icon_slot": picked, "picked": True}
    # Persisted with the user's word: the store reads both back.
    assert tasks_store.task_ids()["s1"]["icon_slot"] == picked
    assert tasks_store.task_ids()["s1"]["icon"] == "on"
    assert tasks_store.icon_overrides() == {"s1": picked}
    assert tasks_store.icon_slots()["s1"] == picked
    # A neighbour's face may be picked too: the choice is the user's.
    theirs = tasks_store.icon_slot(numbers["s2"], P)
    assert tasks_store.set_icon("s1", theirs)["icon_slot"] == theirs
    # A rekey carries the pick AND the word with the number.
    tasks_store.ensure_ids([("sess", P, 1.0)], rekeys=(("s1", "sess"),))
    assert tasks_store.task_ids()["sess"]["icon_slot"] == theirs
    assert tasks_store.task_ids()["sess"]["icon"] == "on"
    assert tasks_store.worn_icons(set()) == {"sess": (theirs, True)}
    assert "s1" not in tasks_store.icon_overrides()


def test_remove_drops_the_pick_and_says_off(state):
    numbers = tasks_store.ensure_ids([("s1", P, 1.0)])
    own = tasks_store.icon_slot(numbers["s1"], P)
    tasks_store.set_icon("s1", 42)
    # Remove: the task wears nothing now, and the record says so.
    assert tasks_store.set_icon("s1", None) == {"icon_slot": None, "picked": False}
    rec = tasks_store.task_ids()["s1"]
    assert rec["icon"] == "off" and "icon_slot" not in rec
    assert tasks_store.icon_overrides() == {}
    # The face it HAS is the number's own again (what a star brings back).
    assert tasks_store.icon_slots() == {"s1": own}
    # The off word rides a rekey too: a removed icon does not come back on a
    # pending task's first run.
    tasks_store.ensure_ids([("sess", P, 1.0)], rekeys=(("s1", "sess"),))
    assert tasks_store.task_ids()["sess"]["icon"] == "off"
    assert tasks_store.worn_icons({"sess"}) == {}


def test_a_drafts_icon_survives_its_renumbering(state):
    """A draft re-minted in another project (`reproject`) keeps the user's
    word and pick: a new number is bookkeeping, not taking the pick back."""
    tasks_store.ensure_ids([("draft:d1", P, 1.0)], reproject=True)
    tasks_store.set_icon("draft:d1", 42)
    tasks_store.ensure_ids([("draft:d1", Q, 1.0)], reproject=True)
    rec = tasks_store.task_ids()["draft:d1"]
    assert rec["project"] == Q
    assert (rec["icon_slot"], rec["icon"]) == (42, "on")


def test_an_unknown_icon_word_reads_as_follow_the_star(state):
    tasks_store.ensure_ids([("s1", P, 1.0)])
    tasks_store._update(tasks_store.TASK_IDS_FILE,
                        lambda store: (store["s1"].__setitem__("icon", "maybe"), True))
    assert "icon" not in tasks_store.task_ids()["s1"]
    assert tasks_store.worn_icons(set()) == {}
    assert set(tasks_store.worn_icons({"s1"})) == {"s1"}


def test_a_pick_for_an_unnumbered_task_is_refused(state):
    assert tasks_store.set_icon("nobody", 3) is None
    assert tasks_store.set_icon("nobody", None) is None
    assert tasks_store.clear_icon_off("nobody") is False


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
    assert tasks_store.set_icon("sess", 42)["icon_slot"] == 42
    assert tasks_store.icon_slots() == {"sess": 42}


# ------------------------------------- the icon and the star are two things


def _faces():
    from fused_render.server.routers import tasks as tasks_router
    return tasks_router._icon_faces()


def _star(key, on=True):
    """What the star route does around the store (`api_task_favourite`):
    a task that BECOMES starred gets a removed icon back."""
    was = tasks_store.favourite_of(tasks_store.favourites_state(), key)[0]
    tasks_store.set_favourite(key, on)
    if on and not was:
        tasks_store.clear_icon_off(key)


def test_an_untouched_task_follows_the_star(state):
    """Starred, untouched: the number's face (auto-assigned, as before).
    Not starred, untouched: the ring. Unstar takes the auto face away."""
    numbers = tasks_store.ensure_ids([("s1", P, 1.0), ("s2", P, 2.0)])
    own = tasks_store.icon_slot(numbers["s1"], P)
    assert _faces() == {}
    _star("s1")
    assert _faces() == {"s1": (own, False)}
    _star("s1", False)
    assert _faces() == {}


def test_a_picked_icon_is_worn_starred_or_not_and_stays_on_unstar(state):
    tasks_store.ensure_ids([("s1", P, 1.0)])
    tasks_store.set_icon("s1", 42)
    assert _faces() == {"s1": (42, True)}
    _star("s1")
    assert _faces() == {"s1": (42, True)}
    # Owner, 2026-11-16: an icon picked by hand stays on the unstarred task.
    _star("s1", False)
    assert _faces() == {"s1": (42, True)}


def test_a_favourite_can_wear_the_ring_and_a_star_gives_the_icon_back(state):
    numbers = tasks_store.ensure_ids([("s1", P, 1.0)])
    own = tasks_store.icon_slot(numbers["s1"], P)
    _star("s1")
    tasks_store.set_icon("s1", None)
    # Removed while starred: the ring, the star stands.
    assert tasks_store.favourite_of(tasks_store.favourites_state(), "s1")[0]
    assert _faces() == {}
    # Unstar and star again: the icon (the number's own) comes back.
    _star("s1", False)
    assert _faces() == {}
    _star("s1")
    assert _faces() == {"s1": (own, False)}
    assert "icon" not in tasks_store.task_ids()["s1"]


def test_a_star_does_not_undo_a_pick(state):
    tasks_store.ensure_ids([("s1", P, 1.0)])
    tasks_store.set_icon("s1", 42)
    assert tasks_store.clear_icon_off("s1") is False
    assert tasks_store.task_ids()["s1"]["icon"] == "on"


def test_legacy_pick_without_a_word_keeps_todays_faces(state):
    """No migration (owner, 2026-11-16): a record written before the word
    existed — a slot, no `icon` — stays the ring unstarred and shows that
    slot starred, exactly as it did."""
    tasks_store.ensure_ids([("s1", P, 1.0), ("s2", P, 2.0)])
    tasks_store._update(tasks_store.TASK_IDS_FILE,
                        lambda store: (store["s1"].__setitem__("icon_slot", 42), True))
    assert _faces() == {}
    tasks_store.set_favourite("s1", True)
    assert _faces() == {"s1": (42, False)}
    tasks_store.set_favourite("s1", False)
    assert _faces() == {}

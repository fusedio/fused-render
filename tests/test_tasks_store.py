"""Task numbers and unread marks (fused_render/tasks_store.py).

The two global stores behind the Tasks page. What is tested here is the part
that has to survive being wrong exactly once: a task number the user has seen
must never change, and a message they did not read must never be marked read.
"""
import json
import os
import threading

import pytest

from fused_render import tasks_store
from tests import _machinery_records as records


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "fused-render-home" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    return d


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(tasks_store, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clear_head_cache():
    tasks_store.reset_cache()
    yield
    tasks_store.reset_cache()


def _transcript(projects_dir, encoded, session_id, cwd, first_ts, prompt="hi"):
    d = projects_dir / encoded
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text(json.dumps({
        "type": "user", "cwd": cwd, "timestamp": first_ts, "uuid": "u1",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    }) + "\n")
    return path


def _store(state_dir):
    return json.loads((state_dir / "task_ids.json").read_text())


# ----------------------------------------------------------------- numbering


def test_numbers_restart_at_one_in_every_project():
    """TASK-001 is a per-project name, not a global one — each folder counts
    from the start, which is what makes the number short enough to say out
    loud."""
    ids = tasks_store.ensure_ids([
        ("s1", "/home/a", 10.0),
        ("s2", "/home/a", 20.0),
        ("s3", "/home/b", 30.0),
    ])
    assert ids == {"s1": "TASK-001", "s2": "TASK-002", "s3": "TASK-001"}


def test_numbers_are_zero_padded_to_three_and_then_simply_grow(state_dir):
    """Padding lines a column up; it is not a limit."""
    (state_dir / "task_ids.json").write_text(
        json.dumps({"old": {"project": "/p", "n": 999}}))
    assert tasks_store.ensure_ids([("new", "/p", 1.0)]) == {"new": "TASK-1000"}


def test_a_number_is_allocated_once_and_survives_a_deletion(state_dir):
    """Deleting TASK-001 must not promote TASK-002. The rule is "max seen plus
    one", so a freed number is never handed out again."""
    tasks_store.ensure_ids([("s1", "/p", 1.0), ("s2", "/p", 2.0)])

    store = _store(state_dir)
    del store["s1"]  # the user deleted TASK-001
    (state_dir / "task_ids.json").write_text(json.dumps(store))

    again = tasks_store.ensure_ids([("s2", "/p", 2.0), ("s3", "/p", 3.0)])
    assert again["s2"] == "TASK-002", "an existing number was renumbered"
    assert again["s3"] == "TASK-003", "a deleted number was recycled"


def test_allocation_order_is_oldest_session_first():
    """A backfill numbers a project's history in the order it happened, not in
    whatever order the filesystem listed it."""
    ids = tasks_store.ensure_ids([
        ("newest", "/p", 300.0),
        ("oldest", "/p", 100.0),
        ("middle", "/p", 200.0),
    ])
    assert ids == {"oldest": "TASK-001", "middle": "TASK-002",
                   "newest": "TASK-003"}


def test_ensure_ids_is_idempotent():
    first = tasks_store.ensure_ids([("s1", "/p", 1.0), ("s2", "/p", 2.0)])
    second = tasks_store.ensure_ids([("s2", "/p", 2.0), ("s1", "/p", 1.0)])
    assert first == second


def test_an_unreadable_record_does_not_take_a_number_with_it(state_dir):
    """A corrupt record reads as "no number", and the key is re-allocated —
    never inherited as a zero that collides with the next allocation."""
    (state_dir / "task_ids.json").write_text(
        json.dumps({"s1": "nonsense", "s2": {"project": "/p", "n": "x"}}))
    ids = tasks_store.ensure_ids([("s1", "/p", 1.0), ("s2", "/p", 2.0)])
    assert ids == {"s1": "TASK-001", "s2": "TASK-002"}


def test_a_missing_store_is_not_an_error():
    assert tasks_store.task_ids() == {}
    assert tasks_store.task_number("nobody") == ""


# --------------------------------------------------------------------- rekey


def test_rekey_moves_the_number_onto_the_session_id(state_dir):
    """§5: a task exists before its session does. When the first run mints a
    session id the number FOLLOWS it — the row the user has been watching does
    not renumber the moment it finally runs."""
    pending = tasks_store.pending_key("20260816-090000-abc")
    tasks_store.ensure_ids([("other", "/p", 1.0), (pending, "/p", 2.0)])
    assert tasks_store.task_number(pending) == "TASK-002"

    assert tasks_store.rekey(pending, "session-xyz") == "TASK-002"
    assert tasks_store.task_number("session-xyz") == "TASK-002"
    assert tasks_store.task_number(pending) == ""
    assert pending not in _store(state_dir)


def test_rekey_does_not_renumber_a_session_that_already_has_one():
    """Two occurrences of one recurring message can chain into the same
    session. The first transfers; the second's number is spent, because reusing
    it is the one thing allocate-once forbids."""
    tasks_store.ensure_ids([("session", "/p", 1.0)])
    pending = tasks_store.pending_key("second")
    tasks_store.ensure_ids([(pending, "/p", 2.0)])

    assert tasks_store.rekey(pending, "session") == "TASK-001"
    # The spent number stays spent: its record is left where it is, unread by
    # anything, precisely so the next allocation cannot hand it out again.
    assert tasks_store.task_number(pending) == "TASK-002"
    assert tasks_store.ensure_ids([("later", "/p", 9.0)])["later"] == "TASK-003"


def test_rekey_of_something_that_has_no_number_does_nothing():
    assert tasks_store.rekey("nothing", "nowhere") == ""
    assert tasks_store.task_ids() == {}


# ------------------------------------------------------------------- backfill


def test_backfill_numbers_every_session_oldest_first_per_project(projects_dir):
    _transcript(projects_dir, "-home-a", "s-late", "/home/a", "2026-08-16T12:00:00Z")
    _transcript(projects_dir, "-home-a", "s-early", "/home/a", "2026-08-16T09:00:00Z")
    _transcript(projects_dir, "-home-b", "s-other", "/home/b", "2026-08-16T11:00:00Z")

    ids = tasks_store.backfill()
    assert ids == {"s-early": "TASK-001", "s-late": "TASK-002",
                   "s-other": "TASK-001"}


def test_backfill_is_idempotent_and_only_picks_up_what_is_new(projects_dir,
                                                              state_dir):
    _transcript(projects_dir, "-home-a", "s1", "/home/a", "2026-08-16T09:00:00Z")
    first = tasks_store.backfill()
    before = _store(state_dir)

    # A session that started EARLIER shows up late (it was on another machine,
    # or the folder was only just readable). It must not renumber s1.
    _transcript(projects_dir, "-home-a", "s0", "/home/a", "2026-08-16T01:00:00Z")
    second = tasks_store.backfill()

    assert second["s1"] == first["s1"] == "TASK-001"
    assert second["s0"] == "TASK-002"
    assert _store(state_dir)["s1"] == before["s1"]

    # And a third run changes nothing at all.
    assert tasks_store.backfill() == second


def test_backfill_skips_a_transcript_with_no_readable_cwd(projects_dir):
    """Filing them all under "" would pool every unreadable session into one
    nameless project and take numbers there."""
    d = projects_dir / "-home-a"
    d.mkdir(parents=True)
    (d / "broken.jsonl").write_text("{not json\n")
    assert tasks_store.backfill() == {}


# ------------------------------------------------------------- message ids


def test_message_ids_are_the_position_in_the_thread():
    assert tasks_store.message_ids(3) == ["MSG-001", "MSG-002", "MSG-003"]
    assert tasks_store.message_ids(0) == []
    assert tasks_store.format_message_id(12) == "MSG-012"


def test_message_number_reads_back_and_refuses_nonsense():
    assert tasks_store.message_number("MSG-012") == 12
    assert tasks_store.message_number("MSG-1000") == 1000
    for junk in ("", "12", "TASK-001", "MSG-x", None, "MSG-"):
        assert tasks_store.message_number(junk) == 0


# ---------------------------------------------------------------- the unread


def test_marking_one_message_read_leaves_an_older_one_unread():
    """The whole reason the record is a SET. A watermark would swallow MSG-002,
    which is a notification lost silently — the one failure unread exists to
    prevent."""
    tasks_store.mark_read("t", "MSG-003")
    state = tasks_store.read_state()
    assert tasks_store.is_read(state, "t", "MSG-003")
    assert not tasks_store.is_read(state, "t", "MSG-002")
    assert not tasks_store.is_read(state, "t", "MSG-004")
    assert tasks_store.read_count(state, "t", 5) == 1


def test_a_thread_read_through_compacts_to_a_floor(state_dir):
    for n in (1, 2, 3):
        tasks_store.mark_read("t", tasks_store.format_message_id(n))
    record = json.loads((state_dir / "read.json").read_text())["t"]
    assert record["read_floor"] == 3
    assert record["read_ids"] == []
    state = tasks_store.read_state()
    assert all(tasks_store.is_read(state, "t", f"MSG-00{n}") for n in (1, 2, 3))
    assert not tasks_store.is_read(state, "t", "MSG-004")


def test_a_gap_filled_in_later_collapses_the_whole_run(state_dir):
    tasks_store.mark_read("t", "MSG-003")
    tasks_store.mark_read("t", "MSG-001")
    record = json.loads((state_dir / "read.json").read_text())["t"]
    assert record["read_floor"] == 1 and record["read_ids"] == ["MSG-003"]

    tasks_store.mark_read("t", "MSG-002")
    record = json.loads((state_dir / "read.json").read_text())["t"]
    assert record["read_floor"] == 3 and record["read_ids"] == []


def test_a_whole_task_mark_is_one_write_and_lands_as_the_watermark(state_dir):
    """"Mark read" on a task row. The point of the batch is that it is ONE
    read-modify-write for a thread of any length — 89 messages was 89 of them —
    and the compaction the single mark already had is what turns it into the
    integer that means "all of it"."""
    record = tasks_store.mark_read_many("t", tasks_store.message_ids(89))
    assert record["read_floor"] == 89
    assert record["read_ids"] == []
    on_disk = json.loads((state_dir / "read.json").read_text())["t"]
    assert on_disk == record

    state = tasks_store.read_state()
    assert tasks_store.read_count(state, "t", 89) == 89
    # And nothing beyond it: a message that has not arrived yet is not read.
    assert not tasks_store.is_read(state, "t", "MSG-090")


def test_a_whole_task_mark_keeps_the_exact_truth_around_a_gap(state_dir):
    """The batch marks the ids it is GIVEN and nothing else, which is what lets
    the router leave a still-pending message alone: MSG-002 has not happened, so
    it is not passed, and it must not come back already-read when it fires."""
    record = tasks_store.mark_read_many("t", ["MSG-001", "MSG-003", "MSG-004"])
    assert record["read_floor"] == 1
    assert record["read_ids"] == ["MSG-003", "MSG-004"]
    state = tasks_store.read_state()
    assert not tasks_store.is_read(state, "t", "MSG-002")
    assert tasks_store.read_count(state, "t", 4) == 3


def test_the_batch_and_the_single_mark_are_one_mechanism(state_dir):
    """mark_read IS mark_read_many of one, so the two cannot drift apart in what
    they compact or what they promise."""
    tasks_store.mark_read("a", "MSG-002")
    tasks_store.mark_read_many("b", ["MSG-002"])
    state = json.loads((state_dir / "read.json").read_text())
    assert state["a"]["read_ids"] == state["b"]["read_ids"] == ["MSG-002"]
    assert state["a"]["read_floor"] == state["b"]["read_floor"] == 0
    # An id that is not one is not recorded as a read message.
    record = tasks_store.mark_read_many("c", ["MSG-001", "nonsense", ""])
    assert record["read_floor"] == 1 and record["read_ids"] == []


def test_a_second_whole_task_mark_never_moves_another_task(state_dir):
    tasks_store.mark_read_many("t", tasks_store.message_ids(3))
    tasks_store.mark_read("u", "MSG-002")
    state = tasks_store.read_state()
    assert tasks_store.read_count(state, "t", 3) == 3
    assert tasks_store.read_count(state, "u", 3) == 1
    assert not tasks_store.is_read(state, "u", "MSG-001")


def test_read_count_never_exceeds_the_thread(state_dir):
    """A mark left over from a transcript that was replaced must not drive an
    unread count negative."""
    tasks_store.mark_read("t", "MSG-009")
    state = tasks_store.read_state()
    assert tasks_store.read_count(state, "t", 3) == 0
    assert tasks_store.read_count(state, "t", 9) == 1


def test_last_read_at_is_recorded_but_is_not_the_floor(state_dir):
    tasks_store.mark_read("t", "MSG-005", now=1755300000.0)
    record = json.loads((state_dir / "read.json").read_text())["t"]
    assert record["last_read_at"] == 1755300000.0
    # It is a wall clock, not a watermark: everything below MSG-005 is still
    # unread however long ago it happened.
    state = tasks_store.read_state()
    assert not tasks_store.is_read(state, "t", "MSG-004")


def test_the_baseline_marks_everything_that_already_exists_read(state_dir):
    """Day one. Unread has to mean "arrived since I started using this" — a
    fresh store that called every message ever written unread put a badge on
    174 of 192 rows on a real machine."""
    assert not tasks_store.initialized(tasks_store.read_state())
    assert tasks_store.initialize([("t", 10), ("u", 0)], now=1755300000.0)

    state = tasks_store.read_state()
    assert tasks_store.initialized(state)
    assert tasks_store.read_count(state, "t", 10) == 10
    assert tasks_store.is_read(state, "t", "MSG-010")
    # The message after the baseline is the first genuinely new one.
    assert not tasks_store.is_read(state, "t", "MSG-011")
    # A task with nothing in it takes no floor at all.
    assert "u" not in state


def test_the_baseline_is_stamped_exactly_once(state_dir):
    """A second run must not move it forward and silently mark unread things
    read."""
    tasks_store.initialize([("t", 3)], now=1755300000.0)
    assert tasks_store.initialize([("t", 9)], now=1755400000.0) is False

    state = tasks_store.read_state()
    assert state[tasks_store.INIT_KEY] == 1755300000.0
    assert not tasks_store.is_read(state, "t", "MSG-004")


def test_an_explicit_mark_still_works_above_the_baseline(state_dir):
    tasks_store.initialize([("t", 3)], now=1755300000.0)
    tasks_store.mark_read("t", "MSG-005")
    state = tasks_store.read_state()
    assert tasks_store.is_read(state, "t", "MSG-005")
    assert not tasks_store.is_read(state, "t", "MSG-004")
    assert tasks_store.read_count(state, "t", 5) == 4


def test_the_baseline_keeps_marks_made_before_it(state_dir):
    """Marking before the first listing is possible (the thread endpoint is
    reachable on its own), and the baseline must only ever add to what is
    read."""
    tasks_store.mark_read("t", "MSG-009")
    tasks_store.initialize([("t", 3)], now=1755300000.0)
    state = tasks_store.read_state()
    assert tasks_store.is_read(state, "t", "MSG-009")
    assert tasks_store.is_read(state, "t", "MSG-003")


def test_the_baseline_key_is_not_mistaken_for_a_task(state_dir):
    tasks_store.initialize([("t", 1)], now=1755300000.0)
    state = tasks_store.read_state()
    assert not tasks_store.is_read(state, tasks_store.INIT_KEY, "MSG-001")
    assert tasks_store.read_count(state, tasks_store.INIT_KEY, 5) == 0


def test_a_corrupt_read_store_reads_as_nothing_read(state_dir):
    (state_dir / "read.json").write_text("{{{ not json")
    assert tasks_store.read_state() == {}
    assert not tasks_store.is_read({}, "t", "MSG-001")


def test_concurrent_marks_all_survive(state_dir):
    """Two writers, one file. Without the read-modify-write inside the lock the
    second would persist a snapshot taken before the first's change and silently
    drop it."""
    if tasks_store.fcntl is None:
        # `_update`'s own comment says it: "Windows falls back to no
        # inter-process lock" — not a bug this sweep introduced, the same
        # accepted posture as claude_sessions.api_claude_session_triage. With
        # no lock at all, two threads racing the read-modify-write WILL drop
        # one side's marks; that is not this test's bug to catch, since the
        # code never promised otherwise there.
        pytest.skip("no lock on this platform (tasks_store._update, by design)")
    errors = []

    def mark(prefix):
        try:
            for n in range(1, 26):
                tasks_store.mark_read(f"{prefix}{n}", "MSG-001")
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=mark, args=(p,)) for p in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    state = tasks_store.read_state()
    assert len(state) == 50
    assert all(tasks_store.is_read(state, f"a{n}", "MSG-001") for n in range(1, 26))
    assert all(tasks_store.is_read(state, f"b{n}", "MSG-001") for n in range(1, 26))


def test_concurrent_allocations_do_not_collide(state_dir):
    """Two listings racing to number the same project must not hand out one
    number twice."""
    if tasks_store.fcntl is None:
        # Same reasoning as test_concurrent_marks_all_survive above: with no
        # lock on this platform, two threads can read the same "next number"
        # and both allocate it — an accepted gap, not a regression to catch.
        pytest.skip("no lock on this platform (tasks_store._update, by design)")

    def allocate(prefix):
        for n in range(10):
            tasks_store.ensure_ids([(f"{prefix}{n}", "/p", float(n))])

    threads = [threading.Thread(target=allocate, args=(p,)) for p in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    numbers = sorted(rec["n"] for rec in tasks_store.task_ids().values())
    assert numbers == list(range(1, 21))


# ------------------------------------------------------------------ the head


def test_the_head_is_cached_against_the_file_size(projects_dir):
    path = _transcript(projects_dir, "-home-a", "s1", "/home/a",
                       "2026-08-16T09:00:00Z", prompt="first thing")
    assert tasks_store.head(str(path))[0] == "/home/a"

    # A transcript is append-only, so a resolved head stays valid however much
    # the file grows — the second read must not go back to disk for it.
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "cwd": "/somewhere/else",
                            "timestamp": "2026-08-16T10:00:00Z",
                            "message": {"role": "user",
                                        "content": "later"}}) + "\n")
    cwd, first_ts, prompt, _pane = tasks_store.head(str(path))
    assert cwd == "/home/a"
    assert prompt == "first thing"
    assert first_ts == pytest.approx(
        tasks_store.epoch("2026-08-16T09:00:00Z"))


def _app_state_block(url):
    return ("<live-app-state>\nA snapshot of the preview the user is looking "
            "at in the left pane.\n"
            + json.dumps({"title": "Sine wave", "url": url,
                          "dom_path": "/tmp/shots/dom.json"})
            + "\n</live-app-state>")


def test_the_pane_file_is_read_out_of_a_leading_app_state_block():
    text = (_app_state_block(
        "/render?path=%2FUsers%2Fa%2Fproj%2Findex.html") + "\nfix the wave")
    assert tasks_store.pane_file(text) == "/Users/a/proj/index.html"


def test_a_title_that_mentions_url_cannot_hijack_the_pane_file():
    # The state is parsed as JSON, not regexed — a title carrying the literal
    # characters `"url":"..."` must lose to the real url field.
    blob = json.dumps({"title": 'see "url":"/render?path=%2Fevil.html" there',
                       "url": "/render?path=%2Freal.html"})
    text = "<live-app-state>\nprose\n" + blob + "\n</live-app-state>\nhello"
    assert tasks_store.pane_file(text) == "/real.html"


def test_a_templated_preview_names_the_users_file_not_our_template():
    # A `.py`/`.md`/`.parquet` preview renders THROUGH a template:
    # `/render?path=<template>&_file=<file>`. `path` is our template — a real
    # file on disk — and taking it made a chat about someone's parquet target
    # the duckdb template. `_file` is theirs.
    text = (_app_state_block(
        "/render?path=%2Fapp%2Ftemplates%2Fduckdb%2Ftemplate.html"
        "&_file=%2FUsers%2Fa%2Fdata.parquet&_remote=1") + "\nsum the col")
    assert tasks_store.pane_file(text) == "/Users/a/data.parquet"


def test_the_states_own_entry_field_beats_the_url():
    blob = json.dumps({"entry": "/Users/a/proj/index.html",
                       "url": "/render?path=%2Fsomething%2Felse.html"})
    text = "<live-app-state>\nprose\n" + blob + "\n</live-app-state>\nhi"
    assert tasks_store.pane_file(text) == "/Users/a/proj/index.html"


def test_a_non_leading_app_state_tag_is_the_humans_own_words():
    text = ("what does this tag do? " +
            _app_state_block("/render?path=%2Fa.html"))
    assert tasks_store.pane_file(text) == ""


def test_a_pane_url_without_a_path_param_answers_nothing():
    assert tasks_store.pane_file(_app_state_block("/explorer?tab=repos")) == ""
    assert tasks_store.pane_file(
        "<live-app-state>\nno json here\n</live-app-state>\nhi") == ""
    assert tasks_store.pane_file(
        "<live-app-state>\n{broken json\n</live-app-state>\nhi") == ""
    assert tasks_store.pane_file("plain words") == ""


def test_the_head_finds_the_pane_even_when_the_words_come_later(projects_dir):
    # A send can be the block alone (a screenshot with no words); the prompt
    # then comes from a later record, but the pane was already on record one.
    d = projects_dir / "-home-a"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "s-pane.jsonl"
    block = _app_state_block("/render?path=%2Fhome%2Fa%2Findex.html")
    path.write_text(
        json.dumps({"type": "user", "cwd": "/home/a",
                    "timestamp": "2026-08-16T09:00:00Z", "uuid": "u1",
                    "message": {"role": "user", "content": block}}) + "\n" +
        json.dumps({"type": "user", "cwd": "/home/a",
                    "timestamp": "2026-08-16T09:01:00Z", "uuid": "u2",
                    "message": {"role": "user",
                                "content": "actual words"}}) + "\n")
    cwd, _ts, prompt, pane = tasks_store.head(str(path))
    assert cwd == "/home/a"
    assert prompt == "actual words"
    assert pane == "/home/a/index.html"


def test_the_project_of_a_cwd_is_the_folder_itself():
    assert tasks_store.project_of("/home/a/") == "/home/a"
    assert tasks_store.project_of("/") == "/"
    assert tasks_store.project_of("") == ""


def test_an_unreadable_transcript_costs_only_itself(tmp_path):
    assert tasks_store.head(str(tmp_path / "nope.jsonl")) == \
        (None, None, "", "")


def test_epoch_reads_z_and_naive_stamps_as_utc():
    assert tasks_store.epoch("2026-08-16T09:00:00Z") == \
        tasks_store.epoch("2026-08-16T09:00:00+00:00")
    assert tasks_store.epoch("2026-08-16T09:00:00") == \
        tasks_store.epoch("2026-08-16T09:00:00Z")
    assert tasks_store.epoch("not a time") is None
    assert tasks_store.epoch(None) is None


def test_the_store_dir_is_global_not_branch_nested(monkeypatch):
    """Sessions are one pool for the machine, so a number allocated from a
    worktree must still be that task's number on main. Same rule, same
    directory, as claude_sessions.py's own state."""
    monkeypatch.setenv("FUSED_RENDER_HOME", "/tmp/whatever")
    import importlib

    reloaded = importlib.reload(tasks_store)
    try:
        assert reloaded.STATE_DIR == os.path.join("/tmp/whatever",
                                                  "claude-sessions")
    finally:
        importlib.reload(tasks_store)


# ------------------------------------------------------------- the machinery
# One stripper, in one place, for the four readers that parse a transcript's
# first user message. Before this they each had their own policy and no two
# agreed: the Tasks list dropped every record opening with a known tag (so a
# `<live-app-state>` prefix took the user's words with it), tasks_store and the
# session picker filtered nothing at all (so rows were titled `<live-app-state>`),
# and the template's own list dropped anything starting with "<". See the tag
# lists in tasks_store for the corpus counts behind the DROP/STRIP split.


def test_a_prepended_block_is_stripped_and_the_words_survive():
    """THE BUG. `<live-app-state>` is not machinery-only — the fused-render
    Claude page puts it in FRONT of what the user typed, so dropping the record
    deletes the human's message. One real session's only message was this
    string, and "what is this" was gone from the app entirely."""
    assert tasks_store.strip_machinery(
        records.prefixed(records.APP_STATE, records.PANE_SHOT,
                         records.PROSE)) == records.PROSE
    # …and it is therefore NOT machinery, whatever the leading tag is.
    assert tasks_store.is_machinery(
        records.prefixed(records.APP_STATE, records.PROSE)) is False


def test_the_annotation_preamble_is_stripped_down_to_the_note():
    """The annotation block carries no tag — one sentence, field notes for the
    model, and a fenced json array. Anchored on the fence, and only at position
    zero, which is why `composeOutgoing` fixes the block order."""
    assert tasks_store.strip_machinery(
        records.prefixed(records.APP_STATE, records.ANNOTATION,
                         records.ANNOTATED_ASK)) == records.ANNOTATED_ASK
    # The preamble alone, with no words after the fence, leaves nothing — a real
    # send (annotations and no typed message). The strip's answer stays "";
    # naming such a send is `ann_notes`' job, not this function's.
    assert tasks_store.strip_machinery(records.ANNOTATION) == ""
    assert tasks_store.strip_machinery(records.ANNOTATION_NOTED) == ""


def test_the_notes_on_the_pins_name_a_send_that_carried_no_words():
    """Annotations have needed no message since the "apply the comments"
    prefill was deleted, so for such a send the notes on the pins are the only
    text in the record a human wrote — and every reader was built on words
    arriving as free text. A SECOND source rather than a wider strip:
    `is_machinery` asks `strip_machinery` whether a record is worth keeping,
    and that question has a different answer."""
    assert tasks_store.ann_notes(
        records.prefixed(records.APP_STATE, records.PANE_SHOT,
                         records.ANNOTATION_NOTED)) == records.ANNOTATION_NOTE
    # A pin the user placed and wrote nothing on is still not a title, and
    # neither is anything that is not an annotation block at all.
    assert tasks_store.ann_notes(records.ANNOTATION) == ""
    assert tasks_store.ann_notes(records.PROSE) == ""
    assert tasks_store.ann_notes("") == ""


def test_a_head_prompt_falls_back_to_the_notes(tmp_path):
    """The end the Tasks list actually reads: a chat whose only user record is
    an annotation send used to hand it a blank title."""
    path = tmp_path / "t.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"cwd": str(tmp_path), "type": "user",
                   "timestamp": "2026-08-20T12:00:00Z",
                   "message": {"role": "user", "content": records.prefixed(
                       records.APP_STATE, records.ANNOTATION_NOTED)}}, fh)
        fh.write("\n")
    assert tasks_store.head(str(path))[2] == records.ANNOTATION_NOTE


def test_a_wordless_send_is_empty_but_still_not_machinery():
    """A screenshot with no words is something the USER did. `is_machinery` says
    "Claude Code wrote this whole record", so it must be false here even though
    the strip leaves nothing — a caller that wants "no words" asks the strip."""
    wordless = records.prefixed(records.APP_STATE, records.PANE_SHOT)
    assert tasks_store.strip_machinery(wordless) == ""
    assert tasks_store.is_machinery(wordless) is False


@pytest.mark.parametrize("text", [
    records.TASK_NOTIFICATION,
    records.SLASH_COMMAND,
    records.SLASH_COMMAND_ARGS,
    records.LOCAL_COMMAND_STDOUT,
    records.BASH_ENVELOPE,
])
def test_the_records_claude_code_writes_are_machinery_whole(text):
    assert tasks_store.strip_machinery(text) == ""
    assert tasks_store.is_machinery(text) is True


def test_a_half_written_machinery_record_is_still_machinery():
    """A transcript caught mid-flush has the opener and no close, so no balanced
    strip can fire. Everything from a machinery opener on is machinery whatever
    follows it, which is the same fallback template.html's BLOCK_OPENERS pass
    makes for a truncated preview."""
    assert tasks_store.strip_machinery(
        records.TASK_NOTIFICATION_HALF_WRITTEN) == ""
    assert tasks_store.is_machinery(records.TASK_NOTIFICATION_HALF_WRITTEN) is True


def test_a_tag_further_in_leaves_a_real_message_real():
    """Only a LEADING block is machinery. `<system-reminder>` appended to
    something a human typed is the pre-existing rule and the reason every match
    here is anchored at position zero."""
    said = "now ship it <system-reminder>be careful</system-reminder>"
    assert tasks_store.strip_machinery(said) == said
    assert tasks_store.is_machinery(said) is False


def test_markup_the_user_typed_is_not_a_machinery_block():
    """Why the strip knows the tag NAMES instead of matching `<\\w+>`: this is a
    real question about real markup, and a generic matcher would silently eat
    the half of it that makes it a question — the same class of bug as dropping
    the app-state prefix."""
    said = "<div class=\"card\">Order now</div> why does this render twice?"
    assert tasks_store.strip_machinery(said) == said
    assert tasks_store.is_machinery(said) is False


def test_the_slash_command_is_read_out_of_the_envelope_in_either_order():
    """A session whose only user records are a slash command has no prose to be
    named from, and the command IS real information. Both orders, because real
    transcripts contain both."""
    assert tasks_store.slash_command(records.SLASH_COMMAND) == "/making-a-release"
    assert tasks_store.slash_command(records.SLASH_COMMAND_ARGS) == "/model"
    assert tasks_store.slash_command(records.TASK_NOTIFICATION) == ""
    assert tasks_store.slash_command(records.PROSE) == ""


def test_the_head_prompt_is_the_words_not_the_block(projects_dir):
    path = _transcript(projects_dir, "-home-a", "s1", "/home/a",
                       "2026-08-16T09:00:00Z",
                       prompt=records.prefixed(records.APP_STATE, records.PROSE))
    assert tasks_store.head(str(path))[2] == records.PROSE


def test_the_head_keeps_scanning_past_a_machinery_record(projects_dir):
    """An empty remainder is not an answer. Accepting one gave the row a blank
    title while the message that could have named it sat two lines further
    down."""
    path = _transcript(projects_dir, "-home-a", "s1", "/home/a",
                       "2026-08-16T09:00:00Z", prompt=records.TASK_NOTIFICATION)
    with open(path, "a", encoding="utf-8") as f:
        for extra in ({"isSidechain": True, "text": "go and research this"},
                      {"text": "fix the parser"}):
            f.write(json.dumps({
                "type": "user", "cwd": "/home/a",
                "timestamp": "2026-08-16T09:01:00Z",
                "isSidechain": extra.get("isSidechain", False),
                "message": {"role": "user",
                            "content": [{"type": "text",
                                         "text": extra["text"]}]}}) + "\n")
    # Not the notification, and not the SUBAGENT's prompt either — `isSidechain`
    # is a prompt this module writes for a subagent, never one the user typed,
    # and its sibling reader in templates/claude/agent.py has always skipped it.
    assert tasks_store.head(str(path))[2] == "fix the parser"

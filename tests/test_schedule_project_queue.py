"""One task in progress per FOLDER, from the scheduler's side.

`project_queue.py` answers which folder a task edits and who is holding it;
tests/test_project_queue.py pins those answers. This file pins what
`schedule.py` does with them — the second gate in front of the same dispatch:

* **the order** — `priority` is stored flag-agnostically and read only with the
  flag on, so `_claim_due` and `queue()` list and send in the same order;
* **the hold** — a due entry whose working tree another conversation is editing
  is left `pending` and untouched, and one claim marks that folder taken for
  the rest of the pass;
* **held answers** — a card decision parked while the folder was busy is
  delivered (through `agent._decide`, which owns the validation) BEFORE any
  message, and holds the folder for that pass;
* **run-now on a busy folder** is a Skip, not a refusal;
* **the wake** — a turn ending, and the watcher seeing a session stop, ring the
  loop so the next queued message goes in about a second rather than thirty.

**Flag off is the control.** Every ordering and hold case is parametrized
across both values of the pref: with it off nothing here may change what the
rest of tests/test_schedule*.py already pins.

`project_queue.holders` is monkeypatched in most cases — the derivation is
tested against real run dirs, a real registry and real transcripts in
tests/test_project_queue.py, and repeating that here would test that file
twice and this one not at all.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from fused_render import (
    claude_spawn,
    schedule,
    schedule_wake,
    session_liveness,
    tasks_store,
    tasks_watch,
)
from fused_render import project_queue as pq

# Captured before `nothing_is_live` can replace it: the cases that are about the
# per-session hold need the real rule, and every other case needs it stubbed.
_REAL_SESSION_LIVE = schedule._session_live

SID = "11111111-1111-1111-1111-111111111111"
SID2 = "22222222-2222-2222-2222-222222222222"
SID3 = "33333333-3333-3333-3333-333333333333"


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A store and a prefs file of our own — the flag lives beside the schedule
    (both under FUSED_RENDER_HOME), so one redirect covers both."""
    house = tmp_path / "home"
    house.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(house))
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(state))
    pq.reset_cache()
    yield house
    pq.reset_cache()


@pytest.fixture(autouse=True)
def unbounded(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE", raising=False)


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture(autouse=True)
def fresh_process():
    schedule._watched.clear()
    schedule._wake.clear()
    schedule._cancel_rearm()
    yield
    schedule._watched.clear()
    schedule._wake.clear()
    schedule._cancel_rearm()


@pytest.fixture(autouse=True)
def nothing_is_live(monkeypatch):
    """No human turn anywhere. The per-session hold has its own file
    (test_schedule_session_liveness.py) and must not quietly decide anything
    here; the cases that want it re-stub it."""
    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: False)


@pytest.fixture(autouse=True)
def no_agent(monkeypatch):
    """No claude template agent by default, so `holders` finds no run holders
    and nothing tries to exec agent.py out of the real install."""
    monkeypatch.setattr(pq, "agent_module", lambda: None)


@pytest.fixture(autouse=True)
def rung(monkeypatch):
    """Every `tasks_watch.notify` the scheduler makes, recorded."""
    calls = []
    monkeypatch.setattr(tasks_watch, "notify", lambda keys=None: calls.append(set(keys or ())))
    return calls


@pytest.fixture()
def spawned(monkeypatch):
    """Every send that reached the helper. Same seam (and the same stubbed
    thread body) as tests/test_schedule.py."""
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id="", **kw):
        calls.append({"target": target, "session_id": session_id})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


@pytest.fixture()
def folders(tmp_path):
    """Two working trees, each with a page in it."""
    made = {}
    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        (d / "index.html").write_text("<html></html>")
        made[name] = d
    return made


@pytest.fixture()
def flag(request, home):
    """The pref, written for the value this case was parametrized with. `enabled`
    is read fresh per call, so no reset is needed either side."""
    on = getattr(request, "param", True)
    (home / "prefs.json").write_text(json.dumps({"project_queue_enabled": on}))
    return on


def _on(home, value=True):
    (home / "prefs.json").write_text(json.dumps({"project_queue_enabled": value}))


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _held(key, session_id=SID, run_id="r-1", request_id="p1", decision="allow"):
    """One held answer in the shape the decide path stores: the ORIGINAL
    `_decide` arguments, so delivery is a replay and not a second copy of that
    function's validation."""
    return pq.hold_answer(key, session_id, run_id, request_id,
                          {"raw": {"run_id": run_id, "request_id": request_id,
                                   "decision": decision, "scope": "once"}})


class FakeAgent:
    """The three things the delivery path touches. `RUNS` and `_alive` are what
    `validate_held_answers` reads; `_decide` is the delivery itself."""

    def __init__(self, runs, alive=True):
        self.RUNS = str(runs)
        self.alive = alive
        self.decided = []

    def _alive(self, run_dir):
        return self.alive

    def _permissions(self, run_dir):
        return []

    def _perm_dir(self, run_dir):
        return os.path.join(run_dir, "perm")

    def _write_decision(self, perm_dir, request_id, payload):
        os.makedirs(perm_dir, exist_ok=True)
        with open(os.path.join(perm_dir, request_id + ".res.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh)
        return True

    def _decide(self, **kw):
        self.decided.append(kw)
        return {"ok": True}


# ==================================================================== priority


def test_a_new_entry_is_never_born_priority(folders):
    """Priority is something a user asks for on work already waiting. Stored
    either way — the flag decides who READS it, not whether it exists."""
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))
    assert entry["priority"] is False
    assert schedule.create(str(folders["alpha"]), "go", _ago(1),
                           priority=True)["priority"] is True
    # and only a real `true` counts, like every other flag field here
    assert schedule.create(str(folders["alpha"]), "go", _ago(1),
                           priority="yes")["priority"] is False


def test_list_entries_carries_priority(folders):
    schedule.create(str(folders["alpha"]), "go", _ago(1), priority=True)
    assert [e["priority"] for e in schedule.list_entries()] == [True]


@pytest.mark.parametrize("flag", [False, True], indirect=True)
def test_due_order_is_the_same_with_the_flag_either_way(folders, flag):
    """The control. Nothing is priority, so both orders are due-then-id — which
    is what makes every existing scheduler test still true with the flag on."""
    first = schedule.create(str(folders["alpha"]), "one", _ago(60))
    second = schedule.create(str(folders["alpha"]), "two", _ago(30))

    assert schedule._claim_due(schedule._now()) == [first["id"], second["id"]]
    assert [e["id"] for e in schedule.queue()["queued"]] == [first["id"],
                                                             second["id"]]


@pytest.mark.parametrize("flag", [False, True], indirect=True)
def test_priority_jumps_the_line_only_with_the_flag_on(folders, flag):
    older = schedule.create(str(folders["alpha"]), "one", _ago(60))
    skipped = schedule.create(str(folders["alpha"]), "two", _ago(30),
                              priority=True)

    wanted = [skipped["id"], older["id"]] if flag else [older["id"], skipped["id"]]
    assert schedule._claim_due(schedule._now()) == wanted
    # the LIST and the RUN order are one key: a queue listed in one order and
    # sent in another is worse than no queue at all
    assert [e["id"] for e in schedule.queue()["queued"]] == wanted


def test_two_skips_keep_their_own_order(folders, home):
    """Priority ties fall to the older `due`, so skipping two things does not
    reshuffle them."""
    _on(home)
    older = schedule.create(str(folders["alpha"]), "one", _ago(60), priority=True)
    newer = schedule.create(str(folders["alpha"]), "two", _ago(30), priority=True)
    plain = schedule.create(str(folders["alpha"]), "three", _ago(90))

    assert schedule._claim_due(schedule._now()) == [older["id"], newer["id"],
                                                    plain["id"]]


# ================================================================ set_priority


def test_set_priority_updates_pending_and_rings(folders, rung):
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1), session_id=SID)
    schedule._wake.clear()

    assert schedule.set_priority([entry["id"]], True) == {
        "updated": [entry["id"]], "refused": []}
    assert schedule.list_entries()[0]["priority"] is True
    # the loop, so the new head goes in a second; and the page, which draws the
    # position and would otherwise take a poll interval to show the skip
    assert schedule._wake.is_set()
    assert rung[-1] == {SID}


def test_set_priority_keys_a_task_with_no_session_by_its_entry(folders, rung):
    """The Tasks page files an entry that has not run under `pending:<id>`;
    ringing any other key would wake a row nobody is watching."""
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))
    schedule.set_priority([entry["id"]], True)
    assert rung[-1] == {tasks_store.pending_key(entry["id"])}


def test_set_priority_refuses_anything_not_pending(folders, spawned):
    """An entry the tick has claimed is away: moving it up a line it has left
    would be a promise this module cannot keep."""
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))
    schedule.tick()
    assert schedule.list_entries()[0]["state"] != schedule.PENDING

    assert schedule.set_priority([entry["id"]], True) == {
        "updated": [], "refused": [entry["id"]]}
    assert schedule.set_priority(["no-such-id"], True) == {
        "updated": [], "refused": ["no-such-id"]}


def test_set_priority_un_skips_too(folders):
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1), priority=True)
    schedule.set_priority([entry["id"]], True)
    schedule.set_priority([entry["id"]], False)
    stored = schedule.list_entries()[0]
    assert stored["priority"] is False
    # BOTH FIELDS, TOGETHER. An entry with no promotion has no moment to be
    # promoted at, and a stamp left behind would put it back at the head the
    # next time anybody pressed the button on something else.
    assert "priority_at" not in stored


def test_run_next_stamps_the_moment_and_the_newest_click_goes_first(folders):
    """B, THEN C, THEN D RUN D, C, B (Akshil, 2026-09-12). `priority` alone only
    says "ahead of the un-promoted"; the stamp is what makes the button mean
    play-next instead of join-the-promoted-pile."""
    b = schedule.create(str(folders["alpha"]), "b", _ago(300), session_id=SID)
    c = schedule.create(str(folders["alpha"]), "c", _ago(200), session_id=SID)
    d = schedule.create(str(folders["alpha"]), "d", _ago(100), session_id=SID)
    for entry in (b, c, d):
        schedule.set_priority([entry["id"]], True)

    key = _key(folders["alpha"])
    now = schedule._now()
    stored = {e["id"]: e for e in schedule.list_entries()}
    assert all(e["priority_at"] > 0 for e in stored.values())
    assert [schedule._queue_position(stored[e["id"]], key, now)
            for e in (d, c, b)] == [1, 2, 3]

    # …and pressing it again on B takes the head straight back.
    schedule.set_priority([b["id"]], True)
    stored = {e["id"]: e for e in schedule.list_entries()}
    assert [schedule._queue_position(stored[e["id"]], key, now)
            for e in (b, d, c)] == [1, 2, 3]


def test_run_next_on_a_task_with_two_due_messages_keeps_them_in_order(folders):
    """🔴 review, 2026-09-12. One press promotes EVERY due message the task
    has waiting in that folder (`api_queue_skip`), and the stamp sorts newest
    first — so a clock read inside the loop stamped the second message a few
    microseconds after the first and put it in FRONT of it. One press is one
    gesture and one instant: the entries tie, and a tie falls through to `due`,
    which is the order they were typed in."""
    first = schedule.create(str(folders["alpha"]), "one", _ago(300),
                            session_id=SID)
    second = schedule.create(str(folders["alpha"]), "two", _ago(200),
                             session_id=SID)

    schedule.set_priority([first["id"], second["id"]], True)

    stored = {e["id"]: e for e in schedule.list_entries()}
    assert stored[first["id"]]["priority_at"] == stored[second["id"]]["priority_at"]
    key = _key(folders["alpha"])
    now = schedule._now()
    assert [schedule._queue_position(stored[e["id"]], key, now)
            for e in (first, second)] == [1, 2]


# ============================================================== the folder hold


def _key(folder):
    return pq.queue_key(str(folder))


def _holder(session_id, kind="run", run_id="run-1", task_key=None):
    return {"session_id": session_id, "run_id": run_id,
            "task_key": task_key if task_key is not None else session_id,
            "kind": kind}


@pytest.mark.parametrize("flag", [False, True], indirect=True)
def test_a_busy_folder_holds_only_with_the_flag_on(folders, spawned, flag,
                                                   monkeypatch):
    """Another conversation is editing this tree. Flag on: left pending,
    untouched — a wait, never a verdict. Flag off: today's behaviour, which
    knows nothing about folders."""
    monkeypatch.setattr(pq, "holders",
                        lambda now=None: {_key(folders["alpha"]): _holder(SID2)})
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1), session_id=SID)

    sent = schedule.tick()

    if flag:
        assert sent == []
        stored = schedule.list_entries()[0]
        assert stored["state"] == schedule.PENDING
        assert stored["fired"] == ""
        assert spawned == []
    else:
        assert [e["id"] for e in sent] == [entry["id"]]


def test_a_folder_frees_and_the_held_entry_goes_next_pass(folders, spawned,
                                                          home, monkeypatch):
    _on(home)
    busy = {_key(folders["alpha"]): _holder(SID2)}
    monkeypatch.setattr(pq, "holders", lambda now=None: dict(busy))
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1), session_id=SID)
    assert schedule.tick() == []

    busy.clear()
    assert [e["id"] for e in schedule.tick()] == [entry["id"]]


def test_one_claim_takes_the_folder_for_the_rest_of_the_pass(folders, spawned,
                                                             home, monkeypatch):
    """Two entries, one folder, one tick: the first claim marks the folder the
    same way `busy.add(session)` marks the session, so the second waits. Both
    have no session id, which is exactly the pair a session hold cannot see."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    first = schedule.create(str(folders["alpha"]), "one", _ago(60))
    second = schedule.create(str(folders["alpha"]), "two", _ago(30))

    assert [e["id"] for e in schedule.tick()] == [first["id"]]
    stored = {e["id"]: e for e in schedule.list_entries()}
    assert stored[second["id"]]["state"] == schedule.PENDING

    # …and with the first one's holder gone, the second goes.
    assert [e["id"] for e in schedule.tick()] == [second["id"]]


def test_two_folders_run_side_by_side(folders, spawned, home, monkeypatch):
    """No cap across folders — worktrees are the whole reason the key is the
    working tree and not the machine."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    a = schedule.create(str(folders["alpha"]), "one", _ago(60))
    b = schedule.create(str(folders["beta"]), "two", _ago(30))

    assert sorted(e["id"] for e in schedule.tick()) == sorted([a["id"], b["id"]])


def test_an_entry_into_its_own_holder_follows_the_session_rules(folders, spawned,
                                                                home, monkeypatch):
    """The folder gate lets a conversation into the folder it is already
    holding — and then the per-session rules, unchanged, decide. Live turn:
    held. Quiet: sent."""
    _on(home)
    monkeypatch.setattr(pq, "holders",
                        lambda now=None: {_key(folders["alpha"]): _holder(SID)})
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1), session_id=SID)

    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: True)
    assert schedule.tick() == []

    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: False)
    assert [e["id"] for e in schedule.tick()] == [entry["id"]]


def test_a_claimed_but_unspawned_entry_holds_its_folder(folders, spawned, home):
    """A `sending` entry is a folder about to be busy, and `holders` is NOT
    stubbed here: this is the one case where the derivation and the scheduler
    have to agree, because the scheduler is where the fact comes from."""
    _on(home)
    first = schedule.create(str(folders["alpha"]), "one", _ago(60))
    second = schedule.create(str(folders["alpha"]), "two", _ago(30), session_id=SID)
    # Claimed by hand and left there — the window between claim and spawn.
    assert schedule._claim(first["id"], schedule._now()) is not None
    assert pq.holders()[_key(folders["alpha"])]["kind"] == "sending"

    assert schedule.tick() == []
    assert {e["id"]: e["state"] for e in schedule.list_entries()}[
        second["id"]] == schedule.PENDING


def test_an_entry_with_no_folder_is_never_held(folders, spawned, home,
                                               monkeypatch):
    """`queue_key` answers "" for a path it will not gate ($HOME, `/`). "" is
    "no folder", and no folder is never busy — a task the server cannot place
    runs rather than queueing behind something it cannot name."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {"": _holder(SID2)})
    monkeypatch.setattr(pq, "queue_key", lambda project: "")
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))

    assert [e["id"] for e in schedule.tick()] == [entry["id"]]


# ============================================================== held answers


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    fake = FakeAgent(tmp_path / "runs")
    monkeypatch.setattr(pq, "agent_module", lambda: fake)
    return fake


def test_a_held_answer_is_delivered_and_holds_the_folder(folders, spawned,
                                                         home, agent, rung,
                                                         monkeypatch):
    """The answer goes first and the message waits: the run the user answered
    is already open in that tree, and it is older than anything queued."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    key = _key(folders["alpha"])
    _held(key)
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1), session_id=SID2)

    assert schedule.tick() == []
    # replayed through `_decide`, which owns the validation — not a decision
    # file written from the scheduler
    assert agent.decided == [{"run_id": "r-1", "request_id": "p1",
                              "decision": "allow", "scope": "once"}]
    assert pq.held_answers() == []
    assert {e["id"]: e["state"] for e in schedule.list_entries()}[
        entry["id"]] == schedule.PENDING
    assert {SID} in rung

    # next pass, nothing held: the message goes
    assert [e["id"] for e in schedule.tick()] == [entry["id"]]


def test_a_held_answer_waits_while_the_folder_is_still_busy(folders, home,
                                                            agent, monkeypatch):
    _on(home)
    key = _key(folders["alpha"])
    monkeypatch.setattr(pq, "holders", lambda now=None: {key: _holder(SID2)})
    _held(key)

    schedule.tick()

    assert agent.decided == []
    assert len(pq.held_answers()) == 1


def test_every_answer_for_one_run_goes_but_never_two_sessions(folders, home,
                                                              agent, monkeypatch):
    """A run can have several cards open at once, so they all go. A DIFFERENT
    parked session in the same folder is put straight back — resuming two runs
    into one working tree is what this feature exists to prevent."""
    _on(home)
    key = _key(folders["alpha"])
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    _held(key, SID, "r-1", "p1")
    _held(key, SID, "r-1", "p2")
    _held(key, SID2, "r-2", "p3")

    schedule.tick()

    assert [d["request_id"] for d in agent.decided] == ["p1", "p2"]
    assert [(a["session_id"], a["request_id"]) for a in pq.held_answers()] == [
        (SID2, "p3")]


def test_a_re_held_answer_keeps_the_place_it_had_in_the_line(
        folders, spawned, home, agent, monkeypatch):
    """Every answer for a folder comes out at once and the ones for other
    sessions go back — and going back used to restamp `at`, sending a record to
    the BACK of a line it was at the front of, once per pass, for as long as the
    other conversation kept the folder (round-3 review, 2026-09-12)."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    key = _key(folders["alpha"])
    _held(key, session_id=SID, run_id="r-1", request_id="p1")
    second = _held(key, session_id=SID2, run_id="r-2", request_id="p2")

    schedule.tick()          # SID resumes; SID2's answer goes back

    waiting = pq.held_answers()
    assert [a["request_id"] for a in waiting] == ["p2"]
    assert waiting[0]["at"] == second["at"]

    # …and its place is real: an answer held AFTER it does not overtake it.
    _held(key, session_id=SID3, run_id="r-3", request_id="p3")
    schedule.tick()
    assert [call["request_id"] for call in agent.decided] == ["p1", "p2"]
    assert [a["request_id"] for a in pq.held_answers()] == ["p3"]


def test_an_answer_whose_delivery_raises_is_held_again_and_goes_next_pass(
        folders, spawned, home, agent, monkeypatch):
    """The pop is the latch that stops two ticks delivering one answer, so it
    happens BEFORE `_decide` — which means a `_decide` that raises used to take
    the user's decision with it and leave the card asking for ever (round-3
    review, 2026-09-12). Put back with the `at` it had; the folder is held for
    this pass anyway, so the next one simply tries again."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    key = _key(folders["alpha"])
    record = _held(key)
    boom = [True]

    def once(**kw):
        if boom:
            boom.pop()
            raise RuntimeError("the run dir went out from under it")
        return agent.__class__._decide(agent, **kw)

    monkeypatch.setattr(agent, "_decide", once)

    schedule.tick()
    waiting = pq.held_answers()
    assert [a["request_id"] for a in waiting] == ["p1"]
    assert waiting[0]["at"] == record["at"]
    assert agent.decided == []

    schedule.tick()
    assert pq.held_answers() == []
    assert [call["request_id"] for call in agent.decided] == ["p1"]


def test_a_held_answer_for_a_dead_run_expires_and_frees_the_folder(
        folders, spawned, home, agent, monkeypatch):
    """`validate_held_answers` writes the decision out as `expired` — the same
    verdict `_decide` gives a dead run — and the message behind it goes."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    agent.alive = False
    key = _key(folders["alpha"])
    _held(key)
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))

    assert [e["id"] for e in schedule.tick()] == [entry["id"]]
    assert agent.decided == []
    assert pq.held_answers() == []
    expired = os.path.join(agent.RUNS, "r-1", "perm", "p1.res.json")
    with open(expired, encoding="utf-8") as fh:
        assert json.load(fh) == {"decision": "expired"}


def test_one_bad_answer_does_not_cost_the_tick_its_messages(folders, spawned,
                                                            home, agent,
                                                            monkeypatch):
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    monkeypatch.setattr(agent, "_decide",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    _held(_key(folders["alpha"]))
    other = schedule.create(str(folders["beta"]), "go", _ago(1))

    assert [e["id"] for e in schedule.tick()] == [other["id"]]


def test_held_answers_are_delivered_with_nothing_due(folders, home, agent,
                                                     monkeypatch):
    """The delivery is not a side effect of having messages to send: a user who
    answered a card while the folder was busy is owed it either way."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    _held(_key(folders["alpha"]))

    assert schedule.tick() == []
    assert len(agent.decided) == 1


def test_the_flag_off_never_touches_a_held_answer(folders, spawned, agent,
                                                  monkeypatch):
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    _held(_key(folders["alpha"]))
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))

    assert [e["id"] for e in schedule.tick()] == [entry["id"]]
    assert agent.decided == []
    assert len(pq.held_answers()) == 1


# ==================================================================== run-now


def test_run_now_on_a_busy_folder_is_a_skip(folders, spawned, home, monkeypatch):
    """The gesture still means something exact — it is the Skip verb — so the
    row can read `#1 in line · behind …` instead of an error."""
    _on(home)
    holder = _holder(SID2, kind="run", run_id="run-9")
    monkeypatch.setattr(pq, "holders",
                        lambda now=None: {_key(folders["alpha"]): holder})
    ahead = schedule.create(str(folders["alpha"]), "ahead", _ago(90))
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))

    out = schedule.run_now(entry["id"])

    assert out["ok"] is False
    assert out["found"] is True
    assert out["reason"] == "queued"
    assert out["queued"] is True
    assert out["ahead_session"] == SID2
    assert out["ahead_run"] == "run-9"
    # skipped to the head of its folder's line, past an older entry
    assert out["entry"]["priority"] is True
    assert out["position"] == 1
    # …and nothing was claimed
    assert out["entry"]["state"] == schedule.PENDING
    assert spawned == []
    # RUN NEXT IS PLAY NEXT: the most recent promotion goes first. Promoting
    # `ahead` (the older message) puts IT at the head and this one at #2 —
    # and promoting this one again takes the head straight back.
    assert schedule.run_now(ahead["id"])["position"] == 1
    stored = {e["id"]: e for e in schedule.list_entries()}
    assert schedule._queue_position(
        stored[entry["id"]], _key(folders["alpha"]), schedule._now()) == 2
    assert schedule.run_now(entry["id"])["position"] == 1


def test_run_now_names_the_holder_by_its_task_and_not_by_its_session(
        folders, home, monkeypatch):
    """A `sending` holder — claimed, not yet spawned — has no conversation to
    its name, and the row still has to say who it is waiting behind. The TASK
    key is what the Tasks page can turn into a number ("behind TASK-041");
    `ahead_session` stays beside it for a caller that wants the conversation."""
    _on(home)
    key = _key(folders["alpha"])
    holder = _holder("", kind="sending", run_id="", task_key="pending:e-x")
    monkeypatch.setattr(pq, "holders", lambda now=None: {key: holder})
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))

    out = schedule.run_now(entry["id"])

    assert out["ahead_task_key"] == "pending:e-x"
    assert out["ahead_session"] == ""


def test_a_queued_position_counts_held_answers_first(folders, home, monkeypatch):
    """A held answer outranks every message in its folder, so it is counted
    ahead even of a skip."""
    _on(home)
    key = _key(folders["alpha"])
    monkeypatch.setattr(pq, "holders", lambda now=None: {key: _holder(SID2)})
    _held(key)
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))

    assert schedule.run_now(entry["id"])["position"] == 2


def test_run_now_is_unchanged_with_the_flag_off(folders, spawned, monkeypatch):
    monkeypatch.setattr(pq, "holders",
                        lambda now=None: {_key(folders["alpha"]): _holder(SID2)})
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))

    out = schedule.run_now(entry["id"])

    assert out["ok"] is True
    assert "queued" not in out
    assert len(spawned) == 1


def test_run_now_into_its_own_folder_still_runs(folders, spawned, home,
                                                monkeypatch):
    _on(home)
    monkeypatch.setattr(pq, "holders",
                        lambda now=None: {_key(folders["alpha"]): _holder(SID)})
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1), session_id=SID)

    assert schedule.run_now(entry["id"])["ok"] is True


# ================================================== priority is never inherited


def test_a_materialized_occurrence_is_never_priority(folders, home):
    """A skip is a decision about ONE waiting run. A template whose occurrence
    was skipped must not mint every future run at the head of the line."""
    _on(home)
    template = schedule.create(str(folders["alpha"]), "daily", None,
                               repeats="*/5 * * * *")
    occurrence = next(e for e in schedule.list_entries()
                      if e.get("template_id") == template["id"])
    assert occurrence["priority"] is False

    schedule.set_priority([occurrence["id"]], True)
    schedule._update(occurrence["id"], state=schedule.SENT, turn="ok")
    schedule._materialize(schedule._now() + timedelta(minutes=10))

    fresh = [e for e in schedule.list_entries()
             if e.get("template_id") == template["id"]
             and e["state"] == schedule.PENDING]
    assert fresh and all(e["priority"] is False for e in fresh)


def test_coalescing_keeps_the_survivors_priority(folders, home):
    """`_coalesce` collapses a backlog onto one surviving entry, in place — a
    skip the user asked for on that run is not something a catch-up may undo,
    and it must not invent one either."""
    _on(home)
    template = schedule.create(str(folders["alpha"]), "daily", None,
                               repeats="*/5 * * * *")
    occurrence = next(e for e in schedule.list_entries()
                      if e.get("template_id") == template["id"])
    schedule._update(occurrence["id"], due=_ago(3600).isoformat())
    schedule.set_priority([occurrence["id"]], True)

    schedule._coalesce(schedule._now())

    survivor = next(e for e in schedule.list_entries()
                    if e["id"] == occurrence["id"])
    assert survivor["priority"] is True
    assert survivor["skipped"] >= 1  # it really was collapsed


def test_a_restored_occurrence_keeps_its_priority(folders, home):
    _on(home)
    template = schedule.create(str(folders["alpha"]), "daily", None,
                               repeats="*/5 * * * *")
    occurrence = next(e for e in schedule.list_entries()
                      if e.get("template_id") == template["id"])
    schedule.set_priority([occurrence["id"]], True)
    schedule.cancel(occurrence["id"])

    assert schedule.restore(occurrence["id"])["priority"] is True


def test_learning_the_session_id_keeps_priority(folders, home, spawned):
    """The rekey a first run makes — `pending:<id>` becomes a session id — is a
    field merge, so everything else on the entry survives it."""
    _on(home)
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1), priority=True)
    schedule._update(entry["id"], state=schedule.SENT)

    schedule._turn_tick(dict(entry), "r-1", None, {"session_id": SID})

    stored = schedule.list_entries()[0]
    assert stored["claude_session_id"] == SID
    assert stored["priority"] is True


def test_a_resent_message_is_not_priority(folders, home, spawned):
    """Asking again is a new message, not a skip."""
    _on(home)
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1), priority=True)
    schedule._update(entry["id"], state=schedule.SENT, turn="failed",
                     claude_session_id=SID)

    fresh = schedule.resend(entry["id"])["entry"]
    assert fresh["priority"] is False


# ======================================================================= wake


def test_a_turn_ending_rings_the_loop_and_the_page(folders, rung):
    """The 30-second poll is right for "did anything come due" and wrong for
    "the thing in the way has stopped". Flag-agnostic: with the queue off the
    ring costs one early pass that finds the same nothing."""
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))
    schedule._update(entry["id"], state=schedule.SENT)
    schedule._wake.clear()
    rung.clear()

    schedule._turn_tick(dict(entry, claude_session_id=SID), "r-1", None,
                        {"done": True})

    assert schedule._wake.is_set()
    assert rung[-1] == {SID}


def test_an_unwatched_turn_closing_rings_too(folders, rung):
    """A turn nobody could follow to the end still ENDED."""
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))
    schedule._update(entry["id"], state=schedule.SENT, claude_session_id=SID)
    schedule._wake.clear()
    rung.clear()

    schedule._close_unwatched(dict(entry), "stopped reporting")

    assert schedule._wake.is_set()
    assert rung[-1] == {SID}


def test_wake_is_a_hint_anyone_may_ring():
    schedule._wake.clear()
    schedule.wake()
    assert schedule._wake.is_set()


# ============================================== the watcher rings the scheduler


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """A `~/.claude/sessions` of our own, and a recorder in place of
    `schedule.wake` — `_wake_schedule` looks the attribute up on the module at
    call time, which is the seam."""
    sessions = tmp_path / "claude" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(tasks_watch, "SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(tasks_watch, "HISTORY_PATH",
                        str(tmp_path / "claude" / "history.jsonl"))
    tasks_watch.reset()
    yield sessions
    tasks_watch.reset()


@pytest.fixture()
def woke(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule, "wake", lambda: calls.append(1))
    return calls


def _row(sessions, status, stamp, name="p.json"):
    path = sessions / name
    path.write_text(json.dumps({"pid": os.getpid(), "sessionId": SID,
                                "cwd": "/proj", "status": status,
                                "updatedAt": int(stamp * 1000)}))
    os.utime(path, (stamp, stamp))
    tasks_watch.tick()


@pytest.mark.parametrize("flag", [False, True], indirect=True)
def test_a_session_leaving_busy_rings_the_scheduler(registry, woke, flag):
    """This loop stats the registry once a second, so it learns a run stopped a
    poll interval before the scheduler's own timer would. Gated: with the flag
    off nothing about a default install changes."""
    stamp = 1_800_000_000.0
    _row(registry, "busy", stamp)
    assert woke == []          # arriving busy is not a folder freeing

    _row(registry, "idle", stamp + 1)

    assert bool(woke) is flag


def test_a_departed_row_rings_the_scheduler(registry, woke, home):
    _on(home)
    stamp = 1_800_000_000.0
    _row(registry, "busy", stamp)
    woke.clear()

    (registry / "p.json").unlink()
    tasks_watch.tick()

    assert woke


def test_a_dead_pid_rings_the_scheduler(registry, woke, home, monkeypatch):
    _on(home)
    stamp = 1_800_000_000.0
    _row(registry, "busy", stamp)
    woke.clear()

    # The file stays behind, untouched, exactly as a crashed claude leaves it.
    monkeypatch.setattr(tasks_watch, "_pid_alive", lambda pid: False)
    tasks_watch.tick()

    assert woke


def test_a_session_staying_busy_rings_nothing(registry, woke, home):
    """A status rewrite that is still `busy` is not a folder freeing, and a ring
    per second per live session would be a busy loop with extra steps."""
    _on(home)
    stamp = 1_800_000_000.0
    _row(registry, "busy", stamp)
    woke.clear()

    _row(registry, "busy", stamp + 1)

    assert woke == []


# =========================================== follow-ups into a queued new chat


def _pair(folder, gap=0):
    """A queued first message and the one typed behind it, both due."""
    leader = schedule.create(str(folder), "first", _ago(60))
    follower = schedule.create(str(folder), "second", _ago(60 - gap),
                               follow_of=leader["id"])
    return leader, follower


def _stored(entry_id):
    return next(e for e in schedule.list_entries() if e["id"] == entry_id)


def test_follow_of_must_name_a_message_that_exists(folders):
    """A follower borrows its leader's task key and its leader's session, so an
    id that names nothing would file a message under a row that is not there."""
    leader = schedule.create(str(folders["alpha"]), "first", _ago(60))

    assert schedule.create(str(folders["alpha"]), "second", _ago(59),
                           follow_of=leader["id"])["follow_of"] == leader["id"]
    with pytest.raises(ValueError, match="follow_of"):
        schedule.create(str(folders["alpha"]), "second", _ago(59),
                        follow_of="no-such-entry")
    # …and nothing to follow is the ordinary shape, stored as ""
    assert schedule.create(str(folders["alpha"]), "third",
                           _ago(58))["follow_of"] == ""


@pytest.mark.parametrize("flag", [False, True], indirect=True)
def test_a_follower_waits_for_the_chat_its_leader_is_opening(folders, spawned,
                                                             flag, monkeypatch):
    """The folder is free — the leader is away and holds nothing this pass can
    see — and the follower still must not go: the two messages are one
    conversation, and the second cannot open it."""
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    leader, follower = _pair(folders["alpha"])
    # The leader is sent with its turn still open and no session reported yet,
    # and watched, so the sweep leaves it alone.
    schedule._update(leader["id"], state=schedule.SENT, run_id="r-1")
    schedule._watching(leader["id"], True)

    sent = schedule.tick()

    if flag:
        assert sent == []
        assert _stored(follower["id"])["state"] == schedule.PENDING
    else:
        # Today's behaviour knows nothing about `follow_of`: a fresh session.
        assert [e["id"] for e in sent] == [follower["id"]]
        assert spawned[-1]["session_id"] == ""


def test_a_leader_goes_first_and_its_follower_resumes_what_it_opened(
        folders, spawned, home, monkeypatch):
    """The whole point of the field, in two passes: one message at a time, the
    second into the conversation the first opened."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    leader, follower = _pair(folders["alpha"])

    assert [e["id"] for e in schedule.tick()] == [leader["id"]]
    assert spawned == [{"target": str(folders["alpha"]), "session_id": ""}]

    # The run reports which session it landed in, and its turn ends.
    schedule._turn_tick(_stored(leader["id"]), "r-1", None,
                        {"session_id": SID, "done": True})

    assert [e["id"] for e in schedule.tick()] == [follower["id"]]
    assert spawned[-1]["session_id"] == SID
    # …and the row records the conversation it went into, marked as learned —
    # the system worked this id out from a run, nobody chose it.
    stored = _stored(follower["id"])
    assert stored["session_id"] == SID
    assert stored["session_learned"] is True


def test_a_cancelled_leader_does_not_orphan_its_follower(folders, spawned, home,
                                                         monkeypatch):
    """The message is still owed and there is no thread to continue, so it opens
    one. Waiting for ever would be this feature losing the user's words."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    leader, follower = _pair(folders["alpha"])
    schedule.cancel(leader["id"])

    assert [e["id"] for e in schedule.tick()] == [follower["id"]]
    assert spawned[-1]["session_id"] == ""


def test_an_erased_leader_leaves_its_follower_standing_alone(folders, spawned,
                                                             home, monkeypatch):
    """A hand-edited store, or a delete that took the leader's row. The follower
    is what it was before the field existed: an ordinary fresh-session message."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    leader, follower = _pair(folders["alpha"])
    schedule._write([e for e in schedule.list_entries()
                     if e["id"] != leader["id"]])

    assert [e["id"] for e in schedule.tick()] == [follower["id"]]
    assert spawned[-1]["session_id"] == ""


@pytest.mark.parametrize("flag", [False, True], indirect=True)
def test_a_follower_never_sorts_before_its_leader(folders, flag):
    """Even due EARLIER — a clock that moved, an edit that back-dated it. With
    the flag on the line reads the chain; with it off this is the old sort,
    which is exactly what makes every existing case still true."""
    leader = schedule.create(str(folders["alpha"]), "first", _ago(60))
    follower = schedule.create(str(folders["alpha"]), "second", _ago(90),
                               follow_of=leader["id"])

    wanted = ([leader["id"], follower["id"]] if flag
              else [follower["id"], leader["id"]])
    assert schedule._claim_due(schedule._now()) == wanted
    assert [e["id"] for e in schedule.queue()["queued"]] == wanted


def test_two_followers_of_one_leader_keep_the_order_they_were_typed(folders,
                                                                    home):
    _on(home)
    leader = schedule.create(str(folders["alpha"]), "first", _ago(60))
    second = schedule.create(str(folders["alpha"]), "second", _ago(59),
                             follow_of=leader["id"])
    third = schedule.create(str(folders["alpha"]), "third", _ago(58),
                            follow_of=second["id"])

    assert schedule._claim_due(schedule._now()) == [leader["id"], second["id"],
                                                    third["id"]]


def test_a_follower_is_filed_under_its_leaders_row(folders, rung, home):
    """`tasks_watch.notify` keys are matched against the rows the listing built,
    and a follower has no row of its own — it is a message in the leader's."""
    _on(home)
    leader, follower = _pair(folders["alpha"])

    schedule.set_priority([follower["id"]], True)
    assert rung[-1] == {tasks_store.pending_key(leader["id"])}

    # …and once the leader has run, the same row is the session.
    schedule._update(leader["id"], claude_session_id=SID)
    assert schedule._task_key(_stored(follower["id"])) == SID


def test_a_follow_chain_that_points_at_itself_costs_nothing(folders, home):
    """The store is a file a human may edit. A cycle ends the walk where it
    started rather than spinning a tick."""
    _on(home)
    leader, follower = _pair(folders["alpha"])
    schedule._write([dict(e, follow_of=follower["id"]) if e["id"] == leader["id"]
                     else e for e in schedule.list_entries()])

    assert schedule._task_key(_stored(follower["id"])).startswith("pending:")
    assert schedule._claim_due(schedule._now())


def test_a_follow_chain_past_the_bound_stands_alone(folders, home, monkeypatch):
    """`FOLLOW_HOPS` is a WALK bound, not a filing rule.

    Answering with the middle entry the walk happened to stop on files one
    message under a key that is itself a follower — `pending:<middle>`, while
    the middle entry's own row is `pending:<head>` — so a single message appears
    under two keys and a ring reaches neither reliably. None is the answer an
    erased leader already gives, and dispatch has to agree with the filing or
    the message would wait on a leader whose row it is not even on."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    chain = [schedule.create(str(folders["alpha"]), "m0", _ago(100))]
    for n in range(1, schedule.FOLLOW_HOPS + 2):
        chain.append(schedule.create(str(folders["alpha"]), f"m{n}",
                                     _ago(100 - n), follow_of=chain[-1]["id"]))
    by_id = {e["id"]: e for e in schedule.list_entries()}
    head = chain[0]["id"]

    # The last link the bound reaches still files under the head of the chain…
    within = by_id[chain[schedule.FOLLOW_HOPS]["id"]]
    assert schedule.leader_of(within, by_id)["id"] == head
    assert schedule._task_key(within) == tasks_store.pending_key(head)

    # …and the one past it stands alone, in the filing AND in the dispatch.
    beyond = by_id[chain[schedule.FOLLOW_HOPS + 1]["id"]]
    assert schedule.leader_of(beyond, by_id) is None
    assert schedule._task_key(beyond) == tasks_store.pending_key(beyond["id"])
    assert schedule._follow_session(beyond, by_id) == ("", True)
    assert pq.order_key(beyond, by_id) == pq.order_key(beyond)


# ====================================== the finished turn's own closing echo


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    """A `~/.claude/projects` of our own, so the liveness rule reads files this
    test wrote and never the developer's real sessions."""
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(session_liveness, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture()
def live_reads_the_transcript(monkeypatch, transcripts):
    """Un-stub the per-session hold for the cases that are ABOUT it. Everything
    else in this file keeps `nothing_is_live`."""
    monkeypatch.setattr(schedule, "_session_live", _REAL_SESSION_LIVE)
    return transcripts


def _transcript(root, session_id, seconds_ago):
    """A transcript whose newest real record is `seconds_ago` old — which is
    what a turn in flight and a turn that ended a moment ago look like from the
    outside. Telling them apart is the whole of this section."""
    d = root / ("-encoded-" + session_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text(json.dumps({
        "type": "assistant",
        "timestamp": _ago(seconds_ago).isoformat().replace("+00:00", "Z"),
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "done"}]}}) + "\n")
    return path


def _typed_into(root, session_id, seconds_ago):
    """The same transcript with a USER row as its last word — a turn somebody
    opened, which is what an echo never is however fresh the stamp."""
    path = _transcript(root, session_id, seconds_ago + 1)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "user",
            "timestamp": _ago(seconds_ago).isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": "and now this"}}) + "\n")
    return path


def _finished_leader(leader, seconds_ago):
    """The leader as `_turn_tick` leaves it the moment its turn ends: sent, its
    session reported, a verdict and the stamp that goes with it."""
    schedule._update(leader["id"], state=schedule.SENT, run_id="r-1",
                     fired=_ago(seconds_ago + 30).isoformat(),
                     claude_session_id=SID, turn="ok",
                     turn_at=_ago(seconds_ago).isoformat())


def test_a_follower_goes_the_moment_its_leaders_turn_ends(
        folders, spawned, home, monkeypatch, live_reads_the_transcript):
    """THE BUG (browser QA, 2026-09-12): the leader finished at T and its
    follower did not go until T+60. The folder was free and nothing was busy —
    the transcript's 45-second window was still counting the leader's own
    closing rows as a live turn, so the per-session hold held the follower
    against the very turn that had just ended."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    leader, follower = _pair(folders["alpha"])
    _finished_leader(leader, 5)
    _transcript(live_reads_the_transcript, SID, 5)

    assert [e["id"] for e in schedule.tick()] == [follower["id"]]
    assert spawned[-1]["session_id"] == SID


def test_a_session_still_working_past_the_echo_keeps_the_hold(
        folders, spawned, home, monkeypatch, live_reads_the_transcript):
    """The verdict may only silence its OWN echo. A transcript written to well
    after the stamp is new work — the user typing into the same chat — and two
    processes on one transcript is what the hold exists to stop."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    leader, follower = _pair(folders["alpha"])
    _finished_leader(leader, 60)
    _transcript(live_reads_the_transcript, SID, 2)

    assert schedule.tick() == []
    assert _stored(follower["id"])["state"] == schedule.PENDING


@pytest.mark.parametrize("flag", [False, True], indirect=True)
def test_the_echo_is_only_silenced_with_the_flag_on(
        folders, spawned, flag, monkeypatch, live_reads_the_transcript):
    """A correctness fix for the pre-existing per-session hold, kept to the
    feature that cannot live without it: with the queue off the 30-second poll
    sends it a tick later, which is what shipped."""
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    done = schedule.create(str(folders["alpha"]), "first", _ago(60))
    schedule._update(done["id"], state=schedule.SENT, claude_session_id=SID,
                     turn="ok", turn_at=_ago(5).isoformat())
    _transcript(live_reads_the_transcript, SID, 5)
    later = schedule.create(str(folders["alpha"]), "second", _ago(30),
                            session_id=SID)

    sent = [e["id"] for e in schedule.tick()]
    assert sent == ([later["id"]] if flag else [])


@pytest.mark.parametrize("verdict", ["unknown", "cancelled"])
def test_a_verdict_nobody_watched_land_never_echoes(
        folders, spawned, home, monkeypatch, live_reads_the_transcript,
        verdict):
    """ONLY A WATCHED TURN MAY SILENCE A TRANSCRIPT (round-3 review,
    2026-09-12). `_claim_due`'s sweep stamps `unknown` on every SENT entry whose
    turn was open when the app stopped — on the first tick after a restart that
    is a `claude` still writing — and the ✕ arm stamps `cancelled` in the line
    after `agent._cancel`, whose gentler road only asks the CLI to abort. Either
    word read as a verdict calls a live transcript's own pulse an echo and puts
    a second `claude --resume` on it."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    leader, follower = _pair(folders["alpha"])
    schedule._update(leader["id"], state=schedule.SENT, run_id="r-1",
                     fired=_ago(35).isoformat(), claude_session_id=SID,
                     turn=verdict, turn_at=_ago(5).isoformat())
    _transcript(live_reads_the_transcript, SID, 5)

    assert schedule.tick() == []
    assert _stored(follower["id"])["state"] == schedule.PENDING


def test_a_turn_opened_inside_the_echo_window_still_holds(
        folders, spawned, home, monkeypatch, live_reads_the_transcript):
    """THE TAIL'S SHAPE DECIDES (round-3 review, 2026-09-12). The user typing
    three seconds after the leader finished is activity younger than
    `turn_at + 15s`, and measuring by the stamp alone read that as the leader's
    own echo — two processes on one transcript, arriving through the rule meant
    to stop them. A `user` row as the last word is a turn somebody has open."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    leader, follower = _pair(folders["alpha"])
    _finished_leader(leader, 5)
    _typed_into(live_reads_the_transcript, SID, 3)

    assert schedule.tick() == []
    assert _stored(follower["id"])["state"] == schedule.PENDING


def test_a_verdict_with_no_stamp_keeps_the_old_rule(
        folders, spawned, home, monkeypatch, live_reads_the_transcript):
    """An entry a pre-stamp store wrote has no moment to measure the tail
    against, so the window decides — the honest answer, and the same one the
    Tasks router gives the same entry."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    done = schedule.create(str(folders["alpha"]), "first", _ago(60))
    schedule._update(done["id"], state=schedule.SENT, claude_session_id=SID,
                     turn="ok", turn_at="")
    _transcript(live_reads_the_transcript, SID, 5)
    schedule.create(str(folders["alpha"]), "second", _ago(30), session_id=SID)

    assert schedule.tick() == []


def test_a_hold_on_a_reservation_is_woken_when_the_reservation_ends(
        folders, spawned, home):
    """`_turn_ended` rings when a verdict lands, which covers every hold that
    ends on an EVENT. A reservation — an admitted send whose process has not
    appeared — ends on a clock and rings nothing, so the pass that held for it
    arms a timer for the moment it actually runs out (round-3 review,
    2026-09-12: it used to ring two seconds in, which is not when any of these
    clocks expire, and then never again while the hold stood)."""
    _on(home)
    pq.reserve(_key(folders["alpha"]), SID2, ttl=pq.RESERVATION_TTL)
    schedule.create(str(folders["alpha"]), "behind it", _ago(30),
                    session_id=SID)

    assert schedule.tick() == []
    timer = schedule._rearm_timer
    assert timer is not None
    assert pq.RESERVATION_TTL - 5 < timer.interval <= pq.RESERVATION_TTL


def test_a_clock_longer_than_the_poll_is_capped_to_it(
        folders, spawned, home, monkeypatch, live_reads_the_transcript):
    """The 45-second transcript window outlasts the poll that is the floor under
    all of this, so the timer is capped: waking later than the ordinary pass
    would is a bell that rings after the thing it was for."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    _transcript(live_reads_the_transcript, SID, 2)
    schedule.create(str(folders["alpha"]), "held", _ago(30), session_id=SID)

    assert schedule.tick() == []
    assert schedule._rearm_timer.interval == float(schedule.POLL_INTERVAL_S)


def test_a_second_pass_re_arms_the_same_hold(folders, spawned, home):
    """One timer at a time, re-armed as the clock runs down — not one per tick
    piling up, and not one for the whole episode fired far too early."""
    _on(home)
    pq.reserve(_key(folders["alpha"]), SID2, ttl=pq.RESERVATION_TTL)
    schedule.create(str(folders["alpha"]), "behind it", _ago(30),
                    session_id=SID)

    assert schedule.tick() == []
    first = schedule._rearm_timer
    assert schedule.tick() == []
    second = schedule._rearm_timer

    assert second is not None and second is not first
    assert first.finished.is_set()          # the old one was cancelled, not left
    assert second.interval <= first.interval


def test_nothing_held_re_arms_nothing(folders, spawned, home, monkeypatch):
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    schedule.create(str(folders["alpha"]), "go", _ago(30))

    assert len(schedule.tick()) == 1
    assert schedule._rearm_timer is None


def test_a_hold_whose_clock_has_already_run_out_does_not_spin(home):
    """The floor. A delay of zero would be a tick per tick for as long as the
    state stood; the next pass is what settles it."""
    schedule._rearm([0.0, 0.0])
    assert schedule._rearm_timer.interval == schedule._REARM_FLOOR_S


# ================================ run now on a message whose time is not yet


def test_run_now_on_a_far_future_message_really_joins_the_line(
        folders, spawned, home, monkeypatch):
    """`due` is the ask and never moves — so a message due TOMORROW, skipped to
    the head of a busy folder's line, was in a line nothing could see it in
    (browser QA, 2026-09-12). `run_now_at` is the second stamp that puts it
    there, and the tick reads it the moment the folder frees."""
    _on(home)
    key = _key(folders["alpha"])
    live = {key: _holder(SID2)}
    monkeypatch.setattr(pq, "holders", lambda now=None: dict(live))
    entry = schedule.create(str(folders["alpha"]), "tomorrow", _ago(-86400))

    out = schedule.run_now(entry["id"])

    assert out["reason"] == "queued"
    assert out["position"] == 1
    stored = _stored(entry["id"])
    # The ask is untouched: the calendar still draws the chip on tomorrow.
    assert stored["due"] == entry["due"]
    assert stored["run_now_at"]
    # It is really IN the line now — listed, and claimable.
    assert [e["id"] for e in schedule.queue()["queued"]] == [entry["id"]]
    assert schedule.tick() == []          # …behind the holder, which is the ask

    live.clear()
    assert [e["id"] for e in schedule.tick()] == [entry["id"]]


def test_a_far_future_message_nobody_asked_for_stays_out_of_the_line(
        folders, spawned, home, monkeypatch):
    """The control. Without the gesture the entry is waiting on the CLOCK, and
    calling it queued would move tomorrow's work into a lane that reads as
    about to run."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    schedule.create(str(folders["alpha"]), "tomorrow", _ago(-86400))

    assert schedule.queue()["queued"] == []
    assert schedule.tick() == []


def test_a_deferred_run_now_stamps_a_busy_session_too(
        folders, spawned, home, monkeypatch, live_reads_the_transcript):
    """The other deferring arm makes the same promise — "it will go on its own
    as soon as that turn ends" — and for a message due next week it was not
    true until this stamp existed."""
    _on(home)
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    _transcript(live_reads_the_transcript, SID, 2)
    entry = schedule.create(str(folders["alpha"]), "next week", _ago(-604800),
                            session_id=SID)

    out = schedule.run_now(entry["id"])

    assert out["ok"] is False
    assert "turn" in out["reason"]
    assert out["entry"]["run_now_at"]
    assert [e["id"] for e in schedule.queue()["queued"]] == [entry["id"]]


def test_run_now_stamps_nothing_with_the_flag_off(folders, spawned, monkeypatch):
    """Flag off is byte-for-byte what shipped: the busy-folder arm does not
    exist, and the busy-session refusal leaves the entry exactly as it was."""
    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: True)
    entry = schedule.create(str(folders["alpha"]), "next week", _ago(-604800),
                            session_id=SID)

    assert schedule.run_now(entry["id"])["ok"] is False
    assert _stored(entry["id"])["run_now_at"] == ""
    assert schedule.queue()["queued"] == []


def test_run_now_at_is_never_inherited(folders, home, spawned):
    """Same rule as `priority`, and for the same reason: "run it now" was said
    about ONE waiting run."""
    _on(home)
    template = schedule.create(str(folders["alpha"]), "daily", None,
                               repeats="*/5 * * * *")
    occurrence = next(e for e in schedule.list_entries()
                      if e.get("template_id") == template["id"])
    assert occurrence["run_now_at"] == ""

    schedule._update(occurrence["id"], run_now_at=_ago(5).isoformat())
    schedule.cancel(occurrence["id"])
    # a restored occurrence comes back waiting for its own time, not for now
    assert schedule.restore(occurrence["id"])["run_now_at"] == ""

    schedule._update(occurrence["id"], run_now_at=_ago(5).isoformat(),
                     state=schedule.SENT, turn="ok")
    schedule._materialize(schedule._now() + timedelta(minutes=10))
    fresh = [e for e in schedule.list_entries()
             if e.get("template_id") == template["id"]
             and e["state"] == schedule.PENDING]
    assert fresh and all(e["run_now_at"] == "" for e in fresh)

    # …and asking again is a new message, which nobody has pressed Run now on
    schedule._update(occurrence["id"], state=schedule.SENT, turn="failed",
                     claude_session_id=SID)
    assert schedule.resend(occurrence["id"])["entry"]["run_now_at"] == ""


# ================================================ dispatch into a live host


class _HostAgent:
    """The two calls the host dispatch makes on agent.py: "is there a session I
    can hand a follow-up to" and "here it is"."""

    def __init__(self, runs, run_id="", answer=None):
        # An empty runs tree, because `holders()` walks it on the same pass.
        self.RUNS = str(runs)
        self.run_id = run_id
        self.answer = answer if answer is not None else {"sent": True}
        self.live_host_calls = []
        self.sends = []
        self.cancels = []

    def _alive(self, run_dir):
        return False

    def _permissions(self, run_dir):
        return []

    def _live_host(self, file, session_id="", limit=None):
        self.live_host_calls.append((file, session_id))
        return {"run_id": self.run_id}

    def _send(self, run_id, message, read_dirs="", model="", effort="",
              permission_mode=""):
        self.sends.append({"run_id": run_id, "message": message,
                           "read_dirs": read_dirs, "model": model,
                           "effort": effort, "permission_mode": permission_mode})
        return dict(self.answer)

    def _cancel(self, run_id, interrupt_first=True):
        self.cancels.append(run_id)
        return {"ok": True}

    def _attach_dirs(self, raw):
        """The real one validates and normalises; what matters to the dispatch
        is that the string it hands `_send` and the string it checks against
        `host.json` go through the SAME function, which is this seam."""
        return list(json.loads(raw)) if raw else []


@pytest.fixture()
def host(tmp_path, monkeypatch):
    """The agent module `_host_send` loads, replaceable per case — plus the
    `host.json` the dispatch reads before it hands a message over.

    That file is the host's record of what it was SPAWNED with, so the defaults
    are what these cases send into it (`opus`, `high`, no extra read dirs) and a
    case that wants a mismatch names its own. A run with no `host.json` at all
    is a host we cannot reason about, which is the spawn (the case below)."""
    made = {}
    runs = tmp_path / "host-runs"
    runs.mkdir()

    def use(run_id="", answer=None, read_dirs=(), model="opus", effort="high",
            host_json=True):
        agent = _HostAgent(runs, run_id, answer)
        if run_id and host_json:
            run_dir = runs / run_id
            run_dir.mkdir(exist_ok=True)
            (run_dir / "host.json").write_text(json.dumps(
                {"pid": os.getpid(), "read_dirs": list(read_dirs),
                 "model": model, "effort": effort, "mode": "prompt"}))
        made["agent"] = agent
        monkeypatch.setattr(pq, "agent_module", lambda: agent)
        return agent

    return use


def _shot(home, name="a1b2c3d4.png"):
    """One real file where the upload endpoint would have put it."""
    root = home / "task-shots"
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_bytes(b"\x89PNG")
    return str(path)


def test_a_message_for_a_live_host_goes_into_its_inbox_not_a_new_process(
        folders, home, spawned, host):
    """AKSHIL'S BUG, the scheduler's half. A chat whose host is up and idle
    between turns had a scheduled message for its own session spawned BESIDE it
    — two `claude` processes on one session id, two writers on one transcript.
    The chat's own composer has never done that: a follow-up goes into the live
    host's inbox and is absorbed. This is that path, taken by the scheduler."""
    _on(home)
    agent = host(run_id="run-live")
    entry = schedule.create(str(folders["alpha"]), "carry on", _ago(1),
                            session_id=SID, model="opus", effort="high")

    assert [e["id"] for e in schedule.tick()] == [entry["id"]]
    assert spawned == []                                  # nothing was spawned
    assert agent.live_host_calls == [(str(folders["alpha"]), SID)]
    assert agent.sends[0]["run_id"] == "run-live"
    assert "carry on" in agent.sends[0]["message"]
    stored = _stored(entry["id"])
    assert stored["state"] == schedule.SENT
    assert stored["run_id"] == "run-live"
    assert stored["host_sent"] is True


def test_a_host_that_died_under_us_falls_back_to_the_spawn(folders, home,
                                                           spawned, host):
    """The ONE thing that still spawns for a session that had a host: the host
    was gone by the time the write reached it (`agent._send` answers
    `{"error": …}`, its own liveness check). There is nothing left to race, and
    the message is owed either way. (`{"respawn": True}` cannot come back at
    all now — the send names no read dir and no effort, which are the only two
    things that reach that arm — but a dict without `sent` is a spawn whatever
    it says.)"""
    _on(home)
    host(run_id="run-live", answer={"error": "no live session"})
    entry = schedule.create(str(folders["alpha"]), "carry on", _ago(1),
                            session_id=SID)

    schedule.tick()
    assert [c["session_id"] for c in spawned] == [SID]
    stored = _stored(entry["id"])
    assert stored["run_id"] == "r-1" and "host_sent" not in stored


def test_no_live_host_and_no_session_both_spawn(folders, home, spawned, host):
    """A conversation with no host is the ordinary send, and a fresh
    conversation has no host by definition — neither pays for the lookup twice
    nor changes shape."""
    _on(home)
    agent = host(run_id="")
    fresh = schedule.create(str(folders["alpha"]), "brand new", _ago(1))
    schedule.tick()
    assert [c["session_id"] for c in spawned] == [""]
    assert agent.live_host_calls == []      # no session: never asked

    resumed = schedule.create(str(folders["beta"]), "carry on", _ago(1),
                              session_id=SID2)
    schedule.tick()
    assert [c["session_id"] for c in spawned] == ["", SID2]
    assert agent.live_host_calls == [(str(folders["beta"]), SID2)]
    assert _stored(fresh["id"])["state"] == schedule.SENT
    assert _stored(resumed["id"])["state"] == schedule.SENT


def test_with_the_flag_off_a_live_host_is_never_asked(folders, home, spawned,
                                                      host):
    """Flag off is byte-for-byte what shipped: the scheduler spawns, and
    agent.py is not even loaded."""
    _on(home, False)
    agent = host(run_id="run-live")
    schedule.create(str(folders["alpha"]), "carry on", _ago(1), session_id=SID)

    schedule.tick()
    assert [c["session_id"] for c in spawned] == [SID]
    assert agent.live_host_calls == [] and agent.sends == []


def test_a_host_that_raises_is_a_spawn(folders, home, spawned, monkeypatch, host):
    """Best-effort: a template we cannot reach is the send the scheduler has
    always made, never a failed entry."""
    _on(home)
    agent = host(run_id="run-live")

    def boom(file, session_id="", limit=None):
        raise RuntimeError("no agent here")

    monkeypatch.setattr(agent, "_live_host", boom)
    entry = schedule.create(str(folders["alpha"]), "carry on", _ago(1),
                            session_id=SID)

    schedule.tick()
    assert [c["session_id"] for c in spawned] == [SID]
    assert _stored(entry["id"])["state"] == schedule.SENT


def test_nothing_of_the_entrys_own_settings_is_carried_into_a_live_host(
        folders, home, spawned, host):
    """BUGBOT, 2026-09-12. A guest changes NOTHING about the house it walks
    into — all four of the settings `agent._send` can act on go in empty, even
    when the entry names one:

    * `permission_mode` becomes a `set_permission_mode` the CLI keeps for the
      rest of the session, and every entry carries one (`create` fills the
      field with this module's `auto`, BROADER than a chat's default
      `prompt`) — a scheduled follow-up was loosening a live chat's
      permissions and leaving them loosened;
    * `model` is the same shape (`set_model`, applied mid-session and kept);
    * `effort` and `read_dirs` are fixed at spawn, so naming either makes
      `_send` TREE-KILL the chat's session to force a respawn.

    Empty means "whatever this chat is already set to", which is the only thing
    a guest may say — and, since the two respawn triggers are never named, the
    host can never be killed by this path either. A spawn keeps the entry's
    mode (the case below); a message into somebody's live session does not."""
    _on(home)
    agent = host(run_id="run-live")
    schedule.create(str(folders["alpha"]), "carry on", _ago(1), session_id=SID,
                    permission_mode="plan", model="opus", effort="high")

    schedule.tick()
    assert agent.sends[0]["permission_mode"] == ""
    assert agent.sends[0]["model"] == "" and agent.sends[0]["effort"] == ""
    assert agent.sends[0]["read_dirs"] == ""
    assert spawned == [] and agent.cancels == []


def test_a_spawn_still_carries_the_entrys_mode(folders, home, spawned, host,
                                               monkeypatch):
    """The control: with no live host the send is a process of its own, and it
    is started in the mode the entry asked for, exactly as before."""
    _on(home)
    host(run_id="")
    modes = []
    real = claude_spawn.spawn_helper

    def spy(target, prompt, permission_mode, session_id="", **kw):
        modes.append(permission_mode)
        return real(target, prompt, permission_mode, session_id, **kw)

    monkeypatch.setattr(claude_spawn, "spawn_helper", spy)
    schedule.create(str(folders["alpha"]), "go", _ago(1),
                    permission_mode="plan")

    schedule.tick()
    assert modes == ["plan"]


def test_an_attachment_the_host_was_not_granted_goes_in_without_the_grant(
        folders, home, spawned, host):
    """BUGBOT HIGH, 2026-09-12. The round before this one read the host's grant
    off `host.json` and took the SPAWN when the directory was missing — a
    `claude --resume` on that session while its idle host was still alive, i.e.
    the two-writers bug this whole path exists to prevent, arriving through the
    guard meant to stop it. A live host always wins: the message goes in with no
    new grant asked for (which is also what keeps `_send` off the tree-kill),
    and an image the host cannot read raises an ordinary permission card in a
    chat that is open with the user in front of it."""
    _on(home)
    agent = host(run_id="run-live")                      # granted nothing
    entry = schedule.create(str(folders["alpha"]), "look at this", _ago(1),
                            session_id=SID, images=[_shot(home)])

    schedule.tick()
    assert spawned == [] and agent.cancels == []
    assert agent.sends[0]["run_id"] == "run-live"
    assert agent.sends[0]["read_dirs"] == ""
    assert _stored(entry["id"])["host_sent"] is True


def test_an_attachment_the_host_already_has_still_asks_for_no_grant(
        folders, home, spawned, host):
    """…and a host that WAS spawned with the task-shots directory takes the
    same send, byte for byte: the grant is not a decision this path makes any
    more, so both hosts get the identical empty string and neither is
    respawned. (The difference between them survives only as a debug line
    counting what the guest gave up.)"""
    _on(home)
    agent = host(run_id="run-live", read_dirs=[schedule.shots_dir()])
    entry = schedule.create(str(folders["alpha"]), "look at this", _ago(1),
                            session_id=SID, images=[_shot(home)])

    schedule.tick()
    assert spawned == []
    assert agent.sends[0]["read_dirs"] == ""
    assert _stored(entry["id"])["host_sent"] is True


def test_an_effort_the_host_was_not_started_with_is_left_behind(
        folders, home, spawned, host):
    """Effort is fixed at spawn — there is no control request for it — so
    naming one the host lacks is what USED to force the respawn, and then (the
    round before this) the spawn beside a live host. The entry's effort is worth
    a process of its own only when there is no session to join; where there is
    one, the turn runs at the effort that session already has."""
    _on(home)
    agent = host(run_id="run-live", effort="medium")
    schedule.create(str(folders["alpha"]), "think harder", _ago(1),
                    session_id=SID, effort="high")

    schedule.tick()
    assert spawned == [] and agent.cancels == []
    assert agent.sends[0]["effort"] == ""


def test_a_model_the_chat_is_not_on_is_left_behind_too(folders, home, spawned,
                                                       host):
    """`set_model` is applied MID-SESSION and kept, so carrying the entry's
    model would leave somebody's chat on another model for every turn after
    this one — and spawning instead would put a second process on their
    transcript. Neither: the message runs on the model the chat is on."""
    _on(home)
    agent = host(run_id="run-live", model="sonnet")
    schedule.create(str(folders["alpha"]), "carry on", _ago(1), session_id=SID,
                    model="opus")

    schedule.tick()
    assert spawned == []
    assert agent.sends[0]["run_id"] == "run-live"
    assert agent.sends[0]["model"] == ""


def test_a_host_with_no_host_json_is_still_a_live_host(folders, home, spawned,
                                                       host):
    """`host.json` no longer decides anything: `_live_host` said there is a
    session to hand a follow-up to, and that is the whole question. A record we
    cannot read is not a reason to start a second process on that session —
    the only spawn left is "no host at all"."""
    _on(home)
    agent = host(run_id="run-live", host_json=False)
    schedule.create(str(folders["alpha"]), "carry on", _ago(1), session_id=SID)

    schedule.tick()
    assert spawned == []
    assert agent.sends[0]["run_id"] == "run-live"


def _cancel_requested(monkeypatch):
    """Every `_report` call, with the manager's ✕ pressed on every read-back."""
    reports = []

    def report(entry_id, **fields):
        reports.append((entry_id, fields))
        return {"cancel_requested": True}

    monkeypatch.setattr(schedule, "_report", report)
    return reports


def test_a_host_sent_entry_is_not_cancellable(folders, home, spawned, host,
                                              monkeypatch):
    """BUGBOT HIGH, 2026-09-12. The run id on a host-sent entry is the CHAT'S
    session host — the process the user has a page open on — so the manager's ✕
    must not be offered for it at all. `agent._cancel` would end their live
    session to withdraw one scheduled follow-up."""
    _on(home)
    host(run_id="run-live")
    entry = schedule.create(str(folders["alpha"]), "carry on", _ago(1),
                            session_id=SID)
    reports = _cancel_requested(monkeypatch)

    schedule.tick()
    opened = next(f for _id, f in reports if f.get("state") == "running")
    assert opened["cancellable"] is False
    assert _stored(entry["id"])["host_sent"] is True


def test_a_spawned_entry_is_cancellable_as_it_always_was(folders, home,
                                                         spawned, host,
                                                         monkeypatch):
    """The control, and the reason the flag is written only when it is true: a
    send this module actually started is a process this module can stop, and
    nothing about that entry changes."""
    _on(home)
    host(run_id="")                       # no live host: the ordinary spawn
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))
    reports = _cancel_requested(monkeypatch)

    schedule.tick()
    opened = next(f for _id, f in reports if f.get("state") == "running")
    assert opened["cancellable"] is True
    assert "host_sent" not in _stored(entry["id"])


def test_a_cancel_on_a_host_sent_entry_leaves_the_chats_session_alone(
        folders, home, spawned, host, monkeypatch):
    """The other end of the same fix. A `cancel_requested` flag reaching a
    host-sent entry — a stale registry row, a client that ignored
    `cancellable` — stops the WATCH and records the entry as cancelled, and the
    live session it was written into is never touched."""
    _on(home)
    agent = host(run_id="run-live")
    entry = schedule.create(str(folders["alpha"]), "carry on", _ago(1),
                            session_id=SID)
    _cancel_requested(monkeypatch)
    schedule.tick()

    stored = _stored(entry["id"])
    assert schedule._turn_tick(dict(stored), "run-live", agent, {}) is False
    assert agent.cancels == []                      # the chat is left alone
    assert _stored(entry["id"])["turn"] == "cancelled"


def test_a_cancel_on_a_spawned_entry_still_stops_the_run(folders, home,
                                                         spawned, host,
                                                         monkeypatch):
    """The control again: a run this module spawned is stopped by the ✕, which
    is what makes the manager's button an action rather than a decoration."""
    _on(home)
    agent = host(run_id="")
    entry = schedule.create(str(folders["alpha"]), "go", _ago(1))
    _cancel_requested(monkeypatch)
    schedule.tick()

    stored = _stored(entry["id"])
    assert schedule._turn_tick(dict(stored), stored["run_id"], agent, {}) is False
    assert agent.cancels == [stored["run_id"]]
    assert _stored(entry["id"])["turn"] == "cancelled"


# =============================================== origin — who asked for this

# WHY THE FIELD EXISTS (Akshil, 2026-09-12). A message somebody SCHEDULED into a
# conversation still shuts that chat's composer until it goes — the scheduler is
# about to send it into this very session, and a line typed over it is two
# messages racing into one run, which is the pre-queue behaviour and the one the
# user asked to keep. A message the CHAT itself queued is that conversation's own
# next line, ordered by the admission that stored it, and it must never block.
# Both are the same pending entry in the same store, so the store has to be able
# to tell them apart: `origin` is that one word, written by the admission
# endpoint and by nothing else.


def test_origin_is_stored_only_when_there_is_one(folders):
    """The absence IS the meaning — "nobody's chat queued this" — so an ordinary
    spawn's entry is main's, field for field, and a stored `"origin": ""` would
    be the same sentence said in a way that rewrites every entry on disk."""
    plain = schedule.create(str(folders["alpha"]), "go", _ago(60))
    assert "origin" not in plain

    chat = schedule.create(str(folders["alpha"]), "go", _ago(60), origin="chat")
    assert chat["origin"] == "chat"
    # Normalised like every other string the router hands this module: None and
    # blank space are the one thing they both mean.
    assert "origin" not in schedule.create(str(folders["alpha"]), "go",
                                           _ago(60), origin="   ")
    assert "origin" not in schedule.create(str(folders["alpha"]), "go",
                                           _ago(60), origin=None)


def test_origin_survives_the_store(folders):
    """The client reads it off `/api/schedule`, which serializes the store's
    dicts as they are — so a field that did not come back out of a read would be
    a field the composer could not see."""
    chat = schedule.create(str(folders["alpha"]), "go", _ago(60), origin="chat")
    plain = schedule.create(str(folders["alpha"]), "go", _ago(59))

    stored = {e["id"]: e for e in schedule.list_entries()}
    assert stored[chat["id"]]["origin"] == "chat"
    assert "origin" not in stored[plain["id"]]


def test_a_materialized_occurrence_is_born_without_an_origin(folders):
    """A repeat is a SCHEDULE, whoever first typed it: every run of it is a
    message the calendar put there, and one that inherited "chat" would leave a
    conversation's composer open to a message nothing ordered against it."""
    template = schedule.create(str(folders["alpha"]), "nightly",
                               repeats="0 3 * * *", origin="chat")
    assert template["origin"] == "chat"

    occurrence = next(e for e in schedule.list_entries()
                      if e.get("template_id") == template["id"])
    assert "origin" not in occurrence


def test_restore_invents_no_origin(folders):
    """Un-skipping is a decision about WHEN a run happens and about nothing
    else; it has no more business minting a provenance than it has rewriting the
    message."""
    template = schedule.create(str(folders["alpha"]), "nightly",
                               repeats="0 3 * * *")
    occurrence = next(e for e in schedule.list_entries()
                      if e.get("template_id") == template["id"])
    schedule.cancel(occurrence["id"])

    assert "origin" not in schedule.restore(occurrence["id"])


def test_a_resend_of_a_chats_message_is_still_the_chats_message(folders,
                                                                spawned):
    """Asking again is the same ask: the composer that stayed open for the first
    send has no reason to shut for the second, and the re-ask is filed under the
    same conversation."""
    original = schedule.create(str(folders["alpha"]), "go", _ago(600),
                               origin="chat")
    schedule._update(original["id"], state=schedule.SENT,
                     claude_session_id=SID, fired=_ago(590).isoformat())

    answer = schedule.resend(original["id"])

    assert answer["ok"] is True
    assert answer["entry"]["origin"] == "chat"
    # …and a re-ask of a scheduled message stays a scheduled message.
    plain = schedule.create(str(folders["alpha"]), "go", _ago(600))
    schedule._update(plain["id"], state=schedule.SENT,
                     claude_session_id=SID2, fired=_ago(590).isoformat())
    assert "origin" not in schedule.resend(plain["id"])["entry"]


def test_a_run_now_stamp_is_not_read_once_the_flag_is_off(folders, home, spawned,
                                                          monkeypatch):
    """Flag-off audit (2026-09-12): a message asked for now while its folder was
    busy carries `run_now_at`; if the queue is then turned OFF, that stamp must
    not fire a message due next Tuesday on the first flag-off tick. Off, an
    entry is due when `due` says."""
    (home / "prefs.json").write_text(json.dumps({"project_queue_enabled": True}))
    live = {pq.queue_key(str(folders["alpha"])): {"session_id": "other",
                                                  "run_id": "r-x",
                                                  "task_key": "other",
                                                  "kind": "run"}}
    monkeypatch.setattr(pq, "holders", lambda now=None: dict(live))
    entry = schedule.create(str(folders["alpha"]), "tomorrow", _ago(-86400))
    assert schedule.run_now(entry["id"])["reason"] == "queued"
    assert _stored(entry["id"])["run_now_at"]

    (home / "prefs.json").write_text(json.dumps({"project_queue_enabled": False}))
    monkeypatch.setattr(pq, "holders", lambda now=None: {})
    assert schedule.queue()["queued"] == []          # not in the line any more
    assert schedule.tick() == []                     # and not sent
    assert _stored(entry["id"])["state"] == schedule.PENDING

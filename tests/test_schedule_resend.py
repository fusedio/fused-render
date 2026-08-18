"""Ask again (`POST /api/schedule/resend`).

Run-now claims a PENDING entry, so the one case the user actually asked for a
Re-run button in — a task that ran and broke — could not be served by it at all:
the run that failed spent its message and there is nothing left to claim. This
file pins the verb that closes that, and every rule is a consequence of what the
verb MEANS.

**It is not "run that row again", it is "ask again".** A task is a thread and
asking for the work a second time is another message in it. So:

* **the original entry is not touched.** Its state, its `due`, its `fired` and
  its error all stand — history goes on saying that run happened and broke. Same
  principle as run-now not moving `due`.
* **the new entry is an ordinary one-off**, pending at now, created and claimed
  and sent by exactly the paths every other message uses. No second spawn.
* **it continues the same thread**, resuming the session the original actually
  ran in (`claude_session_id`), stamped as an id the system learned rather than
  one a user chose.
* **an occurrence's template is untouched.** The new entry carries no
  `template_id`: a button press is not a scheduled run of the rule, and counting
  it as one would block materialization, feed the coalescer and corrupt the
  `count` budget.
* **only a run that WENT AND ENDED may be re-sent** — `sent` or `error`.
  Everything else is refused with a sentence saying what to do instead.

Frozen clocks; nothing here sleeps.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_spawn, schedule, schedule_wake
from fused_render.server import create_app

WRITE = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def unbounded(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE", raising=False)


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture(autouse=True)
def clean_event_log():
    schedule._events.clear()
    yield
    schedule._events.clear()


@pytest.fixture(autouse=True)
def fresh_process():
    schedule._watched.clear()
    yield
    schedule._watched.clear()


@pytest.fixture(autouse=True)
def nothing_is_live(monkeypatch):
    """No transcript on disk anywhere, so no session reads as mid-turn."""
    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: False)


@pytest.fixture()
def spawned(monkeypatch):
    """Every spawn, WITH the store's own view of the entry at the moment of the
    spawn — which is what makes claim-before-spawn testable from here rather
    than merely asserted about."""
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id=""):
        claimed = [e["state"] for e in schedule._read()
                   if e.get("fired") and not e.get("run_id")]
        calls.append({"message": prompt, "session_id": session_id,
                      "target": target, "permission_mode": permission_mode,
                      "states_at_spawn": claimed})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _at(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _entries():
    return {e["id"]: e for e in schedule.list_entries()}


def _ran_and_broke(target, message="pull the news", session="sess-ran",
                   **extra):
    """One message that went out and whose turn then failed — the exact shape
    the Re-run button is for."""
    entry = schedule.create(str(target), message, _at(-60), **extra)
    schedule._update(entry["id"], state=schedule.SENT, fired=_at(-55).isoformat(),
                     run_id="r-old", turn="failed", error="the turn died",
                     claude_session_id=session)
    return _entries()[entry["id"]]


# ------------------------------------------------------- the original stands


def test_resend_leaves_the_original_exactly_as_it_was(target, spawned):
    """The headline contract. A run that happened and broke goes on saying so:
    nothing about the old row is rewritten, not even to tidy it."""
    original = _ran_and_broke(target)
    before = dict(original)

    result = schedule.resend(original["id"])
    assert result["ok"] is True

    after = _entries()[original["id"]]
    assert after == before, "the original entry is read, never written"
    assert after["state"] == schedule.SENT
    assert after["error"] == "the turn died"
    assert after["turn"] == "failed"


def test_the_new_entry_is_an_ordinary_one_off_due_now(target, spawned):
    """A new MESSAGE, with the original's words, due the moment it was asked
    for — not a rewrite of the row that failed."""
    original = _ran_and_broke(target, "pull the news")
    schedule._update(original["id"], title="News", description="every morning",
                     permission_mode="acceptEdits")
    original = _entries()[original["id"]]
    now = datetime.now(timezone.utc)

    fresh = schedule.resend(original["id"], now)["entry"]

    assert fresh["id"] != original["id"]
    assert fresh["message"] == "pull the news"
    assert fresh["target"] == original["target"]
    assert fresh["title"] == "News"
    assert fresh["description"] == "every morning"
    assert fresh["permission_mode"] == "acceptEdits"
    assert schedule.parse_due(fresh["due"]) == now, "due is now, not the old time"
    assert fresh["repeats"] == "" and fresh["rule"] is None
    assert not fresh.get("new_task_each_run")
    # Provenance, and the only link between the two rows — the original is not
    # written to, so nothing else records that this is a re-ask of it.
    assert fresh["resent_from"] == original["id"]
    # It went, through the ordinary send.
    assert fresh["state"] == schedule.SENT
    assert len(spawned) == 1
    assert spawned[0]["message"] == "pull the news"
    assert spawned[0]["permission_mode"] == "acceptEdits"


def test_the_re_ask_resumes_the_conversation_the_original_ran_in(target,
                                                                 spawned):
    """`claude_session_id` is the only field that knows which thread the run
    landed in, and it is what the new message's `session_id` is copied from —
    so the re-ask continues that conversation instead of opening a second one
    beside it."""
    original = _ran_and_broke(target, session="sess-thread")

    fresh = schedule.resend(original["id"])["entry"]

    assert fresh["session_id"] == "sess-thread"
    # Learned by the system from a run, not chosen by a user — the same marker
    # `_chain_session` writes for the same fact.
    assert fresh["session_learned"] is True
    assert spawned[0]["session_id"] == "sess-thread", "it resumes, not restarts"


def test_a_send_that_never_reached_a_session_starts_a_fresh_thread(target,
                                                                    spawned):
    """The honest answer when there is no thread to continue: a message that
    failed before Claude Code minted a session has nothing to resume."""
    entry = schedule.create(str(target), "go", _at(-60))
    schedule._update(entry["id"], state=schedule.ERROR,
                     error="failed to start session")

    fresh = schedule.resend(entry["id"])["entry"]
    assert fresh["session_id"] == ""
    assert fresh["session_learned"] is False
    assert spawned[0]["session_id"] == ""


# ------------------------------------------------------ one way to spawn


def test_the_new_message_is_claimed_before_it_is_spawned(target, spawned):
    """Claim-before-spawn, reached from the new door. The entry is written
    `sending` BEFORE the helper is away — read out of the store from inside the
    spawn itself, so this is the real order and not a restatement of it."""
    original = _ran_and_broke(target)
    schedule.resend(original["id"])

    assert spawned[0]["states_at_spawn"] == [schedule.SENDING]


def test_a_tick_after_a_resend_does_not_send_it_again(target, spawned):
    """The other half: the new entry is left in a state the sweep will not act
    on, so the two paths cannot both send it."""
    original = _ran_and_broke(target)
    schedule.resend(original["id"])
    assert len(spawned) == 1

    assert schedule.tick(now=datetime.now(timezone.utc)) == []
    assert len(spawned) == 1


def test_a_busy_conversation_queues_the_re_ask_rather_than_refusing_it(
        target, spawned, monkeypatch):
    """`ok` is true once the message EXISTS, not only when it went out. If the
    thread it resumes has a turn open, the ordinary hold applies: the entry
    stays pending at the head of the queue and the tick sends it when the turn
    ends. Calling that a failure would be a lie about a message that is really
    scheduled — so it comes back as a note beside `ok: true`."""
    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: session == "sess-busy")
    original = _ran_and_broke(target, session="sess-busy")

    result = schedule.resend(original["id"])
    assert result["ok"] is True
    assert "turn running right now" in result["reason"]
    assert result["entry"]["state"] == schedule.PENDING
    assert spawned == []

    # ...and it goes on its own once the conversation is quiet.
    monkeypatch.setattr(schedule, "_session_live",
                        lambda session, now, seen=None: False)
    schedule.tick(now=datetime.now(timezone.utc))
    assert len(spawned) == 1
    assert spawned[0]["session_id"] == "sess-busy"


# ------------------------------------------- a recurring rule is not disturbed


def test_re_sending_one_occurrence_does_not_count_as_a_run_of_the_rule(target,
                                                                       spawned):
    """The `template_id` decision, pinned. A re-send is a manual re-ask, not a
    scheduled run: the new entry belongs to no template, so the series' budget,
    its `due` and its next occurrence are all exactly where they were."""
    template = schedule.create(str(target), "daily", due=_at(3600),
                               rule={"freq": "day"})
    occurrence = next(e for e in _entries().values()
                      if str(e.get("template_id") or "") == template["id"])
    # Run it, and let its turn break — the ordinary way an occurrence ends up
    # wanting a re-ask.
    assert schedule.run_now(occurrence["id"])["ok"] is True
    schedule._update(occurrence["id"], turn="failed", error="the turn died",
                     claude_session_id="sess-occ")
    before = _entries()
    made_before = before[template["id"]]["made"]
    due_before = before[template["id"]]["due"]
    rule_before = before[template["id"]]["rule"]

    fresh = schedule.resend(occurrence["id"])["entry"]

    assert fresh.get("template_id", "") == "", "not an occurrence of anything"
    after = _entries()
    assert after[template["id"]]["made"] == made_before
    assert after[template["id"]]["due"] == due_before
    assert after[template["id"]]["rule"] == rule_before
    assert after[occurrence["id"]]["due"] == occurrence["due"]
    assert after[occurrence["id"]]["state"] == schedule.SENT

    # And the successor still arrives exactly where the rule put it — a day
    # after the occurrence's own (unmoved) due time. If the re-send had carried
    # `template_id`, materialization would have been blocked while it was
    # pending and the coalescer would have read it as backlog.
    schedule.tick(now=datetime.now(timezone.utc))
    following = [e for e in _entries().values()
                 if str(e.get("template_id") or "") == template["id"]
                 and e["state"] == schedule.PENDING]
    assert len(following) == 1
    step = (schedule.parse_due(following[0]["due"])
            - schedule.parse_due(occurrence["due"]))
    assert step == timedelta(days=1)


def test_cancelling_the_template_does_not_reach_a_re_send(target, spawned):
    """The other half of carrying no `template_id`: "stop this recurring job"
    means no further RUNS OF THE RULE, and a manual re-ask is not one of
    those."""
    template = schedule.create(str(target), "daily", due=_at(3600),
                               rule={"freq": "day"})
    occurrence = next(e for e in _entries().values()
                      if str(e.get("template_id") or "") == template["id"])
    schedule.run_now(occurrence["id"])
    schedule._update(occurrence["id"], turn="failed",
                     claude_session_id="sess-occ")
    # Queued rather than sent, so there is something for the cascade to reach.
    schedule._update(occurrence["id"], turn="")
    fresh = schedule.resend(occurrence["id"])["entry"]
    schedule._update(occurrence["id"], turn="failed")

    schedule.cancel(template["id"])
    assert _entries()[fresh["id"]]["state"] != schedule.CANCELLED


# --------------------------------------------------------------- the refusals


@pytest.mark.parametrize("state,expected", [
    (schedule.PENDING, "not sent yet"),
    (schedule.SENDING, "already sending"),
    (schedule.CANCELLED, "cancelled"),
    (schedule.MISSED, "never ran"),
])
def test_resend_refuses_anything_that_did_not_go_and_end(target, spawned,
                                                          state, expected):
    """`sent` and `error` are the two states a message can be re-sent from: a
    run that went and ended. The live pair point AT run-now instead; the two
    that never went say to schedule it again, because there is no message to
    send AGAIN."""
    entry = schedule.create(str(target), "x", _at(3600))
    schedule._update(entry["id"], state=state)

    result = schedule.resend(entry["id"])
    assert result["ok"] is False
    assert result["found"] is True
    assert expected in result["reason"]
    assert spawned == []
    # Nothing was created, and the entry is where it was.
    assert len(schedule.list_entries()) == 1
    assert _entries()[entry["id"]]["state"] == state


def test_the_pending_refusal_names_run_now(target, spawned):
    """A message that has not gone yet is run-now's job, and the sentence says
    so rather than leaving the user with two buttons and no rule."""
    entry = schedule.create(str(target), "x", _at(3600))
    assert "run it now" in schedule.resend(entry["id"])["reason"]


def test_the_missed_refusal_explains_the_coalescer(target, spawned):
    """The sharp one. `missed`'s commonest source is the coalescer dropping a
    repeat's stale runs, and a re-send that replayed them one at a time would
    undo, click by click, the rule that stops a week of "daily at 9am" landing
    in a thread on Monday morning."""
    entry = schedule.create(str(target), "x", _at(3600))
    schedule._update(entry["id"], state=schedule.MISSED)
    reason = schedule.resend(entry["id"])["reason"]
    assert "latest missed run of a repeat" in reason


def test_resend_refuses_a_recurring_template(target, spawned):
    """A template never ran — its occurrences did."""
    template = schedule.create(str(target), "daily", due=_at(3600),
                               rule={"freq": "day"})
    result = schedule.resend(template["id"])
    assert result["ok"] is False
    assert "repeating schedule" in result["reason"]
    assert spawned == []


def test_resend_on_an_unknown_id_is_not_found(target):
    result = schedule.resend("no-such-entry")
    assert result["ok"] is False
    assert result["found"] is False
    assert "no scheduled message" in result["reason"]


def test_a_sent_message_whose_turn_ended_well_may_still_be_re_sent(target,
                                                                   spawned):
    """"However its turn resolved" is the whole of the rule: asking again for
    work that succeeded is a legitimate ask, and the store has no business
    refusing it."""
    entry = schedule.create(str(target), "again please", _at(-60))
    schedule._update(entry["id"], state=schedule.SENT, turn="ok",
                     claude_session_id="sess-ok")
    result = schedule.resend(entry["id"])
    assert result["ok"] is True
    assert spawned[0]["session_id"] == "sess-ok"


# ----------------------------------------------------------------- the route


def test_the_route_answers_with_the_NEW_entry(client, target, spawned):
    original = _ran_and_broke(target)
    r = client.post("/api/schedule/resend", json={"entry_id": original["id"]},
                    headers=WRITE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["entry"]["id"] != original["id"], "the NEW message, not the old"
    assert body["entry"]["resent_from"] == original["id"]
    assert body["note"] == ""
    assert len(spawned) == 1
    assert _entries()[original["id"]]["state"] == schedule.SENT


def test_the_route_answers_409_with_the_reason_for_something_that_never_went(
        client, target, spawned):
    """409, not 404: the entry exists and the user needs to be told which of the
    several ways it cannot be re-sent applies to it."""
    entry = schedule.create(str(target), "go", _at(3600))
    r = client.post("/api/schedule/resend", json={"entry_id": entry["id"]},
                    headers=WRITE)
    assert r.status_code == 409, r.text
    assert "not sent yet" in r.json()["error"]
    assert spawned == []
    assert len(schedule.list_entries()) == 1


def test_the_route_answers_404_for_an_id_that_is_not_there(client, target):
    r = client.post("/api/schedule/resend", json={"entry_id": "nope"},
                    headers=WRITE)
    assert r.status_code == 404
    assert "no scheduled message" in r.json()["error"]


def test_the_route_needs_an_entry_id(client):
    r = client.post("/api/schedule/resend", json={}, headers=WRITE)
    assert r.status_code == 400
    assert "entry_id" in r.json()["error"]


def test_the_route_400s_when_the_target_has_since_been_deleted(client, target,
                                                               spawned, tmp_path):
    """`create`'s own validation, surfaced as a 400 — a re-ask against a folder
    that no longer exists is a message that could only fail."""
    gone = tmp_path / "gone"
    gone.mkdir()
    original = _ran_and_broke(gone)
    gone.rmdir()

    r = client.post("/api/schedule/resend", json={"entry_id": original["id"]},
                    headers=WRITE)
    assert r.status_code == 400
    assert "no such file or directory" in r.json()["error"]
    assert spawned == []


def test_the_route_carries_the_d3_write_guard(client, target, spawned):
    """It starts an unattended agent turn on the spot — the same member of the
    set the header guard exists for that run-now is."""
    original = _ran_and_broke(target)
    r = client.post("/api/schedule/resend", json={"entry_id": original["id"]})
    assert r.status_code == 403
    assert spawned == []
    assert len(schedule.list_entries()) == 1


def test_the_route_refuses_a_mount_backed_target(client, target, spawned,
                                                  monkeypatch):
    """Re-checked rather than inherited from creation: it passed the gate
    whenever it was scheduled, and a path can become mount-backed after that."""
    from fused_render.shell import mounts

    original = _ran_and_broke(target)
    monkeypatch.setattr(mounts, "is_mount_backed", lambda path: True)

    r = client.post("/api/schedule/resend", json={"entry_id": original["id"]},
                    headers=WRITE)
    assert r.status_code == 400
    assert "remote mount" in r.json()["error"]
    assert spawned == []
    assert len(schedule.list_entries()) == 1

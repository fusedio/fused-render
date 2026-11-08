"""The scheduler defers while a HUMAN turn is live (`session_liveness`).

The gap this closes was written into `tick`'s docstring as known and unfixable:
`_busy_sessions` reads the schedule store, so it knew about the sends the
scheduler had in flight and nothing whatever about the user typing into the same
conversation in the explorer's chat. A scheduled message coming due for session
S while the user was mid-turn in S spawned `claude --resume S` beside the chat's
own process, and two processes appended to one transcript — which, since a task
IS its session, is the one corruption this feature must not be able to cause.

The answer is not a second store. A transcript records the turn without
recording who started it, which is exactly the property needed, and it is the
same read that paints the `running` badge on the Inbox and the Board — extracted
to `fused_render/session_liveness.py` so the scheduler can ask it without
importing a router (schedule.py is below `fused_render.server` and must stay
there).

**Defer, never drop.** A held entry is not touched at all: it stays `pending`,
no state is written, no `missed` verdict is reached, and the tick after the turn
ends sends it. Catch-up is unbounded by default, so waiting costs it nothing.

Real wall-clock instants (the liveness rule is a 45-second window against file
mtimes, so a frozen clock decades away would only prove the fast path), but
nothing here sleeps: "a turn just ended" is written into the transcript as the
record Claude Code writes for it, not waited for.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from fused_render import claude_spawn, schedule, schedule_wake, session_liveness

LIVE_SESSION = "sess-live"


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
def projects_dir(tmp_path, monkeypatch):
    """Nothing reads the real ~/.claude. An empty dir also means "no transcript"
    is the default answer, which is what every other schedule test assumes."""
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(session_liveness, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id="", **kw):
        calls.append({"message": prompt, "session_id": session_id})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _at(seconds: float) -> datetime:
    return _now() + timedelta(seconds=seconds)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_transcript(projects_dir, session_id, records):
    d = projects_dir / ("-encoded-" + session_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _typing(seconds_ago: float = 2):
    """A transcript whose newest record is a real message seconds old — which is
    what a turn in progress looks like from the outside."""
    return [{"type": "user", "timestamp": _iso(_at(-seconds_ago)),
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "hold on"}]}}]


def _turn_over():
    """…and the record Claude Code writes when the turn ends. Newer than any
    real message, so the 45-second window cannot keep the badge lit."""
    return _typing() + [{"type": "system", "subtype": "turn_duration",
                         "timestamp": _iso(_now())}]


def _entries():
    return {e["id"]: e for e in schedule.list_entries()}


# --------------------------------------------------- the rule, on its own


def test_a_transcript_with_a_fresh_message_reads_as_running(projects_dir):
    _write_transcript(projects_dir, LIVE_SESSION, _typing())
    assert session_liveness.session_running(LIVE_SESSION, _now().timestamp())


def test_a_transcript_whose_turn_just_ended_reads_as_idle(projects_dir):
    """The `turn_duration` record is an explicit end marker, and the reason
    mtime alone cannot answer this: the file was touched a moment ago by the
    very record that says the work is over."""
    _write_transcript(projects_dir, LIVE_SESSION, _turn_over())
    assert not session_liveness.session_running(LIVE_SESSION, _now().timestamp())


def test_a_session_with_no_transcript_is_not_running(projects_dir):
    assert not session_liveness.session_running("never-existed",
                                                _now().timestamp())


def test_a_session_id_that_is_not_an_id_cannot_glob_out_of_the_tree(
        projects_dir):
    """The id is used as a glob pattern, so anything that is not one is refused
    rather than trusted to be harmless."""
    _write_transcript(projects_dir, LIVE_SESSION, _typing())
    assert not session_liveness.session_running("*", _now().timestamp())
    assert session_liveness.transcript_path("../../etc/passwd") == ""


# ------------------------------------------------------- the scheduler's use


def test_a_due_message_waits_while_the_user_is_mid_turn(target, spawned,
                                                        projects_dir):
    """Nothing spawns, and — the important half — nothing is written either.
    The entry is exactly as it was, which is what makes this a wait."""
    _write_transcript(projects_dir, LIVE_SESSION, _typing())
    entry = schedule.create(str(target), "the scheduled one", _at(-60),
                            session_id=LIVE_SESSION)

    assert schedule.tick(now=_now()) == []
    assert spawned == []
    held = _entries()[entry["id"]]
    assert held["state"] == schedule.PENDING
    assert held["fired"] == ""
    assert held["error"] == ""


def test_it_goes_on_a_later_tick_once_the_turn_ends(target, spawned,
                                                    projects_dir):
    """Deferred, not dropped. The same entry, the same tick loop, one turn
    later."""
    _write_transcript(projects_dir, LIVE_SESSION, _typing())
    entry = schedule.create(str(target), "the scheduled one", _at(-60),
                            session_id=LIVE_SESSION)
    assert schedule.tick(now=_now()) == []

    _write_transcript(projects_dir, LIVE_SESSION, _turn_over())
    fired = schedule.tick(now=_now())
    assert [e["id"] for e in fired] == [entry["id"]]
    assert len(spawned) == 1
    assert spawned[0]["session_id"] == LIVE_SESSION
    assert _entries()[entry["id"]]["state"] == schedule.SENT


def test_waiting_never_becomes_missed(target, spawned, projects_dir):
    """Catch-up is unbounded, so an entry held back by a conversation that will
    not go quiet simply keeps waiting. `missed` would be this module blaming the
    user for talking, and it would be a verdict on a message that was never
    even attempted."""
    _write_transcript(projects_dir, LIVE_SESSION, _typing())
    entry = schedule.create(str(target), "patient", _at(-30 * 86400),
                            session_id=LIVE_SESSION)

    for _ in range(5):
        assert schedule.tick(now=_now()) == []
    assert _entries()[entry["id"]]["state"] == schedule.PENDING
    assert [e for e in schedule.event_log()
            if e["kind"] == schedule.EVENT_MISSED] == []
    # It is still QUEUED, not expired — the popover keeps offering to cancel it.
    assert [e["id"] for e in schedule.queue()["queued"]] == [entry["id"]]


def test_a_fresh_session_message_is_never_held(target, spawned, projects_dir):
    """`session_id` "" starts a new conversation, so it collides with nothing —
    including a live turn in someone else's."""
    _write_transcript(projects_dir, LIVE_SESSION, _typing())
    entry = schedule.create(str(target), "a fresh one", _at(-60))

    fired = schedule.tick(now=_now())
    assert [e["id"] for e in fired] == [entry["id"]]
    assert len(spawned) == 1


def test_a_message_for_a_quiet_session_goes_while_another_is_busy(
        target, spawned, projects_dir):
    """The hold is per session, not global: one live conversation must not stop
    the whole schedule."""
    _write_transcript(projects_dir, LIVE_SESSION, _typing())
    _write_transcript(projects_dir, "sess-quiet", _turn_over())
    held = schedule.create(str(target), "held", _at(-120),
                           session_id=LIVE_SESSION)
    goes = schedule.create(str(target), "goes", _at(-60),
                           session_id="sess-quiet")

    fired = schedule.tick(now=_now())
    assert [e["id"] for e in fired] == [goes["id"]]
    assert _entries()[held["id"]]["state"] == schedule.PENDING


def test_an_unreadable_liveness_read_does_not_hold_a_message_for_ever(
        target, spawned, monkeypatch):
    """The failure direction is deliberate: a read this module cannot make must
    not be able to park a message indefinitely, so it answers "not live" and the
    send goes exactly as it did before this check existed."""
    def boom(session_id, now, projects_dir=None):
        raise OSError("no")

    monkeypatch.setattr(session_liveness, "session_running", boom)
    entry = schedule.create(str(target), "goes anyway", _at(-60),
                            session_id=LIVE_SESSION)

    fired = schedule.tick(now=_now())
    assert [e["id"] for e in fired] == [entry["id"]]
    assert len(spawned) == 1


def test_the_liveness_read_is_made_once_per_session_per_tick(target, spawned,
                                                             projects_dir,
                                                             monkeypatch):
    """A batch of messages into one conversation must not stat and tail-read the
    same transcript once each."""
    reads = []
    real = session_liveness.session_running

    def counted(session_id, now, projects_dir=None):
        reads.append(session_id)
        return real(session_id, now, projects_dir)

    monkeypatch.setattr(session_liveness, "session_running", counted)
    _write_transcript(projects_dir, LIVE_SESSION, _typing())
    for n in range(3):
        schedule.create(str(target), f"m{n}", _at(-60 - n),
                        session_id=LIVE_SESSION)

    assert schedule.tick(now=_now()) == []
    assert reads == [LIVE_SESSION]


def test_transcript_path_prefers_the_bucket_holding_a_conversation(projects_dir):
    """One id in two buckets: the one the conversation was moved into
    (EnterWorktree) and the original one the CLI's exit bookkeeping re-created
    with no conversation row. The stub's bucket sorts FIRST here, so the answer
    is decided by the conversation check, not by listing order."""
    sid = "sess-moved"
    stub = projects_dir / "-a-repo" / f"{sid}.jsonl"
    moved = projects_dir / "-z-worktree" / f"{sid}.jsonl"
    stub.parent.mkdir()
    moved.parent.mkdir()
    stub.write_text(json.dumps({"type": "cost-state", "sessionId": sid}) + "\n")
    assert session_liveness.transcript_path(sid) == str(stub)  # nothing better
    moved.write_text(json.dumps({"type": "user", "uuid": "u1", "sessionId": sid,
                                 "message": {"role": "user", "content": "hi"}}) + "\n")
    assert session_liveness.transcript_path(sid) == str(moved)


# ------------------------- a lost watch asks the transcript before "unknown"
#
# When the app quits mid-turn (or the watch hits its tick cap) the scheduler
# used to stamp `turn: unknown` — "this app stopped being able to say". The
# session's transcript usually CAN say, so `_close_unwatched` asks it first
# (`schedule._transcript_verdict`). Only evidence newer than the send counts.

LOST_SESSION = "sess-lost"
_REAL_WATCH_TURN = schedule._watch_turn


def _reply(text="done", **extra):
    return {"type": "assistant", "timestamp": _iso(_now()),
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}, **extra}


def _prompt(text="go", when=None):
    return {"type": "user", "timestamp": _iso(when or _now()),
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def _sent_then_abandoned(target):
    """A message resuming LOST_SESSION, sent, then its watching process gone."""
    schedule.create(str(target), "go", _at(-5), session_id=LOST_SESSION)
    schedule.tick()
    (entry,) = schedule.list_entries()
    assert (entry["state"], entry["turn"]) == (schedule.SENT, "")
    schedule._watched.clear()
    return entry


@pytest.fixture()
def unfollowable(monkeypatch):
    monkeypatch.setattr(schedule, "_followable", lambda run_id: False)


def test_a_turn_that_finished_while_unwatched_reads_ok(
        target, spawned, projects_dir, unfollowable):
    _sent_then_abandoned(target)
    _write_transcript(projects_dir, LOST_SESSION, [_prompt(), _reply()])

    schedule.tick()

    stored = schedule.list_entries()[0]
    assert (stored["turn"], stored["error"]) == ("ok", "")
    assert stored["turn_at"]
    kinds = [e["kind"] for e in schedule.event_log()]
    assert kinds[-1] == schedule.EVENT_DONE


def test_an_interrupt_marker_reads_cancelled(target, spawned, projects_dir,
                                             unfollowable):
    _sent_then_abandoned(target)
    _write_transcript(projects_dir, LOST_SESSION,
                      [_prompt(), _prompt("[Request interrupted by user]")])

    schedule.tick()

    assert schedule.list_entries()[0]["turn"] == "cancelled"
    assert schedule.EVENT_FAILED not in [e["kind"] for e in schedule.event_log()]


def test_an_api_error_row_reads_failed(target, spawned, projects_dir,
                                       unfollowable):
    _sent_then_abandoned(target)
    _write_transcript(projects_dir, LOST_SESSION, [
        _prompt(), _reply("API Error: 529 Overloaded", isApiErrorMessage=True)])

    schedule.tick()

    stored = schedule.list_entries()[0]
    assert stored["turn"] == "failed"
    assert "529" in stored["error"]


def test_no_transcript_still_reads_unknown(target, spawned, projects_dir,
                                           unfollowable):
    _sent_then_abandoned(target)

    schedule.tick()

    stored = schedule.list_entries()[0]
    assert stored["turn"] == "unknown"
    assert "interrupted" in stored["error"]


def test_a_finished_turn_older_than_the_send_is_not_this_turn(
        target, spawned, projects_dir, unfollowable):
    """A resumed session already ends in a finished reply — the PREVIOUS turn's.
    Read without the send-time guard, every lost resume would read `ok`."""
    _sent_then_abandoned(target)
    old = _reply()
    old["timestamp"] = _iso(_at(-600))
    _write_transcript(projects_dir, LOST_SESSION,
                      [_prompt(when=_at(-601)), old])

    schedule.tick()

    assert schedule.list_entries()[0]["turn"] == "unknown"


def _tick_capped_watch(monkeypatch, entry):
    """Run the real watch to its end without a verdict — the ~1h tick cap."""
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: object())
    monkeypatch.setattr(claude_spawn, "record_session_when_ready",
                        lambda agent, run_id, on_tick=None: None)
    schedule._watching(entry["id"], True)
    _REAL_WATCH_TURN(entry, entry["run_id"])


def test_an_open_turn_in_a_live_session_is_readopted_not_unknown(
        target, spawned, projects_dir, monkeypatch):
    from fused_render import tasks_watch

    entry = _sent_then_abandoned(target)
    _write_transcript(projects_dir, LOST_SESSION, [_prompt()])
    monkeypatch.setattr(schedule, "_followable", lambda run_id: True)
    monkeypatch.setattr(tasks_watch, "live_from_registry",
                        lambda sid, transcript_mtime=None: (True, 0.0))

    _tick_capped_watch(monkeypatch, entry)

    stored = schedule.list_entries()[0]
    assert (stored["turn"], stored["error"]) == ("", "")
    assert not schedule._is_watched(entry["id"])

    # …and the next sweep picks the watch back up rather than closing it.
    adopted = []
    monkeypatch.setattr(schedule, "_watch_turn",
                        lambda e, run_id: adopted.append(run_id))
    schedule.tick()
    assert adopted == [entry["run_id"]]
    assert schedule.list_entries()[0]["turn"] == ""


def test_an_open_turn_in_a_live_session_with_no_run_is_rechecked_each_sweep(
        target, spawned, projects_dir, unfollowable, monkeypatch):
    """`_resolve_orphan`'s unfollowable branch reaches the same check: left
    unstamped while the session is live, asked once per sweep, and resolved
    by the first sweep after the transcript closes."""
    from fused_render import tasks_watch

    _sent_then_abandoned(target)
    _write_transcript(projects_dir, LOST_SESSION, [_prompt()])
    monkeypatch.setattr(tasks_watch, "live_from_registry",
                        lambda sid, transcript_mtime=None: (True, 0.0))
    reads = []
    real = session_liveness.session_last_turn
    monkeypatch.setattr(session_liveness, "session_last_turn",
                        lambda sid, projects_dir=None: reads.append(sid) or real(sid))

    schedule.tick()
    schedule.tick()
    stored = schedule.list_entries()[0]
    assert (stored["turn"], stored["error"]) == ("", "")
    assert reads == [LOST_SESSION, LOST_SESSION]   # once per sweep, no spin

    _write_transcript(projects_dir, LOST_SESSION, [_prompt(), _reply()])
    schedule.tick()
    assert schedule.list_entries()[0]["turn"] == "ok"


def test_an_open_turn_nothing_is_writing_reads_unknown(
        target, spawned, projects_dir, monkeypatch):
    """A process that died mid-turn leaves the prompt as the last word for
    ever; open in the file is not running."""
    from fused_render import tasks_watch

    entry = _sent_then_abandoned(target)
    _write_transcript(projects_dir, LOST_SESSION, [_prompt()])
    monkeypatch.setattr(schedule, "_followable", lambda run_id: True)
    monkeypatch.setattr(tasks_watch, "live_from_registry",
                        lambda sid, transcript_mtime=None: (False, 0.0))

    _tick_capped_watch(monkeypatch, entry)

    stored = schedule.list_entries()[0]
    assert stored["turn"] == "unknown"
    assert "stopped reporting" in stored["error"]


def test_session_last_turn_reads_the_last_word_without_a_staleness_ceiling(
        projects_dir):
    path = _write_transcript(projects_dir, "sess-old", [
        _prompt(when=_at(-3600)),
        {**_reply(), "timestamp": _iso(_at(-3599))}])
    import os
    os.utime(path, (_at(-3599).timestamp(), _at(-3599).timestamp()))

    last = session_liveness.session_last_turn("sess-old")
    assert last["state"] == "ok"
    assert abs(last["at"] - _at(-3599).timestamp()) < 2
    # the live rule still calls it closed — it is the window that differs
    assert session_liveness.session_turn_open("sess-old", _now().timestamp()) is False

    tool = {"type": "assistant", "timestamp": _iso(_now()),
            "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash"}]}}
    _write_transcript(projects_dir, "sess-tool", [_prompt(), tool])
    assert session_liveness.session_last_turn("sess-tool")["state"] == "open"
    assert session_liveness.session_last_turn("sess-missing") is None

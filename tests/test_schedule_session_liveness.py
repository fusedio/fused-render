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

    def fake_spawn(target, prompt, permission_mode, session_id=""):
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

"""Tasks over HTTP (server/routers/tasks.py).

`GET /api/tasks` is the List page: every task, newest first, each carrying its
three newest messages. `GET /api/tasks/{key}/messages` is Show more.
`POST /api/tasks/read` marks one message read.

The rules under test are the ones that make a task and a session the same
thing: a scheduled message that fired is the transcript prompt it became (one
message, not two), a task with no session yet is still a row, and the title is
the one Claude Code already writes into the transcript.

Nothing here reads the real ~/.claude — every path is under tmp_path.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import schedule, tasks_store
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(tasks_store, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
    # Both modules keep their state in the SAME global directory — task numbers
    # and read marks next to the triage the sessions router writes.
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "STATE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clear_caches():
    tasks_mod.reset_cache()
    sessions_mod._HEAD_CACHE.clear()
    yield
    tasks_mod.reset_cache()
    sessions_mod._HEAD_CACHE.clear()


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    from fused_render import schedule_wake
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


# ------------------------------------------------------------------ fixtures


def _user(text, ts, uuid=None):
    record = {"type": "user", "timestamp": ts,
              "message": {"role": "user",
                          "content": [{"type": "text", "text": text}]}}
    if uuid is not None:
        record["uuid"] = uuid
    return record


def _assistant(text, ts):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _ai_title(title, session_id="s"):
    return {"type": "ai-title", "aiTitle": title, "sessionId": session_id}


def _write_transcript(projects_dir, session_id, cwd, records, encoded=None):
    d = projects_dir / (encoded or "-encoded-" + session_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    lines = []
    for i, record in enumerate(records):
        record = dict(record)
        record.setdefault("cwd", cwd)
        record.setdefault("sessionId", session_id)
        record.setdefault("uuid", f"{session_id}-{i}")
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")
    return path


def _entry(entry_id, message, due, **fields):
    entry = {"id": entry_id, "target": "/tmp", "message": message, "due": due,
             "session_id": "", "permission_mode": "auto",
             "state": schedule.PENDING, "repeats": "", "rule": None,
             "created": due, "fired": "", "run_id": "", "error": "",
             "turn": "", "claude_session_id": ""}
    entry.update(fields)
    return entry


def _seed_schedule(entries):
    schedule._write(entries)


def _already_using(state_dir):
    """A read store that has already been through its day-one baseline, so
    anything a test writes afterwards counts as genuinely new. Without this
    every message would be below the baseline and therefore read."""
    (state_dir / "read.json").write_text(
        json.dumps({tasks_store.INIT_KEY: 0.0}))


def _tasks(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    return r.json()["tasks"]


def _by_key(client):
    return {t["key"]: t for t in _tasks(client)}


T9 = "2026-08-16T09:00:00Z"
T10 = "2026-08-16T10:00:00Z"
T11 = "2026-08-16T11:00:00Z"
T12 = "2026-08-16T12:00:00Z"


# ------------------------------------------------------------- the plain case


def test_a_chat_session_is_a_task(client, projects_dir, state_dir):
    """No schedule anywhere near it. A session the user typed into is a task —
    the Tasks page absorbs the Inbox, so it lists every session, not just the
    scheduled ones."""
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [
        _user("pull today's news", T9),
        _assistant("done", T10),
        _ai_title("Pull today's news"),
    ])

    tasks = _tasks(client)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["key"] == "sess-a"
    assert task["session_id"] == "sess-a"
    assert task["task_id"] == "TASK-001"
    assert task["project"] == "/home/me/proj"
    assert task["target"] == "/home/me/proj"
    assert task["title"] == "Pull today's news"
    assert task["title_source"] == "ai"
    assert task["description"] == ""
    assert task["status"] == "done"
    assert task["failed"] is False
    assert task["live"] is False
    assert task["message_count"] == 1
    assert [m["message_id"] for m in task["messages"]] == ["MSG-001"]
    message = task["messages"][0]
    assert message["kind"] == "chat"
    assert message["body"] == "pull today's news"
    assert message["entry_id"] == ""
    assert message["anchor"] == "sess-a-0", "the record uuid, for scroll-to"
    assert message["unread"] is True


def test_the_last_ai_title_wins(client, projects_dir):
    """Claude Code re-emits the record every turn and the title tracks the
    conversation — the first one is what the session looked like before it was
    about anything."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("hello", T9),
        _ai_title("Untitled exploration"),
        _user("now build the parser", T10),
        _ai_title("Build the transcript parser"),
    ])
    task = _tasks(client)[0]
    assert task["title"] == "Build the transcript parser"
    assert task["title_source"] == "ai"


def test_the_title_falls_back_to_the_first_message(client, projects_dir):
    """A session with no ai-title yet still needs a name, and the first line of
    the first message is what the user will recognise."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("check the deploy\nand tell me what broke", T9),
        _assistant("ok", T10),
    ])
    task = _tasks(client)[0]
    assert task["title"] == "check the deploy"
    assert task["title_source"] == "message"


def test_an_explicit_user_title_beats_the_ai_one(client, projects_dir):
    """What the user called it wins over what Claude Code called it."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("pull the news", T9), _ai_title("News puller"),
    ])
    _seed_schedule([_entry("e1", "pull the news", T9, state=schedule.SENT,
                           fired=T9, turn="ok", claude_session_id="sess-a",
                           title="Morning briefing")])
    task = _tasks(client)[0]
    assert task["title"] == "Morning briefing"
    assert task["title_source"] == "user"


def test_a_corrupt_line_costs_that_line_and_nothing_else(client, projects_dir):
    """A truncated write — the shape a transcript takes for the seconds a turn
    is in flight — must not take the listing down with it."""
    path = _write_transcript(projects_dir, "sess-a", "/p", [
        _user("first", T9), _ai_title("A title"),
    ])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"type": "user", "message": {"role": "us\n')
        f.write(json.dumps(_user("second", T10, uuid="u2")) + "\n")

    task = _tasks(client)[0]
    assert task["message_count"] == 2
    assert [m["body"] for m in task["messages"]] == ["second", "first"]


def test_the_machinery_claude_code_writes_is_not_a_message(client,
                                                           projects_dir):
    """A finished subagent reporting back, a slash command's name and its
    stdout: `type: user` records the user did not write. On a real machine they
    were a third of everything in the store."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("fix the parser", T9, uuid="u1"),
        _user("<task-notification>\n<task-id>b</task-id>", T10, uuid="u2"),
        _user("<local-command-stdout>Set mode</local-command-stdout>", T11,
              uuid="u3"),
        {"type": "user", "timestamp": T11, "uuid": "u4", "isMeta": True,
         "message": {"role": "user", "content": "Caveat: the messages below"}},
        _user("now ship it <system-reminder>be careful</system-reminder>", T12,
              uuid="u5"),
    ])
    task = _tasks(client)[0]
    assert task["message_count"] == 2
    assert [m["body"] for m in task["messages"]] == [
        "now ship it <system-reminder>be careful</system-reminder>",
        "fix the parser",
    ], "a tag further in leaves a real message real"


def test_an_unreadable_transcript_does_not_hide_the_others(client, projects_dir):
    d = projects_dir / "-broken"
    d.mkdir()
    (d / "sess-bad.jsonl").write_text("not json at all\n")
    _write_transcript(projects_dir, "sess-ok", "/p", [_user("hi", T9)])

    keys = {t["key"] for t in _tasks(client)}
    assert "sess-ok" in keys


# ------------------------------------------------------- messages and numbers


def test_only_the_three_newest_messages_ride_along(client, projects_dir):
    """The accordion shows three and offers Show more; the ids still count from
    the bottom of the whole thread."""
    records = []
    for n in range(1, 6):
        records.append(_user(f"message {n}", f"2026-08-16T0{n}:00:00Z",
                             uuid=f"u{n}"))
    _write_transcript(projects_dir, "sess-a", "/p", records)

    task = _tasks(client)[0]
    assert task["message_count"] == 5
    assert [m["message_id"] for m in task["messages"]] == [
        "MSG-005", "MSG-004", "MSG-003"]
    assert [m["body"] for m in task["messages"]] == [
        "message 5", "message 4", "message 3"]


def test_show_more_returns_the_whole_thread_newest_first(client, projects_dir):
    records = [_user(f"message {n}", f"2026-08-16T0{n}:00:00Z", uuid=f"u{n}")
               for n in range(1, 6)]
    _write_transcript(projects_dir, "sess-a", "/p", records)

    r = client.get("/api/tasks/sess-a/messages")
    assert r.status_code == 200
    messages = r.json()["messages"]
    assert [m["message_id"] for m in messages] == [
        "MSG-005", "MSG-004", "MSG-003", "MSG-002", "MSG-001"]
    assert messages[0]["body"] == "message 5"


def test_an_unknown_task_is_a_404(client):
    assert client.get("/api/tasks/nope/messages").status_code == 404


def test_numbers_restart_per_project_and_survive_a_new_session(client,
                                                               projects_dir):
    _write_transcript(projects_dir, "sess-a", "/home/a", [_user("a", T9)])
    _write_transcript(projects_dir, "sess-b", "/home/a", [_user("b", T10)])
    _write_transcript(projects_dir, "sess-c", "/home/b", [_user("c", T11)])

    numbers = {t["key"]: t["task_id"] for t in _tasks(client)}
    assert numbers == {"sess-a": "TASK-001", "sess-b": "TASK-002",
                       "sess-c": "TASK-001"}

    # A later session must not disturb what is already numbered.
    _write_transcript(projects_dir, "sess-d", "/home/a", [_user("d", T12)])
    tasks_mod.reset_cache()
    again = {t["key"]: t["task_id"] for t in _tasks(client)}
    assert again["sess-a"] == "TASK-001"
    assert again["sess-d"] == "TASK-003"


def test_tasks_are_sorted_by_last_activity(client, projects_dir):
    _write_transcript(projects_dir, "old", "/p", [_user("old", T9)])
    _write_transcript(projects_dir, "new", "/p", [_user("new", T12)])
    assert [t["key"] for t in _tasks(client)] == ["new", "old"]


# --------------------------------------------------------------- the schedule


def test_a_pending_scheduled_message_is_a_task_with_no_session(client,
                                                               tmp_path,
                                                               state_dir):
    """§5: the row exists from creation, with an empty session id. Without it
    the Board's Upcoming column would always be empty, which is most of the
    point of the Board."""
    _already_using(state_dir)
    target = tmp_path / "proj" / "report.py"
    target.parent.mkdir()
    target.write_text("x = 1\n")
    _seed_schedule([_entry("20260817-090000-abc", "pull today's news", T12,
                           target=str(target))])

    task = _tasks(client)[0]
    assert task["key"] == "pending:20260817-090000-abc"
    assert task["session_id"] == ""
    assert task["task_id"] == "TASK-001"
    # A task on a FILE belongs to the folder (§2) — files and folders do not get
    # separate session pools — and keeps the file as its displayed target.
    assert task["project"] == str(target.parent)
    assert task["target"] == str(target)
    assert task["status"] == "upcoming"
    assert task["message_count"] == 1
    message = task["messages"][0]
    assert message["kind"] == "scheduled"
    assert message["state"] == "pending"
    assert message["entry_id"] == "20260817-090000-abc"
    assert message["unread"] is False, "nothing has happened to read yet"
    assert task["unread"] == 0


def test_a_fired_scheduled_message_is_one_message_not_two(client, projects_dir):
    """The send hands the entry's body to the session verbatim, so the entry
    and the transcript prompt are the SAME message. Listing both is the bug this
    join exists to prevent."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("pull today's news", T9, uuid="anchor-1"),
        _assistant("here it is", T10),
    ])
    _seed_schedule([_entry("e1", "pull today's news", T9, state=schedule.SENT,
                           fired=T9, turn="ok", claude_session_id="sess-a")])

    task = _tasks(client)[0]
    assert task["message_count"] == 1
    message = task["messages"][0]
    assert message["kind"] == "scheduled"
    assert message["entry_id"] == "e1"
    assert message["anchor"] == "anchor-1", "the join keeps the scroll target"
    assert message["turn"] == "done"


def test_a_thread_mixes_scheduled_and_typed_messages(client, projects_dir):
    """A task is a bag of messages and does not care where each came from."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("pull today's news", T9, uuid="a1"),
        _user("now summarise it", T10, uuid="a2"),
    ])
    _seed_schedule([
        _entry("e1", "pull today's news", T9, state=schedule.SENT, fired=T9,
               turn="ok", claude_session_id="sess-a"),
        _entry("e2", "pull today's news", T12, claude_session_id="sess-a"),
    ])

    r = client.get("/api/tasks/sess-a/messages")
    messages = r.json()["messages"]
    assert [(m["message_id"], m["kind"], m["state"]) for m in messages] == [
        ("MSG-003", "scheduled", "pending"),
        ("MSG-002", "chat", "sent"),
        ("MSG-001", "scheduled", "sent"),
    ]
    assert _tasks(client)[0]["message_count"] == 3


def test_a_recurring_template_is_not_a_message(client, projects_dir):
    """A template never fires — its materialised occurrence is the message."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([
        _entry("tpl", "every morning", T9, state=schedule.RECURRING,
               repeats="0 9 * * *", claude_session_id="sess-a"),
        _entry("occ", "every morning", T12, claude_session_id="sess-a",
               template_id="tpl"),
    ])
    task = _by_key(client)["sess-a"]
    assert task["message_count"] == 2
    assert [m["entry_id"] for m in task["messages"]] == ["occ", ""]
    assert task["messages"][0]["template_id"] == "tpl"


def test_a_skipped_occurrence_reads_as_skipped_and_archives_the_task(
        client, projects_dir):
    """The user's skip and the loop's own missed verdict are the same fact about
    a repeating run, and schedule-lib.ts files both under Archive."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("occ", "every morning", T12, state=schedule.MISSED,
                           claude_session_id="sess-a", template_id="tpl")])
    task = _by_key(client)["sess-a"]
    assert task["messages"][0]["state"] == "skipped"
    assert task["status"] == "archived"


def test_a_task_whose_session_has_no_transcript_still_lists(client, tmp_path):
    """The session may be seconds old, or its transcript may have moved. Either
    way the user's message is not dropped on the floor."""
    _seed_schedule([_entry("e1", "hello", T9, state=schedule.SENT, fired=T9,
                           turn="ok", claude_session_id="ghost",
                           target=str(tmp_path))])
    task = _by_key(client)["ghost"]
    assert task["session_id"] == "ghost"
    assert task["message_count"] == 1
    assert task["messages"][0]["kind"] == "scheduled"


def test_a_pending_row_keeps_its_number_when_it_finally_runs(client,
                                                             projects_dir,
                                                             tmp_path):
    """§5's whole point: the number is allocated at creation and the session id
    fills in later, so the row the user has been watching does not renumber the
    moment it does something."""
    _seed_schedule([_entry("e1", "pull the news", T9, target=str(tmp_path))])
    pending = _tasks(client)[0]
    assert pending["key"] == "pending:e1"
    number = pending["task_id"]

    # The first run mints a session id and writes a transcript.
    _write_transcript(projects_dir, "sess-a", str(tmp_path),
                      [_user("pull the news", T9, uuid="a1")])
    _seed_schedule([_entry("e1", "pull the news", T9, state=schedule.SENT,
                           fired=T9, turn="ok", claude_session_id="sess-a",
                           target=str(tmp_path))])
    tasks_mod.reset_cache()

    tasks = _tasks(client)
    assert len(tasks) == 1, "the pending row and the session are one task"
    assert tasks[0]["key"] == "sess-a"
    assert tasks[0]["task_id"] == number


# ------------------------------------------------ which session an entry is in


def test_a_pending_message_naming_a_session_is_that_session_s_task(
        client, projects_dir, tmp_path):
    """A message that has not run yet still KNOWS which conversation it is
    going to continue — that is what `session_id` is — so it belongs to that
    task now, not to a `pending:` row of its own beside it.

    Grouping on `claude_session_id` alone put a re-send queued behind the very
    turn it is re-asking, and a message scheduled out of an open chat, into a
    phantom second row that merged into the real one the moment the watcher
    reported."""
    _write_transcript(projects_dir, "sess-a", str(tmp_path),
                      [_user("pull the news", T9, uuid="a1")])
    _seed_schedule([_entry("e1", "try that again", T12, session_id="sess-a",
                           target=str(tmp_path))])

    tasks = _tasks(client)
    assert len(tasks) == 1, "one conversation, one row"
    assert tasks[0]["key"] == "sess-a"
    assert [m["entry_id"] for m in tasks[0]["messages"]] == ["e1", ""]
    assert tasks[0]["messages"][0]["state"] == "pending"


def test_a_named_pending_message_does_not_split_when_it_runs(
        client, projects_dir, tmp_path):
    """The queued half of the re-send story, end to end: the entry is one row
    while it waits, and the SAME one row once the watcher fills in the answer.
    No split on the way in, no duplicate on the way out, and no renumbering."""
    _write_transcript(projects_dir, "sess-a", str(tmp_path),
                      [_user("pull the news", T9, uuid="a1")])
    _seed_schedule([_entry("e1", "try that again", T12, session_id="sess-a",
                           target=str(tmp_path))])
    waiting = _tasks(client)
    assert [t["key"] for t in waiting] == ["sess-a"]
    number = waiting[0]["task_id"]

    _write_transcript(projects_dir, "sess-a", str(tmp_path), [
        _user("pull the news", T9, uuid="a1"),
        _user("try that again", T12, uuid="a2"),
    ])
    _seed_schedule([_entry("e1", "try that again", T12, session_id="sess-a",
                           state=schedule.SENT, fired=T12, turn="ok",
                           claude_session_id="sess-a", target=str(tmp_path))])
    tasks_mod.reset_cache()

    tasks = _tasks(client)
    assert len(tasks) == 1, "the answer agrees with the input: still one task"
    assert tasks[0]["key"] == "sess-a"
    assert tasks[0]["task_id"] == number
    assert tasks[0]["message_count"] == 2, "the join, not a second message"


def test_messages_with_no_session_stay_separate_tasks(client, tmp_path):
    """"" is not a session id — it is "start a fresh one" — so two messages
    that name none are two tasks. Letting the empty string group would collapse
    every unrelated fresh-session message on the machine into one row, which is
    a far worse bug than the one the fallback fixes."""
    _seed_schedule([
        _entry("e1", "one thing", T9, target=str(tmp_path)),
        _entry("e2", "another thing", T12, target=str(tmp_path)),
    ])
    assert sorted(t["key"] for t in _tasks(client)) == ["pending:e1",
                                                        "pending:e2"]


def test_the_session_a_run_landed_in_beats_the_one_it_asked_for(
        client, projects_dir, tmp_path):
    """Precedence is answer-first. A resume that forked into a new session RAN
    in `claude_session_id`, and that is the thread the message is in whatever
    it asked to resume."""
    _write_transcript(projects_dir, "asked", str(tmp_path),
                      [_user("earlier", T9, uuid="b1")])
    _write_transcript(projects_dir, "landed", str(tmp_path),
                      [_user("do the thing", T12, uuid="c1")])
    _seed_schedule([_entry("e1", "do the thing", T12, session_id="asked",
                           state=schedule.SENT, fired=T12, turn="ok",
                           claude_session_id="landed", target=str(tmp_path))])

    tasks = _by_key(client)
    assert tasks["landed"]["messages"][0]["entry_id"] == "e1"
    assert [m["entry_id"] for m in tasks["asked"]["messages"]] == [""]


def test_a_named_pending_message_makes_the_task_its_run_will_join(
        client, projects_dir, tmp_path):
    """A session with no transcript yet is still a task (it may be seconds old,
    or not started at all), and it must be the SAME task the run joins — one
    row that fills in, not a row that is replaced by a second one."""
    _seed_schedule([_entry("e1", "kick it off", T12, session_id="not-yet",
                           target=str(tmp_path))])
    waiting = _tasks(client)
    assert [t["key"] for t in waiting] == ["not-yet"]
    assert waiting[0]["session_id"] == "not-yet"
    assert waiting[0]["status"] == "upcoming"
    number = waiting[0]["task_id"]

    _write_transcript(projects_dir, "not-yet", str(tmp_path),
                      [_user("kick it off", T12, uuid="d1")])
    _seed_schedule([_entry("e1", "kick it off", T12, session_id="not-yet",
                           state=schedule.SENT, fired=T12, turn="ok",
                           claude_session_id="not-yet", target=str(tmp_path))])
    tasks_mod.reset_cache()

    tasks = _tasks(client)
    assert [t["key"] for t in tasks] == ["not-yet"]
    assert tasks[0]["task_id"] == number
    assert tasks[0]["message_count"] == 1


# -------------------------------------------------- when a task stops being one
#
# The rule: a task that NEVER RAN disappears when its work is cancelled; a task
# that HAS run keeps its row, in Archive. Decided in `_collect`, so every view
# agrees — and an absence of a task, never a filter.


def test_a_task_that_never_ran_vanishes_when_it_is_cancelled(client, tmp_path):
    """Deleting a scheduled message cancels its entry. With no session behind it
    there is no transcript and no history — nothing for a row to be about — and
    the row that used to survive was an empty shell sitting in Archive."""
    _seed_schedule([_entry("e1", "pull the news", T9, state=schedule.CANCELLED,
                           target=str(tmp_path))])
    assert _tasks(client) == []
    # And it is gone from the one view that could still reach it by key.
    assert client.get("/api/tasks/pending:e1/messages").status_code == 404


def test_a_never_run_skip_vanishes_too(client, tmp_path):
    """A skipped OCCURRENCE is the same fact by a different route — the loop's
    missed verdict rather than the user's cancel — and it ran exactly as much."""
    _seed_schedule([_entry("occ", "every morning", T9, state=schedule.MISSED,
                           template_id="tpl", target=str(tmp_path))])
    assert _tasks(client) == []


def test_a_pending_message_keeps_its_task_when_its_neighbour_is_cancelled(
        client, tmp_path):
    """The boundary that matters most: only when NOTHING is left to run does a
    task go. Cancelling one of two scheduled messages takes that one's row and
    leaves the upcoming one exactly where it was — each session-less message is
    its own task, so the cancel cannot reach past its own row."""
    _seed_schedule([
        _entry("e1", "pull the news", T9, state=schedule.CANCELLED,
               target=str(tmp_path)),
        _entry("e2", "pull the weather", T12, target=str(tmp_path)),
    ])
    tasks = _tasks(client)
    assert [t["key"] for t in tasks] == ["pending:e2"]
    assert tasks[0]["status"] == "upcoming"


def test_a_thread_with_anything_left_to_run_is_still_a_task(tmp_path):
    """The `all` over a task's entries, at the level it is written.

    Grouping gives a session-less task exactly ONE entry today (a message that
    names no session is asking for a fresh one, so it keys under its own
    `pending:` row — see `test_messages_with_no_session_stay_separate_tasks`),
    which means the HTTP cases above can only exercise the one-entry form. The
    rule is written over the whole thread anyway, because the two ways a thread
    earns its row must not depend on that: anything left to run, or anything that
    already ran."""
    def task(*entries):
        return {"key": "pending:e1", "session_id": "", "path": None,
                "entries": list(entries)}

    cancelled = _entry("e1", "gone", T9, state=schedule.CANCELLED)
    skipped = _entry("e2", "skipped", T9, state=schedule.MISSED,
                     template_id="tpl")
    assert tasks_mod._is_task(task(cancelled, skipped)) is False
    assert tasks_mod._is_task(task(cancelled)) is False
    # Anything left to run.
    assert tasks_mod._is_task(task(cancelled, _entry("e3", "soon", T12))) is True
    # Anything that already ran, even with the cancel newest.
    ran = _entry("e4", "went", T9, state=schedule.SENT, fired=T9, turn="ok")
    assert tasks_mod._is_task(task(ran, cancelled)) is True
    # A send that BROKE is news, not an absence: `error` keeps the row.
    broke = _entry("e5", "boom", T9, state=schedule.ERROR, error="gone")
    assert tasks_mod._is_task(task(broke)) is True
    # And a session keeps it whatever its entries say.
    named = dict(task(cancelled, skipped), session_id="sess-a")
    assert tasks_mod._is_task(named) is True


def test_a_session_keeps_its_row_with_every_entry_cancelled(client,
                                                            projects_dir):
    """A task that has run is a Claude session with a real transcript, and this
    app does not destroy transcripts (D306). Archive is the honest resting place
    for that one — the row stays, whatever happened to its entries."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "later", T12, state=schedule.CANCELLED,
                           claude_session_id="sess-a")])
    task = _by_key(client)["sess-a"]
    assert task["status"] == "archived"
    assert task["messages"][0]["state"] == "cancelled"


def test_a_session_with_one_cancelled_and_one_pending_entry_is_upcoming(
        client, tmp_path):
    """Two messages scheduled into the same conversation, one of them deleted.
    The row stays and still reads `upcoming`, because the surviving message is
    the newest thing in the thread and it has not happened yet."""
    _seed_schedule([
        _entry("e1", "try that again", T9, session_id="not-yet",
               state=schedule.CANCELLED, target=str(tmp_path)),
        _entry("e2", "and then this", T12, session_id="not-yet",
               target=str(tmp_path)),
    ])
    tasks = _tasks(client)
    assert [t["key"] for t in tasks] == ["not-yet"]
    assert tasks[0]["status"] == "upcoming"
    assert tasks[0]["message_count"] == 2


def test_a_run_whose_transcript_is_missing_keeps_its_row(client, tmp_path):
    """It ran: `sent` says the body was handed to a session. The transcript may
    be seconds old or may have been moved, and the module's whole posture is that
    an unreadable transcript costs a fact and never the user's message."""
    _seed_schedule([_entry("e1", "pull the news", T9, state=schedule.SENT,
                           fired=T9, turn="ok", target=str(tmp_path))])
    tasks = _tasks(client)
    assert [t["key"] for t in tasks] == ["pending:e1"]
    assert tasks[0]["session_id"] == "", "no session named, and still a task"
    assert tasks[0]["message_count"] == 1


def test_a_dropped_task_keeps_its_number_allocated_and_unused(client, tmp_path,
                                                              state_dir):
    """Allocate once, never renumber — the store's rule, unchanged by this one.
    A dropped task's number stays where it is: nothing reclaims it, so the next
    task in the project takes the NEXT one, and an unskip that brings the entry
    back finds the row still called what the user saw."""
    _seed_schedule([_entry("e1", "pull the news", T9, target=str(tmp_path))])
    assert _tasks(client)[0]["task_id"] == "TASK-001"
    store = json.loads((state_dir / "task_ids.json").read_text())
    assert store["pending:e1"]["n"] == 1

    cancelled = _entry("e1", "pull the news", T9, state=schedule.CANCELLED,
                       target=str(tmp_path))
    _seed_schedule([cancelled])
    tasks_mod.reset_cache()
    assert _tasks(client) == []
    assert json.loads((state_dir / "task_ids.json").read_text()) == store, \
        "the record is not touched, let alone released"

    _seed_schedule([cancelled,
                    _entry("e2", "another thing", T12, target=str(tmp_path))])
    tasks_mod.reset_cache()
    rows = _tasks(client)
    assert [(t["key"], t["task_id"]) for t in rows] == [("pending:e2",
                                                        "TASK-002")]

    # And the dropped row keeps the number it was showing if it comes back.
    _seed_schedule([_entry("e1", "pull the news", T9, target=str(tmp_path)),
                    _entry("e2", "another thing", T12, target=str(tmp_path))])
    tasks_mod.reset_cache()
    back = {t["key"]: t["task_id"] for t in _tasks(client)}
    assert back == {"pending:e1": "TASK-001", "pending:e2": "TASK-002"}


# --------------------------------------------------------------- the calendar


def _epoch(iso):
    return tasks_store.epoch(iso)


def _scheduled(client, frm, to):
    r = client.get("/api/tasks/scheduled", params={"from": frm, "to": to})
    assert r.status_code == 200, r.text
    return r.json()["items"]


DAY_START = _epoch("2026-08-16T00:00:00Z")
DAY_END = _epoch("2026-08-17T00:00:00Z")


def test_the_window_is_from_inclusive_and_to_exclusive(client, tmp_path):
    """The client sends local-midnight bounds because its columns are local
    days. A message at 23:59 on the last column has to survive; the one at the
    next midnight belongs to the next column."""
    _seed_schedule([
        _entry("at-from", "first", "2026-08-16T00:00:00Z", target=str(tmp_path)),
        _entry("late", "last minute", "2026-08-16T23:59:00Z",
               target=str(tmp_path)),
        _entry("at-to", "next day", "2026-08-17T00:00:00Z",
               target=str(tmp_path)),
    ])
    items = _scheduled(client, DAY_START, DAY_END)
    assert [i["message"]["body"] for i in items] == ["first", "last minute"]


def test_a_windowed_message_is_the_whole_task_message(client, tmp_path):
    """The same shape the listing returns — not a trimmed pair — and `kind` is
    present and "scheduled" so the client's own filter stays a no-op."""
    _seed_schedule([_entry("e1", "pull the news", "2026-08-16T09:00:00Z",
                           target=str(tmp_path), template_id="tpl")])
    item = _scheduled(client, DAY_START, DAY_END)[0]
    assert item["task_key"] == "pending:e1"
    assert set(item["message"]) == {
        "message_id", "kind", "body", "at", "ran_at", "state", "unread",
        "entry_id", "template_id", "turn", "anchor"}
    assert item["message"]["kind"] == "scheduled"
    assert item["message"]["message_id"] == "MSG-001"
    assert item["message"]["entry_id"] == "e1"
    assert item["message"]["template_id"] == "tpl"
    assert item["message"]["at"] == _epoch("2026-08-16T09:00:00Z")


def test_the_window_carries_every_message_a_task_has_in_it(client,
                                                           projects_dir):
    """The regression this endpoint exists for: the listing's three-message
    tail under-draws a time axis, so an hourly message drew three chips instead
    of a day of them."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("start", T9,
                                                           uuid="u1")])
    entries = []
    for hour in range(40):
        due = _epoch("2026-08-16T00:00:00Z") + hour * 1800
        entries.append(_entry(
            f"e{hour}", f"run {hour}",
            __import__("datetime").datetime.fromtimestamp(
                due, __import__("datetime").timezone.utc).isoformat(),
            claude_session_id="sess-a", template_id="tpl"))
    _seed_schedule(entries)

    items = _scheduled(client, DAY_START, DAY_END)
    assert len(items) == 40
    assert all(i["task_key"] == "sess-a" for i in items)
    # And the ids still count from the bottom of the WHOLE thread — the typed
    # message at 09:00 takes its place in the middle of the run.
    assert items[0]["message"]["message_id"] == "MSG-001"
    assert items[-1]["message"]["message_id"] == "MSG-041"


def test_chat_messages_never_appear_on_the_calendar(client, projects_dir):
    """A typed message has no time the calendar could place it at."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("typed today", "2026-08-16T09:00:00Z", uuid="u1")])
    _seed_schedule([_entry("e1", "scheduled today", "2026-08-16T10:00:00Z",
                           claude_session_id="sess-a")])
    items = _scheduled(client, DAY_START, DAY_END)
    assert [i["message"]["body"] for i in items] == ["scheduled today"]


def test_a_fired_message_is_placed_at_the_time_it_was_scheduled_for(
        client, projects_dir):
    """`at` is the DUE time, always. The run supplies `ran_at`, and the join
    supplies the anchor — neither may move the chip."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("pull the news", "2026-08-16T09:05:00Z", uuid="u1")])
    _seed_schedule([_entry("e1", "pull the news", "2026-08-16T09:00:00Z",
                           state=schedule.SENT, fired="2026-08-16T09:05:00Z",
                           turn="ok", claude_session_id="sess-a")])
    item = _scheduled(client, DAY_START, DAY_END)[0]
    assert item["message"]["at"] == _epoch("2026-08-16T09:00:00Z")
    assert item["message"]["ran_at"] == _epoch("2026-08-16T09:05:00Z")
    assert item["message"]["state"] == "sent"
    assert item["message"]["anchor"] == "u1"


def test_a_caught_up_message_keeps_the_day_it_was_scheduled_for(
        client, projects_dir):
    """The user-reported bug, at the size it actually happens.

    A message scheduled for the 14th, run on the 16th because the app was shut
    over the weekend, must stay in the 14th's column — catch-up is unbounded, so
    this is the ordinary outcome and not an edge case. `at` said the 16th and the
    chip jumped to today; now `at` says the 14th and `ran_at` says the 16th."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("pull the news", "2026-08-16T09:05:00Z", uuid="u1")])
    _seed_schedule([_entry("e1", "pull the news", "2026-08-14T09:00:00Z",
                           state=schedule.SENT, fired="2026-08-16T09:04:00Z",
                           turn="ok", claude_session_id="sess-a")])

    # The 16th's column does not draw it…
    assert _scheduled(client, DAY_START, DAY_END) == []
    # …the 14th's does.
    day14 = _epoch("2026-08-14T00:00:00Z")
    item = _scheduled(client, day14, day14 + 86400)[0]
    assert item["message"]["at"] == _epoch("2026-08-14T09:00:00Z")
    # `ran_at` is the TRANSCRIPT's time, not the `fired` claim stamp: the
    # session's own record of the prompt is the most accurate answer there is.
    assert item["message"]["ran_at"] == _epoch("2026-08-16T09:05:00Z")
    assert item["message"]["anchor"] == "u1", "the join still found its prompt"


def test_an_unrun_message_has_not_run(client, tmp_path):
    """`ran_at` is 0 for anything that has not happened, which is what makes it
    readable as a fact rather than as a guess at one."""
    _seed_schedule([_entry("e1", "later", "2026-08-16T09:00:00Z",
                           target=str(tmp_path))])
    item = _scheduled(client, DAY_START, DAY_END)[0]
    assert item["message"]["at"] == _epoch("2026-08-16T09:00:00Z")
    assert item["message"]["ran_at"] == 0.0


def test_the_join_still_picks_the_nearest_prompt_for_each_run(client,
                                                              projects_dir):
    """The anchor match is unchanged — it still needs the distance heuristic to
    tell N identical daily prompts apart, and each run must take its OWN. Only
    what the match writes changed: `ran_at`, never `at`."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("daily news", "2026-08-14T09:02:00Z", uuid="day14"),
        _user("daily news", "2026-08-15T09:03:00Z", uuid="day15"),
        _user("daily news", "2026-08-16T09:01:00Z", uuid="day16"),
    ])
    _seed_schedule([
        _entry(f"e{d}", "daily news", f"2026-08-{d}T09:00:00Z",
               state=schedule.SENT, fired=f"2026-08-{d}T09:00:30Z", turn="ok",
               claude_session_id="sess-a", template_id="tpl")
        for d in (14, 15, 16)
    ])

    r = client.get("/api/tasks/sess-a/messages")
    messages = list(reversed(r.json()["messages"]))  # oldest first
    assert [m["anchor"] for m in messages] == ["day14", "day15", "day16"]
    assert [m["at"] for m in messages] == [
        _epoch("2026-08-14T09:00:00Z"), _epoch("2026-08-15T09:00:00Z"),
        _epoch("2026-08-16T09:00:00Z")]
    assert [m["ran_at"] for m in messages] == [
        _epoch("2026-08-14T09:02:00Z"), _epoch("2026-08-15T09:03:00Z"),
        _epoch("2026-08-16T09:01:00Z")]


def test_a_typed_message_ran_when_it_was_typed(client, projects_dir):
    """A chat message has no gap for the two stamps to disagree across."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("hello", T9, uuid="u1")])
    message = _tasks(client)[0]["messages"][0]
    assert message["kind"] == "chat"
    assert message["at"] == _epoch(T9)
    assert message["ran_at"] == _epoch(T9)


def test_an_empty_window_is_an_empty_list(client, tmp_path):
    _seed_schedule([_entry("e1", "later", "2026-09-01T09:00:00Z",
                           target=str(tmp_path))])
    assert _scheduled(client, DAY_START, DAY_END) == []
    # An inverted window is a question with an empty answer, not an error: the
    # calendar can ask for one while it is still settling on its bounds.
    assert _scheduled(client, DAY_END, DAY_START) == []


def test_the_window_is_cached_but_notices_a_change(client, tmp_path):
    _seed_schedule([_entry("e1", "one", "2026-08-16T09:00:00Z",
                           target=str(tmp_path))])
    assert len(_scheduled(client, DAY_START, DAY_END)) == 1
    _seed_schedule([
        _entry("e1", "one", "2026-08-16T09:00:00Z", target=str(tmp_path)),
        _entry("e2", "two", "2026-08-16T10:00:00Z", target=str(tmp_path))])
    assert len(_scheduled(client, DAY_START, DAY_END)) == 2


def test_a_recurring_template_is_not_drawn(client, tmp_path):
    """A template never fires; its materialised occurrence is what the calendar
    has to place."""
    _seed_schedule([
        _entry("tpl", "every morning", "2026-08-16T09:00:00Z",
               state=schedule.RECURRING, repeats="0 9 * * *",
               target=str(tmp_path)),
        _entry("occ", "every morning", "2026-08-16T09:00:00Z",
               template_id="tpl", target=str(tmp_path)),
    ])
    items = _scheduled(client, DAY_START, DAY_END)
    assert [i["message"]["entry_id"] for i in items] == ["occ"]


def test_a_never_run_message_that_was_cancelled_draws_no_chip(client, tmp_path):
    """The window reads the same collection the listing does, so the two cannot
    disagree: a chip for a task the listing no longer contains would point at a
    row that is not there."""
    _seed_schedule([
        _entry("gone", "deleted", "2026-08-16T09:00:00Z",
               state=schedule.CANCELLED, target=str(tmp_path)),
        _entry("kept", "still here", "2026-08-16T10:00:00Z",
               target=str(tmp_path)),
    ])
    items = _scheduled(client, DAY_START, DAY_END)
    assert [i["message"]["entry_id"] for i in items] == ["kept"]
    assert [t["key"] for t in _tasks(client)] == ["pending:kept"]


# ----------------------------------------------------------------- the status


@pytest.mark.parametrize("fields,expected", [
    ({}, "upcoming"),
    ({"state": schedule.SENDING}, "in_progress"),
    ({"state": schedule.SENT, "turn": "ok", "fired": T12}, "done"),
    ({"state": schedule.MISSED}, "done"),
    ({"state": schedule.CANCELLED}, "archived"),
    ({"state": schedule.ERROR, "error": "target vanished"}, "failed"),
    ({"state": schedule.SENT, "turn": "failed", "fired": T12}, "failed"),
    ({"state": schedule.SENT, "turn": "unknown", "fired": T12}, "failed"),
])
def test_status_follows_the_newest_message(client, projects_dir, fields,
                                           expected):
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "later", T12, claude_session_id="sess-a",
                           **fields)])
    assert _by_key(client)["sess-a"]["status"] == expected


def test_triage_wins_where_it_disagrees(client, projects_dir, state_dir):
    """The user dragged the card. A derivation that undid that on the next poll
    would make the board unusable."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "later", T12, claude_session_id="sess-a")])
    assert _by_key(client)["sess-a"]["status"] == "upcoming"

    (state_dir / "triage.json").write_text(
        json.dumps({"sess-a": {"status": "archived"}}))
    assert _by_key(client)["sess-a"]["status"] == "archived"


def test_a_dead_turn_is_reported_as_a_failure(client, projects_dir):
    """`sent` only means the SESSION STARTED; reporting a dead turn as a clean
    send sends the user looking in the wrong place.

    It is a STATUS, not only the flag beside it. As `done` with a flag, every
    view had to remember to read the flag to say anything at all, and one that
    did not simply never showed the failure."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("pull the news", T9, uuid="a1")])
    _seed_schedule([_entry("e1", "pull the news", T9, state=schedule.SENT,
                           fired=T9, turn="failed", claude_session_id="sess-a")])
    task = _by_key(client)["sess-a"]
    assert task["failed"] is True
    assert task["messages"][0]["state"] == "error"
    assert task["status"] == "failed"


def test_a_skipped_occurrence_is_archived_and_not_failed(client, projects_dir):
    """A run the coalescer dropped was filed away and never attempted. Only
    something that actually ran can fail, and calling a routine skip a failure
    makes ordinary behaviour look like breakage."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "every morning", T12, template_id="tpl",
                           state=schedule.MISSED,
                           error="skipped: only the latest missed run is sent",
                           claude_session_id="sess-a")])
    task = _by_key(client)["sess-a"]
    assert task["messages"][0]["state"] == "skipped"
    assert task["status"] == "archived"
    assert task["failed"] is False


def test_triage_still_wins_over_a_failure(client, projects_dir, state_dir):
    """`failed` is derived and triage is the user's own act, so filing a broken
    run under done is allowed to stick. The `failed` flag stays true underneath
    it, which is the one direction the two are meant to disagree in."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "x", T12, state=schedule.SENT, fired=T12,
                           turn="failed", claude_session_id="sess-a")])
    assert _by_key(client)["sess-a"]["status"] == "failed"

    (state_dir / "triage.json").write_text(
        json.dumps({"sess-a": {"status": "done"}}))
    task = _by_key(client)["sess-a"]
    assert task["status"] == "done"
    assert task["failed"] is True


# ----------------------------------------------------------------- the unread


def test_marking_one_message_read_leaves_the_older_one_unread(client,
                                                              projects_dir,
                                                              state_dir):
    """Clicking MSG-003 says nothing about the MSG-002 the user scrolled past.
    A moving watermark would swallow it silently, which is the one failure
    unread exists to prevent."""
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("one", T9, uuid="u1"),
        _user("two", T10, uuid="u2"),
        _user("three", T11, uuid="u3"),
    ])
    assert _by_key(client)["sess-a"]["unread"] == 3

    r = client.post("/api/tasks/read",
                    json={"key": "sess-a", "message_id": "MSG-003"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "unread": 2}

    messages = client.get("/api/tasks/sess-a/messages").json()["messages"]
    read = {m["message_id"]: m["unread"] for m in messages}
    assert read == {"MSG-003": False, "MSG-002": True, "MSG-001": True}
    assert _by_key(client)["sess-a"]["unread"] == 2


def test_reading_a_thread_through_clears_it(client, projects_dir, state_dir):
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("one", T9, uuid="u1"), _user("two", T10, uuid="u2")])
    for message_id in ("MSG-001", "MSG-002"):
        client.post("/api/tasks/read",
                    json={"key": "sess-a", "message_id": message_id})
    task = _by_key(client)["sess-a"]
    assert task["unread"] == 0
    assert all(m["unread"] is False for m in task["messages"])


def test_a_future_message_is_not_unread(client, projects_dir, state_dir):
    """A task scheduled for tomorrow has no response to have missed."""
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/p", [_user("one", T9,
                                                           uuid="u1")])
    _seed_schedule([_entry("e1", "later", T12, claude_session_id="sess-a")])
    client.post("/api/tasks/read", json={"key": "sess-a",
                                         "message_id": "MSG-001"})
    task = _by_key(client)["sess-a"]
    assert task["message_count"] == 2
    assert task["unread"] == 0


def test_day_one_is_quiet_and_only_what_arrives_after_it_is_unread(
        client, projects_dir, state_dir):
    """Unread means "arrived since I started using this", not "exists". A store
    that has never existed would otherwise light up every row on the machine,
    which is a badge on everything and therefore a badge that means nothing."""
    records = [_user(f"message {n}", f"2026-08-16T0{n}:00:00Z", uuid=f"u{n}")
               for n in range(1, 10)] + [_user("message 10", T10, uuid="u10")]
    path = _write_transcript(projects_dir, "sess-a", "/p", records)

    task = _by_key(client)["sess-a"]
    assert task["message_count"] == 10
    assert task["unread"] == 0
    assert all(m["unread"] is False for m in task["messages"])
    assert tasks_store.initialized(tasks_store.read_state())

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_user("message 11", T11, uuid="u11")) + "\n")
    task = _by_key(client)["sess-a"]
    assert task["message_count"] == 11
    assert task["unread"] == 1
    assert task["messages"][0]["message_id"] == "MSG-011"
    assert task["messages"][0]["unread"] is True
    assert task["messages"][1]["unread"] is False


def test_the_baseline_is_stamped_once_and_never_moves(client, projects_dir,
                                                      state_dir):
    """A later run must not re-stamp: that would silently mark unread things
    read, which is the failure the baseline exists to avoid the reverse of."""
    path = _write_transcript(projects_dir, "sess-a", "/p",
                             [_user("one", T9, uuid="u1")])
    _by_key(client)
    stamped = json.loads((state_dir / "read.json").read_text())[
        tasks_store.INIT_KEY]

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_user("two", T10, uuid="u2")) + "\n")
    for _ in range(3):
        tasks = _by_key(client)
    assert tasks["sess-a"]["unread"] == 1
    assert json.loads((state_dir / "read.json").read_text())[
        tasks_store.INIT_KEY] == stamped


def test_a_task_that_appears_after_the_baseline_is_unread(client,
                                                          projects_dir):
    """The baseline is a per-task floor, not one global clock, so a session
    created afterwards has its first message land unread."""
    _write_transcript(projects_dir, "old", "/p", [_user("old", T9, uuid="u1")])
    assert _by_key(client)["old"]["unread"] == 0

    _write_transcript(projects_dir, "new", "/p", [_user("new", T10, uuid="u2")])
    tasks = _by_key(client)
    assert tasks["new"]["unread"] == 1
    assert tasks["old"]["unread"] == 0


def test_the_read_endpoint_refuses_nonsense(client):
    bad = client.post("/api/tasks/read", json={"key": "s", "message_id": "12"})
    assert bad.status_code == 400
    assert "MSG-nnn" in bad.json()["detail"]
    assert client.post("/api/tasks/read",
                       json={"key": "  ", "message_id": "MSG-001"}
                       ).status_code == 400


def test_marking_a_task_that_no_longer_exists_is_not_an_error(client):
    r = client.post("/api/tasks/read",
                    json={"key": "gone", "message_id": "MSG-001"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "unread": 0}


# ------------------------------------------------------------------ the cache


def test_a_growing_transcript_is_read_incrementally(client, projects_dir):
    """Transcripts are append-only, so a poll pays for the turn that happened
    since — not for the file again."""
    path = _write_transcript(projects_dir, "sess-a", "/p",
                             [_user("one", T9, uuid="u1")])
    assert _by_key(client)["sess-a"]["message_count"] == 1

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_user("two", T10, uuid="u2")) + "\n")
    task = _by_key(client)["sess-a"]
    assert task["message_count"] == 2
    assert task["messages"][0]["body"] == "two"

    # A half-written line is re-read whole on the next poll rather than dropped.
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"type": "user", "timestamp": "2026-08-16T13:00:00Z", '
                '"uuid": "u3", "message": {"role": "user", "cont')
    assert _by_key(client)["sess-a"]["message_count"] == 2
    with open(path, "a", encoding="utf-8") as f:
        f.write('ent": [{"type": "text", "text": "three"}]}}\n')
    task = _by_key(client)["sess-a"]
    assert task["message_count"] == 3
    assert task["messages"][0]["body"] == "three"


def test_a_replaced_transcript_is_re_read_from_the_top(client, projects_dir):
    path = _write_transcript(projects_dir, "sess-a", "/p", [
        _user("one", T9, uuid="u1"), _user("two", T10, uuid="u2")])
    assert _by_key(client)["sess-a"]["message_count"] == 2

    _write_transcript(projects_dir, "sess-a", "/p", [_user("only", T9)],
                      encoded=os.path.basename(os.path.dirname(str(path))))
    task = _by_key(client)["sess-a"]
    assert task["message_count"] == 1
    assert task["messages"][0]["body"] == "only"

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
import time

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


def test_a_title_read_off_a_scheduled_entry_says_which_message_it_read(
        client, tmp_path):
    """The message branch has TWO sources and only one of them is a name.

    With no transcript to read a first prompt from, the earliest scheduled
    entry's message is the best the server has — but on a task scheduled from
    the New task form that message IS what the user typed into the ask box, so a
    client prefilling Title from it duplicates the description into the name.
    `entry` is the row saying which of the two it read, because the client cannot
    tell them apart by looking at the string."""
    _seed_schedule([_entry("e1", "pull today's news\nand file it", T12,
                           target=str(tmp_path))])
    task = _tasks(client)[0]
    assert task["title"] == "pull today's news"
    assert task["title_source"] == "entry"


def test_the_sessions_own_first_prompt_outranks_the_entry_that_asked_it(
        client, projects_dir, tmp_path):
    """Both sources present. The transcript's first prompt is the session's own
    opening line and stays `message`; the entry fallback is only for a session
    whose transcript says nothing."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("check the deploy",
                                                          T9)])
    _seed_schedule([_entry("e1", "pull today's news", T9, state=schedule.SENT,
                           fired=T9, turn="ok", claude_session_id="sess-a")])
    task = _by_key(client)["sess-a"]
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


# ---------------------------------------------------------------- the next run
# `next_run` / `next_run_entry`: the one fact about the FUTURE that a
# three-message window cannot be trusted to hold. The Board orders Upcoming by
# soonest-next-run and its Run now button fires the entry named here, so the two
# fields are the sort and the button agreeing.

T8 = "2026-08-16T08:00:00Z"
OCT = "2026-10-01T09:00:00Z"


def test_the_next_run_is_the_earliest_pending_even_outside_the_window(
        client, projects_dir):
    """`min(at)` over every PENDING entry, taken before the tail is cut.

    The window cannot answer this. Two typed prompts and next month's occurrence
    are the three newest by `at`, which is exactly the shape that used to bury the
    run that should go first: ascending by `at`, the 08:00 pending is the first of
    four and the tail keeps the last three."""
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("first run", T11, uuid="a1"),
        _user("second run", T12, uuid="a2"),
    ])
    _seed_schedule([
        _entry("e-overdue", "the run that is late", T8,
               claude_session_id="sess-a"),
        _entry("e-oct", "next month", OCT, claude_session_id="sess-a"),
    ])
    task = _by_key(client)["sess-a"]

    assert task["message_count"] == 4
    assert [m["entry_id"] for m in task["messages"]] == ["e-oct", "", ""], \
        "the overdue pending is not in the window at all"
    assert task["next_run"] == tasks_store.epoch(T8)
    assert task["next_run_entry"] == "e-overdue"
    # Which is the whole point: earlier than anything the row is carrying.
    assert task["next_run"] < min(m["at"] for m in task["messages"])


def test_the_next_run_is_zero_with_nothing_pending(client, projects_dir):
    """0 / "" rather than a missing key — the same shape every other absent time
    on the row takes, so the client reads one thing and not two."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "hi", T9, state=schedule.SENT, fired=T9,
                           turn="ok", claude_session_id="sess-a")])
    task = _by_key(client)["sess-a"]
    assert task["next_run"] == 0
    assert task["next_run_entry"] == ""

    # And a task with no schedule anywhere near it reads the same way.
    _write_transcript(projects_dir, "sess-b", "/p", [_user("typed", T10)])
    tasks_mod.reset_cache()
    assert _by_key(client)["sess-b"]["next_run"] == 0


def test_only_pending_entries_name_the_next_run(client, projects_dir):
    """A message that already went, one that was cancelled and one that is
    mid-flight are not runs that are still to come. Only `pending` is."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([
        _entry("gone", "sent", T8, state=schedule.SENT, fired=T8, turn="ok",
               claude_session_id="sess-a"),
        _entry("dropped", "cancelled", T9, state=schedule.CANCELLED,
               claude_session_id="sess-a"),
        _entry("claimed", "already claimed", T10, state=schedule.SENDING,
               claude_session_id="sess-a"),
        _entry("tpl", "every morning", T10, state=schedule.RECURRING,
               repeats="0 9 * * *", claude_session_id="sess-a"),
        _entry("waiting", "the next one", T11, claude_session_id="sess-a"),
        _entry("later", "the one after", T12, claude_session_id="sess-a"),
    ])
    task = _by_key(client)["sess-a"]
    assert task["next_run"] == tasks_store.epoch(T11)
    assert task["next_run_entry"] == "waiting"


def test_a_recurring_occurrence_can_be_the_next_run(client, projects_dir):
    """The template never fires; its materialised occurrence is a pending entry
    like any other, and it is the run the lane is about."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([
        _entry("tpl", "every morning", T9, state=schedule.RECURRING,
               repeats="0 9 * * *", claude_session_id="sess-a"),
        _entry("occ", "every morning", T12, claude_session_id="sess-a",
               template_id="tpl"),
    ])
    task = _by_key(client)["sess-a"]
    assert task["next_run"] == tasks_store.epoch(T12)
    assert task["next_run_entry"] == "occ"


def test_a_pending_entry_with_no_id_or_no_due_names_nothing(client,
                                                            projects_dir):
    """Both halves have to be real. An entry with no id cannot be FIRED, so
    naming it would put a card at the top of Upcoming whose button sends some
    other message — the lie the pair of fields exists to remove. An entry with no
    readable due time would claim the task runs next at the epoch."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([
        _entry("", "hand-edited: no id", T8, claude_session_id="sess-a"),
        _entry("no-due", "hand-edited: no due", "", claude_session_id="sess-a"),
        _entry("real", "the one that can run", T12, claude_session_id="sess-a"),
    ])
    task = _by_key(client)["sess-a"]
    assert task["next_run"] == tasks_store.epoch(T12)
    assert task["next_run_entry"] == "real"


def test_the_next_run_is_named_on_a_task_that_has_never_run(client, tmp_path):
    """The §5 row — a pending message with no session — is the commonest
    Upcoming card there is, and it must be sortable and runnable too."""
    _seed_schedule([
        _entry("e1", "pull the news", T12, target=str(tmp_path)),
        _entry("e2", "and this", T9, target=str(tmp_path)),
    ])
    rows = _by_key(client)
    assert rows["pending:e1"]["next_run"] == tasks_store.epoch(T12)
    assert rows["pending:e1"]["next_run_entry"] == "e1"
    assert rows["pending:e2"]["next_run_entry"] == "e2"


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


def test_a_stale_in_progress_pin_does_not_outlive_its_run(client, projects_dir,
                                                          state_dir):
    """The pin is a claim about the present, and the run it named has ended.

    The Inbox's `autoFlow` writes `in_progress` for every session it sees
    running and only writes it back to `done` if that same page is still open
    to witness the stop, so a run nobody watched finish leaves the pin behind
    forever. Honouring it was five cards stuck in In Progress for days over
    entries whose turns were recorded `done`."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "x", T12, state=schedule.SENT, fired=T12,
                           turn="ok", claude_session_id="sess-a")])
    (state_dir / "triage.json").write_text(
        json.dumps({"sess-a": {"status": "in_progress"}}))

    task = _by_key(client)["sess-a"]
    assert task["live"] is False
    assert task["messages"][0]["turn"] == "done"
    assert task["status"] == "done"


def test_a_stale_in_progress_pin_on_an_empty_thread_is_dropped(client,
                                                               projects_dir,
                                                               state_dir):
    """The one the board showed with no messages at all. Nothing is running and
    there is no turn to still be open, so the pin is the only thing holding it
    in the lane."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "x", T12, state=schedule.CANCELLED,
                           claude_session_id="sess-a")])
    (state_dir / "triage.json").write_text(
        json.dumps({"sess-a": {"status": "in_progress"}}))
    assert _by_key(client)["sess-a"]["status"] == "archived"


def test_an_in_progress_pin_holds_while_the_turn_is_still_open(client,
                                                               projects_dir,
                                                               state_dir):
    """The guard that protects a genuinely running turn from the reap above.

    A turn thinking through a long tool call appends nothing to its transcript
    for minutes and reads as NOT live, so liveness alone would reap it. The
    store still has the send in flight — spawned, no `turn` verdict — which is
    `_busy_sessions`, the second guard, and the one that is right here.

    Note what this asserts about the RENDERED turn: it is `idle`, because
    `_entry_turn` folds liveness in and has no word for "sent, no verdict, not
    live". So the message the row carries cannot be the guard; the store's own
    claim has to be."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "x", T12, state=schedule.SENT, fired=T12,
                           turn="", claude_session_id="sess-a")])
    (state_dir / "triage.json").write_text(
        json.dumps({"sess-a": {"status": "in_progress"}}))

    task = _by_key(client)["sess-a"]
    assert task["live"] is False
    assert task["messages"][0]["turn"] == "idle"
    assert task["status"] == "in_progress"


def test_an_in_progress_pin_holds_over_a_send_in_flight(client, projects_dir,
                                                        state_dir):
    """A `sending` claim has not reached a transcript at all, so there is no
    turn to have settled and nothing to reap."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "x", T12, state=schedule.SENDING,
                           claude_session_id="sess-a")])
    (state_dir / "triage.json").write_text(
        json.dumps({"sess-a": {"status": "in_progress"}}))
    assert _by_key(client)["sess-a"]["status"] == "in_progress"


def test_a_stale_pin_does_not_hide_the_next_occurrence(client, projects_dir,
                                                       state_dir):
    """A recurring task whose last run left a pin behind, and whose next
    occurrence has since materialised. The card belongs in Upcoming — the pin
    is describing a run that is over, and honouring it hides the fact that this
    task is due again.

    Note that a user cannot have MEANT this pin: dropping an upcoming card on
    In Progress is `dropAction`'s run-now move, not a triage write, so the only
    thing that puts an `in_progress` pin on an upcoming task is a stale one."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([
        _entry("e1", "every day", T9, state=schedule.SENT, fired=T9, turn="ok",
               template_id="tpl", claude_session_id="sess-a"),
        _entry("e2", "every day", T12, template_id="tpl", session_id="sess-a"),
    ])
    (state_dir / "triage.json").write_text(
        json.dumps({"sess-a": {"status": "in_progress"}}))

    task = _by_key(client)["sess-a"]
    assert task["status"] == "upcoming"
    assert task["next_run_entry"] == "e2"


def test_a_pin_placed_after_the_run_ended_is_the_users_own_and_sticks(
        client, projects_dir, state_dir):
    """The reopen drag: a `done` card dropped back on In Progress. `dropLanes`
    offers that lane for a done task, so it is a real gesture, and a derivation
    that undid it on the next 20s poll would make the board unusable.

    A stamp is what tells it apart from the Inbox's automatic pin. `autoFlow`
    writes `{status}` and nothing else, so its pins carry no `at` and are
    reapable; the shell stamps its own writes, and a stamp later than anything
    that has happened in the session is a decision no run has contradicted."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "x", T12, state=schedule.SENT, fired=T12,
                           turn="ok", claude_session_id="sess-a")])
    assert _by_key(client)["sess-a"]["status"] == "done"

    (state_dir / "triage.json").write_text(json.dumps(
        {"sess-a": {"status": "in_progress", "at": str(time.time())}}))
    assert _by_key(client)["sess-a"]["status"] == "in_progress"


def test_a_pin_holds_on_a_task_that_still_has_work_ahead_of_it(
        client, projects_dir, state_dir):
    """The same deliberate pin, on a task whose next run has not happened yet.

    This is where measuring the stamp against "the last activity" went wrong.
    The row's newest message is one due TOMORROW, and a scheduled message's `at`
    is the time it was ASKED FOR — it never moves and it is not a thing that has
    occurred (see `_entry_at`). Folded into the activity the pin is compared
    against, it made every stamp a user could make look older than the session,
    so the reap fired on the next 20s poll for exactly the tasks that have work
    coming.

    The gesture is reachable: `archiveIntent` and `dropLanes` both put a card
    coming back out of Archive into In Progress, whatever it has scheduled, and
    that write is stamped by the triage endpoint.

    `last_active` deliberately still reads the due time — the List surfaces a
    message due tomorrow near the top so it can be seen BEFORE it fires — which
    is why the two are separate values and this asserts both."""
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + 86400))
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([
        _entry("e1", "every day", T9, state=schedule.SENT, fired=T9, turn="ok",
               template_id="tpl", claude_session_id="sess-a"),
        _entry("e2", "every day", soon, template_id="tpl",
               session_id="sess-a"),
    ])
    (state_dir / "triage.json").write_text(json.dumps(
        {"sess-a": {"status": "in_progress", "at": str(time.time())}}))

    task = _by_key(client)["sess-a"]
    assert task["next_run"] > time.time(), "the upcoming run is the whole case"
    assert task["status"] == "in_progress"
    assert task["last_active"] == task["next_run"], \
        "the sort still surfaces the run ahead; only the pin's clock changed"


def test_an_unreadable_created_stamp_still_leaves_a_row(client, tmp_path):
    """The dates on this row are floats and 0.0 is how they say "never", so the
    one place a stamp is parsed for activity has to answer in floats too. A None
    reaching the arithmetic raises `TypeError`, which `api_tasks` catches per
    task — the row would not be wrong, it would be GONE."""
    _seed_schedule([_entry("e1", "x", T12, target=str(tmp_path),
                           created="whenever")])
    task = _tasks(client)[0]
    assert task["status"] == "upcoming"
    assert task["last_active"] > 0, "the due time still surfaces it"


def test_a_pin_placed_before_the_run_ended_is_reaped(client, projects_dir,
                                                     state_dir):
    """The other side of the stamp. A pin from before the last run finished is
    describing that run, and the run answered it."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "x", T12, state=schedule.SENT, fired=T12,
                           turn="ok", claude_session_id="sess-a")])
    (state_dir / "triage.json").write_text(json.dumps(
        {"sess-a": {"status": "in_progress", "at": "1.0"}}))
    assert _by_key(client)["sess-a"]["status"] == "done"


def test_the_triage_write_stamps_the_pin(client, projects_dir, state_dir):
    """The stamp the reap above reads has to actually be written, or every pin
    the shell makes is reaped on the next poll."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "x", T12, state=schedule.SENT, fired=T12,
                           turn="ok", claude_session_id="sess-a")])
    r = client.post("/api/claude-sessions/triage",
                    json={"session_id": "sess-a", "status": "in_progress"})
    assert r.status_code == 200, r.text

    rec = json.loads((state_dir / "triage.json").read_text())["sess-a"]
    assert float(rec["at"]) > 0
    assert _by_key(client)["sess-a"]["status"] == "in_progress"


@pytest.mark.parametrize("pinned", ["done", "archived"])
def test_the_timeless_pins_still_win_over_a_settled_run(client, projects_dir,
                                                        state_dir, pinned):
    """Only `in_progress` is falsifiable. `done` and `archived` are filing
    decisions that stay true however long the card sits there, so the reap must
    not touch them — the user dragged the card and a derivation that undid it on
    the next poll would make the board unusable."""
    _write_transcript(projects_dir, "sess-a", "/p", [_user("hi", T9)])
    _seed_schedule([_entry("e1", "x", T12, state=schedule.ERROR,
                           error="target vanished", claude_session_id="sess-a")])
    assert _by_key(client)["sess-a"]["status"] == "failed"

    (state_dir / "triage.json").write_text(
        json.dumps({"sess-a": {"status": pinned}}))
    assert _by_key(client)["sess-a"]["status"] == pinned


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


def test_marking_a_whole_task_read_is_one_call(client, projects_dir, state_dir):
    """The List row's own button. Per-message was the only way to clear a task,
    so "I have seen all of this" cost one click per row — 89 of them on the
    longest real thread."""
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user(f"m{n}", f"2026-08-16T0{n}:00:00Z", uuid=f"u{n}")
        for n in range(1, 6)
    ])
    _write_transcript(projects_dir, "sess-b", "/p", [_user("other", T10,
                                                           uuid="ub")])
    assert _by_key(client)["sess-a"]["unread"] == 5

    r = client.post("/api/tasks/read", json={"key": "sess-a", "all": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "unread": 0}

    tasks = _by_key(client)
    assert tasks["sess-a"]["unread"] == 0
    assert all(m["unread"] is False for m in tasks["sess-a"]["messages"])
    messages = client.get("/api/tasks/sess-a/messages").json()["messages"]
    assert all(m["unread"] is False for m in messages)
    # ...and only that task. A whole-task mark is still about ONE task.
    assert tasks["sess-b"]["unread"] == 1

    # It lands as the watermark, which is what "all of it" is: one integer, no
    # id list.
    record = json.loads((state_dir / "read.json").read_text())["sess-a"]
    assert record["read_floor"] == 5
    assert record["read_ids"] == []


def test_a_whole_task_mark_leaves_a_pending_message_alone(client, projects_dir,
                                                          state_dir):
    """A message that has not happened has no response to have missed, so it is
    not unread and must not be marked — otherwise it fires already-read and the
    notification is lost silently, which is the one failure unread exists to
    prevent."""
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/p", [_user("one", T9,
                                                           uuid="u1")])
    _seed_schedule([_entry("e1", "later", T12, claude_session_id="sess-a")])
    assert _by_key(client)["sess-a"]["unread"] == 1

    assert client.post("/api/tasks/read",
                       json={"key": "sess-a", "all": True}
                       ).json() == {"ok": True, "unread": 0}
    record = json.loads((state_dir / "read.json").read_text())["sess-a"]
    # MSG-001 only. The pending MSG-002 is not in the mark at all.
    assert record["read_floor"] == 1
    assert record["read_ids"] == []
    assert not tasks_store.is_read(tasks_store.read_state(), "sess-a",
                                  "MSG-002")


def test_a_whole_task_mark_does_not_reach_a_message_that_arrives_after_it(
        client, projects_dir, state_dir):
    _already_using(state_dir)
    path = _write_transcript(projects_dir, "sess-a", "/p",
                             [_user("one", T9, uuid="u1")])
    client.post("/api/tasks/read", json={"key": "sess-a", "all": True})

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_user("two", T10, uuid="u2")) + "\n")
    task = _by_key(client)["sess-a"]
    assert task["unread"] == 1
    assert task["messages"][0]["message_id"] == "MSG-002"


def test_the_per_message_mark_is_unchanged_by_the_whole_task_one(
        client, projects_dir, state_dir):
    """Both live on one endpoint, and the narrow one must stay narrow: clicking
    MSG-003 says nothing about the MSG-002 the user scrolled past."""
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/p", [
        _user("one", T9, uuid="u1"),
        _user("two", T10, uuid="u2"),
        _user("three", T11, uuid="u3"),
    ])
    assert client.post("/api/tasks/read",
                       json={"key": "sess-a", "message_id": "MSG-003"}
                       ).json() == {"ok": True, "unread": 2}
    messages = client.get("/api/tasks/sess-a/messages").json()["messages"]
    assert {m["message_id"]: m["unread"] for m in messages} == {
        "MSG-003": False, "MSG-002": True, "MSG-001": True}

    # And the whole-task ask then clears what is left, in one request.
    assert client.post("/api/tasks/read",
                       json={"key": "sess-a", "all": True}
                       ).json() == {"ok": True, "unread": 0}


def test_marking_a_whole_task_read_before_day_one_marks_nothing(client,
                                                                projects_dir,
                                                                state_dir):
    """Nothing is unread until the baseline is stamped, so there is nothing for
    this to clear — and it must not write a floor that would then hide the first
    message that genuinely arrives."""
    path = _write_transcript(projects_dir, "sess-a", "/p",
                             [_user("one", T9, uuid="u1")])
    assert client.post("/api/tasks/read",
                       json={"key": "sess-a", "all": True}
                       ).json() == {"ok": True, "unread": 0}
    # Nothing to mark and nothing marked — the store is not even created.
    assert "sess-a" not in tasks_store.read_state()
    assert not (state_dir / "read.json").exists()

    _by_key(client)  # stamps the baseline
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_user("two", T10, uuid="u2")) + "\n")
    assert _by_key(client)["sess-a"]["unread"] == 1


def test_the_read_endpoint_refuses_nonsense(client):
    bad = client.post("/api/tasks/read", json={"key": "s", "message_id": "12"})
    assert bad.status_code == 400
    assert "MSG-nnn" in bad.json()["detail"]
    assert client.post("/api/tasks/read",
                       json={"key": "  ", "message_id": "MSG-001"}
                       ).status_code == 400
    # Neither field is a client bug, not a licence to clear a whole thread.
    missing = client.post("/api/tasks/read", json={"key": "s"})
    assert missing.status_code == 400
    assert "message_id" in missing.json()["detail"]
    # ...and so is asking for both at once.
    both = client.post("/api/tasks/read",
                       json={"key": "s", "message_id": "MSG-001", "all": True})
    assert both.status_code == 400
    assert client.post("/api/tasks/read",
                       json={"key": " ", "all": True}).status_code == 400


def test_marking_a_whole_task_that_no_longer_exists_is_not_an_error(client,
                                                                    state_dir):
    r = client.post("/api/tasks/read", json={"key": "gone", "all": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "unread": 0}
    # A whole-task mark is DEFINED by a thread, so with no thread there is
    # nothing to write — not even a record for the key that was asked about.
    assert "gone" not in tasks_store.read_state()
    assert not (state_dir / "read.json").exists()


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

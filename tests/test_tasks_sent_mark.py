"""A SEND IS A TASK BEFORE ANY FILE SAYS SO.

`tasks_watch.mark_running` used to carry one fact — "something started here" —
and the row it lit up had to already exist. That covered a follow-up into a
conversation with a transcript and nothing else: a BRAND-NEW chat has no
transcript, no scheduled entry and no session on disk for its first seconds, so
the first thing a user does in this app was the one thing the Tasks page could
not show.

The mark now carries the WORDS and the TARGET as well (`POST /api/tasks/running`
`text`/`file`, `tasks_watch.sent_marks`), which is enough to be a row on its own:

* it folds into the thread as the newest message, shaped like a schedule entry
  that has been sent and not answered — so the preview shows the sentence and
  `_status` reads `in_progress` off the same one fact (`_fold_sent_mark`);
* a session nothing else lists becomes a PLACEHOLDER row keyed by that session
  id, placed in the project the send named (`_collect`, `_place`);
* and all of it has a FUSE. When the mark runs out the row is whatever disk
  says: a real row if a transcript landed, and NO ROW AT ALL if nothing did —
  a send whose run died on the spot leaves nothing behind, because nothing
  happened.

The dedupe is the part worth being careful about, and it has two halves: the
transcript prompt the send becomes, and the schedule entry the send came from.
Both are tested below, because showing the user their own sentence twice is
exactly what this whole listing promises never to do.

Nothing here reads the real ~/.claude — every path is under tmp_path.
"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import schedule, tasks_store, tasks_watch
from fused_render._view_url_codec import canonical_fs_path
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
    monkeypatch.setattr(sessions_mod, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
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


SID = "11111111-2222-4333-8444-555555555555"


def _near_now(offset_sec: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + offset_sec))


def _user(text, ts, uuid=None):
    record = {"type": "user", "timestamp": ts,
              "message": {"role": "user",
                          "content": [{"type": "text", "text": text}]}}
    if uuid is not None:
        record["uuid"] = uuid
    return record


def _write_transcript(projects_dir, session_id, cwd, records):
    d = projects_dir / ("-encoded-" + session_id)
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


def _by_key(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    return {t["key"]: t for t in r.json()["tasks"]}


def _expire(session_id=SID):
    """Run the mark's fuse down without waiting fifteen real seconds — and
    ANNOUNCE it, which is the pair `tick` makes. The expiry reads the clock, so
    moving the expiry is how a test moves the clock; the bump is what the page
    holding the long-poll is actually waiting for."""
    tasks_watch._marks[session_id]["until"] = time.time() - 1
    tasks_watch._bump(tasks_watch._expire_marks(time.time()))


# --------------------------------------------------- the send as a whole row


def test_a_send_into_nothing_is_a_row_with_the_words_on_it(client, tmp_path):
    """THE WHOLE FEATURE IN ONE ROW. No transcript, no entry, no session on disk
    — just a page saying what it sent and where. The row exists, it is keyed by
    the session the turn runs in, it is In Progress, and it shows the sentence."""
    target = tmp_path / "proj" / "app.py"
    target.parent.mkdir()
    target.write_text("x = 1\n")
    tasks_watch.mark_running(SID, text="pull today's news", file=str(target))

    rows = _by_key(client)
    assert SID in rows, "a send with nothing behind it is still a task"
    row = rows[SID]
    assert row["session_id"] == SID
    assert row["status"] == "in_progress"
    assert row["live"] is True
    assert row["title"] == "pull today's news"
    assert row["title_source"] == "message"
    assert row["messages"][0]["body"] == "pull today's news"
    assert row["messages"][0]["state"] == "sent"
    assert row["messages"][0]["turn"] == "", "a turn that has not reported an end"


def test_the_row_lands_in_the_project_the_send_named(client, tmp_path):
    """`project` and `target` come off the mark's `file` — the folder of a file,
    the folder itself for a folder. Without it the placeholder would sit in no
    project at all, which is the one thing the Cards wall and the desk cannot
    draw."""
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "app.py"
    target.write_text("x = 1\n")
    tasks_watch.mark_running(SID, text="go", file=str(target))
    row = _by_key(client)[SID]
    # `project`/`target` come back in the shell's canonical form (forward
    # slashes on a drive-letter path — router.ts `rootedFsPath` / D-number
    # in canonical_fs_path's docstring), not os.path's native separator, so
    # `str(proj)`/`str(target)` on Windows must be canonicalized before the
    # comparison rather than compared as-is.
    assert row["project"] == canonical_fs_path(str(proj))
    assert row["target"] == canonical_fs_path(str(target))

    tasks_mod.reset_cache()
    tasks_watch.mark_running(SID, text="go", file=str(proj))
    row = _by_key(client)[SID]
    assert row["project"] == canonical_fs_path(str(proj))
    assert row["target"] == canonical_fs_path(str(proj)), \
        "a folder target is its own target"


def test_a_marked_send_lights_up_a_task_that_already_exists(client,
                                                            projects_dir):
    """The ordinary follow-up: the conversation has a transcript, the send is a
    new message in it. One row, not two — the mark is keyed by the session, and
    the session IS the key."""
    _write_transcript(projects_dir, SID, "/home/me/proj",
                      [_user("one", _near_now(-600), uuid="u1")])
    tasks_watch.mark_running(SID, text="and now the weather", file="/home/me/proj")

    rows = _by_key(client)
    assert list(rows) == [SID]
    row = rows[SID]
    assert row["status"] == "in_progress"
    assert row["message_count"] == 2
    assert [m["body"] for m in row["messages"]] == ["and now the weather", "one"]


def test_a_mark_with_no_words_is_still_only_a_liveness_floor(client,
                                                             projects_dir):
    """A caller that says nothing adds nothing to the thread — an empty bubble
    would be worse than the ring alone — and the row is still live, which is the
    contract this endpoint has always kept."""
    _write_transcript(projects_dir, SID, "/home/me/proj",
                      [_user("one", _near_now(-600), uuid="u1")])
    tasks_watch.mark_running(SID)

    row = _by_key(client)[SID]
    assert row["status"] == "in_progress"
    assert row["message_count"] == 1
    assert [m["body"] for m in row["messages"]] == ["one"]


# ------------------------------------------------------------- the dedupe


def test_the_transcript_catching_up_does_not_say_it_twice(client, projects_dir):
    """The prompt has landed. The mark is still live — it has a fifteen-second
    fuse and the round trip took two — and the message is now on disk, so the
    mark has nothing left to say. ONE message."""
    tasks_watch.mark_running(SID, text="pull today's news", file="/home/me/proj")
    _write_transcript(projects_dir, SID, "/home/me/proj", [
        _user("pull today's news", _near_now(-2), uuid="u1"),
    ])

    row = _by_key(client)[SID]
    assert tasks_watch.is_marked_running(SID), "the mark has not run out yet"
    assert row["message_count"] == 1
    assert [m["body"] for m in row["messages"]] == ["pull today's news"]
    assert row["status"] == "in_progress"


def test_the_transcript_prompt_wears_the_app_state_block_and_still_matches(
        client, projects_dir):
    """What the page sends is `<live-app-state>…</live-app-state>` + the words;
    what it marks is the words alone. The listing strips the block off the
    transcript prompt before comparing, so the two are still ONE message."""
    tasks_watch.mark_running(SID, text="pull today's news", file="/home/me/proj")
    _write_transcript(projects_dir, SID, "/home/me/proj", [
        _user("<live-app-state>\n{\"file\": \"x\"}\n</live-app-state>\n"
              "pull today's news", _near_now(-2), uuid="u1"),
    ])
    row = _by_key(client)[SID]
    assert row["message_count"] == 1
    assert [m["body"] for m in row["messages"]] == ["pull today's news"]
    assert row["status"] == "in_progress"


def test_the_same_words_said_ten_minutes_ago_are_a_different_message(
        client, projects_dir):
    """The dedupe is TIME-QUALIFIED for a transcript prompt, and this is why:
    asking the same thing again is the ordinary way a chat works. An old prompt
    with the same words is not this send, and swallowing the new one would lose
    the message the user just typed."""
    _write_transcript(projects_dir, SID, "/home/me/proj", [
        _user("run it again", _near_now(-600), uuid="u1"),
    ])
    tasks_watch.mark_running(SID, text="run it again", file="/home/me/proj")

    row = _by_key(client)[SID]
    assert row["message_count"] == 2
    assert [m["body"] for m in row["messages"]] == ["run it again", "run it again"]


def test_a_scheduled_entry_with_the_same_message_is_the_message(client):
    """A scheduled send marks too, and the entry it came from is ALREADY a
    message in this thread — with its own state, its own verdict and its own
    due time. The dedupe against it is deliberately not time-qualified: a
    caught-up run's `at` is the day it was DUE, which can be nowhere near when
    it actually went."""
    schedule._write([_entry("e1", "the daily digest", _near_now(-86400),
                            state=schedule.SENT, fired=_near_now(-3),
                            claude_session_id=SID)])
    tasks_watch.mark_running(SID, text="the daily digest", file="/tmp")

    row = _by_key(client)[SID]
    assert row["message_count"] == 1
    assert [m["body"] for m in row["messages"]] == ["the daily digest"]
    assert row["messages"][0]["entry_id"] == "e1", "the STORE's message, not the mark's"


# --------------------------------------------------------------- the fuse


def test_a_placeholder_whose_send_died_leaves_no_row_behind(client, tmp_path):
    """NOTHING HAPPENED, SO THERE IS NOTHING. A run that never started wrote no
    transcript, made no session and left no entry — so when the mark runs out
    there is nothing for a row to be about, and the row goes. This is the whole
    reason the placeholder is allowed to exist at all: it cannot outlive the one
    fact holding it up."""
    tasks_watch.mark_running(SID, text="go", file=str(tmp_path))
    assert SID in _by_key(client)

    _expire()
    tasks_mod.reset_cache()
    assert SID not in _by_key(client)


def test_a_placeholder_whose_transcript_landed_becomes_that_row(
        client, projects_dir):
    """The other ending, and the reason the placeholder is keyed by the SESSION
    ID: the transcript row IS this row. Same key, same task number — nothing
    swaps under the reader when the mark's fuse burns out."""
    tasks_watch.mark_running(SID, text="pull today's news", file="/home/me/proj")
    before = _by_key(client)[SID]

    _write_transcript(projects_dir, SID, "/home/me/proj", [
        _user("pull today's news", _near_now(-2), uuid="u1"),
    ])
    _expire()
    tasks_mod.reset_cache()

    after = _by_key(client)[SID]
    assert after["task_id"] == before["task_id"], "the number does not move"
    assert after["project"] == "/home/me/proj"
    assert after["message_count"] == 1
    assert after["messages"][0]["body"] == "pull today's news"
    assert after["messages"][0]["anchor"], "a real record, with somewhere to scroll"


# -------------------------------------------------------------- the endpoint


def test_the_running_endpoint_takes_the_words_and_the_target(client, tmp_path):
    """`POST /api/tasks/running` is where the page says all of it, in the one
    call it already made. Both fields are optional and both are stripped."""
    target = tmp_path / "app.py"
    target.write_text("x = 1\n")
    r = client.post("/api/tasks/running",
                    json={"session_id": SID, "text": "  pull the news  ",
                          "file": "  %s  " % target})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "session_id": SID}
    assert tasks_watch.sent_marks()[SID]["text"] == "pull the news"
    assert tasks_watch.sent_marks()[SID]["file"] == str(target)


def test_the_running_endpoint_still_takes_a_bare_session_id(client):
    """An older client, or any caller that only wants the liveness floor, sends
    neither field and gets exactly the mark this endpoint has always made."""
    r = client.post("/api/tasks/running", json={"session_id": SID})
    assert r.status_code == 200, r.text
    assert tasks_watch.sent_marks()[SID] == {
        "at": pytest.approx(time.time(), abs=5), "text": "", "file": ""}


def test_the_running_endpoint_refuses_the_wrong_shape(client):
    """Typed, so a client bug is a 422 and not a mark carrying a dict where a
    sentence should be."""
    r = client.post("/api/tasks/running",
                    json={"session_id": SID, "text": {"oops": 1}})
    assert r.status_code == 422
    assert not tasks_watch.sent_marks()


def test_the_idle_call_takes_the_words_away_with_the_mark(client):
    """`mark_idle` retires the whole record. The reply landed, so the row is
    whatever disk says — and with nothing on disk that is no row."""
    client.post("/api/tasks/running",
                json={"session_id": SID, "text": "go", "file": "/tmp"})
    assert tasks_watch.sent_marks()
    client.post("/api/tasks/idle", json={"session_id": SID})
    assert tasks_watch.sent_marks() == {}
    assert SID not in _by_key(client)


def test_sent_marks_never_hands_back_one_that_has_run_out(client):
    """A snapshot of LIVE marks only — a listing must not see a mark expire
    halfway down it — and the expired entry is left for `_expire_marks` to drop
    and ANNOUNCE, because retiring a row with no generation bump behind it
    leaves the page drawing a send that is over."""
    tasks_watch.mark_running(SID, text="go", file="/tmp")
    tasks_watch._marks[SID]["until"] = time.time() - 1
    assert tasks_watch.sent_marks() == {}
    assert SID in tasks_watch._marks, "still there, still owed an announcement"
    assert tasks_watch._expire_marks(time.time()) == {SID}


def test_the_text_a_mark_remembers_is_capped(client):
    """A ceiling on what a page can park in this process. The row draws one line
    of it either way."""
    tasks_watch.mark_running(SID, text="x" * 10_000, file="/tmp")
    assert len(tasks_watch.sent_marks()[SID]["text"]) == tasks_watch.MARK_TEXT_MAX


# --------------------------------------------------- the rest of the audit


def test_marking_a_message_read_rings_the_long_poll(client, projects_dir):
    """`unread` is a LISTED fact — it rides on every row and feeds the sidebar's
    own dot — so reading a message has to announce itself like every other verb
    in this router. It was the one that did not."""
    _write_transcript(projects_dir, SID, "/home/me/proj",
                      [_user("one", _near_now(-600), uuid="u1")])
    _by_key(client)  # day-one baseline
    before = tasks_watch.generation()
    r = client.post("/api/tasks/read", json={"key": SID, "message_id": "MSG-001"})
    assert r.status_code == 200, r.text
    assert tasks_watch.generation() > before


def test_marking_a_whole_task_read_rings_once_and_only_if_it_moved(
        client, projects_dir, state_dir):
    """Same ring for the whole-task branch — and NOT for a task that had nothing
    unread, because a no-op on disk must not cost every watcher a redraw."""
    (state_dir / "read.json").write_text(json.dumps({tasks_store.INIT_KEY: 0.0}))
    _write_transcript(projects_dir, SID, "/home/me/proj",
                      [_user("one", _near_now(-600), uuid="u1")])
    before = tasks_watch.generation()
    assert client.post("/api/tasks/read",
                       json={"key": SID, "all": True}).status_code == 200
    rang = tasks_watch.generation()
    assert rang > before

    assert client.post("/api/tasks/read",
                       json={"key": SID, "all": True}).status_code == 200
    assert tasks_watch.generation() == rang, "nothing moved, nothing announced"


def test_filing_a_session_rings_the_tasks_long_poll(client, projects_dir):
    """`/api/claude-sessions/triage` writes the very file the Tasks listing asks
    whether a task is archived, so a status written there moves the row's lane
    in every window — and had no way to say so."""
    _write_transcript(projects_dir, SID, "/home/me/proj",
                      [_user("one", _near_now(-600), uuid="u1")])
    before = tasks_watch.generation()
    r = client.post("/api/claude-sessions/triage",
                    json={"session_id": SID, "status": "archived"})
    assert r.status_code == 200, r.text
    assert tasks_watch.generation() > before
    assert _by_key(client)[SID]["status"] == "archived"


# ------------------------------------------------------- the permission card


class _CardAgent:
    """The parts of agent.py the card watch reads: where the runs live, and
    where a run keeps its cards."""

    def __init__(self, runs):
        self.RUNS = str(runs)

    def _perm_dir(self, run_dir):
        return os.path.join(run_dir, "perm")

    def _session_from_out(self, run_dir):
        return ""


@pytest.fixture()
def carded(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    run_dir = runs / "20260916-120000-abcdef"
    (run_dir / "perm").mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"file": "/home/me/proj", "resumed_from": "",
                    "session_id": SID}))
    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: _CardAgent(runs))
    return run_dir


def test_a_card_raised_out_of_process_rings_the_long_poll(carded):
    """NOBODY WHO WRITES A CARD CAN CALL `notify`. The card is written by
    `permission_server.py` — an MCP server the CLI spawns — and answered by
    `agent._decide`, which runs in the executor's subprocess. Neither is this
    process and neither may import `fused_render` at all, so the only thing
    they share with the listing is the file itself. One stat per run dir is
    what turns that into news.

    A run's FIRST sighting is a baseline: `_start` creates `perm/` empty before
    the CLI can raise anything, so treating a new run dir as news would ring on
    every spawn for a card that does not exist."""
    assert tasks_watch._read_permission_cards() == set(), "the baseline"

    (carded / "perm" / "p1.req.json").write_text(
        json.dumps({"id": "p1", "tool": "Bash", "input": {}}))
    assert tasks_watch._read_permission_cards() == {SID}
    assert tasks_watch._read_permission_cards() == set(), "announced once"


def test_answering_the_card_rings_it_again(carded):
    """The answer lands in the same directory, so the same stat sees it — which
    is the half that matters for the reader who has just unblocked the run and
    is watching the row leave Needs attention."""
    tasks_watch._read_permission_cards()
    (carded / "perm" / "p1.req.json").write_text(json.dumps({"id": "p1"}))
    assert tasks_watch._read_permission_cards() == {SID}

    (carded / "perm" / "p1.decision.json").write_text(
        json.dumps({"decision": "allow"}))
    assert tasks_watch._read_permission_cards() == {SID}


def test_the_card_watch_names_the_session_a_fresh_run_minted(carded):
    """The run has no `session` file and has announced nothing — the state every
    brand-new chat is in for its first seconds, which is also when its very
    first tool call raises its very first card. `meta["session_id"]` is the only
    id there is, and without it the card belonged to no session this listing
    knew."""
    assert not os.path.exists(carded / "session")
    tasks_watch._read_permission_cards()
    (carded / "perm" / "p1.req.json").write_text(json.dumps({"id": "p1"}))
    assert tasks_watch._read_permission_cards() == {SID}


# ------------------------------------------------------------- the fast lane


def test_the_send_arrives_and_departs_down_the_changes_long_poll(client,
                                                                 tmp_path):
    """THE LATENCY THIS IS ALL FOR. `mark_running` bumps the generation, so the
    page holding `/api/tasks/changes` is handed the whole placeholder row in the
    same moment the send happened — not on the next 20-second listing. And when
    the mark's fuse burns out with nothing behind it, `_expire_marks` bumps
    again and the same lane names the key `gone`, so the row leaves the page the
    way it arrived."""
    at = tasks_watch.generation()
    tasks_watch.mark_running(SID, text="pull today's news", file=str(tmp_path))

    r = client.get("/api/tasks/changes", params={"since": at, "wait": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [row["key"] for row in body["rows"]] == [SID]
    assert body["rows"][0]["status"] == "in_progress"
    assert body["rows"][0]["messages"][0]["body"] == "pull today's news"

    at = body["generation"]
    _expire()
    body = client.get("/api/tasks/changes",
                      params={"since": at, "wait": 0}).json()
    assert body["rows"] == []
    assert body["gone"] == [SID]


def test_a_re_mark_without_words_keeps_the_words(client, tmp_path):
    """Re-attach and the poll's session-id ping mark without text on purpose.
    The listing must go on drawing the send's words (bugbot)."""
    tasks_watch.mark_running(SID, text="pull today's news", file=str(tmp_path))
    tasks_watch.mark_running(SID, turn=5.0)
    mark = tasks_watch.sent_marks()[SID]
    assert mark["text"] == "pull today's news"
    assert mark["file"] == str(tmp_path)
    row = _by_key(client)[SID]
    assert row["status"] == "in_progress"
    assert row["messages"][0]["body"] == "pull today's news"


def test_a_re_mark_with_new_words_replaces_them(client, tmp_path):
    tasks_watch.mark_running(SID, text="first", file=str(tmp_path))
    tasks_watch.mark_running(SID, text="second")
    mark = tasks_watch.sent_marks()[SID]
    assert (mark["text"], mark["file"]) == ("second", str(tmp_path))


def test_the_full_thread_holds_the_in_flight_send(client, tmp_path):
    """`GET /api/tasks/{key}/messages` must show the sentence the row shows."""
    tasks_watch.mark_running(SID, text="pull today's news", file=str(tmp_path))
    r = client.get(f"/api/tasks/{SID}/messages")
    assert r.status_code == 200
    bodies = [m["body"] for m in r.json()["messages"]]
    assert bodies == ["pull today's news"]
    assert r.json()["messages"][0]["message_id"] == "MSG-001"


def test_the_in_flight_send_takes_the_next_message_id_in_the_thread(
        client, projects_dir):
    """Listing and thread must agree on the id, or marking it read names the
    wrong message (bugbot)."""
    _write_transcript(projects_dir, SID, "/home/me/proj", [
        _user("first thing", _near_now(-600), uuid="u1"),
    ])
    tasks_watch.mark_running(SID, text="second thing", file="/home/me/proj")
    row = _by_key(client)[SID]
    listed = {m["body"]: m["message_id"] for m in row["messages"]}
    thread = {m["body"]: m["message_id"]
              for m in client.get(f"/api/tasks/{SID}/messages").json()["messages"]}
    assert listed["second thing"] == thread["second thing"] == "MSG-002"
    assert thread["first thing"] == "MSG-001"
    r = client.post("/api/tasks/read", json={"key": SID, "message_id": "MSG-002"})
    assert r.status_code == 200, r.text


def test_a_wordless_mark_on_an_unknown_session_is_not_a_row(client, tmp_path):
    """Nothing to show, so nothing to list: a blank card is worse than none."""
    tasks_watch.mark_running(SID, file=str(tmp_path))
    assert SID not in _by_key(client)


def test_the_card_scan_sleeps_while_nothing_is_alive(monkeypatch):
    """No registry row and no live mark means no run can have raised a card —
    the runs tree is not listed at all on that tick."""
    calls = []
    monkeypatch.setattr(tasks_watch, "_read_permission_cards",
                        lambda: calls.append(1) or set())
    monkeypatch.setattr(tasks_watch, "_read_registry", lambda: set())
    monkeypatch.setattr(tasks_watch, "_read_live_transcripts", lambda: set())
    with tasks_watch._cond:
        tasks_watch._registry.clear()
        tasks_watch._marks.clear()
    tasks_watch.tick()
    assert calls == []
    tasks_watch.mark_running(SID, text="hi")
    tasks_watch.tick()
    assert calls == [1]

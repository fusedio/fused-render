"""Drafts, round 4: one record per draft, versioned, pushed.

What is under test is the promise of design-drafts-one-record.md — duplicates
and resurrections are impossible BY CONSTRUCTION, not by client bookkeeping:

* every record carries a `version` the server increments on each write;
* a write may be conditional on it (`If-Match`), and a loser gets a 409 with the
  record it lost to instead of overwriting somebody's words;
* the Schedule hop edits the record it was opened on — a `new:<file>` chat draft
  carries the modal's `form` — so no `draft:<id>` and no `from_chat_key` copy;
* `POST /api/schedule` spends that draft by its chat key (`draft_key`), making
  the same two moves it has always made for `draft_id`;
* `/api/tasks/changes` says which drafts moved, so two windows on one key agree.

The fixtures are `test_drafts.py`'s, shared through `conftest`-style imports of
the same helpers — everything lives under tmp_path, and nothing here reads the
developer's own ~/.claude or ~/.fused-render.
"""
import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from fused_render import drafts, schedule_wake, tasks_store, tasks_watch
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod

WRITE = {"X-Fused": "1"}
T9 = "2026-09-11T09:00:00Z"


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
    """The one global dir all three stores share — see `test_drafts.py`."""
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "STATE_DIR", str(d))
    monkeypatch.setattr(drafts, "STATE_DIR", str(d))
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
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture(autouse=True)
def runs(tmp_path, monkeypatch):
    """A runs tree of our own, and the stand-in that reads it (`test_drafts`)."""
    d = tmp_path / "runs"
    d.mkdir()

    class _Agent:
        RUNS = str(d)

        def _permissions(self, run_dir):
            return []

        def _alive(self, run_dir):
            return False

        def _session_from_out(self, run_dir):
            return ""

    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: _Agent())
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _chat_url(key: str) -> str:
    return "/api/drafts/chat/" + quote(key, safe="")


def _by_key(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    return {t["key"]: t for t in r.json()["tasks"]}


def _transcript(projects_dir, session_id, cwd="/home/me/proj"):
    d = projects_dir / ("-encoded-" + session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.jsonl").write_text(json.dumps(
        {"type": "user", "timestamp": T9, "cwd": cwd, "sessionId": session_id,
         "uuid": session_id + "-0",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "one"}]}}) + "\n")


# --------------------------------------------------------------- the version


def test_every_write_moves_the_version_on(state_dir):
    """One per key, one at a time. The number is what a second window's save is
    measured against, so it has to move on every write and never backwards."""
    assert drafts.put_chat("sess-a", "one")["version"] == 1
    assert drafts.put_chat("sess-a", "one two")["version"] == 2
    assert drafts.get_chat("sess-a")["version"] == 2
    # A second key keeps its own count — versions are per record, not a store
    # generation.
    assert drafts.put_chat("sess-b", "elsewhere")["version"] == 1

    record, _ = drafts.put_task("draft-0001", {"title": "a form"})
    assert record["version"] == 1
    record, _ = drafts.put_task("draft-0001", {"description": "and a body"})
    assert record["version"] == 2


def test_a_discarded_draft_comes_back_at_one(state_dir):
    """A version belongs to the RECORD, not to the key for all time. Somebody
    holding version 9 of a draft that has since been discarded and started
    again collides with the new one rather than matching it — which is the
    resurrection this round exists to make impossible."""
    drafts.put_chat("sess-a", "one")
    drafts.put_chat("sess-a", "one two")
    assert drafts.delete_chat("sess-a") is True
    assert drafts.put_chat("sess-a", "written afresh")["version"] == 1
    with pytest.raises(drafts.VersionConflict):
        drafts.put_chat("sess-a", "the stale window's words", if_version=2)


def test_a_record_written_before_versions_reads_as_zero(state_dir, client):
    """Every draft on disk when this shipped has no number, and 0 is what a
    client that has never written one holds — so the two agree without a
    migration."""
    (state_dir / "drafts.json").write_text(json.dumps(
        {"chat": {"sess-a": {"text": "from an older build", "updated_at": 1.0}},
         "task": {}}))
    assert drafts.get_chat("sess-a")["version"] == 0
    assert client.get("/api/drafts").json()["chat"]["sess-a"]["version"] == 0
    # ...and the first write numbers it.
    assert drafts.put_chat("sess-a", "touched", if_version=0)["version"] == 1


def test_an_old_record_carrying_from_chat_key_still_loads(state_dir, client):
    """The field the hop's copy needed is gone (§1). A store written by the
    build before this one still has it, and reading it must be a drop rather
    than a refusal — the words in that record are the whole point of the file.
    """
    (state_dir / "drafts.json").write_text(json.dumps(
        {"chat": {}, "task": {"draft-0001": {
            "title": "roll up the PRs", "from_chat_key": "new:/tmp/x.py",
            "created_at": 1.0, "updated_at": 2.0}}}))
    stored = drafts.get_task("draft-0001")
    assert stored["title"] == "roll up the PRs"
    assert "from_chat_key" not in stored
    body = client.get("/api/drafts").json()["task"]["draft-0001"]
    assert body["title"] == "roll up the PRs"
    assert "from_chat_key" not in body
    # ...and a client that still SENDS one is not refused either.
    r = client.put("/api/drafts/task/draft-0001",
                   json={"title": "roll up", "from_chat_key": "new:/tmp/x.py"})
    assert r.status_code == 200, r.text
    assert "from_chat_key" not in r.json()


# ------------------------------------------------------------- the If-Match


def test_a_conditional_write_that_matches_goes_through(client):
    r = client.put(_chat_url("sess-a"), json={"text": "one"})
    version = r.json()["draft"]["version"]
    r = client.put(_chat_url("sess-a"), json={"text": "one two"},
                   headers={"If-Match": str(version)})
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["version"] == version + 1


def test_a_stale_write_is_refused_with_the_record_it_lost_to(client):
    """409 AND THE RECORD, because a bare refusal is not something an editor can
    act on: what it does next — adopt, or re-send its own words — is read out of
    this body (§2)."""
    client.put(_chat_url("sess-a"), json={"text": "one"})
    client.put(_chat_url("sess-a"), json={"text": "the other window's words"})

    r = client.put(_chat_url("sess-a"), json={"text": "mine"},
                   headers={"If-Match": "1"})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "version"
    assert body["version"] == 2
    assert body["key"] == "sess-a"
    assert body["record"]["text"] == "the other window's words"
    assert body["record"]["version"] == 2
    # Nothing was written: a refused write is a write that did not happen.
    assert drafts.get_chat("sess-a")["text"] == "the other window's words"


def test_a_stale_write_against_a_deleted_record_says_so(client):
    """`record: null` is the other thing that can have happened — somebody
    discarded it while this editor was typing."""
    client.put(_chat_url("sess-a"), json={"text": "one"})
    client.delete(_chat_url("sess-a"))
    r = client.put(_chat_url("sess-a"), json={"text": "mine"},
                   headers={"If-Match": "1"})
    assert r.status_code == 409
    assert r.json() == {"error": "version", "record": None, "version": 0,
                        "key": "sess-a"}


def test_the_version_may_ride_in_the_body_and_may_be_an_etag(client):
    """Two spellings of one header, plus the body field, because `If-Match` is
    quoted by some clients and unavailable to others (a DELETE with no headers
    to spare)."""
    client.put(_chat_url("sess-a"), json={"text": "one"})
    r = client.put(_chat_url("sess-a"), json={"text": "two", "version": 1})
    assert r.status_code == 200, r.text
    r = client.put(_chat_url("sess-a"), json={"text": "three"},
                   headers={"If-Match": 'W/"2"'})
    assert r.status_code == 200, r.text
    r = client.put(_chat_url("sess-a"), json={"text": "four", "version": 2})
    assert r.status_code == 409, r.text


def test_an_unconditional_write_still_wins(client):
    """Every client written before this round sends no version at all, and it
    must go on working exactly as it did — which is also what makes the header
    safe to add (§2, "Missing header = unconditional")."""
    client.put(_chat_url("sess-a"), json={"text": "one"})
    client.put(_chat_url("sess-a"), json={"text": "two"})
    r = client.put(_chat_url("sess-a"), json={"text": "unconditional"})
    assert r.status_code == 200, r.text
    assert drafts.get_chat("sess-a")["text"] == "unconditional"
    # A malformed header is read as absent rather than refused: a draft write
    # must not be lost to a header.
    r = client.put(_chat_url("sess-a"), json={"text": "still fine"},
                   headers={"If-Match": "nonsense"})
    assert r.status_code == 200, r.text


def test_a_conditional_delete_is_refused_when_the_words_moved(client):
    """The trash on a row is aimed at what the row was showing. If somebody has
    typed since, the delete loses and says what it lost to."""
    client.put(_chat_url("sess-a"), json={"text": "one"})
    client.put(_chat_url("sess-a"), json={"text": "one, and more since"})
    r = client.request("DELETE", _chat_url("sess-a"), json={"version": 1})
    assert r.status_code == 409
    assert r.json()["record"]["text"] == "one, and more since"
    assert drafts.get_chat("sess-a") is not None

    r = client.request("DELETE", _chat_url("sess-a"),
                       headers={"If-Match": "2"})
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True
    assert drafts.get_chat("sess-a") is None


def test_the_task_routes_take_the_same_header(client):
    r = client.put("/api/drafts/task/draft-0001", json={"title": "a form"})
    assert r.json()["draft"]["version"] == 1
    r = client.put("/api/drafts/task/draft-0001", json={"title": "edited"},
                   headers={"If-Match": "1"})
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["version"] == 2

    r = client.put("/api/drafts/task/draft-0001", json={"title": "stale"},
                   headers={"If-Match": "1"})
    assert r.status_code == 409
    assert r.json() == {"error": "version", "version": 2,
                        "draft_id": "draft-0001",
                        "record": drafts.get_task("draft-0001")}
    assert drafts.get_task("draft-0001")["title"] == "edited"

    r = client.request("DELETE", "/api/drafts/task/draft-0001",
                       headers={"If-Match": "1"})
    assert r.status_code == 409
    r = client.request("DELETE", "/api/drafts/task/draft-0001",
                       headers={"If-Match": "2"})
    assert r.status_code == 200, r.text
    assert drafts.get_task("draft-0001") is None


def test_a_bound_session_is_versioned_by_the_record_it_writes(client, projects_dir):
    """One record, two doors — and one version. A composer writing to a session
    whose words live in a bound form is writing THAT record, so the number it is
    handed and the number it must send back are the form's."""
    _transcript(projects_dir, "sess-a")
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Ship it", "session_id": "sess-a"})
    seen = client.get("/api/drafts").json()["chat"]["sess-a"]
    assert seen["bound_draft"] == "draft-0001"
    assert seen["version"] == drafts.get_task("draft-0001")["version"] == 1

    r = client.put(_chat_url("sess-a"), json={"text": "Ship it today"},
                   headers={"If-Match": "1"})
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["version"] == 2
    assert drafts.get_task("draft-0001")["title"] == "Ship it today"

    r = client.put(_chat_url("sess-a"), json={"text": "stale"},
                   headers={"If-Match": "1"})
    assert r.status_code == 409
    assert r.json()["record"]["bound_draft"] == "draft-0001"


# ------------------------------------------------ the hop edits one record


def test_a_chat_draft_carries_the_hop_s_form(client, tmp_path):
    """§1. The Schedule hop's New task modal seeds from this key and saves back
    to it: no `draft:<id>` is minted, so the words and the settings are one
    record with one number."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    r = client.put(_chat_url(key), json={
        "text": "roll up yesterday's PRs",
        "form": {"when": "2026-09-18T09:00", "repeat": "weekly",
                 "model": "opus", "target": str(folder)}})
    assert r.status_code == 200, r.text
    form = r.json()["draft"]["form"]
    assert form["when"] == "2026-09-18T09:00"
    assert form["repeat"] == "weekly"
    assert form["model"] == "opus"

    # It round-trips through the file...
    assert client.get("/api/drafts").json()["chat"][key]["form"]["when"] == \
        "2026-09-18T09:00"
    # ...and no second record was made anywhere.
    assert client.get("/api/drafts").json()["task"] == {}
    rows = _by_key(client)
    assert list(rows) == [key], "one draft, one row"


def test_the_composer_s_own_autosave_keeps_the_settings(client, tmp_path):
    """The other door writes text and nothing else, and must not be able to
    clear a time somebody chose in the modal: a form that is not sent is a form
    that was not mentioned."""
    key = "new:" + str(tmp_path)
    client.put(_chat_url(key), json={"text": "one",
                                     "form": {"when": "2026-09-18T09:00",
                                              "model": "opus"}})
    r = client.put(_chat_url(key), json={"text": "one two"})
    assert r.json()["draft"]["form"]["when"] == "2026-09-18T09:00"
    assert r.json()["draft"]["form"]["model"] == "opus"
    # ...and a form that names a field DOES move it, including to nothing —
    # while the fields it says nothing about stand.
    r = client.put(_chat_url(key), json={"text": "one two", "form": {"when": None}})
    form = r.json()["draft"]["form"]
    assert form["when"] is None and form["model"] == "opus"
    # A form of nothing but blanks is no form at all, and is not stored as one.
    r = client.put(_chat_url(key), json={"text": "one two", "form": {"model": ""}})
    assert r.json()["draft"]["form"] == {}


def test_clearing_the_words_keeps_the_settings_and_the_row_goes(client, tmp_path):
    """The same bargain a bound form already makes (`_put_bound`): a blank box
    clears the WORDS, and the folder, time and model somebody spent a minute
    choosing stay. What every reader sees is no draft — no row, no chip."""
    key = "new:" + str(tmp_path)
    client.put(_chat_url(key), json={"text": "one",
                                     "form": {"when": "2026-09-18T09:00"}})
    assert client.put(_chat_url(key), json={"text": ""}).json()["draft"] is None
    assert drafts.get_chat(key) is None
    assert key not in _by_key(client)
    # ...and the settings come back with the next thing typed.
    r = client.put(_chat_url(key), json={"text": "typing again"})
    assert r.json()["draft"]["form"]["when"] == "2026-09-18T09:00"


def test_a_hopped_draft_row_reads_as_scheduled_for_later(client, tmp_path):
    """The listing half (§ "Server model deltas"): a draft the reader set for
    Friday has to SAY Friday on the Upcoming row, or the hop reads as having
    done nothing."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "roll up the PRs"})
    row = _by_key(client)[key]
    assert row["form"] is None, "no hop yet: nothing to reopen a modal on"

    client.put(_chat_url(key), json={"text": "roll up the PRs",
                                     "form": {"when": "2026-09-18T09:00",
                                              "repeat": "weekly"}})
    row = _by_key(client)[key]
    assert row["form"]["when"] == "2026-09-18T09:00"
    assert row["form"]["repeat"] == "weekly"
    assert row["form"]["updated_at"] == row["updated_at"]
    # The row is still a draft, and still keyed and numbered as the chat it is.
    assert row["kind"] == "draft" and row["draft_kind"] == "chat"
    assert row["draft_id"] == "", "no task draft was minted by the hop"
    assert row["when"] is None, "same as a `draft:<id>` row — the form says when"


# ------------------------------------------------ scheduling spends the key


def test_scheduling_by_draft_key_moves_the_number_and_drops_the_draft(client,
                                                                      tmp_path):
    """The two moves `draft_id` has always made, for the key the hop actually
    edited (§5). Without the rekey the TASK-001 somebody was typing into becomes
    TASK-002 the moment they press Schedule; without the delete the task they
    booked is listed twice."""
    target = tmp_path / "project"
    target.mkdir()
    key = "new:" + str(target / "notes.py")
    client.put(_chat_url(key), json={"text": "roll up the PRs"})
    before = _by_key(client)[key]
    assert before["task_id"], "an unsent chat is a row with a number"

    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "roll up the PRs",
                          "delay_seconds": 600, "title": "Roll up the PRs",
                          "draft_key": key})
    assert r.status_code == 200, r.text
    assert drafts.get_chat(key) is None

    rows = _by_key(client)
    assert key not in rows, "one task, not a task and the draft it came from"
    booked = [row for row in rows.values() if row["title"] == "Roll up the PRs"]
    assert len(booked) == 1
    assert booked[0]["task_id"] == before["task_id"], "the number came along"


def test_scheduling_by_draft_key_deletes_exactly_once(client, tmp_path,
                                                      projects_dir):
    """`draft_key` and `session_id` name the same record when the chat has
    already run, and the create must not spend it twice — a second delete would
    be aimed at whatever the reader has typed since."""
    _transcript(projects_dir, "sess-a")
    target = tmp_path / "project"
    target.mkdir()
    client.put(_chat_url("sess-a"), json={"text": "and then deploy"})
    gen = client.get("/api/tasks").json()["generation"]

    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "and then deploy",
                          "delay_seconds": 600, "session_id": "sess-a",
                          "draft_key": "sess-a"})
    assert r.status_code == 200, r.text
    assert drafts.get_chat("sess-a") is None
    # A session keyed draft never had a number of its own — the conversation
    # holds it — so nothing was moved onto the new entry.
    assert _by_key(client)["sess-a"]["draft"] is None
    changed = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
    assert "sess-a" in changed["drafts"]["gone"]


def test_a_draft_key_that_is_not_one_changes_nothing(client, tmp_path):
    """Optional and silently ignored, like every other clean-up on this route:
    the task IS scheduled, and a malformed key is not worth a 400."""
    target = tmp_path / "project"
    target.mkdir()
    key = "new:" + str(target)
    client.put(_chat_url(key), json={"text": "untouched"})
    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "hi",
                          "delay_seconds": 600, "draft_key": "/etc/passwd"})
    assert r.status_code == 200, r.text
    assert drafts.get_chat(key) is not None


def test_a_task_draft_is_still_spent_by_its_id(client, tmp_path):
    """`draft_id` is unchanged — a draft born in the New task modal has no
    originating chat, and that is the whole difference between the two."""
    target = tmp_path / "project"
    target.mkdir()
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Ship it", "target": str(target)})
    before = _by_key(client)["draft:draft-0001"]["task_id"]
    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "Ship it",
                          "delay_seconds": 600, "title": "Ship it",
                          "draft_id": "draft-0001"})
    assert r.status_code == 200, r.text
    assert drafts.get_task("draft-0001") is None
    rows = _by_key(client)
    assert "draft:draft-0001" not in rows
    booked = [row for row in rows.values() if row["title"] == "Ship it"]
    assert len(booked) == 1 and booked[0]["task_id"] == before


# --------------------------------------------------- the changes endpoint


def test_the_changes_payload_carries_the_drafts_that_moved(client, tmp_path):
    """§3. The rows this endpoint already sends cannot say that a draft was
    edited elsewhere — a row carries a preview, not the record — so the keys
    that were announced are answered with a version apiece."""
    key = "new:" + str(tmp_path)
    gen = client.get("/api/tasks").json()["generation"]
    client.put(_chat_url(key), json={"text": "one"})
    client.put("/api/drafts/task/draft-0001", json={"title": "a form"})

    r = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
    changed = {row["key"]: row["version"] for row in r["drafts"]["changed"]}
    assert changed == {key: 1, "draft:draft-0001": 1}
    assert r["drafts"]["gone"] == []

    gen = r["generation"]
    client.put(_chat_url(key), json={"text": "one two"})
    r = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
    assert r["drafts"]["changed"] == [{"key": key, "version": 2}]


def test_a_discarded_draft_is_reported_gone(client, tmp_path):
    """The other half: the composer that is holding this key has to empty
    itself, and a row's disappearance does not say which key it was."""
    key = "new:" + str(tmp_path)
    client.put(_chat_url(key), json={"text": "one"})
    gen = client.get("/api/tasks").json()["generation"]
    assert client.delete(_chat_url(key)).json()["removed"] is True

    r = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
    assert r["drafts"] == {"changed": [], "gone": [key]}


def test_the_changes_payload_costs_one_read(client, tmp_path, monkeypatch):
    """Cheap by construction (§3): one json read of the drafts store, and no
    row building — this rides on every long-poll answer."""
    key = "new:" + str(tmp_path)
    client.put(_chat_url(key), json={"text": "one"})
    gen = client.get("/api/tasks").json()["generation"]
    tasks_watch.notify({key})

    reads = []
    real = drafts.list_all
    monkeypatch.setattr(drafts, "list_all", lambda: (reads.append(1), real())[1])
    r = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
    assert r["drafts"]["changed"] == [{"key": key, "version": 1}]
    assert len(reads) == 2, "one for the rows the listing built, one for this"

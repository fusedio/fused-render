"""Drafts — the store (fused_render/drafts.py), the routes
(server/routers/drafts.py), and the two joins onto the Tasks listing.

What is under test is what design.md decided and nothing else: a draft lives on
the server in one global file, an empty draft is a delete, a task draft is a row
with no TASK number, a chat draft is a one-line preview on its session's row,
and every verb that ends a task — archive, delete, erase, and scheduling the
draft for real — takes the draft with it.

Nothing here reads the real ~/.claude or the real ~/.fused-render: every path is
under tmp_path, and `drafts.STATE_DIR` is redirected alongside tasks_store's
because both are computed at import from the same env var.
"""
import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from fused_render import drafts, schedule, schedule_wake, tasks_store
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod

WRITE = {"X-Fused": "1"}


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
    """The one global dir all three stores share. `drafts.STATE_DIR` is derived
    from the env at import exactly like `tasks_store.STATE_DIR`, so a test that
    redirects one and not the other would write drafts into the developer's own
    ~/.fused-render."""
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


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


T9 = "2026-09-11T09:00:00Z"
T10 = "2026-09-11T10:00:00Z"


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


def _user(text, ts):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def _by_key(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    return {t["key"]: t for t in r.json()["tasks"]}


# ---------------------------------------------------------------- the store


def test_a_chat_draft_round_trips(state_dir):
    stored = drafts.put_chat("sess-a", "half a thought", [])
    assert stored["text"] == "half a thought"
    assert stored["updated_at"] > 0
    assert drafts.get_chat("sess-a")["text"] == "half a thought"
    assert list(drafts.list_chat()) == ["sess-a"]
    # One file, in the global dir every other session store uses.
    assert (state_dir / "drafts.json").exists()


def test_an_empty_chat_draft_is_a_delete(state_dir):
    """The composer's autosave fires again after the send cleared the box, and
    that write must not resurrect what the send deleted."""
    drafts.put_chat("sess-a", "typing", [])
    assert drafts.put_chat("sess-a", "   ", []) is None
    assert drafts.get_chat("sess-a") is None
    assert drafts.list_chat() == {}


def test_attachments_alone_keep_a_chat_draft(state_dir):
    """A picture dropped into an empty composer is a draft. Text is not the
    only thing a person can leave unsent."""
    stored = drafts.put_chat("sess-a", "", [
        {"path": "/shots/a1b2.png", "name": "screenshot.png", "kind": "image"}])
    assert stored is not None
    assert stored["attachments"] == [
        {"path": "/shots/a1b2.png", "name": "screenshot.png", "kind": "image"}]


def test_an_unreadable_attachment_row_is_dropped_not_raised(state_dir):
    """Lenient where schedule._attachments is strict — see the module
    docstring: a draft that refused to save over a malformed row would be the
    store failing at its only job."""
    stored = drafts.put_chat("sess-a", "hi", [
        "not-an-object", {"name": "no path"},
        {"path": "/shots/x.pdf", "name": "../../etc/passwd", "kind": "nonsense"}])
    assert stored["attachments"] == [
        # The name is a BASENAME and the unknown kind fell back to the first.
        {"path": "/shots/x.pdf", "name": "passwd", "kind": "image"}]


def test_a_new_chat_key_is_accepted_and_a_bad_one_is_not(state_dir):
    """Two shapes, because a chat has two ages (design.md, "Chat draft key")."""
    assert drafts.chat_key("sess-a") == "sess-a"
    assert drafts.chat_key("new:/Users/me/x.py") == "new:/Users/me/x.py"
    assert drafts.is_new_chat_key("new:/Users/me/x.py") is True
    assert drafts.chat_key("/etc/passwd") == ""
    assert drafts.chat_key("") == ""
    assert drafts.chat_key(None) == ""
    assert drafts.chat_key("x" * 600) == ""


def test_a_draft_id_is_a_token(state_dir):
    assert drafts.draft_id("d-1234-abcd") == "d-1234-abcd"
    assert drafts.draft_id("short") == "", "under eight characters"
    assert drafts.draft_id("has/slash/init") == ""
    assert drafts.put_task("nope", {"title": "x"}) is None


def test_a_task_draft_merges_fields_and_keeps_created_at(state_dir):
    first = drafts.put_task("draft-0001", {"title": "Ship it", "junk": "dropped"})
    assert "junk" not in first
    assert first["title"] == "Ship it"
    assert first["created_at"] == first["updated_at"]

    # A second autosave sends only what changed; the rest stands.
    second = drafts.put_task("draft-0001", {"target": "/tmp/proj"})
    assert second["title"] == "Ship it"
    assert second["target"] == "/tmp/proj"
    assert second["created_at"] == first["created_at"], "the row must sit still"


def test_when_and_repeat_pass_through_whatever_the_form_held(state_dir):
    """A structured repeat is an object and a cron line is a string; the modal
    has to reopen on exactly what it closed on."""
    stored = drafts.put_task("draft-0001", {
        "when": "2026-09-12T09:00", "repeat": {"every": "week", "on": ["mon"]}})
    assert stored["when"] == "2026-09-12T09:00"
    assert stored["repeat"] == {"every": "week", "on": ["mon"]}


def test_an_all_empty_task_draft_is_a_delete(state_dir):
    drafts.put_task("draft-0001", {"title": "Ship it"})
    assert drafts.put_task("draft-0001", {"title": "  "}) is None
    assert drafts.get_task("draft-0001") is None
    assert drafts.delete_task("draft-0001") is False


def test_a_corrupt_store_reads_as_no_drafts(state_dir):
    (state_dir / "drafts.json").write_text("{ not json")
    assert drafts.list_chat() == {}
    assert drafts.list_task() == {}
    # ...and writing over it still works.
    assert drafts.put_chat("sess-a", "recovered", []) is not None


def test_the_preview_is_the_first_non_empty_line(state_dir):
    assert drafts.preview("\n\n  hello there \nsecond line") == "hello there"
    assert drafts.preview("   \n\t") == ""
    long_line = "x" * 500
    clipped = drafts.preview(long_line)
    assert len(clipped) == drafts.PREVIEW_MAX
    assert clipped.endswith("…")


# ---------------------------------------------------------------- the routes


def test_the_chat_routes_upsert_and_delete(client):
    r = client.put("/api/drafts/chat/sess-a", json={"text": "unsent"})
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["text"] == "unsent"

    assert client.get("/api/drafts").json()["chat"]["sess-a"]["text"] == "unsent"

    # Empty in is a delete, and the answer says so.
    assert client.put("/api/drafts/chat/sess-a", json={"text": ""}).json()["draft"] is None
    assert client.get("/api/drafts").json()["chat"] == {}

    # Deleting one that was never saved is not a 404 — the composer clears
    # after a send whether or not the debounce ever got round to a first save.
    r = client.delete("/api/drafts/chat/sess-a")
    assert r.status_code == 200
    assert r.json()["removed"] is False


def test_a_new_chat_key_round_trips_through_the_route(client):
    """The key carries a file path, so the route has to accept separators."""
    r = client.put("/api/drafts/chat/new:%2FUsers%2Fme%2Fx.py",
                   json={"text": "before the first send"})
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "new:/Users/me/x.py"
    assert client.get("/api/drafts").json()["chat"]["new:/Users/me/x.py"]["text"] \
        == "before the first send"


def test_a_bad_key_is_a_400(client):
    assert client.put("/api/drafts/chat/%2Fetc%2Fpasswd",
                      json={"text": "x"}).status_code == 400
    assert client.put("/api/drafts/task/short", json={"title": "x"}).status_code == 400
    assert client.delete("/api/drafts/task/short").status_code == 400


def test_the_task_routes_upsert_and_delete(client):
    r = client.put("/api/drafts/task/draft-0001",
                   json={"title": "Nightly report", "target": "/tmp/proj"})
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["title"] == "Nightly report"

    assert list(client.get("/api/drafts").json()["task"]) == ["draft-0001"]

    r = client.delete("/api/drafts/task/draft-0001")
    assert r.json() == {"ok": True, "draft_id": "draft-0001", "removed": True}
    assert client.get("/api/drafts").json()["task"] == {}


def test_every_mutation_bumps_the_watch_generation(client):
    """The chip has to appear without a reload, which is the long-poll's job.

    EVERY key, including `new:` — round 1 kept that one silent because no row
    carried it, and round 2 gave it a row of its own (Akshil, 2026-09-11)."""
    before = client.get("/api/tasks").json()["generation"]
    client.put("/api/drafts/chat/sess-a", json={"text": "unsent"})
    after = client.get("/api/tasks").json()["generation"]
    assert after > before

    client.put("/api/drafts/chat/new:%2Ftmp%2Fx.py", json={"text": "unsent"})
    assert client.get("/api/tasks").json()["generation"] > after


# ----------------------------------------------------------------- the joins


def test_a_session_row_carries_its_chat_draft(client, projects_dir):
    _write_transcript(projects_dir, "sess-a", "/home/me/proj",
                      [_user("pull today's news", T9)])
    assert _by_key(client)["sess-a"]["draft"] is None

    client.put("/api/drafts/chat/sess-a",
               json={"text": "\nand also check the\nsecond line"})
    row = _by_key(client)["sess-a"]
    assert row["draft"]["preview"] == "and also check the"
    assert row["draft"]["updated_at"] > 0

    client.delete("/api/drafts/chat/sess-a")
    assert _by_key(client)["sess-a"]["draft"] is None


def test_a_task_draft_is_a_row_with_a_number(client, tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Nightly report", "description": "roll it up",
                     "target": str(target), "model": "opus"})
    row = _by_key(client)["draft:draft-0001"]
    assert row["kind"] == "draft"
    assert row["draft_kind"] == "task"
    assert row["state"] == "draft"
    assert row["draft_id"] == "draft-0001"
    # ROUND 2: a draft is a thing you can name, so it is numbered — allocated
    # in the project it points at, through the same store `pending:` rows use.
    assert row["task_id"] == "TASK-001"
    assert tasks_store.task_number("draft:draft-0001") == "TASK-001"
    assert row["title"] == "Nightly report"
    # The same rule every other row's project follows (`_workdir`): a folder
    # target IS the project, a file target is the folder it sits in.
    assert row["target"] == str(target)
    assert row["project"] == str(target)
    assert row["when"] is None
    assert row["session_id"] == ""
    assert row["message_count"] == 0
    assert row["happened_at"] == 0.0
    assert row["at"] == row["created_at"] > 0
    assert row["updated_at"] > 0
    # The whole form, so the modal can reopen on it without a second request.
    assert row["form"]["model"] == "opus"
    assert row["form"]["description"] == "roll it up"


def test_a_draft_row_falls_back_to_its_description_then_to_a_name(client):
    client.put("/api/drafts/task/draft-0001",
               json={"description": "  \nroll up yesterday's PRs\nand file them"})
    assert _by_key(client)["draft:draft-0001"]["title"] == "roll up yesterday's PRs"

    client.put("/api/drafts/task/draft-0002", json={"model": "opus"})
    assert _by_key(client)["draft:draft-0002"]["title"] == "Untitled draft"


def test_draft_rows_reach_the_changes_endpoint_but_not_the_pulse(client):
    """A new draft has to appear without a reload (changes), and must not be
    counted as news in the sidebar (pulse) — design.md puts drafts in the List
    and the Board only."""
    before = client.get("/api/tasks").json()["generation"]
    client.put("/api/drafts/task/draft-0001", json={"title": "Nightly report"})

    r = client.get(f"/api/tasks/changes?since={before}&wait=0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [row["key"] for row in body["rows"]] == ["draft:draft-0001"]
    assert body["gone"] == []

    assert [t["key"] for t in client.get("/api/tasks/pulse").json()["tasks"]] == []

    # ...and discarding it names the key as gone rather than leaving the row.
    gen = body["generation"]
    client.delete("/api/drafts/task/draft-0001")
    body = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
    assert body["rows"] == []
    assert body["gone"] == ["draft:draft-0001"]


# ------------------------------------------------------------- the lifecycle


def test_archiving_a_task_drops_its_chat_draft(client, projects_dir):
    _write_transcript(projects_dir, "sess-a", "/home/me/proj",
                      [_user("pull today's news", T9)])
    client.put("/api/drafts/chat/sess-a", json={"text": "unsent"})

    r = client.post("/api/tasks/archive", json={"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert drafts.get_chat("sess-a") is None
    assert _by_key(client)["sess-a"]["draft"] is None


def test_deleting_and_erasing_a_task_drop_its_chat_draft(client, projects_dir):
    _write_transcript(projects_dir, "sess-a", "/home/me/proj",
                      [_user("one", T9)])
    _write_transcript(projects_dir, "sess-b", "/home/me/proj",
                      [_user("two", T10)])
    client.put("/api/drafts/chat/sess-a", json={"text": "unsent a"})
    client.put("/api/drafts/chat/sess-b", json={"text": "unsent b"})

    assert client.post("/api/tasks/delete", json={"key": "sess-a"}).status_code == 200
    assert drafts.get_chat("sess-a") is None
    assert drafts.get_chat("sess-b") is not None

    assert client.post("/api/tasks/erase", json={"key": "sess-b"}).status_code == 200
    assert drafts.get_chat("sess-b") is None


def test_scheduling_a_draft_deletes_it_in_the_same_request(client, tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Nightly report", "target": str(target)})
    assert "draft:draft-0001" in _by_key(client)

    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "roll it up",
                          "delay_seconds": 600, "title": "Nightly report",
                          "draft_id": "draft-0001"})
    assert r.status_code == 200, r.text
    assert drafts.get_task("draft-0001") is None
    rows = _by_key(client)
    assert "draft:draft-0001" not in rows, "one task, not two"
    assert any(row["title"] == "Nightly report" for row in rows.values())


def test_a_schedule_without_a_draft_id_is_unchanged(client, tmp_path):
    """Every client written before drafts existed sends no `draft_id`, and the
    `?new=1` hop still does not."""
    target = tmp_path / "project"
    target.mkdir()
    drafts.put_task("draft-0001", {"title": "keep me"})
    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "hi",
                          "delay_seconds": 600})
    assert r.status_code == 200, r.text
    assert r.json()["entry"]["state"] == schedule.PENDING
    assert drafts.get_task("draft-0001") is not None


# -------------------------------------------------------- round 2: numbers
#
# Every draft has a TASK number, and the number moves forward with the draft
# rather than being minted again on the other side (design.md, "Round 2";
# Akshil, 2026-09-11). What these hold is the pair of promises `allocate once,
# never renumber` already makes for `pending:<entry-id>`, one stage earlier.


def _chat_url(key: str) -> str:
    return "/api/drafts/chat/" + quote(key, safe="")


def test_a_draft_keeps_its_number_when_it_is_scheduled(client, tmp_path):
    """The renumber this prevents is the one a person would actually notice:
    pressing Schedule on the TASK-001 they had been typing into and watching a
    TASK-002 appear."""
    target = tmp_path / "project"
    target.mkdir()
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Nightly report", "target": str(target)})
    number = _by_key(client)["draft:draft-0001"]["task_id"]
    assert number == "TASK-001"

    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "roll it up",
                          "delay_seconds": 600, "title": "Nightly report",
                          "draft_id": "draft-0001"})
    assert r.status_code == 200, r.text
    entry_id = r.json()["entry"]["id"]

    rows = _by_key(client)
    assert "draft:draft-0001" not in rows, "one task, not two"
    assert rows["pending:" + entry_id]["task_id"] == number


def test_a_discarded_draft_does_not_give_its_number_back(client, tmp_path):
    """Gaps are the price. Releasing a number is the one thing allocate-once
    forbids — the project's high-water mark would drop and the next draft would
    be handed a number the user has already seen on something else."""
    target = tmp_path / "project"
    target.mkdir()
    client.put("/api/drafts/task/draft-0001",
               json={"title": "one", "target": str(target)})
    assert _by_key(client)["draft:draft-0001"]["task_id"] == "TASK-001"

    assert client.delete("/api/drafts/task/draft-0001").status_code == 200
    client.put("/api/drafts/task/draft-0002",
               json={"title": "two", "target": str(target)})
    assert _by_key(client)["draft:draft-0002"]["task_id"] == "TASK-002"
    # ...and the discarded one's record is still in the store, unread by
    # anything, holding the mark.
    assert tasks_store.task_number("draft:draft-0001") == "TASK-001"


# ----------------------------------------------- round 2: new-chat drafts


def test_an_unsent_new_chat_is_a_row(client, tmp_path):
    """A folder somebody has typed into but never sent is the same unfinished
    thing as a half-filled form, and round 1 listed only one of the two."""
    folder = tmp_path / "proj"
    folder.mkdir()
    view = folder / "page.html"
    view.write_text("<p>hi</p>")
    key = "new:" + str(view)

    r = client.put(_chat_url(key), json={"text": "look at the chart\nand fix it"})
    assert r.status_code == 200, r.text

    row = _by_key(client)[key]
    assert row["kind"] == "draft"
    assert row["draft_kind"] == "chat"
    assert row["state"] == "draft"
    assert row["status"] == "upcoming"
    assert row["task_id"] == "TASK-001"
    assert row["draft_id"] == ""
    assert row["session_id"] == ""
    assert row["title"] == "look at the chart", "the first line, nothing else"
    # The FOLDER is the project — the file is what the chat opens on.
    assert row["project"] == str(folder)
    assert row["cwd"] == str(folder)
    assert row["target"] == str(view)
    assert row["file"] == str(view)
    assert row["draft"]["preview"] == "look at the chart"
    assert row["draft"]["updated_at"] > 0
    assert row["form"] is None, "no form to reopen — it is a composer"
    assert row["message_count"] == 0
    assert row["happened_at"] == 0.0
    assert row["when"] is None


def test_a_new_chat_draft_on_a_folder_is_numbered_in_that_folder(client, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "start here"})
    row = _by_key(client)[key]
    assert row["project"] == str(folder) == row["target"]
    assert row["task_id"] == "TASK-001"


def test_a_new_chat_draft_with_no_text_still_has_a_name(client, tmp_path):
    """A picture dropped into an empty composer is a draft, and a draft row has
    to be clickable, so it can never render as a blank line."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key),
               json={"text": "", "attachments": [
                   {"path": "/shots/a1.png", "name": "a1.png", "kind": "image"}]})
    assert _by_key(client)[key]["title"] == "Untitled chat"


def test_a_new_chat_draft_title_is_clipped(client, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "y" * 300})
    title = _by_key(client)[key]["title"]
    assert len(title) == 80 and title.endswith("…")


def test_new_chat_draft_rows_appear_and_vanish_through_the_changes_endpoint(
        client, tmp_path):
    """Round 1 announced nothing for a `new:` key — there was no row. There is
    now, so it has to arrive and leave without a reload."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)

    before = client.get("/api/tasks").json()["generation"]
    client.put(_chat_url(key), json={"text": "unsent"})

    body = client.get(f"/api/tasks/changes?since={before}&wait=0").json()
    assert [row["key"] for row in body["rows"]] == [key]
    assert body["rows"][0]["draft_kind"] == "chat"

    gen = body["generation"]
    client.delete(_chat_url(key))
    body = client.get(f"/api/tasks/changes?since={gen}&wait=0").json()
    assert body["rows"] == []
    assert body["gone"] == [key]

    # ...and never in the pulse. A form nobody has finished is not news.
    client.put(_chat_url(key), json={"text": "unsent again"})
    assert [t["key"] for t in client.get("/api/tasks/pulse").json()["tasks"]] == []


# ------------------------------------------------- round 2: rekey forward


def test_the_rekey_route_moves_the_number_onto_the_session(client, projects_dir,
                                                           tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "first message"})
    number = _by_key(client)[key]["task_id"]
    assert number == "TASK-001"

    # The send happened: Claude Code minted a session and wrote a transcript.
    _write_transcript(projects_dir, "sess-a", str(folder),
                      [_user("first message", T9)])
    r = client.post("/api/drafts/chat/rekey", json={"from": key, "to": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json()["task_id"] == number

    rows = _by_key(client)
    assert key not in rows, "the folder row is the session's row now"
    assert rows["sess-a"]["task_id"] == number
    assert tasks_store.task_number("sess-a") == number


def test_the_rekey_route_deletes_a_draft_the_send_already_emptied(client, tmp_path):
    """The normal case: the send cleared the box, so there is nothing to move
    and the `new:` key is simply gone."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "first message"})
    client.put(_chat_url(key), json={"text": ""})  # the send

    r = client.post("/api/drafts/chat/rekey", json={"from": key, "to": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] is False
    assert drafts.get_chat(key) is None
    assert drafts.get_chat("sess-a") is None


def test_the_rekey_route_carries_text_typed_after_the_send(client, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "and one more thing"})

    r = client.post("/api/drafts/chat/rekey", json={"from": key, "to": "sess-a"})
    assert r.json()["moved"] is True
    assert drafts.get_chat(key) is None
    assert drafts.get_chat("sess-a")["text"] == "and one more thing"


def test_the_rekey_route_is_a_no_op_for_a_draft_that_never_existed(client, tmp_path):
    """The client fires this on every first send and cannot know whether the
    debounce ever got round to a first save."""
    key = "new:" + str(tmp_path / "proj")
    r = client.post("/api/drafts/chat/rekey", json={"from": key, "to": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "from": key, "to": "sess-a",
                        "task_id": "", "moved": False}


def test_the_rekey_route_refuses_a_to_that_is_not_a_session(client, tmp_path):
    key = "new:" + str(tmp_path / "proj")
    assert client.post("/api/drafts/chat/rekey",
                       json={"from": key, "to": "new:/tmp/x"}).status_code == 400
    assert client.post("/api/drafts/chat/rekey",
                       json={"from": key, "to": "/etc/passwd"}).status_code == 400
    assert client.post("/api/drafts/chat/rekey",
                       json={"from": "/etc/passwd", "to": "sess-a"}).status_code == 400
    assert client.post("/api/drafts/chat/rekey",
                       json={"from": "sess-a", "to": "sess-a"}).status_code == 400


# --------------------------------------- round 2: a draft moves, never twice


def test_a_task_draft_put_takes_the_chat_draft_it_came_from(client, tmp_path):
    """The composer → New task hop. For one instant the same unfinished
    sentence would be two rows; `from_chat_key` is the client naming the one it
    came from, in the request that made the other."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "roll up yesterday's PRs"})
    assert key in _by_key(client)

    r = client.put("/api/drafts/task/draft-0001",
                   json={"title": "roll up yesterday's PRs",
                         "target": str(folder), "from_chat_key": key})
    assert r.status_code == 200, r.text
    assert r.json()["from_chat_key"] == key
    assert drafts.get_chat(key) is None

    rows = _by_key(client)
    assert key not in rows
    assert "draft:draft-0001" in rows
    # …and the key is KEPT, because the move has to be reversible: the row's
    # form is what a reopened modal seeds from, and "Back to chat" reads the
    # key off it.
    assert rows["draft:draft-0001"]["form"]["from_chat_key"] == key


def test_the_chat_key_is_stored_and_survives_later_saves(client, tmp_path):
    """The way back, once the hop's URL is gone.

    A draft reopened from its row carries no `?back=` and no sessionStorage
    stash — the stored key is the only thing left that knows which conversation
    these words were typed in, so a keystroke save that names nothing must not
    erase it (Akshil, 2026-09-11)."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "roll up yesterday's PRs"})
    client.put("/api/drafts/task/draft-0001",
               json={"title": "roll up", "target": str(folder),
                     "from_chat_key": key})
    stored = drafts.get_task("draft-0001")
    assert stored["from_chat_key"] == key

    # The ordinary autosave that follows sends the form and nothing else.
    r = client.put("/api/drafts/task/draft-0001",
                   json={"title": "roll up yesterday's PRs",
                         "target": str(folder)})
    assert r.json()["draft"]["from_chat_key"] == key
    assert drafts.get_task("draft-0001")["from_chat_key"] == key
    assert _by_key(client)["draft:draft-0001"]["form"]["from_chat_key"] == key


def test_a_chat_key_that_is_not_one_is_stored_as_nothing(client, tmp_path):
    """Validated as a key, not kept as text: the field is either something the
    chat half can be written under or it is empty."""
    client.put("/api/drafts/task/draft-0001",
               json={"title": "a thought", "from_chat_key": "/etc/passwd"})
    assert drafts.get_task("draft-0001")["from_chat_key"] == ""


def test_the_chat_key_alone_is_not_a_draft(client, tmp_path):
    """`from_chat_key` is provenance, not content — an all-empty form that
    names one is still a delete."""
    key = "new:" + str(tmp_path)
    client.put(_chat_url(key), json={"text": "still here"})
    r = client.put("/api/drafts/task/draft-0001",
                   json={"title": "", "description": "", "from_chat_key": key})
    assert r.json()["draft"] is None
    assert drafts.get_task("draft-0001") is None
    # …and the chat draft it named is untouched, for the same reason.
    assert drafts.get_chat(key) is not None


def test_a_session_chat_draft_is_taken_too(client, projects_dir):
    _write_transcript(projects_dir, "sess-a", "/home/me/proj",
                      [_user("one", T9)])
    client.put("/api/drafts/chat/sess-a", json={"text": "half a thought"})
    client.put("/api/drafts/task/draft-0001",
               json={"title": "half a thought", "from_chat_key": "sess-a"})
    assert drafts.get_chat("sess-a") is None
    assert _by_key(client)["sess-a"]["draft"] is None


def test_an_empty_task_put_keeps_the_chat_draft(client, tmp_path):
    """An all-empty form is a delete, and dropping the chat draft over a write
    that stored nothing would lose the text outright."""
    key = "new:" + str(tmp_path)
    client.put(_chat_url(key), json={"text": "still here"})
    r = client.put("/api/drafts/task/draft-0001",
                   json={"title": "  ", "from_chat_key": key})
    assert r.json()["draft"] is None
    assert drafts.get_chat(key) is not None


def test_scheduling_into_a_session_drops_that_session_chat_draft(client, tmp_path,
                                                                 projects_dir):
    """The words were just booked as a message; text left behind would paint a
    `✎ Draft` chip on the very task that consumed it."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj",
                      [_user("one", T9)])
    target = tmp_path / "project"
    target.mkdir()
    client.put("/api/drafts/chat/sess-a", json={"text": "and then deploy"})

    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "and then deploy",
                          "delay_seconds": 600, "session_id": "sess-a"})
    assert r.status_code == 200, r.text
    assert drafts.get_chat("sess-a") is None
    assert _by_key(client)["sess-a"]["draft"] is None

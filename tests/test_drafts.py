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
import importlib.util
import io
import json
import os
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from fused_render import drafts, schedule, schedule_wake, tasks_store
from fused_render._view_url_codec import canonical_fs_path
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


@pytest.fixture(autouse=True)
def runs(tmp_path, monkeypatch):
    """A runs tree of our own, and the stand-in that reads it.

    The listing looks in here to settle a `new:<file>` key onto the session its
    first send created (`tasks_mod._settle_new_chats`), and the real tree is a
    per-user directory under the machine's temp root — the developer's own live
    chats. Autouse, so no test in this file can read it, and so a draft test
    that stages no run gets the same empty answer every time.

    A stand-in rather than the real `agent.py` for the reason `test_tasks_api`'s
    own fake gives: it is a TEMPLATE, outside the package's import graph by
    design (SPEC PY-15), and what is under test is what the router does with its
    answers."""
    d = tmp_path / "runs"
    d.mkdir()

    class _Agent:
        RUNS = str(d)

        def _permissions(self, run_dir):
            return []

        def _alive(self, run_dir):
            return False

        def _session_from_out(self, run_dir):
            return ""   # the `session` file is the only id these tests write

    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: _Agent())
    return d


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


def _put(ident, fields) -> dict | None:
    """`drafts.put_task`, minus the evicted-ids half most tests here have no
    reason to look at — the tests that ARE about eviction (below, "one bound
    draft per session") call `drafts.put_task` directly to see both."""
    record, _evicted = drafts.put_task(ident, fields)
    return record


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
    assert _put("nope", {"title": "x"}) is None


def test_a_task_draft_merges_fields_and_keeps_created_at(state_dir):
    first = _put("draft-0001", {"title": "Ship it", "junk": "dropped"})
    assert "junk" not in first
    assert first["title"] == "Ship it"
    assert first["created_at"] == first["updated_at"]

    # A second autosave sends only what changed; the rest stands.
    second = _put("draft-0001", {"target": "/tmp/proj"})
    assert second["title"] == "Ship it"
    assert second["target"] == "/tmp/proj"
    assert second["created_at"] == first["created_at"], "the row must sit still"


def test_when_and_repeat_pass_through_whatever_the_form_held(state_dir):
    """A structured repeat is an object and a cron line is a string; the modal
    has to reopen on exactly what it closed on."""
    stored = _put("draft-0001", {
        "title": "Standup",
        "when": "2026-09-12T09:00", "repeat": {"every": "week", "on": ["mon"]}})
    assert stored["when"] == "2026-09-12T09:00"
    assert stored["repeat"] == {"every": "week", "on": ["mon"]}


def test_only_words_or_files_make_a_task_draft(state_dir):
    """WHAT A DRAFT IS: `title.strip() or description.strip() or attachments`,
    and nothing else (Akshil, 2026-09-12).

    Everything else the form holds — the folder, the model, the effort, the
    permission mode, the time, the repeat rule — is a setting that rides along
    with a draft. They used to count, so changing the folder or picking a time
    on an untouched card minted an "Untitled draft" row holding nothing anybody
    had typed."""
    for settings in ({"target": "/tmp/proj"}, {"model": "opus"},
                     {"effort": "high"}, {"permission": "auto"},
                     {"when": "2026-09-12T09:00"}, {"repeat": "week"},
                     {"custom_rule": {"freq": "day"}},
                     {"new_task_each_run": True}):
        assert _put("draft-0001", settings) is None, settings
        assert drafts.get_task("draft-0001") is None, settings

    # The three that DO make one, each on its own.
    assert _put("draft-0001", {"title": "Ship it"}) is not None
    assert drafts.delete_task("draft-0001") is True
    assert _put("draft-0001", {"description": "roll up the PRs"}) is not None
    assert drafts.delete_task("draft-0001") is True
    assert _put("draft-0001", {"attachments": [
        {"path": "/shots/a1b2.png", "name": "shot.png", "kind": "image"}]}) is not None


def test_clearing_the_words_then_the_last_file_lets_the_draft_go(state_dir):
    """THE REPORTED DEAD END (Akshil, 2026-09-12): "if I clear the text it says
    untitled draft, after that if I lose the attachment it doesn't clear the
    draft". `target` was counted as content, so the record outlived everything
    a person had actually put in it."""
    shot = [{"path": "/shots/a1b2.png", "name": "shot.png", "kind": "image"}]
    _put("draft-0001", {"title": "Ship it", "target": "/tmp/proj",
                        "model": "opus", "attachments": shot})
    # Text cleared: still a draft, because the picture is still in the tray.
    assert _put("draft-0001", {"title": "", "description": ""}) is not None
    # …and the picture removed after it: now there is nothing left to keep.
    assert _put("draft-0001", {"attachments": []}) is None
    assert drafts.get_task("draft-0001") is None


def test_an_all_empty_task_draft_is_a_delete(state_dir):
    _put("draft-0001", {"title": "Ship it"})
    assert _put("draft-0001", {"title": "  "}) is None
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
    # target IS the project, a file target is the folder it sits in. Compared
    # through `canonical_fs_path`, the same normalisation the row itself
    # applies — `str(target)` is backslashed on Windows, and the row is not.
    assert row["target"] == canonical_fs_path(str(target))
    assert row["project"] == canonical_fs_path(str(target))
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

    # …and "Untitled draft" is now reachable ONLY by an attachment-only draft:
    # a settings-only form is not a draft at all (Akshil, 2026-09-12).
    client.put("/api/drafts/task/draft-0002", json={"attachments": [
        {"path": "/shots/a1b2.png", "name": "shot.png", "kind": "image"}]})
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
    _put("draft-0001", {"title": "keep me"})
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


def test_a_draft_changing_folder_is_renumbered_in_the_new_project(client, tmp_path):
    """A draft's number belongs to the project it points at NOW.

    The bug this ends: the number is allocated at the first keystroke, in
    whatever folder the modal happened to open on, and then the person picks a
    different one — the number rode along, and a project counting TASK-001…015
    showed a TASK-202 borrowed from the project the draft was started in
    (Akshil, 2026-09-11). Nothing has been promised while it is still a draft,
    which is what makes this the one row allocate-once may renumber."""
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beta = tmp_path / "beta"
    beta.mkdir()
    client.put("/api/drafts/task/draft-beta1",
               json={"title": "already here", "target": str(beta)})
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Nightly report", "target": str(alpha)})
    assert _by_key(client)["draft:draft-0001"]["task_id"] == "TASK-001"

    # The folder changes — the same autosave PUT every other keystroke makes.
    client.put("/api/drafts/task/draft-0001", json={"target": str(beta)})
    row = _by_key(client)["draft:draft-0001"]
    assert row["project"] == canonical_fs_path(str(beta))
    assert row["title"] == "Nightly report", "an omitted field is not a delete"
    assert row["task_id"] == "TASK-002", "beta's next free number, not alpha's"
    assert tasks_store.task_number("draft:draft-0001") == "TASK-002"

    # ...and alpha's TASK-001 is a GAP, not a number handed out twice: the same
    # price a discarded draft pays two tests up.
    client.put("/api/drafts/task/draft-0002",
               json={"title": "later", "target": str(alpha)})
    assert _by_key(client)["draft:draft-0002"]["task_id"] == "TASK-002"


def test_a_moved_draft_keeps_the_new_number_when_it_is_scheduled(client, tmp_path):
    """Scheduling is where the renumbering STOPS. The number the draft ends up
    with is the one the pending row carries, so the last thing the user saw in
    the modal is what they see in the list."""
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beta = tmp_path / "beta"
    beta.mkdir()
    client.put("/api/drafts/task/draft-beta1",
               json={"title": "already here", "target": str(beta)})
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Nightly report", "target": str(alpha)})
    # Listed BEFORE the move, which is what allocates alpha's number — without
    # it the draft would simply be numbered late, in beta, and prove nothing.
    assert _by_key(client)["draft:draft-0001"]["task_id"] == "TASK-001"

    client.put("/api/drafts/task/draft-0001", json={"target": str(beta)})
    number = _by_key(client)["draft:draft-0001"]["task_id"]
    assert number == "TASK-002"

    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(beta), "message": "roll it up",
                          "delay_seconds": 600, "title": "Nightly report",
                          "draft_id": "draft-0001"})
    assert r.status_code == 200, r.text
    rows = _by_key(client)
    assert "draft:draft-0001" not in rows, "one task, not two"
    assert rows["pending:" + r.json()["entry"]["id"]]["task_id"] == number


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
    # The FOLDER is the project — the file is what the chat opens on. Compared
    # through `canonical_fs_path`, same as above.
    assert row["project"] == canonical_fs_path(str(folder))
    assert row["cwd"] == canonical_fs_path(str(folder))
    assert row["target"] == canonical_fs_path(str(view))
    assert row["file"] == canonical_fs_path(str(view))
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
    assert row["project"] == canonical_fs_path(str(folder)) == row["target"]
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


# ----------------------------------- round 2: the first send's session
#
# A chat with no session drafts under `new:<file>` and is numbered under that
# key. The first send creates the session, and the number has to follow it —
# but WHICH session a send created is not a question the page can answer, and
# four rounds of bugbot went into trying (PR #1118, 2026-09-12). What the send
# CAN say is which draft it is spending, so it says that: a session-less start
# carries `draft_key`, `agent._start` writes it into the run's `meta.json`, and
# the listing build moves the number onto the session that run made
# (`routers/tasks.py::_settle_new_chats`).
#
# The first build of this matched on the target and an empty `resumed_from`
# instead — "a first-ever run on this folder" — and every other way a folder
# gets its first run passes those guards too: a scheduled task's first fire,
# a `new_task_each_run` entry, canvases.py's spawn. Each would have taken the
# number AND deleted the unsent words (review, 2026-09-12). That is the second
# test below, and it is the one this round exists for.


def _stage_run(runs, name, file, session_id="", resumed_from="", started=None,
               draft_key=None):
    """One run dir as `agent._start` leaves it: `meta.json` with the target, the
    session it resumed (empty when the send had none) and — for a native
    composer's session-less send only — the draft key that send is spending,
    plus the `session` file `_poll` writes once the CLI reports an id.

    `draft_key=None` is every OTHER caller of `_start`: the scheduler, the apps
    API, canvases.py. They write no such field, which is exactly what stops them
    claiming a draft.

    `started` back-dates `meta.json` — the run's own clock, which is what tells
    a draft the send spent from one typed into after it."""
    d = runs / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"file": str(file), "resumed_from": resumed_from,
            "message": "first message"}
    if draft_key is not None:
        meta["draft_key"] = draft_key
    path = d / "meta.json"
    path.write_text(json.dumps(meta))
    if started is not None:
        os.utime(path, (started, started))
    if session_id:
        (d / "session").write_text(session_id)
    return d


def test_a_session_less_send_carries_the_number_onto_the_session_it_made(
        client, projects_dir, runs, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "first message"})
    number = _by_key(client)[key]["task_id"]
    assert number == "TASK-001"

    # The send: a run tagged with this draft's key, and Claude Code minted a
    # session for it.
    _write_transcript(projects_dir, "sess-a", str(folder),
                      [_user("first message", T9)])
    _stage_run(runs, "20260912-090000-aa", folder, "sess-a", draft_key=key,
               started=time.time() + 5)

    rows = _by_key(client)
    assert key not in rows, "the folder row is the session's row now"
    assert rows["sess-a"]["task_id"] == number
    assert tasks_store.task_number("sess-a") == number
    # And the draft the send spent is gone — never copied onto the session:
    # those words are in the transcript.
    assert client.get("/api/drafts").json()["chat"] == {}


def test_a_first_run_nobody_tagged_leaves_the_draft_whole(
        client, projects_dir, runs, tmp_path):
    """THE ONE THIS ROUND EXISTS FOR (review, 2026-09-12).

    A scheduled task's first fire on the same folder is a run with no
    `resumed_from` and a brand-new session — identical, on disk, to a composer's
    first send in every way except the tag. It must leave the unsent words, the
    draft and the number exactly where they are; the earlier target-matching
    version deleted all three.
    """
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "still typing this"})
    number = _by_key(client)[key]["task_id"]

    _write_transcript(projects_dir, "sess-a", str(folder),
                      [_user("the scheduled prompt", T9)])
    _stage_run(runs, "20260912-090000-aa", folder, "sess-a",
               started=time.time() + 5)

    rows = _by_key(client)
    assert rows[key]["task_id"] == number
    assert rows["sess-a"]["task_id"] != number, "the scheduler gets its own"
    assert client.get("/api/drafts").json()["chat"][key]["text"] == \
        "still typing this"


def test_a_run_tagged_for_another_chat_is_not_this_key_s_send(
        client, projects_dir, runs, tmp_path):
    """The tag is matched verbatim and nothing else is consulted. A folder and a
    file inside it are two chats to draft in, and a number that crossed between
    them would land on a conversation the reader never typed into."""
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "app.py").write_text("x = 1\n")
    key = "new:" + str(folder / "app.py")
    client.put(_chat_url(key), json={"text": "first message"})
    number = _by_key(client)[key]["task_id"]

    _write_transcript(projects_dir, "sess-a", str(folder),
                      [_user("first message", T9)])
    _stage_run(runs, "20260912-090000-aa", folder, "sess-a",
               draft_key="new:" + str(folder), started=time.time() + 5)

    assert _by_key(client)[key]["task_id"] == number
    assert client.get("/api/drafts").json()["chat"][key]["text"] == "first message"


def test_a_send_into_a_session_leaves_another_chats_draft_alone(
        client, projects_dir, runs, tmp_path):
    """The bug this whole move was rewritten for. Opening a recent session in a
    folder that holds an unsent draft used to walk that row — words, number and
    all — onto a conversation the send had nothing to do with. A send with a
    session to send into creates nothing, so it carries no tag."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "still typing this"})
    number = _by_key(client)[key]["task_id"]

    _write_transcript(projects_dir, "sess-a", str(folder),
                      [_user("something else", T9)])
    _stage_run(runs, "20260912-090000-aa", folder, "sess-a",
               resumed_from="sess-a", started=time.time() + 5)

    rows = _by_key(client)
    assert rows[key]["task_id"] == number
    assert rows["sess-a"]["task_id"] != number
    assert client.get("/api/drafts").json()["chat"][key]["text"] == "still typing this"


def test_a_run_that_never_minted_a_session_leaves_the_draft_alone(
        client, runs, tmp_path):
    """`_start` failed, or the CLI died before its first row. There is nothing
    to carry the number to, and the draft is still the only copy of what the
    user typed."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "first message"})
    number = _by_key(client)[key]["task_id"]

    _stage_run(runs, "20260912-090000-aa", folder, draft_key=key,
               started=time.time() + 5)

    assert _by_key(client)[key]["task_id"] == number
    assert client.get("/api/drafts").json()["chat"][key]["text"] == "first message"


def test_words_typed_after_the_run_started_are_not_that_run_s_draft(
        client, projects_dir, runs, tmp_path):
    """A draft saved AFTER the run began is not the message it sent — it is the
    next one, typed into the same box while the session id was still on its way
    — so the key keeps both its words and its number."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    _write_transcript(projects_dir, "sess-a", str(folder),
                      [_user("first message", T9)])
    _stage_run(runs, "20260912-090000-aa", folder, "sess-a", draft_key=key,
               started=time.time() - 60)
    client.put(_chat_url(key), json={"text": "and one more thing"})
    number = _by_key(client)[key]["task_id"]

    rows = _by_key(client)
    assert rows[key]["task_id"] == number
    assert client.get("/api/drafts").json()["chat"][key]["text"] == "and one more thing"


def test_the_number_moves_even_though_the_composer_deleted_the_draft(
        client, projects_dir, runs, tmp_path):
    """The ORDINARY case, and the one with no draft record left to read: the
    composer deletes its own `new:<file>` draft in the same tick as the send, so
    by the next build the number is all that is still filed under the key. It
    still has to reach the session — which is why the settle asks the numbers
    store and not only the drafts store."""
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    client.put(_chat_url(key), json={"text": "first message"})
    number = _by_key(client)[key]["task_id"]
    client.delete(_chat_url(key))
    assert client.get("/api/drafts").json()["chat"] == {}

    _write_transcript(projects_dir, "sess-a", str(folder),
                      [_user("first message", T9)])
    _stage_run(runs, "20260912-090000-aa", folder, "sess-a", draft_key=key,
               started=time.time() + 5)

    assert _by_key(client)["sess-a"]["task_id"] == number
    assert tasks_store.task_number(key) == ""


# ------------------------------- round 2: the template's half of the tag
#
# The settle above reads a field somebody else writes. `agent.py` is a TEMPLATE
# — outside the package's import graph by design (SPEC PY-15), so it cannot be
# imported, only loaded by path — and it is the process that actually spends the
# draft. These two tests are the contract between the halves: the key goes into
# `meta.json` verbatim when a send hands one over, and the field is ABSENT for
# every other caller of `_start` (the scheduler, the apps API, canvases.py),
# which is what keeps them from claiming a draft.


def _load_agent_template():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_drafts", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeHost:
    """The session host `_start` spawns. What is under test is the file it
    writes BEFORE the spawn, and a real host would go looking for a CLI."""

    pid = 4242

    def __init__(self):
        self.stdin = io.BytesIO()


def _start_a_run(tmp_path, monkeypatch, target, **kw):
    agent = _load_agent_template()
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda *a, **kwargs: _FakeHost())
    out = agent._start(str(target), "first message", "", "", "", **kw)
    assert "error" not in out, out
    with open(os.path.join(agent.RUNS, out["run_id"], "meta.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def test_start_writes_the_draft_key_it_was_handed(tmp_path, monkeypatch):
    folder = tmp_path / "proj"
    folder.mkdir()
    key = "new:" + str(folder)
    meta = _start_a_run(tmp_path, monkeypatch, folder, draft_key=key)
    # VERBATIM: the key is stored unnormalised on both sides on purpose, so a
    # tilde or a trailing slash that the template "helpfully" resolved would be
    # a key the server can never match.
    assert meta["draft_key"] == key
    assert meta["resumed_from"] == ""


def test_start_writes_no_draft_key_when_nobody_sent_one(tmp_path, monkeypatch):
    folder = tmp_path / "proj"
    folder.mkdir()
    meta = _start_a_run(tmp_path, monkeypatch, folder)
    assert "draft_key" not in meta


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


# ----------------------------------------------- round 3: what Schedule takes
#
# Two things the first cut of the feature let slip past the create endpoint
# (Bugbot, PR #1118). Both are about a draft OUTLIVING the thing it turned into,
# which is the one outcome "a draft moves, never duplicates" forbids.


def test_scheduling_a_hop_drops_the_chat_draft_it_came_from(client, tmp_path):
    """The composer hop's chat draft, retired by the create rather than by the
    task draft's first autosave.

    That autosave is what normally moves it (`from_chat_key` on the task-draft
    PUT), but a card opened from the Schedule button opens ready to send: press
    it inside the 600 ms debounce and no task draft is minted at all, so nothing
    ever names the chat key. `session_id` cannot stand in — a chat that has never
    sent anything has no session, and its draft is keyed `new:<file>`."""
    target = tmp_path / "project"
    target.mkdir()
    key = "new:" + str(target / "notes.py")
    client.put(_chat_url(key), json={"text": "roll up the PRs"})
    assert key in _by_key(client), "the unsent chat is a row of its own"

    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "roll up the PRs",
                          "delay_seconds": 600, "title": "Roll up the PRs",
                          "from_chat_key": key})
    assert r.status_code == 200, r.text
    assert drafts.get_chat(key) is None
    rows = _by_key(client)
    assert key not in rows, "one task, not a task and the draft it came from"
    assert any(row["title"] == "Roll up the PRs" for row in rows.values())


def test_a_session_keyed_from_chat_key_is_taken_too(client, tmp_path, projects_dir):
    """The same field carries the other shape — a chat that HAS a session, hopped
    to the card before `session_id` was ever on the payload."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    target = tmp_path / "project"
    target.mkdir()
    client.put("/api/drafts/chat/sess-a", json={"text": "and then deploy"})

    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "and then deploy",
                          "delay_seconds": 600, "from_chat_key": "sess-a"})
    assert r.status_code == 200, r.text
    assert drafts.get_chat("sess-a") is None


def test_a_bad_or_absent_from_chat_key_changes_nothing(client, tmp_path):
    """Optional and silently ignored — every client written before drafts
    existed sends none, and a malformed one is not worth a 400 on a request that
    has already scheduled the task."""
    target = tmp_path / "project"
    target.mkdir()
    key = "new:" + str(target)
    client.put(_chat_url(key), json={"text": "untouched"})
    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "hi",
                          "delay_seconds": 600, "from_chat_key": "/etc/passwd"})
    assert r.status_code == 200, r.text
    assert drafts.get_chat(key) is not None


# ------------------------------ round 3: a draft that belongs to a session
#
# THE BUG (Akshil, 2026-09-12). The chat composer's Schedule button can hop out
# of a conversation that has ALREADY RUN, and the task being written is the next
# message of that thread. Press Schedule straight away and it landed there,
# because the page still held the session id. Exit the card and the draft on
# disk knew nothing about it: reopening that draft and scheduling it opened a
# SECOND session with a SECOND task number, and the TASK-nnn the reader had been
# watching was gone.
#
# So the binding is STORED, and everything below follows from it: such a draft
# is not a row and not a number — the CONVERSATION's row is the one the reader
# knows, and it simply grows the red `✎ Draft` chip — and the way back into the
# form is the composer's own Schedule button, which reopens the draft already
# bound to that session instead of minting a second one.
#
# Round 3 built it the other way, with a draft row wearing the session's
# identity and the session's own row held back behind it, and every part of that
# read wrong: a second thing to read, the conversation gone from the Cards wall
# (which draws transcripts, and a form has none), and one session row that
# opened a modal where every other opens the chat (bugbot, PR #1126).


def _bound_draft(client, ident, session_id, target, **fields):
    """The hop's first autosave: a task draft that names the session it is a
    message to, exactly as `NewJobModal`'s `draftBody` sends it."""
    body = {"title": "Ship the changelog", "target": str(target),
            "session_id": session_id}
    body.update(fields)
    r = client.put("/api/drafts/task/" + ident, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_session_a_draft_is_going_into_round_trips(state_dir):
    """Stored and validated as a SESSION id, never as `new:<file>`: binding is to
    a thread, and a chat that has never been sent has none."""
    stored = _put("draft-0001", {"title": "hi", "session_id": "sess-a"})
    assert stored["session_id"] == "sess-a"
    assert drafts.get_task("draft-0001")["session_id"] == "sess-a"
    assert drafts.bound_session("sess-a") == "sess-a"
    assert drafts.bound_session("new:/Users/me/x.py") == ""
    assert drafts.bound_session("/etc/passwd") == ""
    assert drafts.bound_session(None) == ""
    # ...and it survives the saves that do not mention it, like every other
    # stored field (`TASK_FIELDS`): the modal's later autosaves may send only
    # what changed.
    assert _put("draft-0001",
                {"title": "hi again"})["session_id"] == "sess-a"


# --------------------------------------- one bound draft per session (server)
#
# THE OWNER'S OWN RULE, "a draft moves, never duplicates" (design.md, Round 2),
# read the other way round. `from_chat_key` above keeps it for the chat → task
# hop; this is the same promise for two TASK drafts that both end up naming the
# same session — a New task form opened twice out of one conversation's
# Schedule button, say, once before a reload and once after. The session's row
# can wear exactly one `✎ Draft` chip (`_bound_chips`), so binding a second
# draft to it is not a second fact, it is a stale copy of the first one, and
# the newest write is the one that is telling the truth (review, 2026-09-12).


def test_binding_a_second_draft_evicts_the_first_bound_to_the_same_session(
        state_dir):
    first, evicted = drafts.put_task(
        "draft-0001", {"title": "one", "session_id": "sess-a"})
    assert first is not None
    assert evicted == [], "nothing else was bound yet"

    second, evicted = drafts.put_task(
        "draft-0002", {"title": "two", "session_id": "sess-a"})
    assert second is not None
    assert second["session_id"] == "sess-a"
    # THE LISTING KEY (`draft:<id>`), same shape `unbind_session` answers in —
    # the caller announces it and a page holding that row has to be told to
    # drop it.
    assert evicted == ["draft:draft-0001"]
    assert drafts.get_task("draft-0001") is None, "the loser's words are gone"
    assert drafts.get_task("draft-0002") is not None


def test_a_put_for_the_same_id_updates_in_place_and_evicts_nobody(state_dir):
    """The ordinary autosave — the SAME draft, saved again and again — must
    never read as a second draft colliding with itself."""
    drafts.put_task("draft-0001", {"title": "one", "session_id": "sess-a"})
    record, evicted = drafts.put_task("draft-0001", {"title": "one, edited"})
    assert record["title"] == "one, edited"
    assert record["session_id"] == "sess-a", "unmentioned fields survive"
    assert evicted == []
    assert drafts.get_task("draft-0001") is not None


def test_unbound_drafts_are_untouched_by_a_binding_elsewhere(state_dir):
    """Only a draft bound to the SAME session is at risk. An unbound draft, and
    a draft bound to a DIFFERENT session, are not the fact "one per session" is
    about, whatever else is going on in the store at the time."""
    drafts.put_task("draft-unbound", {"title": "no session at all"})
    drafts.put_task(
        "draft-other", {"title": "a different thread", "session_id": "sess-b"})

    _record, evicted = drafts.put_task(
        "draft-0001", {"title": "one", "session_id": "sess-a"})
    assert evicted == []
    assert drafts.get_task("draft-unbound") is not None
    assert drafts.get_task("draft-other")["session_id"] == "sess-b"


def test_the_router_announces_the_draft_a_binding_evicted(client, tmp_path):
    """The eviction happens inside `put_task`'s own lock; a page holding the
    loser's row would go on showing it until the next full listing without the
    router telling the long-poll too (`routers/drafts.py`, the PUT handler)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)
    before = client.get("/api/tasks").json()["generation"]

    _bound_draft(client, "draft-0002", "sess-a", elsewhere)
    assert drafts.get_task("draft-0001") is None

    body = client.get(f"/api/tasks/changes?since={before}&wait=0").json()
    assert "draft:draft-0001" in body["gone"]


def test_a_session_bound_draft_is_not_a_row_and_the_session_wears_the_chip(
        client, tmp_path, projects_dir):
    """ONE TASK, ONE ROW — and it is the CONVERSATION's row (Akshil,
    2026-09-12).

    The draft is the thread's next message being written, so it is not a second
    thing beside the thread. Nothing is emitted for it and nothing is hidden for
    it: the session's row stands exactly as it always did — same title, same
    number, same message count, same lane — and grows two fields, the id of the
    form being written into it and that form's preview for the chip."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    before = _by_key(client)["sess-a"]
    assert before["task_id"] == "TASK-001"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    _bound_draft(client, "draft-0001", "sess-a", elsewhere,
                 description="ship the changelog, then tag it")
    rows = _by_key(client)
    assert "draft:draft-0001" not in rows, "a bound draft is not a row"
    row = rows["sess-a"]
    # Untouched, field for field: this is the row the reader already knew.
    assert row["task_id"] == before["task_id"] == "TASK-001"
    assert row["title"] == before["title"]
    assert row["status"] == before["status"]
    assert row["message_count"] == before["message_count"] == 1
    assert row["project"] == before["project"]
    assert row.get("kind") == before.get("kind"), "not a draft row, and never was"
    # …and the two fields that are new. The chip's words are the form's TITLE,
    # which is what the reader named this thing; the description is the fallback
    # for a form that has only been typed into.
    assert row["bound_draft"] == "draft-0001"
    assert row["draft"]["preview"] == "Ship the changelog"
    assert row["draft"]["updated_at"] > 0

    # Nothing was allocated under the draft's own key either — the number is the
    # session's, and minting a second one was the whole bug.
    assert "draft:draft-0001" not in tasks_store.task_ids()


def test_the_conversations_own_composer_outranks_the_form(client, tmp_path,
                                                          projects_dir):
    """One row, one chip, and the chat's own unsent words win.

    A session can have both — text left in its composer AND a New task form
    opened out of it — and `draft` is one field because the reader has one
    question. The composer's copy is literally sitting in that conversation; the
    form is a message about to be scheduled into it."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)
    client.put(_chat_url("sess-a"), json={"text": "half a thought"})

    row = _by_key(client)["sess-a"]
    assert row["draft"]["preview"] == "half a thought"
    assert row["bound_draft"] == "draft-0001", "still the way back to the form"


def test_a_draft_with_no_session_is_numbered_exactly_as_before(client, tmp_path):
    """The binding is the exception, not the new rule. A form opened from "+ New
    task" belongs to nobody and still mints a number of its own."""
    target = tmp_path / "project"
    target.mkdir()
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Nightly report", "target": str(target)})
    row = _by_key(client)["draft:draft-0001"]
    assert row["task_id"] == "TASK-001"
    assert row["session_id"] == ""
    assert "draft:draft-0001" in tasks_store.task_ids()


def test_discarding_a_bound_draft_takes_the_chip_off_the_row(client, tmp_path,
                                                             projects_dir):
    """The chip is the only trace, so Discard is the only thing that has to
    reverse — the row itself never moved."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)
    assert _by_key(client)["sess-a"]["bound_draft"] == "draft-0001"

    assert client.delete("/api/drafts/task/draft-0001").status_code == 200
    row = _by_key(client)["sess-a"]
    assert row["bound_draft"] == ""
    assert row["draft"] is None
    assert row["task_id"] == "TASK-001", "nothing about the row was ever changed"
    assert row["message_count"] == 1


def test_emptying_a_bound_draft_takes_the_chip_off_too(client, tmp_path,
                                                       projects_dir):
    """An all-empty form is a DELETE (`_empty_task`), and the chip has to go on
    that door as well — not only on Discard."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)
    assert _by_key(client)["sess-a"]["bound_draft"] == "draft-0001"

    r = client.put("/api/drafts/task/draft-0001",
                   json={"title": "", "description": "", "target": str(elsewhere),
                         "session_id": "sess-a", "attachments": []})
    assert r.json()["draft"] is None
    rows = _by_key(client)
    assert "draft:draft-0001" not in rows
    assert rows["sess-a"]["bound_draft"] == ""
    assert rows["sess-a"]["draft"] is None


def test_the_chip_repaints_through_the_changes_endpoint(client, tmp_path,
                                                        projects_dir):
    """One row moves, and it is the session's — the draft write announces that
    key too (`routers/drafts._announce`), which is what makes the chip appear
    and vanish without a reload."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    _by_key(client)  # a first full listing, so the session is numbered
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    before = client.get("/api/tasks").json()["generation"]
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)
    body = client.get("/api/tasks/changes?since=%d&wait=0" % before).json()
    rows = {row["key"]: row for row in body["rows"]}
    assert rows["sess-a"]["bound_draft"] == "draft-0001"
    assert rows["sess-a"]["draft"]["preview"] == "Ship the changelog"
    # The draft's own key is announced as well and answers `gone`: it is not a
    # row, and a page holding one from an older build is told to drop it.
    assert "draft:draft-0001" in body["gone"]

    gen = body["generation"]
    assert client.delete("/api/drafts/task/draft-0001").status_code == 200
    body = client.get("/api/tasks/changes?since=%d&wait=0" % gen).json()
    rows = {row["key"]: row for row in body["rows"]}
    assert rows["sess-a"]["bound_draft"] == ""
    assert rows["sess-a"]["draft"] is None


def test_a_poll_about_the_session_alone_carries_the_chip(client, tmp_path,
                                                         projects_dir):
    """The chip is read off the DRAFT STORE, never off the rows a build happened
    to produce.

    A narrowed build asking about a session alone runs no draft half at all
    (`_draft_shaped`), so a join that depended on a draft row having been built
    would drop the chip on exactly that poll — and the row the page is holding
    would lose it until the next full listing."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)

    narrowed = tasks_mod._task_rows(only=frozenset({"sess-a"}))
    assert [r["key"] for r in narrowed] == ["sess-a"]
    assert narrowed[0]["bound_draft"] == "draft-0001"
    assert narrowed[0]["draft"]["preview"] == "Ship the changelog"


def test_scheduling_a_bound_draft_lands_in_the_session_and_keeps_the_number(
        client, tmp_path, projects_dir):
    """The whole bug, end to end: reopen the draft, press Schedule, and the
    message is the next turn of the SAME conversation under the SAME number."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    number = _by_key(client)["sess-a"]["task_id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)

    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(elsewhere), "message": "ship it",
                          "delay_seconds": 600, "title": "Ship the changelog",
                          "draft_id": "draft-0001", "session_id": "sess-a"})
    assert r.status_code == 200, r.text
    entry = r.json()["entry"]
    assert entry["session_id"] == "sess-a"

    rows = _by_key(client)
    assert "draft:draft-0001" not in rows, "the draft's whole purpose is over"
    assert "pending:" + entry["id"] not in rows, "the entry is filed on the session"
    assert rows["sess-a"]["task_id"] == number
    # ...and no number was invented on the way. `pending:<entry-id>` is not a key
    # this task is ever filed under, so a rekey onto it would have been a second
    # identity for a task that already has one.
    store = tasks_store.task_ids()
    assert "pending:" + entry["id"] not in store
    assert "draft:draft-0001" not in store


def test_an_unbound_draft_still_rekeys_onto_its_pending_entry(client, tmp_path):
    """The guard is only for the bound case — pinned here as the other side of
    the branch. A draft that belongs to nobody still carries its number forward
    onto the entry it becomes."""
    target = tmp_path / "project"
    target.mkdir()
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Nightly report", "target": str(target)})
    # A number is allocated by the LISTING, not by the save, so the draft has to
    # have been listed once before there is anything to carry forward.
    assert _by_key(client)["draft:draft-0001"]["task_id"] == "TASK-001"
    r = client.post("/api/schedule", headers=WRITE,
                    json={"target": str(target), "message": "roll it up",
                          "delay_seconds": 600, "draft_id": "draft-0001"})
    entry_id = r.json()["entry"]["id"]
    assert "pending:" + entry_id in tasks_store.task_ids()


def test_a_bound_draft_never_reaches_the_new_chat_settle(client, tmp_path,
                                                         projects_dir, runs):
    """`_settle_new_chats` is untouched by any of this: it reads `new:<file>`
    CHAT drafts and the run that spent one, and a task draft bound to a session
    is neither."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)
    _task_drafts, chat_drafts = drafts.list_all()
    assert tasks_mod._settle_new_chats(chat_drafts) is False
    assert drafts.get_task("draft-0001")["session_id"] == "sess-a"


class _SettledAgent:
    """The same runs tree with nobody waiting on it — what `_park` leaves behind
    once the card has been answered."""

    def __init__(self, runs):
        self.RUNS = str(runs)

    def _permissions(self, run_dir):
        return [{"id": "p1", "tool": "Bash", "decision": "allow", "input": {}}]

    def _alive(self, run_dir):
        return False

    def _session_from_out(self, run_dir):
        return ""


def _park(monkeypatch, runs, session_id):
    """One LIVE run, parked on a permission card nobody has answered — the state
    `_status` reads as `needs_attention`.

    A stand-in agent of the same shape the `runs` fixture installs, with the two
    answers `_parked_runs` actually asks for: an undecided request, and a process
    that is still alive to be unblocked."""
    _stage_run(runs, "r-1", "/home/me/proj", session_id)

    class _Parked:
        RUNS = str(runs)

        def _permissions(self, run_dir):
            return [{"id": "p1", "tool": "Bash", "decision": "",
                     "input": {"command": "rm -rf build"}}]

        def _alive(self, run_dir):
            return True

        def _session_from_out(self, run_dir):
            return ""

        def _tool_detail(self, name, inp):
            return str((inp or {}).get("command") or "")

    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: _Parked())


def test_a_live_session_is_untouched_by_a_draft_bound_to_it(
        client, tmp_path, projects_dir, runs, monkeypatch):
    """Nothing is hidden, whatever the conversation is doing — and a run parked
    on a permission card is the case that used to be worth a rule of its own.

    Round 3 dropped a settled session's row behind its draft and had to carve
    out the live statuses so a run in flight could never vanish from the listing
    or from the pulse the sidebar dot and Notifications read. Nothing is dropped
    now, so there is no rule to carve: the row is the row, the pill says what
    the run is doing, and the chip says a message is being written to it."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)
    _park(monkeypatch, runs, "sess-a")

    rows = _by_key(client)
    assert rows["sess-a"]["status"] == "needs_attention"
    assert rows["sess-a"]["bound_draft"] == "draft-0001"
    assert "draft:draft-0001" not in rows

    # The pulse is the half the sidebar reads, and a listing that hid rows was a
    # dot that never lit and a Notification nobody got.
    pulse = {t["key"] for t in client.get("/api/tasks/pulse").json()["tasks"]}
    assert "sess-a" in pulse

    # …and the narrowed build the changes long-poll runs says the same thing.
    assert [r["key"] for r in
            tasks_mod._task_rows(only=frozenset({"sess-a"}))] == ["sess-a"]

    # …and the settled case is the same case. There is no second behaviour here.
    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: _SettledAgent(runs))
    rows = _by_key(client)
    assert rows["sess-a"]["bound_draft"] == "draft-0001"
    assert "draft:draft-0001" not in rows


def test_erasing_a_session_cuts_its_task_draft_loose(client, tmp_path,
                                                     projects_dir):
    """The words stay, the binding does not (review, 2026-09-12).

    A chat draft is deleted by an erase — there is no conversation left for it to
    be typed into — but a TASK draft is a form somebody is still filling in, and
    the session was only where they had meant to send it."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    assert _by_key(client)["sess-a"]["task_id"] == "TASK-001"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)

    before = client.get("/api/tasks").json()["generation"]
    assert client.post("/api/tasks/erase",
                       json={"key": "sess-a"}).status_code == 200
    stored = drafts.get_task("draft-0001")
    assert stored["session_id"] == "", "the binding"
    assert stored["title"] == "Ship the changelog", "and not the words"

    # An ordinary `draft:<id>` again: its own number, in its own folder. The
    # session's TASK-001 is a reservation now (`forget_session` stamps it
    # `erased` so it can never be reissued), and a draft wearing it for ever was
    # the bug.
    row = _by_key(client)["draft:draft-0001"]
    assert row["session_id"] == ""
    assert row["project"] == canonical_fs_path(str(elsewhere))
    ids = tasks_store.task_ids()
    assert "draft:draft-0001" in ids, "an allocation of its own at last"
    # Numbers are per project and this draft points at a folder of its own, so
    # the digits may perfectly well read TASK-001 again — what changed is WHOSE
    # number it is. The session's record stays as a reservation nobody may
    # reissue, and the draft is no longer wearing it.
    assert ids["draft:draft-0001"]["project"] != ids["sess-a"]["project"]
    assert row["task_id"] == tasks_store.format_task_id(ids["draft:draft-0001"]["n"])
    assert tasks_store.erased() == {"sess-a"}

    # AND THE DRAFT'S OWN KEY IS ANNOUNCED (bugbot, PR #1126). The erase used to
    # notify the session key alone, so the page went on holding a row — or, for
    # the build where it was one, a draft row — carrying the dead TASK number and
    # the erased `session_id`, and pressing Schedule on it sent the message back
    # into the conversation this gesture had just destroyed. A narrowed poll for
    # the draft's key now answers the re-numbered, unbound row.
    body = client.get("/api/tasks/changes?since=%d&wait=0" % before).json()
    moved = {r["key"]: r for r in body["rows"]}
    assert "draft:draft-0001" in moved, "the row that just changed its number"
    assert moved["draft:draft-0001"]["session_id"] == ""
    assert moved["draft:draft-0001"]["project"] == canonical_fs_path(str(elsewhere))
    assert "sess-a" in body["gone"], "and the conversation itself"


def test_a_draft_left_bound_to_an_erased_session_wears_no_dead_number(
        client, tmp_path, projects_dir):
    """AN ERASED RECORD IS NO RECORD, read off the store rather than trusted to
    have been unbound.

    A bound draft is not a row — the conversation's row wears its chip instead —
    and an ERASED conversation has no row to wear one, so a draft still naming it
    would be words nobody can reach. `forget_session` keeps the mapping as a
    reservation, which is exactly the fact that says so, and such a draft is an
    ordinary row again: blank-numbered (nothing may reissue the session's TASK
    number) and in its own folder. Covers the draft written between the erase's
    two writes, and any store left bound by an older build."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("one", T9)])
    assert _by_key(client)["sess-a"]["task_id"] == "TASK-001"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _bound_draft(client, "draft-0001", "sess-a", elsewhere)
    tasks_store.forget_session("sess-a")   # the erase's share of it, by hand

    row = _by_key(client)["draft:draft-0001"]
    assert row["task_id"] == "", "a blank number, never a dead one"
    assert row["project"] == canonical_fs_path(str(elsewhere))


def test_unbind_session_cuts_that_session_and_nothing_else(state_dir):
    """The store's half on its own: one pass, one lock, and no write at all when
    nothing is bound.

    It answers the LISTING KEYS it cut, not a count (bugbot, PR #1126): those
    rows have just changed their number, their folder and the thread they would
    be sent to, and the caller has to announce them."""
    _put("draft-0001", {"title": "a", "session_id": "sess-a"})
    _put("draft-0002", {"title": "b", "session_id": "sess-b"})
    _put("draft-0003", {"title": "c"})
    typed_at = drafts.get_task("draft-0001")["updated_at"]

    assert drafts.unbind_session("sess-a") == ["draft:draft-0001"]
    cut = drafts.get_task("draft-0001")
    assert cut["session_id"] == ""
    assert cut["title"] == "a", "the words are not what an erase takes"
    # …and the clock the row prints and sorts on did not move: nobody typed.
    assert cut["updated_at"] == typed_at
    assert drafts.get_task("draft-0002")["session_id"] == "sess-b"
    assert drafts.get_task("draft-0003")["session_id"] == ""

    assert drafts.unbind_session("sess-a") == [], "nothing bound, nothing written"
    assert drafts.unbind_session("") == []
    assert drafts.unbind_session(None) == []


def test_a_custom_repeat_keeps_its_rule(state_dir):
    """`repeat` is a preset KEY, and "custom" is a pointer at a rule the
    recurrence dialog built. A draft that stored the key and dropped the rule
    reopened saying Custom, holding nothing, with Save refused and nothing on the
    card saying why."""
    rule = {"freq": "week", "interval": 2, "byday": [1, 3]}
    stored = _put("draft-0001", {"title": "Standup notes",
                                 "repeat": "custom",
                                 "custom_rule": rule})
    assert stored["custom_rule"] == rule
    assert drafts.get_task("draft-0001")["custom_rule"] == rule
    # Pass-through, not parsed: this store is not the authority on what a
    # recurrence rule looks like, and a shape it validated would be a second
    # copy of `recur`'s grammar going stale.
    assert _put("draft-0002", {"title": "Retro",
                               "custom_rule": {"freq": "fortnight"}}
               )["custom_rule"] == {"freq": "fortnight"}
    # A field the client omits keeps what the stored draft had, like every other.
    assert _put("draft-0001", {"title": "Standup"}
               )["custom_rule"] == rule
    # …and clearing the choice clears the rule with it.
    assert _put("draft-0001", {"repeat": None, "custom_rule": None}
               )["custom_rule"] is None


def test_a_rule_alone_is_not_a_draft(state_dir):
    """A recurrence rule is a SETTING — how a task would run, not a task
    (Akshil, 2026-09-12). It used to count, and the cost was an "Untitled draft"
    row minted by opening the when-row and picking a time."""
    assert _put("draft-0001",
                {"custom_rule": {"freq": "month"}}) is None
    assert drafts.get_task("draft-0001") is None
    # …and it rides along perfectly well once there are words to ride with.
    assert _put("draft-0001", {"title": "Monthly report",
                               "custom_rule": {"freq": "month"}}
               )["custom_rule"] == {"freq": "month"}


def test_the_rule_rides_down_on_the_draft_row(client, tmp_path):
    """The row's `form` is what the modal reopens on, so the rule has to be in
    it or the round trip is only half built."""
    rule = {"freq": "month", "monthly": "nth-weekday"}
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Monthly report", "target": str(tmp_path),
                     "repeat": "custom", "custom_rule": rule})
    row = _by_key(client)["draft:draft-0001"]
    assert row["form"]["repeat"] == "custom"
    assert row["form"]["custom_rule"] == rule


# ------------------------------------------- one read of the file, one lock


def test_list_all_answers_both_halves_off_one_read(state_dir, monkeypatch):
    """`list_task()` and `list_chat()` are each a whole `load()`, and the tasks
    listing wants both on every build. Same projections, half the reads."""
    _put("draft-0001", {"title": "Nightly report"})
    drafts.put_chat("sess-a", "half a thought", [])

    reads = []
    real = drafts.load
    monkeypatch.setattr(drafts, "load", lambda: (reads.append(1), real())[1])

    task, chat = drafts.list_all()
    assert len(reads) == 1
    assert task == drafts.list_task()
    assert chat == drafts.list_chat()


def test_a_narrowed_build_about_nothing_draft_shaped_never_takes_the_ids_lock(
        client, projects_dir, tmp_path, monkeypatch):
    """`ensure_ids(reproject=True)` is a write-shaped pass under the task_ids
    lock, and `_draft_numbers` was taking it on EVERY build — including the
    `/api/tasks/changes` builds narrowed to one unrelated session. `_draft_rows`
    only ever emits `draft:`/`new:` keys (a chat draft on a real session is a
    CHIP on that session's row, not a row), so such a build had no draft row to
    find and was paying the lock for an empty list."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj",
                      [_user("pull today's news", T9)])
    # Both kinds of draft exist, so the skip is about the NARROWING and not
    # about an empty store — and sess-a carries a chat draft of its own, the
    # one thing a session key does have to keep answering for.
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Nightly report", "target": str(tmp_path)})
    client.put("/api/drafts/chat/sess-a", json={"text": "half a thought"})

    calls = []
    real = tasks_store.ensure_ids
    monkeypatch.setattr(tasks_store, "ensure_ids",
                        lambda *a, **kw: (calls.append(kw), real(*a, **kw))[1])

    rows = tasks_mod._task_rows(only=frozenset({"sess-a"}))
    assert [r["key"] for r in rows] == ["sess-a"]
    # The chip is still joined on — that read is the build's own, not the
    # draft-row half's.
    assert rows[0]["draft"]["preview"] == "half a thought"
    assert not any(kw.get("reproject") for kw in calls)

    # ...and a build that DOES name a draft still numbers it.
    calls.clear()
    rows = tasks_mod._task_rows(only=frozenset({"draft:draft-0001"}))
    assert [r["key"] for r in rows] == ["draft:draft-0001"]
    assert any(kw.get("reproject") for kw in calls)


def test_the_full_listing_is_unchanged_by_the_narrowing(client, projects_dir,
                                                        tmp_path):
    """The skip is `only`-only: an unnarrowed build still builds every draft
    row, numbers and all."""
    _write_transcript(projects_dir, "sess-a", "/home/me/proj",
                      [_user("pull today's news", T9)])
    client.put("/api/drafts/task/draft-0001",
               json={"title": "Nightly report", "target": str(tmp_path)})
    client.put(f"/api/drafts/chat/{quote('new:' + str(tmp_path), safe='')}",
               json={"text": "never sent"})

    rows = _by_key(client)
    assert rows["draft:draft-0001"]["title"] == "Nightly report"
    assert rows["draft:draft-0001"]["task_id"]
    chat_key = "new:" + str(tmp_path)
    assert rows[chat_key]["title"] == "never sent"
    assert rows[chat_key]["task_id"]

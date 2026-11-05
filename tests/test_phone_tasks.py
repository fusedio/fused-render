"""`/api/phone/tasks/*` (fused_render/server/routers/phone_tasks.py,
docs/phone-tasks.md) through the real server: the list trimmed to what a
phone draws and its `unchanged` answer, archived rows on request, the folder
facet and the needs-you count, one task's transcript reduced to phone turns
from a fixture transcript (every kind, the 600-char cut, the `since` cursor),
the live card from the run's permission rows, the pending key, and — on the
write side — every verb the phone gets, the ones it never does, and the
`X-Fused` guard. The test_bots_phone.py pattern, over test_tasks_api.py's
transcript fixtures."""
import importlib.util
import json
import os

import pytest

from fused_render import schedule, tasks_store
from fused_render.server.routers import phone_tasks
from fused_render.server.routers import tasks as tasks_mod
from test_tasks_api import (  # noqa: F401 — the fixtures (home, projects_dir, state_dir, client, …) and helpers
    T9, T10, T11, T12, _ai_title, _already_using, _api_error, _assistant, _clear_caches, _entry, _later,
    _seed_schedule, _user, _write_transcript, client, home, no_real_wake, projects_dir, state_dir,
)

P = "/api/phone/tasks"
H = {"X-Fused": "1"}
DEV = {"X-Fused-Device": "a1b2c3d4e5f6"}


@pytest.fixture
def agent(projects_dir, tmp_path, monkeypatch):
    """The in-process agent module the tasks router hands the phone door (the
    test_tasks_api erase test's own loading), pointed at the fixture dirs."""
    spec = importlib.util.spec_from_file_location(
        "claude_agent_phone", os.path.join("fused_render", "claude_agent", "agent.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "PROJECTS", str(projects_dir))
    monkeypatch.setattr(mod, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(mod, "MODEL_WINDOWS", str(tmp_path / "model-windows.json"))
    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: mod)
    return mod


def _tool_use(tid, name, inp, ts):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _tool_result(tid, content, ts, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [block]}}


def _folder(tmp_path, name):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return str(d)


def _session(agent, projects_dir, tmp_path, sid, folder, records):
    target = _folder(tmp_path, folder)
    return _write_transcript(projects_dir, sid, target, records, encoded=agent._munge(target)), target


# ------------------------------------------------------------------ the list ---
def test_list_is_trimmed_rows_newest_first_with_the_facet_and_the_badge(client, agent, projects_dir, state_dir, tmp_path):
    _already_using(state_dir)
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("pull the news", T9), _assistant("done", T10), _ai_title("Pull news", "sess-a")])
    _session(agent, projects_dir, tmp_path, "sess-b", "alpha", [_user("x" * 500, T11), _assistant("ok", T12)])
    _session(agent, projects_dir, tmp_path, "sess-c", "beta", [_user("hello", T9), _api_error("Usage limit reached", T10)])
    r = client.get(P)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"tasks", "total", "generation", "projects", "attention", "ts"}
    rows = out["tasks"]
    # Newest activity first: b (T12), then c and a (both T10, the server's own tie order).
    assert rows[0]["key"] == "sess-b" and {rows[1]["key"], rows[2]["key"]} == {"sess-a", "sess-c"}
    a = next(t for t in rows if t["key"] == "sess-a")
    # Exactly the phone's keys, and nothing from the desktop's wide row.
    assert set(a) == {"key", "task_id", "title", "title_source", "status", "failed", "blocked_reason", "attention",
                      "live", "unread", "starred", "project", "project_label", "target_label", "entry_url", "repo", "prs",
                      "backend", "model", "last_message", "last_reply", "started", "last_active", "happened_at",
                      "message_count", "next_run", "next_run_repeats", "queue_position", "queue_ahead_title",
                      "entry_id", "bot"}
    assert "messages" not in a and "description" not in a
    assert a["title"] == "Pull news" and a["task_id"].startswith("TASK-") and a["status"] == "done"
    assert a["project_label"] == "alpha" and a["project"].endswith("alpha")
    b = next(t for t in rows if t["key"] == "sess-b")
    assert len(b["title"]) <= phone_tasks.TEXT_MAX
    assert b["last_message"]["role"] == "user" and len(b["last_message"]["text"]) == phone_tasks.TEXT_MAX
    # The facet counts the rows shown; the badge counts the needs-you rows.
    facet = {p["label"]: p["count"] for p in out["projects"]}
    assert facet == {"alpha": 2, "beta": 1}
    assert out["projects"][0]["label"] == "alpha"  # most tasks first
    c = next(t for t in rows if t["key"] == "sess-c")
    assert c["status"] == "blocked" and c["blocked_reason"] == "usage_limit"  # the API-error row: needs you
    assert out["attention"] == 1
    # The poll with the last answer's token costs nothing; the token is the
    # generation PLUS what the rows say, so the view switch is a change too.
    again = client.get(P, params={"gen": out["generation"]}).json()
    assert again == {"unchanged": True, "generation": out["generation"]}
    assert isinstance(out["generation"], str) and "." in out["generation"]
    assert client.get(P, params={"gen": "nope"}).json()["tasks"]
    assert client.get(P, params={"gen": out["generation"], "archived": "1"}).json()["tasks"]


def test_list_limit_and_its_validation(client, agent, projects_dir, state_dir, tmp_path):
    for i in range(4):
        _session(agent, projects_dir, tmp_path, f"sess-{i}", "alpha", [_user(f"m{i}", T9)])
    out = client.get(P, params={"limit": "2"}).json()
    assert len(out["tasks"]) == 2 and out["total"] == 4
    assert client.get(P, params={"limit": "x"}).status_code == 400
    # The token names the limit: a wider page is never "unchanged".
    assert client.get(P, params={"limit": "2", "gen": out["generation"]}).json() == {"unchanged": True, "generation": out["generation"]}
    assert len(client.get(P, params={"limit": "3", "gen": out["generation"]}).json()["tasks"]) == 3


def test_needs_you_rows_ride_past_the_cap(client, agent, projects_dir, state_dir, tmp_path):
    """The badge counts every needs-you row; the list can show each of them even
    when the cap would have cut it off."""
    _already_using(state_dir)
    for i in range(3):
        _session(agent, projects_dir, tmp_path, f"sess-{i}", "alpha", [_user(f"m{i}", T11), _assistant("ok", T12)])
    _session(agent, projects_dir, tmp_path, "sess-old", "alpha", [_user("hello", T9), _api_error("Usage limit reached", T10)])
    out = client.get(P, params={"limit": "2"}).json()
    assert out["attention"] == 1 and out["total"] == 4
    assert len(out["tasks"]) == 3 and out["tasks"][-1]["key"] == "sess-old" and out["tasks"][-1]["status"] == "blocked"


def test_archived_rows_only_on_request(client, agent, projects_dir, state_dir, tmp_path):
    _already_using(state_dir)
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9), _assistant("done", T10)])
    _session(agent, projects_dir, tmp_path, "sess-b", "alpha", [_user("two", T9), _assistant("done", T10)])
    assert client.post("/api/tasks/archive", headers=H, json={"key": "sess-a"}).status_code == 200
    out = client.get(P).json()
    assert [t["key"] for t in out["tasks"]] == ["sess-b"]
    assert out["projects"] == [{"path": out["tasks"][0]["project"], "label": "alpha", "count": 1}]
    both = client.get(P, params={"archived": "1"}).json()
    assert {t["key"]: t["status"] for t in both["tasks"]} == {"sess-a": "archived", "sess-b": "done"}


def test_drafts_never_reach_the_phone(client, agent, projects_dir, state_dir, tmp_path, monkeypatch):
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9)])
    rows, gen = tasks_mod._listing()
    draft = {**rows[0], "key": "draft:1", "kind": "draft", "state": "draft", "title": "Untitled draft"}
    monkeypatch.setattr(tasks_mod, "_listing", lambda at_least=None: (rows + [draft], gen))
    assert [t["key"] for t in client.get(P).json()["tasks"]] == ["sess-a"]


# ------------------------------------------------------------------ the detail ---
def test_detail_reduces_the_transcript_to_phone_turns(client, agent, projects_dir, state_dir, tmp_path):
    _already_using(state_dir)
    big = "line\n" * 400
    target = _folder(tmp_path, "alpha")
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [
        _user("fix the build", T9),
        {"type": "assistant", "timestamp": T10, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "npm test", "description": "Run the tests"}},
        ]}},
        _tool_result("t1", big, T10),
        _tool_use("t2", "Edit", {"file_path": os.path.join(target, "app.py"), "old_string": "a", "new_string": "b"}, T11),
        _tool_result("t2", "boom", T11, is_error=True),
        _assistant("Fixed it.", T11),
        _api_error("Usage limit reached", T12),
    ])
    r = client.get(f"{P}/sess-a")
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"task", "turns", "cursor", "first", "total", "reset", "live", "run_id", "cards", "card", "scheduled", "ts"}
    assert out["task"]["key"] == "sess-a" and out["live"] is False and out["card"] is None and out["cards"] == []
    kinds = [(t["kind"], t["i"]) for t in out["turns"]]
    assert [k for k, _ in kinds] == ["user", "assistant", "tool", "tool", "assistant", "system"]
    assert [i for _, i in kinds] == list(range(6)) and out["cursor"] == 6 and out["first"] == 0
    user, prose, bash, edit, reply, err = out["turns"]
    assert user["text"] == "fix the build" and user["at"]
    assert prose["text"] == "Looking."
    assert bash["name"] == "Bash" and bash["status"] == "ok" and "test" in bash["summary"].lower()
    assert len(bash["detail"]) <= phone_tasks.DETAIL_MAX
    assert edit["name"] == "Edit" and edit["status"] == "error" and "app.py" in edit["summary"]
    assert reply["text"] == "Fixed it."
    assert err["tone"] == "error" and "Usage limit" in err["text"]
    for t in out["turns"]:
        assert "segments" not in t and "uuid" not in t and "thinking" not in t and t["id"]
    assert len({t["id"] for t in out["turns"]}) == 6
    assert out["cursor"] == 6 and out["total"] == 6 and out["reset"] is False
    # `since=<cursor>` re-sends the last turn (a reply still streaming grows in place) and nothing before it.
    tail = client.get(f"{P}/sess-a", params={"since": "6", "last": err["id"]}).json()
    assert [t["i"] for t in tail["turns"]] == [5] and tail["cursor"] == 6 and tail["first"] == 5 and tail["reset"] is False
    # The phone names a turn that is not there any more (or a cursor past the end): start over.
    for params in ({"since": "6", "last": "gone"}, {"since": "9"}, {"since": "3", "last": user["id"]}):
        again = client.get(f"{P}/sess-a", params={**params, "limit": "2"}).json()
        assert again["reset"] is True and [t["i"] for t in again["turns"]] == [4, 5], params
    # A cursor far behind is paged, not dumped: `limit` turns from it, and `cursor` says where to go on.
    page = client.get(f"{P}/sess-a", params={"since": "1", "last": user["id"], "limit": "2"}).json()
    assert [t["i"] for t in page["turns"]] == [0, 1] and page["cursor"] == 2 and page["reset"] is False
    # A window: the last `limit` turns, `first` says where they start.
    win = client.get(f"{P}/sess-a", params={"limit": "2"}).json()
    assert [t["i"] for t in win["turns"]] == [4, 5] and win["first"] == 4
    # Load earlier: the `limit` turns before an index.
    earlier = client.get(f"{P}/sess-a", params={"before": "4", "limit": "2"}).json()
    assert [t["i"] for t in earlier["turns"]] == [2, 3] and earlier["first"] == 2 and earlier["cursor"] == 6
    assert client.get(f"{P}/sess-a", params={"before": "1", "limit": "5"}).json()["turns"][0]["i"] == 0
    assert client.get(f"{P}/sess-a", params={"since": "x"}).status_code == 400


def test_detail_of_an_unknown_key_is_404(client, agent):
    r = client.get(f"{P}/nope")
    assert r.status_code == 404 and r.json()["error"]


def test_detail_carries_the_live_cards_and_names_the_one_that_is_live(client, agent, projects_dir, state_dir, tmp_path, monkeypatch):
    _already_using(state_dir)
    _, target = _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("go", T9), _assistant("sure", T10)])
    perms = [
        {"id": "r1", "tool": "Bash", "tool_use_id": "t1", "input": {"command": "rm -rf build", "description": "Clean"},
         "created_at": 1.0, "decision": "allow", "held": False, "scope": "once", "mode": "", "reason": "",
         "severity": "", "risk_summary": "", "risk_state": "", "answers": {}},
        {"id": "r2", "tool": "AskUserQuestion", "tool_use_id": "t2",
         "input": {"questions": [{"header": "Scope", "question": "Which one?", "multiSelect": False,
                                  "options": [{"label": "A", "description": "the first"}, {"label": "B"}]}]},
         "created_at": 2.0, "decision": "", "held": True, "scope": "", "mode": "", "reason": "",
         "severity": "", "risk_summary": "", "risk_state": "", "answers": {}},
        {"id": "r3", "tool": "Bash", "tool_use_id": "t3", "input": {"command": "git push --force"},
         "created_at": 3.0, "decision": "", "held": False, "scope": "", "mode": "", "reason": "",
         "severity": "high", "risk_summary": "Rewrites remote history", "risk_state": "ready", "answers": {}},
    ]
    monkeypatch.setattr(agent, "_history_live",
                        lambda file, session_id, inbox=True: {"live_run": "run-1", "permissions": perms, "mode": "", "inbox": []})
    out = client.get(f"{P}/sess-a").json()
    assert out["live"] is True and out["run_id"] == "run-1"
    assert [c["request_id"] for c in out["cards"]] == ["r1", "r2", "r3"]
    # A held card is ANSWERED (the queue holds the answer until the folder frees): not pending, never live.
    assert [c["verdict"] for c in out["cards"]] == ["allow", "held", "pending"]
    assert [c["kind"] for c in out["cards"]] == ["permission", "question", "permission"]
    q = out["cards"][1]
    assert q["held"] is True and q["questions"] == [{"header": "Scope", "question": "Which one?", "multiSelect": False,
                                                     "options": [{"label": "A", "description": "the first"}, {"label": "B", "description": ""}]}]
    push = out["cards"][2]
    assert push["detail"] == "git push --force" and push["risk"] == {"severity": "high", "summary": "Rewrites remote history"}
    assert out["cards"][0]["risk"] is None
    # The live card is the oldest undecided one the queue is not holding — the one the run is blocked on.
    assert out["card"]["request_id"] == "r3"
    assert push["detail"] == "git push --force"
    for c in out["cards"]:
        assert "input" not in c and "tool_use_id" not in c


def test_a_pending_key_answers_with_the_scheduled_message_and_no_conversation(client, agent, state_dir, tmp_path):
    target = _folder(tmp_path, "alpha")
    _seed_schedule([_entry("e1", "later, do the thing " + "x" * 700, _later(), target=target)])
    key = tasks_store.pending_key("e1")
    out = client.get(f"{P}/{key}").json()
    assert out["task"]["key"] == key and out["task"]["status"] == "upcoming"
    assert out["turns"] == [] and out["cursor"] == 0 and out["live"] is False and out["card"] is None
    [sched] = out["scheduled"]
    assert sched["entry_id"] == "e1" and sched["state"] == schedule.PENDING
    assert sched["text"].startswith("later, do the thing") and len(sched["text"]) <= phone_tasks.DETAIL_MAX
    listed = client.get(P).json()["tasks"]
    assert [t["key"] for t in listed] == [key] and listed[0]["next_run"]


# ------------------------------------------------------------------ `_turns` ---
def test_turns_reducer_on_fixture_segments():
    """The reducer alone (no server): prose and tool calls interleaved in one
    assistant turn come out in order, a stopped turn says so, a compaction is a
    system line, thinking is dropped, a segment's text may still be a list."""
    turns = [
        {"role": "user", "text": "hi", "uuid": "u1", "ts": 1.0},
        {"role": "assistant", "text": "A B", "uuid": "a1", "stopped": True, "segments": [
            {"kind": "thinking", "text": "hmm"},
            {"kind": "text", "text": ["A", " "]},
            {"kind": "tool", "id": "t1", "name": "Read", "input": {"file_path": "/x/y/z.py"}, "status": "ok", "output": "ok" * 400, "images": []},
            {"kind": "text", "text": "B", "ts": 2.0},
            {"kind": "notice", "text": "Interrupted", "status": "interrupted"},
        ]},
        {"role": "compact", "text": "", "uuid": "c1"},
        {"role": "assistant", "text": "   ", "uuid": "a2"},
        {"role": "error", "text": "Wi-Fi off"},
        "garbage",
    ]
    out = phone_tasks._turns(turns, agent=None)
    assert [t["kind"] for t in out] == ["user", "assistant", "tool", "assistant", "system", "system", "system", "system"]
    assert [t["i"] for t in out] == list(range(8))
    assert out[1]["text"] == "A" and out[3]["text"] == "B" and out[3]["at"] == 2.0
    tool = out[2]
    assert tool["name"] == "Read" and tool["summary"] == "Read: z.py" and tool["status"] == "ok"
    assert len(tool["detail"]) == phone_tasks.DETAIL_MAX
    assert out[4] == {"i": 4, "id": "a1#3", "kind": "system", "text": "Interrupted", "tone": "interrupted", "at": None}
    # Ids are stable across the list's own indexes: the record uuid plus the segment run.
    assert [t["id"] for t in out[:4]] == ["u1", "a1#0", "a1#1", "a1#2"] and out[5]["id"] == "a1#stop"
    assert out[6]["id"] == "c1" and out[7]["id"].startswith("err:")
    assert out[5]["text"] == "Stopped" and out[6]["text"] == "Context compacted"
    assert out[7]["tone"] == "error" and out[7]["text"] == "Wi-Fi off"
    assert phone_tasks._turns([], agent=None) == []


def test_tool_summary_falls_back_per_tool():
    s = phone_tasks._tool_summary
    assert s(None, "Bash", {"command": "ls -la", "description": "List files"}) == "Bash: List files"
    assert s(None, "Bash", {"command": "ls -la"}) == "Bash: ls -la"
    assert s(None, "Write", {"file_path": "/a/b/app.py"}) == "Write: app.py"
    assert s(None, "Grep", {"pattern": "def main"}) == "Grep: def main"
    assert s(None, "WebFetch", {"url": "https://x.y/z"}) == "WebFetch: https://x.y/z"
    assert s(None, "Agent", {"description": "Review the diff"}) == "Agent: Review the diff"
    assert s(None, "Mystery", {}) == "Mystery"
    assert s(None, "Read", {"file_path": "/x/Readme.md"}) == "Read: Readme.md"  # a detail that starts with the name keeps its prefix
    assert len(s(None, "Bash", {"command": "x" * 500})) <= 130


# ================================================================ the write side ---
@pytest.fixture
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    (fdir / "app").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


def test_every_write_needs_the_fused_header(client, agent, projects_dir, tmp_path):
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9)])
    for path, body in ((P, {"prompt": "go", "target": "~/x"}), (P + "/new-app", {"prompt": "an app"}),
                       (P + "/sess-a/send", {"text": "hi"}), (P + "/sess-a/archive", {}),
                       (P + "/sess-a/decide", {"request_id": "r", "decision": "allow"})):
        assert client.post(path, json=body).status_code == 403, path


def test_desktop_only_verbs_and_unknown_keys_are_404(client, agent, projects_dir, tmp_path):
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9)])
    for op in ("erase", "export", "worktrees", "settings", "admit", "force", "pr-status", "seen", "ui", "nope"):
        r = client.post(f"{P}/sess-a/{op}", headers=H, json={})
        assert r.status_code == 404 and r.json()["error"], op
    r = client.post(f"{P}/missing/send", headers=H, json={"text": "hi"})
    assert r.status_code == 404 and "missing" in r.json()["error"]
    # The desktop's own doors are not reachable through the prefix either.
    assert client.post(f"{P}/sess-a/erase", headers=H, json={}).status_code == 404


def test_create_from_a_known_folder_stamps_the_phone_origin(client, agent, projects_dir, workspace, tmp_path, monkeypatch):
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9)])
    monkeypatch.setattr(schedule, "run_now", lambda entry_id: {"ok": True})
    alpha = str(tmp_path / "alpha")
    # Folders the listing does not know, folders that are not there, the home
    # dir and the root, the workspace root itself, a dot-directory and a symlink
    # under the workspace that points out of it: all refused.
    (workspace / ".hidden").mkdir()
    (workspace / "local").mkdir(exist_ok=True)
    os.symlink(str(tmp_path), str(workspace / "local" / "escape"))
    for bad in ("/etc", str(tmp_path / "nowhere"), "", None, "~/elsewhere", "~", "/", str(workspace),
                str(workspace / ".hidden"), str(workspace / "local" / "escape"), str(tmp_path / "alpha" / "..")):
        r = client.post(P, headers=H, json={"prompt": "go", "target": bad})
        assert r.status_code == 400 and "folder" in r.json()["error"], bad
    assert phone_tasks._known_folder(str(tmp_path / "alpha")) == alpha  # the row's own spelling, as written
    assert client.post(P, headers=H, json={"prompt": " ", "target": alpha}).status_code == 400
    # A row's own folder, tilde'd the way the list hands it out.
    tilded = client.get(P).json()["projects"][0]["path"]
    r = client.post(P, headers=H, json={"prompt": "do the thing", "target": tilded, "permission_mode": "auto",
                                        "model": "opus", "backend": "codex"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"key", "entry_id", "task_id"}
    assert out["key"] == tasks_store.pending_key(out["entry_id"]) and out["task_id"].startswith("TASK-")
    [entry] = [e for e in schedule.list_entries() if e["id"] == out["entry_id"]]
    assert entry["origin"] == "phone" and entry["target"] == alpha and entry["message"] == "do the thing"
    assert entry["permission_mode"] == "prompt" and not entry.get("model")  # the body's own mode and model are ignored
    # A folder under the workspace is fine too, with no row behind it.
    new = workspace / "local" / "fresh"
    new.mkdir(parents=True)
    r = client.post(P, headers=H, json={"prompt": "start here", "target": str(new)})
    assert r.status_code == 200, r.text
    assert client.get(f"{P}/{r.json()['key']}").json()["scheduled"][0]["text"] == "start here"


def test_new_app_names_scaffolds_and_answers_the_task_key(client, agent, workspace, monkeypatch):
    from fused_render.server.routers import apps as apps_mod
    calls = []
    monkeypatch.setattr(schedule, "run_now", lambda entry_id: {"ok": True})
    monkeypatch.setattr(apps_mod, "_await_sent", lambda entry_id, entry: entry)  # no 20 s wait on a stubbed send

    async def relay(body, **kw):
        calls.append(("relay", body["prompt"], body["model"]))
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True, "result": {"text": "Plant-Watering-Log.\n"}})
    from fused_render.server import ai as ai_mod
    monkeypatch.setattr(ai_mod, "_ai_relay", relay)

    r = client.post(P + "/new-app", headers=H, json={"prompt": "track which plants I watered"})
    assert r.status_code == 200, r.text
    out = r.json()
    dest = workspace / "app" / "plant-watering-log"
    assert out["app"]["name"] == "plant-watering-log" and dest.is_dir() and (dest / "index.html").is_file()
    assert out["app"]["url"] == "/render?path=" + str(dest / "index.html")
    assert out["key"] == tasks_store.pending_key(out["entry_id"]) and out["entry_id"]
    assert calls == [("relay", "track which plants I watered", "haiku")]
    # THE BUILD TASK ASKS. The scheduler's default for a scheduled message is
    # "auto", the broadest; a phone-started build runs in the asking mode, on
    # the app's entry page, stamped phone, with no model or effort.
    [entry] = [e for e in schedule.list_entries() if e["id"] == out["entry_id"]]
    assert entry["permission_mode"] == "prompt" and entry["origin"] == "phone"
    assert entry["target"] == str(dest / "index.html") and entry["message"] == "track which plants I watered"
    assert not entry.get("model") and not entry.get("effort")
    # A taken name: the desktop's own 409 and sentence.
    r = client.post(P + "/new-app", headers=H, json={"prompt": "again", "name": "plant-watering-log"})
    assert r.status_code == 409 and "already exists" in r.json()["error"]
    # A bad typed name: the desktop's validator, as a 400.
    assert client.post(P + "/new-app", headers=H, json={"prompt": "x", "name": "../up"}).status_code == 400
    assert client.post(P + "/new-app", headers=H, json={"prompt": ""}).status_code == 400


def test_new_app_falls_back_to_a_dated_slug_when_the_relay_is_down(client, agent, workspace, monkeypatch):
    from fused_render.server.routers import apps as apps_mod
    monkeypatch.setattr(apps_mod, "_create_app_task",
                        lambda entry, prompt, *rest, **kw: ({"id": "e1", "claude_session_id": "sess-x"}, None))
    monkeypatch.setattr(phone_tasks, "_dated_slug", lambda: "app-20261105-1000")

    async def down(body, **kw):
        raise RuntimeError("relay down")
    from fused_render.server import ai as ai_mod
    monkeypatch.setattr(ai_mod, "_ai_relay", down)
    r = client.post(P + "/new-app", headers=H, json={"prompt": "a thing"})
    assert r.status_code == 200, r.text
    assert r.json()["app"]["name"] == "app-20261105-1000" and r.json()["key"] == "sess-x"

    async def prose(body, **kw):
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True, "result": {"text": "Sure! How about expense tracker?"}})
    monkeypatch.setattr(ai_mod, "_ai_relay", prose)
    second = client.post(P + "/new-app", headers=H, json={"prompt": "b thing"}).json()["app"]["name"]
    # The same minute's slug is taken by the first: a suffix, never a 409 for a name nobody typed.
    assert second == "app-20261105-1000-2"
    # …even when the name is taken between the check and the copy (two quick taps).
    real = apps_mod._new_app
    raced = []

    def race(body, **kw):
        if not raced:
            raced.append(body["name"])
            (workspace / "app" / body["name"]).mkdir()
        return real(body, **kw)
    monkeypatch.setattr(apps_mod, "_new_app", race)
    third = client.post(P + "/new-app", headers=H, json={"prompt": "c thing"}).json()["app"]["name"]
    assert raced == ["app-20261105-1000-3"] and third == "app-20261105-1000-4"


def test_send_cancel_and_the_pending_refusals_go_through_the_desktop_handlers(client, agent, projects_dir, tmp_path, monkeypatch):
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9), _assistant("ok", T10)])
    seen = []
    monkeypatch.setattr(tasks_mod, "api_task_send", lambda key, body, x_fused=None: seen.append(("send", key, body, x_fused)) or {"queued": False, "key": key})
    monkeypatch.setattr(tasks_mod, "api_task_cancel", lambda key, x_fused=None: seen.append(("cancel", key)) or {"cancelled": True})
    r = client.post(f"{P}/sess-a/send", headers=H, json={"text": "more", "model": "opus", "backend": "codex"})
    assert r.status_code == 200 and r.json() == {"queued": False, "key": "sess-a"}
    assert seen[-1] == ("send", "sess-a", {"text": "more"}, "1")  # model and backend never leave the phone
    assert client.post(f"{P}/sess-a/send", headers=H, json={"text": " "}).status_code == 400
    assert client.post(f"{P}/sess-a/cancel", headers=H).json() == {"cancelled": True}
    assert seen[-1] == ("cancel", "sess-a")


def test_archive_refuses_a_running_task_with_the_desktops_words(client, agent, projects_dir, state_dir, tmp_path, monkeypatch):
    _already_using(state_dir)
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9), _assistant("ok", T10)])
    real_row = phone_tasks._row
    # The gate is the row's STATUS, the desktop's own rule (eraseBlocked): a turn in flight refuses…
    monkeypatch.setattr(phone_tasks, "_row", lambda key: {**real_row(key), "status": "in_progress", "live": False})
    r = client.post(f"{P}/sess-a/archive", headers=H)
    assert r.status_code == 409 and r.json() == {"error": "Stop the run first"}
    # …while a parked card (needs_attention) and a merely fresh transcript (live) are filed.
    monkeypatch.setattr(phone_tasks, "_row", lambda key: {**real_row(key), "status": "needs_attention", "live": True})
    assert client.post(f"{P}/sess-a/archive", headers=H).status_code == 200
    assert client.post(f"{P}/sess-a/unarchive", headers=H).status_code == 200
    monkeypatch.setattr(phone_tasks, "_row", real_row)
    assert client.post(f"{P}/sess-a/archive", headers=H).status_code == 200
    assert [t["status"] for t in client.get(P, params={"archived": "1"}).json()["tasks"]] == ["archived"]
    assert client.post(f"{P}/sess-a/unarchive", headers=H).status_code == 200
    assert [t["status"] for t in client.get(P).json()["tasks"]] == ["done"]


def test_rename_star_read_and_delete(client, agent, projects_dir, state_dir, tmp_path):
    _already_using(state_dir)
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9), _assistant("ok", T10)])
    assert client.post(f"{P}/sess-a/rename", headers=H, json={"title": "  My task "}).status_code == 200
    row = client.get(P).json()["tasks"][0]
    assert row["title"] == "My task" and row["title_source"] == "renamed" and row["unread"] >= 1
    assert client.post(f"{P}/sess-a/rename", headers=H, json={"title": 7}).status_code == 400
    assert client.post(f"{P}/sess-a/favourite", headers=H, json={"on": True}).status_code == 200
    assert client.get(P).json()["tasks"][0]["starred"] is True
    assert client.post(f"{P}/sess-a/favourite", headers=H, json={"on": "yes"}).status_code == 400
    assert client.post(f"{P}/sess-a/read", headers=H).status_code == 200
    assert client.get(P).json()["tasks"][0]["unread"] == 0
    assert client.post(f"{P}/sess-a/delete", headers=H).status_code == 200
    assert client.get(P).json()["tasks"] == []
    assert client.post(f"{P}/sess-a/delete", headers=H).status_code == 404


def test_delete_while_running_is_the_desktops_409(client, agent, projects_dir, tmp_path, monkeypatch):
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9)])
    from fastapi import HTTPException

    def refuse(patch, x_fused=None):
        raise HTTPException(status_code=409, detail="the task is running; stop it first")
    monkeypatch.setattr(tasks_mod, "api_task_delete", refuse)
    r = client.post(f"{P}/sess-a/delete", headers=H)
    assert r.status_code == 409 and r.json() == {"error": "the task is running; stop it first"}


def test_decide_forces_once_and_refuses_a_card_that_is_not_live(client, agent, projects_dir, tmp_path, monkeypatch):
    from fused_render import project_queue
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9), _assistant("ok", T10)])
    perms = [{"id": "r1", "tool": "Bash", "input": {"command": "ls"}, "decision": "", "held": False, "answers": {}},
             {"id": "r0", "tool": "Bash", "input": {"command": "pwd"}, "decision": "allow", "held": False, "answers": {}},
             {"id": "rh", "tool": "Bash", "input": {"command": "rm"}, "decision": "", "held": True, "answers": {}},
             {"id": "q1", "tool": "AskUserQuestion", "input": {"questions": [{"question": "Which?", "options": [{"label": "A"}]}]},
              "decision": "", "held": False, "answers": {}}]
    live = {"live_run": "run-1", "permissions": perms, "mode": "", "inbox": []}
    monkeypatch.setattr(agent, "_history_live", lambda file, session_id, inbox=True: live)
    decided = []
    monkeypatch.setattr(agent, "_decide", lambda **kw: decided.append(kw) or {"decided": kw["request_id"]})
    monkeypatch.setattr(project_queue, "agent_module", lambda: agent)
    monkeypatch.setattr(project_queue, "enabled", lambda: False)
    r = client.post(f"{P}/sess-a/decide", headers=H, json={"request_id": "r1", "decision": "deny", "scope": "session",
                                                            "mode": "acceptEdits", "note": "no"})
    assert r.status_code == 200, r.text
    assert r.json() == {"held": False, "decided": "r1"}
    assert decided == [{"run_id": "run-1", "request_id": "r1", "decision": "deny", "scope": "once", "mode": "",
                        "answers": "", "note": "no", "custom": ""}]
    # The decided card, a HELD card (answered at the Mac, the queue delivers it), a question card, a stranger's
    # id, a bad decision, a run that ended: refused before anything is written.
    for body in ({"request_id": "r0", "decision": "allow"}, {"request_id": "rh", "decision": "allow"},
                 {"request_id": "q1", "decision": "allow"}, {"request_id": "zzz", "decision": "allow"},
                 {"request_id": "r1", "decision": "maybe"}, {"request_id": "../x", "decision": "allow"}):
        r = client.post(f"{P}/sess-a/decide", headers=H, json=body)
        assert r.status_code == 400, body
    live["live_run"] = ""
    assert client.post(f"{P}/sess-a/decide", headers=H, json={"request_id": "r1", "decision": "allow"}).status_code == 400
    assert len(decided) == 1


def test_answer_sends_the_composers_payload(client, agent, projects_dir, tmp_path, monkeypatch):
    from fused_render import project_queue
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9), _assistant("ok", T10)])
    questions = [{"question": "Which one?\nSay.", "multiSelect": True,
                  "options": [{"label": "A"}, {"label": "B, with a comma"}, {"label": "C"}]},
                 {"question": "Colour", "options": [{"label": "red"}, {"label": "blue"}]}]
    perms = [{"id": "q1", "tool": "AskUserQuestion", "input": {"questions": questions}, "decision": "", "held": False, "answers": {}},
             {"id": "r1", "tool": "Bash", "input": {"command": "ls"}, "decision": "", "held": False, "answers": {}}]
    monkeypatch.setattr(agent, "_history_live", lambda file, session_id, inbox=True: {"live_run": "run-1", "permissions": perms, "mode": "", "inbox": []})
    decided = []
    monkeypatch.setattr(agent, "_decide", lambda **kw: decided.append(kw) or {"ok": True})
    monkeypatch.setattr(project_queue, "agent_module", lambda: agent)
    monkeypatch.setattr(project_queue, "enabled", lambda: False)
    # The card carries the question and label strings EXACTLY (a newline kept): the answer names them.
    card = client.get(f"{P}/sess-a").json()["card"]
    assert card["request_id"] == "q1" and [q["question"] for q in card["questions"]] == ["Which one?\nSay.", "Colour"]
    assert [o["label"] for o in card["questions"][0]["options"]] == ["A", "B, with a comma", "C"]
    body = {"request_id": "q1", "answers": {"Which one?\nSay.": ["C", "mine", "B, with a comma", "A"], "Colour": "blue"},
            "custom": {"Which one?\nSay.": "mine"}}
    r = client.post(f"{P}/sess-a/answer", headers=H, json=body)
    assert r.status_code == 200, r.text
    [kw] = decided
    assert kw["decision"] == "allow" and kw["scope"] == "once"
    # A list becomes the ", "-join in the card's option order with the typed line last — the string
    # the agent latches. Checked against the agent's own validator, not a copy of its rule.
    answers = json.loads(kw["answers"])
    assert answers == {"Which one?\nSay.": "A, B, with a comma, C, mine", "Colour": "blue"}
    assert agent._answers_from(questions, answers, json.loads(kw["custom"])) == answers
    assert agent._answers_from(questions, {"Which one?\nSay.": ["A"]}, None) is None  # what a list would have been
    for bad in ({"request_id": "q1", "answers": {}}, {"request_id": "q1", "answers": {"Colour": ["blue"]}, "custom": 3},
                {"request_id": "q1", "answers": {"Colour": 7}}, {"request_id": "r1", "answers": {"Colour": "blue"}}):
        assert client.post(f"{P}/sess-a/answer", headers=H, json=bad).status_code == 400, bad
    assert len(decided) == 1


def test_skip_goes_to_the_queue_door(client, agent, projects_dir, tmp_path, monkeypatch):
    _session(agent, projects_dir, tmp_path, "sess-a", "alpha", [_user("one", T9)])
    seen = []
    monkeypatch.setattr(tasks_mod, "api_queue_skip", lambda body, x_fused=None: seen.append((body, x_fused)) or {"ok": True, "position": 1})
    assert client.post(f"{P}/sess-a/skip", headers=H).json() == {"ok": True, "position": 1}
    assert seen == [({"key": "sess-a"}, "1")]

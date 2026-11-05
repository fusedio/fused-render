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
    assert [t["key"] for t in rows][:2] == ["sess-b", "sess-c"] or rows[0]["key"] in ("sess-b", "sess-c")
    a = next(t for t in rows if t["key"] == "sess-a")
    # Exactly the phone's keys, and nothing from the desktop's wide row.
    assert set(a) == {"key", "task_id", "title", "title_source", "status", "failed", "blocked_reason", "attention",
                      "live", "unread", "starred", "project", "project_label", "target_label", "entry", "repo", "prs",
                      "backend", "model", "last_message", "last_reply", "started", "last_active", "happened_at",
                      "message_count", "next_run", "next_run_repeats", "queue_position", "queue_ahead_title",
                      "entry_id", "bot"}
    assert "messages" not in a and "description" not in a
    assert a["title"] == "Pull news" and a["task_id"].startswith("TASK-") and a["status"] == "done"
    assert a["project_label"] == "alpha" and a["project"].endswith("alpha")
    b = next(t for t in rows if t["key"] == "sess-b")
    assert len(b["title"]) <= phone_tasks.TEXT_MAX
    assert b["last_message"] is None or len(b["last_message"]["text"]) <= phone_tasks.TEXT_MAX
    # The facet counts the rows shown; the badge counts the needs-you rows.
    facet = {p["label"]: p["count"] for p in out["projects"]}
    assert facet == {"alpha": 2, "beta": 1}
    assert out["projects"][0]["label"] == "alpha"  # most tasks first
    c = next(t for t in rows if t["key"] == "sess-c")
    assert out["attention"] == sum(1 for t in rows if t["status"] in ("needs_attention", "blocked"))
    assert c["status"] in ("blocked", "needs_attention", "done")
    # The poll with the snapshot's own generation costs nothing.
    again = client.get(P, params={"gen": out["generation"]}).json()
    assert again == {"unchanged": True, "generation": out["generation"]}
    assert client.get(P, params={"gen": "nope"}).json()["tasks"]


def test_list_limit_and_its_validation(client, agent, projects_dir, state_dir, tmp_path):
    for i in range(4):
        _session(agent, projects_dir, tmp_path, f"sess-{i}", "alpha", [_user(f"m{i}", T9)])
    out = client.get(P, params={"limit": "2"}).json()
    assert len(out["tasks"]) == 2 and out["total"] == 4
    assert client.get(P, params={"limit": "x"}).status_code == 400


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
    assert set(out) == {"task", "turns", "cursor", "first", "live", "run_id", "cards", "card", "scheduled", "ts"}
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
        assert "segments" not in t and "uuid" not in t and "thinking" not in t
    # `since=<cursor>` re-sends the last turn (a reply still streaming grows in place) and nothing before it.
    tail = client.get(f"{P}/sess-a", params={"since": "6"}).json()
    assert [t["i"] for t in tail["turns"]] == [5] and tail["cursor"] == 6 and tail["first"] == 5
    # A window: the last `limit` turns, `first` says where they start.
    win = client.get(f"{P}/sess-a", params={"limit": "2"}).json()
    assert [t["i"] for t in win["turns"]] == [4, 5] and win["first"] == 4
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
    assert [c["verdict"] for c in out["cards"]] == ["allow", "pending", "pending"]
    assert [c["kind"] for c in out["cards"]] == ["permission", "question", "permission"]
    q = out["cards"][1]
    assert q["held"] is True and q["questions"] == [{"header": "Scope", "question": "Which one?", "multiSelect": False,
                                                     "options": [{"label": "A", "description": "the first"}, {"label": "B", "description": ""}]}]
    push = out["cards"][2]
    assert push["detail"] == "git push --force" and push["risk"] == {"severity": "high", "summary": "Rewrites remote history"}
    assert out["cards"][0]["risk"] is None
    # The live card is the OLDEST undecided one — the one the run is blocked on.
    assert out["card"]["request_id"] == "r2"
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
    assert out[1]["text"] == "A " and out[3]["text"] == "B" and out[3]["at"] == 2.0
    tool = out[2]
    assert tool["name"] == "Read" and tool["summary"] == "Read: z.py" and tool["status"] == "ok"
    assert len(tool["detail"]) == phone_tasks.DETAIL_MAX
    assert out[4] == {"i": 4, "kind": "system", "text": "Interrupted", "tone": "interrupted", "at": None}
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
    assert len(s(None, "Bash", {"command": "x" * 500})) <= 130

"""`/api/experts*` through the real server: each route's shape, and the session MCP door."""
from _experts_conftest import *  # noqa: F401,F403
import json
import os
import urllib.error
import urllib.request

import pytest

from fused_render.experts import chat, launch, services, store


@pytest.fixture(autouse=True)
def _idle(monkeypatch):
    monkeypatch.setattr(store, "live_runs", lambda home: [])


def req(client, method, path, body=None, headers=None):
    h = {"Content-Type": "application/json", "X-Fused": "1", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(client.base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_create_list_patch_delete(client, tmp_path):
    st, row = req(client, "POST", "/api/experts", {"name": "Canvas", "about": "Fused canvases",
                                                       "folder": str(tmp_path)})
    assert st == 200, row
    sid = row["id"]
    assert row == {"id": sid, "name": "Canvas", "about": "Fused canvases", "folder": str(tmp_path), "face": None,
                   "home": row["home"], "working": False, "created": row["created"], "last": None, "updating": False, "last_update": None,
                   "new": False, "pinned": False}
    assert os.path.isdir(row["home"]) and row["created"] > 0
    assert req(client, "POST", "/api/experts", {"name": "canvas", "about": "dup"})[0] == 409
    assert req(client, "POST", "/api/experts", {"about": "no name"})[0] == 400
    assert req(client, "POST", "/api/experts", {"name": "x", "about": "y", "model": "opus"})[0] == 400
    assert req(client, "POST", "/api/experts", {"name": "x", "about": "y"}, {"X-Fused": ""})[0] == 403
    assert req(client, "GET", "/api/experts") == (200, [row])
    # Pinned to the top of the Bots list: the row carries it, and it is not a manifest field.
    assert req(client, "POST", f"/api/experts/{sid}/pin", {"pinned": True}, {"X-Fused": ""})[0] == 403
    assert req(client, "POST", f"/api/experts/{sid}/pin", {"pinned": True}) == (200, {"ok": True})
    assert req(client, "GET", "/api/experts")[1][0]["pinned"] is True
    assert req(client, "PATCH", f"/api/experts/{sid}", {"pinned": False})[0] == 400
    assert req(client, "POST", f"/api/experts/{sid}/pin", {"pinned": False}) == (200, {"ok": True})
    st, out = req(client, "PATCH", f"/api/experts/{sid}", {"about": "Canvases", "face": {"shape": "circle"}})
    assert st == 200 and out["about"] == "Canvases" and out["face"] == {"shape": "circle"}
    assert req(client, "PATCH", "/api/experts/nope", {"about": "x"}) == (404, {"error": "no such expert"})
    assert req(client, "DELETE", f"/api/experts/{sid}") == (200, {"ok": True})
    assert req(client, "GET", "/api/experts") == (200, [])


def test_chat_clear_seen_and_the_rows_last_message(client, monkeypatch, tmp_path):
    s = make_expert()
    path = tmp_path / "main.jsonl"
    path.write_text(json.dumps({"type": "assistant", "timestamp": "2020-01-01T10:00:00Z",
                                "message": {"content": [{"type": "text", "text": "Hello."}]}}) + "\n")
    monkeypatch.setattr(chat, "transcript", lambda target, sid: str(path))
    assert req(client, "GET", f"/api/experts/{s.id}/chat") == (
        200, {"session_id": None, "folded": [], "backend": "claude", "backends": ["claude", "copilot"]})
    chat.record_start(s.id, "main", "m1")
    (row,) = req(client, "GET", "/api/experts")[1]
    assert row["last"] == {"text": "Hello.", "at": row["last"]["at"]} and row["last"]["at"] > 0 and not row["new"]
    chat.change(s.home, lambda d: d.update(last_update=1.0))  # a learning run ended
    assert req(client, "GET", "/api/experts")[1][0]["new"] is True
    assert req(client, "POST", f"/api/experts/{s.id}/seen") == (200, {"ok": True})  # the Learning tab opened
    assert req(client, "GET", "/api/experts")[1][0]["new"] is False
    st, out = req(client, "POST", f"/api/experts/{s.id}/chat/clear")
    assert st == 200 and out["session_id"] is None and [f["session_id"] for f in out["folded"]] == ["m1"]
    assert req(client, "GET", f"/api/experts/{s.id}/chat")[1] == out


def test_memory_skills_and_notes(client):
    from fused_render.experts import home as hm
    from fused_render.experts import notes

    s = make_expert()
    hm.write_text(os.path.join(s.home, "memory.md"), "We use parquet.\n")
    hm.write_text(hm.skill_path(s.home, "toml"), hm.skill_text("toml", "Writing canvas.toml", "Body."))
    assert req(client, "GET", f"/api/experts/{s.id}/memory") == (200, {"text": "We use parquet.\n"})
    _, out = req(client, "GET", f"/api/experts/{s.id}/skills")
    assert out["skills"] == [{"name": "toml", "description": "Writing canvas.toml",
                              "text": hm.skill_text("toml", "Writing canvas.toml", "Body."), "body": "Body."}]
    # Settings › Skills: an edit keeps other frontmatter keys; a new one needs a free slug; delete removes the folder.
    hm.write_text(hm.skill_path(s.home, "toml"), "---\nname: toml\ndescription: old\nallowed-tools: Read\n---\n\nBody.\n")
    url = f"/api/experts/{s.id}/skills"
    code, out = req(client, "PUT", f"{url}/toml", {"description": 'When "toml"', "body": "New."})
    assert code == 200 and out["skills"][0]["text"] == "---\nname: toml\ndescription: \"When 'toml'\"\nallowed-tools: Read\n---\n\nNew.\n"
    assert req(client, "PUT", f"{url}/toml", {"description": "d", "body": "", "new": True})[0] == 400
    assert req(client, "PUT", f"{url}/Bad_Name", {"description": "d", "body": "", "new": True})[0] == 400
    assert req(client, "PUT", f"{url}/gone", {"description": "d", "body": ""})[0] == 400
    code, out = req(client, "PUT", f"{url}/layout", {"description": "Laying out", "body": "Steps.", "new": True})
    assert code == 200 and [k["name"] for k in out["skills"]] == ["layout", "toml"] and out["skills"][0]["body"] == "Steps."
    assert req(client, "DELETE", f"{url}/%2e%2e")[0] in (400, 404, 405)
    code, out = req(client, "DELETE", f"{url}/toml")
    assert code == 200 and [k["name"] for k in out["skills"]] == ["layout"]
    name = notes.add(s, "task1", 'Note from task "Sync"', "Use parquet.")
    _, out = req(client, "GET", f"/api/experts/{s.id}/notes")
    assert [(n["name"], n["kind"], n["title"]) for n in out["notes"]] == [(name, "note", "Sync")]
    assert req(client, "DELETE", f"/api/experts/{s.id}/notes/{name}") == (200, {"ok": True})
    assert req(client, "DELETE", f"/api/experts/{s.id}/notes/{name}")[0] == 404


@needs_git
def test_changes_and_a_diff(client):
    from fused_render.experts import home as hm

    s = make_expert()
    assert req(client, "GET", f"/api/experts/{s.id}/changes") == (200, {"changes": []})
    hm.write_text(os.path.join(s.home, "memory.md"), "By hand.\n")
    (row,) = req(client, "GET", f"/api/experts/{s.id}/changes")[1]["changes"]
    assert set(row) == {"sha", "at", "label", "read", "changed"} and row["label"] == "You edited memory"
    st, out = req(client, "GET", f"/api/experts/{s.id}/changes/{row['sha']}")
    assert st == 200 and "+By hand." in out["diff"] and set(out) == {"diff"}
    assert req(client, "GET", f"/api/experts/{s.id}/changes/{'0' * 40}")[0] == 404


def test_nothing_waiting_starts_no_update(client):
    s = make_expert()
    assert req(client, "POST", f"/api/experts/{s.id}/update") == (200, {"started": False})


def test_the_session_door_needs_the_token(client, monkeypatch):
    s = make_expert()
    token = launch.new_token(s.id)
    calls = []
    monkeypatch.setattr(services, "mcp_tools", lambda server: [{"name": f"{server}-tool"}])
    monkeypatch.setattr(services, "call", lambda spec, server, name, args:
                        calls.append((spec.id, server, name, args)) or {"content": [], "isError": False})
    path = f"/api/experts/{s.id}/expert_services/tools"
    assert req(client, "GET", path)[0] == 403
    assert req(client, "GET", path + "?token=" + token) == (200, {"tools": [{"name": "expert_services-tool"}]})
    other = make_expert("Other", "x")
    assert req(client, "GET", f"/api/experts/{other.id}/expert_services/tools?token={token}")[0] == 403
    assert req(client, "GET", f"/api/experts/{s.id}/expert/tools?token={token}")[0] == 400
    body = {"name": "slack_post", "args": {"text": "hi"}}
    assert req(client, "POST", f"/api/experts/{s.id}/expert_services/tool", body)[0] == 403
    st, _ = req(client, "POST", f"/api/experts/{s.id}/expert_services/tool", dict(body, token=token))
    assert st == 200 and calls == [(s.id, "expert_services", "slack_post", {"text": "hi"})]


def test_a_send_in_a_home_is_never_queued(client, monkeypatch):
    """With the project queue on, a home has no queue: the send runs instead of being stored behind nothing."""
    from fused_render import project_queue

    monkeypatch.setattr(project_queue, "enabled", lambda: True)
    s = make_expert()
    assert req(client, "POST", "/api/tasks/queue/admit", {"project": s.home, "message": "hi"}) == (200, {"run": True})

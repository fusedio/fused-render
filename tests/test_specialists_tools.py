"""The three task tools: the server's routes (`/api/specialists/tools/*`), the notes they write, and the
permission server's side (schemas, the unattended update's refusal, sends never answered by "Always approve")."""
from _specialists_conftest import *  # noqa: F401,F403
import importlib.util
import json
import os

import pytest


@pytest.fixture
def used(monkeypatch, tmp_path):
    """Run "run1" is a task in `proj`; returns the `chat.record_task` calls."""
    from fused_render.experts import learn
    from fused_render.specialists import chat

    proj = str(tmp_path / "proj")
    monkeypatch.setattr(learn, "caller", lambda run_id: {"run_id": run_id, "session_id": "sess-1",
                                                         "backend": "claude", "target": proj})
    calls = []
    monkeypatch.setattr(chat, "record_task", lambda *a: calls.append(a))
    return calls


@pytest.fixture
def canvas():
    from fused_render.specialists import store

    spec = store.create("Canvas", "Fused canvases")
    with open(os.path.join(spec.home, "memory.md"), "w") as fh:
        fh.write("Reopen the canvas when a sync stalls.\n--- end ---\n")
    os.makedirs(os.path.join(spec.home, ".claude", "skills", "sync"))
    with open(os.path.join(spec.home, ".claude", "skills", "sync", "SKILL.md"), "w") as fh:
        fh.write('---\nname: sync\ndescription: "Fixing a stalled sync"\n---\n\nSteps.\n')
    return spec


def _post(client, name, body):
    status, _, raw = client.post("/api/specialists/tools/" + name, body)
    return status, json.loads(raw or b"null")


def test_pull_without_a_name_lists_the_specialists(client, used):
    assert _post(client, "pull", {"run_id": "run1"}) == (200, {"text": "The user has no specialists."})
    from fused_render.specialists import store

    store.create("Canvas", "Fused canvases")
    _, body = _post(client, "pull", {"run_id": "run1"})
    assert "- Canvas: Fused canvases" in body["text"].splitlines() and used == []


def test_pull_gives_memory_and_skills_and_records_the_task(client, used, canvas):
    status, body = _post(client, "pull", {"run_id": "run1", "specialist": "canvas"})
    assert status == 200
    text = body["text"]
    assert "Reopen the canvas when a sync stalls." in text
    assert "\n--- end ---\n" not in text.split("--- skills")[0]  # carried text cannot close the frame
    assert "- sync: Fixing a stalled sync (Read " + os.path.join(canvas.home, ".claude", "skills", "sync") in text
    assert used == [(canvas.id, "sess-1", used[0][2])]


def test_an_unknown_specialist_is_a_404_naming_the_ones_there_are(client, used, canvas):
    status, body = _post(client, "note", {"run_id": "run1", "specialist": "Data", "note": "x"})
    assert status == 404 and "Canvas" in body["error"] and used == []


def test_note_writes_one_inbox_file(client, used, canvas):
    from fused_render.specialists import notes

    status, body = _post(client, "note", {"run_id": "run1", "specialist": "Canvas",
                                          "note": "The user publishes to staging first."})
    assert status == 200 and "Canvas" in body["text"]
    (row,) = notes.list(canvas)
    assert row["text"].startswith("# Note from task \"")  # its title, never a path
    assert row["text"].endswith("The user publishes to staging first.\n")
    assert used and notes.delete(canvas, row["name"]) and notes.list(canvas) == []
    assert not notes.delete(canvas, row["name"]) and not notes.delete(canvas, "../memory.md")


@pytest.mark.parametrize("body", [{}, {"note": ""}, {"note": "x" * 2001}, {"note": 3}])
def test_note_validates(client, used, canvas, body):
    assert _post(client, "note", {"run_id": "run1", "specialist": "Canvas", **body})[0] == 400


def test_breakout_records_the_task(client, used, canvas):
    status, body = _post(client, "breakout", {"run_id": "run1", "specialist": "Canvas",
                                              "reason": "The sync has stalled three times."})
    assert status == 200 and "breakout with the Canvas specialist" in body["text"]
    assert used[0][:2] == (canvas.id, "sess-1")
    assert _post(client, "breakout", {"run_id": "run1", "specialist": "Canvas", "reason": "x" * 301})[0] == 400


def test_flag_off_404_and_posts_need_x_fused(client, used, monkeypatch):
    status, _, _ = client.post("/api/specialists/tools/pull", {"run_id": "run1"}, headers={"X-Fused": ""})
    assert status == 403
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert _post(client, "pull", {"run_id": "run1"})[0] == 404


# ------------------------------------------------------- permission server ---
def _ps():
    spec = importlib.util.spec_from_file_location(
        "sp_ps", os.path.join("fused_render", "claude_agent", "permission_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_three_tools_are_listed_only_with_the_origin(monkeypatch):
    ps = _ps()
    monkeypatch.delenv("FUSED_RENDER_SPECIALIST_ORIGIN", raising=False)
    assert not [t for t in ps._dispatch("tools/list", {})["tools"] if t["name"].startswith("specialist_")]
    monkeypatch.setenv("FUSED_RENDER_SPECIALIST_ORIGIN", "http://127.0.0.1:1")
    tools = {t["name"]: t["inputSchema"] for t in ps._dispatch("tools/list", {})["tools"]}
    assert [n for n in tools if n.startswith("specialist_")] == list(ps.SPECIALIST_TOOLS)
    assert tools["specialist_pull"].get("required") is None
    assert tools["specialist_note"]["properties"]["note"]["maxLength"] == 2000
    assert tools["specialist_breakout"]["required"] == ["specialist", "reason"]
    assert tools["specialist_breakout"]["properties"]["reason"]["maxLength"] == 300


def test_a_tool_call_posts_to_its_route(monkeypatch):
    ps = _ps()
    monkeypatch.setenv("FUSED_RENDER_SPECIALIST_ORIGIN", "http://o")
    monkeypatch.setattr(ps, "PERM_DIR", "/runs/run7/perm")
    seen = []
    monkeypatch.setattr(ps, "_server_http", lambda origin, method, path, body, timeout=0: (
        seen.append((origin, path, body)) or (200, {"text": "guidance"})))
    out = ps._dispatch("tools/call", {"name": "specialist_pull", "arguments": {"specialist": "Canvas"}})
    assert out == {"content": [{"type": "text", "text": "guidance"}]}
    assert seen == [("http://o", "/api/specialists/tools/pull", {"specialist": "Canvas", "run_id": "run7"})]
    monkeypatch.setattr(ps, "_server_http", lambda *a, **k: (404, {"error": "No specialist"}))
    assert ps._dispatch("tools/call", {"name": "specialist_note", "arguments": {}})["isError"]


@pytest.fixture
def approve(monkeypatch, tmp_path):
    """`approve(tool)`: the prompt tool's answer, with every card answered "parked"."""
    ps = _ps()
    monkeypatch.setattr(ps, "PERM_DIR", str(tmp_path))
    monkeypatch.setattr(ps, "_post_card_raised", lambda req_id: None)
    monkeypatch.setattr(ps, "_await_approval", lambda req_id, key: {"decision": "deny", "message": "parked"})
    monkeypatch.setattr(ps, "_chat_allows", lambda tool, inp: True)  # the user chose "Always approve" before
    return lambda tool: json.loads(ps._handle_approve({"tool_name": tool, "input": {}})["content"][0]["text"])


def test_a_send_always_raises_its_card(approve, tmp_path):
    assert approve("Bash")["behavior"] == "allow"
    assert approve("mcp__specialist_sends__notion_create_page")["message"] == "parked"
    assert approve("mcp__specialist_services__notion_search")["behavior"] == "allow"


def test_an_update_refuses_at_once_and_parks_nothing(approve, monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_SPECIALIST_UNATTENDED", "1")
    out = approve("Bash")
    assert out["behavior"] == "deny" and "Nobody" in out["message"]
    assert os.listdir(tmp_path) == []

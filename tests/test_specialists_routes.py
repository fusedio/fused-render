"""`/api/specialists*` core routes through the real server (harness stubbed), and the session MCP pipe."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from fused_render.specialists import chat, harness, launch, store


def j(resp):
    status, _headers, body = resp
    return status, json.loads(body or b"{}")


def req(client, method, path, body=None):
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(client.base + path, data=data, method=method,
                               headers={"Content-Type": "application/json", "X-Fused": "1"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_create_list_get_patch_delete(client):
    st, out = j(client.post("/api/specialists", {"name": "Canvas", "subject": "Fused canvases",
                                                 "triggers": {"folders": [], "keywords": ["canvas"]}}))
    assert st == 200, out
    sid = out["id"]
    assert out["name"] == "Canvas" and out["triggers"]["keywords"] == ["canvas"] and os.path.isdir(out["home"])
    st, out = j(client.post("/api/specialists", {"name": "canvas", "subject": "dup"}))
    assert st == 409
    st, out = j(client.post("/api/specialists", {"subject": "no name"}))
    assert st == 400
    st, out = j(client.post("/api/specialists", {"name": "x"}, headers={"X-Fused": ""}))
    assert st == 403
    st, rows = j(client.get("/api/specialists"))
    assert st == 200 and rows == [{"id": sid, "name": "Canvas", "subject": "Fused canvases", "face": "",
                                   "model": "", "status": "Ready", "working": False}]
    st, out = req(client, "PATCH", f"/api/specialists/{sid}", {"subject": "Canvases", "effort": "high",
                                                               "triggers": {"keywords": ["toml"]}})
    assert st == 200 and out["subject"] == "Canvases" and out["triggers"]["keywords"] == ["toml"]
    st, out = req(client, "PATCH", f"/api/specialists/{sid}", {"color": "red"})
    assert st == 400
    st, out = j(client.get(f"/api/specialists/{sid}/history"))
    assert st == 200 and out["commits"][0]["message"].startswith("settings")
    st, out = j(client.get("/api/specialists/nope"))
    assert st == 404 and out == {"error": "no such specialist"}
    st, out = req(client, "DELETE", f"/api/specialists/{sid}")
    assert st == 200 and out == {"ok": True}
    assert j(client.get("/api/specialists"))[1] == []


def test_charter_skills_people(client):
    s = make_specialist()
    base = f"/api/specialists/{s.id}"
    st, out = j(client.get(base + "/charter"))
    assert st == 200 and out["text"].endswith("@said.md\n")
    st, out = req(client, "PUT", base + "/charter", {"text": "# Mine\nBe brief.\n"})
    assert st == 200 and out["text"] == "# Mine\nBe brief.\n<!-- fused:imports -->\n@memory.md\n@said.md\n"
    assert req(client, "PUT", base + "/charter", {"text": " "})[0] == 400
    st, out = req(client, "PUT", base + "/skills/publish-flow", {"text": "# Publishing\nSteps..."})
    assert st == 200 and out["text"].startswith('---\nname: publish-flow\ndescription: "Publishing"')
    st, out = j(client.get(base + "/skills"))
    assert [k["name"] for k in out["skills"]] == ["publish-flow"] and out["skills"][0]["description"] == "Publishing"
    assert j(client.get(base + "/skills/publish-flow"))[1]["name"] == "publish-flow"
    assert req(client, "PUT", base + "/skills/Bad_Name", {"text": "x"})[0] == 400
    assert req(client, "DELETE", base + "/skills/publish-flow")[0] == 200
    assert j(client.get(base + "/skills/publish-flow"))[0] == 404
    rows = [{"area": "Sync", "owner": "Ana", "service": "slack", "handle": "#sync", "notes": "a|b"}]
    st, out = req(client, "PUT", base + "/people", {"rows": rows})
    assert st == 200 and out["rows"] == [{**rows[0], "notes": "a/b"}]
    assert j(client.get(base + "/people"))[1]["rows"] == out["rows"]


def test_chat_and_clear(client):
    s = make_specialist()
    chat.record_start(s.id, "sess-1")
    assert j(client.get(f"/api/specialists/{s.id}/chat"))[1]["current"] == "sess-1"
    st, out = j(client.post(f"/api/specialists/{s.id}/chat/clear", {}))
    assert st == 200 and out["current"] is None and out["folded"][0]["session_id"] == "sess-1"


def test_runs_routes(client, monkeypatch):
    s = make_specialist()
    stub_side(monkeypatch, answer="fine")
    row = harness.run_side(s, "check", "x", trigger={})
    st, out = j(client.get(f"/api/specialists/{s.id}/runs"))
    assert st == 200 and out["runs"][0]["id"] == row["id"] and "transcript" not in out["runs"][0]
    st, out = j(client.get(f"/api/specialists/{s.id}/runs/{row['id']}"))
    assert st == 200 and out["answer"] == "fine" and out["transcript"]
    assert j(client.get(f"/api/specialists/{s.id}/runs/nope"))[0] == 404


@pytest.fixture
def fake_roster(monkeypatch):
    from fused_render.bots import apptools

    recs = [apptools.ToolRec(app="notion", app_dir="connect:notion", name="search", description="Search.",
                             spec=SimpleNamespace(read_only=True, input_schema={"type": "object"})),
            apptools.ToolRec(app="notion", app_dir="connect:notion", name="create_page", description="Create.",
                             spec=SimpleNamespace(read_only=False, input_schema={"type": "object"}))]
    monkeypatch.setattr(apptools, "registry", lambda force=False: recs)
    monkeypatch.setattr(apptools, "run_tool", lambda rec, args, timeout_s=0: apptools.RunResult(True, "R:" + rec.name))
    return recs


def test_policy_and_connections_routes(client, fake_roster):
    s = make_specialist()
    q = f"/api/specialists/policy?specialist={s.id}&kind=main&session_id=x&tool=mcp__specialist_services__notion_search"
    assert j(client.get(q)) == (200, {"answer": "ask_always"})
    st, out = req(client, "PUT", f"/api/specialists/{s.id}/connections", {"add": ["notion"]})
    rows = {r["key"]: r for r in out["services"]}
    assert st == 200 and rows["notion"]["allowed"] is True
    assert {"name": "mcp__specialist_services__notion_create_page", "write": True} in rows["notion"]["tools"]
    assert j(client.get(q)) == (200, {"answer": "allow"})
    bq = q.replace("kind=main", "kind=breakout").replace("notion_search", "notion_create_page")
    assert j(client.get(bq)) == (200, {"answer": "ask_always"})


def _service_card(run_id, spec_id, kind, session_id, tool, bid=""):
    """A run dir as agent._start leaves it for a specialist session, with one parked card for `tool`."""
    from fused_render.claude_agent import agent_module

    run_dir = os.path.join(agent_module().RUNS, run_id)
    os.makedirs(os.path.join(run_dir, "perm"), exist_ok=True)
    with open(os.path.join(run_dir, "meta.json"), "w") as f:
        json.dump({"session_id": session_id, "specialist": {"id": spec_id, "kind": kind, "breakout_id": bid}}, f)
    with open(os.path.join(run_dir, "perm", "req1.req.json"), "w") as f:
        json.dump({"id": "req1", "tool": tool, "input": {}}, f)
    return run_dir


def test_a_session_alone_cannot_make_a_service_call_run(client, fake_roster, monkeypatch):
    """QA (privileges) defect 2 / CONTRACT C2: the session's token, with everything its run dir holds (the launch
    spec as `specialist.json`, meta), cannot record a grant; only the user's `decide allow` through the agent
    router does, and that grant is spent once by that session only."""
    from fused_render.claude_agent import agent_module
    import fused_render.specialists.agent_hooks  # noqa: F401 — loaded at app start (server/app.py)

    s = make_specialist()  # notion not allowed: every call needs a card
    chat.record_start(s.id, "ms-1")
    sp = launch.spec(f"{s.id}:main", s.home)
    dump = json.dumps(sp)
    assert "GRANT" not in dump and "secret" not in dump and set(sp["policy_env"]) == {"FUSED_SPECIALIST_POLICY"}
    assert set(launch.check_token(s.id, "main", sp["token"])) == {"spec", "kind", "breakout_id", "exp"}
    tool = "mcp__specialist_services__notion_create_page"
    run_dir = _service_card("run-g1", s.id, "main", "ms-1", tool)
    with open(os.path.join(run_dir, "specialist.json"), "w") as f:
        f.write(dump)
    base = f"/api/specialists/{s.id}"
    H = {"X-Specialist-Token": sp["token"]}
    call = {"kind": "main", "server": "specialist_services", "name": "notion_create_page", "args": {},
            "session_id": "ms-1"}
    needs = {"content": [{"type": "text", "text": "This call needs the user's approval card."}], "isError": True}
    assert j(client.post(f"{base}/session-tool", call, headers=H)) == (200, needs)
    # the old grant door is gone, whatever the session sends it
    for body in ({}, {"specialist": s.id, "kind": "main", "session_id": "ms-1", "tool": tool, "secret": sp["token"]}):
        assert j(client.post("/api/specialists/policy/grant", body))[0] in (404, 405)
    assert j(client.post(f"{base}/session-tool", call, headers=H)) == (200, needs)
    # the user allows the card: the decide passes through the agent router, which records the grant
    agent = agent_module()
    monkeypatch.setattr(agent, "_alive", lambda run_dir: True)
    st, out = j(client.post("/api/claude/agent", {"action": "decide", "run_id": "run-g1", "request_id": "req1",
                                                  "decision": "allow", "scope": "once"}))
    assert st == 200 and out.get("decision") == "allow", out
    other = launch.new_token(s.id, "breakout", "b-000000000001")  # another session of the specialist
    assert j(client.post(f"{base}/session-tool", {**call, "kind": "breakout"},
                         headers={"X-Specialist-Token": other}))[1]["isError"]
    st, out = j(client.post(f"{base}/session-tool", call, headers=H))
    assert out == {"content": [{"type": "text", "text": "R:create_page"}], "isError": False}
    assert j(client.post(f"{base}/session-tool", call, headers=H)) == (200, needs)  # spent
    # a deny records nothing
    _service_card("run-g2", s.id, "main", "ms-1", tool)
    j(client.post("/api/claude/agent", {"action": "decide", "run_id": "run-g2", "request_id": "req1",
                                       "decision": "deny", "scope": "once"}))
    assert j(client.post(f"{base}/session-tool", call, headers=H)) == (200, needs)


def test_a_grant_is_recorded_once_per_card_and_only_for_service_tools(client, fake_roster):
    from fused_render.specialists import agent_hooks

    s = make_specialist()
    _service_card("run-g3", s.id, "breakout", "bs-1", "mcp__specialist_services__notion_create_page", "b-1")
    assert agent_hooks.grant_from_decision("run-g3", "req1")
    assert agent_hooks.grant_from_decision("run-g3", "req1")  # repeated by the post-hook: still one grant
    assert launch.take_grant(s.id, "breakout", "b-1", "bs-1", "notion_create_page")
    assert not launch.take_grant(s.id, "breakout", "b-1", "bs-1", "notion_create_page")
    assert agent_hooks.grant_from_decision("run-g3", "req1")  # a late repeat after the spend grants nothing
    assert not launch.take_grant(s.id, "breakout", "b-1", "bs-1", "notion_create_page")
    _service_card("run-g4", s.id, "main", "ms-1", "Bash")
    assert not agent_hooks.grant_from_decision("run-g4", "req1")
    assert not agent_hooks.grant_from_decision("../x", "req1")


def test_session_tool_door(client, fake_roster):
    s = make_specialist()
    tok = launch.new_token(s.id, "main")
    services_mod = __import__("fused_render.specialists.services", fromlist=["x"])
    store.update(s.id, connections=["notion"])
    base = f"/api/specialists/{s.id}"
    H = {"X-Specialist-Token": tok}
    st, out = j(client.get(f"{base}/session-tools?kind=main&server=specialist_services", headers=H))
    assert st == 200 and [t["name"] for t in out["tools"]] == ["notion_search", "notion_create_page"]
    st, out = j(client.get(f"{base}/session-tools?kind=main&server=specialist", headers=H))
    assert [t["name"] for t in out["tools"]] == ["note"]
    assert j(client.get(f"{base}/session-tools?kind=breakout&server=specialist", headers=H))[0] == 403
    assert j(client.get(f"{base}/session-tools?kind=main&server=specialist",
                        headers={"X-Specialist-Token": "bad"}))[0] == 403
    # the token is no longer accepted in the query string
    assert j(client.get(f"{base}/session-tools?kind=main&token={tok}&server=specialist"))[0] == 403
    st, out = j(client.post(f"{base}/session-tool", {"kind": "main", "server": "specialist_services",
                                                     "name": "notion_create_page", "args": {}}, headers=H))
    assert st == 200 and out == {"content": [{"type": "text", "text": "R:create_page"}], "isError": False}
    st, out = j(client.post(f"{base}/session-tool", {"kind": "main", "server": "specialist",
                                                     "name": "note", "args": {"kind": "nope"}}, headers=H))
    assert st == 200 and out["isError"] and services_mod.NEEDS_CARD


def test_note_tool_reaches_the_inbox(client, monkeypatch):
    s = make_specialist()
    from fused_render.specialists import inbox

    got = []
    monkeypatch.setattr(inbox, "add", lambda spec_id, **kw: got.append((spec_id, kw)) or {"id": "n-123"},
                        raising=False)
    chat.record_start(s.id, "main-sess")
    tok = launch.new_token(s.id, "main")
    st, out = j(client.post(f"/api/specialists/{s.id}/session-tool", {
        "kind": "main", "server": "specialist", "name": "note",
        "args": {"kind": "convention", "claim": "Publish to staging first.", "basis": "user-said"}},
        headers={"X-Specialist-Token": tok}))
    assert st == 200 and not out["isError"] and "n-123" in out["content"][0]["text"]
    assert got[0][0] == s.id and got[0][1]["source"] == {"type": "main", "session_id": "main-sess", "breakout_id": ""}


def test_mutating_routes_need_x_fused(client):
    s = make_specialist()
    base = f"/api/specialists/{s.id}"
    for method, path, body in (("PATCH", base, {"subject": "x"}), ("DELETE", base, None),
                               ("PUT", base + "/charter", {"text": "x"}), ("PUT", base + "/skills/a", {"text": "x"}),
                               ("DELETE", base + "/skills/a", None), ("PUT", base + "/people", {"rows": []}),
                               ("PUT", base + "/connections", {"add": []}), ("POST", base + "/chat/clear", {}),
                               ("POST", "/api/specialists", {"name": "y"})):
        import urllib.error
        import urllib.request

        r = urllib.request.Request(client.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   method=method, headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(r)
        assert e.value.code == 403, (method, path)
    assert store.get(s.id).subject == s.subject


def test_delete_is_409_while_the_nightly_job_runs(client, monkeypatch):
    from fused_render.specialists import nightly

    s = make_specialist()
    monkeypatch.setattr(nightly, "running", lambda spec_id: True)
    st, out = req(client, "DELETE", f"/api/specialists/{s.id}")
    assert st == 409 and "nightly update is running" in out["error"]


def test_sessmcp_pipe_against_the_server(client, fake_roster):
    """The stdio server the CLI spawns: initialize, tools/list, tools/call through the real routes."""
    s = make_specialist()
    tok = launch.new_token(s.id, "main")
    script = os.path.join(os.path.dirname(launch.__file__), "sessmcp.py")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "notion_search", "arguments": {}}}]
    out = {}
    store.update(s.id, connections=["notion"])
    env = {**os.environ, "FUSED_SPECIALIST_TOKEN": tok}
    proc = subprocess.Popen([sys.executable, "-I", script, client.base, s.id, "main", "-", "specialist_services"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env)
    try:
        for m in msgs:
            proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()
            line = json.loads(proc.stdout.readline())
            out[line["id"]] = line
    finally:
        proc.stdin.close()
        proc.wait(10)
    assert out[1]["result"]["serverInfo"]["name"] == "specialist_services"
    assert [t["name"] for t in out[2]["result"]["tools"]] == ["notion_search", "notion_create_page"]
    assert out[3]["result"] == {"content": [{"type": "text", "text": "R:search"}], "isError": False}


def test_working_while_the_main_chats_turn_is_live(client, monkeypatch):
    """C5: `working` is true from the main chat's prompt hook to its Stop (and while a run's turn is open in the
    home); a breakout's live turn, in its workroom, does not make the main chat look busy."""
    from fused_render.specialists import history

    s = make_specialist()
    rows = lambda: j(client.get("/api/specialists"))[1]  # noqa: E731
    assert rows()[0]["working"] is False and rows()[0]["model"] == ""
    history.before_turn(s.id, "main", "ms-1")
    assert rows()[0]["working"] is True and rows()[0]["status"] == "Working now"
    assert j(client.get(f"/api/specialists/{s.id}"))[1]["working"] is True
    history.after_turn(s.id, "main", "ms-1")
    assert rows()[0]["working"] is False
    monkeypatch.setattr(store, "main_live_runs", lambda home: [{"run_id": "r", "sessions": {"ms-1"}}])
    assert rows()[0]["working"] is True

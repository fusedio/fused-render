"""The `canvas_screenshot` MCP tool: the model's own eyes on a canvas.

Same file round trip as `app_state` (tests/test_claude_app_state.py), one
directory over: the tool parks a request in `canvasshot/`, the canvas page
uploads the picture into the shots dir and answers with its path, the tool
returns the bytes as an image block. Offered only to a session whose target
sits in a canvas clone, and pre-allowed there (a read of the user's own
screen for the agent they are talking to).
"""
import base64
import importlib.util
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _mcp_stdio import MCPServer  # noqa: E402

AGENT_DIR = os.path.join("fused_render", "claude_agent")
SERVER = os.path.join(AGENT_DIR, "permission_server.py")

# A 1×1 PNG: real bytes, so the tool's read-back and base64 are exercised.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def _load(name):
    path = os.path.join(AGENT_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "canvasshot").mkdir(parents=True)
    return d


def _server(run_dir, canvas=True):
    args = [sys.executable, os.path.abspath(SERVER), os.path.join(str(run_dir), "perm")]
    if canvas:
        args.append("--canvas-shots=" + os.path.join(str(run_dir), "canvasshot"))
    s = MCPServer(args)
    s.initialize()
    return s


@pytest.fixture
def server(run_dir):
    s = _server(run_dir)
    yield s
    s.close()


def _wait_for_request(req_dir, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = [n for n in os.listdir(req_dir) if n.endswith(".req.json")]
        if names:
            with open(os.path.join(req_dir, names[0]), encoding="utf-8") as fh:
                return json.load(fh)
        time.sleep(0.05)
    raise AssertionError("no request was ever parked in %s" % req_dir)


# ------------------------------------------------------------------ the server

def test_the_tool_rides_the_canvas_flag_and_the_flag_alone(run_dir):
    """The roster follows the channel: with `--canvas-shots=` the tool is
    offered and dispatched; without it, neither — exactly `app_state`'s rule
    with STATE_DIR. The flag is one token, so the perm dir keeps its place."""
    with_canvas = _server(run_dir, canvas=True)
    try:
        names = [t["name"] for t in with_canvas.call("tools/list")["result"]["tools"]]
        assert names == ["approve", "canvas_screenshot"]
        schema = next(t for t in with_canvas.call("tools/list")["result"]["tools"]
                      if t["name"] == "canvas_screenshot")["inputSchema"]
        assert schema.get("required", []) == []          # never blocked on phrasing
        assert schema["properties"]["what"]["enum"] == ["canvas", "udf"]
    finally:
        with_canvas.close()
    without = _server(run_dir, canvas=False)
    try:
        names = [t["name"] for t in without.call("tools/list")["result"]["tools"]]
        assert names == ["approve"]
        err = without.call("tools/call", {"name": "canvas_screenshot",
                                          "arguments": {}})["error"]
        assert "unknown tool" in err["message"]
    finally:
        without.close()


def test_a_canvas_screenshot_is_answered_by_the_page_with_a_picture(
        run_dir, server, agent, tmp_path):
    """The round trip: the tool parks {what, udf}, the page uploads the picture
    into the shots dir and answers with its path, and the model gets the bytes
    as an IMAGE block plus a text block naming what it is looking at."""
    agent.RUNS = str(run_dir.parent)
    agent.SHOTS = str(tmp_path / "shots")
    os.makedirs(agent.SHOTS)
    pending = server.send_async("tools/call", {
        "name": "canvas_screenshot",
        "arguments": {"what": "udf", "udf": "udf_1", "reason": "after my edit"}})
    req = _wait_for_request(run_dir / "canvasshot")
    assert (req["what"], req["udf"], req["reason"]) == ("udf", "udf_1", "after my edit")
    # Nothing landed in the approvals directory: a picture is not a card.
    assert os.listdir(run_dir / "perm") == []

    image = os.path.join(agent.SHOTS, "shot-udf.png")
    with open(image, "wb") as fh:
        fh.write(PNG)
    answered = agent.main(action="canvas_shot", run_id="run", request_id=req["id"],
                          state=json.dumps({"image": image, "mime": "image/png",
                                            "udf": "udf_1"}))
    assert answered == {"answered": req["id"]}

    content = pending.result(10)["result"]["content"]
    assert [c["type"] for c in content] == ["image", "text"]
    assert content[0]["mimeType"] == "image/png"
    assert base64.b64decode(content[0]["data"]) == PNG
    note = json.loads(content[1]["text"])
    assert note["what"] == "udf" and note["udf"] == "udf_1" and note["path"] == image


def test_a_refusal_from_the_page_reaches_the_model_as_a_sentence(
        run_dir, server, agent):
    """No picture is a text block with `error` — never a JSON-RPC error, which
    would read as a broken tool rather than a fact to act on."""
    agent.RUNS = str(run_dir.parent)
    pending = server.send_async("tools/call", {
        "name": "canvas_screenshot", "arguments": {"what": "canvas"}})
    req = _wait_for_request(run_dir / "canvasshot")
    agent.main(action="canvas_shot", run_id="run", request_id=req["id"],
               state=json.dumps({"error": "no canvas page is open"}))
    content = pending.result(10)["result"]["content"]
    assert len(content) == 1 and content[0]["type"] == "text"
    assert json.loads(content[0]["text"]) == {"error": "no canvas page is open"}


# -------------------------------------------------------------- the page side

def test_the_picture_must_live_in_the_shots_dir(agent, run_dir, tmp_path):
    """The page names a path and the tool READS that path and hands the bytes
    to the model: an answer pointing outside the one directory pictures are
    uploaded to is refused, so a run can never be made to read some other
    file as 'a screenshot'."""
    agent.RUNS = str(run_dir.parent)
    agent.SHOTS = str(tmp_path / "shots")
    os.makedirs(agent.SHOTS)
    (run_dir / "canvasshot" / "r1.req.json").write_text(
        json.dumps({"id": "r1", "what": "canvas", "created_at": 1}), encoding="utf-8")
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    out = agent.main(action="canvas_shot", run_id="run", request_id="r1",
                     state=json.dumps({"image": str(secret), "mime": "image/png"}))
    assert out["error"] and out["retry"] is False
    # Unanswered still: the request stays on the poll for the page to retry.
    assert [r["id"] for r in agent._canvas_shot_requests(str(run_dir))] == ["r1"]


def test_poll_rows_name_what_the_model_asked_for(agent, run_dir):
    """The page's poll loop is the only thing that can answer, so the request
    rides the poll payload with `what` and `udf` — the page needs both to
    capture the right thing."""
    (run_dir / "canvasshot" / "r1.req.json").write_text(
        json.dumps({"id": "r1", "what": "udf", "udf": "udf_1", "reason": "", "created_at": 5}),
        encoding="utf-8")
    (run_dir / "canvasshot" / "bad.req.json").write_text("{not json", encoding="utf-8")
    rows = agent._canvas_shot_requests(str(run_dir))
    assert rows == [{"id": "r1", "what": "udf", "udf": "udf_1", "reason": "", "created_at": 5}]


def test_a_finished_run_releases_a_parked_request(agent, run_dir):
    (run_dir / "canvasshot" / "r1.req.json").write_text(
        json.dumps({"id": "r1", "what": "canvas", "created_at": 1}), encoding="utf-8")
    agent._expire_canvas_shot(str(run_dir), "the run finished")
    assert agent._canvas_shot_requests(str(run_dir)) == []
    answer = agent._read_decision(str(run_dir / "canvasshot"), "r1")
    assert "error" in answer and "finished" in answer["error"]


def test_the_mcp_config_carries_the_channel_only_for_a_canvas_target(
        agent, tmp_path, monkeypatch):
    """`_write_mcp_config(canvas=...)` is the one switch: the flag in argv is
    the roster entry (see the server test above)."""
    agent.RUNS = str(tmp_path / "runs")
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    with open(agent._write_mcp_config(str(run_dir), pane=False, canvas=True),
              encoding="utf-8") as fh:
        args = json.load(fh)["mcpServers"][agent.PERMISSION_SERVER]["args"]
    assert args[-1] == "--canvas-shots=" + os.path.join(str(run_dir), "canvasshot")
    with open(agent._write_mcp_config(str(run_dir), pane=False, canvas=False),
              encoding="utf-8") as fh:
        args = json.load(fh)["mcpServers"][agent.PERMISSION_SERVER]["args"]
    assert not any(a.startswith("--canvas-shots=") for a in args)


def test_a_canvas_target_is_told_about_the_tool_and_a_plain_one_is_not(
        agent, tmp_path, monkeypatch):
    """An un-announced tool does not get called (D235): the prompt names it,
    and only where the roster carries it."""
    root = tmp_path / "canvas"
    clone = root / "unstable" / "demo"
    clone.mkdir(parents=True)
    (clone / "udf_1.py").write_text("def udf(): pass\n", encoding="utf-8")
    monkeypatch.setattr(agent, "_canvases_root", lambda: str(root))
    tool = "mcp__%s__%s" % (agent.PERMISSION_SERVER, agent.CANVAS_SHOT_TOOL)
    assert tool in agent._canvas_shot_note(str(clone / "udf_1.py"))
    assert agent._canvas_shot_note(str(tmp_path / "elsewhere.py")) == ""


def test_a_canvas_session_gets_an_mcp_ceiling_above_the_screenshot_wait(
        agent, tmp_path):
    """The CLI's per-call MCP timeout must outlast the tool's own wait, or the
    model sees an MCP-timeout error instead of the tool's "open the canvas"
    sentence (CodeRabbit on #1924). A plain session keeps the old ceiling."""
    agent.RUNS = str(tmp_path / "runs")
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    def ceiling(canvas):
        with open(agent._write_mcp_config(str(run_dir), pane=False, canvas=canvas),
                  encoding="utf-8") as fh:
            return json.load(fh)["mcpServers"][agent.PERMISSION_SERVER]["timeout"]
    assert ceiling(canvas=True) >= (agent.CANVAS_SHOT_WAIT + 60) * 1000
    assert ceiling(canvas=False) == (agent.PERMISSION_WAIT + 60) * 1000


# ------------------------------------------- D1471: the page answers, any chat

def _canvas_root(tmp_path, monkeypatch, agent, nodes=("udf_1", "udf_2")):
    root = tmp_path / "canvas"
    clone = root / "unstable" / "demo"
    clone.mkdir(parents=True)
    for n in nodes:
        (clone / (n + ".py")).write_text("def udf(): pass\n", encoding="utf-8")
    (clone / "canvas.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(agent, "_canvases_root", lambda: str(root))
    return root, clone


def test_only_a_fused_render_clone_gets_the_tool(agent, tmp_path, monkeypatch):
    """F7: `~/Fused/canvas/<name>` (Fused Web's own clone, no env level) has
    no canvas page that could answer, so no tool, no note, no pre-allow."""
    root, clone = _canvas_root(tmp_path, monkeypatch, agent)
    web = root / "webcanvas"
    web.mkdir()
    (web / "a.py").write_text("", encoding="utf-8")
    got = agent._canvas_of(str(clone / "udf_1.py"))
    assert got == {"env": "unstable", "name": "demo", "node": "udf_1",
                   "clone": os.path.join(os.path.realpath(str(root)), "unstable", "demo")}
    assert agent._canvas_of(str(clone))["node"] == ""
    assert agent._canvas_of(str(web / "a.py")) is None
    assert agent._in_workbench_root(str(web / "a.py"))     # skills stay
    assert agent._canvas_shot_note(str(web / "a.py")) == ""
    assert agent._canvas_of(str(tmp_path / "elsewhere.py")) is None


def test_the_duplicated_canvas_facts_match_their_sources(agent):
    from fused_render import canvases, fusedcli
    ps = _load("permission_server")
    assert agent._CANVAS_ENVS == fusedcli.WORKBENCH_ENV_CHOICES
    assert agent._CANVAS_NAME_RE.pattern == canvases._NAME_RE.pattern
    assert agent.CANVAS_OPEN_TOOL == ps.CANVAS_OPEN_TOOL


def test_a_canvas_session_names_its_canvas_and_pages_to_the_server(
        agent, tmp_path, monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    canvas = {"env": "prod", "name": "demo", "clone": "/x/prod/demo", "node": "udf_1"}
    with open(agent._write_mcp_config(str(run_dir), pane=False, canvas=canvas),
              encoding="utf-8") as fh:
        server = json.load(fh)["mcpServers"][agent.PERMISSION_SERVER]
    assert server["env"]["FUSED_RENDER_CANVAS_CLONE"] == "/x/prod/demo"
    assert server["env"]["FUSED_RENDER_CANVAS_NODE"] == "udf_1"
    assert server["env"]["FUSED_RENDER_CANVAS_PAGES"] == agent._canvas_pages_dir()
    # The ask, then the page: the ceiling covers both waits.
    assert server["timeout"] >= (2 * agent.CANVAS_SHOT_WAIT + 60) * 1000


def test_codex_gets_a_tool_timeout_above_the_screenshot_wait(
        agent, tmp_path, monkeypatch):
    """F8: Codex's default MCP call ceiling is 60 s, under the shot's wait."""
    _root, clone = _canvas_root(tmp_path, monkeypatch, agent)
    b = tmp_path / "bin" / "codex"
    b.parent.mkdir()
    b.write_text("#!/bin/sh\nexit 0\n")
    b.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_CODEX_BIN", str(b))
    agent.RUNS = str(tmp_path / "runs")
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    argv = agent._codex_argv(str(run_dir), False, None, "", "", "", [],
                             str(clone / "udf_1.py"))
    want = "mcp_servers.%s.tool_timeout_sec=%.1f" % (
        agent.PERMISSION_SERVER, 2 * agent.CANVAS_SHOT_WAIT + 60)
    assert want in argv
    import tomllib  # Codex reads `-c` values as TOML: a float of seconds
    assert tomllib.loads(want.split(".", 2)[2])["tool_timeout_sec"] == 260.0
    plain = agent._codex_argv(str(run_dir), True, None, "", "", "", [],
                              str(tmp_path / "x.py"))
    assert not any("tool_timeout_sec" in a for a in plain)


def _pointer(agent, request_id, run_id="run", env="unstable", name="demo"):
    inbox = os.path.join(agent._canvas_pages_dir(), "inbox")
    os.makedirs(inbox, exist_ok=True)
    with open(os.path.join(inbox, request_id + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"run_id": run_id, "request_id": request_id, "env": env,
                   "name": name, "what": "canvas", "udf": "", "created_at": 1}, fh)


def _page(page_id, env="unstable", name="demo", **kw):
    return json.dumps({"env": env, "name": name, "page_id": page_id, **kw})


def _parked(run_dir, request_id):
    (run_dir / "canvasshot" / (request_id + ".req.json")).write_text(
        json.dumps({"id": request_id, "what": "canvas", "created_at": 1}), encoding="utf-8")


def test_the_inbox_lists_its_canvas_rows_and_records_a_heartbeat(agent, run_dir):
    agent.RUNS = str(run_dir.parent)
    _pointer(agent, "r1")
    _parked(run_dir, "r1")
    # A pointer whose run is gone is never offered, and an old one is swept.
    _pointer(agent, "orphan", run_id="gone")
    _pointer(agent, "r2", name="other")
    out = agent.main(action="canvas_shot_inbox", state=_page("p1", visible=True, focused=True))
    assert [r["id"] for r in out["rows"]] == ["r1"]
    assert out["rows"][0]["runId"] == "run" and out["rows"][0]["claimed"] is False
    beat = os.path.join(agent._canvas_pages_dir(), "unstable", "demo", "p1.json")
    with open(beat, encoding="utf-8") as fh:
        assert json.load(fh)["focused"] is True
    # Every poll refreshes it, not only the first (the dir exists by then).
    agent.main(action="canvas_shot_inbox", state=_page("p1", visible=True, focused=False))
    with open(beat, encoding="utf-8") as fh:
        assert json.load(fh)["focused"] is False
    assert not os.path.exists(os.path.join(agent._canvas_pages_dir(), "inbox", "orphan.json"))
    # A bad page is refused, never joined into a path.
    assert agent.main(action="canvas_shot_inbox",
                      state=_page("../x"))["rows"] == []
    assert agent.main(action="canvas_shot_inbox",
                      state=_page("p1", env="stg"))["rows"] == []
    # Answered: off the list.
    (run_dir / "canvasshot" / "r1.res.json").write_text('{"error": "x"}', encoding="utf-8")
    assert agent.main(action="canvas_shot_inbox", state=_page("p1"))["rows"] == []


def test_one_page_claims_a_request_and_a_dead_claim_expires(agent, run_dir, monkeypatch):
    agent.RUNS = str(run_dir.parent)
    _pointer(agent, "r1")
    _parked(run_dir, "r1")
    claim = lambda p: agent.main(action="canvas_shot_claim", run_id="run",
                                 request_id="r1", state=json.dumps({"page_id": p}))
    assert claim("front") == {"claimed": True}
    assert claim("front") == {"claimed": True}           # idempotent
    assert claim("hidden") == {"claimed": False}
    # The other page no longer sees the row; the holder does, marked claimed.
    assert agent.main(action="canvas_shot_inbox", state=_page("hidden"))["rows"] == []
    assert agent.main(action="canvas_shot_inbox", state=_page("front"))["rows"][0]["claimed"]
    # Its page went away: after the TTL another page may take it.
    path = os.path.join(agent._canvas_pages_dir(), "inbox", "r1.claim")
    old = time.time() - agent.CANVAS_CLAIM_TTL - 5
    os.utime(path, (old, old))
    assert claim("hidden") == {"claimed": True}
    assert claim("nobody")["claimed"] is False
    assert agent.main(action="canvas_shot_claim", run_id="run", request_id="nope",
                      state=json.dumps({"page_id": "p"}))["claimed"] is False


def test_the_open_canvas_ask_reads_as_a_blocked_task(agent):
    from fused_render.server.routers import tasks
    news = tasks._attention_of(agent, {"tool": agent.CANVAS_OPEN_TOOL,
                                       "input": {"env": "prod", "name": "demo"}})
    assert news == {"tool": "", "summary": "Open demo to take a screenshot"}
    assert agent.CANVAS_OPEN_TOOL in agent._RISK_SKIP


# ---- the server side, live over stdio

@pytest.fixture
def pages_env(tmp_path, agent, run_dir, monkeypatch):
    """A canvas session's server, wired the way `_write_mcp_config` wires it."""
    _root, clone = _canvas_root(tmp_path, monkeypatch, agent)
    agent.RUNS = str(run_dir.parent)
    agent.SHOTS = str(tmp_path / "shots")
    os.makedirs(agent.SHOTS)
    env = {"FUSED_RENDER_CANVAS_CLONE": str(clone),
           "FUSED_RENDER_CANVAS_NODE": "",
           "FUSED_RENDER_CANVAS_PAGES": agent._canvas_pages_dir(),
           "FUSED_RENDER_CANVAS_SHOT_TIMEOUT": "6"}
    servers = []

    def start(**extra):
        args = [sys.executable, os.path.abspath(SERVER), str(run_dir / "perm"),
                "--canvas-shots=" + str(run_dir / "canvasshot")]
        s = MCPServer(args, env={**env, **extra})
        s.initialize()
        servers.append(s)
        return s
    yield start
    for s in servers:
        s.close()


def _beat(agent, page_id, name="demo"):
    agent.main(action="canvas_shot_inbox",
               state=_page(page_id, name=name, visible=True, focused=True, frame_up=True))


def test_an_open_page_answers_whatever_surface_the_chat_is_on(agent, run_dir, pages_env):
    """Aman's case: the chat is in the rail card, no chat holds a canvas — the
    PAGE sees the request in its inbox, claims it and answers. The note says
    what the picture really is (F6)."""
    server = pages_env()
    _beat(agent, "page")
    pending = server.send_async("tools/call", {
        "name": "canvas_screenshot", "arguments": {"what": "udf", "udf": "udf_2"}})
    deadline = time.monotonic() + 5
    rows = []
    while not rows and time.monotonic() < deadline:
        _beat(agent, "page")
        rows = agent.main(action="canvas_shot_inbox", state=_page("page"))["rows"]
        time.sleep(0.1)
    assert rows and rows[0]["udf"] == "udf_2"
    assert agent.main(action="canvas_shot_claim", run_id="run", request_id=rows[0]["id"],
                      state=json.dumps({"page_id": "page"}))["claimed"]
    image = os.path.join(agent.SHOTS, "shot.png")
    with open(image, "wb") as fh:
        fh.write(PNG)
    agent.main(action="canvas_shot", run_id="run", request_id=rows[0]["id"],
               state=json.dumps({"image": image, "mime": "image/png", "udf": "udf_2",
                                 "framing": "whole-frame"}))
    content = pending.result(10)["result"]["content"]
    note = json.loads(content[1]["text"])
    assert note["framing"] == "whole-frame" and "WHOLE canvas frame" in note["note"]
    assert "udf_2" in note["note"]
    # Done: the pointer is gone, so no page picks it up late.
    assert os.listdir(os.path.join(agent._canvas_pages_dir(), "inbox")) == []
    assert os.listdir(run_dir / "perm") == []          # no ask: a page was open


def test_another_canvas_open_still_asks_so_one_click_switches(agent, run_dir, pages_env):
    """Another canvas's page is no answer for this one: the same Open / Not
    now ask, raised fast, and Open switches the user over."""
    server = pages_env()
    _beat(agent, "elsewhere", name="other_canvas")
    began = time.monotonic()
    pending = server.send_async("tools/call", {"name": "canvas_screenshot", "arguments": {}})
    ask = _ask(run_dir)
    assert time.monotonic() - began < 5
    assert ask["tool"] == agent.CANVAS_OPEN_TOOL and ask["input"]["name"] == "demo"
    agent.main(action="decide", run_id="run", request_id=ask["id"], decision="deny")
    err = json.loads(pending.result(10)["result"]["content"][0]["text"])["error"]
    assert "continue without a screenshot" in err


def _ask(run_dir, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = [n for n in os.listdir(run_dir / "perm") if n.endswith(".req.json")]
        if names:
            with open(run_dir / "perm" / names[0], encoding="utf-8") as fh:
                return json.load(fh)
        time.sleep(0.05)
    raise AssertionError("no open-canvas ask was parked")


def test_no_page_open_asks_and_not_now_returns_at_once(agent, run_dir, pages_env):
    """F5: no page for any canvas → a card in perm/ (the task reads blocked);
    Not now → the tool returns a sentence to go on without."""
    server = pages_env()
    pending = server.send_async("tools/call", {"name": "canvas_screenshot", "arguments": {}})
    ask = _ask(run_dir)
    assert ask["tool"] == agent.CANVAS_OPEN_TOOL
    assert ask["input"] == {"env": "unstable", "name": "demo"}
    began = time.monotonic()
    agent.main(action="decide", run_id="run", request_id=ask["id"], decision="deny")
    err = json.loads(pending.result(10)["result"]["content"][0]["text"])["error"]
    assert time.monotonic() - began < 2
    assert "chose not to open" in err and "continue without a screenshot" in err


def test_open_hands_the_request_to_the_page_that_opens(agent, run_dir, pages_env):
    server = pages_env()
    pending = server.send_async("tools/call", {"name": "canvas_screenshot", "arguments": {}})
    ask = _ask(run_dir)
    agent.main(action="decide", run_id="run", request_id=ask["id"], decision="allow")
    # The canvas page comes up and finds the request in its inbox.
    rows = agent.main(action="canvas_shot_inbox", state=_page("new"))["rows"]
    assert len(rows) == 1
    agent.main(action="canvas_shot", run_id="run", request_id=rows[0]["id"],
               state=json.dumps({"error": "drawn"}))
    assert json.loads(pending.result(10)["result"]["content"][0]["text"]) == {"error": "drawn"}


def test_an_unanswered_ask_is_not_now_at_the_ceiling(agent, run_dir, pages_env):
    server = pages_env(FUSED_RENDER_CANVAS_SHOT_TIMEOUT="4")
    pending = server.send_async("tools/call", {"name": "canvas_screenshot", "arguments": {}})
    err = json.loads(pending.result(15)["result"]["content"][0]["text"])["error"]
    assert "nobody answered" in err and "continue without a screenshot" in err
    ask = _ask(run_dir)
    assert agent._read_decision(str(run_dir / "perm"), ask["id"])["reason"] == "timeout"


def test_a_wrong_udf_name_is_refused_with_the_nodes(agent, run_dir, pages_env):
    server = pages_env()
    out = server.call("tools/call", {"name": "canvas_screenshot",
                                     "arguments": {"what": "udf", "udf": "nope"}}, timeout=10)
    err = json.loads(out["result"]["content"][0]["text"])["error"]
    assert err == 'no UDF named "nope" on this canvas — nodes: udf_1, udf_2'
    assert os.listdir(run_dir / "canvasshot") == []


def test_a_page_opened_some_other_way_ends_the_ask(agent, run_dir, pages_env):
    """The user opens the canvas from the sidebar instead of pressing Open:
    the card closes as opened and that page answers."""
    server = pages_env()
    pending = server.send_async("tools/call", {"name": "canvas_screenshot", "arguments": {}})
    ask = _ask(run_dir)
    rows = agent.main(action="canvas_shot_inbox", state=_page("late"))["rows"]
    deadline = time.monotonic() + 5
    while not agent._read_decision(str(run_dir / "perm"), ask["id"]) and time.monotonic() < deadline:
        agent.main(action="canvas_shot_inbox", state=_page("late"))
        time.sleep(0.1)
    assert agent._read_decision(str(run_dir / "perm"), ask["id"]) == {
        "decision": "allow", "reason": "opened"}
    agent.main(action="canvas_shot", run_id="run", request_id=rows[0]["id"],
               state=json.dumps({"error": "drawn"}))
    assert json.loads(pending.result(10)["result"]["content"][0]["text"]) == {"error": "drawn"}

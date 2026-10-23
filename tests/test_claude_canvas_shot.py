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

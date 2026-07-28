"""The claude template's approval bridge: headless `claude -p` has no terminal
to prompt on, so `--permission-prompt-tool` routes each request to
`templates/claude/permission_server.py`, which parks it as a file until the
chat window answers through `agent.py`'s `decide` action.

Two contracts are worth pinning, and neither shows up in a test of anything
else:

* the **CLI wire shape** — a permission result is one text block whose text is
  JSON with `behavior: allow|deny` (message REQUIRED on a deny), and the flags
  that make the CLI ask us at all. Get any of it wrong and the failure is the
  original bug back again: a silent refusal nobody sees.
* the **decision file is a one-way latch** — first writer wins. A second answer
  to the same request (double-click, or a cancel landing on a card that was
  just allowed) must not overwrite a verdict the tool may already have acted
  on, and nothing that is not an explicit "allow" may read as one.

The claude CLI itself is never invoked here: the MCP server is driven directly
over its stdio JSON-RPC, which is exactly the surface the CLI talks to.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")
SERVER = os.path.join(TEMPLATE_DIR, "permission_server.py")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


# --------------------------------------------------------------- the MCP wire

class _Server:
    """A live permission_server subprocess, spoken to the way the CLI does."""

    def __init__(self, perm_dir, env=None):
        self.proc = subprocess.Popen(
            [sys.executable, os.path.abspath(SERVER), str(perm_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, **(env or {})})
        self._next_id = 0

    def call(self, method, params=None):
        self._next_id += 1
        self.proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": self._next_id, "method": method,
            "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def send_async(self, method, params, sink):
        """Fire a request whose response will not come back for a while."""
        self._next_id += 1
        req_id = self._next_id
        self.proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": req_id, "method": method,
            "params": params}) + "\n")
        self.proc.stdin.flush()

        def read():
            sink.append(json.loads(self.proc.stdout.readline()))
        t = threading.Thread(target=read, daemon=True)
        t.start()
        return t

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


@pytest.fixture
def server(tmp_path):
    s = _Server(tmp_path / "perm")
    s.call("initialize", {"protocolVersion": "2025-06-18",
                          "capabilities": {}, "clientInfo": {"name": "test"}})
    yield s
    s.close()


def _wait_for_request(perm_dir, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = [n for n in os.listdir(perm_dir) if n.endswith(".req.json")]
        if names:
            with open(os.path.join(perm_dir, names[0]), encoding="utf-8") as fh:
                return json.load(fh)
        time.sleep(0.05)
    raise AssertionError("permission_server never parked a request")


def _result_payload(response):
    """The permission result the CLI will parse out of a tools/call reply."""
    content = response["result"]["content"]
    assert len(content) == 1 and content[0]["type"] == "text", (
        "the CLI rejects anything but a single text block "
        "('Permission prompt tool returned an invalid result')")
    return json.loads(content[0]["text"])


def test_initialize_advertises_only_the_approval_tool(server):
    tools = server.call("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["approve"]


def test_allow_round_trip_returns_the_tool_input_unchanged(tmp_path, agent, server):
    perm_dir = tmp_path / "perm"
    tool_input = {"command": "ls -la", "description": "list"}
    sink = []
    reader = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Bash", "input": tool_input,
                      "tool_use_id": "toolu_01"}}, sink)

    req = _wait_for_request(perm_dir)
    assert req["tool"] == "Bash" and req["input"] == tool_input
    assert req["tool_use_id"] == "toolu_01"
    # The id is minted server-side, never taken from the CLI's tool_use_id:
    # it is joined into a path.
    assert agent._safe_name(req["id"]) and req["id"] != "toolu_01"

    assert agent._write_decision(str(perm_dir), req["id"],
                                 {"decision": "allow", "scope": "once"})
    reader.join(timeout=10)
    payload = _result_payload(sink[0])
    assert payload["behavior"] == "allow"
    assert payload["updatedInput"] == tool_input
    assert "updatedPermissions" not in payload


def test_session_scope_adds_a_session_rule_for_that_tool(tmp_path, server):
    perm_dir = tmp_path / "perm"
    sink = []
    reader = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Edit", "input": {"file_path": "/x.html"}}}, sink)
    req = _wait_for_request(perm_dir)
    (perm_dir / (req["id"] + ".res.json")).write_text(
        json.dumps({"decision": "allow", "scope": "session"}))
    reader.join(timeout=10)

    payload = _result_payload(sink[0])
    assert payload["behavior"] == "allow"
    # Bare tool name, no ruleContent: the wire hands us no permission
    # suggestions to narrow with, and matching stays the CLI's job.
    assert payload["updatedPermissions"] == [{
        "type": "addRules",
        "rules": [{"toolName": "Edit"}],
        "behavior": "allow",
        "destination": "session",
    }]


def test_deny_carries_a_message_because_the_cli_requires_one(tmp_path, server):
    perm_dir = tmp_path / "perm"
    sink = []
    reader = server.send_async("tools/call", {
        "name": "approve",
        "arguments": {"tool_name": "Bash", "input": {"command": "rm -rf /"}}}, sink)
    req = _wait_for_request(perm_dir)
    (perm_dir / (req["id"] + ".res.json")).write_text(json.dumps({"decision": "deny"}))
    reader.join(timeout=10)

    payload = _result_payload(sink[0])
    assert payload["behavior"] == "deny"
    assert payload["message"], "deny.message is required by the CLI's schema"


def test_unanswered_request_denies_itself_and_records_the_verdict(tmp_path):
    perm_dir = tmp_path / "perm"
    s = _Server(perm_dir, env={"FUSED_RENDER_PERMISSION_TIMEOUT": "1"})
    try:
        s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        sink = []
        reader = s.send_async("tools/call", {
            "name": "approve",
            "arguments": {"tool_name": "Bash", "input": {"command": "sleep 1"}}}, sink)
        req = _wait_for_request(perm_dir)
        reader.join(timeout=20)
        payload = _result_payload(sink[0])
        assert payload["behavior"] == "deny"
        # Written down, not just returned — otherwise the card reads as "still
        # waiting for you" forever on a page that re-attaches.
        recorded = json.loads((perm_dir / (req["id"] + ".res.json")).read_text())
        assert recorded == {"decision": "deny", "reason": "timeout"}
    finally:
        s.close()


def test_concurrent_requests_each_get_their_own_card(tmp_path, server):
    """Parallel tool calls must not queue behind the first unanswered one."""
    perm_dir = tmp_path / "perm"
    sinks = [[], []]
    readers = [
        server.send_async("tools/call", {
            "name": "approve",
            "arguments": {"tool_name": "Read", "input": {"file_path": "/a"}}}, sinks[0]),
        server.send_async("tools/call", {
            "name": "approve",
            "arguments": {"tool_name": "Read", "input": {"file_path": "/b"}}}, sinks[1]),
    ]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        names = [n for n in os.listdir(perm_dir) if n.endswith(".req.json")]
        if len(names) == 2:
            break
        time.sleep(0.05)
    assert len(names) == 2, "the second request never surfaced"
    for name in names:
        req_id = name[:-len(".req.json")]
        (perm_dir / (req_id + ".res.json")).write_text(json.dumps({"decision": "allow"}))
    for reader in readers:
        reader.join(timeout=10)
    assert all(_result_payload(s[0])["behavior"] == "allow" for s in sinks)


# ------------------------------------------------------------ agent.py side

def _run_dir(agent, tmp_path):
    run_dir = tmp_path / "run"
    os.makedirs(run_dir / "perm")
    return str(run_dir)


def test_decide_latches_on_the_first_answer(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    with open(os.path.join(perm_dir, "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {"command": "ls"}}, fh)

    monkey_runs(agent, tmp_path)
    assert agent._decide("run", "req-1", "allow", "once")["decision"] == "allow"
    # A second click (or a cancel racing it) must not flip the verdict the tool
    # has already been told about.
    assert agent._decide("run", "req-1", "deny", "once")["decision"] == "allow"
    assert agent._permissions(run_dir)[0]["decision"] == "allow"


def _lose_the_latch(perm_dir, request_id, payload, delay=0.25):
    """Take the O_EXCL latch the way a writer does — create first, write the
    JSON a beat later — so the next writer loses the race to a file that exists
    but does not parse yet. Returns the thread doing the delayed write."""
    path = os.path.join(perm_dir, request_id + ".res.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def finish():
        time.sleep(delay)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    t = threading.Thread(target=finish, daemon=True)
    t.start()
    return t


def test_decide_waits_out_an_in_flight_write_instead_of_guessing(agent, tmp_path):
    """The window between O_EXCL create and the JSON landing is not "unanswered".

    Reading it as unanswered is how the loser of a double-click reported ITS
    verdict — the card saying Allowed while claude was handed the Deny that
    actually won.
    """
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    with open(os.path.join(perm_dir, "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    monkey_runs(agent, tmp_path)

    t = _lose_the_latch(perm_dir, "req-1", {"decision": "deny", "scope": "once"})
    out = agent._decide("run", "req-1", "allow", "session")
    t.join(timeout=5)
    assert out["decision"] == "deny", "reported the click, not the winner"
    assert agent._permissions(run_dir)[0]["decision"] == "deny"


def test_decide_reports_an_error_when_no_write_survives(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    with open(os.path.join(perm_dir, "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    monkey_runs(agent, tmp_path)
    agent.DECISION_WRITE_WINDOW = 0.2  # don't sit through the real window

    # A latch taken by a writer that then died: the file exists, holds the
    # latch, and will never parse.
    os.close(os.open(os.path.join(perm_dir, "req-1.res.json"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    assert agent._decide("run", "req-1", "allow", "once") == {
        "error": "could not record that decision"}


def test_a_failed_write_does_not_jam_the_latch(agent, tmp_path, monkeypatch):
    """A write that dies after the create must release the file it claimed —
    an empty one holds the latch forever while never parsing, so the request
    could no longer be answered by anyone."""
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)

    real_fdopen = os.fdopen

    def boom(fd, *a, **kw):
        real_fdopen(fd, *a, **kw).close()
        raise OSError("disk full")

    monkeypatch.setattr(agent.os, "fdopen", boom)
    assert not agent._write_decision(perm_dir, "req-1", {"decision": "allow"})
    assert not os.path.exists(os.path.join(perm_dir, "req-1.res.json"))

    monkeypatch.undo()
    assert agent._write_decision(perm_dir, "req-1", {"decision": "allow"})
    assert agent._read_decision(perm_dir, "req-1")["decision"] == "allow"


def test_read_decision_does_not_block_the_poll_path(agent, tmp_path):
    """`poll` runs every 400 ms and must never sit on a partial file — it
    reports the request as still pending and the next tick corrects it."""
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    os.close(os.open(os.path.join(perm_dir, "req-1.res.json"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    started = time.monotonic()
    assert agent._read_decision(perm_dir, "req-1") == {}
    assert time.monotonic() - started < 0.5


def test_server_timeout_yields_to_a_click_landing_in_the_same_instant(tmp_path):
    """The sharp end of the same bug: here a misread reaches CLAUDE, denying a
    tool the user allowed."""
    perm_dir = tmp_path / "perm"
    s = _Server(perm_dir, env={"FUSED_RENDER_PERMISSION_TIMEOUT": "1"})
    try:
        s.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        sink = []
        reader = s.send_async("tools/call", {
            "name": "approve",
            "arguments": {"tool_name": "Bash", "input": {"command": "ls"}}}, sink)
        req = _wait_for_request(perm_dir)

        # Claim the latch just as the server's own wait expires, and let the
        # JSON land a beat later — exactly the shape of a click racing the
        # timeout.
        time.sleep(0.9)
        t = _lose_the_latch(str(perm_dir), req["id"],
                            {"decision": "allow", "scope": "once"}, delay=0.4)
        reader.join(timeout=20)
        t.join(timeout=5)

        payload = _result_payload(sink[0])
        assert payload["behavior"] == "allow", (
            "the server denied a request the user had allowed")
    finally:
        s.close()


@pytest.mark.parametrize("decision", ["deny", "", "Allow", "allow ", "yes", "1"])
def test_only_the_exact_string_allow_grants(agent, tmp_path, decision):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    monkey_runs(agent, tmp_path)
    assert agent._decide("run", "req-1", decision, "session")["decision"] == "deny"


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "a\\b", ".hidden", ""])
def test_traversal_ids_are_refused(agent, tmp_path, bad):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    monkey_runs(agent, tmp_path)
    assert "error" in agent._decide("run", bad, "allow", "once")
    assert "error" in agent._decide(bad, "req-1", "allow", "once")


def test_decide_refuses_a_request_that_was_never_raised(agent, tmp_path):
    _run_dir(agent, tmp_path)
    monkey_runs(agent, tmp_path)
    assert agent._decide("run", "made-up", "allow", "once") == {
        "error": "unknown permission request"}


def test_cancel_releases_every_parked_request(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    for n in ("a", "b"):
        with open(os.path.join(perm_dir, "req-%s.req.json" % n), "w") as fh:
            json.dump({"id": "req-" + n, "tool": "Bash", "input": {}}, fh)
    with open(os.path.join(run_dir, "pid"), "w") as fh:
        fh.write("-1")  # no such pgid; _cancel swallows the failed kill
    monkey_runs(agent, tmp_path)

    agent._cancel("run")
    assert [p["decision"] for p in agent._permissions(run_dir)] == ["deny", "deny"]


def test_poll_marks_an_unanswered_request_expired_once_the_run_is_over(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Bash", "input": {}}, fh)
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "result", "session_id": "s", "result": "done"}) + "\n")
    monkey_runs(agent, tmp_path)

    data = agent._poll("run")
    assert data["done"]
    # Not "pending": the run that was waiting on it is gone, so the buttons
    # would lead nowhere.
    assert data["permissions"][0]["decision"] == "expired"


def test_poll_reports_awaiting_while_a_request_is_parked(agent, tmp_path, monkeypatch):
    run_dir = _run_dir(agent, tmp_path)
    with open(os.path.join(agent._perm_dir(run_dir), "req-1.req.json"), "w") as fh:
        json.dump({"id": "req-1", "tool": "Edit", "input": {"file_path": "/x"}}, fh)
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "system", "session_id": "s"}) + "\n")
    monkey_runs(agent, tmp_path)
    monkeypatch.setattr(agent, "_alive", lambda _: True)

    data = agent._poll("run")
    assert not data["done"] and data["phase"] == "awaiting"
    assert data["permissions"][0]["tool"] == "Edit"


def test_malformed_request_files_are_skipped_not_fatal(agent, tmp_path):
    run_dir = _run_dir(agent, tmp_path)
    perm_dir = agent._perm_dir(run_dir)
    open(os.path.join(perm_dir, "half.req.json"), "w").write("{not json")
    with open(os.path.join(perm_dir, "esc.req.json"), "w") as fh:
        json.dump({"id": "../escape", "tool": "Bash", "input": {}}, fh)
    with open(os.path.join(perm_dir, "ok.req.json"), "w") as fh:
        json.dump({"id": "ok", "tool": "Bash", "input": {}}, fh)

    assert [p["id"] for p in agent._permissions(run_dir)] == ["ok"]


# ------------------------------------------------------------- the spawn line

def test_start_asks_the_cli_to_route_permissions_here(agent, tmp_path, monkeypatch):
    target = tmp_path / "sample.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    seen = {}

    class _Proc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    run_id = agent._start(str(target), "hi", "", "", "")["run_id"]
    cmd = seen["cmd"]

    tool = "mcp__%s__%s" % (agent.PERMISSION_SERVER, agent.PERMISSION_TOOL)
    assert cmd[cmd.index("--permission-prompt-tool") + 1] == tool
    # acceptEdits was only ever there because headless claude could not be
    # asked; with a prompt tool wired up the default (ask) is answerable.
    assert "--permission-mode" not in cmd
    # A prompt tool also un-gates these two in headless mode; this chat renders
    # neither, so they stay off.
    assert cmd[cmd.index("--disallowed-tools") + 1] == "AskUserQuestion,ExitPlanMode"

    config = json.loads(open(cmd[cmd.index("--mcp-config") + 1]).read())
    entry = config["mcpServers"][agent.PERMISSION_SERVER]
    assert os.path.basename(entry["args"][0]) == "permission_server.py"
    assert os.path.isfile(entry["args"][0])
    assert entry["args"][1] == agent._perm_dir(os.path.join(agent.RUNS, run_id))
    # The per-call ceiling has to clear the server's own wait, or the CLI cuts
    # in first and reports an MCP timeout instead of "nobody answered".
    assert entry["timeout"] > agent.PERMISSION_WAIT * 1000
    assert entry["env"]["FUSED_RENDER_PERMISSION_TIMEOUT"] == str(agent.PERMISSION_WAIT)


def test_template_wires_the_decide_action(agent):
    html = open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8").read()
    assert 'action: "decide"' in html
    assert "syncPermissions(data.permissions" in html
    # A tool input is model-authored text; rendering it as markup would be an
    # injection straight into the approval prompt the user is reading.
    assert ".innerHTML" not in html.split("function buildPermCard")[1] \
        .split("function syncPermissions")[0]


def monkey_runs(agent, tmp_path):
    """Point the module's RUNS at tmp_path, where `_run_dir` made "run"."""
    agent.RUNS = str(tmp_path)

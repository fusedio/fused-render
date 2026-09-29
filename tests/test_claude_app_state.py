"""claude's live-app-state channels: the chat's agent can SEE the app in the
left pane.

Two directions, and this file is the PYTHON half of both:

* **push** — the chat snapshots the left iframe (console errors, params, a
  bounded DOM outline) at send time and prepends it to the message inside a
  `<live-app-state>` block. The block is for the model, not for the user, so
  everything user-facing (the chat log, the session-list preview, the commit
  subject, a re-attach match) has to see the message WITHOUT it. What is pinned
  here is the stripping, and that the tag agent.py strips is still the tag
  `apps/claude/protocol/wire.ts` writes.
* **pull** — a second MCP tool on the same server (`app_state`) lets the agent
  re-read the page after an edit, over the same file round trip the approval
  tool uses. Its answer comes from the chat's poll loop, so an unanswered
  request must bound itself instead of blocking `claude` forever.

The claude CLI is never invoked: the MCP server is driven over its own stdio
JSON-RPC (the surface the CLI talks to).

The snapshot-taking itself — the DOM outline, the console buffer, the composer's
Escape claimants, the outline file — is the native chat's, and is tested in
`frontend/src/apps/claude` under vitest, where the DOM is real.
"""
import importlib.util
import json
import os
import re
import stat
import sys
import time

import pytest

from _mcp_stdio import MCPServer

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")
SERVER = os.path.join(TEMPLATE_DIR, "permission_server.py")

#: The native chat's wire vocabulary — the TypeScript half of every constant
#: duplicated across the Python/JS seam here (03 §4e).
_WIRE_TS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "frontend", "src", "apps", "claude", "protocol", "wire.ts")


def _ts_const(name: str) -> str:
    """The string value of an `export const <name> = "…"` in wire.ts."""
    src = open(_WIRE_TS, encoding="utf-8").read()
    found = re.findall(
        r"""(?m)^\s*export\s+const\s+%s\s*(?::[^=]*)?=\s*['"]([^'"]+)['"]""" % name,
        src)
    assert len(found) == 1, f"one writer of {name} in wire.ts, or this is stale"
    return found[0]


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


def _server(run_dir, env=None):
    """The template's permission_server, spawned the way `_write_mcp_config`
    spawns it: perm dir first, app-state dir second."""
    s = MCPServer([sys.executable, os.path.abspath(SERVER),
                   os.path.join(str(run_dir), "perm"),
                   os.path.join(str(run_dir), "appstate")], env=env)
    s.initialize()
    return s


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    return d


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


def _text_payload(response):
    """The JSON the model will read out of a tools/call reply."""
    content = response["result"]["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return json.loads(content[0]["text"])


# ------------------------------------------------------------ the second tool

def test_the_server_advertises_both_tools(server):
    """The approval tool is called BY the CLI; app_state is called by the model.
    Losing either half of this list is a silent loss of a whole channel."""
    tools = server.call("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["approve", "app_state"]
    schema = next(t for t in tools if t["name"] == "app_state")["inputSchema"]
    # `reason` is optional: the agent must never be blocked from looking.
    assert schema.get("required", []) == []
    assert "reason" in schema["properties"]


def test_an_app_state_call_is_answered_by_the_page(run_dir, server, agent):
    """The round trip: the tool parks a request, the page (here: agent.py's new
    action, which is all the page calls) writes the snapshot back, and the model
    gets the snapshot as the tool result."""
    agent.RUNS = str(run_dir.parent)
    pending = server.send_async("tools/call", {
        "name": "app_state", "arguments": {"reason": "after editing index.html"}})
    req = _wait_for_request(run_dir / "appstate")
    assert req["reason"] == "after editing index.html"

    snapshot = {"entry": "/p/index.html", "title": "Demo",
                "console": [{"level": "error", "text": "boom"}]}
    answered = agent.main(action="app_state", run_id="run", request_id=req["id"],
                          state=json.dumps(snapshot))
    assert answered == {"answered": req["id"]}
    assert _text_payload(pending.result(10)) == {"state": snapshot}


def test_an_app_state_request_never_lands_in_the_approvals_directory(
        run_dir, server):
    """Separate subdirectory, so the page's permission-card rendering and this
    never see each other's files: a card for "app_state" is not a thing the
    user should be asked to click, and a snapshot answer is not a verdict."""
    server.send_async("tools/call", {"name": "app_state", "arguments": {}})
    _wait_for_request(run_dir / "appstate")
    assert os.listdir(run_dir / "perm") == []


def test_an_unanswered_app_state_call_gives_up_instead_of_blocking(run_dir):
    """No page (mode switch, closed window) must not mean a wedged `claude`.
    The result is a structured "nobody answered", not an MCP error — the model
    can act on a sentence, it cannot act on a broken tool."""
    s = _server(run_dir, env={"FUSED_RENDER_APP_STATE_TIMEOUT": "1"})
    try:
        pending = s.send_async("tools/call", {"name": "app_state", "arguments": {}})
        payload = _text_payload(pending.result(20))
    finally:
        s.close()
    assert "state" not in payload
    assert "did not answer" in payload["error"]
    # Recorded, not just returned: the request must stop reading as "still
    # waiting for you" on disk, so a late answer cannot overwrite the verdict
    # the model was already given (same latch rule as a permission decision).
    req = _wait_for_request(run_dir / "appstate")
    res = json.load(open(run_dir / "appstate" / (req["id"] + ".res.json"),
                         encoding="utf-8"))
    assert res.get("error")


def test_an_answer_landing_in_the_same_instant_beats_the_timeout(run_dir):
    """The O_EXCL gap: the timeout writer that LOSES the create must wait the
    real answer's content out rather than substituting its own — the same
    misreport the approval path fixed (a verdict the page never gave)."""
    s = _server(run_dir, env={"FUSED_RENDER_APP_STATE_TIMEOUT": "1"})
    try:
        pending = s.send_async("tools/call", {"name": "app_state", "arguments": {}})
        req = _wait_for_request(run_dir / "appstate")
        path = run_dir / "appstate" / (req["id"] + ".res.json")
        # Claim the file the instant before the server's own timeout write, and
        # fill it a beat later — exactly what a page answer looks like.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        time.sleep(1.4)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"state": {"title": "late but real"}}, fh)
        payload = _text_payload(pending.result(20))
    finally:
        s.close()
    assert payload == {"state": {"title": "late but real"}}


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_parked_app_state_request_is_private(run_dir, server):
    """It names the app and the reason the agent is looking — the run tree sits
    under a world-readable temp root, so 0600 from the create as everywhere."""
    server.send_async("tools/call", {"name": "app_state", "arguments": {}})
    req = _wait_for_request(run_dir / "appstate")
    mode = stat.S_IMODE(os.stat(run_dir / "appstate"
                                / (req["id"] + ".req.json")).st_mode)
    assert mode == 0o600, oct(mode)


def test_an_unknown_tool_is_still_refused(server):
    err = server.call("tools/call", {"name": "app_stat", "arguments": {}})["error"]
    assert "unknown tool" in err["message"]


def test_a_session_with_no_pane_is_not_offered_the_tool_at_all(tmp_path):
    """D239: an ordinary folder has no left pane, so there is no page to read
    back — and a tool the model can call but that can never answer is worse than
    no tool. It would call it after every edit, wait out the 20s timeout and get
    a sentence about a window, once per turn.

    The channel's EXISTENCE is what decides: `agent.py` spawns the server with no
    app-state directory for a no-pane target, and no directory means no tool in
    `tools/list` and no dispatch for it. One signal, not two — a roster that
    varied independently of the channel could advertise a tool the server cannot
    serve.
    """
    s = MCPServer([sys.executable, os.path.abspath(SERVER),
                   str(tmp_path / "perm")])
    try:
        s.initialize()
        tools = s.call("tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == ["approve"]
        err = s.call("tools/call",
                     {"name": "app_state", "arguments": {}})["error"]
        assert "unknown tool" in err["message"]
    finally:
        s.close()


# --------------------------------------------------------------- the page side

def test_poll_surfaces_pending_app_state_requests(agent, run_dir):
    """The page's existing poll loop is the only thing that can answer, so the
    request has to ride back on the poll payload — alongside `permissions`,
    never mixed into it."""
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: True
    state_dir = run_dir / "appstate"
    (state_dir / "abc.req.json").write_text(json.dumps(
        {"id": "abc", "reason": "checking my edit", "created_at": 1.0}))
    data = agent._poll("run")
    assert data["app_state"] == [{"id": "abc", "reason": "checking my edit",
                                 "created_at": 1.0}]
    assert data["permissions"] == []

    # Answered requests drop off the list: there is no card to rebuild, so a
    # re-attaching page must not answer the same request twice.
    agent.main(action="app_state", run_id="run", request_id="abc",
               state=json.dumps({"title": "x"}))
    assert agent._poll("run")["app_state"] == []


def test_a_malformed_app_state_request_is_skipped_not_fatal(agent, run_dir):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: True
    (run_dir / "appstate" / "half.req.json").write_text("{not json")
    (run_dir / "appstate" / "bad.req.json").write_text(json.dumps({"id": "../x"}))
    assert agent._poll("run")["app_state"] == []


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "a\\b", ".hidden", ""])
def test_an_app_state_answer_cannot_escape_the_run_dir(agent, run_dir, bad):
    agent.RUNS = str(run_dir.parent)
    assert agent.main(action="app_state", run_id="run", request_id=bad,
                      state="{}").get("error")


def test_an_app_state_answer_for_a_request_nobody_raised_is_refused(agent, run_dir):
    agent.RUNS = str(run_dir.parent)
    assert agent.main(action="app_state", run_id="run", request_id="nope",
                      state="{}").get("error")


def test_an_unparseable_snapshot_is_recorded_as_an_error_not_as_state(
        agent, run_dir):
    """The page hands the snapshot over as a JSON string. Garbage must reach the
    model as an explicit "could not read the page", never as a silent empty
    snapshot it would then reason from."""
    agent.RUNS = str(run_dir.parent)
    (run_dir / "appstate" / "abc.req.json").write_text(json.dumps({"id": "abc"}))
    agent.main(action="app_state", run_id="run", request_id="abc", state="{oops")
    res = json.load(open(run_dir / "appstate" / "abc.res.json", encoding="utf-8"))
    assert res.get("error") and "state" not in res


def test_cancelling_a_run_releases_a_parked_app_state_request(agent, run_dir):
    """`_deny_pending` has to release BOTH request kinds: the app_state call
    blocks the subprocess exactly like an approval does, so a cancelled run
    would otherwise leave `claude` waiting on a window that is gone."""
    agent.RUNS = str(run_dir.parent)
    (run_dir / "perm" / "p1.req.json").write_text(json.dumps({"id": "p1"}))
    (run_dir / "appstate" / "s1.req.json").write_text(json.dumps({"id": "s1"}))
    agent._deny_pending(str(run_dir), "cancelled")
    assert json.load(open(run_dir / "perm" / "p1.res.json",
                          encoding="utf-8"))["decision"] == "deny"
    assert json.load(open(run_dir / "appstate" / "s1.res.json",
                          encoding="utf-8"))["error"]


def test_a_finished_run_releases_a_parked_app_state_request(agent, run_dir):
    """Same reasoning as the expired permission card: once the run is over, the
    page's poll loop has stopped, so nothing will ever answer this."""
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: False
    (run_dir / "out.jsonl").write_text(json.dumps(
        {"type": "result", "session_id": "s", "result": "done"}) + "\n")
    (run_dir / "appstate" / "s1.req.json").write_text(json.dumps({"id": "s1"}))
    data = agent._poll("run")
    assert data["done"] and data["app_state"] == []
    assert json.load(open(run_dir / "appstate" / "s1.res.json",
                          encoding="utf-8"))["error"]


# ------------------------------------------------- the spawn line & the prompt

class _HostProc:
    """Stands in for the session_host.py process `_start` now Popens instead
    of the CLI itself: the CLI's argv is built by `_claude_argv` inside THAT
    process, from the JSON request written to this stub's stdin — so a test
    that wants the argv has to capture the request and rebuild it, the same
    call session_host.py's own `main()` makes."""
    pid = 4242

    class _Stdin:
        def __init__(self, seen):
            self._seen = seen
            self._buf = b""

        def write(self, data):
            self._buf += data

        def close(self):
            self._seen["req"] = json.loads(self._buf.decode("utf-8"))

    def __init__(self, seen):
        self.stdin = _HostProc._Stdin(seen)


def _argv_from_req(agent, req, run_dir):
    return agent._claude_argv(
        run_dir, req["pane"], req["cli_mode"] or None, req["session_id"],
        req["model"], req["effort"], req["extra_read_dirs"], req["file"])


def _spawn(agent, monkeypatch, target, message="hi"):
    """Run `_start` against a fake Popen and return the argv `_claude_argv`
    builds from the request it hands the session host."""
    seen = {}

    # The argv is what's under test, not where claude lives. CI runners have no
    # claude on PATH, so resolving the real one would fail there and pass only
    # on a developer machine that happens to have it installed.
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: _HostProc(seen))
    out = agent._start(str(target), message, "", "", "")
    assert "error" not in out, out
    run_dir = os.path.join(agent.RUNS, out["run_id"])
    return _argv_from_req(agent, seen["req"], run_dir), run_dir


def test_the_mcp_server_gets_its_own_app_state_directory(agent, tmp_path,
                                                         monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><p>hi</p></html>',
        encoding="utf-8")
    _cmd, run_dir = _spawn(agent, monkeypatch, project)
    cfg = json.load(open(os.path.join(run_dir, "mcp.json"), encoding="utf-8"))
    args = cfg["mcpServers"][agent.PERMISSION_SERVER]["args"]
    assert args[1:] == [os.path.join(run_dir, "perm"),
                        os.path.join(run_dir, "appstate")]
    assert os.path.isdir(os.path.join(run_dir, "appstate"))


def test_a_no_pane_target_gets_no_app_state_channel_and_no_pre_allowance(
        agent, tmp_path, monkeypatch):
    """The spawn side of D239, and both halves have to agree.

    An ordinary folder (no top-level page carrying `<meta name="fused-app">` —
    the same `app_entry.entry_html` predicate the pane and the prompt read,
    D301) has no left
    pane. So: no app-state directory in the server's argv, which is what makes
    the tool absent from the roster, and no pre-allowance naming a tool that does
    not exist. The permission bridge itself is untouched — approvals are not
    about the pane.
    """
    agent.RUNS = str(tmp_path / "runs")
    plain = tmp_path / "downloads"
    plain.mkdir()
    (plain / "a.pdf").write_bytes(b"%PDF-1.4\n")
    cmd, run_dir = _spawn(agent, monkeypatch, plain)
    cfg = json.load(open(os.path.join(run_dir, "mcp.json"), encoding="utf-8"))
    args = cfg["mcpServers"][agent.PERMISSION_SERVER]["args"]
    assert args[1:] == [os.path.join(run_dir, "perm")]
    assert not os.path.exists(os.path.join(run_dir, "appstate"))
    tool = "mcp__%s__%s" % (agent.PERMISSION_SERVER, agent.APP_STATE_TOOL)
    assert tool not in cmd[cmd.index("--allowed-tools") + 1]
    assert "--permission-prompt-tool" in cmd
    # A folder that IS an app keeps the channel — the predicate is the only
    # thing that decides, so both answers are pinned in one place.
    app = tmp_path / "proj"
    app.mkdir()
    (app / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><p>hi</p></html>',
        encoding="utf-8")
    cmd2, run2 = _spawn(agent, monkeypatch, app)
    assert tool in cmd2[cmd2.index("--allowed-tools") + 1]
    assert os.path.isdir(os.path.join(run2, "appstate"))


def test_reading_the_users_own_screen_does_not_raise_a_card(agent, tmp_path,
                                                            monkeypatch):
    """app_state reads the page the user is already looking at, for the agent
    they are already talking to — carding that would be a prompt with no
    decision in it, once per edit. It is pre-allowed, and nothing else is.

    An APP folder, deliberately: only a target with a pane is offered the tool at
    all (D239), so an empty directory would be asserting the opposite rule."""
    agent.RUNS = str(tmp_path / "runs")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><p>hi</p></html>',
        encoding="utf-8")
    cmd, _run_dir = _spawn(agent, monkeypatch, project)
    tool = "mcp__%s__%s" % (agent.PERMISSION_SERVER, agent.APP_STATE_TOOL)
    allowed = cmd[cmd.index("--allowed-tools") + 1].split(",")
    assert tool in allowed
    # The only OTHER pre-allowance is reading an annotation's screenshot, and it
    # is scoped to the one directory those live in (test_claude_shots.py).
    assert allowed == [tool, agent._read_rule(agent.SHOTS)]
    # The bridge itself stays wired: everything else still has to be answerable.
    assert "--permission-prompt-tool" in cmd


# There are TWO kinds of directory target now: this template is the only chat, so
# it opens on ordinary folders as well as app folders, and `_split_system_prompt`
# picks between two prompts on `app_entry.entry_html`. `_app_dir` is the fixture
# for the app-folder half — an empty directory is NOT one, which is what these
# tests silently relied on until the second shape existed.

def _app_dir(tmp_path, name="proj"):
    d = tmp_path / name
    d.mkdir()
    # The marker is what makes the page an app entry (D301) — a bare filename
    # no longer does.
    (d / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><p>hi</p></html>',
        encoding="utf-8")
    return d


def test_a_directory_target_is_told_about_the_tool(agent, tmp_path, monkeypatch):
    """Every directory target gets an --append-system-prompt naming the tool: a
    tool the model is never told about is a tool it never calls. Asserted for BOTH
    folder shapes, since each has its own prompt and only one of them existed when
    the disclosure was written."""
    agent.RUNS = str(tmp_path / "runs")
    for target in (_app_dir(tmp_path, "app-proj"), tmp_path / "plain"):
        if not os.path.isdir(target):
            os.makedirs(target)
        cmd, _run_dir = _spawn(agent, monkeypatch, target)
        prompt = cmd[cmd.index("--append-system-prompt") + 1]
        # D239 narrowed this to the targets that HAVE a pane: an app folder is
        # told about the tool, an ordinary folder is not offered the tool at all,
        # so announcing it there would describe a tool the roster does not carry.
        if agent._is_app_dir(str(target)):
            assert agent.APP_STATE_TOOL in prompt, target
        else:
            assert agent.APP_STATE_TOOL not in prompt, target
        # Never the FILE-scoping prompt: a folder target is a folder, and scoping
        # it to one file is exactly what the directory branch exists to avoid.
        assert "Keep your work scoped to this file" not in prompt, target


def test_a_directory_target_is_told_what_kind_of_project_it_is_in(
        agent, tmp_path, monkeypatch):
    """Here rather than only in the starter CLAUDE.md, which is the user's file
    in the user's folder: a project whose CLAUDE.md was edited away, or that
    predates it, would otherwise have nothing telling the session that the HTML
    in front of it is an app with a Python bridge behind it."""
    agent.RUNS = str(tmp_path / "runs")
    cmd, _run_dir = _spawn(agent, monkeypatch, _app_dir(tmp_path))
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "fused-render project" in prompt
    # and the skill that documents the bridge is named, so the model reaches for
    # it instead of inferring the API from whatever is in the file
    assert "fused-render-authoring" in prompt


def test_an_ordinary_folder_is_not_told_it_is_a_fused_render_project(
        agent, tmp_path, monkeypatch):
    """`~/Downloads` is not a fused-render project and must not be told it is. The
    claim was unconditional while this template was gated to app folders; the mode
    is offered on every directory now, so an unconditional claim is a plain lie —
    one that costs something, because it invites the agent to hunt for a Python
    bridge that is not there and to read a folder of PDFs as a codebase."""
    agent.RUNS = str(tmp_path / "runs")
    plain = tmp_path / "downloads"
    plain.mkdir()
    (plain / "receipt.pdf").write_bytes(b"%PDF-1.4\n")
    cmd, _run_dir = _spawn(agent, monkeypatch, plain)
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "fused-render project" not in prompt
    assert "fused-render-authoring" not in prompt
    # What it gets instead: the folder-scoping instruction the deleted plain chat
    # template gave an ordinary folder, ported rather than reinvented.
    assert "Keep your work scoped to this folder" in prompt
    assert str(plain) in prompt
    # And NOTHING about a pane (D239). The paragraph describing fused-render's own
    # file browser beside the chat went with the pane it described — a prompt that
    # tells the model what the user can see beside the conversation, when there is
    # nothing beside the conversation, is a false claim about the screen. The
    # app-state disclosure goes too: this target is not offered the tool.
    assert "file browser" not in prompt
    assert "never try to edit it" not in prompt
    assert "Beside this chat" not in prompt
    assert agent.APP_STATE_TOOL not in prompt
    assert agent.APP_STATE_TAG not in prompt


def test_a_folder_whose_html_appears_later_gets_the_project_prompt(
        agent, tmp_path, monkeypatch):
    """The shape is decided per RUN, off the same `app_entry` rule the left pane
    uses (../shared/app_entry.py), not cached at open. So the folder the user is
    scaffolding INTO — empty when the chat opened, an app by the second turn —
    starts being told what it is as soon as it is one, and the prompt can never
    disagree with what the pane is actually framing."""
    agent.RUNS = str(tmp_path / "runs")
    d = tmp_path / "becoming"
    d.mkdir()
    cmd, _run_dir = _spawn(agent, monkeypatch, d)
    assert "fused-render project" not in cmd[cmd.index("--append-system-prompt") + 1]
    (d / "index.html").write_text(
        '<html><head><meta name="fused-app" /></head><p>hi</p></html>',
        encoding="utf-8")
    cmd, _run_dir = _spawn(agent, monkeypatch, d)
    assert "fused-render project" in cmd[cmd.index("--append-system-prompt") + 1]


def test_a_file_target_still_gets_the_file_scoping_prompt(agent, tmp_path,
                                                          monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "page.html"
    target.write_text("<p>hi</p>")
    cmd, _run_dir = _spawn(agent, monkeypatch, target)
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "Keep your work scoped to this file" in prompt


def test_a_file_target_is_also_told_about_the_app_state_tool(agent, tmp_path,
                                                             monkeypatch):
    """D235: a file target has a left pane too — the file in its own default
    view — so the same "an un-announced tool never gets called" argument that
    put the disclosure in the directory prompt applies here. It did not before,
    when the gate offered this template for project folders only and the file
    branch was the plain viewer's prompt verbatim."""
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "notes.md"
    target.write_text("# hi")
    cmd, _run_dir = _spawn(agent, monkeypatch, target)
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert agent.APP_STATE_TOOL in prompt
    # and it describes OUR viewer around THEIR file — never "your app", which
    # would invite edits to the template doing the rendering
    assert "fused-render's own preview" in prompt
    assert "never edit the viewer" in prompt


def test_a_file_target_is_not_told_it_is_a_fused_render_project(
        agent, tmp_path, monkeypatch):
    """The framing belongs to the DIRECTORY prompt, which is the only one this
    template's gate ever reaches — `condition.py` offers the split view solely
    for a project folder two levels under the workspace root. The file branch of
    `_start` is a fork of the plain viewer prompt and stays that way, so the app
    framing cannot leak into a target that is just a file being looked at."""
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "notes.md"
    target.write_text("# hi")
    cmd, _run_dir = _spawn(agent, monkeypatch, target)
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "fused-render project" not in prompt


# ------------------------------------------ the pushed block: model-only text

def test_the_state_block_reaches_the_cli_but_not_the_users_transcript(
        agent, tmp_path, monkeypatch):
    """The user typed the message, not the block. So the model gets the whole
    thing in the turn queued for the CLI, while everything replayed back to
    the page — the re-attach match, the session-list preview, the commit
    subject — gets the message the user actually typed."""
    agent.RUNS = str(tmp_path / "runs")
    project = tmp_path / "proj"
    project.mkdir()
    sent = ('<live-app-state>\nsnapshot of the app the user is looking at\n'
            '{"console": [{"level": "error", "text": "boom"}]}\n'
            '</live-app-state>\n\nwhy is the map blank?')
    _cmd, run_dir = _spawn(agent, monkeypatch, project, sent)
    inbox_dir = os.path.join(run_dir, "inbox")
    entry = json.load(open(
        os.path.join(inbox_dir, os.listdir(inbox_dir)[0]), encoding="utf-8"))
    assert entry["message"]["content"][0]["text"] == sent
    meta = json.load(open(os.path.join(run_dir, "meta.json"), encoding="utf-8"))
    assert meta["message"] == "why is the map blank?"


def test_history_hides_the_state_block_from_the_restored_transcript(agent,
                                                                    tmp_path):
    """The transcript on disk holds what claude was SENT, so the block comes
    back on every restore. Stripped in one place — here — rather than in the
    page, so the session-list preview, the commit subject and the restored log
    cannot disagree about what the user said."""
    project = tmp_path / "proj"
    project.mkdir()
    agent.PROJECTS = str(tmp_path / "projects")
    session = os.path.join(agent.PROJECTS, agent._munge(str(project)))
    os.makedirs(session)
    with open(os.path.join(session, "sid.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"message": {"role": "user", "content": [
            {"type": "text",
             "text": "<live-app-state>\n{\"title\": \"x\"}\n</live-app-state>\n\n"
                     "fix the header"}]}}) + "\n")
    turns = agent._history(str(project), "sid")["turns"]
    # `uuid` is the transcript record's own id, carried so the chat can scroll
    # to one turn (`?msg=`, the native chat's Transcript anchor); "" on a row that
    # has none, as this fixture does.
    assert turns == [{"role": "user", "text": "fix the header", "uuid": ""}]


def test_the_chat_and_the_agent_agree_on_the_block_delimiters(agent):
    """D146: the chat writes the block and agent.py strips it, so the marker is
    a rule in two places and needs a test rather than a comment. A drift here
    is invisible — the chat simply starts showing JSON to the user.

    The chat composes the tag from one exported const rather than spelling the
    literal twice, so it is that const the two sides have to agree on. Read as
    TEXT because a Python test cannot import TypeScript — the same pattern as
    tests/test_trouble_parity.py."""
    assert _ts_const("APP_STATE_TAG") == agent.APP_STATE_TAG
    assert agent._strip_app_state(
        "<%s>\n{}\n</%s>\n\nhello" % (agent.APP_STATE_TAG, agent.APP_STATE_TAG)
    ) == "hello"


def test_the_block_the_chat_prepends_is_one_the_merge_orders(agent):
    """The tag is only stripped because it is one of the blocks `mergeOutgoing`
    prepends — `BLOCK_ORDER` is that list, and a tag that fell out of it would
    stop being written while agent.py went on stripping a tag nobody sends."""
    src = open(_WIRE_TS, encoding="utf-8").read()
    order = re.search(r"export\s+const\s+BLOCK_ORDER[^=]*=\s*\[(.*?)\]", src, re.S)
    assert order, "BLOCK_ORDER moved out of wire.ts — this test reads nothing"
    assert "APP_STATE_TAG" in order.group(1)


def test_the_app_state_action_refuses_a_call_that_names_no_run(agent):
    """The other half of the same wire: the action name the chat calls back
    with (`runAgent("app_state", …)` in `apps/claude/protocol`) has to be one
    `main()` routes, or the pull settles as a permanent error."""
    assert agent.main(action="app_state", run_id="", request_id="x",
                      state="{}").get("error")


# ------------------------------- answering a pull: what reaches disk

def test_an_answer_that_did_not_reach_disk_is_reported_as_retryable(
        agent, run_dir, monkeypatch):
    """The write can fail (a full disk is the ordinary one), and the return value
    used to be discarded — so the action claimed `{"answered": ...}` with nothing
    on disk, the page kept the id claimed forever, and the tool call sat blocked
    for its whole timeout before telling the model the window never answered.
    While the window was alive and willing. Same treatment as `_decide`."""
    agent.RUNS = str(run_dir.parent)
    (run_dir / "appstate" / "abc.req.json").write_text(json.dumps({"id": "abc"}))
    monkeypatch.setattr(agent, "_write_decision", lambda *a, **k: False)
    out = agent.main(action="app_state", run_id="run", request_id="abc",
                     state=json.dumps({"title": "t"}))
    assert "answered" not in out
    assert out.get("error")
    # The flag, not the wording, is what the page keys on: this one is worth
    # another go in 400 ms, unlike the two "unknown" refusals below.
    assert out.get("retry") is True


def test_an_answer_to_a_request_that_does_not_exist_is_not_retried(agent, run_dir):
    """The other errors this action can return. Neither improves by being tried
    again — there is no such request, or no such run — so they must NOT carry the
    retry flag, or the page would spin on them until the run ends."""
    agent.RUNS = str(run_dir.parent)
    gone = agent.main(action="app_state", run_id="nope", request_id="abc", state="{}")
    unknown = agent.main(action="app_state", run_id="run", request_id="abc", state="{}")
    for out in (gone, unknown):
        assert out.get("error") and not out.get("retry"), out


def test_a_null_snapshot_on_the_wire_is_the_hard_error_the_page_now_avoids(
        agent, run_dir):
    """The python half of the same bug, pinned so the page's fix cannot be
    quietly undone: `JSON.stringify(null)` is the string "null", which parses to
    a non-dict and settles the tool call as a permanent error."""
    agent.RUNS = str(run_dir.parent)
    (run_dir / "appstate" / "abc.req.json").write_text(json.dumps({"id": "abc"}))
    agent.main(action="app_state", run_id="run", request_id="abc", state="null")
    res = json.load(open(run_dir / "appstate" / "abc.res.json", encoding="utf-8"))
    assert res.get("error") and "state" not in res


def test_a_path_carrying_block_is_still_stripped_from_the_transcript(agent):
    """agent.py's stripper is anchored on the tag, not the contents, and the tag
    did not change — pinned so the user's own words stay the transcript."""
    text = ('<live-app-state>\nsnapshot of the app\n'
            '{"title": "x", "dom_path": "/tmp/shots/appstate-1-1.json"}\n'
            '</live-app-state>\n\nwhy is it blank?')
    assert agent._strip_app_state(text) == "why is it blank?"


# ----------------------------------------- who decides whether there IS a pane

# Three things have to agree about the pane, per turn: the MCP roster (the
# app-state directory's existence), the pre-allowance on the spawn line, and the
# appended system prompt. `_start` resolved that answer from DISK on every turn,
# while the CHAT resolves it exactly once, at mount. So a mid-session kind flip
# put the two out of step in both directions:
#
#   * scaffold an app into an ordinary folder (turn 1 writes index.html) and turn
#     2 offers `app_state`, pre-allows it and asserts "The user sees the app
#     rendered live beside this chat" — with no pane on screen. The model calls
#     it, the pull burns its null polls and replies with the chat's
#     "unreadable" sentence, contradicting the invariant that sentence states
#     ("no pane means no `app_state` tool in the run's roster… this sentence can
#     never be the answer to it").
#   * delete the entry page and the reverse happens: the tool drops and the
#     prompt flips to ordinary-folder wording while a live pane is on screen.
#
# THE CHAT IS AUTHORITATIVE, because the question is "is there a page beside this
# chat" and only the chat knows what is on screen. Disk is the fallback for the
# one caller that has no chat (the apps API, which always spawns on an app
# folder).

def _spawn_with(agent, monkeypatch, target, **kw):
    """`_spawn`, through `main` and with STRING params — the shape the page's
    runPython call actually delivers (the param binder is str-shaped), so the
    "0" that means a real no cannot be mistaken for the "" that means absence."""
    seen = {}

    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kwargs: _HostProc(seen))
    out = agent.main(action="start", file=str(target), message="hi", **kw)
    assert "error" not in out, out
    run_dir = os.path.join(agent.RUNS, out["run_id"])
    return _argv_from_req(agent, seen["req"], run_dir), run_dir


def _pane_facts(agent, cmd, run_dir):
    """The three things that must agree."""
    tool = "mcp__%s__%s" % (agent.PERMISSION_SERVER, agent.APP_STATE_TOOL)
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    return {
        "state_dir": os.path.isdir(os.path.join(run_dir, "appstate")),
        "pre_allowed": tool in cmd[cmd.index("--allowed-tools") + 1],
        "in_prompt": agent.APP_STATE_TOOL in prompt,
        "says_beside_this_chat": "beside this chat" in prompt,
    }


def test_the_page_can_say_there_is_no_pane_and_the_whole_run_agrees(
        agent, tmp_path, monkeypatch):
    """The scaffolding repro. The folder IS an app folder on disk by turn 2, and
    the page still has no pane — so nothing in the run offers the tool."""
    agent.RUNS = str(tmp_path / "runs")
    scaffolded = _app_dir(tmp_path, "scaffolded")
    cmd, run_dir = _spawn_with(agent, monkeypatch, scaffolded, has_pane="0")
    facts = _pane_facts(agent, cmd, run_dir)
    assert facts == {"state_dir": False, "pre_allowed": False,
                     "in_prompt": False, "says_beside_this_chat": False}
    # ...and it is the ordinary-FOLDER prompt, not the app-folder one with its
    # pane paragraph removed.
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "fused-render project" not in prompt
    assert "embedded in a local file explorer" in prompt


def test_the_page_can_say_there_is_a_pane_and_the_whole_run_agrees(
        agent, tmp_path, monkeypatch):
    """The reverse flip: the entry page is gone from disk, the pane is still on
    screen, and the tool stays in the roster so `app_state` remains answerable."""
    agent.RUNS = str(tmp_path / "runs")
    plain = tmp_path / "was-an-app"
    plain.mkdir()
    cmd, run_dir = _spawn_with(agent, monkeypatch, plain, has_pane="1")
    facts = _pane_facts(agent, cmd, run_dir)
    assert facts == {"state_dir": True, "pre_allowed": True,
                     "in_prompt": True, "says_beside_this_chat": True}


def test_disk_still_answers_when_no_page_says_otherwise(agent, tmp_path,
                                                        monkeypatch):
    """The apps API calls `_start` directly with no page to ask. Unchanged
    behaviour: resolve from disk."""
    agent.RUNS = str(tmp_path / "runs")
    for target, wanted in ((_app_dir(tmp_path, "app"), True),
                           (tmp_path / "plain", False)):
        os.makedirs(target, exist_ok=True)
        cmd, run_dir = _spawn_with(agent, monkeypatch, target)
        facts = _pane_facts(agent, cmd, run_dir)
        assert facts["state_dir"] is wanted, target
        assert facts["pre_allowed"] is wanted, target
        assert facts["in_prompt"] is wanted, target


def test_the_prompt_reads_the_pane_value_the_spawn_already_computed(
        agent, tmp_path, monkeypatch):
    """A SECOND `_is_app_dir` call for the prompt reopens the window `pane`
    exists to close: an index.html appearing between the two (a concurrent
    scaffolding session, the user's editor, an in-flight `git checkout`) — or a
    transient EMFILE hitting `_is_app_dir`'s blanket `except Exception: return
    False` — spawns a run WITHOUT the app-state directory while the appended
    prompt announces the tool.

    Simulated with an `_is_app_dir` that flips after its first call, which is
    exactly what a race looks like from in here.
    """
    agent.RUNS = str(tmp_path / "runs")
    app = _app_dir(tmp_path, "racing")
    calls = []
    real = agent._is_app_dir

    def flaky(path):
        calls.append(path)
        return real(path) if len(calls) == 1 else not real(path)

    monkeypatch.setattr(agent, "_is_app_dir", flaky)
    cmd, run_dir = _spawn_with(agent, monkeypatch, app)
    facts = _pane_facts(agent, cmd, run_dir)
    assert facts["state_dir"] == facts["in_prompt"] == facts["pre_allowed"], facts
    assert len(calls) == 1, \
        "the kind is resolved once per run; the prompt reads that value"

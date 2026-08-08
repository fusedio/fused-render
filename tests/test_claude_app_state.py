"""claude's live-app-state channels: the split view's agent can SEE the
app in the left pane.

Two directions, and neither exists in the `claude` template:

* **push** — `template.html` snapshots the left iframe (console errors, params,
  a bounded DOM outline) at send time and prepends it to the message inside a
  `<live-app-state>` block. The block is for the model, not for the user, so
  everything user-facing (the chat log, the sidecar preview, the commit
  subject, a re-attach match) has to see the message WITHOUT it.
* **pull** — a second MCP tool on the same server (`app_state`) lets the agent
  re-read the page after an edit, over the same file round trip the approval
  tool uses. Its answer comes from the page's poll loop, so an unanswered
  request must bound itself instead of blocking `claude` forever.

The claude CLI is never invoked: the MCP server is driven over its own stdio
JSON-RPC (the surface the CLI talks to), and the page's own JS functions are
extracted and run under node — the same treatment the approval card's
summariser gets in test_claude_permission_bridge.py.

There is no frame descent to test any more, and that absence is itself asserted
below. The first build of this feature framed `/embed/<app>` — fused-render's
React shell, with the app nested one iframe deeper — and had to walk down to
find the app's window; #372 moved the pane to `/render?path=<entry>`, the raw
rendered document, so the frame's own `contentDocument` IS the app's. The walker
is gone, and with it the class of bug where the viewer's chrome got described as
the user's app.

What node still cannot cover is the live document: real console timing (the
app's first inline script runs before the frame's `load` and its logging is
genuinely missed), real iframe navigation, and what a real page outlines. Those
need a browser.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

from _mcp_stdio import MCPServer

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")
SERVER = os.path.join(TEMPLATE_DIR, "permission_server.py")
TEMPLATE = os.path.join(TEMPLATE_DIR, "template.html")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


@pytest.fixture
def html():
    return open(TEMPLATE, encoding="utf-8").read()


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

def _spawn(agent, monkeypatch, target, message="hi"):
    """Run `_start` against a fake Popen and return the argv it built."""
    seen = {}

    class _Proc:
        pid = 4242

    # The argv is what's under test, not where claude lives. CI runners have no
    # claude on PATH, so resolving the real one would fail there and pass only
    # on a developer machine that happens to have it installed.
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: (seen.__setitem__("cmd", cmd), _Proc())[1])
    out = agent._start(str(target), message, "", "", "")
    assert "error" not in out, out
    return seen["cmd"], os.path.join(agent.RUNS, out["run_id"])


def test_the_mcp_server_gets_its_own_app_state_directory(agent, tmp_path,
                                                         monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    _cmd, run_dir = _spawn(agent, monkeypatch, project)
    cfg = json.load(open(os.path.join(run_dir, "mcp.json"), encoding="utf-8"))
    args = cfg["mcpServers"][agent.PERMISSION_SERVER]["args"]
    assert args[1:] == [os.path.join(run_dir, "perm"),
                        os.path.join(run_dir, "appstate")]
    assert os.path.isdir(os.path.join(run_dir, "appstate"))


def test_a_no_pane_target_gets_no_app_state_channel_and_no_pre_allowance(
        agent, tmp_path, monkeypatch):
    """The spawn side of D239, and both halves have to agree.

    An ordinary folder (no `index.html`, no single top-level `.html` — the same
    `app_entry.entry_html` predicate the pane and the prompt read) has no left
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
    (app / "index.html").write_text("<p>hi</p>", encoding="utf-8")
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
    (project / "index.html").write_text("<p>hi</p>", encoding="utf-8")
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
    (d / "index.html").write_text("<p>hi</p>", encoding="utf-8")
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
    (d / "index.html").write_text("<p>hi</p>", encoding="utf-8")
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
    thing on the command line, while everything replayed back to the page —
    the re-attach match, the sidecar preview, the commit subject — gets the
    message the user actually typed."""
    agent.RUNS = str(tmp_path / "runs")
    project = tmp_path / "proj"
    project.mkdir()
    sent = ('<live-app-state>\nsnapshot of the app the user is looking at\n'
            '{"console": [{"level": "error", "text": "boom"}]}\n'
            '</live-app-state>\n\nwhy is the map blank?')
    cmd, run_dir = _spawn(agent, monkeypatch, project, sent)
    assert sent in cmd
    meta = json.load(open(os.path.join(run_dir, "meta.json"), encoding="utf-8"))
    assert meta["message"] == "why is the map blank?"


def test_history_hides_the_state_block_from_the_restored_transcript(agent,
                                                                    tmp_path):
    """The transcript on disk holds what claude was SENT, so the block comes
    back on every restore. Stripped in one place — here — rather than in the
    page, so the sidecar preview, the commit subject and the restored log
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
    assert turns == [{"role": "user", "text": "fix the header"}]


def test_the_page_and_the_agent_agree_on_the_block_delimiters(agent, html):
    """D146: the page writes the block and agent.py strips it, so the marker is
    a rule in two places and needs a test rather than a comment. A drift here
    is invisible — the chat simply starts showing JSON to the user."""
    # The page composes the tag from one const rather than spelling the literal
    # twice, so it is that const the two sides have to agree on.
    assert 'const APP_STATE_TAG = "%s";' % agent.APP_STATE_TAG in html
    assert agent._strip_app_state(
        "<%s>\n{}\n</%s>\n\nhello" % (agent.APP_STATE_TAG, agent.APP_STATE_TAG)
    ) == "hello"


def test_the_page_and_the_agent_agree_on_the_app_state_action(agent, html):
    """The other half of the same wire: the poll payload key and the action
    name the page calls back with."""
    assert 'action: "app_state"' in html
    assert "data.app_state" in html
    assert agent.main(action="app_state", run_id="", request_id="x",
                      state="{}").get("error")

# ------------------------------------------------- the page's own JS, in node

def _node(fn_names, call, html, prelude=""):
    """Run named top-level functions/consts out of template.html under node.

    Extracted and executed rather than asserted about, like the approval card's
    summariser: what matters is the object the agent ends up reading, not the
    shape of the source that built it."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own snapshot helpers")
    chunks = []
    for name in fn_names:
        start = html.index(name)
        if name.startswith("function") or name.startswith("async function"):
            end = html.index("\n}\n", start) + 3      # closing brace at column 0
            chunks.append(html[start:end])
            continue
        # A declaration: take whole lines until one whose CODE ends in `;`, so a
        # wrapped literal comes along and a trailing // comment does not confuse it.
        taken = []
        for line in html[start:].split("\n"):
            taken.append(line)
            if line.split("//")[0].rstrip().endswith(";"):
                break
        chunks.append("\n".join(taken))
    script = prelude + "\n" + "\n".join(chunks) + "\n" + call
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# A DOM small enough to write by hand and complete enough for the two functions
# that matter here: annPathOf walks up (parentElement / previousElementSibling /
# tagName) and annResolve walks down (body / children / getElementById). jsdom is
# not a dependency of this repo, and stubbing exactly the surface under test is
# what makes the round trip below a real round trip rather than a mock agreeing
# with itself.
_DOM = """
function el(tag, opts, kids) {
  opts = opts || {};
  const e = {tagName: tag.toUpperCase(), nodeType: 1, id: opts.id || "",
             className: opts.cls || "", children: [], childNodes: [],
             parentElement: null, previousElementSibling: null,
             textContent: opts.text || ""};
  if (opts.text) e.childNodes.push({nodeType: 3, nodeValue: opts.text});
  (kids || []).forEach((k) => {
    k.parentElement = e;
    k.previousElementSibling = e.children[e.children.length - 1] || null;
    e.children.push(k);
    e.childNodes.push(k);
  });
  return e;
}
function docOf(body) {
  return {body: body, title: "", getElementById(id) {
    let hit = null;
    (function walk(n) {
      if (n.id === id && !hit) hit = n;
      (n.children || []).forEach(walk);
    })(body);
    return hit;
  }};
}
// Every element in document order, body excluded (annPathOf is relative to it).
function flatten(body) {
  const out = [];
  (function walk(n) { (n.children || []).forEach((k) => { out.push(k); walk(k); }); })(body);
  return out;
}
// The outline's nodes in the same order, its root excluded.
function flatOutline(root) {
  const out = [];
  (function walk(n) { (n.children || []).forEach((k) => { out.push(k); walk(k); }); })(root);
  return out;
}
const BODY = el("body", {}, [
  el("header", {id: "top"}, [el("h1", {text: "Ookla speeds"})]),
  el("main", {cls: "wrap"}, [
    el("div", {cls: "row"}, [
      el("button", {text: "Deploy"}),
      el("button", {id: "reset", text: "Reset"}),
    ]),
    el("div", {cls: "row"}, [el("span", {text: "42 Mbps"})]),
  ]),
  el("script", {text: "fused.runPython('./data.py')"}),
]);
const DOC = docOf(BODY);
"""

_OUTLINE_FNS = ["const APP_STATE_MAX_TEXT", "const APP_STATE_MAX_NODES",
                "const APP_STATE_MAX_DEPTH", "const APP_STATE_MAX_NODE_TEXT",
                "const QUIET_TAGS", "function clipText(", "function annPathOf(",
                "function annResolve(", "function outlineNode("]

# A window whose document can be swapped, which is what a live-reload looks like
# from out here. `calls` records what the app's OWN console received, so the
# call-through can be checked rather than assumed.
_FRAME = """
function fakeWin(doc, href) {
  const w = {calls: [], listeners: [],
             location: {href: href || "http://x/render?path=/p/index.html",
                        pathname: "/render", search: "?path=/p/index.html&zoom=3"}};
  w.document = doc;
  w.console = {error(...a) { w.calls.push("error:" + a.join(" ")); },
               warn(...a) { w.calls.push("warn:" + a.join(" ")); }};
  w.addEventListener = (name) => w.listeners.push(name);
  return w;
}
"""

_STATE_FNS = ["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
              "const APP_STATE_MAX_NODES", "const APP_STATE_MAX_DEPTH",
              "const APP_STATE_MAX_NODE_TEXT", "const APP_STATE_TAG",
              "const APP_STATE_UNREADABLE",
              "const appLogs", "let appEntry", "let appLoads",
              "function clipText(", "function pushAppLog(", "function fmtLogArg(",
              "function appWindow(", "function watchWindow(", "function watchApp(",
              "function searchParamsOf(", "const CHAT_PARAMS",
              "function appParamsOf(", "const QUIET_TAGS", "function annPathOf(",
              "function outlineNode(", "function appStateSnapshot("]


# ------------------------------------------------------------ the pushed block

def test_the_state_block_is_omitted_when_there_is_nothing_to_say(html):
    """An empty snapshot must not push a block: a turn that says nothing about
    the app should look exactly like a turn from before this feature."""
    empty = _node(_BLOCK,
                  "console.log(JSON.stringify(appStateBlock(null)));", html)
    assert empty == ""


def test_the_state_block_labels_itself_and_carries_the_json(html):
    block = _node(_BLOCK,
                  "console.log(JSON.stringify(appStateBlock("
                  '{"title": "Demo", "console": [{"level": "error", "text": "boom"}]}'
                  ")));", html)
    assert block.startswith("<live-app-state>")
    assert block.endswith("</live-app-state>")
    # One paragraph telling the model what it is, then the payload.
    assert "looking at" in block.split("\n")[1]
    assert "boom" in block


def test_the_console_buffer_is_capped_and_each_entry_truncated(html):
    """A live-reloading page logs forever, and one console line can be a whole
    stack trace. Neither may grow the prompt without bound."""
    logs = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                  "const appLogs", "function clipText(", "function pushAppLog("],
                 "for (let i = 0; i < 400; i++) pushAppLog('error', 'x'.repeat(900));"
                 "console.log(JSON.stringify(appLogs));", html)
    assert len(logs) == 50
    assert len(logs[0]["text"]) == 301 and logs[0]["text"].endswith("…")


def test_the_buffer_keeps_the_newest_entries(html):
    """Which end is dropped matters: the errors that appeared AFTER Claude's edit
    are the ones worth reporting."""
    logs = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                  "const appLogs", "function clipText(", "function pushAppLog("],
                 "for (let i = 0; i < 60; i++) pushAppLog('warn', 'line ' + i);"
                 "console.log(JSON.stringify(appLogs.map((e) => e.text)));", html)
    assert logs[0] == "line 10" and logs[-1] == "line 59"


def test_console_log_is_not_captured_only_errors_and_warnings(html):
    """50 ring slots spent on an app's ordinary chatter would push out the
    errors they exist for."""
    out = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const appLogs", "function clipText(", "function pushAppLog(",
                 "function fmtLogArg(", "function watchWindow("],
                _DOM + _FRAME + "const W = fakeWin(docOf(BODY));"
                + "const originalLog = (msg) => W.calls.push('log:' + msg);"
                + "W.console.log = originalLog;"
                + "watchWindow(W);"
                + "W.console.log('just chatter');"
                + "W.console.error('a real problem');"
                + "console.log(JSON.stringify({untouched: W.console.log === originalLog,"
                  " buffered: appLogs.map((e) => e.text)}));", html)
    assert out["untouched"] is True, "console.log must be left alone"
    assert out["buffered"] == ["a real problem"]


def test_the_dom_outline_is_bounded_in_depth_and_node_count(html):
    """outerHTML for a real app is tens of KB that would dominate every turn."""
    out = _node(_OUTLINE_FNS,
                _DOM
                + "let deep = el('span', {text: 'bottom'});"
                + "for (let i = 0; i < 8; i++) deep = el('div', {}, [deep]);"
                + "const wide = el('ul', {}, Array.from({length: 200},"
                  " (_, i) => el('li', {text: 'item ' + i})));"
                + "const body = el('body', {}, [deep, wide]);"
                + "const doc = docOf(body);"
                + "const tree = outlineNode(body, 0, null, doc);"
                + "let depth = 0, count = 0;"
                + "(function walk(n, d) { depth = Math.max(depth, d); count++;"
                  " (n.children || []).forEach((k) => walk(k, d + 1)); })(tree, 0);"
                + "console.log(JSON.stringify({depth, count}));", html)
    assert out["depth"] <= 4, out
    # the shared budget caps the WHOLE walk, not each level
    assert out["count"] <= 61, out


def test_the_outline_notes_where_it_truncated(html):
    """Truncation the agent cannot see is a lie about the page: it would read an
    elided element as an absent one."""
    out = _node(_OUTLINE_FNS,
                _DOM
                + "const body = el('body', {}, Array.from({length: 200},"
                  " (_, i) => el('p', {text: 'p' + i})));"
                + "const tree = outlineNode(body, 0, null, docOf(body));"
                + "console.log(JSON.stringify(tree.truncated || ''));", html)
    assert "sibling(s) not shown" in out


def test_the_outline_lists_a_script_without_quoting_its_source(html):
    """A clipped 120 chars of an app's own JS in a structural outline is noise at
    best; the agent can read the file properly. The element is still listed —
    "there is a script here" is structure."""
    out = _node(_OUTLINE_FNS,
                _DOM + "const tree = outlineNode(BODY, 0, null, DOC);"
                + "console.log(JSON.stringify(flatOutline(tree)"
                  ".filter((n) => n.tag === 'script')));", html)
    assert len(out) == 1 and out[0]["tag"] == "script"
    assert "text" not in out[0], out


# ------------------------------------- ONE identifier space, shared with #372

def test_an_outline_path_resolves_back_to_the_element_it_describes(html):
    """The property that makes the shared scheme worth anything: every path the
    outline emits is one `annResolve` hands back the very same element for. If it
    were a second, subtly different path builder, the agent could read a node in
    the outline and be unable to act on the pin the user put on it."""
    out = _node(_OUTLINE_FNS,
                _DOM + "const tree = outlineNode(BODY, 0, null, DOC);"
                + "const els = flatten(BODY), nodes = flatOutline(tree);"
                + "const rows = els.map((e, i) => ({tag: e.tagName.toLowerCase(),"
                  " path: nodes[i] && nodes[i].path,"
                  " same: annResolve({anchorPath: nodes[i] && nodes[i].path}, DOC) === e}));"
                + "console.log(JSON.stringify(rows));", html)
    assert len(out) >= 8, out
    assert all(r["path"] for r in out), out
    assert all(r["same"] for r in out), [r for r in out if not r["same"]]
    # the shape is annotations' own, not a private one
    assert out[0]["path"] == "header:nth-of-type(1)", out[0]


def test_an_outline_node_carries_the_id_the_annotation_anchor_would_use(html):
    """anchorId is annResolve's preferred key, so the outline has to surface it
    too — otherwise the agent sees a path for an element the user's pin names by
    id, and cannot tell they are the same thing."""
    out = _node(_OUTLINE_FNS,
                _DOM + "const tree = outlineNode(BODY, 0, null, DOC);"
                + "const byId = flatOutline(tree).filter((n) => n.id);"
                + "console.log(JSON.stringify(byId.map((n) => ({id: n.id,"
                  " same: annResolve({anchorId: n.id}, DOC) === DOC.getElementById(n.id)}))));",
                html)
    assert sorted(r["id"] for r in out) == ["reset", "top"], out
    assert all(r["same"] for r in out), out


def test_the_path_scheme_has_exactly_one_implementation(html):
    """D146: a rule in two implementations needs a test, and the cheapest way to
    pass that test is not to have two. The outline calls the annotation layer's
    builder rather than growing its own."""
    assert html.count("function annPathOf(") == 1
    start = html.index("function outlineNode(")
    body = html[start:html.index("\n}\n", start)]
    assert "annPathOf(" in body, "the outline builds its own paths"


# ------------------------------------- reading the framed app, without descent

def test_no_frame_descent_machinery_remains(html):
    """#372 reframed the pane as /render?path=<entry> — the RAW rendered
    document — so `leftframe.contentDocument` is the app's document and there is
    nothing to walk down through. The walker is deleted, not left dormant:
    dormant code that once resolved a window is exactly what would get
    reinstated by the next person who reads the comment above it."""
    for gone in ["reachableFrames", "hasRuntime", "appFrameOf",
                 "resolveAppWindow", "APP_FRAME_MAX_DEPTH"]:
        assert gone not in html, gone


def test_the_app_document_is_the_left_frames_own(html):
    url = _node(_STATE_FNS,
                _DOM + _FRAME
                + "let annFrame = {isConnected: true,"
                  " contentWindow: fakeWin(docOf(BODY))};"
                + "const s = appStateSnapshot();"
                + "console.log(JSON.stringify({url: s.url, tag: s.dom.tag,"
                  " params: s.params}));", html)
    assert url["tag"] == "body"
    assert url["url"] == "/render?path=/p/index.html&zoom=3"
    # /render's own plumbing is not something the app defined; the app's is
    assert url["params"] == {"zoom": "3"}


def test_a_project_with_no_app_entry_degrades_instead_of_describing_this_chat(html):
    """When there is no entry html the loader REMOVES the iframe, so `annFrame`
    is a detached element. Reporting this chat's own window as the app's is the
    defect that framing would invite, so the snapshot has to see "no app" — and
    a snapshot with nothing in it at all is null, not an empty object."""
    out = _node(_STATE_FNS,
                _DOM + _FRAME
                + "let annFrame = {isConnected: false, contentWindow: null};"
                + "console.log(JSON.stringify({bare: appStateSnapshot(),"
                  " withLog: (pushAppLog('error', 'the left pane could not open"
                  " the app: no app entry'), appStateSnapshot())}));", html)
    # nothing known and nothing logged: no block at all this turn
    assert out["bare"] is None
    # once the console says WHY, the sentence earns its place beside it
    assert "could not be read" in out["withLog"]["unreadable"]
    assert out["withLog"]["console"][0]["text"].startswith("the left pane")
    assert "dom" not in out["withLog"] and "title" not in out["withLog"]
    assert "url" not in out["withLog"], "an unreadable frame must not report a url"


def test_about_blank_is_not_described_as_the_app(html):
    """The placeholder document every iframe starts on. Describing it would put
    an empty body in the prompt as though the app rendered nothing."""
    out = _node(_STATE_FNS,
                _DOM + _FRAME
                + "let annFrame = {isConnected: true,"
                  " contentWindow: fakeWin(docOf(BODY), 'about:blank')};"
                + "console.log(JSON.stringify(appStateSnapshot()));", html)
    assert out is None


def test_an_unreachable_frame_never_throws_the_send(html):
    """Cross-origin, or torn down mid-navigation. Nothing here may break the
    chat: a failure degrades to less state, never to a thrown send."""
    out = _node(_STATE_FNS,
                _DOM + _FRAME
                + "let annFrame = {isConnected: true, get contentWindow() {"
                  " throw new Error('cross-origin'); }};"
                + "pushAppLog('error', 'boom');"
                + "console.log(JSON.stringify(appStateSnapshot()));", html)
    assert out["unreadable"]
    assert out["console"][0]["text"] == "boom"


def test_the_chats_own_params_are_not_reported_as_the_apps(html):
    """This page sets no param boundary, so the pane's runtime hands back the
    app's params merged with this chat's bookkeeping. Telling the model the app
    is "running with" session_id and split is worse than saying nothing."""
    out = _node(["function clipText(", "const CHAT_PARAMS", "function appParamsOf(",
                 "const APP_STATE_MAX_TEXT"],
                "console.log(JSON.stringify(appParamsOf({session_id: 's', run: 'r',"
                " split: '70', model: 'sonnet', effort: 'high', permission: 'prompt',"
                " _file: '/p', _mode: 'claude', path: '/p/index.html',"
                " annotations: '[]', city: 'Lisbon'})));", html)
    assert out == {"city": "Lisbon"}


# ------------------------------------------- instrumentation, and the reload

def test_the_reload_marker_fires_on_the_frames_own_load(html):
    """With /render in the pane, a live-reload replaces the LEFT FRAME's document,
    so its `load` fires again — which is why the 100 ms poller the first build
    needed (the reload used to navigate a nested frame the outer load never saw)
    is gone. The marker rides in the buffer rather than clearing it: "these were
    there before, this one appeared after your edit" is the buffer's whole value."""
    out = _node(_STATE_FNS,
                _DOM + _FRAME
                + "const W = fakeWin(docOf(BODY));"
                + "let annFrame = {isConnected: true, contentWindow: W};"
                + "watchApp();"                          # first document
                + "pushAppLog('error', 'was already broken');"
                + "watchApp();"                          # same document: a no-op
                + "W.document = docOf(BODY);"            # a live-reload landed
                + "watchApp();"
                + "console.log(JSON.stringify(appLogs.map((e) => [e.level, e.text])));",
                html)
    assert [lvl for lvl, _ in out] == ["error", "reload"], out
    assert "reloaded" in out[1][1]


def test_the_watcher_is_wired_to_the_load_event_not_to_a_timer(html):
    """The interval existed only because the old /embed framing hid the reload
    from the outer frame. Hooking the same `load` the annotation rewiring uses
    keeps one place where a fresh document is noticed."""
    assert "APP_WATCH_INTERVAL" not in html
    assert "setInterval(watchApp" not in html
    start = html.index('annFrame.addEventListener("load"')
    assert "watchApp()" in html[start:start + 500], "the load hook does not watch"


def test_a_document_is_only_wrapped_once_but_a_new_one_is_wrapped_again(html):
    """The marker lives on the DOCUMENT: a same-origin navigation keeps the
    window proxy but replaces the global, so a flag on the window would outlive
    the wrapper it is meant to track and the reloaded page would go unlogged."""
    out = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const appLogs", "function clipText(", "function pushAppLog(",
                 "function fmtLogArg(", "function watchWindow("],
                _DOM + _FRAME + "const W = fakeWin(docOf(BODY));"
                + "const first = watchWindow(W), again = watchWindow(W);"
                + "W.document = docOf(BODY);"
                + "const fresh = watchWindow(W);"
                + "console.log(JSON.stringify({first, again, fresh}));", html)
    assert out == {"first": True, "again": False, "fresh": True}


def test_the_wrapper_calls_through_so_the_apps_own_logging_still_happens(html):
    """Instrumentation the user can notice is instrumentation that changed the
    app: whatever devtools showed before must still show."""
    out = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const appLogs", "function clipText(", "function pushAppLog(",
                 "function fmtLogArg(", "function watchWindow("],
                _DOM + _FRAME + "const W = fakeWin(docOf(BODY));"
                + "watchWindow(W);"
                + "W.console.error('boom', {code: 7});"
                + "W.console.warn(new Error('careful'));"
                + "console.log(JSON.stringify({buffered: appLogs.map((e) => e.text),"
                  " passedThrough: W.calls, hooked: W.listeners}));", html)
    assert out["buffered"] == ['boom {"code":7}', "careful"]
    assert out["passedThrough"] == ['error:boom [object Object]', "warn:Error: careful"]
    # uncaught errors and rejections are the ones a console wrapper cannot see
    assert out["hooked"] == ["error", "unhandledrejection"]


# ------------------------- one composition point, one strip (both blocks)

_WIRE_FNS = ["const APP_STATE_TAG", "function appStateBlock(",
             # formatAnnotations' preamble names the target's KIND (a file's pane
             # is fused-render's preview OF the file, not an app with an entry
             # page), and this is the one writer of that noun.
             "let targetNoun", "let paneNoun", "function formatAnnotations(",
             "function composeOutgoing(",
             "function stripAppStateBlock(", "function stripBlocks(",
             # The pane shot is a third block on the same wire (see
             # test_claude_shots.py); these two are what stripBlocks needs to
             # be its exact inverse, whether or not a given message carries one.
             "const PANE_SHOT_TAG", "function paneShotBlock(",
             # stripBlocks names its no-words markers through these (see
             # test_claude_shots.py) so resumeRun cannot drift from it.
             "const MARKER_ANN", "const MARKER_VIEW", "const MARKER_JOIN",
             "function isMarkerOnly(", "function stripPaneBlock(",
             "function stripAnnBlock("]

_PENDING = ('[{"id": "1", "sent": 0, "createdAt": 5, "anchorId": "reset",'
            ' "tag": "button", "text": "Reset"}]')
_STATE = '{"title": "Demo", "dom": {"tag": "body", "path": null}}'


def _round_trip(html, typed, annotated, stateful):
    """Compose a wire message the way the composer does, then strip it the way
    the transcript readers do."""
    return _node(_WIRE_FNS,
                 "const wire = composeOutgoing(%s, %s, %s);"
                 "console.log(JSON.stringify({wire, back: stripBlocks(wire)}));"
                 % (json.dumps(typed), _PENDING if annotated else "[]",
                    _STATE if stateful else "null"), html)


@pytest.mark.parametrize("annotated,stateful", [
    (False, False), (True, False), (False, True), (True, True),
])
def test_a_wire_message_strips_back_to_exactly_what_the_user_typed(html, annotated,
                                                                  stateful):
    """The bug this pins, in both directions: main's `stripAnnBlock` bails unless
    the text STARTS with its own preamble, so an app-state block in front of it
    silently no-ops the strip and leaks the annotation JSON into the transcript —
    while an app-state block placed after the annotation fence leaks itself
    instead. Neither order works with one strip that only knows one block, so
    there is one composer and one strip and this covers all four combinations."""
    out = _round_trip(html, "why is the map blank?", annotated, stateful)
    assert out["back"] == "why is the map blank?", out["wire"]
    assert ("live-app-state" in out["wire"]) is stateful
    assert ("The user annotated " in out["wire"]) is annotated


def test_an_annotation_only_send_still_collapses_to_its_marker(html):
    """No typed text: the transcript shows a small marker, and the app-state
    block riding along must not turn into the bubble's contents."""
    out = _round_trip(html, "", True, True)
    assert out["back"] == "📌 annotations", out["wire"]


def test_the_state_block_is_removed_wherever_it_sits(html):
    """A position-independent peel, matching agent.py's regex: the composer fixes
    the order, but a message stored by an older build (or by a future one that
    reorders) must still strip clean rather than half-strip."""
    out = _node(_WIRE_FNS,
                'const tail = "hello\\n\\n<live-app-state>\\n{}\\n</live-app-state>\\n\\n";'
                'console.log(JSON.stringify(stripBlocks(tail)));', html)
    assert out == "hello"


def test_the_two_readers_of_a_wire_message_use_the_same_strip(html):
    """The re-attach probe compares against the bubble on screen and the history
    restore renders the bubble. If they stripped differently, a re-attach would
    stop matching and trim another turn's rows."""
    assert html.count("stripAnnBlock(") == 2, "stripAnnBlock is called outside stripBlocks"
    assert "stripBlocks(probe.message" in html
    assert "addUser(stripBlocks(t.text))" in html


# ------------------------------------------------- the pull channel, on screen

def test_the_pull_answers_with_a_snapshot_taken_now_not_the_pushed_one(html):
    """The tool exists because the pushed snapshot went stale the moment Claude
    edited something; answering from a cached one would be the same staleness
    with an extra round trip."""
    start = html.index("async function answerAppState(")
    body = html[start:html.index("\n}\n", start)]
    assert "appStateSnapshot()" in body
    # and it says so in the log — the whole transparency story for a read that
    # deliberately raises no card
    assert '"read app state' in body or "read app state" in body


def test_reading_the_app_state_is_noted_in_the_log_not_carded(html):
    assert "addNote(" in html
    assert 'p.tool' in html  # permission cards still exist for everything else
    start = html.index("async function answerAppState(")
    assert "buildPermCard" not in html[start:html.index("\n}\n", start)]


# ------------------------------ an empty snapshot: null means two things

_PULL_FNS = _STATE_FNS + ["function appStatePull(", "const answeredStates",
                          "const notedStates",
                          "const APP_STATE_NULL_POLLS", "const nullStatePolls",
                          "const APP_STATE_MEMO_MAX", "function appStateTrim(",
                          "async function answerAppState("]

# `fused.runPython` and the on-screen note, recorded rather than performed. The
# thing under test is WHICH polls send an answer and what that answer says.
_PULL_STUBS = """
var AGENT = "./agent.py";
var sentStates = [];
var noted = [];
function addNote(text, working) { noted.push(text); }
var fused = {runPython: async (agent, params) => {
  sentStates.push(params.state); return {};
}};
"""


def test_a_mid_reload_pull_is_retried_rather_than_settled_as_an_error(html):
    """The model is TOLD to call app_state after an edit, and an edit live-reloads
    the left pane — so the snapshot is empty at exactly the moment the tool is
    most likely to be called. Sending the bare `null` that is right for the push
    channel makes agent.py write a permanent "could not read the app's state",
    and the id stays claimed so no later poll retries. The reload has to be
    waited out instead."""
    out = _node(_PULL_FNS, _DOM + _FRAME + _PULL_STUBS
                + "let annFrame = {isConnected: true, contentWindow: null};"
                + """
(async () => {
  const req = [{id: "r1", reason: "checking my edit"}];
  for (let i = 0; i < 3; i++) await answerAppState(req, "run", null);
  const duringReload = {sent: sentStates.length, noted: noted.length,
                        claimed: answeredStates.has("r1")};
  // the pane finishes loading
  annFrame = {isConnected: true, contentWindow: fakeWin(docOf(BODY))};
  await answerAppState(req, "run", null);
  console.log(JSON.stringify({duringReload: duringReload,
    answers: sentStates.map((s) => JSON.parse(s)), noted: noted}));
})();
""", html)
    # nothing sent, nothing claimed, and no note on screen for a read that has
    # not happened yet
    assert out["duringReload"] == {"sent": 0, "noted": 0, "claimed": False}
    # once the app is back, the very next poll answers with the real thing
    assert len(out["answers"]) == 1
    assert out["answers"][0]["dom"]["tag"] == "body"
    assert out["noted"] == ["read app state — checking my edit"]


def test_a_pull_that_can_never_read_the_app_is_answered_instead_of_spun(html):
    """The other null: a project with no app entry, where the iframe was REMOVED
    and no amount of waiting will produce a document. Retrying to the tool's own
    timeout (minutes) would be a worse answer for the model than the sentence
    saying so, so the retry is bounded."""
    out = _node(_PULL_FNS, _DOM + _FRAME + _PULL_STUBS
                + "let annFrame = {isConnected: false, contentWindow: null};"
                + """
(async () => {
  const req = [{id: "r1"}];
  const perPoll = [];
  for (let i = 0; i < APP_STATE_NULL_POLLS + 3; i++) {
    await answerAppState(req, "run", null);
    perPoll.push(sentStates.length);
  }
  console.log(JSON.stringify({perPoll: perPoll,
    answers: sentStates.map((s) => JSON.parse(s)), raw: sentStates}));
})();
""", html)
    # exactly one answer, and not before the bound is spent
    assert out["perPoll"] == [0] * 5 + [1, 1, 1], out["perPoll"]
    assert len(out["answers"]) == 1
    answer = out["answers"][0]
    # a dict, NOT the bare `null` agent.py reads as a permanent failure
    assert out["raw"][0] != "null"
    assert isinstance(answer, dict)
    assert "could not be read" in answer["unreadable"]
    # the security constraint: we could not read the app's window, so there is no
    # honest source for any of these — and this chat's own window is not it
    for leak in ("url", "params", "title", "dom"):
        assert leak not in answer, leak


def test_the_push_and_the_pull_read_one_snapshot_and_disagree_about_null(html):
    """D146: two callers now interpret the same return value differently, so the
    difference is asserted rather than described. Push omits the block entirely
    (a turn with nothing to say must look like a turn from before this feature);
    pull cannot omit anything, because the model is blocked on a reply."""
    out = _node(_PULL_FNS, _DOM + _FRAME + _PULL_STUBS
                + "let annFrame = {isConnected: true, contentWindow: null};"
                + "appEntry = '/p/index.html';"
                + "console.log(JSON.stringify({push: appStateSnapshot(),"
                  " pull: appStatePull(), sentence: APP_STATE_UNREADABLE}));",
                html)
    assert out["push"] is None
    assert out["pull"]["unreadable"] == out["sentence"]
    # the one thing we DO know without reading the window
    assert out["pull"]["entry"] == "/p/index.html"


def test_a_failed_answer_is_retried_and_a_refused_one_is_not(html):
    """The page's half of the same gap. It only ever un-claimed on a THROW, so a
    resolved `{error: ...}` — the shape a write that did not reach disk now
    returns — left the id claimed forever: no later poll retried it and the tool
    call blocked for its full timeout. A throw and a retryable error are the same
    news; a refusal ("no such request") is not, and spinning on it every 400 ms
    until the run ends would be the wrong answer."""
    out = _node(_PULL_FNS, _DOM + _FRAME + """
var AGENT = "./agent.py";
var sentStates = [];
var noted = [];
function addNote(text, working) { noted.push(text); }
var replies = [null,                                     // a throw
               {error: "could not record", retry: true}, // transient
               {error: "unknown app-state request"}];    // permanent
var fused = {runPython: async (a, p) => {
  sentStates.push(p.state);
  const r = replies.shift();
  if (r === null) throw new Error("bridge down");
  return r || {};
}};
let annFrame = {isConnected: true, contentWindow: fakeWin(docOf(BODY))};
(async () => {
  const req = [{id: "r1", reason: "why"}];
  const claims = [];
  for (let i = 0; i < 4; i++) {
    await answerAppState(req, "run", null);
    claims.push([sentStates.length, answeredStates.has("r1")]);
  }
  console.log(JSON.stringify({claims: claims, noted: noted}));
})();
""", html)
    # attempt 1 threw and 2 came back retryable, so each was tried again; 3 was a
    # refusal, so attempt 4 never left the page.
    assert out["claims"] == [[1, False], [2, False], [3, True], [3, True]]
    # ONE line in the log for one request, however many attempts it took: the note
    # is the transparency story for the read, not a tally of the bridge's health.
    assert out["noted"] == ["read app state — why"]


def test_the_per_request_bookkeeping_is_capped(html):
    """`answeredStates` and friends hold one entry per app_state call for the
    page's lifetime. Bounded in practice; capped anyway, oldest first — a request
    answered hundreds of calls ago can never be polled again, since agent.py only
    ever lists the unanswered ones."""
    out = _node(_PULL_FNS, _DOM + _FRAME + _PULL_STUBS + """
let annFrame = {isConnected: true, contentWindow: fakeWin(docOf(BODY))};
(async () => {
  for (let i = 0; i < APP_STATE_MEMO_MAX + 25; i++) {
    await answerAppState([{id: "r" + i}], "run", null);
  }
  console.log(JSON.stringify({answered: answeredStates.size, noted: notedStates.size,
                              cap: APP_STATE_MEMO_MAX, sent: sentStates.length}));
})();
""", html)
    assert out["sent"] == out["cap"] + 25, "every request still gets answered"
    assert out["answered"] <= out["cap"]
    assert out["noted"] <= out["cap"]


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


# ------------------------------------------------- Escape has three claimants

def test_escape_prefers_the_smallest_undo_it_can_do(html):
    """Three features bind Escape in this pane, and the order is least-destructive
    first. Dismissing a popover is small and repeatable, leaving annotate mode is
    reversible with one click, killing a live turn is neither — so an Escape
    pressed with a text box open means the text box, and one pressed while
    annotating means annotate mode, not the run."""
    def act(open_, annotating, run):
        return _node(["function escapeAction("],
                     "console.log(JSON.stringify(escapeAction(%s, %s, %s)));"
                     % (json.dumps(open_), json.dumps(annotating), json.dumps(run)),
                     html)

    assert act(True, False, "run-7") == "close-composer"
    assert act(True, True, None) == "close-composer"
    # The banner says "Esc or click to stop", so Escape must leave annotate mode —
    # and must do it in preference to ending the turn.
    assert act(False, True, None) == "exit-annotate"
    assert act(False, True, "run-7") == "exit-annotate"
    assert act(False, False, "run-7") == "stop-run"
    # Inert otherwise: this page is in an iframe and must not swallow the shell's
    # Escape for nothing.
    assert act(False, False, None) == ""


def test_escape_is_bound_inside_the_framed_app_too(html):
    """Annotate mode is used with the pointer over the iframe, so that is where
    the keydown lands — and a keydown in the frame's document does not bubble to
    this one. Binding only the parent document is why Escape looked broken while
    annotating: the same reason mousedown/mousemove/click are all bound on `doc`."""
    load = html.index("annFrame.addEventListener(\"load\"")
    body = html[load:html.index("// ── send one message", load)]
    assert "doc.addEventListener(\"keydown\", onEscape" in body, body[-3000:]
    # and the parent keeps its own binding, for an Escape pressed in the chat pane
    assert "document.addEventListener(\"keydown\", onEscape" in html


def test_the_composers_escape_does_not_also_reach_the_run_killer(html):
    """The textarea's handler runs first and hides the popover, so without a
    stopPropagation the document binding would look at an already-closed
    composer and end the turn as well."""
    start = html.index("annTa.addEventListener(\"keydown\"")
    head = html[start:start + 400]
    assert "stopPropagation" in head, head


# ------------------------------- the outline travels as a file, not in the message

# The outline used to be stringified straight into the message, which put it in
# the CLI's own session transcript: N messages meant N full DOM trees re-read on
# every later turn, all but the newest already stale. It goes to a file now and
# only the path rides along.

# `paneNoun` is what the block preamble calls the pane — "app" for an app
# folder, "preview" for a file, one writer (test_claude_kind.py).
_BLOCK = ["let paneNoun", "const APP_STATE_TAG", "function appStateBlock("]
_FILE = ["function shotJoin(", "let appStateSeq", "async function appStateFile("]


def test_the_block_points_at_the_outline_instead_of_carrying_it(html):
    block = _node(_BLOCK, 'console.log(JSON.stringify(appStateBlock('
                  '{"title": "Disk Cleaner", '
                  '"dom_path": "/tmp/shots/appstate-1-1.json"})));', html)
    assert "/tmp/shots/appstate-1-1.json" in block
    # and it TELLS the model the tree is in a file — a path with no instruction
    # is a path that never gets read
    assert "dom_path" in block and "read it" in block


def test_an_outline_that_could_not_be_written_still_rides_inline(html):
    """The fallback has to keep working: no directory or a failed write must not
    leave the agent knowing less about the screen than before this existed."""
    block = _node(_BLOCK, 'console.log(JSON.stringify(appStateBlock('
                  '{"dom": {"tag": "body"}})));', html)
    assert '"tag":"body"' in block.replace(" ", "")
    assert "dom_path" not in block


def test_the_preamble_does_not_promise_a_file_that_is_not_there(html):
    """Two shapes, one preamble. Describing `dom_path` when the outline came
    inline would send the model looking for a key that does not exist."""
    inline = _node(_BLOCK, 'console.log(JSON.stringify(appStateBlock('
                   '{"dom": {"tag": "body"}})));', html)
    assert "read it" not in inline


def test_the_outline_is_written_into_the_screenshot_directory(html):
    """That directory, and not a new one: it is already 0700-enforced, already
    pruned, and already the one path --allowed-tools lets Read touch without
    raising a card."""
    prelude = """
let written = null;
async function shotDirPath() { return "/tmp/shots"; }
const fused = { async uploadFile(path) { written = path; } };
"""
    out = _node(_FILE, 'appStateFile({"title": "x", "dom": {"tag": "body"}})'
                '.then((s) => console.log(JSON.stringify({state: s, '
                'written: written})));', html, prelude)
    assert out["written"].startswith("/tmp/shots/appstate-")
    assert out["written"].endswith(".json")
    assert out["state"]["dom_path"] == out["written"]
    # the whole point: the bytes are gone from what gets composed
    assert "dom" not in out["state"]
    assert out["state"]["title"] == "x"


def test_two_sends_in_the_same_millisecond_do_not_share_a_file(html):
    """Date.now() alone collides, and the second send would overwrite the first
    send's outline while the first was still being read."""
    prelude = """
async function shotDirPath() { return "/tmp/shots"; }
const fused = { async uploadFile() {} };
"""
    out = _node(_FILE, 'Promise.all([appStateFile({"dom": {"tag": "body"}}), '
                'appStateFile({"dom": {"tag": "body"}})]).then((s) => '
                'console.log(JSON.stringify(s.map((x) => x.dom_path))));',
                html, prelude)
    assert out[0] != out[1], out


def test_a_failed_write_keeps_the_outline_inline_rather_than_losing_it(html):
    """console.warn goes to stderr, so the fallback stays quiet on stdout."""
    prelude = """
async function shotDirPath() { throw new Error("no screenshot directory"); }
const fused = { async uploadFile() {} };
"""
    out = _node(_FILE, 'appStateFile({"dom": {"tag": "body"}})'
                '.then((s) => console.log(JSON.stringify(s)));', html, prelude)
    assert out["dom"] == {"tag": "body"}
    assert "dom_path" not in out


def test_a_snapshotless_send_is_left_alone(html):
    prelude = """
async function shotDirPath() { throw new Error("never called"); }
const fused = { async uploadFile() {} };
"""
    out = _node(_FILE, 'appStateFile(null).then((s) => '
                'console.log(JSON.stringify({state: s})));', html, prelude)
    assert out["state"] is None


def test_the_send_path_writes_the_outline_before_it_composes(html):
    """composeOutgoing is sync and pure — the exact inverse of stripBlocks — so
    the write cannot happen inside it."""
    assert "const sentState = await appStateFile(state);" in html
    assert "composeOutgoing(message, pending, sentState," in html


def test_the_outline_path_comes_from_the_shots_directory_the_agent_grants(html):
    """Pinned as source, because the security of this rests on the file landing
    under the directory `_read_rule` already covers."""
    start = html.index("async function appStateFile(")
    body = html[start:html.index("\n}\n", start)]
    assert "shotDirPath()" in body


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
# while the PAGE resolves it exactly once — `paneURL()` runs in the boot IIFE and
# `enterNoPane()` removes `#left` permanently. So a mid-session kind flip put the
# two out of step in both directions:
#
#   * scaffold an app into an ordinary folder (turn 1 writes index.html) and turn
#     2 offers `app_state`, pre-allows it and asserts "The user sees the app
#     rendered live beside this chat" — with no pane on screen. The model calls
#     it, `answerAppState` burns its null polls and replies
#     APP_STATE_UNREADABLE, contradicting the invariant the page states at
#     template.html's APP_STATE_UNREADABLE ("no pane means no `app_state` tool in
#     the run's roster… this sentence can never be the answer to it").
#   * delete the entry page and the reverse happens: the tool drops and the
#     prompt flips to ordinary-folder wording while a live pane is on screen.
#
# THE PAGE IS AUTHORITATIVE, because the question is "is there a page beside this
# chat" and only the page knows what is on screen. Disk is the fallback for the
# one caller that has no page (the apps API, which always spawns on an app
# folder).

def _spawn_with(agent, monkeypatch, target, **kw):
    """`_spawn`, through `main` and with STRING params — the shape the page's
    runPython call actually delivers (the param binder is str-shaped), so the
    "0" that means a real no cannot be mistaken for the "" that means absence."""
    seen = {}

    class _Proc:
        pid = 4242

    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kwargs: (seen.__setitem__("cmd", cmd), _Proc())[1])
    out = agent.main(action="start", file=str(target), message="hi", **kw)
    assert "error" not in out, out
    return seen["cmd"], os.path.join(agent.RUNS, out["run_id"])


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


def test_the_page_sends_its_own_answer_on_every_start(html):
    """The page's half. `noPane` is the layout flag `enterNoPane` sets, and the
    only thing that knows whether `#left` is in the document."""
    start = html.index('action: "start"')
    call = html[start:html.index("}, { key: null });", start)]
    assert "has_pane" in call and "noPane" in call


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

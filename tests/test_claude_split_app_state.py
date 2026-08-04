"""claude_split's live-app-state channels: the split view's agent can SEE the
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

The frame chain is STUBBED, and it is stubbed because a real browser found the
defect these tests now pin: `/embed/<app>` is fused-render's own shell and the
app is a nested iframe one level deeper, so the first build described the
viewer's chrome and captured none of the app's logging. The stub pins the
descent, the instrumentation target and the re-wrap-on-reload rule. What it
still cannot cover is the live document itself — real console timing, real
iframe navigation, and what a real page outlines to all need a browser.
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

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude_split")
SERVER = os.path.join(TEMPLATE_DIR, "permission_server.py")
TEMPLATE = os.path.join(TEMPLATE_DIR, "template.html")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_split_" + name, path)
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
    _cmd, run_dir = _spawn(agent, monkeypatch, project)
    cfg = json.load(open(os.path.join(run_dir, "mcp.json"), encoding="utf-8"))
    args = cfg["mcpServers"][agent.PERMISSION_SERVER]["args"]
    assert args[1:] == [os.path.join(run_dir, "perm"),
                        os.path.join(run_dir, "appstate")]
    assert os.path.isdir(os.path.join(run_dir, "appstate"))


def test_reading_the_users_own_screen_does_not_raise_a_card(agent, tmp_path,
                                                            monkeypatch):
    """app_state reads the page the user is already looking at, for the agent
    they are already talking to — carding that would be a prompt with no
    decision in it, once per edit. It is pre-allowed, and nothing else is."""
    agent.RUNS = str(tmp_path / "runs")
    project = tmp_path / "proj"
    project.mkdir()
    cmd, _run_dir = _spawn(agent, monkeypatch, project)
    tool = "mcp__%s__%s" % (agent.PERMISSION_SERVER, agent.APP_STATE_TOOL)
    assert cmd[cmd.index("--allowed-tools") + 1] == tool
    # The bridge itself stays wired: everything else still has to be answerable.
    assert "--permission-prompt-tool" in cmd


def test_a_directory_target_is_told_about_the_tool(agent, tmp_path, monkeypatch):
    """The one directory target that DOES get an --append-system-prompt: a tool
    the model is never told about is a tool it never calls."""
    agent.RUNS = str(tmp_path / "runs")
    project = tmp_path / "proj"
    project.mkdir()
    cmd, _run_dir = _spawn(agent, monkeypatch, project)
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert agent.APP_STATE_TOOL in prompt
    # Narrow: naming the tool only, NOT the file-scoping prompt a file target
    # gets — the split view is a whole project and scoping it to one file is
    # exactly what the directory branch exists to avoid.
    assert "Keep your work scoped to this file" not in prompt


def test_a_file_target_still_gets_the_file_scoping_prompt(agent, tmp_path,
                                                          monkeypatch):
    agent.RUNS = str(tmp_path / "runs")
    target = tmp_path / "page.html"
    target.write_text("<p>hi</p>")
    cmd, _run_dir = _spawn(agent, monkeypatch, target)
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "Keep your work scoped to this file" in prompt
    assert agent.APP_STATE_TOOL not in prompt


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
    assert "<%s>" % agent.APP_STATE_TAG in html
    assert "</%s>" % agent.APP_STATE_TAG in html
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

def _node(fn_names, call, html):
    """Run named top-level functions/consts out of template.html under node.

    Extracted and executed rather than asserted about, like the approval card's
    summariser: what matters is the object the agent ends up reading, not the
    shape of the source that built it."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own snapshot helpers")
    chunks = []
    for name in fn_names:
        start = html.index(name)
        if name.startswith("function"):
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
    out = subprocess.run(["node", "-e", "\n".join(chunks) + "\n" + call],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_state_block_is_omitted_when_there_is_nothing_to_say(html):
    """An empty snapshot must not push a block: a turn that says nothing about
    the app should look exactly like today's turn."""
    empty = _node(["function appStateBlock("],
                  "console.log(JSON.stringify(appStateBlock(null)));", html)
    assert empty == ""


def test_the_state_block_labels_itself_and_carries_the_json(html):
    block = _node(["function appStateBlock("],
                  "console.log(JSON.stringify(appStateBlock("
                  '{"title": "Demo", "console": [{"level": "error", "text": "boom"}]}'
                  ")));", html)
    assert block.startswith("<live-app-state>")
    assert "</live-app-state>" in block
    # One line telling the model what it is, then the payload.
    assert "looking at" in block.split("\n")[1]
    assert "boom" in block
    # The user's own text is not inside the block.
    assert block.rstrip().endswith("</live-app-state>")


def test_the_console_buffer_is_capped_and_each_entry_truncated(html):
    """Unbounded, this is a log of every error a live-reloading page ever threw
    — pushed into the model's context on every turn."""
    got = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const appLogs", "function clipText(", "function pushAppLog("],
                "for (let i = 0; i < 200; i++) pushAppLog('error', 'x'.repeat(5000));"
                "console.log(JSON.stringify("
                "{n: appLogs.length, len: appLogs[0].text.length}));", html)
    assert got["n"] == 50
    assert got["len"] <= 301


def test_the_buffer_keeps_the_newest_entries(html):
    got = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const appLogs", "function clipText(", "function pushAppLog("],
                "for (let i = 0; i < 60; i++) pushAppLog('warn', 'm' + i);"
                "console.log(JSON.stringify("
                "[appLogs[0].text, appLogs[appLogs.length - 1].text]));", html)
    assert got == ["m10", "m59"]


def test_the_dom_outline_is_bounded_in_depth_and_node_count(html):
    """A structural outline, not `outerHTML`: a real app's body is tens of KB
    and would dominate every turn. Driven here with a stub node object — this
    checks the BUDGET, which is the part that can run away; it says nothing
    about what a real document outlines to (that needs a browser)."""
    stub = """
    function node(tag, kids) {
      return {tagName: tag, id: "", className: "", children: kids || [],
              childNodes: [], textContent: "t"};
    }
    let deep = node("DIV");
    for (let i = 0; i < 40; i++) deep = node("DIV", [deep]);
    const wide = node("BODY", Array.from({length: 500}, () => node("SPAN")));
    """
    got = _node(["const APP_STATE_MAX_NODES", "const APP_STATE_MAX_DEPTH",
                 "const APP_STATE_MAX_NODE_TEXT", "const QUIET_TAGS",
                 "function clipText(", "function outlineNode("],
                stub + "console.log(JSON.stringify({"
                "deep: JSON.stringify(outlineNode(deep, 0)).length,"
                "wide: JSON.stringify(outlineNode(wide, 0)).length,"
                "wideKids: outlineNode(wide, 0).children.length}));", html)
    assert got["wideKids"] <= 60
    assert got["deep"] < 2000 and got["wide"] < 8000


def test_the_outline_notes_where_it_truncated(html):
    """Truncation the model cannot see is a lie about the page: it would report
    a missing element as absent when it was only elided."""
    stub = """
    function node(tag, kids) {
      return {tagName: tag, id: "", className: "", children: kids || [],
              childNodes: [], textContent: ""};
    }
    const wide = node("BODY", Array.from({length: 500}, () => node("SPAN")));
    """
    got = _node(["const APP_STATE_MAX_NODES", "const APP_STATE_MAX_DEPTH",
                 "const APP_STATE_MAX_NODE_TEXT", "const QUIET_TAGS",
                 "function clipText(", "function outlineNode("],
                stub + "console.log(JSON.stringify(outlineNode(wide, 0)));", html)
    assert got.get("truncated")


# The frame chain the pane really has, as a stub: #leftframe holds fused-render's
# own embed shell (no runtime), and the APP is a nested iframe one level deeper
# with `fused` injected. Targeting the outer document described the chrome and
# captured none of the app's logging — found in a browser, pinned here.
_FRAME_CHAIN = """
function node(tag, text, kids) {
  return {tagName: tag, id: "", className: "", children: kids || [],
          childNodes: [{nodeType: 3, nodeValue: text || ""}], textContent: text || ""};
}
function win(spec) {
  const w = {console: {error(){}, warn(){}}, addEventListener(){}, calls: [],
             location: {href: spec.href || "http://x/", search: spec.search || "",
                        pathname: spec.pathname || "/"}};
  w.document = {title: spec.title || "", __fusedAppWatched: false,
                body: spec.body || node("BODY"),
                querySelectorAll: () => (spec.frames || []).map((f) => ({contentWindow: f}))};
  if (spec.runtime) w.fused = {params: {getAll: () => ({})}};
  w.console.error = function (...a) { w.calls.push(a.join(" ")); };
  w.console.warn = function (...a) { w.calls.push(a.join(" ")); };
  return w;
}
const app = win({runtime: true, title: "", href: "http://x/render?path=/p/index.html"});
const shell = win({title: "index.html — Fused Render", frames: [app]});
"""

_FRAME_FNS = ["function reachableFrames(", "function hasRuntime(",
              "function appFrameOf(", "const APP_FRAME_MAX_DEPTH",
              "function resolveAppWindow("]


def test_the_app_is_the_innermost_frame_not_the_pane_itself(html):
    """/embed/<app> is the viewer's shell; the app is one iframe deeper. Reading
    the outer document reported `title: "index.html — Fused Render"` and a DOM
    outline of fused-render's own chrome."""
    got = _node(_FRAME_FNS, _FRAME_CHAIN
                + "const r = resolveAppWindow(shell);"
                "console.log(JSON.stringify({resolved: r.resolved,"
                " isApp: r.win === app, title: r.win.document.title}));", html)
    assert got == {"resolved": True, "isApp": True, "title": ""}


def test_the_descent_stops_at_the_app_even_when_the_app_has_frames_of_its_own(html):
    """`fused` (the injected runtime) is the tell, so an app that embeds an
    iframe is described as itself rather than as whatever it embeds."""
    got = _node(_FRAME_FNS, _FRAME_CHAIN
                + "const widget = win({runtime: true, title: 'widget'});"
                "const inner = win({runtime: true, title: 'the app',"
                " frames: [widget]});"
                "const outer = win({title: 'shell', frames: [inner]});"
                "console.log(JSON.stringify(resolveAppWindow(outer)"
                ".win.document.title));", html)
    assert got == "the app"


def test_an_unreachable_app_frame_is_reported_not_papered_over(html):
    """A cross-origin child (an exported page in the pane) throws on `.document`.
    The descent must degrade to "I could not reach the app" rather than to a
    confident description of the chrome."""
    got = _node(_FRAME_FNS, _FRAME_CHAIN + """
    const blocked = {get document() { throw new Error("cross-origin"); }};
    const outer = win({title: "shell", frames: [blocked]});
    console.log(JSON.stringify(resolveAppWindow(outer).resolved));
    """, html)
    assert got is False


def test_the_descent_refuses_to_guess_between_several_plain_frames(html):
    """Two candidate frames and no runtime in either: picking one would risk
    describing a widget as the whole app."""
    got = _node(_FRAME_FNS, _FRAME_CHAIN
                + "const outer = win({title: 'shell', frames:"
                " [win({title: 'a'}), win({title: 'b'})]});"
                "console.log(JSON.stringify(resolveAppWindow(outer)"
                ".win.document.title));", html)
    assert got == "shell"


def test_instrumentation_targets_the_apps_window_not_the_shells(html):
    """The other half of the same defect: the console wrapper went onto the
    shell, so nothing the app logged was ever seen. Asserted by logging into
    BOTH windows and checking which one the buffer heard."""
    got = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const appLogs", "function clipText(", "function pushAppLog(",
                 "function fmtLogArg(", "function watchWindow("],
                _FRAME_CHAIN + """
                watchWindow(app);
                app.console.warn("from the app");
                shell.console.warn("from the chrome");
                console.log(JSON.stringify(appLogs.map((e) => e.text)));
                """, html)
    assert got == ["from the app"]


def test_the_wrapper_calls_through_so_the_apps_own_logging_still_happens(html):
    got = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const appLogs", "function clipText(", "function pushAppLog(",
                 "function fmtLogArg(", "function watchWindow("],
                _FRAME_CHAIN + """
                watchWindow(app);
                app.console.error("boom");
                console.log(JSON.stringify(app.calls));
                """, html)
    assert got == ["boom"]


def test_a_document_is_only_wrapped_once_but_a_new_one_is_wrapped_again(html):
    """The re-wrap IS the reload detector: a same-origin navigation keeps the
    window and replaces its document, so the marker lives on the document."""
    got = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const appLogs", "function clipText(", "function pushAppLog(",
                 "function fmtLogArg(", "function watchWindow("],
                _FRAME_CHAIN + """
                const first = watchWindow(app);
                const again = watchWindow(app);
                app.document = {__fusedAppWatched: false, querySelectorAll: () => []};
                const afterReload = watchWindow(app);
                console.log(JSON.stringify([first, again, afterReload]));
                """, html)
    assert got == [True, False, True]


_SNAPSHOT_FNS = ["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const APP_STATE_MAX_NODES", "const APP_STATE_MAX_DEPTH",
                 "const APP_STATE_MAX_NODE_TEXT", "const appLogs",
                 "let appEntry", "function clipText(",
                 "function searchParamsOf(", "const CHAT_PARAMS",
                 "function appParamsOf(", "const QUIET_TAGS",
                 "function outlineNode("] + _FRAME_FNS + [
                 "function appWindow(", "function appStateSnapshot("]


def test_the_snapshot_describes_the_app_it_resolved(html):
    """The whole snapshot over the real frame shape: shell outside, app inside."""
    got = _node(_SNAPSHOT_FNS, _FRAME_CHAIN + """
    const app2 = win({runtime: true, title: "Widget dashboard",
                      pathname: "/render", search: "?path=/p/index.html&zoom=4",
                      body: node("BODY", "", [node("H1", "Widget dashboard")])});
    const shell2 = win({title: "index.html — Fused Render", frames: [app2]});
    const leftframe = {contentWindow: shell2};
    appEntry = "/p/index.html";
    console.log(JSON.stringify(appStateSnapshot()));
    """, html)
    assert got["title"] == "Widget dashboard"          # not the viewer's title
    assert got["dom"]["children"][0]["text"] == "Widget dashboard"
    assert got["url"] == "/render?path=/p/index.html&zoom=4"
    assert "unreadable" not in got


def test_an_unresolved_frame_reports_nothing_rather_than_the_chrome(html):
    """The failure mode that shipped: describing fused-render's own UI as the
    user's app. With no app frame to reach, the snapshot must say so and
    describe NO title and NO DOM — a confident wrong answer is the worst one."""
    got = _node(_SNAPSHOT_FNS, _FRAME_CHAIN + """
    const shell2 = win({title: "index.html — Fused Render",
                        body: node("BODY", "", [node("DIV", "breadcrumb")]),
                        pathname: "/embed/p/index.html", search: "?zoom=9"});
    const leftframe = {contentWindow: shell2};
    console.log(JSON.stringify(appStateSnapshot()));
    """, html)
    assert "could not be reached" in got["unreadable"]
    assert "title" not in got and "dom" not in got
    # ...and NO url or params either. These were the leak that survived the first
    # fix: `url` was gated only on having *a* window, so the shell's own address
    # and query string were reported as the app's, beside a note saying the app
    # could not be read. A smaller field carrying the same lie is still the lie.
    assert "url" not in got, got
    assert "params" not in got, got


def test_the_watcher_wires_the_descent_to_the_instrumentation(html):
    """End to end over the stub chain: the thing the timer calls must resolve the
    app and wrap THAT — and a replaced document must be re-wrapped and counted,
    since a live-reload navigates the inner frame and fires no outer `load`."""
    got = _node(["const APP_STATE_MAX_LOGS", "const APP_STATE_MAX_TEXT",
                 "const appLogs", "let appLoads", "function clipText(",
                 "function pushAppLog(", "function fmtLogArg("] + _FRAME_FNS
                + ["function appWindow(", "function watchWindow(",
                   "function watchApp("],
                _FRAME_CHAIN + """
                const leftframe = {contentWindow: shell};
                watchApp(); watchApp();
                app.console.warn("before the edit");
                app.document = {__fusedAppWatched: false, querySelectorAll: () => []};
                watchApp();
                app.console.warn("after the edit");
                console.log(JSON.stringify({loads: appLoads,
                  log: appLogs.map((e) => e.level + ":" + e.text)}));
                """, html)
    # Two documents seen, the reload marked between them, and the app's own
    # logging captured on both sides of it.
    assert got["loads"] == 2
    assert got["log"][0] == "warn:before the edit"
    assert got["log"][1].startswith("reload:")
    assert got["log"][2] == "warn:after the edit"


def test_the_chats_own_params_are_not_reported_as_the_apps(html):
    """The pane sets no param boundary, so the child's runtime reads THIS page's
    URL as its ancestor and its getAll() comes back carrying our bookkeeping.
    Reporting `session_id` and `split` as the app's params would have the model
    reasoning about state that belongs to the chat around it."""
    got = _node(["function clipText(", "const CHAT_PARAMS", "function appParamsOf("],
                "console.log(JSON.stringify(appParamsOf({_file: '/p', _mode: "
                "'_render', session_id: 'abc', run: 'r', split: '70', model: "
                "'sonnet', effort: 'high', permission: 'auto', zoom: '4'})));",
                html)
    assert got == {"zoom": "4"}


def test_child_params_fall_back_to_parsing_its_query_string(html):
    """The child normally answers with its own `fused.params.getAll()`; a child
    that has not booted its runtime yet still has a URL."""
    got = _node(["function searchParamsOf("],
                "console.log(JSON.stringify(searchParamsOf('?a=1&b=two')));", html)
    assert got == {"a": "1", "b": "two"}


def test_the_outline_lists_a_script_without_quoting_its_source(html):
    """Measured against a real app: the outline came back carrying the first 120
    chars of its own inline script. A structural outline is not a place for code
    fragments — the agent can read the file."""
    stub = """
    function node(tag, text, kids) {
      return {tagName: tag, id: "", className: "", children: kids || [],
              childNodes: [{nodeType: 3, nodeValue: text}], textContent: text};
    }
    const body = node("BODY", "", [node("P", "hello"),
                                   node("SCRIPT", "console.warn('secret sauce')")]);
    """
    got = _node(["const APP_STATE_MAX_NODES", "const APP_STATE_MAX_DEPTH",
                 "const APP_STATE_MAX_NODE_TEXT", "const QUIET_TAGS",
                 "function clipText(", "function outlineNode("],
                stub + "console.log(JSON.stringify(outlineNode(body, 0)));", html)
    tags = [k["tag"] for k in got["children"]]
    assert tags == ["p", "script"]           # still listed
    assert got["children"][0]["text"] == "hello"
    assert "text" not in got["children"][1]  # never quoted

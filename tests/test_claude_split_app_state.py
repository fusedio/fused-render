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
    allowed = cmd[cmd.index("--allowed-tools") + 1].split(",")
    assert tool in allowed
    # The only OTHER pre-allowance is reading an annotation's screenshot, and it
    # is scoped to the one directory those live in (test_claude_split_shots.py).
    assert allowed == [tool, agent._read_rule(agent.SHOTS)]
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
    empty = _node(["const APP_STATE_TAG", "function appStateBlock("],
                  "console.log(JSON.stringify(appStateBlock(null)));", html)
    assert empty == ""


def test_the_state_block_labels_itself_and_carries_the_json(html):
    block = _node(["const APP_STATE_TAG", "function appStateBlock("],
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
                " _file: '/p', _mode: 'claude_split', path: '/p/index.html',"
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
             "function formatAnnotations(", "function composeOutgoing(",
             "function stripAppStateBlock(", "function stripBlocks(",
             # The pane shot is a third block on the same wire (see
             # test_claude_split_shots.py); these two are what stripBlocks needs to
             # be its exact inverse, whether or not a given message carries one.
             "const PANE_SHOT_TAG", "function paneShotBlock(",
             "function stripPaneBlock(",
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


# --------------------------------------------------- Escape has two claimants

def test_the_annotation_composer_wins_escape_over_stopping_the_run(html):
    """Both features bind Escape in this pane. Dismissing a popover is small and
    repeatable; killing a live turn is neither — so an Escape pressed with a text
    box open means the text box."""
    def act(open_, run):
        return _node(["function escapeAction("],
                     "console.log(JSON.stringify(escapeAction(%s, %s)));"
                     % (json.dumps(open_), json.dumps(run)), html)

    assert act(True, "run-7") == "close-composer"
    assert act(True, None) == "close-composer"
    assert act(False, "run-7") == "stop-run"
    # Inert otherwise: this page is in an iframe and must not swallow the shell's
    # Escape for nothing.
    assert act(False, None) == ""


def test_the_composers_escape_does_not_also_reach_the_run_killer(html):
    """The textarea's handler runs first and hides the popover, so without a
    stopPropagation the document binding would look at an already-closed
    composer and end the turn as well."""
    start = html.index("annTa.addEventListener(\"keydown\"")
    head = html[start:start + 400]
    assert "stopPropagation" in head, head

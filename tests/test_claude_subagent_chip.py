"""Node-probe tests for the `Task` chip's spawned-subagent rendering.

agent.py's `_attach_subagents` (tests/test_claude_subagents.py) puts a
`subagent` object on a `Task` tool segment — agentType, description, model,
spawnDepth, agentId, and (while running) a `progress` snapshot. This file
covers the RENDERER for that: the status badge in the chip's body
(`subagentStatusLine`, `formatElapsed`), the fetch of the subagent's own
transcript while its chip is open (`pumpSubagentLog`, the one entry point
both callers — the poll tick and the "toggle" listener — go through), and
rendering that transcript through the SAME segment machinery the main
conversation uses (`renderSubagentTranscript` calling `renderSegments`) —
which is also what makes a NESTED subagent (spawnDepth > 1) work with no
extra code.

Design, after four review rounds each found a HIGH in some corner of "is
this cached copy trustworthy": there is no such question any more.
`pumpSubagentLog` re-fetches on every poll tick while the chip is open and
renders whatever comes back, live; `subagentCache` survives only so a
re-open (or the very first tick) doesn't show a blank frame while the fetch
resolves, and is never consulted to decide what the UI's state IS. The one
exception — skip re-fetching once the Task is terminal AND a fetch has
already returned a real, non-empty transcript — is a single fact about data
already in hand, not a trust judgement, so getting it wrong costs one
harmless extra fetch rather than a wrong render.

Same harness as tests/test_claude_template_segments.py (same `_DOM`, same
anchors): read that file's module docstring for why node-and-textual-anchors
rather than a browser. The one addition here is a `fused` stub — `params.get`
and an async `runPython` whose answers a test controls and whose calls a test
can inspect — since this feature is the first of these chip renderers to make
a network call.
"""
import json
import os
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "claude",
    "template.html")


@pytest.fixture(scope="module")
def source():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


def _block(src, start, end):
    i = src.index(start)
    j = src.index(end, i) + len(end)
    return src[i:j]


_PERM_START = "function formatEditDiff(input) {"
_PERM_END = ("  return rest.length ? Object.fromEntries(rest.map((k) => "
             "[k, input[k]])) : null;\n}")
_SEG_START = "const TOOL_STATUS_GLYPH = {"
_SEG_END = "  return tail >= 0 ? segText(list[tail]) : null;\n}"
_TOOL_NAMES_START = 'const PLAN_TOOL = "ExitPlanMode";'
_TOOL_NAMES_END = 'const ANSWERABLE_TOOL = "AskUserQuestion";'

# Same stub as test_claude_template_segments.py's `_DOM` (copied rather than
# imported: these are textual source fragments handed to node, not python).
_DOM = r"""
let _uid = 0;
let _focused = null;
function textNode(s) {
  const t = {nodeType: 3, nodeValue: String(s), parentElement: null};
  Object.defineProperty(t, "textContent", {get: () => t.nodeValue});
  return t;
}
function makeEl(tag) {
  const e = {
    uid: ++_uid, tagName: String(tag).toUpperCase(), nodeType: 1,
    className: "", children: [], parentElement: null, open: false,
    _text: "", _html: null, attrs: {}, src: undefined, alt: undefined,
    style: {}, scrollHeight: 0, offsetHeight: 0, clientHeight: 0, scrollTop: 0,
    scrolledIntoView: null,
    scrollIntoView(opt) { e.scrolledIntoView = opt === undefined ? {} : opt; },
    appendChild(n) {
      if (n.parentElement) n.parentElement.removeChild(n);
      n.parentElement = e; e.children.push(n); return n;
    },
    append(...nodes) {
      nodes.forEach((n) => e.appendChild(typeof n === "string" ? textNode(n) : n));
    },
    removeChild(n) {
      const i = e.children.indexOf(n);
      if (i >= 0) e.children.splice(i, 1);
      n.parentElement = null; return n;
    },
    remove() { if (e.parentElement) e.parentElement.removeChild(e); },
    replaceWith(n) { e.after(n); e.remove(); },
    after(n) {
      const p = e.parentElement;
      if (!p) return;
      if (n.parentElement) n.parentElement.removeChild(n);
      p.children.splice(p.children.indexOf(e) + 1, 0, n);
      n.parentElement = p;
    },
    replaceChildren(...nodes) {
      e.children.slice().forEach((k) => e.removeChild(k));
      e._text = ""; e._html = null;
      nodes.forEach((n) => e.appendChild(typeof n === "string" ? textNode(n) : n));
    },
    focus() { _focused = e; },
    setAttribute(k, v) { e.attrs[String(k)] = String(v); },
    getAttribute(k) { return k in e.attrs ? e.attrs[k] : null; },
    _on: {},
    addEventListener(type, fn) { (e._on[type] = e._on[type] || []).push(fn); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
  Object.defineProperty(e, "textContent", {
    get: () => e._text + e.children.map((k) => k.textContent).join(""),
    set: (v) => {
      e.children.slice().forEach((k) => e.removeChild(k));
      e._html = null; e._text = String(v);
    },
  });
  Object.defineProperty(e, "innerHTML", {
    get: () => (e._html === null ? "" : e._html),
    set: (v) => {
      e.children.slice().forEach((k) => e.removeChild(k));
      e._text = ""; e._html = String(v);
    },
  });
  Object.defineProperty(e, "firstChild", {get: () => e.children[0] || null});
  Object.defineProperty(e, "lastChild",
                        {get: () => e.children[e.children.length - 1] || null});
  e.classList = {
    add(...cs) {
      const have = e.className ? e.className.split(/\s+/) : [];
      cs.forEach((c) => { if (have.indexOf(c) < 0) have.push(c); });
      e.className = have.join(" ");
    },
    remove(...cs) {
      e.className = (e.className ? e.className.split(/\s+/) : [])
        .filter((c) => cs.indexOf(c) < 0).join(" ");
    },
    contains(c) { return (e.className ? e.className.split(/\s+/) : []).indexOf(c) >= 0; },
    toggle(c, on) { if (on) e.classList.add(c); else e.classList.remove(c); },
  };
  return e;
}
const document = {createElement: makeEl, createTextNode: textNode};
function dump(n) {
  if (n.nodeType === 3) return {tag: "#text", text: n.nodeValue};
  return {
    uid: n.uid, tag: n.tagName.toLowerCase(), cls: n.className,
    text: n.textContent, html: n.innerHTML, open: !!n.open,
    src: n.src === undefined ? null : n.src, attrs: n.attrs,
    children: n.children.map(dump),
  };
}
function flat(d) {
  const out = [d];
  (d.children || []).forEach((k) => out.push(...flat(k)));
  return out;
}
// Fires the stored click handlers, then flips `open` — same order a browser
// runs its default action in — and NOW fires "toggle" (this stub's one
// simplification: real browsers fire it as a separate microtask, but nothing
// here depends on that ordering relative to anything else in the same tick).
function clickSummary(det) {
  const head = det.children.find((k) => k.tagName === "SUMMARY");
  (head._on.click || []).forEach((fn) => fn({}));
  det.open = !det.open;
  (det._on.toggle || []).forEach((fn) => fn({}));
}
"""

_STUBS = r"""
const mdCalls = [];
const renderMd = (t) => { mdCalls.push(t); return "<md>" + t + "</md>"; };
const attachCalls = [];
const attachCodeCopy = (el) => { attachCalls.push(el.uid); };

// The one new external dependency this feature adds: a fetch. `runPythonLog`
// records every call so a test can assert de-duplication; `runPythonQueue`
// lets a test script each call's answer (default: no turns). A queued item
// of `{__defer: true, value}` resolves LATER, on demand — for a test that
// needs to force something (a rebuild, another call) to happen BETWEEN a
// fetch starting and it resolving — via `pendingResolvers`, in call order.
// A queued item of `{__reject: true, message}` rejects instead, for the
// error-handling probes.
const runPythonLog = [];
const runPythonQueue = [];
const pendingResolvers = [];
const AGENT = "./agent.py";
const FILE = "/proj/page.html";
let SESSION_ID = "sess1";
const fused = {
  params: { get: (k) => (k === "session_id" ? SESSION_ID : "") },
  runPython: (agent, params) => {
    runPythonLog.push({agent, params});
    const next = runPythonQueue.length ? runPythonQueue.shift() : {turns: []};
    if (next && next.__defer) {
      return new Promise((resolve, reject) => {
        pendingResolvers.push({resolve, reject, value: next.value});
      });
    }
    if (next && next.__reject) return Promise.reject(new Error(next.message));
    return Promise.resolve(typeof next === "function" ? next(params) : next);
  },
};
"""


def _node(script, tmp_path):
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the template's own JS")
    harness = tmp_path / "harness.mjs"
    harness.write_text(script, encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True,
                          text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture()
def probe_src(source):
    perm = _block(source, _PERM_START, _PERM_END)
    seg = _block(source, _SEG_START, _SEG_END)
    names = _block(source, _TOOL_NAMES_START, _TOOL_NAMES_END)
    return "\n".join([_DOM, _STUBS, names, perm, seg])


def _run(probe_src, body, tmp_path):
    return _node(probe_src + "\n" + body, tmp_path)


def _by_class(tree, cls):
    out = []

    def walk(n):
        if n.get("cls") and cls in n["cls"].split():
            out.append(n)
        for k in n.get("children") or []:
            walk(k)
    walk(tree)
    return out


def _task_seg(subagent=None, status="running", tid="t1", output=None):
    seg = {"kind": "tool", "id": tid, "name": "Task",
           "input": {"description": "Verify suites", "subagent_type": "general-purpose"},
           "status": status, "output": output, "images": []}
    if subagent is not None:
        seg["subagent"] = subagent
    return seg


def _sub(agent_id="a1", agent_type="general-purpose", spawn_depth=1,
         progress=None):
    d = {"agentId": agent_id, "agentType": agent_type,
         "description": "Verify suites", "model": "opus",
         "spawnDepth": spawn_depth}
    if progress is not None:
        d["progress"] = progress
    return d


# ------------------------------------------------------------ pure helpers

def test_format_elapsed_under_a_minute(probe_src, tmp_path):
    got = _run(probe_src, """
console.log(JSON.stringify({
  a: formatElapsed("2026-01-01T00:00:00.000Z",
                    Date.parse("2026-01-01T00:00:07.000Z")),
  b: formatElapsed("", 1000),
  c: formatElapsed("not-a-date", 1000),
}));
""", tmp_path)
    assert got["a"] == "7s"
    assert got["b"] == ""
    assert got["c"] == ""


def test_format_elapsed_minutes_and_hours(probe_src, tmp_path):
    got = _run(probe_src, """
console.log(JSON.stringify({
  m: formatElapsed("2026-01-01T00:00:00.000Z",
                    Date.parse("2026-01-01T00:01:05.000Z")),
  h: formatElapsed("2026-01-01T00:00:00.000Z",
                    Date.parse("2026-01-01T01:02:00.000Z")),
}));
""", tmp_path)
    assert got["m"] == "1m 05s"
    assert got["h"] == "1h 2m"


def test_subagent_status_line_falls_back_to_total_elapsed_with_no_activity_yet(
        probe_src, tmp_path):
    """The very first tick after spawn: nothing has been written, so there is
    no `lastActivityAt` yet and the badge falls back to total time since
    start — worded as a bare duration, not "active ... ago", since it is not
    measuring activity at all here."""
    got = _run(probe_src, """
const seg = %s;
console.log(JSON.stringify({
  s: subagentStatusLine(seg, Date.parse("2026-01-01T00:00:10.000Z")),
}));
""" % json.dumps(_task_seg(subagent=_sub(progress={
        "startedAt": "2026-01-01T00:00:00.000Z", "lastActivityAt": "",
        "lastTool": "Bash"}))), tmp_path)
    assert got["s"] == "Running general-purpose — Bash (10s)"


def test_subagent_status_line_uses_last_activity_when_present(probe_src, tmp_path):
    """The PRIMARY path (code review finding E): once the agent has done
    anything at all, the parenthetical measures time since ITS LAST activity,
    not total runtime — and the wording says so ("active ... ago"), so a
    subagent twenty minutes into a run whose last tool call was two seconds
    ago reads as still alive, not as freshly started."""
    got = _run(probe_src, """
const seg = %s;
console.log(JSON.stringify({
  s: subagentStatusLine(seg, Date.parse("2026-01-01T00:20:02.000Z")),
}));
""" % json.dumps(_task_seg(subagent=_sub(progress={
        "startedAt": "2026-01-01T00:00:00.000Z",
        "lastActivityAt": "2026-01-01T00:20:00.000Z",
        "lastTool": "Grep"}))), tmp_path)
    # 20 minutes of total runtime, but the badge reads off the 2s since the
    # last tool call — and says so, rather than a bare "(2s)" a reader could
    # mistake for "started 2s ago".
    assert got["s"] == "Running general-purpose — Grep (active 2s ago)"


def test_subagent_status_line_done_and_error(probe_src, tmp_path):
    got = _run(probe_src, """
console.log(JSON.stringify({
  ok: subagentStatusLine(%s),
  err: subagentStatusLine(%s),
}));
""" % (json.dumps(_task_seg(subagent=_sub(), status="ok")),
       json.dumps(_task_seg(subagent=_sub(), status="error"))), tmp_path)
    assert got["ok"] == "general-purpose — done"
    assert got["err"] == "general-purpose — failed"


# --------------------------------------------------- the chip: collapsed

def test_a_task_chip_shows_a_status_badge_when_collapsed(probe_src, tmp_path):
    got = _run(probe_src, """
const el = buildToolChip(%s, "k1");
console.log(JSON.stringify({tree: dump(el.el), calls: runPythonLog}));
""" % json.dumps(_task_seg(subagent=_sub())), tmp_path)
    badge = _by_class(got["tree"], "chip-subagent-status")
    assert len(badge) == 1
    assert "general-purpose" in badge[0]["text"]
    # Collapsed by default (registerCard's own policy) — never fetched.
    assert got["calls"] == []


def test_a_task_with_no_subagent_yet_has_no_badge_or_log(probe_src, tmp_path):
    got = _run(probe_src, """
const el = buildToolChip(%s, "k1");
console.log(JSON.stringify({tree: dump(el.el)}));
""" % json.dumps(_task_seg(subagent=None)), tmp_path)
    assert _by_class(got["tree"], "chip-subagent-status") == []
    assert _by_class(got["tree"], "chip-subagent-log") == []


def test_the_depth_cap_hides_the_log_but_keeps_the_badge(probe_src, tmp_path):
    got = _run(probe_src, """
const el = buildToolChip(%s, "k1");
console.log(JSON.stringify({tree: dump(el.el)}));
""" % json.dumps(_task_seg(subagent=_sub(spawn_depth=999))), tmp_path)
    assert len(_by_class(got["tree"], "chip-subagent-status")) == 1
    assert _by_class(got["tree"], "chip-subagent-log") == []


def test_subagent_arriving_a_tick_after_the_bare_task_call_still_gets_a_badge(
        probe_src, tmp_path):
    """agent.py can attach `subagent` a poll AFTER the bare Task call first
    shows up (the sidecar lands once the CLI has spawned it), with `status`
    unchanged the whole time — the chip's rebuild key has to notice anyway."""
    got = _run(probe_src, """
const el = buildToolChip(%s, "k1");
el.update(%s);
console.log(JSON.stringify({tree: dump(el.el)}));
""" % (json.dumps(_task_seg(subagent=None)),
       json.dumps(_task_seg(subagent=_sub()))), tmp_path)
    assert len(_by_class(got["tree"], "chip-subagent-status")) == 1


# ----------------------------------------------------------- expand: fetch

def test_opening_the_chip_fetches_the_subagents_transcript(probe_src, tmp_path):
    got = _run(probe_src, """
async function main() {
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);  // closed -> open
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({calls: runPythonLog, open: el.el.open}));
}
main();
""" % json.dumps(_task_seg(subagent=_sub(agent_id="a7"))), tmp_path)
    assert got["open"] is True
    assert len(got["calls"]) == 1
    call = got["calls"][0]
    assert call["params"]["action"] == "subagent"
    assert call["params"]["agent_id"] == "a7"
    assert call["params"]["session_id"] == "sess1"
    assert call["params"]["file"] == "/proj/page.html"


def test_a_collapsed_chip_never_fetches_even_when_updated_repeatedly(
        probe_src, tmp_path):
    got = _run(probe_src, """
const el = buildToolChip(%s, "k1");
el.update(%s);
el.update(%s);
console.log(JSON.stringify({calls: runPythonLog}));
""" % (json.dumps(_task_seg(subagent=_sub())),
       json.dumps(_task_seg(subagent=_sub())),
       json.dumps(_task_seg(subagent=_sub()))), tmp_path)
    assert got["calls"] == []


def test_a_running_open_chip_refetches_on_the_existing_poll_tick(
        probe_src, tmp_path):
    """No second timer: `update()` — the function the main 400 ms poll already
    calls for every segment — is what re-fetches a RUNNING subagent's log
    while its chip is open."""
    got = _run(probe_src, """
async function main() {
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  await new Promise((r) => setTimeout(r, 0));   // let tick 1's fetch resolve
  el.update(%s);   // tick 2, still running
  await new Promise((r) => setTimeout(r, 0));
  el.update(%s);   // tick 3, still running
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({calls: runPythonLog.length}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub())),
       json.dumps(_task_seg(subagent=_sub())),
       json.dumps(_task_seg(subagent=_sub()))), tmp_path)
    # One fetch per tick while open+running, each awaited out before the next
    # poll would naturally arrive (~400ms later in reality) — the toggle fired
    # the first, each subsequent update() (a fresh poll) fires one more.
    assert got["calls"] == 3


def test_a_terminal_fetch_still_retrieves_the_agents_final_message(
        probe_src, tmp_path):
    """Code review round 7, HIGH 1: the previous stop condition ("any cached
    content at all, current status terminal") froze the transcript at
    whatever a RUNNING-status fetch last returned. The tick right after the
    Task goes terminal — the one carrying the agent's own concluding
    message — hit that early return and the message was never retrieved,
    permanently. A fetch made while still running can never prove there is
    nothing left to say; only a fetch made AT the terminal status can."""
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({turns: [{role: "user", text: "early"}]});
  runPythonQueue.push({turns: [
    {role: "user", text: "early"},
    {role: "assistant", text: "", segments: [{kind: "text", text: "FINAL ANSWER"}]},
  ]});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  await new Promise((r) => setTimeout(r, 0));      // fetch #1: cached while "running"
  el.update(%s);                                    // Task goes terminal
  await new Promise((r) => setTimeout(r, 0));       // fetch #2 MUST happen
  console.log(JSON.stringify({calls: runPythonLog.length, tree: dump(el.el)}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(), status="running")),
       json.dumps(_task_seg(subagent=_sub(), status="ok"))), tmp_path)
    assert got["calls"] == 2
    assistants = _by_class(got["tree"], "sub-turn-assistant")
    assert len(assistants) == 1
    seg_text = _by_class(assistants[0], "seg-text")
    assert len(seg_text) == 1 and seg_text[0]["html"] == "<md>FINAL ANSWER</md>"


def test_a_re_open_after_the_final_terminal_fetch_does_not_refetch(
        probe_src, tmp_path):
    """The flip side of the fix above: once a fetch that ITSELF started at a
    terminal status has come back with real content, that copy is final —
    a later tick (or re-open) must not go back to the wire for it again."""
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({turns: [{role: "user", text: "early"}]});
  runPythonQueue.push({turns: [{role: "user", text: "final"}]});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  await new Promise((r) => setTimeout(r, 0));       // fetch #1: "running"
  el.update(%s);                                     // terminal
  await new Promise((r) => setTimeout(r, 0));        // fetch #2: terminal, real -> final
  el.update(%s);                                     // still terminal
  await new Promise((r) => setTimeout(r, 0));        // must NOT fetch again
  console.log(JSON.stringify({calls: runPythonLog.length, tree: dump(el.el)}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(), status="running")),
       json.dumps(_task_seg(subagent=_sub(), status="ok")),
       json.dumps(_task_seg(subagent=_sub(), status="ok"))), tmp_path)
    assert got["calls"] == 2
    users = _by_class(got["tree"], "sub-turn-user")
    assert len(users) == 1 and users[0]["text"] == "final"


def test_a_terminal_tick_still_fetches_with_nothing_usable_in_hand_yet(
        probe_src, tmp_path):
    """The flip side: with no non-empty transcript cached yet (the first
    fetch was empty, e.g. the agent hadn't written anything when it was
    asked), a later tick at a terminal status must still go back to the
    wire — "stop fetching" is conditioned on having something real, not on
    having reached a terminal status at all."""
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({turns: []});
  runPythonQueue.push({turns: [{role: "user", text: "arrived late"}]});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                              // fetch #1: empty, "running"
  await new Promise((r) => setTimeout(r, 0));
  el.update(%s);                                     // now "ok"
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({calls: runPythonLog.length, tree: dump(el.el)}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(), status="running")),
       json.dumps(_task_seg(subagent=_sub(), status="ok"))), tmp_path)
    assert got["calls"] == 2
    users = _by_class(got["tree"], "sub-turn-user")
    assert len(users) == 1 and users[0]["text"] == "arrived late"


def test_two_concurrent_opens_of_the_same_agent_do_not_double_fetch(
        probe_src, tmp_path):
    got = _run(probe_src, """
const el = buildToolChip(%s, "k1");
clickSummary(el.el);   // open -> queues a fetch
clickSummary(el.el);   // close
clickSummary(el.el);   // open again before the first fetch resolved
console.log(JSON.stringify({calls: runPythonLog.length}));
""" % json.dumps(_task_seg(subagent=_sub())), tmp_path)
    assert got["calls"] == 1


# ------------------------------------------------- the transcript itself

def test_the_fetched_turns_render_through_the_shared_segment_machinery(
        probe_src, tmp_path):
    """Not a bespoke renderer: an assistant turn's segments go through
    `renderSegments`, so a subagent's own Bash/Edit calls come out as the
    SAME tool chips the main conversation uses."""
    payload = {"turns": [
        {"role": "user", "text": "go check it"},
        {"role": "assistant", "text": "", "segments": [
            {"kind": "text", "text": "Checking now."},
            {"kind": "tool", "id": "x1", "name": "Bash",
             "input": {"command": "pytest"}, "status": "ok", "output": "1 passed",
             "images": []},
        ]},
    ]}
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push(%s);
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el)}));
}
main();
""" % (json.dumps(payload), json.dumps(_task_seg(subagent=_sub(agent_id="a1")))),
       tmp_path)
    log = _by_class(got["tree"], "chip-subagent-log")[0]
    users = _by_class(got["tree"], "sub-turn-user")
    assert len(users) == 1 and users[0]["text"] == "go check it"
    nested = _by_class(log, "toolchip")
    assert len(nested) == 1
    assert "Bash" in nested[0]["text"] or "bash" in nested[0]["text"].lower()


def test_a_nested_task_inside_a_fetched_transcript_gets_its_own_expand(
        probe_src, tmp_path):
    """spawnDepth > 1: the fetched turns can themselves contain a `Task`
    segment with its OWN `subagent` field (agent.py attaches it the same
    way). Reusing buildToolChip for it is what makes this work with no
    recursive rendering code of its own."""
    nested_sub = _sub(agent_id="a2", agent_type="code-reviewer", spawn_depth=2)
    payload = {"turns": [
        {"role": "assistant", "text": "", "segments": [
            {"kind": "tool", "id": "tu2", "name": "Task",
             "input": {"description": "nested review", "subagent_type": "code-reviewer"},
             "status": "running", "output": None, "images": [],
             "subagent": nested_sub},
        ]},
    ]}
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push(%s);
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el)}));
}
main();
""" % (json.dumps(payload), json.dumps(_task_seg(subagent=_sub(agent_id="a1")))),
       tmp_path)
    nested_badges = _by_class(got["tree"], "chip-subagent-status")
    assert any("code-reviewer" in b["text"] for b in nested_badges)


def test_renderSubagentTranscript_updates_in_place_across_two_calls(
        probe_src, tmp_path):
    """The container is a fixed number of turn slots reused across polls
    (same idea as renderSegments), not rebuilt from scratch each time — a
    growing subagent transcript must not lose the reader's place."""
    got = _run(probe_src, """
const container = document.createElement("div");
renderSubagentTranscript(container, [{role: "user", text: "go"}]);
const firstUid = container.children[0].uid;
renderSubagentTranscript(container, [
  {role: "user", text: "go"},
  {role: "assistant", text: "", segments: [{kind: "text", text: "done"}]},
]);
console.log(JSON.stringify({
  n: container.children.length,
  sameUid: container.children[0].uid === firstUid,
  firstText: container.children[0].textContent,
}));
""", tmp_path)
    assert got["n"] == 2
    assert got["sameUid"] is True
    assert got["firstText"] == "go"


# ------------------------------------------------- rebuild mid-fetch (#2)

def test_a_rebuild_mid_fetch_lands_the_result_in_the_current_log(probe_src, tmp_path):
    """A fetch started on one tick can resolve after a LATER tick has already
    torn the body down and built a fresh `.chip-subagent-log` (here: the
    Task's own `output` changing while still running, the same kind of event
    as a status flip) — the result must land in whatever log element is
    CURRENTLY on screen, not the one that was live when the fetch began.
    Under the bug this fixes, `getLogEl`/`getSeg` were plain values captured
    at call time, so the resolved data wrote into an orphaned node and the
    visible (rebuilt) log stayed empty forever."""
    payload = {"turns": [{"role": "user", "text": "hello from the agent"}]}
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({__defer: true, value: %s});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                          // fetch #1 starts, deferred
  await new Promise((r) => setTimeout(r, 0));
  el.update(%s);                                 // output changed -> rebuild -> fresh log
  const p = pendingResolvers.shift();
  p.resolve(p.value);                            // resolves AFTER the rebuild
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el)}));
}
main();
""" % (json.dumps(payload), json.dumps(_task_seg(subagent=_sub())),
       json.dumps(_task_seg(subagent=_sub(), output="partial"))), tmp_path)
    users = _by_class(got["tree"], "sub-turn-user")
    assert len(users) == 1 and users[0]["text"] == "hello from the agent"


# ------------------------------------------------- error handling (#3)

def test_a_failed_fetch_does_not_throw_and_can_retry(probe_src, tmp_path):
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({__reject: true, message: "boom"});
  runPythonQueue.push({turns: [{role: "user", text: "recovered"}]});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                          // fetch #1: rejects
  await new Promise((r) => setTimeout(r, 0));
  el.update(%s);                                 // still running: tick retries
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el), calls: runPythonLog.length}));
}
main().catch((err) => { console.log(JSON.stringify({threw: String(err)})); });
""" % (json.dumps(_task_seg(subagent=_sub())),
       json.dumps(_task_seg(subagent=_sub()))), tmp_path)
    assert "threw" not in got, got
    assert got["calls"] == 2
    users = _by_class(got["tree"], "sub-turn-user")
    assert len(users) == 1 and users[0]["text"] == "recovered"


def test_repeated_failures_stop_after_a_bound_and_show_something(
        probe_src, tmp_path):
    """A server error or an executor timeout must not become an unbounded
    400 ms retry storm. After enough consecutive failures the chip gives up
    and says so, instead of trying forever."""
    for _ in range(10):
        pass  # queued below; a plain loop keeps the failures explicit
    got = _run(probe_src, """
async function main() {
  for (let i = 0; i < 10; i++) runPythonQueue.push({__reject: true, message: "down"});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  for (let i = 0; i < 10; i++) {
    await new Promise((r) => setTimeout(r, 0));
    el.update(%s);
  }
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el), calls: runPythonLog.length}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub())),
       json.dumps(_task_seg(subagent=_sub()))), tmp_path)
    assert got["calls"] <= 6, "retries must be bounded, not unbounded"
    errors = _by_class(got["tree"], "sub-turn-error")
    assert len(errors) == 1 and "down" in errors[0]["text"]


def test_a_single_failure_paints_immediately_not_only_at_the_bound(
        probe_src, tmp_path):
    """Code review round 7, HIGH 2: the give-up message used to paint ONLY
    once the fail count reached MAX_SUBAGENT_FETCH_ATTEMPTS. A restored or
    finished conversation never polls again — there is no external tick
    left to drive a fail count from 1 up to the bound — so a single failed
    fetch (nowhere near the bound) used to leave the chip permanently blank
    with nothing anywhere to explain it. It must explain itself the moment
    there is nothing else worth showing, on the very first failure."""
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({__reject: true, message: "down"});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el), calls: runPythonLog.length}));
}
main();
""" % json.dumps(_task_seg(subagent=_sub(agent_id="a1"))), tmp_path)
    assert got["calls"] == 1
    errors = _by_class(got["tree"], "sub-turn-error")
    assert len(errors) == 1 and "down" in errors[0]["text"]


def test_repeated_close_reopen_cycles_each_paint_their_own_failure(
        probe_src, tmp_path):
    """Code review round 7, HIGH 2 (the reviewer's probe scenario):
    `reexpand` resets the fail count to 0 on every open, so a person
    closing and re-opening a chip that keeps failing gets exactly ONE
    attempt per cycle — the bound (5) is structurally unreachable that way.
    Before the fix, painting only "at the bound" meant NONE of those single
    attempts ever explained themselves: ten rejections across four
    close/re-open cycles left the chip permanently, silently empty. Each
    cycle's own failure must now be visible."""
    got = _run(probe_src, """
async function main() {
  const seen = [];
  const el = buildToolChip(%s, "k1");
  for (let i = 0; i < 4; i++) {
    runPythonQueue.push({__reject: true, message: "down " + i});
    clickSummary(el.el);              // open (reexpand resets the fail count)
    await new Promise((r) => setTimeout(r, 0));
    seen.push(dump(el.el));
    clickSummary(el.el);              // close, ready for the next cycle
    await new Promise((r) => setTimeout(r, 0));
  }
  console.log(JSON.stringify({seen, calls: runPythonLog.length}));
}
main();
""" % json.dumps(_task_seg(subagent=_sub(agent_id="a1"))), tmp_path)
    assert got["calls"] == 4
    for tree in got["seen"]:
        errors = _by_class(tree, "sub-turn-error")
        assert len(errors) == 1 and errors[0]["text"], (
            "each independent close/re-open cycle must show its OWN failure")


def test_a_success_after_earlier_failures_clears_the_failure_state(
        probe_src, tmp_path):
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({__reject: true, message: "flaky"});
  runPythonQueue.push({turns: [{role: "user", text: "ok now"}]});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  await new Promise((r) => setTimeout(r, 0));
  el.update(%s);
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({
    failCount: subagentFailCount.get("a1") || 0,
  }));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="a1"))),
       json.dumps(_task_seg(subagent=_sub(agent_id="a1")))), tmp_path)
    assert got["failCount"] == 0


# ------------------------------------------------- give-up recovery (#A)

def test_reexpanding_after_giving_up_gets_a_fresh_chance(probe_src, tmp_path):
    """Code review finding A: `subagentFailCount` is cumulative over the
    page's whole lifetime, not "this burst" — five rejections spread over
    hours (a dev-server restart, an executor recycle) must not permanently
    kill a subagent's transcript. Collapsing and re-expanding the chip is
    the recovery gesture; it must reach a real fetch again, not stay stuck
    behind the old count."""
    got = _run(probe_src, """
async function main() {
  for (let i = 0; i < 5; i++) runPythonQueue.push({__reject: true, message: "down"});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);              // open -> fetch #1 fails
  for (let i = 0; i < 5; i++) {
    await new Promise((r) => setTimeout(r, 0));
    el.update(%s);                   // ticks 2..5: each fails, hits the bound
  }
  await new Promise((r) => setTimeout(r, 0));
  const failedBefore = subagentFailCount.get("a1") || 0;
  clickSummary(el.el);              // close
  runPythonQueue.push({turns: [{role: "user", text: "recovered"}]});
  clickSummary(el.el);              // re-open: reexpand:true clears the count
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({
    failedBefore,
    bound: MAX_SUBAGENT_FETCH_ATTEMPTS,
    failedAfter: subagentFailCount.get("a1") || 0,
    tree: dump(el.el),
  }));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="a1"))),
       json.dumps(_task_seg(subagent=_sub(agent_id="a1")))), tmp_path)
    assert got["failedBefore"] >= got["bound"]
    assert got["failedAfter"] == 0
    users = _by_class(got["tree"], "sub-turn-user")
    assert len(users) == 1 and users[0]["text"] == "recovered"


# ------------------------------------------------- paint on give-up (#B)

def test_the_give_up_message_paints_from_the_final_failed_attempt(
        probe_src, tmp_path):
    """A tighter version of the same finding: NOTHING calls `update()` (or
    anything else) after the failure that trips the bound. If the paint
    only happened on a LATER tick noticing the count was already over the
    line — rather than from inside the failing call itself — this would
    show a blank log forever, which is exactly finding B."""
    got = _run(probe_src, """
async function main() {
  for (let i = 0; i < 5; i++) runPythonQueue.push({__reject: true, message: "final straw"});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                    // failure 1 of 5
  for (let i = 0; i < 4; i++) {           // failures 2..5 — the 5th trips the bound
    await new Promise((r) => setTimeout(r, 0));
    el.update(%s);
  }
  await new Promise((r) => setTimeout(r, 0));   // let the 5th (bound-tripping) fetch settle
  // No further el.update() call anywhere after this — the "no more ticks"
  // case: whatever is on screen now is whatever the failing call itself put
  // there, or nothing ever will be.
  console.log(JSON.stringify({tree: dump(el.el), calls: runPythonLog.length}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="a1"))),
       json.dumps(_task_seg(subagent=_sub(agent_id="a1")))), tmp_path)
    assert got["calls"] == 5
    errors = _by_class(got["tree"], "sub-turn-error")
    assert len(errors) == 1 and "final straw" in errors[0]["text"]


# ------------------------------------------------- fetch gating

def test_a_collapsed_chip_triggers_no_further_fetch_even_on_a_later_tick(
        probe_src, tmp_path):
    """A closed chip must never start a second (potentially multi-megabyte)
    fetch for a chip nobody is looking at, even when a later poll tick
    calls pumpSubagentLog again with the Task now terminal."""
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({__defer: true, value: {turns: [{role: "user", text: "x"}]}});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                 // open -> fetch #1 starts (deferred)
  clickSummary(el.el);                 // CLOSED before it resolves
  el.update(%s);                        // status goes terminal mid-flight
  const p = pendingResolvers.shift();
  p.resolve(p.value);                  // fetch #1 resolves now, chip closed
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({calls: runPythonLog.length}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="running")),
       json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="ok"))), tmp_path)
    assert got["calls"] == 1, "a collapsed chip must not trigger a further fetch"


def test_a_given_up_agent_gets_no_further_fetch_on_a_later_tick(probe_src, tmp_path):
    """An agent already given up on (the fail bound reached) must not get a
    bonus fetch just because a later poll tick calls pumpSubagentLog again
    while the chip is open. (Code review round 7, MEDIUM: the chip has to
    actually be OPEN for this to test anything — a collapsed chip bails at
    `if (!el.open) return` long before the fail-bound check, which is
    exactly the gap the original version of this test had.)"""
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({turns: []});   // nothing useful yet, still worth asking again
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                 // OPEN it — the fail-bound gate this
                                        // test claims to pin is unreachable
                                        // otherwise
  await new Promise((r) => setTimeout(r, 0));
  subagentFailCount.set("a1", MAX_SUBAGENT_FETCH_ATTEMPTS);   // now given up
  const before = runPythonLog.length;
  runPythonQueue.push({__defer: true, value: {turns: [{role: "user", text: "x"}]}});
  el.update(%s);   // a later tick, chip STILL open, no re-open involved
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({calls: runPythonLog.length - before}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="running")),
       json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="running"))), tmp_path)
    # Given up already: no fetch at all, deferred or otherwise.
    assert got["calls"] == 0


# ------------------------------------------------- agent-mismatch guard (#D)

def test_a_rebuild_onto_a_different_agent_mid_fetch_does_not_cross_wires(
        probe_src, tmp_path):
    """A fetch in flight for agent A must not paint its result into agent
    B's log if the chip gets rebuilt onto B before A's fetch resolves — but
    A's answer IS a real, valid, potentially multi-megabyte fetch, cached
    under A's own agentId regardless, because the cache is keyed by agentId
    and stays valid no matter what this one chip currently points at. Only
    the DOM paint is gated on the match, not the cache write."""
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({__defer: true, value: {turns: [{role: "user", text: "from A"}]}});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                 // fetch for agent A starts, deferred
  el.update(%s);                        // rebuild retargets this chip at agent B
  const p = pendingResolvers.shift();
  p.resolve(p.value);                  // A's fetch resolves AFTER the retarget
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({
    tree: dump(el.el),
    cachedA: subagentCache.has("aA"),
    cacheB: subagentCache.get("aB") || null,
  }));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="aA"))),
       json.dumps(_task_seg(subagent=_sub(agent_id="aB")))), tmp_path)
    # A's answer IS cached under A — but it must never have painted into
    # B's log, or B's own cache under B's key (the rebuild onto B
    # legitimately triggers B's OWN fetch, which is unrelated and must not
    # be confused with A's outcome bleeding across).
    assert got["cachedA"] is True
    if got["cacheB"] is not None:
        assert got["cacheB"]["turns"] != [{"role": "user", "text": "from A"}]
    users = _by_class(got["tree"], "sub-turn-user")
    assert not any(u["text"] == "from A" for u in users)


# ------------------------------------------------- round 3: derived render

def test_the_give_up_message_is_not_repainted_once_shown(probe_src, tmp_path):
    """Code review round 3, finding #4 (and round 5's structural fix): the
    give-up message now writes directly into its OWN sibling slot via
    `textContent`, never by creating/replacing a child node — so "idempotent"
    is simply "no write happens when the text is already correct", provable
    directly rather than needing a uid/identity check on a node that no
    longer exists as a separate thing from its container."""
    got = _run(probe_src, """
const container = document.createElement("div");
renderSubagentFailure(container, "down");
const firstText = container.textContent;
renderSubagentFailure(container, "down");
console.log(JSON.stringify({
  n: container.children.length,
  text: container.textContent,
  sameText: container.textContent === firstText,
}));
""", tmp_path)
    assert got["n"] == 0, "no child node is ever created — the text lives on the slot itself"
    assert got["text"] == "Couldn't load this subagent's transcript: down."
    assert got["sameText"] is True


def test_session_id_absent_never_touches_the_loading_set(probe_src, tmp_path):
    """Code review round 3, finding #5: the `session_id` check used to sit
    INSIDE the try, after `subagentLoading.add(agentId)` — so a page where
    `session_id` has not landed yet added and (via `finally`) immediately
    deleted the agentId on every single tick, forever, unlike every other
    bail-out path above it which returns before touching the set at all.
    Hoisted above the add: `add` must never even be CALLED when there is no
    session id, not merely "cleaned up again by the time anyone looks"."""
    got = _run(probe_src, """
async function main() {
  SESSION_ID = "";
  const addCalls = [];
  const realAdd = subagentLoading.add.bind(subagentLoading);
  subagentLoading.add = (id) => { addCalls.push(id); return realAdd(id); };
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  await new Promise((r) => setTimeout(r, 0));
  el.update(%s);
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({
    addCalls: addCalls.length,
    stillLoading: subagentLoading.has("a1"),
    calls: runPythonLog.length,
  }));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="a1"))),
       json.dumps(_task_seg(subagent=_sub(agent_id="a1")))), tmp_path)
    assert got["addCalls"] == 0, "no session_id must never even register as loading"
    assert got["stillLoading"] is False
    assert got["calls"] == 0, "no session_id means no fetch is ever attempted"


# ------------------------------------------------- round 4

def test_renderSubagentTranscript_never_manages_a_notice_at_all(probe_src, tmp_path):
    """Code review round 5, structural fix A: the partial notice used to be
    the LAST entry of this same positionally-diffed list (round 3), which
    round 4 then had to patch around twice (a stale notice reused as a
    turn's element; a shrinking list mis-placing the new one). Round 5
    removes the mechanism instead of patching it again: this function knows
    nothing about notices any more, full stop — growing the turn count
    (the same "1 turn, then 2" scenario the round 4 bug used) just adds a
    second turn element, cleanly, with nothing else in the child list ever."""
    got = _run(probe_src, """
const container = document.createElement("div");
renderSubagentTranscript(container, [
  {role: "assistant", text: "", segments: [{kind: "text", text: "working on it"}]},
]);
renderSubagentTranscript(container, [
  {role: "assistant", text: "", segments: [{kind: "text", text: "working on it"}]},
  {role: "assistant", text: "", segments: [{kind: "text", text: "all done"}]},
]);
console.log(JSON.stringify({tree: dump(container)}));
""", tmp_path)
    kids = got["tree"]["children"]
    assert len(kids) == 2
    assert all(k["cls"] == "sub-turn-assistant" for k in kids)
    seg_text = _by_class(kids[1], "seg-text")
    assert len(seg_text) == 1 and seg_text[0]["html"] == "<md>all done</md>"


def test_a_role_flip_at_the_same_index_does_not_weld_text(probe_src, tmp_path):
    """Code review round 5, finding #5: a rewritten/compacted transcript can
    put a DIFFERENT role at an index a previous render already used —
    renderSegments only ever appends, it never clears, so a reused element
    that used to be a user turn (text set via textContent) would keep that
    text sitting underneath the newly-appended assistant segment views
    forever. `turnEl.replaceChildren()` on a class change closes this."""
    got = _run(probe_src, """
const container = document.createElement("div");
renderSubagentTranscript(container, [{role: "user", text: "a question"}]);
renderSubagentTranscript(container, [
  {role: "assistant", text: "", segments: [{kind: "text", text: "an answer"}]},
]);
console.log(JSON.stringify({tree: dump(container)}));
""", tmp_path)
    kids = got["tree"]["children"]
    assert len(kids) == 1
    assert kids[0]["cls"] == "sub-turn-assistant"
    assert "a question" not in kids[0]["text"]
    seg_text = _by_class(kids[0], "seg-text")
    assert len(seg_text) == 1 and seg_text[0]["html"] == "<md>an answer</md>"


def test_a_role_flip_back_to_a_previous_role_still_rebuilds(probe_src, tmp_path):
    """Code review round 6, finding #2: the round 5 fix cleared the DOM
    (`turnEl.replaceChildren()`) but left `renderSegments`'s own
    `segStates` WeakMap (keyed off the container element) pointing at the
    now-detached views — so a flip BACK to a role this element held before
    (assistant -> user -> assistant, which a rewritten/compacted transcript
    produces) matched `view.kind === "text"` on the STALE entry, decided
    nothing had changed, and never rebuilt: the turn rendered into detached
    nodes and stayed permanently blank. `segStates.delete(turnEl)` alongside
    the DOM clear is what closes it — verified here with the full three-step
    sequence, not just one flip."""
    got = _run(probe_src, """
const container = document.createElement("div");
renderSubagentTranscript(container, [
  {role: "assistant", text: "", segments: [{kind: "text", text: "first answer"}]},
]);
renderSubagentTranscript(container, [{role: "user", text: "a follow-up"}]);
renderSubagentTranscript(container, [
  {role: "assistant", text: "", segments: [{kind: "text", text: "second answer"}]},
]);
console.log(JSON.stringify({tree: dump(container)}));
""", tmp_path)
    kids = got["tree"]["children"]
    assert len(kids) == 1
    assert kids[0]["cls"] == "sub-turn-assistant"
    assert "a follow-up" not in kids[0]["text"]
    seg_text = _by_class(kids[0], "seg-text")
    assert len(seg_text) == 1 and seg_text[0]["html"] == "<md>second answer</md>"


def test_via_pump_a_growing_transcript_across_two_ticks_is_not_contaminated(
        probe_src, tmp_path):
    """The end-to-end version through `pumpSubagentLog` itself, not just the
    renderer in isolation: while still running, one tick returns 1 turn and
    the next returns 2 (the agent wrote more) — the second turn must render
    cleanly, with nothing left over from anything this chip showed before."""
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({turns: [
    {role: "assistant", text: "", segments: [{kind: "text", text: "working on it"}]},
  ]});
  runPythonQueue.push({turns: [
    {role: "assistant", text: "", segments: [{kind: "text", text: "working on it"}]},
    {role: "assistant", text: "", segments: [{kind: "text", text: "all done"}]},
  ]});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                  // fetch #1: 1 turn, still "running"
  await new Promise((r) => setTimeout(r, 0));
  el.update(%s);                         // still "running" -> fetch #2: 2 turns
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el)}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="running")),
       json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="running"))), tmp_path)
    log = _by_class(got["tree"], "chip-subagent-log")[0]
    assistants = _by_class(log, "sub-turn-assistant")
    assert len(assistants) == 2
    seg_text = _by_class(assistants[1], "seg-text")
    assert len(seg_text) == 1 and seg_text[0]["html"] == "<md>all done</md>"
    assert assistants[1]["text"] == "", (
        "something welded into the final turn: %r" % assistants[1]["text"])


def test_an_empty_response_at_a_terminal_status_counts_as_a_failure_not_settled(
        probe_src, tmp_path):
    """Code review round 5, structural fix B (this round's HIGH): round 4's
    fix made an empty-at-terminal response settle as `kind: "ok"` — a
    truncated (here, EMPTY) transcript indistinguishable from a genuinely
    complete one, no warning shown anywhere. It must instead count toward
    the fail bound like any other failure, chasing exactly
    MAX_SUBAGENT_FETCH_ATTEMPTS times before giving up and showing the
    failure state — never settling into a silent, confidently-blank "ok"."""
    got = _run(probe_src, """
async function main() {
  for (let i = 0; i < 5; i++) runPythonQueue.push({turns: []});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                  // fetch #1: empty, at a terminal status
  for (let i = 0; i < 4; i++) {
    await new Promise((r) => setTimeout(r, 0));
    el.update(%s);
  }
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el), calls: runPythonLog.length}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="ok")),
       json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="ok"))), tmp_path)
    assert got["calls"] == 5
    errors = _by_class(got["tree"], "sub-turn-error")
    assert len(errors) == 1 and errors[0]["text"]
    # No "ok" anywhere — an empty, never-confirmed transcript must never
    # render as if it were a real (if uneventful) one.
    assert _by_class(got["tree"], "sub-turn-user") == []
    assert _by_class(got["tree"], "sub-turn-assistant") == []


def test_reexpanding_after_giving_up_on_an_empty_response_gets_a_fresh_fetch(
        probe_src, tmp_path):
    """The recovery path for the SAME scenario: once given up on, re-opening
    (reexpand) must still be able to try again and succeed."""
    got = _run(probe_src, """
async function main() {
  for (let i = 0; i < 5; i++) runPythonQueue.push({turns: []});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  for (let i = 0; i < 4; i++) {
    await new Promise((r) => setTimeout(r, 0));
    el.update(%s);
  }
  await new Promise((r) => setTimeout(r, 0));
  clickSummary(el.el);                  // close
  runPythonQueue.push({turns: [{role: "user", text: "recovered"}]});
  clickSummary(el.el);                  // re-open: reexpand clears the fail count
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el)}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="ok")),
       json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="ok"))), tmp_path)
    users = _by_class(got["tree"], "sub-turn-user")
    assert len(users) == 1 and users[0]["text"] == "recovered"


def test_reexpanding_a_good_non_empty_cache_does_not_blank_or_refetch_it(
        probe_src, tmp_path):
    """`reexpand` only ever clears the fail count now — it never touches the
    cache at all — and re-opening a chip whose Task is terminal with a real,
    non-empty transcript already in hand must not blank the log or trigger
    a fresh (possibly multi-megabyte) fetch."""
    got = _run(probe_src, """
async function main() {
  runPythonQueue.push({turns: [{role: "user", text: "the real transcript"}]});
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);                  // fetch #1: good, non-empty, settled
  await new Promise((r) => setTimeout(r, 0));
  clickSummary(el.el);                  // close
  clickSummary(el.el);                  // re-open: must NOT refetch or blank
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({tree: dump(el.el), calls: runPythonLog.length}));
}
main();
""" % json.dumps(_task_seg(subagent=_sub(agent_id="a1"), status="ok")), tmp_path)
    assert got["calls"] == 1, "a good settled cache must not be re-fetched on reopen"
    users = _by_class(got["tree"], "sub-turn-user")
    assert len(users) == 1 and users[0]["text"] == "the real transcript"

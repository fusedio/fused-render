"""Node-probe tests for the `Task` chip's spawned-subagent rendering.

agent.py's `_attach_subagents` (tests/test_claude_subagents.py) puts a
`subagent` object on a `Task` tool segment — agentType, description, model,
spawnDepth, agentId, and (while running) a `progress` snapshot. This file
covers the RENDERER for that: the status badge in the chip's body
(`subagentStatusLine`, `formatElapsed`), the lazy fetch of the subagent's own
transcript on expand (`maybeLoadSubagentTranscript`, `loadSubagentTranscript`),
and rendering that transcript through the SAME segment machinery the main
conversation uses (`renderSubagentTranscript` calling `renderSegments`) —
which is also what makes a NESTED subagent (spawnDepth > 1) work with no
extra code.

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
// lets a test script each call's answer (default: no turns).
const runPythonLog = [];
const runPythonQueue = [];
const AGENT = "./agent.py";
const FILE = "/proj/page.html";
let SESSION_ID = "sess1";
const fused = {
  params: { get: (k) => (k === "session_id" ? SESSION_ID : "") },
  runPython: (agent, params) => {
    runPythonLog.push({agent, params});
    const next = runPythonQueue.length ? runPythonQueue.shift() : {turns: []};
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


def _task_seg(subagent=None, status="running", tid="t1"):
    seg = {"kind": "tool", "id": tid, "name": "Task",
           "input": {"description": "Verify suites", "subagent_type": "general-purpose"},
           "status": status, "output": None, "images": []}
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


def test_subagent_status_line_running_with_last_tool(probe_src, tmp_path):
    got = _run(probe_src, """
const seg = %s;
console.log(JSON.stringify({
  s: subagentStatusLine(seg, Date.parse("2026-01-01T00:00:10.000Z")),
}));
""" % json.dumps(_task_seg(subagent=_sub(progress={
        "startedAt": "2026-01-01T00:00:00.000Z", "lastActivityAt": "",
        "lastTool": "Bash"}))), tmp_path)
    assert got["s"] == "Running general-purpose — Bash (10s)"


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


def test_a_finished_and_cached_subagent_is_not_refetched(probe_src, tmp_path):
    got = _run(probe_src, """
async function main() {
  const el = buildToolChip(%s, "k1");
  clickSummary(el.el);
  await new Promise((r) => setTimeout(r, 0));
  el.update(%s);   // now "ok" — same agentId, already cached
  el.update(%s);
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({calls: runPythonLog.length}));
}
main();
""" % (json.dumps(_task_seg(subagent=_sub(), status="running")),
       json.dumps(_task_seg(subagent=_sub(), status="ok")),
       json.dumps(_task_seg(subagent=_sub(), status="ok"))), tmp_path)
    assert got["calls"] == 1


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

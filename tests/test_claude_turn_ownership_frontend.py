"""One owner for the open exchange — the frontend half (claude template).

`tests/test_claude_turn_ownership.py` covers `_history`'s `open_from` and
`_poll`'s `pending` (the Python half). This file covers what the page does
with them: `loadHistory` reserves the still-open exchange instead of drawing
it (`pendingOpenTurns`), and hands it to whichever attach follows
(`resumeRun`, `adoptLiveRun`) — every call site that runs `loadHistory` also
runs `renderReservedOpenTurns` right after its attach attempt, so the
exchange is drawn exactly once either way:

  * the attach itself draws it (a real, current run is found) — it clears
    `pendingOpenTurns` the instant it does, so `renderReservedOpenTurns`
    finds nothing left to reserve and is a no-op.
  * the attach draws nothing (a stale `run_id`, or `adoptLiveRun`'s watch
    finding no run at all) — `pendingOpenTurns` is still set, so
    `renderReservedOpenTurns` draws it through the same renderer the initial
    restore uses (`renderHistoryTurns`).

These two node-executed tests run the real `renderHistoryTurns` /
`renderReservedOpenTurns` source against a fake DOM, driving each of the two
outcomes above by reproducing what `resumeRun` actually does to
`pendingOpenTurns` on its live path (verified, not assumed — see
`test_resume_run_clears_the_reservation_wherever_it_actually_draws` below, a
source-assertion pinning that exact statement).
"""
import json
import os
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "claude", "template.html")


@pytest.fixture(scope="module")
def html():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _block(html, start_marker, end_marker):
    start = html.index(start_marker)
    end = html.index(end_marker, start) + len(end_marker)
    return html[start:end]


def _addUser_src(html):
    start = html.index("function addUser(text")
    end = html.index("\n}\n", start)
    return html[start:end + 2]


def _render_history_turns_src(html):
    return _block(html, "function renderHistoryTurns(rows) {", "\n}\n")


def _render_reserved_open_turns_src(html):
    return _block(html, "function renderReservedOpenTurns(opts) {", "\n}\n")


def _resume_run_src(html):
    return _block(html, "async function resumeRun(", "\nfunction submitChat()")


def _pending_block_src(html):
    """`pollLoop`'s own render of `data.pending` — the poll's copy of the
    dedup+draw `resumeRun`'s `renderPending` does, and the other site finding
    5 names."""
    marker = "// One owner per region: anything of the user's still queued"
    start = html.index(marker)
    end = html.index("for (const raw of data.pending || []) {", start)
    end = html.index("\n      }\n", end) + len("\n      }\n")
    return html[start:end]


# The fake DOM `test_claude_followup_reply_bubble.py` also uses — a minimal
# element tree with just enough of `appendChild`/`insertBefore`/`querySelector`
# for `addUser` and the render functions above to run unmodified.
_DOM_STUB = """
class FakeEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this._classes = new Set();
    this.classList = { contains: (c) => this._classes.has(c) };
  }
  set className(v) { this._classes = new Set(v.split(" ").filter(Boolean)); }
  get className() { return Array.from(this._classes).join(" "); }
  appendChild(el) { this.children.push(el); return el; }
  insertBefore(el, ref) {
    const already = this.children.indexOf(el);
    if (already !== -1) this.children.splice(already, 1);
    const i = this.children.indexOf(ref);
    if (i === -1) throw new Error("insertBefore: ref not in children");
    this.children.splice(i, 0, el);
    return el;
  }
  querySelector(sel) {
    return _queryAll(this, sel)[0] || null;
  }
  querySelectorAll(sel) {
    return _queryAll(this, sel);
  }
}
// A real descendant combinator (".user .bubble" — a bubble ANYWHERE under a
// `.user` turn, not one element carrying both classes at once), since
// `pollLoop`'s own pending-dedup and `resumeRun`'s `onScreen` both use one.
function _allDescendants(n) {
  const out = [];
  const walk = (x) => { for (const c of x.children) { out.push(c); walk(c); } };
  walk(n);
  return out;
}
function _queryAll(root, sel) {
  const steps = sel.trim().split(/\\s+/).map((s) => s.split(".").filter(Boolean));
  let contexts = [root];
  for (const classes of steps) {
    const next = [];
    for (const ctx of contexts) {
      for (const n of _allDescendants(ctx)) {
        if (classes.every((c) => n._classes && n._classes.has(c))) next.push(n);
      }
    }
    contexts = next;
  }
  return [...new Set(contexts)];
}
const document = { createElement: (tag) => new FakeEl(tag) };
const log = new FakeEl("div");
let followTail = false;
const scrollBottom = () => {};

// Everything `renderHistoryTurns` calls besides `addUser` — stubbed, because
// this suite is about WHICH turns get drawn and how many times, not about
// how a single turn's markdown/pictures/tool timeline render (that is
// `test_claude_shots.py`, `test_claude_template_segments.py`, etc.). Each
// assistant row still gets a real element in `log` so the two tests below
// can count what actually landed on screen.
let stripBlocks = (t) => t;
const shotRestoreReceipt = () => {};
const addNote = () => {};
const attachCodeCopy = () => {};
let assistantTurnsDrawn = 0;
function addAssistantTurn(text, segments) {
  assistantTurnsDrawn++;
  const d = new FakeEl("div");
  d.className = "turn assistant";
  log.appendChild(d);
  return d;
}
let anchorCalls = 0;
function consumeAnchor() { anchorCalls++; return false; }

const classNamesOf = (el) => el.children.map((c) => c.className);
"""


def _run(html, body):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own render glue")
    script = _DOM_STUB + "\n" + body
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                        encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _node_fns(html, fn_names):
    """Extract named top-level functions/consts out of template.html verbatim
    — same extraction `test_claude_shots.py`'s own `_node` uses. Kept as its
    own copy rather than imported across test modules, same reason as that
    one: independent suites, no shared harness to keep in sync."""
    chunks = []
    for name in fn_names:
        start = html.index(name)
        if name.startswith("function") or name.startswith("async function"):
            end = html.index("\n}\n", start) + 3
            chunks.append(html[start:end])
            continue
        taken = []
        for line in html[start:].split("\n"):
            taken.append(line)
            if line.split("//")[0].rstrip().endswith(";"):
                break
        chunks.append("\n".join(taken))
    return "\n".join(chunks)


# The wire's writer and reader (composeOutgoing/stripBlocks and everything
# they call) — the real thing, not a stand-in, because the point of the
# acceptance test below is that the bubble sendFollowUp draws is the SAME
# marker stripBlocks would give this exact wire text on a reload.
_WIRE_FNS = ["let targetNoun", "let paneNoun", "const ANN_TAG", "const ANN_NO_WORDS",
             "function annClock(", "function annStanza(", "function formatAnnotations(",
             "function stripAnnBlock(",
             "function stripAppStateBlock(", "function stripBlocks(",
             "const APP_STATE_TAG", "function appStateBlock(",
             "const PANE_SHOT_TAG", "function paneShotBlock(",
             "const MARKER_ANN", "const MARKER_VIEW", "const MARKER_IMG",
             "const MARKER_FILE",
             "const MARKERS", "const MARKER_JOIN",
             "function stripPaneBlock(", "function paneShotIn(",
             "function composeOutgoing("]


_RESERVED_TURNS = (
    '[{ role: "user", text: "still open", uuid: "u1" }, '
    '{ role: "assistant", text: "reply", segments: [] }]'
)


def test_resume_run_clears_the_reservation_wherever_it_actually_draws(html):
    """Pins the exact statement the two node-execution tests below stand in
    for: `resumeRun` clears `pendingOpenTurns` once it has actually drawn
    something for this exchange, not the instant a genuine, current probe
    comes back — clearing it unconditionally, before any branch had decided
    whether it had anything to draw, lost the reservation for the two
    branches that draw nothing (a `probe.error` whose exchange is already on
    screen, and the done-repair's own `onScreen` guard), and nothing else was
    ever going to draw it for them (finding 4).

    The still-running (live) branch always ends up owning this exchange —
    if not here then through the `pollLoop` it is about to start — so its own
    clear, right before that hand-off, is what the two node-execution tests
    below stand in for."""
    fn = _block(html, "async function resumeRun(", "\nfunction submitChat()")
    # Reached only past both of `probe.done`'s own branches (each returns
    # before falling through to here).
    live_marker = "// Still running:"
    assert fn.index(live_marker) > fn.index("if (probe.done) {")
    live = fn[fn.index(live_marker):]
    clear = live.index("pendingOpenTurns = null;")
    assert clear < live.index("await pollLoop(run_id, gen);")
    # Every other clear in the function sits inside a branch that just drew
    # something — an error rendered, or the done-repair's own turn rendered —
    # never unconditionally ahead of that decision.
    assert fn.count("pendingOpenTurns = null;") == 3


def test_open_exchange_renders_exactly_once_when_attach_draws_it(html):
    """The attach path (resumeRun) found a real, current run and drew the
    open exchange itself — clearing `pendingOpenTurns` as it does (previous
    test). `renderReservedOpenTurns`, called unconditionally right after by
    every `loadHistory` caller, must then find nothing left to draw."""
    src = _addUser_src(html) + "\n" + _render_history_turns_src(html) + "\n" \
        + _render_reserved_open_turns_src(html)
    out = _run(html, src + f"""
// loadHistory's own assignment, off `open_from`.
let pendingOpenTurns = {_RESERVED_TURNS};

// resumeRun's live path: a genuine probe came back, so it takes ownership
// of this exchange and draws it — the exact clear the previous test pins,
// followed by the exact shape resumeRun draws with (addUser, then whatever
// assistant content the probe carried).
pendingOpenTurns = null;
addUser("still open", "u1");
addAssistantTurn("reply", []);

const drewBeforeFallback = classNamesOf(log).length;
const fallbackDidSomething = renderReservedOpenTurns({{ anchor: true }});

console.log(JSON.stringify({{
  order: classNamesOf(log),
  drewBeforeFallback,
  fallbackDidSomething,
  assistantTurnsDrawn,
  anchorCalls,
}}));
""")
    assert out["fallbackDidSomething"] is False, \
        "the fallback must be a no-op once the attach already drew this exchange"
    assert out["order"] == ["turn user", "turn assistant"], \
        "the open exchange must appear exactly once, not doubled by the fallback"
    assert out["drewBeforeFallback"] == 2
    assert out["assistantTurnsDrawn"] == 1
    # The fallback draws nothing here, but a `?msg=` naming a turn INSIDE
    # this exchange (or a closed turn loadHistory deferred behind this same
    # call) still gets its one try now that the whole transcript is finally
    # on screen — resumeRun, which just drew this exchange itself, never
    # spends the anchor on its own.
    assert out["anchorCalls"] == 1


def test_open_exchange_renders_via_fallback_when_attach_finds_nothing(html):
    """The attach found no run at all (a stale `run_id` bail-out, or
    `adoptLiveRun`'s watch exhausting its retry laps) and so never touched
    `pendingOpenTurns` — the reservation `loadHistory` made is still sitting
    there, and the fallback is what turns it into the one thing every other
    caller assumed would already be on screen."""
    src = _addUser_src(html) + "\n" + _render_history_turns_src(html) + "\n" \
        + _render_reserved_open_turns_src(html)
    out = _run(html, f"""
let pendingOpenTurns = {_RESERVED_TURNS};
""" + src + """

// No attach drew anything — pendingOpenTurns reaches this call unchanged.
const fallbackDidSomething = renderReservedOpenTurns({ anchor: true });

console.log(JSON.stringify({
  order: classNamesOf(log),
  fallbackDidSomething,
  assistantTurnsDrawn,
  anchorCalls,
  pendingAfter: pendingOpenTurns,
}));
""")
    assert out["fallbackDidSomething"] is True
    assert out["order"] == ["turn user", "turn assistant"], \
        "the reserved exchange must render exactly once, via the same " \
        "renderer the initial restore uses"
    assert out["assistantTurnsDrawn"] == 1
    assert out["pendingAfter"] is None, \
        "drawn once means spent — a second stray call must not draw it again"
    # This IS the one caller allowed to spend the anchor late, since it is
    # the first render the exchange (and any turn `?msg=` names inside it)
    # ever gets.
    assert out["anchorCalls"] == 1


def test_reserved_turns_are_skipped_once_the_log_generation_moves_on(html):
    """Both call sites (`open()` in the session list, the boot IIFE) sample
    `logGen` before the long awaits that lead here — `resumeRun`'s own probe,
    `adoptLiveRun`'s watch — and pass it through as `opts.gen`. Back bumps
    `logGen` and wipes `log` while such an await is in flight; without this
    check the reservation lands in the wiped log, or (a different chat opened
    in the meantime) consumes the NEW session's reservation instead."""
    src = _addUser_src(html) + "\n" + _render_history_turns_src(html) + "\n" \
        + _render_reserved_open_turns_src(html)
    out = _run(html, f"""
let pendingOpenTurns = {_RESERVED_TURNS};
let logGen = 5;  // Back (or a new chat) bumped this while the caller awaited
""" + src + """
const fallbackDidSomething = renderReservedOpenTurns({ anchor: true, gen: 3 });
console.log(JSON.stringify({
  fallbackDidSomething,
  order: classNamesOf(log),
  assistantTurnsDrawn,
  anchorCalls,
  pendingAfter: pendingOpenTurns,
}));
""")
    assert out["fallbackDidSomething"] is False
    assert out["order"] == [], "a stale-generation call must not touch the wiped log"
    assert out["assistantTurnsDrawn"] == 0
    assert out["anchorCalls"] == 0, \
        "a stale generation's own ?msg= is not this session's to spend"
    # Left alone for whichever fresh `loadHistory` runs next to overwrite.
    assert out["pendingAfter"] is not None and len(out["pendingAfter"]) == 2


def test_a_second_fallback_call_never_redraws(html):
    """Defensive: even if some future call site called
    `renderReservedOpenTurns` twice (a double-await, a retry), the second
    call must find nothing left — `pendingOpenTurns` is consumed by the
    first, successful call, same as a `quiet` addUser is guarded by
    `onScreen` on the resumeRun side."""
    src = _addUser_src(html) + "\n" + _render_history_turns_src(html) + "\n" \
        + _render_reserved_open_turns_src(html)
    out = _run(html, f"""
let pendingOpenTurns = {_RESERVED_TURNS};
""" + src + """
const first = renderReservedOpenTurns({ anchor: true });
const second = renderReservedOpenTurns({ anchor: true });
console.log(JSON.stringify({
  first, second, order: classNamesOf(log), assistantTurnsDrawn,
}));
""")
    assert out["first"] is True
    assert out["second"] is False
    assert out["order"] == ["turn user", "turn assistant"]
    assert out["assistantTurnsDrawn"] == 1


def test_pending_bubbles_in_poll_loop_are_stripped_before_render_and_dedup(html):
    """A follow-up's wire text (`composeOutgoing`) carries `<pane-shot>…`
    and the annotations block on top of whatever the user typed;
    `_pending_messages` only strips the app-state block off it. `pollLoop`'s
    own render of `data.pending` compared and drew that raw wire text
    directly — a follow-up already drawn stripped (by `sendFollowUp` or
    `resumeRun`'s own `renderPending`) never matches it, and a second bubble
    appears carrying the raw `<pane-shot>` block and its path."""
    src = _addUser_src(html) + "\n" + _pending_block_src(html)
    out = _run(html, """
stripBlocks = (t) => t.includes("<pane-shot>") ? "🖼 pane screenshot" : t;
let followupSeq = 0;

// Already on screen, stripped — exactly what sendFollowUp/resumeRun draw.
addUser("🖼 pane screenshot");

const data = { pending: ["<pane-shot>/tmp/fr/shots/x.png</pane-shot>"] };
""" + src + """
console.log(JSON.stringify({
  bubbles: [...log.querySelectorAll(".user .bubble")].map((b) => b.textContent),
  followupSeq,
}));
""")
    assert out["bubbles"] == ["🖼 pane screenshot"], \
        "the raw wire text must never reach the transcript, and a bubble " \
        "already up (stripped) must dedup against the stripped pending text"
    assert out["followupSeq"] == 0, \
        "a message already on screen is not a NEW follow-up landing — " \
        "nothing here should reset the streaming reply bubble"


def test_a_genuinely_new_pending_bubble_bumps_followup_seq(html):
    """finding 10: a follow-up adopted through `pending` — sent from another
    tab, or from this tab before its own `sendFollowUp` bump landed — must
    reset the streaming reply the same way `sendFollowUp`'s own bump does,
    or the assistant's continuation keeps flowing into the bubble positioned
    ABOVE the question this follow-up asks."""
    src = _addUser_src(html) + "\n" + _pending_block_src(html)
    out = _run(html, """
let followupSeq = 0;
const data = { pending: ["a second message, sent from another tab"] };
""" + src + """
console.log(JSON.stringify({
  bubbles: [...log.querySelectorAll(".user .bubble")].map((b) => b.textContent),
  followupSeq,
}));
""")
    assert out["bubbles"] == ["a second message, sent from another tab"]
    assert out["followupSeq"] == 1, \
        "drawing a genuinely new pending bubble must bump followupSeq, " \
        "the same signal sendFollowUp's own bump gives pollLoop"


# Everything `resumeRun` needs besides the DOM stub and `addUser`/`stripBlocks`
# above: a scriptable `fused.runPython` (one scripted response per call, in
# order — resumeRun's own retry loop can call it more than once), and the
# rest of the page it calls into stubbed to a no-op or a call counter.
_RESUME_STUB = """
let sending = false;
let sendSeq = 0;
let logGen = 0;
const AGENT = "agent.py";
const FILE = "/f";
const box = null;
function focusBox() {}
const paramsSet = [];
const fused = {
  params: { set: (k, v, opts) => paramsSet.push([k, v, opts]) },
  runPython: () => { throw new Error("no scripted probe left"); },
};
function scriptProbes(list) {
  let i = 0;
  fused.runPython = async () => list[Math.min(i++, list.length - 1)];
}
let annResolveCalls = 0;
function annResolveSent() { annResolveCalls++; }
let snapInvalidateCalls = 0;
function snapInvalidate() { snapInvalidateCalls++; }
const errors = [];
function addError(msg) { errors.push(msg); }
let pollLoopCalls = [];
async function pollLoop(run_id, gen) { pollLoopCalls.push([run_id, gen]); }
"""


def test_resume_run_shows_the_error_on_an_ordinary_reload_of_a_crashed_run(html):
    """finding 3: the condition guarding `addError` used to be
    `if (!users.length)` — on the ordinary boot path (`quiet` false,
    `neverShown` false) a non-empty conversation always has `users.length >
    0`, so a reload into a crashed or killed run showed no error at all."""
    src = _addUser_src(html) + "\n" + _resume_run_src(html)
    out = _run(html, _RESUME_STUB + src + """
let pendingOpenTurns = { some: "reservation" };
addUser("earlier turn, already on screen");
scriptProbes([{ done: true, error: "claude exited unexpectedly" }]);
(async () => {
  await resumeRun("r1", {});
  console.log(JSON.stringify({ errors, pendingOpenTurnsAfter: pendingOpenTurns }));
})();
""")
    assert out["errors"] == ["claude exited unexpectedly"], \
        "an ordinary reload (quiet=false, neverShown=false) of a crashed " \
        "run, with a non-empty transcript, must still show the error"
    assert out["pendingOpenTurnsAfter"] is None, \
        "the error is now on screen for this exchange, so the reservation " \
        "is spoken for"


def test_resume_run_pending_texts_are_stripped_for_render_and_dedup(html):
    """finding 5 (the other site): `renderPending` inside `resumeRun` must
    run `stripBlocks` before it draws OR dedups a queued message, the same
    as `probeMsg` above it — otherwise a follow-up sent with a screenshot
    reaches this transcript as a second, raw `<pane-shot>…` bubble."""
    src = _addUser_src(html) + "\n" + _resume_run_src(html)
    out = _run(html, _RESUME_STUB + src + """
let pendingOpenTurns = null;
stripBlocks = (t) => t.includes("<pane-shot>") ? "🖼 pane screenshot" : t;
// Already on screen, stripped.
addUser("🖼 pane screenshot");
scriptProbes([{
  done: false, message: "",
  pending: ["<pane-shot>/tmp/fr/shots/x.png</pane-shot>"],
}]);
(async () => {
  await resumeRun("r1", {});
  console.log(JSON.stringify({
    bubbles: [...log.querySelectorAll(".user .bubble")].map((b) => b.textContent),
  }));
})();
""")
    assert out["bubbles"] == ["🖼 pane screenshot"], \
        "the raw wire text must never reach the transcript, and a bubble " \
        "already up (stripped) must dedup against the stripped pending text"


def test_resume_run_never_draws_its_own_assistant_bubble_on_the_live_path(html):
    """finding 7 ("who draws what"): the still-open exchange's assistant
    text comes from `pollLoop`'s own poll, never from `resumeRun` itself — a
    probe that is not yet `done` carries no rendered assistant turn here."""
    src = _addUser_src(html) + "\n" + _resume_run_src(html)
    out = _run(html, _RESUME_STUB + src + """
let pendingOpenTurns = null;
scriptProbes([{ done: false, message: "still going", pending: [] }]);
(async () => {
  await resumeRun("r1", {});
  console.log(JSON.stringify({
    bubbles: [...log.querySelectorAll(".user .bubble")].map((b) => b.textContent),
    assistantTurnsDrawn,
    pollLoopCalls,
  }));
})();
""")
    assert out["bubbles"] == ["still going"]
    assert out["assistantTurnsDrawn"] == 0, \
        "the open exchange's assistant reply belongs to pollLoop, not resumeRun"
    assert out["pollLoopCalls"] == [["r1", 0]]


def test_a_mid_turn_reload_draws_each_region_exactly_once(html):
    """SPEC.md PR-7, acceptance test 10: a mid-turn reload draws the open
    exchange's user turns exactly once and its assistant reply exactly once.

    This runs the three real owners in the order a reload actually calls
    them: `renderHistoryTurns` draws everything BEFORE `open_from` (the
    closed history), then `resumeRun`'s live path draws the open exchange's
    user turn once (from the reserved rows the probe confirms) and hands the
    reply to exactly one `pollLoop` call rather than drawing an assistant
    bubble of its own. Each piece already has its own test above; this one
    pins that running them together does not double anything and does not
    drop anything — the actual failure mode a wrong `open_from` produces."""
    src = _addUser_src(html) + "\n" + _render_history_turns_src(html) + "\n" \
        + _resume_run_src(html)
    out = _run(html, _RESUME_STUB + src + """
// The closed history render: everything before open_from, exactly once.
renderHistoryTurns([
  { role: "user", text: "earlier turn", uuid: "u0" },
  { role: "assistant", text: "earlier reply", segments: [] },
]);
const afterHistory = classNamesOf(log).length;
const assistantTurnsAfterHistory = assistantTurnsDrawn;

// loadHistory's own reservation for the still-open exchange, off open_from.
let pendingOpenTurns = { some: "reservation" };

// The live attach: a genuine, current run is found, so it draws the open
// exchange's user turn itself and clears the reservation.
scriptProbes([{ done: false, message: "still open", pending: [] }]);
(async () => {
  await resumeRun("r1", {});
  console.log(JSON.stringify({
    afterHistory,
    assistantTurnsAfterHistory,
    order: classNamesOf(log),
    assistantTurnsDrawn,
    pollLoopCalls,
    pendingOpenTurnsAfter: pendingOpenTurns,
  }));
})();
""")
    assert out["afterHistory"] == 2, \
        "the closed history must be on screen before the attach ever runs"
    assert out["assistantTurnsAfterHistory"] == 1, \
        "the closed history's own assistant turn (not this exchange's) " \
        "renders once, before the attach ever runs"
    assert out["order"] == ["turn user", "turn assistant", "turn user"], \
        "the open exchange's user turn must land exactly once, after the " \
        "closed history and never duplicated by it"
    assert out["assistantTurnsDrawn"] == out["assistantTurnsAfterHistory"], \
        "resumeRun must not draw the open exchange's assistant reply itself " \
        "-- only the closed history's own turn is counted"
    assert out["pollLoopCalls"] == [["r1", 0]], \
        "the reply is handed to exactly one pollLoop call, the exchange's " \
        "one and only source for it"
    assert out["pendingOpenTurnsAfter"] is None, \
        "the attach drawing this exchange must spend the reservation, or a " \
        "fallback call elsewhere would draw it a second time"


def test_resume_run_draws_every_reserved_turn_not_just_probes_own_message(html):
    """Findings 1/2: `probe.message` is `meta.json`'s message from the run's
    `_start` — the exchange's very first send, never updated by `_send` — so
    it names only ONE of possibly several user turns already echoed to the
    transcript for this exchange (a follow-up folded in and then a second
    one, both drained before this reload). The reserved slice
    `loadHistory` made (`pendingOpenTurns`) is what actually holds all of
    them, in order; `resumeRun` must draw THAT, not just `probeMsg`, or a
    reload mid-reply loses every already-echoed follow-up but the first."""
    src = _addUser_src(html) + "\n" + _render_history_turns_src(html) + "\n" \
        + _resume_run_src(html)
    out = _run(html, _RESUME_STUB + src + """
let pendingOpenTurns = [
  { role: "user", text: "fix the header", uuid: "u1" },
  { role: "user", text: "now the footer too", uuid: "u2" },
];
// probe.message is the run's FIRST message, stale for this second exchange.
scriptProbes([{ done: false, message: "fix the header", pending: [] }]);
(async () => {
  await resumeRun("r1", {});
  console.log(JSON.stringify({
    bubbles: [...log.querySelectorAll(".user .bubble")].map((b) => b.textContent),
    pendingOpenTurnsAfter: pendingOpenTurns,
    pollLoopCalls,
  }));
})();
""")
    assert out["bubbles"] == ["fix the header", "now the footer too"], \
        "every reserved turn must draw, not just the run's first message"
    assert out["pendingOpenTurnsAfter"] is None
    assert out["pollLoopCalls"] == [["r1", 0]]


def test_resume_run_spends_the_anchor_ahead_of_the_poll_hand_off(html):
    """Finding 7: the `?msg=` anchor must be tried the moment the reservation
    is drawn, not deferred until `pollLoop` resolves the WHOLE live turn —
    which can be minutes away. `pollLoop` here never resolves (a promise that
    never settles), so a `consumeAnchor` call reached only after `await
    pollLoop(...)` would never happen inside this test at all."""
    src = _addUser_src(html) + "\n" + _render_history_turns_src(html) + "\n" \
        + _resume_run_src(html)
    out = _run(html, _RESUME_STUB.replace(
        "async function pollLoop(run_id, gen) { pollLoopCalls.push([run_id, gen]); }",
        "async function pollLoop(run_id, gen) { pollLoopCalls.push([run_id, gen]); "
        "return new Promise(() => {}); }",
    ) + src + """
let pendingOpenTurns = [
  { role: "user", text: "fix the header", uuid: "u1" },
];
scriptProbes([{ done: false, message: "fix the header", pending: [] }]);
resumeRun("r1", {});
setTimeout(() => {
  console.log(JSON.stringify({ anchorCalls, pollLoopCalls }));
}, 20);
""")
    assert out["anchorCalls"] == 1, \
        "the anchor must be spent as the reservation is drawn, ahead of the " \
        "poll hand-off — not deferred until pollLoop (which never returns " \
        "here) resolves"
    assert out["pollLoopCalls"] == [["r1", 0]]


# Everything sendMessage needs besides the DOM stub, addUser, and the real
# wire functions (_WIRE_FNS) above: a scriptable start/send, and everything
# else it touches stubbed to a no-op or a call counter.
_SENDMSG_STUB = """
let sending = false;
let sendSeq = 0;
let logGen = 0;
const AGENT = "agent.py";
const FILE = "/f";
const box = null;
function focusBox() {}
const noPane = false;
let annotations = [];
const annPending = () => annotations.filter((c) => !c.sent);
function annLabelFor(i) { return String.fromCharCode(65 + i); }
function annSave() {}
function appStatePush() { return null; }
async function appStateFile(state) { return state; }
async function annOverview(pending) { return null; }
function annApplyOverview() {}
function sentPopWire() {}
function shotReceipt() {}
function annReceiptRow() { return document.createElement("div"); }
function shotReadDirs() { return []; }
function curModel() { return ""; }
function curEffort() { return ""; }
function shotRevoke() {}
const DEFAULT_PERMISSION = "default";
let pollLoopCalls = [];
async function pollLoop(run_id, gen) { pollLoopCalls.push([run_id, gen]); }
const errors = [];
function addError(msg) { errors.push(msg); }
const fused = {
  params: { get: () => "", set: () => {} },
  runPython: async (agent, req) => {
    if (req.action === "start") return { run_id: "r1" };
    return { sent: true };
  },
};
"""


def test_send_message_draws_a_wordless_turn_with_the_same_marker_a_reload_would_show(html):
    """Finding 4: `sendMessage`'s wordless branch (annotations or pictures,
    no typed words) used to draw a bubble-less `div.turn.user` to hang the
    receipt on — the lockstep sibling of D758's fix, already applied to
    `sendFollowUp` but not here. Drawn now with the same
    `stripBlocks(outgoing)` marker a reload's history restore or another
    tab's poll would show, so the ordinary `.user .bubble` textContent dedup
    finds this turn already on screen instead of drawing a second one."""
    src = _node_fns(html, _WIRE_FNS) + "\n" + _addUser_src(html) + "\n" \
        + _block(html, "async function sendMessage(message) {", "\n}\n")
    out = _run(html, _SENDMSG_STUB + f"""
(async () => {{
{src}
let shotAttached = [{{ kind: "pane", view: "/tmp/fr/shots/x.png" }}];
function renderAnn() {{}}
  await sendMessage("");
  console.log(JSON.stringify({{
    turns: [...log.querySelectorAll(".turn.user")].length,
    bubbles: [...log.querySelectorAll(".user .bubble")].map((b) => b.textContent),
    errors,
  }}));
}})();
""")
    assert out["errors"] == []
    assert out["turns"] == 1, "the wordless send must draw exactly one turn"
    assert out["bubbles"] == ["🖼 pane screenshot"], \
        "exactly one bubble, showing the marker label — never a second, " \
        "bubble-less turn nothing later compares against"


# Everything sendFollowUp needs besides the DOM stub, addUser, and the real
# wire functions (_WIRE_FNS) above: a scriptable send, and everything else it
# touches stubbed to a no-op or a call counter.
_FOLLOWUP_STUB = """
let logGen = 0;
let followupSeq = 0;
let activeRun = "r1";
const AGENT = "agent.py";
let annotations = [];
const annPending = () => annotations.filter((c) => !c.sent);
function annLabelFor(i) { return String.fromCharCode(65 + i); }
function annSave() {}
function sentPopWire() {}
function shotReceipt() {}
function annReceiptRow() { return document.createElement("div"); }
function shotReadDirs() { return []; }
function curModel() { return ""; }
function curEffort() { return ""; }
const DEFAULT_PERMISSION = "default";
const errors = [];
function addError(msg) { errors.push(msg); }
const fused = {
  params: { get: () => "" },
  runPython: async () => ({ sent: true }),
};
"""


def test_a_wordless_screenshot_only_follow_up_draws_one_bubble(html):
    """SPEC.md PR-7, acceptance test 11: a wordless follow-up whose text is
    only a marker block draws exactly one bubble showing the marker label,
    never the raw `<pane-shot>` block and never two bubbles.

    Before this, a wordless follow-up (annotations or pictures with no typed
    words) drew a "turn user" div carrying nothing but the receipt — no
    `.bubble` at all, so nothing here matched what `pollLoop`'s pending dedup
    or a reload's history restore later compare against, and either could go
    on to draw a SECOND, separate turn for the same send once the CLI echoed
    it back or the page reloaded. `sendFollowUp` now draws the turn with the
    same marker `stripBlocks` derives from the exact wire text sent, so that
    bubble is the one thing later readers find already on screen."""
    # The real stripBlocks/composeOutgoing declare `function stripBlocks`,
    # which would collide with the DOM stub's own `let stripBlocks` shadow
    # (module-scope `let` and `function` of the same name is a SyntaxError,
    # not a redeclaration this suite could otherwise rely on) — an inner
    # function scope sidesteps that: `addUser`/`log`/`errors` above are still
    # reachable by closure, and `stripBlocks`/`composeOutgoing` declared
    # inside shadow the outer stub cleanly.
    src = _node_fns(html, _WIRE_FNS) + "\n" + _addUser_src(html) + "\n" \
        + _block(html, "async function sendFollowUp(text) {", "\n}\n")
    out = _run(html, _FOLLOWUP_STUB + f"""
(async () => {{
{src}
let shotAttached = [{{ kind: "pane", view: "/tmp/fr/shots/x.png" }}];
function renderAnn() {{}}
  await sendFollowUp("");
  console.log(JSON.stringify({{
    turns: [...log.querySelectorAll(".turn.user")].length,
    bubbles: [...log.querySelectorAll(".user .bubble")].map((b) => b.textContent),
    errors,
  }}));
}})();
""")
    assert out["errors"] == []
    assert out["turns"] == 1, "the wordless send must draw exactly one turn"
    assert out["bubbles"] == ["🖼 pane screenshot"], \
        "exactly one bubble, showing the marker label — never the raw " \
        "<pane-shot> block and never a second, bubble-less turn"

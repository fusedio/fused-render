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

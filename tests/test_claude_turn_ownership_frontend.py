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
`test_resume_run_clears_the_reservation_on_its_live_path` below, a
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
    const classes = sel.split(".").filter(Boolean);
    const matches = (n) => classes.every((c) => n._classes && n._classes.has(c));
    const walk = (n) => {
      for (const c of n.children) {
        if (matches(c)) return c;
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
  querySelectorAll(sel) {
    const classes = sel.split(".").filter(Boolean);
    const matches = (n) => classes.every((c) => n._classes && n._classes.has(c));
    const out = [];
    const walk = (n) => {
      for (const c of n.children) {
        if (matches(c)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
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
const stripBlocks = (t) => t;
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
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_RESERVED_TURNS = (
    '[{ role: "user", text: "still open", uuid: "u1" }, '
    '{ role: "assistant", text: "reply", segments: [] }]'
)


def test_resume_run_clears_the_reservation_on_its_live_path(html):
    """Pins the exact statement the two node-execution tests below stand in
    for: `resumeRun` clears `pendingOpenTurns` as soon as it has a genuine,
    current probe to work with — before any of its own render branches run —
    so `renderReservedOpenTurns` never re-draws what a live attach is about
    to (or just did) draw itself."""
    fn = _block(html, "async function resumeRun(", "\nfunction submitChat()")
    clear = fn.index("pendingOpenTurns = null;")
    # Before the done/error/quiet branching, and after the only two bail-outs
    # that leave this exchange for the fallback instead (an unknown run_id,
    # or a run that belongs to a different target) — neither of those
    # `return`s is reachable once this line runs.
    assert fn.index("probe.error === \"unknown run_id\"") < clear
    assert clear < fn.index("if (probe.done) {")


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
    # The anchor was already spendable the moment the live draw put the turn
    # on screen; a no-op fallback must not burn a second, wasted attempt.
    assert out["anchorCalls"] == 0


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

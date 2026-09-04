"""A mid-turn follow-up must not have later text render ABOVE it.

`pollLoop`'s streaming assistant bubble (`reply`) is created once, guarded by
`if (!reply)`, and inserted before the "working" status line. `addUser` (see
`test_claude_followup_ordering.py`) inserts a follow-up's own bubble before
that same status line too — so once a follow-up lands mid-turn, the DOM order
becomes `[reply][user follow-up][working]`. Because `reply` is never reset,
every poll after that follow-up keeps appending text into the SAME `reply`
bubble, which sits BEFORE the follow-up's own bubble: the turn's continued
answer renders above the message the reader just sent, not below it.

`sendFollowUp` now bumps a module-level `followupSeq` counter right after it
inserts the follow-up's own bubble; `pollLoop` compares that counter against
the value it last saw on every iteration and, on a change, drops its `reply`
pointer (and everything derived from it) so the existing `if (!reply)` guard
starts a brand-new assistant bubble — which lands, via the same
`log.insertBefore(reply, w.el)` it always used, right after the follow-up's
bubble and before the status line.

This runs the three real snippets (the bump in `sendFollowUp`, the reset
check and the existing bubble-creation guard in `pollLoop`) under node
against the same tiny fake DOM `test_claude_followup_ordering.py` uses, wired
together the way `pollLoop`'s own loop body wires them.
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


def _addUser_src(html):
    start = html.index("function addUser(text")
    end = html.index("\n}\n", start)
    return html[start:end + 2]


def _addAssistant_src(html):
    start = html.index("function addAssistant(text")
    end = html.index("\n}\n", start)
    return html[start:end + 2]


def _followup_bump_src(html):
    """The `followupSeq++` sendFollowUp does right after inserting the
    follow-up's own bubble."""
    marker = "// A follow-up landed as its own bubble above"
    start = html.index(marker)
    end = html.index("followupSeq++;", start)
    end = html.index("\n", end) + 1
    return html[start:end]


def _reply_reset_src(html):
    """The reset check `pollLoop` runs each iteration before touching
    `reply` — now also records `segBase`/`textBase`, the offsets a fresh
    bubble slices `data.segments`/`data.text` back to (see
    `_segs_flat_src`)."""
    marker = "// A follow-up landed mid-turn (see sendFollowUp)"
    start = html.index(marker)
    end = html.index("tailText = null;", start)
    end = html.index("\n      }\n", end) + len("\n      }\n")
    return html[start:end]


def _segs_flat_src(html):
    """The `segs`/`flatText` computation right after the reset check — sliced
    back to `segBase`/`textBase` so a fresh post-follow-up bubble only ever
    sees the turn's continuation, not everything already shown above it —
    and the `lastSegLen`/`lastTextLen` update the NEXT reset freezes into
    `segBase`/`textBase`."""
    start = html.index("const fullSegs = Array.isArray(data.segments)")
    end = html.index("lastTextLen = fullText.length;", start)
    end = html.index("\n", end) + 1
    return html[start:end]


def _create_reply_src(html):
    """The pre-existing `if (!reply) { ... }` bubble-creation guard,
    unchanged by this fix — proving the reset above is what makes it run
    again, not a rewrite of the guard itself."""
    start = html.index("if (!reply) {")
    end = html.index("typer = makeTyper(segs.length ? null : segBody);", start)
    end = html.index("\n        }\n", end) + len("\n        }\n")
    return html[start:end]


_DOM_STUB = """
class FakeEl {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this._classes = new Set();
    this.classList = {
      contains: (c) => this._classes.has(c),
    };
  }
  set className(v) { this._classes = new Set(v.split(" ").filter(Boolean)); }
  get className() { return Array.from(this._classes).join(" "); }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  appendChild(el) { this.children.push(el); return el; }
  append(...els) { for (const el of els) this.children.push(el); }
  insertBefore(el, ref) {
    // Real insertBefore MOVES a node already in the tree — addAssistant
    // appendChild's `reply` to `log` itself before pollLoop's own
    // `log.insertBefore(reply, w.el)` repositions it, so this stub has to
    // drop any existing occurrence first or the same node ends up twice.
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
}
const document = { createElement: (tag) => new FakeEl(tag) };
const log = new FakeEl("div");
let followTail = false;
const scrollBottom = () => {};
const followBottom = () => {};
const makeTyper = (el) => ({ update() {}, finish() {}, abort() {}, retarget() {} });
const addWorkingLine = () => {
  const w = new FakeEl("div");
  w.className = "turn working";
  log.appendChild(w);
  return w;
};
const classNamesOf = (el) => el.children.map((c) => c.className);
"""


def _run(html, body):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own follow-up glue")
    script = _DOM_STUB + "\n" + body
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# The driver below reproduces exactly what `pollLoop`'s own loop body does
# each iteration: run the reset check (which also updates segBase/textBase),
# recompute segs/flatText off the current poll's `data`, then the (unchanged)
# creation guard. Wrapped in its own block so each iteration's `const segs`/
# `const flatText` get fresh bindings, same as one trip through `while (true)`.
_ONE_POLL_ITERATION = """
{{
  const data = {data};
  {reset}
  {segsflat}
  {create}
}}
"""


def test_reply_starts_a_new_bubble_after_a_mid_turn_followup(html):
    src = _addUser_src(html) + "\n" + _addAssistant_src(html)
    reset = _reply_reset_src(html)
    segsflat = _segs_flat_src(html)
    create = _create_reply_src(html)
    out = _run(html, src + """
const w = { el: addWorkingLine() };
let reply = null, typer = null, segBody = null, segMode = false, tailText = null;
let followupSeq = 0;
let seenFollowupSeq = followupSeq;
let segBase = 0, textBase = 0;
let lastSegLen = 0, lastTextLen = 0;

// First poll of the turn: no text yet, then some streams in — the reply
// bubble is created once, same as always.
""" + _ONE_POLL_ITERATION.format(
        reset=reset, segsflat=segsflat, create=create,
        data='{ segments: [{ kind: "text", text: "first" }], text: "first" }'
    ) + """

// The reader sends a follow-up while the turn is still streaming.
addUser("second message");
""" + _followup_bump_src(html) + """

// The next poll of the SAME turn — the cursor has not advanced (D687), so
// `data` still carries everything from before the follow-up too.
""" + _ONE_POLL_ITERATION.format(
        reset=reset, segsflat=segsflat, create=create,
        data=('{ segments: [{ kind: "text", text: "first" }, '
              '{ kind: "text", text: "second" }], text: "first second" }')
    ) + """

console.log(JSON.stringify({ order: classNamesOf(log) }));
""")
    assert out["order"] == [
        "turn assistant", "turn user", "turn assistant", "turn working",
    ], (
        "text after a mid-turn follow-up must start its own assistant "
        "bubble AFTER the follow-up, not keep growing the one bubble "
        "positioned before it"
    )


def test_reply_segments_are_not_duplicated_after_a_mid_turn_followup(html):
    """The new bubble a follow-up starts must render only the segments/text
    that arrive AFTER the follow-up's echo — not the whole current-turn list
    `data` still carries (the cursor deliberately does not advance past a
    mid-turn echo, D687), or the streamed answer appears twice."""
    reset = _reply_reset_src(html)
    segsflat = _segs_flat_src(html)
    out = _run(html, """
let followupSeq = 0;
let seenFollowupSeq = followupSeq;
let segBase = 0, textBase = 0;
let lastSegLen = 0, lastTextLen = 0;
let reply = null, typer = null, segBody = null, segMode = false, tailText = null;
let seenByPoll = [];

""" + _ONE_POLL_ITERATION.format(
        reset=reset, segsflat=segsflat, create="seenByPoll.push({ segs, flatText });",
        data='{ segments: [{ kind: "text", text: "hello" }], text: "hello" }'
    ) + """

followupSeq++;

""" + _ONE_POLL_ITERATION.format(
        reset=reset, segsflat=segsflat, create="seenByPoll.push({ segs, flatText });",
        data=('{ segments: [{ kind: "text", text: "hello" }, '
              '{ kind: "text", text: "world" }], text: "hello world" }')
    ) + """

console.log(JSON.stringify({ seenByPoll }));
""")
    first, second = out["seenByPoll"]
    assert first["segs"] == [{"kind": "text", "text": "hello"}]
    assert first["flatText"] == "hello"
    assert second["segs"] == [{"kind": "text", "text": "world"}], (
        "the poll right after a follow-up must only see the NEW segment, "
        "not the whole current-turn list — otherwise the fresh bubble "
        "duplicates the segment(s) already shown above the follow-up")
    assert second["flatText"] == " world"


def test_reply_is_not_reset_when_no_followup_landed(html):
    """Sanity check: without a `followupSeq` change, repeated poll iterations
    must keep reusing the SAME bubble — this fix must not turn every poll
    into a fresh bubble."""
    src = _addAssistant_src(html)
    reset = _reply_reset_src(html)
    segsflat = _segs_flat_src(html)
    create = _create_reply_src(html)
    poll = _ONE_POLL_ITERATION.format(
        reset=reset, segsflat=segsflat, create=create,
        data='{ segments: [{ kind: "text", text: "hi" }], text: "hi" }')
    out = _run(html, src + """
const w = { el: addWorkingLine() };
let reply = null, typer = null, segBody = null, segMode = false, tailText = null;
let followupSeq = 0;
let seenFollowupSeq = followupSeq;
let segBase = 0, textBase = 0;
let lastSegLen = 0, lastTextLen = 0;
""" + poll + poll + poll + """
console.log(JSON.stringify({ order: classNamesOf(log) }));
""")
    assert out["order"] == ["turn assistant", "turn working"]


def test_a_regressed_missing_reset_would_fail_this_suite(html):
    """Proof the suite has teeth: with the reset check removed (the OLD
    shape — `reply` never dropped), the follow-up's text keeps landing in
    the bubble positioned before it."""
    src = _addUser_src(html) + "\n" + _addAssistant_src(html)
    create = _create_reply_src(html)
    out = _run(html, src + """
const w = { el: addWorkingLine() };
let reply = null, typer = null, segBody = null, segMode = false, tailText = null;
let followupSeq = 0;
let segs = [];
""" + create + """
addUser("second message");
""" + _followup_bump_src(html) + """
""" + create + """
console.log(JSON.stringify({ order: classNamesOf(log) }));
""")
    assert out["order"] == ["turn assistant", "turn user", "turn working"], (
        "sanity check itself is wrong: with no reset, the second poll must "
        "reuse the SAME (already non-null) reply bubble and add nothing new"
    )

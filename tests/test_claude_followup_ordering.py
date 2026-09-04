"""Follow-up turns land above the live "✻ working…" status line (claude template).

The status line (`addWorking`) is always the last child `log` gets appended
while a turn is running — every OTHER thing added mid-turn (`addNote`, the
permission card, the streaming reply) deliberately inserts itself BEFORE that
line so it stays pinned to the bottom. `addUser` and the annotation-/
screenshot-only branch of `sendFollowUp` used to be the exception: both ended
with a plain `log.appendChild(...)`, so a follow-up sent while a turn was
already streaming landed AFTER the status line — and every reply that then
streamed in was itself inserted before that line, leaving the reader's own
just-sent message stranded below every reply that followed it. Reported by a
user as "my prompts stay at the bottom".

This runs the real `addUser` source (and the real bare-turn snippet out of
`sendFollowUp`) under node against a tiny fake DOM that actually implements
`appendChild`/`insertBefore`/`querySelector`, then reads back the resulting
child order — not a source-text grep for the fix's own code, which would stay
green even if `addUser` regressed to a bare append (see D683: a grep-only
check on this file already shipped a crash once).
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
    """`addUser`, verbatim out of the page."""
    start = html.index("function addUser(text")
    end = html.index("\n}\n", start)
    return html[start:end + 2]


def _bare_followup_src(html):
    """The annotation-/screenshot-only branch body inside `sendFollowUp` —
    the second, easily-forgotten copy of the same ordering discipline."""
    marker = "// annotation- or screenshot-only follow-up:"
    start = html.index(marker)
    end = html.index("log.appendChild(bare);", start)
    end = html.index("\n", end) + 1
    return html[start:end]


# A fake DOM minimal enough to actually exercise appendChild/insertBefore/
# querySelector(".a.b") — not stubbed-away no-ops, since the whole point is to
# observe where a node actually lands.
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
  appendChild(el) { this.children.push(el); return el; }
  insertBefore(el, ref) {
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
let scrollBottomCalls = 0;
const scrollBottom = () => { scrollBottomCalls++; };
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


def test_addUser_lands_before_a_live_working_line(html):
    """The exact reported bug: a follow-up sent mid-turn must not land after
    the status line, or every reply after it renders above the reader's own
    message."""
    src = _addUser_src(html)
    out = _run(html, src + """
addWorkingLine();
addUser("first follow-up");
addUser("second follow-up");
console.log(JSON.stringify({ order: classNamesOf(log) }));
""")
    assert out["order"] == ["turn user", "turn user", "turn working"], (
        "a follow-up bubble landed after the working line instead of before it"
    )


def test_addUser_still_plain_appends_with_no_turn_running(html):
    """No working line exists between turns (and on the very first message) —
    addUser must behave exactly as a plain append there, not silently reach
    for a stale or nonexistent status line."""
    src = _addUser_src(html)
    out = _run(html, src + """
addUser("only message");
console.log(JSON.stringify({ order: classNamesOf(log) }));
""")
    assert out["order"] == ["turn user"]


def test_addUser_re_arms_follow_and_scrolls_regardless_of_working_line(html):
    """Sending re-arms followTail and calls scrollBottom() either way — the
    ordering fix must not have disturbed that."""
    src = _addUser_src(html)
    out = _run(html, src + """
addWorkingLine();
addUser("hi");
console.log(JSON.stringify({ followTail, scrollBottomCalls }));
""")
    assert out["followTail"] is True
    assert out["scrollBottomCalls"] == 1


def test_bare_followup_turn_lands_before_a_live_working_line(html):
    """The annotation-/screenshot-only branch in `sendFollowUp` carries the
    identical defect independently of `addUser` — this is the second call
    site the fix has to cover in lockstep."""
    src = _bare_followup_src(html)
    out = _run(html, """
addWorkingLine();
""" + src + """
console.log(JSON.stringify({ order: classNamesOf(log) }));
""")
    assert out["order"] == ["turn user", "turn working"], (
        "the bare (annotation-only) follow-up landed after the working line"
    )


def test_bare_followup_turn_still_plain_appends_with_no_turn_running(html):
    src = _bare_followup_src(html)
    out = _run(html, src + """
console.log(JSON.stringify({ order: classNamesOf(log) }));
""")
    assert out["order"] == ["turn user"]


def test_a_regressed_bare_append_would_fail_this_suite(html):
    """Proof the suite has teeth: the OLD `log.appendChild(d)`-only shape,
    run through the same harness, fails the ordering assertion these tests
    make — so a source-text grep alone (which would pass on this shape
    unchanged) is not what is guarding the invariant here."""
    out = _run(html, """
function addUserOldShape(text) {
  const d = document.createElement("div");
  d.className = "turn user";
  const b = document.createElement("div");
  b.className = "bubble";
  d.appendChild(b);
  log.appendChild(d);
}
addWorkingLine();
addUserOldShape("regressed follow-up");
console.log(JSON.stringify({ order: classNamesOf(log) }));
""")
    assert out["order"] == ["turn working", "turn user"], (
        "sanity check itself is wrong: the old shape should append after the "
        "working line"
    )

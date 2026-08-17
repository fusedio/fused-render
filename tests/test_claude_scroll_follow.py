"""The transcript's follow-the-tail rule (claude template).

Whether the log keeps scrolling to the bottom as output streams is reader state
— one `followTail` flag — and NOT a geometry read at each write site. The
distinction is the bug this suite exists for: the typer's rAF used to re-derive
"am I at the bottom?" every frame from a 60px threshold, so a scroll up that had
not yet cleared 60px was answered, in that same frame, with a write of scrollTop
back to the bottom (which also kills the gesture's inertia). Escaping the stream
meant out-running it inside one frame, which is why "I can't scroll up while it
streams" was real but never reproducible on demand.

The same threshold failed the other way: anything that SHRANK the viewport
mid-run (the artifact strip appearing, the composer growing) pushed the gap past
60px on its own and stopped the follow for good, parking the reader short of the
bottom while text kept arriving off-screen.

The block runs under node against a fake scrollport, so what is tested is the
decision it reaches, not the DOM it reached it in — the same harness posture as
tests/test_claude_narrow.py.
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


def _follow_block(html):
    """The follow-the-tail block, verbatim out of the page."""
    start = html.index("let followTail = true;")
    return html[start:html.index("// Back to chats")]


# A scrollport whose geometry the test drives directly. `writes` records every
# programmatic scroll so a test can tell "followed" from "left alone".
#
# `fused` is stubbed because the extracted block reads the page's own params at
# top level (`MESSAGE_ANCHOR`, the scroll-to-a-turn deep link). The slice below
# is taken by source position, so anything that lands between its two markers
# runs here whether or not the follow logic cares about it — modelling the page
# environment is cheaper than moving unrelated code out of the way, and it means
# the next arrival in that range does not fail this file again.
_STUBS = """
const fused = { params: new Map() };
const handlers = {};
const writes = [];
const logwrap = {
  _top: 0, scrollHeight: 1000, clientHeight: 500,
  get scrollTop() { return this._top; },
  set scrollTop(v) { this._top = v; writes.push(v); },
  addEventListener(name, fn) { handlers[name] = fn; },
};
// Drive a reader gesture: move the port, then fire the scroll event the browser
// would have fired. Bypasses the setter so it is not recorded as our own write.
const userScrollTo = (top) => { logwrap._top = top; handlers.scroll(); };
const wheelUp = () => handlers.wheel({ deltaY: -120 });
const wheelDown = () => handlers.wheel({ deltaY: 120 });
// A drag DOWNWARD moves the content up: clientY grows as the finger travels.
const touchDrag = (from, to) => {
  handlers.touchstart({ touches: [{ clientY: from }] });
  handlers.touchmove({ touches: [{ clientY: to }] });
};
"""


def _run(html, call):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own scroll glue")
    script = _STUBS + "\n" + _follow_block(html) + "\n" + call
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_streaming_follows_the_tail_until_the_reader_scrolls_up(html):
    """The default is to follow, and a wheel flick up ends it immediately.

    No distance threshold on the wheel path on purpose: a threshold is a race
    the next animation frame wins."""
    out = _run(html, """
logwrap._top = 500;                       // sitting at the bottom
followBottom();                           // streaming output: follows
const followedAtRest = writes.length;
wheelUp();
followBottom(); followBottom();           // two more frames of output
console.log(JSON.stringify({ followedAtRest, afterWheelUp: writes.length, followTail }));
""")
    assert out["followedAtRest"] == 1
    assert out["afterWheelUp"] == 1, "output kept yanking the reader back after a scroll up"
    assert out["followTail"] is False


def test_a_scroll_up_shorter_than_the_old_threshold_still_stops_the_follow(html):
    """The exact regression: 20px up is well inside the old 60px "near bottom"
    window, so the stream used to treat the reader as still pinned and scroll
    them back down mid-gesture."""
    out = _run(html, """
logwrap._top = 500;
handlers.scroll();                        // establish lastTop/lastHeight at rest
userScrollTo(480);                        // 20px up — inside the old 60px window
followBottom();
console.log(JSON.stringify({ writes: writes.length, followTail, top: logwrap._top }));
""")
    assert out["followTail"] is False
    assert out["writes"] == 0
    assert out["top"] == 480, "the reader's own scroll position was overwritten"


def test_a_short_touch_drag_stops_the_follow_too(html):
    """The touch path used to ask geometry (`followTail = nearBottom()`), so a
    20px drag — inside the old 60px window — RE-ARMED the follow it was trying to
    break, and the next frame scrolled the reader back. Direction, not distance."""
    out = _run(html, """
logwrap._top = 500; handlers.scroll();
touchDrag(400, 420);                      // finger down 20px = content up 20px
followBottom();
console.log(JSON.stringify({ followTail, writes: writes.length }));
""")
    assert out["followTail"] is False
    assert out["writes"] == 0


def test_a_touch_drag_the_other_way_keeps_following(html):
    """Dragging UP is reaching for the newest output, not away from it."""
    out = _run(html, """
logwrap._top = 500; handlers.scroll();
touchDrag(420, 400);                      // finger up = content down
followBottom();
console.log(JSON.stringify({ followTail, writes: writes.length }));
""")
    assert out["followTail"] is True
    assert out["writes"] == 1


def test_scrolling_back_down_by_wheel_alone_does_not_re_arm(html):
    """Only ARRIVING at the bottom re-arms. A few notches down from the middle of
    a long transcript is still reading, and re-following there would rip the
    reader to the tail while they were on their way."""
    out = _run(html, """
logwrap._top = 500; handlers.scroll();
wheelUp(); userScrollTo(300);
wheelDown(); userScrollTo(340);           // heading back, nowhere near the end
followBottom();
console.log(JSON.stringify({ followTail, writes: writes.length }));
""")
    assert out["followTail"] is False
    assert out["writes"] == 0


def test_returning_to_the_bottom_re_arms_the_follow(html):
    """Scrolling back down is how the reader says "keep up with it again" —
    there is no other affordance, so this path has to work."""
    out = _run(html, """
logwrap._top = 500; handlers.scroll();
wheelUp(); userScrollTo(300);
const away = followTail;
userScrollTo(500);                        // back to the bottom
followBottom();
console.log(JSON.stringify({ away, back: followTail, writes: writes.length }));
""")
    assert out["away"] is False
    assert out["back"] is True
    assert out["writes"] == 1


def test_content_getting_shorter_does_not_read_as_a_scroll_up(html):
    """A shrink clamps scrollTop down without the reader touching anything: the
    typer clamps `shown` back when the authoritative text is shorter, chips
    resolve to smaller images, the working line is removed at run end. Reading
    that as a gesture was the second way the follow died mid-turn."""
    out = _run(html, """
logwrap._top = 500; handlers.scroll();
logwrap.scrollHeight = 700;               // content shrank...
logwrap._top = 200;                       // ...so the browser clamped the port
handlers.scroll();
followBottom();
console.log(JSON.stringify({ followTail, writes: writes.length }));
""")
    assert out["followTail"] is True, "a content shrink was mistaken for the reader scrolling up"
    assert out["writes"] == 1


def test_a_smaller_viewport_does_not_end_the_follow(html):
    """The artifact strip appearing (or the composer growing) shrinks
    clientHeight with no gesture at all. Under the old threshold that alone put
    the gap past 60px and stopped the follow permanently."""
    out = _run(html, """
logwrap._top = 500; handlers.scroll();
logwrap.clientHeight = 380;               // artifact strip took 120px
followBottom();
console.log(JSON.stringify({ followTail, lastWrite: writes[writes.length - 1] }));
""")
    assert out["followTail"] is True
    assert out["lastWrite"] == 1000, "the follow stopped when the viewport shrank"


def test_sending_a_message_re_arms_the_follow(html):
    """addUser sets the flag back to true — the answer to what the reader just
    asked has to stream in front of them, not somewhere below the fold."""
    # Matched on the name alone, not the full signature: `addUser` gained a
    # `uuid` parameter for the scroll-to-a-turn deep link, and this test is about
    # the flag it sets, not how many arguments it takes.
    src = html[html.index("function addUser(text"):]
    body = src[:src.index("\n}\n")]
    assert "followTail = true;" in body
    assert "scrollBottom();" in body


def test_a_blocked_run_scrolls_even_a_reader_who_scrolled_away(html):
    """The exception's mechanism, not just its call site: scrollBottom() ignores
    the flag, followBottom() honours it. That difference is the whole reason
    showPermissionCard may keep calling the former."""
    out = _run(html, """
logwrap._top = 500; handlers.scroll();
wheelUp(); userScrollTo(200);
followBottom();                           // streaming output: leaves them alone
const afterFollow = writes.length;
scrollBottom();                           // a card the run is blocked on: does not
console.log(JSON.stringify({ afterFollow, afterCard: writes.length, followTail }));
""")
    assert out["afterFollow"] == 0
    assert out["afterCard"] == 1


def test_only_a_blocked_run_may_scroll_the_reader_unasked(html):
    """Everything that streams goes through followBottom(). The permission card
    is the deliberate exception: the run is blocked on it, so a reader who has
    scrolled away is waiting on something they cannot see."""
    typer = html[html.index("function makeTyper(bodyEl)"):]
    typer = typer[:typer.index("\n}\n")]
    assert "followBottom();" in typer
    assert "nearBottom()" not in typer, "the typer must not re-derive pinning per frame"

    card = html[html.index("function showPermissionCard(card, working)"):]
    card = card[:card.index("\n}\n")]
    assert "scrollBottom();" in card

    note = html[html.index("function addNote(text, working, glyph)"):]
    note = note[:note.index("\n}\n")]
    assert "followBottom();" in note

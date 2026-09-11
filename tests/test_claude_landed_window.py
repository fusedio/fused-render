"""A send into a live host must not re-type the reply already on screen.

THE REPORTED BUG (PR #1119), reproduced in the real app before this test was
written: with a conversation already going, sending a second message made the
previous answer appear a SECOND time, in a fresh bubble under the new message,
for about a second — then it was replaced by the real reply. "The last response
gets duplicated into the UI before the new response starts streaming."

WHY IT HAPPENS. `_read_current_turn`'s cursor only advances past a turn
boundary it has PROVEN — a `_starts_new_turn` echo with a `result` before it —
so a message sent into a still-live host reopens the poll window on the
PREVIOUS reply and keeps it there until the new turn's echo lands (several
polls: the CLI writes `system/init` and `system/status` first). `sendMessage`'s
live-host road starts a FRESH `pollLoop` for those polls, and it used to begin
with `segBase`/`textBase` at zero — so the whole previous reply was typed into
the new bubble, then overwritten once the cursor finally moved.

`landedWindow` is the fix: the loop that SETTLES a reply records the window it
settled, the next loop on the SAME run opens with that as its base, and the
base retires itself the moment the window stops opening on it (the cursor
having stepped over the boundary — from that poll on the payload is the new
turn alone, and slicing it against the old reply's length would cut its head
off).

Node probes over the shipping source, the same discipline as
test_claude_followup_reply_bubble.py: the three snippets are extracted from
template.html by textual anchors and wired together the way `pollLoop`'s own
loop body wires them, so an anchor that stops matching is a test error rather
than a silent pass.
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


def _seed_src(html):
    """`pollLoop`'s base seeding: consume `landedWindow` when this loop opens
    on the same run the last one settled."""
    start = html.index("  // What the previous loop on THIS run left on screen")
    end = html.index("landedWindow = null;", start)
    return html[start:end + len("landedWindow = null;")]


def _segs_flat_src(html):
    """The retire check and the `segs`/`flatText` slice right after it."""
    start = html.index("const fullSegs = Array.isArray(data.segments)")
    end = html.index("lastTextLen = fullText.length;", start)
    return html[start:html.index("\n", end) + 1]


def _record_src(html):
    """What the done branch records for the next loop, ownership guard and
    all."""
    start = html.index("        // AND THE WINDOW IS NOW ON SCREEN")
    end = html.index("          : null;", start)
    end = html.index("\n        }\n", end) + len("\n        }\n")
    return html[start:end]


_HARNESS = """
let landedWindow = null;
let segBase = 0, textBase = 0;
// NOT `baseText`: the seeding snippet extracted from the page declares that
// one itself, and a second declaration here would shadow the shipping line
// this test exists to exercise.
let lastSegLen = 0, lastTextLen = 0;
const seg = (t) => ({ kind: "text", text: t });
const out = [];
"""


def _run(body):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own poll-loop glue")
    proc = subprocess.run(["node", "-e", _HARNESS + body],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _poll(html, data):
    """One trip through the loop body, as far as this fix reaches: the retire
    check, the slice, and what the bubble would be handed."""
    return """
{
  const data = %s;
  %s
  out.push({ segs: segs.map((s) => s.text), flat: flatText, segBase, textBase });
}
""" % (json.dumps(data), _segs_flat_src(html))


PREV = "The answer that was already on screen."
NEW = "A brand new answer."


def test_a_settled_turn_is_recorded_for_the_next_loop(html):
    """The done branch hands the next loop the window it settled — but only
    where the text STAYS: a dropped bubble is not a prefix of anything."""
    body = _record_src(html) + """
out.push(landedWindow);
console.log(JSON.stringify(out));
"""
    kept = _run("""
const run_id = "r1";
const fullSegs = [%s];
const fullText = %s;
const end = { keepText: true };
const seat = 1;
let loopSeq = 1;
""" % (json.dumps(PREV), json.dumps(PREV)) + body)
    assert kept[0] == {"run_id": "r1", "segs": 1, "text": PREV}

    dropped = _run("""
const run_id = "r1";
const fullSegs = [%s];
const fullText = %s;
const end = { keepText: false };
const seat = 1;
let loopSeq = 1;
""" % (json.dumps(PREV), json.dumps(PREV)) + body)
    assert dropped[0] is None, "a reply that was removed is not a base"


def test_a_send_into_a_live_host_does_not_retype_the_reply_on_screen(html):
    """THE BUG. The window reopens on the previous reply and stays there for
    several polls; not one byte of it may reach the new bubble."""
    frames = _run("""
const run_id = "r1";
landedWindow = { run_id: "r1", segs: 1, text: %s };
""" % json.dumps(PREV) + _seed_src(html) + """
""" + _poll(html, {"segments": [{"kind": "text", "text": PREV}], "text": PREV})
        + _poll(html, {"segments": [{"kind": "text", "text": PREV}], "text": PREV})
        + """
console.log(JSON.stringify(out));
""")
    for i, f in enumerate(frames):
        assert f["segs"] == [], "poll %d re-typed the previous reply" % i
        assert f["flat"] == "", "poll %d re-typed the previous reply" % i


def test_the_new_reply_still_lands_once_it_starts(html):
    """...and the fix must not swallow the answer it is protecting: the new
    turn's own text, arriving in the SAME window behind the old reply, is the
    only thing the new bubble gets."""
    frames = _run("""
const run_id = "r1";
landedWindow = { run_id: "r1", segs: 1, text: %s };
""" % json.dumps(PREV) + _seed_src(html) + """
""" + _poll(html, {"segments": [{"kind": "text", "text": PREV}], "text": PREV})
        + _poll(html, {"segments": [{"kind": "text", "text": PREV},
                                    {"kind": "text", "text": NEW}],
                       "text": PREV + NEW})
        + """
console.log(JSON.stringify(out));
""")
    assert frames[0]["segs"] == []
    assert frames[1]["segs"] == [NEW], "the new reply, and nothing of the old"
    assert frames[1]["flat"] == NEW


def test_the_base_retires_when_the_cursor_steps_over_the_boundary(html):
    """The poll after the cursor advances carries the NEW turn alone. Slicing
    that against the old reply's length would cut its head off — or blank it
    when the new answer is shorter, which is the commoner case."""
    short = "OK"
    frames = _run("""
const run_id = "r1";
landedWindow = { run_id: "r1", segs: 1, text: %s };
""" % json.dumps(PREV) + _seed_src(html) + """
""" + _poll(html, {"segments": [{"kind": "text", "text": PREV}], "text": PREV})
        + _poll(html, {"segments": [{"kind": "text", "text": short}], "text": short})
        + """
console.log(JSON.stringify(out));
""")
    assert frames[0]["segs"] == []
    assert frames[1]["segs"] == [short], "the whole new reply, not a slice of it"
    assert frames[1]["flat"] == short
    assert frames[1]["textBase"] == 0, "the base retired itself"


def test_a_different_run_does_not_inherit_the_base(html):
    """`landedWindow` is per run. A loop on a DIFFERENT run — a respawn, a
    fresh `start` — opens on its own turn from byte zero, and hiding a prefix
    it never rendered would drop the head of that turn's reply."""
    frames = _run("""
const run_id = "r2";
landedWindow = { run_id: "r1", segs: 1, text: %s };
""" % json.dumps(PREV) + _seed_src(html) + """
""" + _poll(html, {"segments": [{"kind": "text", "text": NEW}], "text": NEW}) + """
console.log(JSON.stringify(out));
""")
    assert frames[0]["segs"] == [NEW]
    assert frames[0]["flat"] == NEW


# ---- the two Bugbot findings on the first cut of this fix -------------------


def test_a_blank_poll_does_not_retire_the_landed_base(html):
    """BUGBOT, HIGH. A send into an IDLE host leaves out.jsonl ending on the
    previous `result`, so agent.py reads the run as idle and `pending_echo`
    blanks BOTH `text` and `segments` until this send's echo lands.

    An empty string starts with nothing, so the first cut of the retire check
    read that blank payload as "the cursor stepped over the boundary" and threw
    the base away — and the polls after it, still carrying the previous reply
    because the cursor had NOT moved, typed it into the new bubble. Reproduced
    in the running app with a host that takes a moment to start: the duplicate
    came straight back.
    """
    frames = _run("""
const run_id = "r1";
landedWindow = { run_id: "r1", segs: 1, text: %s };
""" % json.dumps(PREV) + _seed_src(html) + """
"""
        # `pending_echo` blanks the payload...
        + _poll(html, {"segments": [], "text": ""})
        # ...and the cursor has still not moved, so this is the OLD reply.
        + _poll(html, {"segments": [{"kind": "text", "text": PREV}], "text": PREV})
        + """
console.log(JSON.stringify(out));
""")
    assert frames[0]["segs"] == [] and frames[0]["flat"] == ""
    assert frames[0]["textBase"] == len(PREV), (
        "a blank payload proves nothing; the base must survive it")
    assert frames[1]["segs"] == [], "the previous reply was re-typed"
    assert frames[1]["flat"] == "", "the previous reply was re-typed"


def test_a_segments_only_window_still_retires_the_base(html):
    """...and the guard for that must not swallow a real window. A turn that
    opens on a tool call carries segments with no prose at all: `text` is empty
    but the payload is the NEW turn, and slicing its segments against the old
    reply's count would drop them."""
    frames = _run("""
const run_id = "r1";
landedWindow = { run_id: "r1", segs: 1, text: %s };
""" % json.dumps(PREV) + _seed_src(html) + """
""" + _poll(html, {"segments": [{"kind": "tool", "text": ""}], "text": ""}) + """
console.log(JSON.stringify(out));
""")
    assert frames[0]["segBase"] == 0, "a segments-only window is a real window"
    assert len(frames[0]["segs"]) == 1, "the new turn's own segment, not a slice"


def test_a_superseded_loop_does_not_overwrite_the_record(html):
    """BUGBOT, MEDIUM. A respawn kills this run and starts a NEW pollLoop while
    the old one is still on its way back from the poll it already sent. Its
    late arrival in the done branch would overwrite — or null — the record the
    newer loop has already made, and the send after that would open with no
    base and flash the previous reply again.

    Same `loopSeq === seat` test the `run` param and the stop note in that very
    block already make."""
    newer = {"run_id": "r1", "segs": 2, "text": "what the newer loop settled"}
    body = _record_src(html) + """
out.push(landedWindow);
console.log(JSON.stringify(out));
"""
    stale = _run("""
const run_id = "r1";
const fullSegs = [%s];
const fullText = %s;
const end = { keepText: true };
const seat = 1;      // this loop
let loopSeq = 2;     // ...but a newer one has taken over
landedWindow = %s;
""" % (json.dumps(PREV), json.dumps(PREV), json.dumps(newer)) + body)
    assert stale[0] == newer, "the superseded loop wrote over the newer record"

    owner = _run("""
const run_id = "r1";
const fullSegs = [%s];
const fullText = %s;
const end = { keepText: true };
const seat = 2;
let loopSeq = 2;     // still the newest loop
landedWindow = null;
""" % (json.dumps(PREV), json.dumps(PREV)) + body)
    assert owner[0] == {"run_id": "r1", "segs": 1, "text": PREV}, (
        "the loop that owns the turn still records it")

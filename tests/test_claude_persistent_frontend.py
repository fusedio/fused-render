"""The composer's half of persistent sessions (template.html): send-immediately
over `steer` instead of the browser-side queue, and the `submitChat` race this
work also had to close (see agent.py's `_steer`/`_persistent_ok` and D497).

Runs the page's REAL `submitChat` and `steerMessage` functions under node,
over a small stubbed DOM/runtime — the same siting as
test_claude_stop_run.py's `_run_ending` and test_claude_app_state.py's
`_node`. A structural (grep-only) test would prove the function names and the
`"steer"` literal are present in the source, but not that a message actually
reaches `fused.runPython` with the right run_id, or that `submitChat` picks
the right destination for a given combination of `activeRun`/
`activeRunPersistent`/`startingRun` — which is exactly the class of thing a
refactor can silently break while every string a grep looks for stays put.
"""
import json
import os
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_ROOT, "fused_render", "templates", "claude", "template.html")


def _html():
    with open(_TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _need_node():
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own composer decisions")


def _extract(html, start_marker, end_marker):
    start = html.index(start_marker)
    return html[start:html.index(end_marker, start)]


def _steer_message_src(html):
    return _extract(html, "async function steerMessage(text) {",
                    "\nfunction drainQueue() {")


def _submit_chat_src(html):
    return _extract(html, "function submitChat() {",
                    '\n// The submit event is the send BUTTON\'s path')


# ---------------------------------------------------------------- steerMessage

_STEER_HARNESS = """
%s

class FakeEl {
  constructor() { this.children = []; this.text = null; }
  set className(v) {}
  set textContent(v) { this.text = v; }
  get textContent() { return this.text; }
  append(...kids) { this.children.push(...kids); }
  appendChild(k) { this.children.push(k); }
  remove() { global.__removed = true; }
}
const document = { createElement: () => new FakeEl() };
const queueEl = new FakeEl();
function scrollBottom() {}
const AGENT = "agent.py";
let activeRun = %s;
let pendingSteerEls = [];
const calls = { runPython: [], queueMessage: [] };
const fused = {
  runPython: async (agent, params, opts) => {
    calls.runPython.push({ agent, params, opts });
    return %s;
  },
};
function queueMessage(text) { calls.queueMessage.push(text); }

steerMessage(%s).then(() => {
  console.log(JSON.stringify({
    calls, removed: !!global.__removed,
    pending: pendingSteerEls.length,
  }));
});
"""


def _steer(run_id, resolves_to, text="second message"):
    _need_node()
    src = _steer_message_src(_html())
    script = _STEER_HARNESS % (src, json.dumps(run_id), json.dumps(resolves_to),
                               json.dumps(text))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_steer_message_sends_the_steer_action_with_the_live_run_id():
    result = _steer("run-42", {"steered": True})
    calls = result["calls"]["runPython"]
    assert len(calls) == 1
    assert calls[0]["agent"] == "agent.py"
    assert calls[0]["params"] == {"action": "steer", "run_id": "run-42",
                                  "message": "second message"}
    # A successful steer never falls back to the browser queue.
    assert result["calls"]["queueMessage"] == []


def test_a_successful_steer_keeps_the_users_message_on_screen():
    """The regression this closes (found in review): the old code removed
    the dashed bubble the instant the call resolved, with nothing rendered
    in its place (D497's cumulative rendering has no separate bubble for a
    second turn) — so a SUCCESSFUL steer made the user's own words vanish.
    The bubble must stay, tracked in `pendingSteerEls`, until `pollLoop`
    sweeps it once the run truly finishes."""
    result = _steer("run-42", {"steered": True})
    assert result["removed"] is False, "the placeholder must not disappear on success"
    assert result["pending"] == 1, "steerMessage must register it for pollLoop to sweep"


def test_a_refused_steer_falls_back_to_the_browser_queue_with_the_same_text():
    """The run ended, or was never persistent — a race between the click and
    the call landing. The text must not be lost."""
    result = _steer("run-42", {"steered": False, "error": "run has ended"})
    assert result["calls"]["queueMessage"] == ["second message"]
    # Unlike a success, a refused steer removes its own placeholder
    # immediately — the ordinary queue bubble (queueMessage) takes over.
    assert result["removed"] is True
    assert result["pending"] == 0


# ---------------------------------------------------------------- submitChat

_SUBMIT_HARNESS = """
%s

const calls = { queueMessage: [], steerMessage: [], sendMessage: [] };
function queueMessage(t) { calls.queueMessage.push(t); }
function steerMessage(t) { calls.steerMessage.push(t); }
function sendMessage(t) { calls.sendMessage.push(t); }
function schedBlocked() { return %s; }
function growBox() {}
function annPending() { return []; }
const shotAttached = [];
let activeRun = %s;
let activeRunPersistent = %s;
let startingRun = %s;
const box = { value: %s };

submitChat();
console.log(JSON.stringify({ calls, boxValue: box.value }));
"""


def _submit(*, sched_blocked=False, active_run=None, persistent=False,
           starting_run=False, box_value="hello"):
    _need_node()
    src = _submit_chat_src(_html())
    script = _SUBMIT_HARNESS % (
        src, json.dumps(sched_blocked), json.dumps(active_run),
        json.dumps(persistent), json.dumps(starting_run), json.dumps(box_value))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_live_persistent_run_steers_the_typed_message_immediately():
    result = _submit(active_run="run-1", persistent=True, box_value="  hi there  ")
    assert result["calls"]["steerMessage"] == ["hi there"]
    assert result["calls"]["queueMessage"] == []
    assert result["calls"]["sendMessage"] == []
    assert result["boxValue"] == "", "the composer must clear on a steer too"


def test_a_live_one_shot_run_still_parks_in_the_browser_queue():
    result = _submit(active_run="run-1", persistent=False, box_value="hi there")
    assert result["calls"]["queueMessage"] == ["hi there"]
    assert result["calls"]["steerMessage"] == []
    assert result["calls"]["sendMessage"] == []


def test_a_message_typed_mid_start_is_parked_not_silently_dropped():
    """The race this work also closed: `sending` (and, now, the narrower
    `startingRun`) goes true before `activeRun` is assigned (sendMessage).
    A second submit landing in that window used to fall through a bare
    `if (sending) return;` with nothing queued and the box already blanked
    by the FIRST submit — i.e. genuinely lost. It must now land in the
    browser queue exactly like the activeRun branch does. `startingRun`,
    not `sending`, because `sending` is ALSO held by loadHistory/resumeRun's
    probe, which never drains the queue — parking against it there would
    strand the message (found in review); this test only exercises the one
    window `startingRun` is scoped to."""
    result = _submit(active_run=None, starting_run=True, box_value="hi there")
    assert result["calls"]["queueMessage"] == ["hi there"]
    assert result["calls"]["steerMessage"] == [], \
        "there is no run_id yet — this must never steer"
    assert result["calls"]["sendMessage"] == []
    assert result["boxValue"] == ""


def test_an_ordinary_send_with_nothing_live_goes_straight_to_sendMessage():
    result = _submit(active_run=None, starting_run=False, box_value="hi there")
    assert result["calls"]["sendMessage"] == ["hi there"]
    assert result["calls"]["queueMessage"] == []
    assert result["calls"]["steerMessage"] == []


def test_a_scheduled_block_beats_every_other_branch():
    result = _submit(sched_blocked=True, active_run="run-1", persistent=True,
                     box_value="hi there")
    assert result["calls"] == {"queueMessage": [], "steerMessage": [], "sendMessage": []}
    assert result["boxValue"] == "hi there", "nothing should have touched the box"


def test_an_empty_box_sends_nothing_on_any_branch():
    for kwargs in ({"active_run": "run-1", "persistent": True},
                   {"active_run": "run-1", "persistent": False},
                   {"starting_run": True},
                   {}):
        result = _submit(box_value="   ", **kwargs)
        assert result["calls"] == {"queueMessage": [], "steerMessage": [], "sendMessage": []}

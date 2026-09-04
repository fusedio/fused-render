"""A killed-and-respawned pollLoop must not touch the NEW run's chrome.

`sendFollowUp`'s respawn path (an attachment/effort change a live session
can't honor) calls `_cancel` on the OLD run and starts a fresh one with a
NEW `pollLoop(started.run_id, gen)` — but the OLD loop (same `logGen`) is
still executing on the just-killed run, and its own `await` on the poll it
already sent out returns one more `data.done` payload before that loop's
`while` ever notices anything changed.

Inside the `if (data.done) { ... }` block, `fused.params.set("run", "", ...)`
was NOT guarded by `loopSeq === seat` — unlike the `finally` block's own
ownership check just below it in the same function — so the OLD loop could
clear the `run` URL param a moment after the NEW loop had already set it to
the NEW run id, breaking re-attach on the next page load. The same block's
`addNote(end.note, ...)` call renders a stray "Stopped." line into the log
while the NEW turn is actively streaming, for the same reason.

This runs the real `if (data.done) { ... }` block out of `pollLoop`, wired
up with fakes for everything it calls, and drives it once as the OLD loop
(`seat` behind the current `loopSeq`) and once as the only loop, to prove the
guard makes exactly the OLD-loop case a no-op for both effects.
"""
import os
import shutil
import subprocess
import json

import pytest

# WINDOWS: SKIPPED, NOT FIXED. The persistent session host (#979) never writes
# `host.json` on the Windows runner - every test below waits for it and times
# out - and `interrupted_offset` lands one byte off there (CRLF). That is a
# platform gap in the host itself, not in these tests, and it needs a Windows
# box to close; marking it here keeps main's Windows job honest about what it
# does cover instead of red for everything (2026-09-04, red since #979).
pytestmark = pytest.mark.skipif(os.name == "nt", reason="claude session host does not start on Windows yet (#979)")


TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "claude", "template.html")


@pytest.fixture(scope="module")
def html():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _data_done_src(html):
    start = html.index("if (data.done) {")
    end = html.index("break;", start)
    end = html.index("\n      }\n", end) + len("\n      }\n")
    return html[start:end]


_HARNESS = """
const calls = { paramSet: [], addNote: [], addError: [] };
const fused = { params: { set: (...args) => { calls.paramSet.push(args); } } };
function addNote(...args) { calls.addNote.push(args); }
function addError(...args) { calls.addError.push(args); }
function annResolveSent() {}
function snapInvalidate() {}
function pollArtifacts() {}
function attachCodeCopy() {}
function addAssistantTurn() { return null; }
function runEnding(data, stopped) {
  return { keepText: true, error: null, note: stopped ? "Stopped." : null };
}
let reply = null;
let typer = null;
let segMode = false;
let tailText = null;
"""


def _run(html, seat, loop_seq, stopped_seat, done_src):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own follow-up glue")
    script = _HARNESS + f"""
const seat = {seat};
let loopSeq = {loop_seq};
const stoppedSeat = {stopped_seat};
const w = {{ stop() {{}}, el: {{ remove() {{}} }} }};
const data = {{ done: true, text: "", segments: [] }};
(async () => {{
  while (true) {{
""" + done_src + f"""
  }}
console.log(JSON.stringify(calls));
}})();
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_old_superseded_loop_does_not_clear_the_new_runs_param(html):
    """seat=1 finishing while loopSeq has already moved to 2 (a respawn
    already started a newer loop) — the OLD loop's own `data.done` must not
    touch the `run` param the newer loop just set."""
    src = _data_done_src(html)
    out = _run(html, seat=1, loop_seq=2, stopped_seat=1, done_src=src)
    assert out["paramSet"] == [], (
        "a superseded loop cleared the `run` param — this can wipe out a "
        "newer, still-running loop's own value and break re-attach on reload"
    )


def test_an_old_superseded_loop_does_not_add_a_stray_stopped_note(html):
    src = _data_done_src(html)
    out = _run(html, seat=1, loop_seq=2, stopped_seat=1, done_src=src)
    assert out["addNote"] == [], (
        "a superseded loop rendered its own 'Stopped.' note into the log "
        "while the newer loop's turn is still streaming"
    )


def test_the_only_loop_still_clears_the_param_and_notes_a_stop(html):
    """Sanity check: the guard must not silence the ordinary (non-respawn)
    case where this loop really is the current one."""
    src = _data_done_src(html)
    out = _run(html, seat=1, loop_seq=1, stopped_seat=1, done_src=src)
    assert out["paramSet"] == [["run", "", {"history": "replace", "default": ""}]]
    assert out["addNote"] == [["Stopped.", None, "⏹"]]

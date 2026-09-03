"""Re-attaching to a run the current frame did not start (claude template).

The run id used to live in exactly one place — the `run` param on one history
entry — so a chat reopened any other way lost it. The reproduction: send a
message in chat A, press Back (which lands on whatever entry is behind it), then
reach chat A again from the session list. The detached claude process is still
streaming into its run dir, but the page has no id to attach to, renders the
mid-flight transcript (which ends at the user's own message, correctly — the
reply is not written yet) and shows no working line at all. Arriving with Back
worked only because that entry still carried `run`.

`_live_run` is the missing lookup: the server knows which runs are still alive.
These tests cover the answer it gives and the two client paths that ask.
"""
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess

import pytest


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "claude", "template.html")


@pytest.fixture(scope="module")
def template():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def html_pane(template):
    """The template with `// …` comments stripped, so a source pin cannot be
    satisfied by prose that merely NAMES the call it is looking for. Same guard
    as test_claude_kind.py's _pane_code."""
    return re.sub(r"(?m)^\s*//.*$", "", template)


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    mod = _load_agent()
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    f = d / "index.html"
    f.write_text("<html></html>")
    return str(f)


def _run_dir(agent, name, *, file, resumed_from="", session=None, alive=True):
    """A run dir shaped the way `_start` leaves one."""
    d = os.path.join(agent.RUNS, name)
    os.makedirs(d)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"file": file, "message": "hi", "resumed_from": resumed_from,
                   "mode": "prompt"}, f)
    # The pid decides liveness. os.getpid() is alive by definition; pid 1 would
    # be too, so a dead run gets a pid that cannot exist.
    with open(os.path.join(d, "pid"), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()) if alive else "2147483646")
    if session is not None:
        with open(os.path.join(d, "session"), "w", encoding="utf-8") as f:
            f.write(session)
    return d


def test_a_live_run_for_this_session_is_found(agent, target):
    _run_dir(agent, "20260817-120000-aaa", file=target, resumed_from="sess-A")
    assert agent._live_run(target, "sess-A") == {"run_id": "20260817-120000-aaa"}


def test_a_finished_run_is_not_offered(agent, target):
    """The whole point is adopting something still streaming. A dead run would
    make the page attach, poll once, and redraw a turn that already ended."""
    _run_dir(agent, "20260817-120000-aaa", file=target, resumed_from="sess-A",
             alive=False)
    assert agent._live_run(target, "sess-A") == {"run_id": ""}


def test_another_chat_s_run_is_not_adopted(agent, target, tmp_path):
    """Matching is on the target first: two chats can be live at once, and
    picking the wrong one would stream someone else's reply into this log."""
    other = str(tmp_path / "proj" / "other.html")
    _run_dir(agent, "20260817-120000-bbb", file=other, resumed_from="sess-A")
    assert agent._live_run(target, "sess-A") == {"run_id": ""}


def test_another_session_of_the_same_file_is_not_adopted(agent, target):
    _run_dir(agent, "20260817-120000-ccc", file=target, resumed_from="sess-B")
    assert agent._live_run(target, "sess-A") == {"run_id": ""}


def test_a_forked_session_id_still_matches(agent, target):
    """`--fork-session` hands back a NEW session id, which the run's poll
    writes to its `session` file — so the id the page holds may be that one OR
    the `resumed_from` in meta.json. Both identify
    the same chat, so either matching is a match — and an id that is neither
    still does not."""
    _run_dir(agent, "20260817-120000-ddd", file=target, resumed_from="sess-old",
             session="sess-new")
    assert agent._live_run(target, "sess-new") == {"run_id": "20260817-120000-ddd"}
    assert agent._live_run(target, "sess-old") == {"run_id": "20260817-120000-ddd"}
    assert agent._live_run(target, "sess-other") == {"run_id": ""}


def test_an_unpolled_new_chat_is_still_found_by_its_cli_minted_id(agent, target):
    """The Back-mid-start blind spot (Akshil, 2026-08-19): a NEW chat left
    before its first poll has an empty `resumed_from` AND no `session` file —
    the first poll is what writes that file, and no poll ever ran. The id the
    reopened page holds (the transcript's filename) is sitting in out.jsonl's
    first system row, so the lookup falls back to reading it there; without the
    fallback this run was invisible and the reopened chat never streamed."""
    d = _run_dir(agent, "20260819-090000-abc", file=target, resumed_from="")
    with open(os.path.join(d, "out.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "system", "subtype": "init",
                            "session_id": "sess-minted"}) + "\n")
    assert agent._live_run(target, "sess-minted") == {"run_id": "20260819-090000-abc"}
    # The fallback widens what can MATCH, never what matches anything: an id
    # that is neither still finds no run.
    assert agent._live_run(target, "sess-other") == {"run_id": ""}


def test_an_unpolled_run_with_no_output_yet_stays_invisible_by_id(agent, target):
    """The narrowest honest answer for a run whose CLI has not spoken: no
    out.jsonl (or an unparsable head) yields no id, so a session-scoped lookup
    finds nothing — while the target-only form still can."""
    _run_dir(agent, "20260819-091500-def", file=target, resumed_from="")
    assert agent._live_run(target, "sess-minted") == {"run_id": ""}
    assert agent._live_run(target, "") == {"run_id": "20260819-091500-def"}


def test_the_newest_live_run_wins(agent, target):
    _run_dir(agent, "20260817-090000-old", file=target, resumed_from="sess-A")
    _run_dir(agent, "20260817-150000-new", file=target, resumed_from="sess-A")
    assert agent._live_run(target, "sess-A") == {"run_id": "20260817-150000-new"}


def test_a_newer_dead_run_does_not_hide_an_older_live_one(agent, target):
    """Newest-first is the scan ORDER, not the answer: the finished turn a reader
    just abandoned is newer than the one still going in another frame."""
    _run_dir(agent, "20260817-090000-old", file=target, resumed_from="sess-A")
    _run_dir(agent, "20260817-150000-new", file=target, resumed_from="sess-A",
             alive=False)
    assert agent._live_run(target, "sess-A") == {"run_id": "20260817-090000-old"}


def test_the_scan_reaches_back_as_far_as_it_claims(agent, target):
    """_LIVE_SCAN_LIMIT is what keeps this cheap on a machine that has been
    chatting for weeks, and it is also the one way a live run can go unseen —
    which is the original bug returning. Pin the boundary: the oldest dir inside
    the window is found, one past it is not."""
    live = "20260101-000000-live"
    _run_dir(agent, live, file=target, resumed_from="sess-A")
    # Newer dirs, all dead, filling the window exactly up to `live`.
    for i in range(agent._LIVE_SCAN_LIMIT - 1):
        _run_dir(agent, "202602%02d-000000-dead" % (i + 1), file=target,
                 resumed_from="sess-A", alive=False)
    assert agent._live_run(target, "sess-A") == {"run_id": live}

    _run_dir(agent, "20260301-000000-dead", file=target, resumed_from="sess-A",
             alive=False)
    assert agent._live_run(target, "sess-A") == {"run_id": ""}, (
        "one dir past the window and the live run is invisible — if this limit "
        "ever needs raising, this is the test that says so"
    )


# -- the unbounded lookup a LOCK needs (A2) ------------------------------------
#
# `_live_run`'s cap is right for its original job (a page re-attaching to its own
# run: if the id is not among the newest few, that frame has been gone long
# enough that adopting is pointless) and wrong for the job canvases.py adds —
# deciding whether to make the embedded workbench read-only because a session is
# editing the clone. There the answer must be RELIABLE, not cheap-and-usually-
# right: a live run that fell out of the window reads as "nobody is editing" and
# the lock silently does not engage. `limit=None` is the unbounded form.


def test_the_lock_lookup_sees_a_live_run_past_the_scan_window(agent, target):
    """The A2 bug: with the default cap, a live run buried under more than
    _LIVE_SCAN_LIMIT newer dirs is invisible. Nothing prunes RUNS, so on a busy
    machine that is the normal case, not the exotic one."""
    live = "20260101-000000-live"
    _run_dir(agent, live, file=target, resumed_from="sess-A")
    for i in range(agent._LIVE_SCAN_LIMIT + 5):
        _run_dir(agent, "202602%02d-000000-dead" % (i + 1), file=target,
                 resumed_from="sess-A", alive=False)
    assert agent._live_run(target, "sess-A") == {"run_id": ""}, (
        "premise check: the capped default still stops at the window"
    )
    assert agent._live_run(target, "sess-A", limit=None) == {"run_id": live}


def test_the_lock_lookup_still_answers_no_when_every_run_is_dead(agent, target):
    """Unbounded must not mean credulous — the scan reads further, it does not
    relax the pid check. A lock that never releases is worse than one that never
    engages."""
    for i in range(agent._LIVE_SCAN_LIMIT + 5):
        _run_dir(agent, "202602%02d-000000-dead" % (i + 1), file=target,
                 resumed_from="sess-A", alive=False)
    assert agent._live_run(target, "sess-A", limit=None) == {"run_id": ""}


def test_the_lock_lookup_is_scoped_to_the_folder(agent, target, tmp_path):
    """A run live in ANOTHER folder must not lock this canvas — the clone dir is
    the identity, and a directory target is what canvases.py passes."""
    clone = tmp_path / "clone"
    clone.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    _run_dir(agent, "20260817-120000-aaa", file=str(other))
    assert agent._live_run(str(clone), limit=None) == {"run_id": ""}
    _run_dir(agent, "20260817-130000-bbb", file=str(clone))
    assert agent._live_run(str(clone), limit=None) == {"run_id": "20260817-130000-bbb"}


def test_without_a_session_it_answers_for_the_target(agent, target):
    """A boot that has a `run`-less URL and no session id yet still deserves an
    answer — the target is enough to identify the chat there."""
    _run_dir(agent, "20260817-120000-eee", file=target, resumed_from="sess-A")
    assert agent._live_run(target, "") == {"run_id": "20260817-120000-eee"}


def test_no_runs_at_all_is_not_an_error(agent, target, monkeypatch):
    assert agent._live_run(target, "sess-A") == {"run_id": ""}
    monkeypatch.setattr(agent, "RUNS", os.path.join(agent.RUNS, "gone"))
    assert agent._live_run(target, "sess-A") == {"run_id": ""}


def test_the_action_is_dispatched(agent, target):
    _run_dir(agent, "20260817-120000-fff", file=target, resumed_from="sess-A")
    assert agent.main(action="live_run", file=target, session_id="sess-A") == {
        "run_id": "20260817-120000-fff"}
    assert "error" in agent.main(action="live_run", file="", session_id="sess-A")


def test_the_first_poll_records_the_session_the_cli_minted(agent):
    """Written next to the sidecar update, under the same one-shot marker, so
    the two ids a chat can be known by are both on disk."""
    block = inspect.getsource(agent._poll)
    block = block[block.index('marker = os.path.join(run_dir, "recorded")'):]
    block = block[:block.index("# The streamed deltas")]
    assert '_private_open(os.path.join(run_dir, "session"))' in block
    assert "fh.write(new_session)" in block


# adoptLiveRun touches no DOM — it is a lookup, a param write and a handoff — so
# it runs under node against stubs that record the order of all three. Since
# the one-shot became a WATCH (Akshil, 2026-08-19: a reopened mid-turn chat has
# to show its streaming row even when the gate is briefly held or the run is
# still spawning), the stubs also carry the watch's world: `logGen` (leaving
# bumps it), a `sleep` collapsed to a real 0ms timer so a test can change the
# world "one lap later", and a param STORE — the watch reads `run` back to tell
# a completed resumeRun from one that bailed, and the resumeRun stub clears it
# the way the real one's done-branch does.
_ADOPT_STUBS = """
const calls = [];
let sending = false, activeRun = null, logGen = 0,
    answer = { run_id: "run-1" }, boom = false;
const AGENT = "agent.py", FILE = "/proj/index.html";
const sleep = () => new Promise((r) => setTimeout(r, 0));
const store = {};
const fused = {
  runPython: async (agent, args) => {
    calls.push(["ask", args.action, args.file, args.session_id]);
    if (boom) throw new Error("python is down");
    return answer;
  },
  params: {
    set: (k, v, o) => { store[k] = v; calls.push(["param", k, v, (o || {}).history]); },
    get: (k) => store[k] || "",
  },
};
const resumeRun = async (id) => { calls.push(["resume", id]); store.run = ""; };
// The transcript-follower (D415) is the lap's SECOND, coarser reader. These
// tests are about the shape of the lap, so it records here; the real one runs
// under its own harness further down.
const followTranscript = async (sid) => { calls.push(["follow", sid]); };
"""


def _adopt(html, call):
    """Run adoptLiveRun() out of the page under node."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own re-attach glue")
    start = html.index("async function adoptLiveRun(")
    body = html[start:html.index("\n}\n", start) + 3]
    script = _ADOPT_STUBS + "\n" + body + "\n(async () => {\n" + call + "\n})();"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_adopting_asks_then_records_then_resumes_in_that_order(html_pane):
    """The order is the contract: `resumeRun` reads the `run` param nowhere, but
    every OTHER reader of the URL (a reload, the Chats button, the queue) does,
    so the id has to be on the URL before the poll loop can end and clear it."""
    calls = _adopt(html_pane, """
await adoptLiveRun("sess-A");
console.log(JSON.stringify(calls));
""")
    assert calls == [
        ["ask", "live_run", "/proj/index.html", "sess-A"],
        ["param", "run", "run-1", "replace"],
        ["resume", "run-1"],
    ]


def test_a_frame_that_owns_a_turn_never_adopts_another(html_pane):
    """Two attachments to one run would double every streamed chunk, and the
    guard is cheaper than the lookup it skips. `activeRun` ends the watch for
    good; a `sending` that never releases spends the whole budget looking at
    the gate and still asks the server nothing."""
    for setup in ("sending = true;", "activeRun = 'run-9';"):
        calls = _adopt(html_pane, setup + """
await adoptLiveRun("sess-A");
console.log(JSON.stringify(calls));
""")
        assert calls == [], setup


def test_nothing_running_leaves_the_transcript_alone(html_pane):
    """An empty answer is the common case — most chats are opened cold — so it
    must not write a param or start a loop. It IS asked again for a few laps
    (the same "" also comes back while a just-left run is still spawning — see
    the watch's comment), so the pin is on the shape: only asks, a bounded
    number of them, and then quiet."""
    calls = _adopt(html_pane, """
answer = { run_id: "" };
await adoptLiveRun("sess-A");
console.log(JSON.stringify(calls));
""")
    assert calls, "an idle chat is still asked at least once"
    assert set(map(tuple, calls)) == {
        ("ask", "live_run", "/proj/index.html", "sess-A")}
    assert len(calls) <= 8, "the watch is a budget, not a poll loop"


def test_a_briefly_held_gate_is_looked_past_not_obeyed(html_pane):
    """The reopen race itself (Akshil, 2026-08-19): `sending` is held at the one
    instant the adoption fires — a second click's loadHistory mid-flight, an
    abandoned turn's finally a tick from releasing — and the one-shot read
    "held right now" as "nothing to do, ever". The watch looks again: the gate
    clears a lap later and the run is adopted."""
    calls = _adopt(html_pane, """
sending = true;
setTimeout(() => { sending = false; }, 0);
await adoptLiveRun("sess-A");
console.log(JSON.stringify(calls));
""")
    assert calls[-2:] == [["param", "run", "run-1", "replace"],
                          ["resume", "run-1"]]


def test_a_run_still_spawning_is_caught_by_a_later_look(html_pane):
    """Back landed during `start`: the server honestly answers "" until the run
    dir has its pid, moments after the reopen asks. The watch's later laps are
    what turn that from a chat that never streams again into a working row one
    tick late."""
    calls = _adopt(html_pane, """
answer = { run_id: "" };
setTimeout(() => { answer = { run_id: "run-1" }; }, 0);
await adoptLiveRun("sess-A");
console.log(JSON.stringify(calls));
""")
    assert calls[-2:] == [["param", "run", "run-1", "replace"],
                          ["resume", "run-1"]]


def test_leaving_ends_the_watch(html_pane):
    """The watch is gen-guarded like every loop that outlives an await: Back
    bumps logGen, and a watch still looking for a run must die with the
    transcript it was watching — its lookups are not the landing page's to
    spend, and its adoption would write a run param the leave just cleared."""
    calls = _adopt(html_pane, """
answer = { run_id: "" };
setTimeout(() => { logGen += 1; answer = { run_id: "run-1" }; }, 0);
await adoptLiveRun("sess-A");
console.log(JSON.stringify(calls));
""")
    assert calls == [["ask", "live_run", "/proj/index.html", "sess-A"]]


def test_a_failed_lookup_is_not_an_error_the_reader_sees(html_pane):
    """The transcript already rendered. A lookup that throws should leave the
    chat exactly as it is, not tear it down."""
    calls = _adopt(html_pane, """
boom = true;
await adoptLiveRun("sess-A");
console.log(JSON.stringify(calls));
""")
    assert calls == [["ask", "live_run", "/proj/index.html", "sess-A"]]


def test_a_missing_session_id_crosses_as_an_empty_string(html_pane):
    """The boot path passes whatever `session_id` the URL had, which can be null,
    and params reach python string-shaped — a literal "null" would match no run
    and quietly answer nothing."""
    calls = _adopt(html_pane, """
await adoptLiveRun(null);
console.log(JSON.stringify(calls));
""")
    assert calls[0] == ["ask", "live_run", "/proj/index.html", ""]


def test_opening_a_chat_from_the_list_asks_whether_it_is_still_running(html_pane):
    """The click path is not a navigation, so nothing re-boots and no `run`
    param arrives — the row has to ask on its own. Read off comment-stripped
    source: the prose here NAMES adoptLiveRun, and a pin a comment can satisfy
    is a pin that survives the call being deleted."""
    body = html_pane[html_pane.index("function addChatRow("):]
    body = body[:body.index("\n}")]
    open_fn = body[body.index("const open ="):]
    assert "loadHistory(s.id)" in open_fn
    assert "adoptLiveRun(s.id)" in open_fn
    assert open_fn.index("loadHistory(s.id)") < open_fn.index("adoptLiveRun(s.id)"), (
        "the transcript renders first; the live turn is then appended to it"
    )


def test_a_boot_without_a_run_param_still_asks(html_pane):
    """A reload after the param was dropped, a bookmark of the bare chat, a mode
    switch: all land on boot with a live turn still streaming server-side. `else`,
    not a second call: a boot that HAS the id must not ask as well."""
    boot = html_pane[html_pane.index("const run_id = fused.params.get(\"run\")"):]
    assert "else if (session_id) await adoptLiveRun(session_id);" in boot


def test_the_lookup_is_the_only_new_agent_action_the_page_calls(html_pane):
    """One reader of `live_run`, so there is one place to change if the answer's
    shape ever grows. The behaviour of the call itself — order, guards, the empty
    answer, a lookup that throws — is covered above under node; this only pins
    that nothing else grew a copy of it.

    The `replace` posture of its `run` write is NOT re-asserted here:
    test_claude_kind.test_every_run_write_is_a_replace_write already holds every
    such write in the file to that rule, and two guards for one invariant is one
    that gets updated and one that goes stale."""
    assert html_pane.count('action: "live_run"') == 1


# ── the standing watch: a turn nobody on this page started (D415) ────────────
#
# The window version of the watch only ran around opening a chat, so a session
# that became busy while the chat sat open lit nothing at all — the reported
# case being the harness waking the run when a background shell finished. The
# watch is now also a timer, and `liveWatchTick` is the lap it runs: the same
# lookup, once, and only when this frame is showing a session and holding
# nothing.


def _watch(html, call):
    """Run liveWatchTick() out of the page under node, over adoptLiveRun."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own re-attach glue")
    start = html.index("async function adoptLiveRun(")
    adopt = html[start:html.index("\n}\n", start) + 3]
    start = html.index("async function liveWatchTick() {")
    tick = html[start:html.index("\n}\n", start) + 3]
    script = (_ADOPT_STUBS + "\nlet liveWatchBusy = false;\n"
              + adopt + "\n" + tick + "\n(async () => {\n" + call + "\n})();")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_watch_asks_once_a_lap_not_eight_times(html_pane):
    """A tick that finds nothing must cost ONE lookup: this runs for the life of
    the page, where the reopen's budget is chasing a run it knows exists.

    Finding nothing is also the ONLY case that reaches the transcript (D415):
    with no run to attach, a stat is the page's one remaining way to notice a
    turn someone is driving from a terminal."""
    calls = _watch(html_pane, """
answer = { run_id: "" };
store.session_id = "sess-A";
await liveWatchTick();
console.log(JSON.stringify(calls));
""")
    assert calls == [["ask", "live_run", "/proj/index.html", "sess-A"],
                     ["follow", "sess-A"]]


def test_the_watch_adopts_a_run_this_frame_never_started(html_pane):
    calls = _watch(html_pane, """
store.session_id = "sess-A";
await liveWatchTick();
console.log(JSON.stringify(calls));
""")
    assert ["resume", "run-1"] in calls


def test_the_run_dir_is_asked_first_and_the_transcript_only_after(html_pane):
    """RUN DIRS FIRST, ALWAYS (D415). A run this app spawned streams token by
    token and owns the chrome; the transcript is the coarser, blinder fallback
    and only speaks for the turns no run dir can account for. So the stat is
    strictly after the lookup, and a turn already ATTACHED skips it entirely —
    that last part is `test_the_watch_is_quiet_while_this_frame_is_busy`, which
    asserts a lap holding `activeRun` costs nothing at all."""
    calls = _watch(html_pane, """
store.session_id = "sess-A";
await liveWatchTick();
console.log(JSON.stringify(calls));
""")
    assert calls.index(["ask", "live_run", "/proj/index.html", "sess-A"]) \
        < calls.index(["follow", "sess-A"])


def test_the_watch_is_quiet_while_this_frame_is_busy(html_pane):
    """Every reason not to look: no session on screen (there is no conversation
    to adopt a turn into), a turn already attached, the send gate held, and a
    lap of its own still in flight."""
    for setup in ('store.session_id = "";',
                  'store.session_id = "sess-A"; activeRun = "run-9";',
                  'store.session_id = "sess-A"; sending = true;',
                  'store.session_id = "sess-A"; liveWatchBusy = true;'):
        calls = _watch(html_pane, setup + """
await liveWatchTick();
console.log(JSON.stringify(calls));
""")
        assert calls == [], setup


# --- what WAKES the watch ----------------------------------------------------
#
# The interval alone was half a fix (Akshil, 2026-08-21, two tabs on one chat):
# the second tab showed the reply ten seconds late and never showed the running
# chrome, because the lap that finally looked found a run that had already
# finished. Adoption was never the defect — the LATENCY was. So the triggers are
# what these pin: the cross-tab stamp (milliseconds, and free), coming back to a
# backgrounded tab, and a halved interval that a hidden tab does not spend.


def _triggers(html, call):
    """Run the watch's REGISTRATION block under node, over a recording tick."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own re-attach glue")
    start = html.index("const LIVE_WATCH_MS = ")
    end = html.index('window.addEventListener("focus"', start)
    block = html[start:html.index("\n", end) + 1]
    stubs = """
const calls = [];
const CHAT_ACTIVITY_KEY = "fused-render:chat-activity";
let activeRun = null, sending = false;
const handlers = {};
const document = { hidden: false, addEventListener: (t, f) => { handlers["doc:" + t] = f; } };
const window = { addEventListener: (t, f) => { handlers["win:" + t] = f; } };
let ticking = null;
const setInterval = (fn, ms) => { ticking = fn; calls.push(["interval", ms]); };
const adoptLiveRun = async (sid, opts) => { calls.push(["adopt", sid, opts.laps, opts.quiet]); };
const fused = { params: { get: () => "sess-A" } };
"""
    script = stubs + block + "\n(async () => {\n" + call + "\n})();"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_tab_that_started_the_turn_wakes_the_others_at_once(html_pane):
    """`noteChatActivity` already stamps localStorage as a turn OPENS, and a
    storage event fires in every other document and never in the writer. So the
    two-tab case costs no polling and lands while the turn is still streaming."""
    calls = _triggers(html_pane, """
await handlers["win:storage"]({ key: CHAT_ACTIVITY_KEY });
console.log(JSON.stringify(calls));
""")
    assert ["adopt", "sess-A", 1, True] in calls


def test_another_store_key_is_not_news_about_a_turn(html_pane):
    calls = _triggers(html_pane, """
await handlers["win:storage"]({ key: "fused-render:queued-msgs" });
console.log(JSON.stringify(calls));
""")
    assert not [c for c in calls if c[0] == "adopt"]


def test_coming_back_to_a_backgrounded_tab_looks_immediately(html_pane):
    """Both halves: returning laps at once, and leaving does not.

    `drain` is the harness paying a debt the browser never has: the triggers do
    not await the lap they start, so the `liveWatchBusy` seat is only free again
    once the lap's own awaits have settled. Real events are milliseconds apart;
    these are back-to-back statements, and without a turn of the event loop
    between them the second one is refused as a burst."""
    calls = _triggers(html_pane, """
const drain = () => new Promise((r) => setTimeout(r, 0));
document.hidden = true;
await handlers["doc:visibilitychange"](); await drain();
document.hidden = false;
await handlers["doc:visibilitychange"](); await drain();
await handlers["win:focus"](); await drain();
console.log(JSON.stringify(calls));
""")
    assert len([c for c in calls if c[0] == "adopt"]) == 2


def test_a_hidden_tab_does_not_spend_the_interval(html_pane):
    """Chrome nobody is looking at is worth nothing, and the storage poke reaches
    a hidden tab anyway; what it gives up is catching a SERVER-started run before
    the reader comes back."""
    calls = _triggers(html_pane, """
document.hidden = true;
await ticking();
document.hidden = false;
await ticking();
console.log(JSON.stringify(calls));
""")
    assert [c for c in calls if c[0] == "interval"] == [["interval", 5000]], (
        "the backstop interval is the one path a server-started run has"
    )
    assert len([c for c in calls if c[0] == "adopt"]) == 1


def test_the_watch_attaches_in_quiet_mode(html_pane):
    html = html_pane
    start = html.index("async function liveWatchTick() {")
    tick = html[start:html.index("\n}\n", start) + 3]
    assert "quiet: true" in tick


# --- `quiet` is dedupe, not silence ------------------------------------------
#
# The first version of `quiet` suppressed the run's user line unconditionally,
# and the second tab then showed an ANSWER HANGING UNDER NO QUESTION: the words
# were typed in the other tab, so this transcript had never shown them. `quiet`
# now means "do not print a message this transcript is already showing", which
# splits the watch's two cases the way they actually differ.


def test_quiet_prints_a_message_this_transcript_has_never_shown(html_pane):
    """Both render sites, because a turn adopted mid-flight and one adopted
    after it finished are the same question about the same message."""
    live = _block_between(html_pane, "    } else if (probeMsg && !(quiet",
                          "addUser(probeMsg);")
    assert "onScreen(probeMsg)" in live
    done = _block_between(html_pane, "      } else if ((!users.length",
                          "addUser(probeMsg);")
    assert "quiet && !onScreen(probeMsg)" in done, (
        "a short turn made in another tab is over before the watch looks; "
        "dropping it silently is what the second tab complained about"
    )


def test_already_shown_is_asked_of_the_whole_transcript(html_pane):
    """`matches` asks only the LAST bubble because it also decides whether to
    STRIP rows. This one only decides whether to PRINT, so it may look wider —
    which is what stops a woken run re-printing a message further up the log."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own re-attach glue")
    start = html_pane.index("  const onScreen = (msg) =>")
    pred = html_pane[start:html_pane.index(";\n", start) + 1]
    script = """
const bubbles = [{textContent: "old question"}, {textContent: "newer one"}];
const log = { querySelectorAll: () => bubbles };
""" + pred + """
console.log(JSON.stringify({
  older: onScreen("old question"),
  last: onScreen("newer one"),
  fresh: onScreen("typed in the other tab"),
  empty: onScreen(""),
}));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"older": True, "last": True,
                                      "fresh": False, "empty": False}


def _block_between(html, start_marker, end_marker):
    i = html.index(start_marker)
    return html[i:html.index(end_marker, i) + len(end_marker)]



# ── following a session this app is not driving (D415) ───────────────────────
#
# The gap the run-dir watch above cannot close, by construction: an interactive
# `claude` in a terminal, or a `claude --resume`, on the very session the chat is
# open on. There is no run dir, so `live_run` answers nothing forever, and the
# chat showed the conversation as it stood at page load until the reader hit
# reload by hand — at which point the missing turns appeared correctly, because
# the RELOAD was never the broken part.
#
# So the reload is what got automated, and deliberately nothing more: the server
# stats the one transcript (`/api/claude-sessions/liveness`), and a `(mtime,
# size)` that moved past the watermark the last render came with re-runs the
# same `loadHistory` a manual reload ran. These pin the rule that decides that —
# extracted from the page, so a decision the page does not actually make cannot
# pass.

def _decide(html, call):
    """Run followDecision() — the pure half of the follower — under node."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own follow rule")
    fn = _block_between(html, "function followDecision(mark, probe, ownEnd) {",
                        "\n}\n")
    out = subprocess.run(["node", "-e", fn + "\n" + call],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_MARK = '{ path: "/p/s.jsonl", mtime: 100, size: 500 }'


def test_a_transcript_that_has_not_moved_costs_nothing(html_pane):
    """The steady state, and the whole reason this is affordable: a chat sitting
    open on a finished conversation stats once a lap and does nothing with the
    answer. No parse, no fetch, no re-render."""
    got = _decide(html_pane, """
console.log(JSON.stringify(followDecision(%s,
  { exists: true, mtime: 100, size: 500, running: false }, 0)));
""" % _MARK)
    assert got == {"refresh": False, "running": False}


def test_either_half_of_the_pair_moving_is_a_move(html_pane):
    """The PAIR, not mtime alone: a coarse filesystem clock can land two appends
    in one tick, and the size is what tells them apart. And mtime alone would
    miss nothing only on a clock nobody promised us."""
    for probe in ('{ exists: true, mtime: 101, size: 500 }',
                  '{ exists: true, mtime: 100, size: 700 }'):
        got = _decide(html_pane, """
console.log(JSON.stringify(followDecision(%s, %s, 0)));
""" % (_MARK, probe))
        assert got["refresh"] is True, probe


def test_a_transcript_that_is_not_there_yet_is_not_a_move(html_pane):
    """`exists: false` is a session whose first row has not been written, not a
    conversation that emptied — re-rendering on it would blank a chat over a
    file the writer has not created."""
    got = _decide(html_pane, """
console.log(JSON.stringify(followDecision(%s,
  { exists: false, mtime: 0, size: 0, running: true }, 0)));
""" % _MARK)
    assert got == {"refresh": False, "running": False}


def test_no_watermark_yet_means_no_opinion(html_pane):
    """Nothing has rendered, so there is nothing to compare against and no
    honest verdict to give. The first `loadHistory` is what arms this."""
    for mark in ("null", '{ path: "" }'):
        got = _decide(html_pane, """
console.log(JSON.stringify(followDecision(%s,
  { exists: true, mtime: 900, size: 900, running: true }, 0)));
""" % mark)
        assert got == {"refresh": False, "running": False}, mark


def test_this_pages_own_finished_turn_is_not_reported_back_as_somebody_elses(
        html_pane):
    """The transcript is at its freshest right after a run THIS frame streamed,
    and `session_liveness` keeps calling the session running for a few seconds
    after the last row lands (housekeeping records arrive late — the whole
    reason that module exists). Without the `ownRunEndedAt` guard every turn
    sent from the app would end with the app announcing it as a turn running
    somewhere else."""
    got = _decide(html_pane, """
console.log(JSON.stringify(followDecision(%s,
  { exists: true, mtime: 140, size: 900, running: true }, 150)));
""" % _MARK)
    # It still re-renders — the rows are real and the watermark is stale — but
    # it does not claim anyone is mid-turn.
    assert got == {"refresh": True, "running": False}


def test_a_turn_that_started_after_ours_ended_does_light_the_line(html_pane):
    got = _decide(html_pane, """
console.log(JSON.stringify(followDecision(%s,
  { exists: true, mtime: 160, size: 900, running: true }, 150)));
""" % _MARK)
    assert got == {"refresh": True, "running": True}


# --- and what the follower does with that verdict ----------------------------

def _follow(html, call):
    """Run followTranscript() under node, over recording stubs."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own follow rule")
    decide = _block_between(html, "function followDecision(mark, probe, ownEnd) {",
                            "\n}\n")
    follow = _block_between(html, "async function followTranscript(session_id) {",
                            "\n}\n")
    stubs = """
const calls = [];
let logGen = 0, activeRun = null, sending = false, ownRunEndedAt = 0;
let transcriptMark = { path: "/p/s.jsonl", mtime: 100, size: 500 };
let probe = { exists: true, mtime: 200, size: 900, running: true };
let ok = true;
// `let`, because two of these tests wrap it to change the world mid-await —
// which is the only way to test a guard that exists precisely for that.
let fetch = async (url) => {
  calls.push(["stat", url]);
  return { ok, json: async () => probe };
};
const loadHistory = async (sid, opts) => {
  calls.push(["history", sid, !!(opts || {}).refresh]);
};
const setExternalWorking = (on) => { calls.push(["working", !!on]); };
"""
    script = (stubs + decide + "\n" + follow + "\n(async () => {\n" + call
              + "\n})();")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_moved_transcript_re_renders_through_the_reload_path(html_pane):
    """`loadHistory`, in REFRESH mode — the same renderer a manual reload used,
    which is what made the reload correct (the background-task chip included)
    and is exactly why this reuses it rather than appending rows of its own.
    D415 rejected the page tailing ~/.claude/projects and guessing turn
    boundaries; this is the alternative it argued for."""
    calls = _follow(html_pane, """
await followTranscript("sess-A");
console.log(JSON.stringify(calls));
""")
    assert calls == [["stat", "/api/claude-sessions/liveness?path=%2Fp%2Fs.jsonl"],
                     ["history", "sess-A", True],
                     ["working", True]]


def test_the_refresh_lands_before_the_line_it_announces(html_pane):
    """Order, not taste: `loadHistory` replaces the log wholesale, so a working
    line added first would be swept away by the very render that justifies it —
    and the reader would watch the chrome flicker off exactly when a turn is
    running."""
    calls = _follow(html_pane, """
await followTranscript("sess-A");
console.log(JSON.stringify(calls));
""")
    assert calls.index(["history", "sess-A", True]) \
        < calls.index(["working", True])


def test_a_failed_stat_leaves_the_conversation_exactly_as_it_rendered(html_pane):
    """A watch that runs forever will meet a server restart, and an unreadable
    answer is not evidence of anything. Neither a non-ok response nor a body
    that is not a stat may re-render the log — the turns already on screen are
    still the best thing known — and neither may claim a turn is running."""
    for setup in ("ok = false;", "probe = null; ok = true;",
                  "probe = { exists: false }; ok = true;"):
        calls = _follow(html_pane, setup + """
await followTranscript("sess-A");
console.log(JSON.stringify(calls.filter((c) => c[0] !== "stat")));
""")
        assert not [c for c in calls if c[0] == "history"], setup
        assert ["working", True] not in calls, setup


def test_nothing_is_asked_before_the_first_render(html_pane):
    """No watermark, no question: the page has nothing to compare an answer
    against, so the stat would be spent on a verdict it could not reach."""
    calls = _follow(html_pane, """
transcriptMark = null;
await followTranscript("sess-A");
console.log(JSON.stringify(calls));
""")
    assert calls == []


def test_a_real_run_taken_mid_stat_outranks_the_answer(html_pane):
    """Everything can change across the await, and each of these outranks a
    stale stat: the reader left the chat (`logGen`), a real run attached and
    owns the chrome, or a send took the gate. Acting anyway means a re-render
    dropped on top of a conversation that is no longer the one asked about."""
    for setup in ("logGen = 1;", 'activeRun = "run-9";', "sending = true;"):
        calls = _follow(html_pane, """
const real = fetch;
fetch = async (u) => { %s return real(u); };
await followTranscript("sess-A");
console.log(JSON.stringify(calls.filter((c) => c[0] !== "stat")));
""" % setup)
        assert calls == [], setup


def test_a_render_that_landed_while_we_asked_wins(html_pane):
    """The watermark is the identity of the visible render. If a `loadHistory`
    completed during the await it wrote a NEWER one, and this probe describes a
    file older than what is on screen — refreshing on it would be a re-render
    justified by evidence that has already expired."""
    calls = _follow(html_pane, """
const real = fetch;
fetch = async (u) => {
  transcriptMark = { path: "/p/s.jsonl", mtime: 200, size: 900 };
  return real(u);
};
await followTranscript("sess-A");
console.log(JSON.stringify(calls.filter((c) => c[0] !== "stat")));
""")
    assert calls == []


def test_a_refresh_does_not_move_the_reader(html_pane):
    """A refresh is not a navigation. This lap can fire every five seconds under
    a reader who is scrolled back reading an earlier turn, so it restores the
    scroll it found — except for a reader who is following the tail, who gets
    carried to the turn that just landed (the same `followTail` flag the
    streaming output obeys, rather than a fresh geometry read: a composer that
    grew under the reader must not be mistaken for the reader moving).
    `?msg=` is a link followed once, so the anchor is not re-honoured on every
    lap either."""
    body = _block_between(html_pane,
                          "async function loadHistory(session_id, opts) {",
                          "\n}\n")
    assert "const keepTop = refresh && !followTail ? logwrap.scrollTop : null;" in body
    assert "if (keepTop !== null) logwrap.scrollTop = keepTop;" in body
    # ...and a failed refresh keeps the turns it has: blanking a conversation
    # because one stat round trip lost is strictly worse than showing it stale.
    assert "if (!refresh) log.innerHTML = \"\";" in body

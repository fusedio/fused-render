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
    """`--fork-session` hands back a NEW session id and `_record_session`
    repoints the sidecar row at it — so the id the page holds may be the one in
    the run's `session` file OR the `resumed_from` in meta.json. Both identify
    the same chat, so either matching is a match — and an id that is neither
    still does not."""
    _run_dir(agent, "20260817-120000-ddd", file=target, resumed_from="sess-old",
             session="sess-new")
    assert agent._live_run(target, "sess-new") == {"run_id": "20260817-120000-ddd"}
    assert agent._live_run(target, "sess-old") == {"run_id": "20260817-120000-ddd"}
    assert agent._live_run(target, "sess-other") == {"run_id": ""}


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
# it runs under node against stubs that record the order of all three.
_ADOPT_STUBS = """
const calls = [];
let sending = false, activeRun = null, answer = { run_id: "run-1" }, boom = false;
const AGENT = "agent.py", FILE = "/proj/index.html";
const fused = {
  runPython: async (agent, args) => {
    calls.push(["ask", args.action, args.file, args.session_id]);
    if (boom) throw new Error("python is down");
    return answer;
  },
  params: { set: (k, v, o) => calls.push(["param", k, v, (o || {}).history]) },
};
const resumeRun = async (id) => { calls.push(["resume", id]); };
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
    guard is cheaper than the lookup it skips."""
    for setup in ("sending = true;", "activeRun = 'run-9';"):
        calls = _adopt(html_pane, setup + """
await adoptLiveRun("sess-A");
console.log(JSON.stringify(calls));
""")
        assert calls == [], setup


def test_nothing_running_leaves_the_transcript_alone(html_pane):
    """An empty answer is the common case — most chats are opened cold — so it
    must not write a param or start a loop."""
    calls = _adopt(html_pane, """
answer = { run_id: "" };
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

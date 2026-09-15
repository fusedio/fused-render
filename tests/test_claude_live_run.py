"""Re-attaching to a run the current frame did not start (the Claude chat).

The run id used to live in exactly one place — the `run` param on one history
entry — so a chat reopened any other way lost it. The reproduction: send a
message in chat A, press Back (which lands on whatever entry is behind it), then
reach chat A again from the session list. The detached claude process is still
streaming into its run dir, but the page has no id to attach to, renders the
mid-flight transcript (which ends at the user's own message, correctly — the
reply is not written yet) and shows no working line at all. Arriving with Back
worked only because that entry still carried `run`.

`_live_run` is the missing lookup: the server knows which runs are still alive.
These tests cover the answer IT gives; the client paths that ask (`adoptLiveRun`
and the watch) live in `apps/claude` and are tested there.
"""
import importlib.util
import inspect
import json
import os

import pytest


def _load_agent():
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


# ---------------- the folder/entry-file spelling (feedback R2-11/R2-13)

def test_an_app_folders_run_is_adopted_by_a_caller_holding_its_ENTRY_FILE(
        agent, target, tmp_path):
    """A chat opened on an APP FOLDER records the folder as its target; the
    Tasks cards wall mounts a tile on `task.target || task.project`, which for
    that same chat resolves to the folder's ENTRY FILE. Compared exactly, every
    lookup answered "" — so no tile ever adopted its live run, and a task
    parked on an AskUserQuestion showed its transcript and never its card, in
    the wall and in Peek both."""
    folder = str(tmp_path / "proj")
    _run_dir(agent, "20260908-120000-aaa", file=folder, resumed_from="sess-A")
    assert agent._live_run(target, "sess-A") == {"run_id": "20260908-120000-aaa"}


def test_and_the_other_way_round_too(agent, target, tmp_path):
    """Either surface can be the one holding the folder — a chat opened on the
    file, asked about by something that knows only the project."""
    _run_dir(agent, "20260908-120000-bbb", file=target, resumed_from="sess-A")
    assert agent._live_run(str(tmp_path / "proj"), "sess-A") == {
        "run_id": "20260908-120000-bbb"}


def test_the_widening_stops_at_the_folder_it_does_not_reach_siblings(
        agent, target, tmp_path):
    """`test_another_chat_s_run_is_not_adopted`, restated as the RULE: two
    files in one folder are two chats, so "same parent" is the wrong
    relaxation. The right one is "one of them IS the folder the other lives
    in", which is the only pair the wall produces."""
    sibling = str(tmp_path / "proj" / "other.html")
    assert agent._folder_and_member(str(tmp_path / "proj"), target) is True
    assert agent._folder_and_member(target, str(tmp_path / "proj")) is True
    assert agent._folder_and_member(target, sibling) is False
    assert agent._folder_and_member(target, target) is False, (
        "an equal pair is the exact match's job, not this one's")


def test_the_widening_needs_a_session_id(agent, target, tmp_path):
    """Without one there is nothing to tell two conversations on the same
    folder apart, so the exact target is all the caller has."""
    folder = str(tmp_path / "proj")
    _run_dir(agent, "20260908-120000-ccc", file=folder)
    assert agent._live_run(target) == {"run_id": ""}
    assert agent._live_run(folder) == {"run_id": "20260908-120000-ccc"}


# ---------------------------------------------------------------------------
# `_history_live`: the live run and its cards ride on the history answer, so a
# restored conversation paints transcript and question card in ONE frame
# instead of learning about the run three round trips later (Akshil,
# 2026-09-11 — the Tasks cards wall showed the chat in ~3 s and the card in ~5).
# ---------------------------------------------------------------------------

def _park_question(run_dir, request_id="q1"):
    perm = os.path.join(run_dir, "perm")
    os.makedirs(perm, exist_ok=True)
    with open(os.path.join(perm, request_id + ".req.json"), "w",
              encoding="utf-8") as f:
        json.dump({"id": request_id, "tool": "AskUserQuestion",
                   "tool_use_id": "toolu_1",
                   "input": {"questions": [{"question": "Which?",
                                            "options": [{"label": "A"}]}]},
                   "created_at": 1}, f)


@pytest.fixture()
def projects(agent, tmp_path, monkeypatch):
    p = tmp_path / "projects"
    p.mkdir()
    monkeypatch.setattr(agent, "PROJECTS", str(p))
    return p


def test_history_carries_the_live_run_and_its_cards(agent, target, projects):
    d = _run_dir(agent, "20260911-180000-aaa", file=target, resumed_from="sess-A")
    _park_question(d)
    out = agent._history(target, "sess-A")
    assert out["live_run"] == "20260911-180000-aaa"
    assert [p["id"] for p in out["permissions"]] == ["q1"]
    assert out["permissions"][0]["tool"] == "AskUserQuestion"
    assert out["permissions"][0]["decision"] == ""
    # `_live_mode` off the run's meta — the picker is right on first paint.
    assert out["mode"] == "prompt"
    # A run can be live before its transcript exists: the card must not wait.
    assert out["turns"] == []


def test_history_says_nothing_is_live_as_an_answer(agent, target, projects):
    """`""` is the page's cue to drop the adoption gate right away, exactly as
    the first `live_run` lap always did."""
    _run_dir(agent, "20260911-180000-aaa", file=target, resumed_from="sess-A",
             alive=False)
    out = agent._history(target, "sess-A")
    assert out["live_run"] == ""
    assert out["permissions"] == []
    assert out["mode"] == ""


def test_history_live_block_matches_what_poll_returns(agent, target, projects):
    """Same rows through the same `_permissions`, so the first poll of the
    adopted run replays them and the page's dedupe-by-id holds."""
    d = _run_dir(agent, "20260911-180000-aaa", file=target, resumed_from="sess-A")
    _park_question(d, "q7")
    hist = agent._history(target, "sess-A")
    polled = agent._poll("20260911-180000-aaa", target)
    assert hist["permissions"] == polled["permissions"]
    assert hist["mode"] == polled["mode"]

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
import json
import os

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
    repoints the sidecar row at it — so the id the page holds is the one in the
    run's `session` file, not the `resumed_from` in meta.json. Both identify the
    same chat, so either matching is a match."""
    _run_dir(agent, "20260817-120000-ddd", file=target, resumed_from="sess-old",
             session="sess-new")
    assert agent._live_run(target, "sess-new") == {"run_id": "20260817-120000-ddd"}


def test_the_newest_live_run_wins(agent, target):
    _run_dir(agent, "20260817-090000-old", file=target, resumed_from="sess-A")
    _run_dir(agent, "20260817-150000-new", file=target, resumed_from="sess-A")
    assert agent._live_run(target, "sess-A") == {"run_id": "20260817-150000-new"}


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


def test_the_first_poll_records_the_session_the_cli_minted(template):
    """Written next to the sidecar update, under the same one-shot marker, so
    the two ids a chat can be known by are both on disk."""
    src = open(os.path.join("fused_render", "templates", "claude", "agent.py"),
               encoding="utf-8").read()
    block = src[src.index('marker = os.path.join(run_dir, "recorded")'):]
    block = block[:block.index("# The streamed deltas")]
    assert '_private_open(os.path.join(run_dir, "session"))' in block
    assert "fh.write(new_session)" in block


def test_opening_a_chat_from_the_list_asks_whether_it_is_still_running(template):
    """The click path is not a navigation, so nothing re-boots and no `run`
    param arrives — the row has to ask on its own."""
    body = template[template.index("function addChatRow("):]
    body = body[:body.index("\n}")]
    open_fn = body[body.index("const open ="):]
    assert "loadHistory(s.id)" in open_fn
    assert "adoptLiveRun(s.id)" in open_fn
    assert open_fn.index("loadHistory(s.id)") < open_fn.index("adoptLiveRun(s.id)"), (
        "the transcript renders first; the live turn is then appended to it"
    )


def test_a_boot_without_a_run_param_still_asks(template):
    """A reload after the param was dropped, a bookmark of the bare chat, a mode
    switch: all land on boot with a live turn still streaming server-side."""
    boot = template[template.index("// ── boot: resume from URL"):]
    assert "else if (session_id) await adoptLiveRun(session_id);" in boot


def test_adopting_never_fights_a_turn_this_frame_owns(template):
    """Two attachments to one run would double every streamed chunk."""
    fn = template[template.index("async function adoptLiveRun("):]
    fn = fn[:fn.index("\n}")]
    assert "if (sending || activeRun) return;" in fn
    assert 'history: "replace"' in fn, "a rediscovered id is bookkeeping, not a step"
    assert "resumeRun(id)" in fn

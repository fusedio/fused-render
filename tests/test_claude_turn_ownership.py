"""One owner for the open exchange (claude template).

A page reload used to draw the conversation from two sources that overlap
and nothing reconciled: `_history` rebuilds the whole conversation from the
CLI's own persisted transcript, INCLUDING the exchange still in flight, and
`_poll` re-streams that same open exchange from a byte cursor into a
different file (`run_dir/out.jsonl`). Two consequences: a follow-up sent
mid-turn was invisible to every read path until the CLI echoed it back, and
already-shown text could print twice because `resumeRun`'s strip guessed the
wrong boundary once one turn owned several user bubbles.

The fix gives each region exactly one owner. The saved transcript owns
CLOSED exchanges; `_history` marks where the still-OPEN one begins
(`open_from`) rather than deciding what to draw. The live poll owns the open
exchange, all of it, including anything of the user's still queued —
`_poll`'s new `pending` field is that queue, read straight off
`run_dir/inbox/` since `out.jsonl` says nothing about a message until the
CLI echoes it.

These tests cover the Python half only; the frontend half (loadHistory /
resumeRun rendering exactly one owner's turns) lives in
test_claude_turn_ownership_frontend.py.
"""
import importlib.util
import json
import os

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


# --------------------------------------------------------------- open_from

def _t_user(text):
    # Real shape: the persisted transcript's `user` rows carry the message
    # text as a bare string, not the `_write_inbox_entry` block-list shape
    # `out.jsonl` echoes carry — measured across real sessions (see
    # SPEC-turn-ownership-round2.md), and there is no `result` row and no
    # `parent_tool_use_id` anywhere in that file, ever.
    return {"type": "user", "message": {"role": "user", "content": text}}


def _t_assistant(text):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


def _t_subagent_user(text):
    """A subagent's own prompt, on the persisted transcript — tagged
    `isSidechain`, never `parent_tool_use_id` (that shape belongs to
    `out.jsonl`, the CLI's live stream-json, not this file)."""
    return {"type": "user", "isSidechain": True,
            "message": {"role": "user", "content": text}}


def _write_transcript(agent, tmp_path, monkeypatch, rows):
    target = tmp_path / "proj" / "page.html"
    os.makedirs(target.parent, exist_ok=True)
    target.write_text("<html></html>")
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    d = projects / agent._munge(str(target.parent))
    os.makedirs(d, exist_ok=True)
    with open(d / "sess1.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    # No live run for this file unless a test sets one up (`_setup_live_run`
    # below) — the common case, and the property `open_from` must reduce to
    # `None` for exactly like an ordinary finished/abandoned session.
    monkeypatch.setattr(agent, "_live_run", lambda file, session_id="", limit=None: {"run_id": ""})
    return str(target)


def _out_start_row(text):
    """The shape `_write_inbox_entry` puts on the wire and the CLI echoes
    back into `out.jsonl` — a `_starts_new_turn` row, unlike the persisted
    transcript's bare-string `user` rows."""
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def _out_subagent_result():
    return {"type": "result", "parent_tool_use_id": "tu1", "result": "subagent done"}


def _setup_live_run(agent, tmp_path, monkeypatch, target, session_id, out_rows):
    """A run still going for `target`, with `out.jsonl` carrying `out_rows` —
    the live signal `_history` now asks instead of reading `open_from` off
    the persisted transcript's own (nonexistent) `result` rows."""
    runs = tmp_path / "runs"
    run_dir = runs / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target)}), encoding="utf-8")
    (run_dir / "session").write_text(session_id, encoding="utf-8")
    with open(run_dir / "out.jsonl", "w", encoding="utf-8") as fh:
        for row in out_rows:
            fh.write(json.dumps(row) + "\n")
    monkeypatch.setattr(agent, "RUNS", str(runs))
    monkeypatch.setattr(agent, "_live_run",
                        lambda file, session_id="", limit=None: {"run_id": "run1"})


def test_open_from_is_none_on_a_fully_closed_transcript(agent, tmp_path, monkeypatch):
    """No live run for this file: nothing is open, whatever the transcript's
    own rows look like."""
    target = _write_transcript(agent, tmp_path, monkeypatch, [
        _t_user("fix the header"),
        _t_assistant("Fixed."),
    ])
    out = agent._history(target, "sess1")
    assert out["open_from"] is None


def test_open_from_marks_the_still_streaming_exchange(agent, tmp_path, monkeypatch):
    target = _write_transcript(agent, tmp_path, monkeypatch, [
        _t_user("fix the header"),
        _t_assistant("Fixed."),
        _t_user("now the footer too"),
        _t_assistant("On it."),
    ])
    # The live run has echoed exactly one turn's own opening message back —
    # that one turn is all it owns.
    _setup_live_run(agent, tmp_path, monkeypatch, target, "sess1",
                    [_out_start_row("now the footer too")])
    out = agent._history(target, "sess1")
    assert out["open_from"] == 2, out["turns"]
    assert out["turns"][2] == {"role": "user", "text": "now the footer too",
                               "uuid": ""}


def test_open_from_survives_a_follow_up_folded_into_the_open_turn(agent, tmp_path, monkeypatch):
    """`_send` folds a follow-up into a turn still streaming — the live run's
    `out.jsonl` echoes back TWO `_starts_new_turn` rows for the one exchange,
    so `open_from` reserves both user turns, not just the newest."""
    target = _write_transcript(agent, tmp_path, monkeypatch, [
        _t_user("fix the header"),
        _t_assistant("Fixed."),
        _t_user("now the footer too"),
        _t_assistant("Working on it."),
        _t_user("and make it sticky"),
    ])
    _setup_live_run(agent, tmp_path, monkeypatch, target, "sess1", [
        _out_start_row("now the footer too"),
        _out_start_row("and make it sticky"),
    ])
    out = agent._history(target, "sess1")
    assert out["open_from"] == 2
    assert [t["text"] for t in out["turns"][2:] if t["role"] == "user"] == [
        "now the footer too", "and make it sticky"]


def test_a_subagents_own_row_does_not_widen_the_open_exchange(agent, tmp_path, monkeypatch):
    """A Task/Agent tool call spawns a subagent whose own turn-opening row
    (`out.jsonl`, `parent_tool_use_id`) must not count toward how many turns
    the open exchange owns, and whose own prompt on the PERSISTED transcript
    (`isSidechain`) must not appear as a turn at all."""
    target = _write_transcript(agent, tmp_path, monkeypatch, [
        _t_user("fix the header"),
        _t_assistant("Fixed."),
        _t_user("delegate this to a subagent"),
        _t_subagent_user("do the subtask"),
        _t_assistant("working"),
    ])
    _setup_live_run(agent, tmp_path, monkeypatch, target, "sess1", [
        _out_start_row("delegate this to a subagent"),
        {"type": "user", "parent_tool_use_id": "tu1", "message": {
            "role": "user", "content": [{"type": "text", "text": "do the subtask"}]}},
        _out_subagent_result(),
    ])
    out = agent._history(target, "sess1")
    assert out["open_from"] == 2
    assert out["turns"][2] == {"role": "user", "text": "delegate this to a subagent",
                               "uuid": ""}


def test_open_from_reopens_after_a_later_turn_starts(agent, tmp_path, monkeypatch):
    """Only the run's own turns are reserved — earlier, closed exchanges the
    run never touched stay outside `open_from` regardless of how many turns
    the whole transcript holds."""
    target = _write_transcript(agent, tmp_path, monkeypatch, [
        _t_user("one"), _t_assistant("a1"),
        _t_user("two"), _t_assistant("a2"),
        _t_user("three"), _t_assistant("a3"),
    ])
    _setup_live_run(agent, tmp_path, monkeypatch, target, "sess1",
                    [_out_start_row("three")])
    out = agent._history(target, "sess1")
    assert out["open_from"] == 4
    assert out["turns"][4] == {"role": "user", "text": "three", "uuid": ""}


# ------------------------------------------------------------------ pending

TEMPLATE_DIR2 = TEMPLATE_DIR


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    return d


def _write_out(run_dir, rows):
    body = "".join(json.dumps(r) + "\n" for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")


def _append_out(run_dir, rows):
    body = "".join(json.dumps(r) + "\n" for r in rows)
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(body)


def _out_result_row(text, session="s"):
    return {"type": "result", "session_id": session, "result": text}


def _out_user_row(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def _out_text_row(chunk):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": chunk}}}


def _setup_run(agent, run_dir, alive=True):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    agent._pid_alive = lambda _pid: alive
    with open(run_dir / "host.json", "w", encoding="utf-8") as fh:
        json.dump({"pid": 4242, "session_id": "s", "file": "", "mode": "",
                   "model": "", "effort": "", "read_dirs": []}, fh)


def _drain(run_dir):
    """What `session_host._drain_inbox` does to `inbox/` on the real host:
    every entry moves to `inbox/done/` BEFORE its echo can exist in
    `out.jsonl` — the CLI has to receive it before it can write it back.
    A test that appends an echo without draining first is testing a
    sequence the real host never produces."""
    inbox = run_dir / "inbox"
    done = inbox / "done"
    done.mkdir(exist_ok=True)
    for name in sorted(os.listdir(inbox)):
        if name.endswith(".json"):
            os.replace(inbox / name, done / name)


def test_a_prompt_queued_mid_turn_survives_a_reload_and_is_not_duplicated(agent, run_dir):
    # An assistant turn is mid-stream (no `result` yet).
    _write_out(run_dir, [_out_user_row("turn one"), _out_text_row("Working")])
    _setup_run(agent, run_dir)

    sent = agent._send("run", "and also fix the footer", "")
    assert sent == {"sent": True}

    # The host has not drained the inbox and the CLI has not echoed it back —
    # exactly what a fresh page load races against.
    poll = agent._poll("run")
    assert poll["pending"] == ["and also fix the footer"], (
        "a written-but-unechoed message must be reachable through `pending` "
        "so a reload does not lose it")

    # Once the CLI echoes it and replies, a later poll must not repeat it —
    # the echoed message now belongs to `text`/`segments`, not `pending`.
    # The drain (moving the entry out of `inbox/`) happens first, same as
    # the real host: an echo can only exist for a message the CLI already
    # received.
    _drain(run_dir)
    _append_out(run_dir, [_out_user_row("and also fix the footer"),
                          _out_text_row("Done.")])
    later = agent._poll("run")
    assert later["pending"] == []
    _append_out(run_dir, [_out_result_row("Working\n\nDone.")])
    done = agent._poll("run")
    assert done["pending"] == []
    assert done["done"] is True


def test_a_second_follow_up_sent_before_the_first_echoes_stays_pending(agent, run_dir):
    """Finding 8: `_send` overwrites `pending_echo` to anchor on the NEWEST
    outstanding message every time it is called. M1 is sent and drained
    (reaches the CLI); M2 is sent but the host has not drained it yet, so it
    is still sitting in `inbox/` when M1's echo lands. M2 must still read as
    pending — it is undrained, hence provably not yet echoed — not be
    silently dropped by a trim that mistook M1's echo for its own."""
    _write_out(run_dir, [_out_user_row("turn one"), _out_text_row("Working")])
    _setup_run(agent, run_dir)

    agent._send("run", "M1", "")
    _drain(run_dir)  # M1 reaches the CLI; its echo has not landed yet
    agent._send("run", "M2", "")  # still sitting in inbox/: undrained

    # M1's echo lands. M2's has not, and M2 was never drained either.
    _append_out(run_dir, [_out_user_row("M1"), _out_text_row("on M1")])
    poll = agent._poll("run")
    assert poll["pending"] == ["M2"]


def test_pending_is_empty_when_nothing_is_queued(agent, run_dir):
    _write_out(run_dir, [_out_user_row("turn one"),
                         _out_text_row("Answer."),
                         _out_result_row("Answer.")])
    _setup_run(agent, run_dir)
    assert agent._poll("run")["pending"] == []


def test_an_already_echoed_message_does_not_appear_in_pending(agent, run_dir):
    _write_out(run_dir, [_out_user_row("turn one"), _out_text_row("Working")])
    _setup_run(agent, run_dir)

    agent._send("run", "follow-up", "")
    # Drain (as the real host does) before the echo lands.
    _drain(run_dir)
    _append_out(run_dir, [_out_user_row("follow-up")])
    poll = agent._poll("run")
    assert poll["pending"] == []


def test_pending_strips_the_app_state_block(agent, run_dir):
    _write_out(run_dir, [_out_user_row("turn one"), _out_text_row("Working")])
    _setup_run(agent, run_dir)
    from_tag = "<%s>ignored</%s>" % (agent.APP_STATE_TAG, agent.APP_STATE_TAG)
    agent._send("run", from_tag + "fix the footer", "")
    poll = agent._poll("run")
    assert poll["pending"] == ["fix the footer"]

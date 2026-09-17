"""`_write_inbox_row` writes atomically into the directory the session host
is actively draining.

`session_host._drain_inbox` runs every `_DRAIN_INTERVAL_SECONDS` (0.2s) for
the life of a session: it lists `run_dir/inbox/*.json`, reads whatever bytes
are on disk for each name, ships them to the CLI's stdin, and moves the name
to `done/`. `_write_inbox_row` used to open the FINAL `*.json` path directly
(create-and-truncate) and only then `json.dump` into it — a drain tick
landing between the create and the dump finishing sees a truncated or empty
entry, ships that, and moves it to `done/` before the write is even
finished: the user's message is gone, permanently, with nothing left to
retry it from. The fix writes to a `.tmp` name the drain's `*.json` filter
never matches, and `os.replace`s it into place only once the write is
whole — so the entry is either entirely invisible to a drain tick, or
entirely present, never caught in between.
"""
import importlib.util
import json
import os

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load_agent():
    path = os.path.join(TEMPLATE_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_inbox", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path, monkeypatch):
    mod = _load_agent()
    monkeypatch.setattr(mod, "RUNS", str(tmp_path))
    return mod


def test_a_write_in_progress_is_never_visible_as_a_json_entry(agent, tmp_path,
                                                               monkeypatch):
    run_dir = tmp_path / "run"
    os.makedirs(run_dir)
    inbox = run_dir / "inbox"

    seen_during_write = []
    real_dump = json.dump

    def spy_dump(obj, fh):
        # The moment `session_host._drain_inbox` would `os.listdir(inbox)`
        # on a tick that landed mid-write — before the fix, this is exactly
        # when the final `*.json` name already existed on disk, empty.
        seen_during_write.extend(os.listdir(inbox))
        real_dump(obj, fh)

    monkeypatch.setattr(agent.json, "dump", spy_dump)
    agent._write_inbox_row(str(run_dir), {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": "hi"}]}})

    assert seen_during_write, "the dump spy never ran"
    assert not any(n.endswith(".json") for n in seen_during_write), \
        "a name ending .json must not exist until the write is whole — " \
        "the drain loop only ever looks for that exact suffix"

    # And once the call returns, exactly one whole, valid entry is there.
    names = [n for n in os.listdir(inbox) if n.endswith(".json")]
    assert len(names) == 1
    with open(inbox / names[0], encoding="utf-8") as f:
        row = json.loads(f.read())
    assert row["message"]["content"][0]["text"] == "hi"
    # No stray .tmp left behind either.
    assert not [n for n in os.listdir(inbox) if n.endswith(".tmp")]


# ============ what the chat can SEE of an undrained inbox (Akshil, 2026-09-12)
#
# A follow-up typed into a running turn is written into `run_dir/inbox/` and
# nowhere else until `session_host._drain_inbox` hands it to the CLI — which,
# for a line typed mid-reply, is after the reply in flight has finished. Between
# those two moments the transcript has no row for it, so a reload drew a
# conversation with the user's own last words missing while the run that will
# answer them was still going. `_poll` and `_history_live` now carry them.


def _run_dir(tmp_path):
    d = tmp_path / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    (d / "out.jsonl").write_text("", encoding="utf-8")
    return d


def _drain(run_dir, name):
    """What the session host does when it takes an entry: the bytes go to the
    CLI's stdin and the name is `os.replace`d into `inbox/done/`."""
    done = run_dir / "inbox" / "done"
    done.mkdir(exist_ok=True)
    os.replace(run_dir / "inbox" / name, done / name)


def _echo(run_dir, text):
    """What the CLI writes back through `--replay-user-messages` when it
    actually OPENS the turn a drained entry asked for. Until this row lands the
    message exists nowhere the page can read it."""
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {
            "role": "user", "content": [{"type": "text", "text": text}]}}) + "\n")


def test_the_poll_lists_every_undrained_follow_up_oldest_first(agent, tmp_path):
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_entry(str(run_dir), "first follow-up")
    agent._write_inbox_entry(str(run_dir), "second follow-up")

    inbox = agent._poll("run")["inbox"]
    assert [row["text"] for row in inbox] == ["first follow-up",
                                              "second follow-up"]
    # The entry's own file name is its id, and its write time is `at`.
    names = sorted(n for n in os.listdir(run_dir / "inbox")
                   if n.endswith(".json"))
    assert [row["id"] for row in inbox] == names
    assert inbox[0]["at"] > 0 and inbox[0]["at"] <= inbox[1]["at"]
    # …and none of them is on the wire yet, which is the only thing that can
    # still be thrown away (`_discard_inbox`).
    assert [row["drained"] for row in inbox] == [False, False]


def test_a_drained_entry_is_still_waiting_until_the_cli_echoes_it(agent,
                                                                  tmp_path):
    """DRAINED IS NOT DELIVERED (Akshil, 2026-09-12). The host `os.replace`s
    each entry into `inbox/done/` within 0.2 s of it being written, while the
    CLI holds it in its own queue until the reply in flight finishes — so
    "the inbox directory IS the untaken set" made the bubble flash for a fifth
    of a second and then vanish for the whole of the turn it was typed into.

    What actually says the message has arrived is the CLI's own echo in
    `out.jsonl`, and nothing else on disk can."""
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_entry(str(run_dir), "first follow-up")
    agent._write_inbox_entry(str(run_dir), "second follow-up")
    names = sorted(n for n in os.listdir(run_dir / "inbox")
                   if n.endswith(".json"))

    _drain(run_dir, names[0])
    inbox = agent._poll("run")["inbox"]
    assert [row["text"] for row in inbox] == ["first follow-up",
                                              "second follow-up"]
    # …and the two are different kinds of waiting: the drained one is past the
    # point where it could be discarded.
    assert [row["drained"] for row in inbox] == [True, False]
    assert [row["id"] for row in inbox] == names

    _drain(run_dir, names[1])
    assert [row["drained"] for row in agent._poll("run")["inbox"]] == \
        [True, True]


def test_an_echoed_entry_leaves_the_poll_and_takes_the_ones_before_it(agent,
                                                                      tmp_path):
    """The echo is the hand-off: once the message is in `out.jsonl` the
    transcript carries it and this field has nothing left to say. Both sides are
    FIFO — the host drains in name order and the CLI echoes in the order it
    drained — so the walk back stops at the first entry that has landed."""
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_entry(str(run_dir), "first follow-up")
    agent._write_inbox_entry(str(run_dir), "second follow-up")
    names = sorted(n for n in os.listdir(run_dir / "inbox")
                   if n.endswith(".json"))
    _drain(run_dir, names[0])
    _drain(run_dir, names[1])

    _echo(run_dir, "first follow-up")
    assert [row["text"] for row in agent._poll("run")["inbox"]] == \
        ["second follow-up"]

    _echo(run_dir, "second follow-up")
    assert agent._poll("run")["inbox"] == []


def test_two_identical_follow_ups_with_one_echo_still_show_the_second(
        agent, tmp_path):
    """🔴 review, 2026-09-12. "go on" twice into a running turn, and the CLI
    has opened only the first: every drained entry was compared against the
    NEWEST echo, the second "go on" matched it, and BOTH bubbles went — the
    reader's own words off the screen while the CLI still held one of them.

    The count is a POSITION, not a text search: the echoed entries are a prefix
    of the drained ones, so one echo means one landed, whatever it says."""
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_entry(str(run_dir), "go on")
    agent._write_inbox_entry(str(run_dir), "go on")
    names = sorted(n for n in os.listdir(run_dir / "inbox")
                   if n.endswith(".json"))
    _drain(run_dir, names[0])
    _drain(run_dir, names[1])

    _echo(run_dir, "go on")
    inbox = agent._poll("run")["inbox"]
    assert [row["text"] for row in inbox] == ["go on"]
    # …and it is the SECOND one — the entry that is still waiting, by id.
    assert [row["id"] for row in inbox] == [names[1]]

    _echo(run_dir, "go on")
    assert agent._poll("run")["inbox"] == []


def test_the_echo_window_is_read_once_while_the_transcript_is_unchanged(
        agent, tmp_path, monkeypatch):
    """🟡 review, 2026-09-12. `_poll` runs every 400 ms for the life of a
    run and this decoded up to a megabyte of `out.jsonl` on every one of them,
    for an answer that can only change when the file does. The memo is keyed on
    what the file IS — its size and its mtime — so a second poll over an
    unchanged transcript reads nothing at all."""
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_entry(str(run_dir), "first follow-up")
    agent._write_inbox_entry(str(run_dir), "second follow-up")
    for name in sorted(n for n in os.listdir(run_dir / "inbox")
                       if n.endswith(".json")):
        _drain(run_dir, name)
    _echo(run_dir, "first follow-up")

    reads = []
    real = agent._read_echo_texts
    monkeypatch.setattr(agent, "_read_echo_texts",
                        lambda run: (reads.append(run), real(run))[1])

    first = agent._poll("run")["inbox"]
    second = agent._poll("run")["inbox"]
    assert [row["text"] for row in first] == ["second follow-up"]
    assert [row["text"] for row in second] == ["second follow-up"]
    assert len(reads) == 1

    # …and a byte appended is what makes it read again.
    _echo(run_dir, "second follow-up")
    assert agent._poll("run")["inbox"] == []
    assert len(reads) == 2
    # …after which the newest drained entry is PROVEN echoed, and the walk is
    # skipped whole: nothing reads the file again until the host drains more.
    assert agent._poll("run")["inbox"] == []
    assert len(reads) == 2


def test_a_drained_control_request_is_not_a_message_on_this_side_either(
        agent, tmp_path):
    """`_write_control_request` rows go through the same directory and the same
    `done/`. They are not words anybody typed, so they are not turns on either
    side of the comparison — and counting one would push every real entry a
    place out of step with its echo."""
    run_dir = _run_dir(tmp_path)
    agent._write_control_request(str(run_dir), "interrupt")
    agent._write_inbox_entry(str(run_dir), "the only real one")
    for name in sorted(n for n in os.listdir(run_dir / "inbox")
                       if n.endswith(".json")):
        _drain(run_dir, name)

    assert [row["text"] for row in agent._poll("run")["inbox"]] == \
        ["the only real one"]
    _echo(run_dir, "the only real one")
    assert agent._poll("run")["inbox"] == []


def test_an_idle_chat_lists_nothing_and_never_opens_a_file(agent, tmp_path,
                                                           monkeypatch):
    """The ordinary poll: no inbox directory at all, one `listdir` that fails,
    nothing read."""
    run_dir = _run_dir(tmp_path)
    assert agent._poll("run")["inbox"] == []
    assert not os.path.isdir(run_dir / "inbox")


def test_a_control_request_is_not_a_message_and_a_tmp_is_not_an_entry(
        agent, tmp_path):
    """The same two exclusions `_discard_inbox` makes over the same directory:
    an `interrupt`/`set_model` row is not words anybody typed, and a `.tmp` name
    is a write still in flight."""
    run_dir = _run_dir(tmp_path)
    agent._write_control_request(str(run_dir), "interrupt")
    (run_dir / "inbox" / "99999999999999999999-ff.json.tmp").write_text(
        json.dumps({"type": "user", "message": {
            "role": "user", "content": [{"type": "text", "text": "half"}]}}),
        encoding="utf-8")
    agent._write_inbox_entry(str(run_dir), "the only real one")

    assert [row["text"] for row in agent._poll("run")["inbox"]] == \
        ["the only real one"]


def test_a_reload_learns_the_inbox_on_the_same_answer_as_the_transcript(
        agent, tmp_path, monkeypatch):
    """`_history_live` carries the same rows, so a chat that has just reloaded
    paints the waiting message in the frame it paints the transcript in — the
    whole reason this rides on history rather than waiting for the first poll."""
    run_dir = _run_dir(tmp_path)
    (run_dir / "meta.json").write_text(json.dumps({"file": str(tmp_path)}),
                                       encoding="utf-8")
    agent._write_inbox_entry(str(run_dir), "typed just before the reload")
    monkeypatch.setattr(agent, "_live_run", lambda file, session_id="",
                        **kw: {"run_id": "run"})

    live = agent._history_live(str(tmp_path), "sess-1")
    assert live["live_run"] == "run"
    assert [row["text"] for row in live["inbox"]] == \
        ["typed just before the reload"]

    # …and nothing live means nothing to report, the way it always has.
    monkeypatch.setattr(agent, "_live_run", lambda file, session_id="",
                        **kw: {"run_id": ""})
    assert agent._history_live(str(tmp_path), "sess-1")["inbox"] == []


def test_an_unknown_run_still_answers_the_inbox_field(agent, tmp_path):
    """Shape stability: the error answers a page's stale `run` param gets carry
    every field the good one does, so the client never has to branch on it."""
    assert agent._poll("no-such-run")["inbox"] == []

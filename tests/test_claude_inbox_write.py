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

AGENT_DIR = os.path.join("fused_render", "claude_agent")


def _load_agent():
    path = os.path.join(AGENT_DIR, "agent.py")
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


def _echo_many(run_dir, *texts):
    """What the CLI writes back for lines queued while it was STREAMING: they
    are dequeued together at the turn's end as ONE user row with one `text`
    block per line (measured 2026-09-21, CLI 2.1.2xx)."""
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {
            "role": "user",
            "content": [{"type": "text", "text": t} for t in texts]}}) + "\n")


def test_a_folded_echo_row_retires_every_entry_it_carries(agent, tmp_path):
    """Akshil, 2026-10-04: a "ghost" of the reader's own message sat under
    the reply that had already answered it, for the rest of the run.

    Two lines typed while the model was streaming come back as ONE echo row
    with two text blocks. Read as one joined string that row matched neither
    entry, so both stayed "drained, waiting" forever. The blocks are the
    messages: one echo row with N blocks is N echoes."""
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_entry(str(run_dir), "also no backward compat")
    agent._write_inbox_entry(str(run_dir), "just build it")
    agent._write_inbox_entry(str(run_dir), "and then test")
    names = sorted(n for n in os.listdir(run_dir / "inbox")
                   if n.endswith(".json"))
    for n in names:
        _drain(run_dir, n)

    _echo_many(run_dir, "also no backward compat", "just build it")
    inbox = agent._poll("run")["inbox"]
    assert [row["text"] for row in inbox] == ["and then test"]
    assert [row["id"] for row in inbox] == [names[2]]

    _echo(run_dir, "and then test")
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


def test_the_poll_reports_the_inbox_whatever_the_queue_pref_says(agent, tmp_path):
    """A reload must redraw the mid-turn follow-ups the CLI still holds, so the
    dispatch reports the inbox with the project queue off (queue="0") and
    with no queue param at all, not only for queue="1"."""
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_entry(str(run_dir), "still waiting")

    for kwargs in ({"queue": "0"}, {}, {"queue": "1"}):
        out = agent.main(action="poll", run_id="run", **kwargs)
        assert [row["text"] for row in out["inbox"]] == ["still waiting"], kwargs


# ============ the echo is matched by IDENTITY, over the WHOLE file (2026-10-20)
#
# Akshil, 2026-10-19: "I wrote a msg and got an answer while the background job
# was running, then it disappeared; now when I ask another follow-up question
# the message comes back and then goes away." The second half was this walk:
# it read only the last megabyte of `out.jsonl`, a long first turn pushed the
# earliest echoes out of that window, the position match found nothing, and
# EVERY drained entry was reported waiting — the page deduped the ones that had
# a transcript row and the one that did not (a line steered into a tool call)
# sat under each later follow-up as a ghost.


def _echo_uuid(run_dir, uuid, *texts):
    """A modern CLI's echo: the stdin row's `uuid` comes back on it."""
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": uuid, "isReplay": True,
                             "message": {"role": "user", "content": [
                                 {"type": "text", "text": t} for t in texts]}})
                 + "\n")


def _stream(run_dir, n_bytes):
    """`n_bytes` of streaming rows — the bulk of any real transcript."""
    row = json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "x" * 900}}}) + "\n"
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        for _ in range(n_bytes // len(row) + 1):
            fh.write(row)


def _entries(agent, run_dir, *texts):
    """Write and drain `texts` in order; returns `[(name, uuid)]`."""
    out = []
    for text in texts:
        agent._write_inbox_entry(str(run_dir), text)
        name = sorted(n for n in os.listdir(run_dir / "inbox")
                      if n.endswith(".json"))[-1]
        with open(run_dir / "inbox" / name, encoding="utf-8") as fh:
            uid = json.load(fh)["uuid"]
        _drain(run_dir, name)
        out.append((name, uid))
    return out


def test_an_entry_carries_a_uuid_the_cli_can_hand_back(agent, tmp_path):
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_entry(str(run_dir), "hi")
    name = [n for n in os.listdir(run_dir / "inbox") if n.endswith(".json")][0]
    with open(run_dir / "inbox" / name, encoding="utf-8") as fh:
        row = json.load(fh)
    assert len(row["uuid"]) == 36
    assert row["message"] == {"role": "user",
                              "content": [{"type": "text", "text": "hi"}]}


def test_echoes_older_than_a_megabyte_still_count(agent, tmp_path):
    """The scenario from the recording: a first turn that streamed megabytes,
    two answered follow-ups, one of them steered (echoed, answered, but with
    no transcript row of its own), then a long reply and a fresh follow-up.
    The newest landed echo is more than a megabyte behind the end of the file
    when the fresh one is asked about. Only the fresh one is waiting — the old
    tail window listed all four."""
    run_dir = _run_dir(tmp_path)
    (t0, u0), (q1, u1), (q2, u2) = _entries(
        agent, run_dir, "do the big task", "is there any data from wikipedia?",
        "so you are getting some data from wikipedia?")
    _echo_uuid(run_dir, u0, "do the big task")
    _stream(run_dir, 3_000_000)
    _echo_uuid(run_dir, u1, "is there any data from wikipedia?")
    _stream(run_dir, 20_000)
    _echo_uuid(run_dir, u2, "so you are getting some data from wikipedia?")
    _stream(run_dir, 1_500_000)
    assert agent._poll("run")["inbox"] == []

    (s, us), = _entries(agent, run_dir, "shorter")
    assert [row["id"] for row in agent._poll("run")["inbox"]] == [s]
    _echo_uuid(run_dir, us, "shorter")
    _stream(run_dir, 5_000)
    assert agent._poll("run")["inbox"] == []


def test_the_file_is_read_incrementally(agent, tmp_path, monkeypatch):
    """One pass over the file on the first look, only the new bytes after —
    `_poll` runs every 400 ms and the transcript grows without bound."""
    run_dir = _run_dir(tmp_path)
    (a, ua), (b, ub) = _entries(agent, run_dir, "one", "two")
    _echo_uuid(run_dir, ua, "one")
    _stream(run_dir, 2_000_000)
    sizes = []
    real_read = agent._read_echo_texts

    def spy(run):
        before = os.path.getsize(run_dir / "out.jsonl")
        idx = agent._echo_index.get(run)
        sizes.append(before - (idx["offset"] if idx else 0))
        return real_read(run)

    monkeypatch.setattr(agent, "_read_echo_texts", spy)
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [b]
    _echo_uuid(run_dir, ub, "two")
    assert agent._poll("run")["inbox"] == []
    assert sizes[0] > 2_000_000          # the first look reads it all
    assert 0 < sizes[1] < 1_000          # the second reads the echo alone


def test_a_half_written_last_line_waits_for_the_next_read(agent, tmp_path):
    run_dir = _run_dir(tmp_path)
    (a, ua), = _entries(agent, run_dir, "one")
    whole = json.dumps({"type": "user", "uuid": ua, "message": {
        "role": "user", "content": [{"type": "text", "text": "one"}]}}) + "\n"
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(whole[:40])
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [a]
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(whole[40:])
    assert agent._poll("run")["inbox"] == []


def test_a_rewritten_file_is_indexed_again_from_the_start(agent, tmp_path):
    run_dir = _run_dir(tmp_path)
    (a, ua), = _entries(agent, run_dir, "one")
    _stream(run_dir, 50_000)
    _echo_uuid(run_dir, ua, "one")
    assert agent._poll("run")["inbox"] == []
    # The file is replaced by a shorter one that holds no echo at all: the
    # stale index must not vouch for "one" — the walk starts over at byte 0.
    (run_dir / "out.jsonl").write_text("", encoding="utf-8")
    (b, ub), = _entries(agent, run_dir, "two")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [a, b]


def test_identity_settles_a_fold_that_position_cannot(agent, tmp_path):
    """The CLI's own fold (measured 2026-09-24): lines queued while it streams
    each get a one-block echo of their own, then ONE merged row repeats them
    all under the last line's uuid. Read by position the repeats break the
    prefix match and every line goes back to "waiting" — the ghost. Read by
    id, the merged row is just the newest line again."""
    run_dir = _run_dir(tmp_path)
    (x, ux), (y, uy), (z, uz) = _entries(agent, run_dir, "first", "second", "third")
    _echo_uuid(run_dir, ux, "first")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [y, z]
    _echo_uuid(run_dir, uy, "second")
    _echo_uuid(run_dir, uz, "third")
    _echo_uuid(run_dir, uz, "second", "third")
    assert agent._poll("run")["inbox"] == []


def test_a_stamped_entry_is_never_settled_by_somebody_elses_words(agent, tmp_path):
    """🔴 review, 2026-10-20. Three "go on"s each echoed under their own id,
    then the CLI's merged row repeating two of them under the last id; a
    FOURTH "go on" not yet echoed. Matched by words it would take one of the
    merged row's leftover blocks and vanish from the screen while the CLI
    still held it. A stamped entry the ids did not match is waiting."""
    run_dir = _run_dir(tmp_path)
    (a, ua), (b, ub), (c, uc) = _entries(agent, run_dir, "go on", "go on", "go on")
    _echo_uuid(run_dir, ua, "go on")
    _echo_uuid(run_dir, ub, "go on")
    _echo_uuid(run_dir, uc, "go on")
    _echo_uuid(run_dir, uc, "go on", "go on")
    assert agent._poll("run")["inbox"] == []
    (d, ud), = _entries(agent, run_dir, "go on")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [d]
    # …and a foreign user row saying the same words (a hand-back) is not it.
    _echo(run_dir, "go on")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [d]
    _echo_uuid(run_dir, ud, "go on")
    assert agent._poll("run")["inbox"] == []


def test_a_lifecycle_receipt_lands_an_entry_without_its_echo(agent, tmp_path):
    """The CLI's `command_lifecycle` frames name the stdin line by its uuid:
    `started` as the turn opens on it, `completed` when its turn ends. Either
    is proof of landing in its own right."""
    run_dir = _run_dir(tmp_path)
    (a, ua), (b, ub) = _entries(agent, run_dir, "first", "second")
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "command_lifecycle", "command_uuid": ua,
                             "state": "queued"}) + "\n")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [a, b]
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "command_lifecycle", "command_uuid": ua,
                             "state": "started"}) + "\n")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [b]


def test_a_file_rewritten_to_the_same_length_is_indexed_again(agent, tmp_path):
    run_dir = _run_dir(tmp_path)
    (a, ua), = _entries(agent, run_dir, "one")
    _echo_uuid(run_dir, ua, "one")
    assert agent._poll("run")["inbox"] == []
    size = os.path.getsize(run_dir / "out.jsonl")
    # Same inode, same length, no newline where the index's edge was.
    (run_dir / "out.jsonl").write_text("x" * size, encoding="utf-8")
    (b, ub), = _entries(agent, run_dir, "two")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [a, b]


def test_the_position_walk_only_spends_echoes_after_the_last_id_match(
        agent, tmp_path):
    """A stamped "go on" that landed, then an unstamped "go on" that has not:
    the landed one's echo must not be read a second time as the other's."""
    run_dir = _run_dir(tmp_path)
    (a, ua), = _entries(agent, run_dir, "go on")
    agent._write_inbox_row(str(run_dir), {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": "go on"}]}})
    b = sorted(n for n in os.listdir(run_dir / "inbox") if n.endswith(".json"))[-1]
    _drain(run_dir, b)
    _echo_uuid(run_dir, ua, "go on")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [b]
    _echo(run_dir, "go on")
    assert agent._poll("run")["inbox"] == []


def test_an_echo_without_a_uuid_still_lands_by_position(agent, tmp_path):
    """A CLI that ignores the stamp (or an entry an older server wrote without
    one) keeps the position walk, exactly as before."""
    run_dir = _run_dir(tmp_path)
    (a, _ua), (b, _ub) = _entries(agent, run_dir, "first", "second")
    _echo(run_dir, "first")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [b]
    _echo(run_dir, "second")
    assert agent._poll("run")["inbox"] == []


def test_a_landed_id_proves_everything_older_landed_too(agent, tmp_path):
    """FIFO: the host drains in order and the CLI echoes in order, so an entry
    whose own echo is not in the index (a legacy entry, an echo the index has
    aged out) is settled by any newer entry that has landed."""
    run_dir = _run_dir(tmp_path)
    # A legacy entry with no uuid at all, then a stamped one.
    agent._write_inbox_row(str(run_dir), {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": "old style"}]}})
    old = [n for n in os.listdir(run_dir / "inbox") if n.endswith(".json")][0]
    _drain(run_dir, old)
    (b, ub), = _entries(agent, run_dir, "new style")
    _echo_uuid(run_dir, ub, "new style")
    assert agent._poll("run")["inbox"] == []


def test_a_user_row_that_is_not_an_entry_does_not_strand_the_walk(agent, tmp_path):
    """Seen live 2026-10-20: a subagent's hand-back is injected by the CLI as
    a `user` row with text blocks — a turn by shape, but no inbox entry ever
    asked for it. Behind an entry's own echo it made the old suffix match
    fail and listed the entry (an uuid-less one, an older server's) as still
    waiting."""
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_row(str(run_dir), {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": "do the thing"}]}})
    name = [n for n in os.listdir(run_dir / "inbox") if n.endswith(".json")][0]
    _drain(run_dir, name)
    _echo(run_dir, "do the thing")
    _echo(run_dir, "<agent-message from=\"a2973b78602769fc3\"> [Subagent hand-back] …")
    assert agent._poll("run")["inbox"] == []


def test_a_slower_reader_never_moves_the_index_backwards(agent, tmp_path):
    """Two windows on one run can read it at once; whichever read further
    wins, and the other is answered with that list — not allowed to write a
    shorter one back and have the next read count the gap twice."""
    run_dir = _run_dir(tmp_path)
    (a, ua), (b, ub) = _entries(agent, run_dir, "one", "two")
    _echo_uuid(run_dir, ua, "one")
    _echo_uuid(run_dir, ub, "two")
    st = os.stat(run_dir / "out.jsonl")
    ident = (st.st_dev, st.st_ino)
    further = [(ua, agent._echo_key("one")), (ub, agent._echo_key("two"))]
    assert agent._echo_store(str(run_dir), ident, st.st_size, further) == further
    # The slower reader comes back with only the first echo and an older offset.
    assert agent._echo_store(str(run_dir), ident, st.st_size // 2, further[:1]) == further
    assert agent._echo_index[str(run_dir)]["offset"] == st.st_size
    assert agent._poll("run")["inbox"] == []


def test_a_held_lock_is_never_evicted(agent, tmp_path):
    """Bugbot, PR #1778: the per-run lock table used to be cleared wholesale
    at its cap, lock holders included."""
    import threading
    held = agent._echo_locks["held-run"] = threading.Lock()
    held.acquire()
    try:
        for i in range(agent._ECHO_CACHE_MAX + 3):
            agent._echo_locks.setdefault(f"idle-{i}", threading.Lock())
        run_dir = _run_dir(tmp_path)
        (a, ua), = _entries(agent, run_dir, "one")
        _echo_uuid(run_dir, ua, "one")
        assert agent._poll("run")["inbox"] == []
        assert agent._echo_locks["held-run"] is held
        assert len(agent._echo_locks) <= agent._ECHO_CACHE_MAX + 1
    finally:
        held.release()


def test_a_legacy_entry_is_not_settled_by_a_block_under_another_id(agent, tmp_path):
    """CodeRabbit, PR #1778: the words-only walk for an unstamped entry must
    not spend an echo block that carries some OTHER entry's id."""
    run_dir = _run_dir(tmp_path)
    agent._write_inbox_row(str(run_dir), {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": "go on"}]}})
    legacy = [n for n in os.listdir(run_dir / "inbox") if n.endswith(".json")][0]
    _drain(run_dir, legacy)
    # An echo of the same words under a foreign id (another run's line, a
    # message the CLI injected on its own) — not this entry's.
    _echo_uuid(run_dir, "someone-else", "go on")
    assert [r["id"] for r in agent._poll("run")["inbox"]] == [legacy]
    _echo(run_dir, "go on")
    assert agent._poll("run")["inbox"] == []


def test_concurrent_readers_at_lock_capacity_agree(agent, tmp_path):
    """CodeRabbit, PR #1778: with the lock table at capacity, readers of one
    run on several threads must end on one consistent index — every one of
    them holding the run's CURRENT lock, never an evicted orphan — and the
    walk must answer the same thing on every thread."""
    import threading
    run_dir = _run_dir(tmp_path)
    (a, ua), (b, ub) = _entries(agent, run_dir, "one", "two")
    _echo_uuid(run_dir, ua, "one")
    _stream(run_dir, 400_000)
    _echo_uuid(run_dir, ub, "two")
    for i in range(agent._ECHO_CACHE_MAX + 5):
        agent._echo_locks.setdefault(f"idle-{i}", threading.Lock())
    answers, errors = [], []

    def read():
        try:
            answers.append([r["id"] for r in agent._poll("run")["inbox"]])
        except Exception as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert answers == [[]] * 8
    state = agent._echo_index[str(run_dir)]
    assert state["offset"] == os.path.getsize(run_dir / "out.jsonl")
    assert [uid for uid, _k in state["echoes"]] == [ua, ub]
    assert not agent._echo_locks[str(run_dir)].locked()

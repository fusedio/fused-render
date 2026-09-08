"""B2: the uncommitted-work sweep must run once PER TURN, not once per run.

`_poll`'s fallback sweep (`_commit_turn`, guarded by `run_dir/committed`) used
to claim a single, whole-run marker the first time a clean turn was seen. A
run used to BE a turn, so that was fine — but a run is now a whole
long-lived session behind one held-open `claude` process (many turns), and
the marker being run-scoped meant only the FIRST clean turn of a session
could ever claim it. Every later turn, however cleanly it ended with its own
uncommitted edits, found the marker already there and swept nothing.

The marker is now keyed by the turn's own cursor boundary (`scan_cursor`,
the same proven turn-start offset `_cancelled_marker_state` keys off of) so
each new turn gets its own one-shot claim, while a poll landing twice on the
SAME already-committed turn still does not double-commit.
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


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    return d


def _write(run_dir, rows):
    body = "".join(json.dumps(r) + "\n" for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")


def _append(run_dir, rows):
    body = "".join(json.dumps(r) + "\n" for r in rows)
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(body)


def _text_row(chunk):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": chunk}}}


def _result_row(session="s"):
    return {"type": "result", "session_id": session, "result": "done"}


def _user_row(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def _setup(agent, run_dir, target, alive=False):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    agent._pid_alive = lambda _pid: alive
    with open(run_dir / "meta.json", "w", encoding="utf-8") as fh:
        json.dump({"file": target, "message": "do the thing"}, fh)


def test_a_later_clean_turn_is_swept_too_not_just_the_first(agent, run_dir, tmp_path):
    """Two turns of the SAME session, both ending clean with `done=True`,
    `error` unset. The old whole-run boolean marker let only turn one claim
    the sweep; turn two's own uncommitted work is left behind forever."""
    target = str(tmp_path / "app.txt")
    calls = []
    agent._commit_turn = lambda file, message: calls.append((file, message))

    _write(run_dir, [_user_row("turn one"), _text_row("ok"), _result_row()])
    _setup(agent, run_dir, target)

    first = agent._poll("run")
    assert first["done"] is True
    assert len(calls) == 1, "turn one's clean end must sweep once"

    # Same content, another poll of the SAME (already-ended) turn one: must
    # not double-commit it.
    again = agent._poll("run")
    assert again["done"] is True
    assert len(calls) == 1, "a repeat poll of the same turn must not re-sweep"

    # Turn two: a genuinely new, PROVEN turn boundary (the second user row,
    # after turn one's own result).
    _append(run_dir, [_user_row("turn two"), _text_row("ok"), _result_row()])
    second = agent._poll("run")
    assert second["done"] is True
    assert len(calls) == 2, (
        "turn two ended clean too — its own uncommitted work must be swept "
        "just as turn one's was, not skipped because turn one already "
        "claimed a whole-run marker")


def test_an_errored_turn_is_not_swept(agent, run_dir, tmp_path):
    target = str(tmp_path / "app.txt")
    calls = []
    agent._commit_turn = lambda file, message: calls.append((file, message))

    _write(run_dir, [_user_row("turn one"), _text_row("ok"), _result_row()])
    _setup(agent, run_dir, target, alive=False)

    agent._poll("run")
    assert len(calls) == 1

    # Turn two crashes: dead with no closing `result` row at all. One poll
    # first, mid-turn (alive again briefly), to let the cursor advance past
    # turn one's own boundary — same as a page continuously polling would
    # naturally do before the process ever died; only then does the FINAL
    # poll's `rows` window hold turn two's content alone, same as real usage.
    agent._alive = lambda _run_dir: True
    agent._pid_alive = lambda _pid: True
    _append(run_dir, [_user_row("turn two"), _text_row("partial")])
    agent._poll("run")

    agent._alive = lambda _run_dir: False
    agent._pid_alive = lambda _pid: False
    second = agent._poll("run")
    assert second["done"] is True
    assert second["error"]
    assert len(calls) == 1, "a crashed turn must not be swept into a commit"

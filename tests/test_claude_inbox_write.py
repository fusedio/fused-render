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

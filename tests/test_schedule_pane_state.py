"""What a scheduled run tells the transcript about the file it ran on.

The Claude page prepends a `<live-app-state>` block to every send a human
makes, and that block is the only durable record of which FILE a chat is
about — a transcript's own `cwd` is always the folder, because Claude Code
keys its session store by cwd and a file has no cwd. The block is built in
browser JS at send time (`template.html` `composeOutgoing`), so a scheduled
run, which has no browser, used to produce a session that read as if it had
come from the folder even when the task named a file.

Both readers of that record are pinned here against what the scheduler
writes: `tasks_store.pane_file`, which decides the file "open this task"
lands on, and the template's `_pane_file`, which decides the chats a file is
offered. A block only one of them can read is worse than no block.
"""
import importlib.util
import json
import os

import pytest

from fused_render import claude_spawn, schedule, tasks_store


def _load_agent():
    """The template's own reader, loaded the way the other template tests load
    it — it is not importable as a package module."""
    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_pane", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def agent():
    return _load_agent()


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    f = d / "index.html"
    f.write_text("<html></html>")
    return str(f), str(d)


def _entry(target, message="run the weekly report"):
    return {"id": "e-1", "target": target, "message": message}


def test_a_file_target_is_named_in_front_of_the_message(target):
    file, _ = target
    out = schedule._outgoing(_entry(file))
    assert out.startswith("<live-app-state>")
    assert out.endswith("run the weekly report")


def test_both_readers_find_the_file_the_task_named(agent, target):
    """The whole point: one block, two hand-duplicated readers (D166), one
    answer. If these disagree, a row opens somewhere the list said it would
    not."""
    file, _ = target
    out = schedule._outgoing(_entry(file))
    assert tasks_store.pane_file(out) == file
    assert agent._pane_file(out) == file


def test_the_block_is_stripped_back_off_the_words(agent, target):
    """It is machinery, and every reader that shows a human what was said
    peels it — the task row must be titled with the message, not with the
    block."""
    file, _ = target
    out = schedule._outgoing(_entry(file))
    assert tasks_store.strip_machinery(out) == "run the weekly report"
    assert agent._strip_machinery(out) == "run the weekly report"


def test_a_folder_target_is_left_alone(target):
    """The folder is already what the transcript's cwd says, so a block naming
    it would add nothing and would claim a pane that never existed."""
    _, workdir = target
    assert schedule._outgoing(_entry(workdir)) == "run the weekly report"


def test_a_target_that_is_not_on_disk_is_left_alone(tmp_path):
    """A deleted or renamed target is not a pane. The send still goes — the
    failure, if there is one, belongs to the spawn and lands on the entry."""
    gone = str(tmp_path / "vanished.html")
    assert schedule._outgoing(_entry(gone)) == "run the weekly report"
    assert schedule._outgoing(_entry("")) == "run the weekly report"


def test_nothing_but_the_target_is_claimed(target):
    """There is no screen to snapshot, no pane shot to take and no annotation
    to carry, and inventing any of them would put a description of a screen
    nobody was looking at into the transcript."""
    file, _ = target
    out = schedule._outgoing(_entry(file))
    assert "pane-shot" not in out
    assert "The user annotated" not in out
    blob = out[out.index("{"):out.index("}") + 1]
    assert json.loads(blob) == {"entry": file, "scheduled": True}
    # And it says so in words, for the model reading it mid-run.
    assert "scheduled run" in out


def test_the_send_carries_the_composed_message(target, monkeypatch):
    """`_send` is where it actually reaches the helper — the stored entry is
    NOT rewritten, so the message the user typed is still the message the
    store holds."""
    file, _ = target
    seen = {}

    def fake_spawn(t, prompt, permission_mode, session_id=""):
        seen["prompt"] = prompt
        return {"error": "stop here"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_update", lambda *a, **kw: None)
    monkeypatch.setattr(schedule, "_report", lambda *a, **kw: None)
    monkeypatch.setattr(schedule, "_emit", lambda *a, **kw: None)
    entry = _entry(file)
    schedule._send(entry)
    assert seen["prompt"] == schedule._outgoing(entry)
    assert entry["message"] == "run the weekly report"

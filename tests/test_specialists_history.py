"""The home's git history: Chat and Update commits, the update's inbox files, hand edits, and Changes."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os
import subprocess

import pytest

from fused_render.specialists import chat, history, store, update

pytestmark = needs_git


def _git(home, *args):
    return subprocess.run(["git", "-C", home, *args], capture_output=True, text=True).stdout


def _row(kind, content):
    return json.dumps({"type": kind, "timestamp": "2026-11-19T10:00:00Z", "message": {"content": content}})


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """`write(rows)` sets the transcript every session reads."""
    path = tmp_path / "t.jsonl"
    monkeypatch.setattr(chat, "transcript", lambda target, sid: str(path))
    monkeypatch.setattr(store, "live_runs", lambda home: [])
    return lambda rows: path.write_text("\n".join(rows) + "\n")


def _edit(home, rel, text):
    os.makedirs(os.path.dirname(os.path.join(home, rel)), exist_ok=True)
    with open(os.path.join(home, rel), "w") as f:
        f.write(text)


def test_a_chat_turn_commits_with_its_first_words(transcript):
    s = make_specialist()
    transcript([_row("user", "Remember that " + "we always use parquet files " * 4),
                _row("assistant", [{"type": "text", "text": "Noted."}])])
    _edit(s.home, "memory.md", "We use parquet.\n")
    history.after_turn(s.home, "sid")
    subject = _git(s.home, "log", "-1", "--format=%s").strip()
    assert subject.startswith("Chat: Remember that we always") and subject.endswith("…") and len(subject) == 67
    history.after_turn(s.home, "sid")  # nothing changed: no commit
    assert _git(s.home, "rev-list", "--count", "HEAD").strip() == "2"


def test_an_update_removes_only_the_files_it_was_given(transcript):
    s = make_specialist()
    _edit(s.home, "inbox/a.md", "note a")
    _edit(s.home, "inbox/late.md", "arrived during the update")
    chat.change(s.home, lambda d: d.update(update_files=["a.md"]))
    transcript([_row("user", update.PROMPT),
                _row("assistant", [{"type": "tool_use", "name": "Edit"}]),
                _row("assistant", [{"type": "text", "text": "Added one line about parquet."}])])
    _edit(s.home, "memory.md", "We use parquet.\n")
    history.after_turn(s.home, "sid")
    assert sorted(os.listdir(os.path.join(s.home, "inbox"))) == ["late.md"]
    assert _git(s.home, "log", "-1", "--format=%B").strip() == "Update\n\nAdded one line about parquet."
    assert chat.load(s.home)["update_files"] == []


def test_changes_lists_memory_and_skills_only_and_hand_edits(transcript):
    s = make_specialist()
    _edit(s.home, "inbox/n.md", "a note")
    history.commit(s.home, "Notes waiting", ["inbox"])
    _edit(s.home, "memory.md", "By hand.\n")
    _edit(s.home, ".claude/skills/x/SKILL.md", "---\nname: x\n---\n")
    history.commit_edits(s.home)
    (row,) = history.changes(s.home)  # neither "Created" nor the inbox-only commit
    assert row["label"] == "You edited memory" and row["at"] > 0
    diff = history.diff(s.home, row["sha"])
    # Hunks only, both files, no file headers: the pane renders every line it gets.
    assert "+By hand." in diff and "+name: x" in diff
    assert all(ln.startswith(("@@", " ", "+", "-", "\\")) and not ln.startswith(("+++", "---"))
               for ln in diff.splitlines())
    assert history.diff(s.home, "not-a-sha") is None and history.diff(s.home, "0" * 40) is None


def test_a_hand_edit_waits_while_a_session_is_open(monkeypatch):
    from fused_render.claude_agent import agent_module

    s = make_specialist()
    _edit(s.home, "memory.md", "By hand.\n")
    # Open, even between turns: a turn that has ended is committed by its run's next poll, with its own label.
    monkeypatch.setattr(agent_module(), "_hosts_in", lambda folder: ["r"])
    history.commit_edits(s.home)
    assert history.changes(s.home) == []


def test_planted_repository_config_is_dropped(transcript):
    s = make_specialist()
    with open(os.path.join(s.home, ".git", "config"), "a") as f:
        f.write('[filter "x"]\n\tclean = cat\n')
    _edit(s.home, "memory.md", "x\n")
    history.commit_edits(s.home)
    assert "filter" not in open(os.path.join(s.home, ".git", "config")).read()

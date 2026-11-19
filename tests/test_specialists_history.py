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
    _edit(s.home, "inbox/brk.md", "# A breakout\n")
    history.after_turn(s.home, "sid")
    subject = _git(s.home, "log", "-1", "--format=%s").strip()
    assert _git(s.home, "status", "--porcelain").strip() == "?? inbox/"  # left for the "Notes waiting" commit
    assert subject.startswith("Chat: Remember that we always") and subject.endswith("…") and len(subject) == 67
    history.after_turn(s.home, "sid")  # nothing changed: no commit
    assert _git(s.home, "rev-list", "--count", "HEAD").strip() == "2"


def test_a_learning_run_removes_only_its_files_and_is_listed_even_when_it_changed_nothing(transcript):
    s = make_specialist()
    _edit(s.home, "inbox/a.md", '# Breakout from "Fix the canvas"\n\nTranscript: /t/b.jsonl\nFrom line: 1\n')
    _edit(s.home, "inbox/b.md", '# Note from task "Sync"\n\nUse parquet.\n')
    history.commit(s.home, "Notes waiting", ["inbox"])
    _edit(s.home, "inbox/late.md", "arrived during the run")
    chat.change(s.home, lambda d: d.update(update={"session": "sid", "files": ["a.md", "b.md"], "lines": {}}))
    transcript([_row("user", update.PROMPT), _row("assistant", [{"type": "text", "text": "Start a reader."}]),
                _row("assistant", [{"type": "text", "text": "Nothing new."}])])  # the final message only
    history.after_turn(s.home, "sid")
    assert sorted(os.listdir(os.path.join(s.home, "inbox"))) == ["late.md"] and chat.load(s.home)["update"] is None
    assert sorted(os.listdir(os.path.join(s.home, "inbox-archive"))) == ["a.md", "b.md"]  # kept, not deleted
    (row,) = history.changes(s.home)  # it changed neither memory nor skills, and is listed
    assert (row["label"], row["read"], row["changed"]) == ("Update", 2, False)
    assert history.learned(s.home, row["sha"]) == {"reply": "Nothing new.", "read": [
        {"kind": "breakout", "title": "Fix the canvas"}, {"kind": "note", "title": "Sync"}]}
    assert history.diff(s.home, row["sha"]) == ""


def test_changes_lists_memory_and_skills_only_and_hand_edits(transcript):
    s = make_specialist()
    _edit(s.home, "inbox/n.md", "a note")
    history.commit(s.home, "Notes waiting", ["inbox"])
    _edit(s.home, "memory.md", "By hand.\n")
    _edit(s.home, ".claude/skills/x/SKILL.md", "---\nname: x\n---\n")
    history.commit_edits(s.home)
    (row,) = history.changes(s.home)  # neither "Created" nor the inbox-only commit
    assert row["label"] == "You edited memory" and row["at"] > 0 and row["changed"]
    assert history.learned(s.home, row["sha"]) == {}
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
    monkeypatch.setattr(agent_module(), "_live_runs_in", lambda folder, idle=False: iter([{"run_id": "r"}]))
    history.commit_edits(s.home)
    assert history.changes(s.home) == []


def test_planted_repository_config_is_dropped(transcript):
    s = make_specialist()
    with open(os.path.join(s.home, ".git", "config"), "a") as f:
        f.write('[filter "x"]\n\tclean = cat\n')
    _edit(s.home, "memory.md", "x\n")
    history.commit_edits(s.home)
    assert "filter" not in open(os.path.join(s.home, ".git", "config")).read()


def test_a_chat_turn_no_page_polled_keeps_its_label_and_a_later_hand_edit_is_yours(transcript):
    s = make_specialist()
    chat.record_start(s.id, "main", "sid")
    _edit(s.home, "memory.md", "We use parquet.\n")
    transcript([_row("user", "Remember parquet"), _row("assistant", [{"type": "text", "text": "Noted."}])])
    history.commit_edits(s.home)
    assert _git(s.home, "log", "-1", "--format=%s").strip() == "Chat: Remember parquet"
    os.utime(os.path.join(s.home, "memory.md"))  # a later edit than the transcript
    t = os.path.getmtime(os.path.join(s.home, "memory.md"))
    os.utime(chat.transcript(s.home, "sid"), (t - 60, t - 60))
    _edit(s.home, "memory.md", "We use parquet. Always.\n")
    history.commit_edits(s.home)
    assert _git(s.home, "log", "-1", "--format=%s").strip() == "You edited memory"


def test_restore_drops_a_failed_update_s_edits(transcript):
    s = make_specialist()
    _edit(s.home, "memory.md", "Half done.\n")
    _edit(s.home, ".claude/skills/new/SKILL.md", "x")
    history.restore(s.home)
    assert open(os.path.join(s.home, "memory.md")).read() == ""
    assert not os.path.exists(os.path.join(s.home, ".claude", "skills", "new"))

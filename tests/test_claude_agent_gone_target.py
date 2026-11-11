"""`agent._start` on a target file that is gone (Akshil, 2026-11-08).

Claude Code runs in the target's FOLDER, so a file a turn renamed or split
away must not lock the chat out while that folder is there. Probed through
the refusal that comes right after the existence check (an unknown backend):
"unknown agent backend" means the target was accepted, "target not found"
means it was not — no run is ever spawned.
"""
import importlib.util
import os

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")


@pytest.fixture
def agent():
    path = os.path.join(AGENT_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_gone", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _start(agent, file):
    return agent._start(file, "hi", "", "", "", backend="no-such-agent")


def test_a_gone_file_in_a_folder_that_is_there_is_accepted(agent, tmp_path):
    out = _start(agent, str(tmp_path / "split_away.py"))
    assert "target not found" not in out.get("error", "")
    assert "unknown agent backend" in out.get("error", "")


def test_a_gone_file_in_a_gone_folder_is_refused(agent, tmp_path):
    gone = tmp_path / "gone" / "hex_pipeline.py"
    assert _start(agent, str(gone)) == {"error": f"target not found: {gone}"}


def test_an_existing_file_and_folder_are_accepted_as_before(agent, tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    for target in (str(f), str(tmp_path)):
        assert "unknown agent backend" in _start(agent, target).get("error", "")


def test_a_gone_folder_is_refused_not_walked_up(agent, tmp_path):
    """A gone folder (no extension: a deleted canvas clone or app folder) is a
    gone chat — it must not start the turn in its parent (Bugbot #2195)."""
    gone = tmp_path / "deleted_clone"
    assert _start(agent, str(gone)) == {"error": f"target not found: {gone}"}


def _breakout_start(agent, file, home, monkeypatch):
    monkeypatch.setattr(agent, "_breakout_pref", lambda: True)
    return agent._start(file, "hi", "", "", "", backend="no-such-agent",
                        breakout=True, breakout_home=home)


def test_a_breakout_in_a_gone_folder_falls_back_to_the_chats_folder(agent, tmp_path, monkeypatch):
    gone = tmp_path / ".claude" / "worktrees" / "h3-reduce-batch-route"
    out = _breakout_start(agent, str(gone), str(tmp_path), monkeypatch)
    assert "target not found" not in out.get("error", "")
    assert "unknown agent backend" in out.get("error", "")


def test_a_breakout_whose_chat_folder_is_gone_too_is_refused(agent, tmp_path, monkeypatch):
    gone = tmp_path / "wt"
    out = _breakout_start(agent, str(gone), str(tmp_path / "also-gone"), monkeypatch)
    assert out == {"error": f"target not found: {gone}"}


def test_a_non_breakout_start_ignores_the_home_and_still_refuses(agent, tmp_path):
    gone = tmp_path / "wt"
    out = agent._start(str(gone), "hi", "", "", "", backend="no-such-agent",
                       breakout_home=str(tmp_path))
    assert out == {"error": f"target not found: {gone}"}

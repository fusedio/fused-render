"""The launch spec of each kind of expert session, and the session token."""
from _experts_conftest import *  # noqa: F401,F403
import json
import os

import pytest

from fused_render.experts import launch, store


@pytest.fixture(autouse=True)
def _idle(monkeypatch):
    monkeypatch.setattr(store, "live_runs", lambda home: [])


def test_main_and_breakout_get_the_services_and_no_rules():
    s = make_expert()
    for kind in ("main", "breakout"):
        sp = launch.spec(f"{s.id}:{kind}", s.home)
        json.dumps(sp)  # the run dir stores it
        assert sp["kind"] == kind and sp["policy_env"] == {}
        assert "deny" not in sp and sp["ask"] == ["mcp__expert_tasks__task_start"]  # each new task is the user's to approve
        assert set(sp["mcp_servers"]) == {"expert_services", "expert_tasks"}
        assert sp["mcp_servers"]["expert_tasks"]["args"][4:] == [f"api/experts/{s.id}/expert_tasks", "expert_tasks"]
        entry = sp["mcp_servers"]["expert_services"]
        assert "botmcp" in os.path.basename(entry["args"][0]) and entry["timeout"] > 60_000
        token = entry["args"][3]
        assert entry["args"][4:] == [f"api/experts/{s.id}/expert_services", "expert_services"]
        assert launch.check_token(s.id, token) and not launch.check_token("other", token)
    assert "breakout" in sp["append"]


def test_a_learning_run_has_the_chat_s_tools_and_nobody_to_ask():
    s = make_expert()
    sp, main = launch.spec(f"{s.id}:update", s.home), launch.spec(f"{s.id}:main", s.home)
    # … but not the task tools: nobody is there to approve a start
    assert set(sp["mcp_servers"]) == set(main["mcp_servers"]) - {"expert_tasks"} and "ask" not in sp
    assert sp["policy_env"] == {"FUSED_EXPERT_UNATTENDED": "1"}  # a card would wait for nobody: refused at once


def test_refusals(monkeypatch, tmp_path):
    s = make_expert()
    assert launch.spec(f"{s.id}:main", str(tmp_path))["reason"] == "wrong_folder"
    assert launch.spec(f"{s.id}:other", s.home)["reason"] == "bad_key"
    assert launch.spec("nope:main", s.home)["reason"] == "no_expert"
    monkeypatch.setenv("FUSED_RENDER_EXPERTS", "0")
    assert launch.spec(f"{s.id}:main", s.home)["reason"] == "experts_disabled"


@needs_git
def test_a_start_commits_a_hand_edit_and_resets_the_settings():
    from fused_render.experts import history

    s = make_expert()
    with open(os.path.join(s.home, "memory.md"), "w") as f:
        f.write("By hand.\n")
    with open(os.path.join(s.home, ".claude", "settings.json"), "w") as f:
        f.write('{"hooks": {}}')
    launch.spec(f"{s.id}:main", s.home)
    assert [c["label"] for c in history.changes(s.home)] == ["You edited memory"]
    assert json.load(open(os.path.join(s.home, ".claude", "settings.json"))) == {"autoMemoryEnabled": False}


@needs_git
def test_a_start_brings_an_older_charter_up_to_date_and_keeps_its_subject():
    import subprocess

    from fused_render.experts import history
    from fused_render.experts import home as hm

    s = make_expert()
    path = os.path.join(s.home, "CLAUDE.md")
    with open(path, "w") as f:
        f.write("# An older charter\nKeep memory as plain sentences.\n\n" + hm.SUBJECT_MARK + "Tiles are 512 px.\n")
    subprocess.run(["git", "-C", s.home, "commit", "-qam", "old"], check=True)
    launch.spec(f"{s.id}:main", s.home)
    assert open(path).read() == hm.charter(s.name, s.about, "Tiles are 512 px.")
    log = lambda: subprocess.run(["git", "-C", s.home, "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log().splitlines()[0] == "Settings" and not history.changes(s.home)
    before, mtime = log(), os.path.getmtime(path)
    launch.spec(f"{s.id}:main", s.home)  # up to date: not rewritten, nothing committed
    assert (log(), os.path.getmtime(path)) == (before, mtime)


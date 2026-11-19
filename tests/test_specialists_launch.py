"""The launch spec of each kind of specialist session, and the session token."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os

import pytest

from fused_render.specialists import launch, store


@pytest.fixture(autouse=True)
def _idle(monkeypatch):
    monkeypatch.setattr(store, "live_runs", lambda home: [])


def test_main_and_breakout_get_the_services_and_no_rules():
    s = make_specialist()
    for kind in ("main", "breakout"):
        sp = launch.spec(f"{s.id}:{kind}", s.home)
        json.dumps(sp)  # the run dir stores it
        assert sp["kind"] == kind and sp["policy_env"] == {}
        assert "deny" not in sp and "ask" not in sp
        assert set(sp["mcp_servers"]) == {"specialist_services"}
        entry = sp["mcp_servers"]["specialist_services"]
        assert "botmcp" in os.path.basename(entry["args"][0]) and entry["timeout"] > 60_000
        token = entry["args"][3]
        assert entry["args"][4:] == [f"api/specialists/{s.id}/specialist_services", "specialist_services"]
        assert launch.check_token(s.id, token) and not launch.check_token("other", token)
    assert "breakout" in sp["append"]


def test_a_learning_run_has_the_chat_s_tools_and_nobody_to_ask():
    s = make_specialist()
    sp, main = launch.spec(f"{s.id}:update", s.home), launch.spec(f"{s.id}:main", s.home)
    assert set(sp["mcp_servers"]) == set(main["mcp_servers"])
    assert sp["policy_env"] == {"FUSED_SPECIALIST_UNATTENDED": "1"}  # a card would wait for nobody: refused at once


def test_refusals(monkeypatch, tmp_path):
    s = make_specialist()
    assert launch.spec(f"{s.id}:main", str(tmp_path))["reason"] == "wrong_folder"
    assert launch.spec(f"{s.id}:other", s.home)["reason"] == "bad_key"
    assert launch.spec("nope:main", s.home)["reason"] == "no_specialist"
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert launch.spec(f"{s.id}:main", s.home)["reason"] == "specialists_disabled"


@needs_git
def test_a_start_commits_a_hand_edit_and_resets_the_settings():
    from fused_render.specialists import history

    s = make_specialist()
    with open(os.path.join(s.home, "memory.md"), "w") as f:
        f.write("By hand.\n")
    with open(os.path.join(s.home, ".claude", "settings.json"), "w") as f:
        f.write('{"hooks": {}}')
    launch.spec(f"{s.id}:main", s.home)
    assert [c["label"] for c in history.changes(s.home)] == ["You edited memory"]
    assert json.load(open(os.path.join(s.home, ".claude", "settings.json"))) == {"autoMemoryEnabled": False}

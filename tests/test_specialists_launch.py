"""The launch spec of each kind of specialist session, and the session token."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os

import pytest

from fused_render.specialists import launch, services, store


@pytest.fixture(autouse=True)
def _idle(monkeypatch):
    monkeypatch.setattr(store, "live_runs", lambda home: [])


def _deny(home):
    h = home.lstrip("/")
    return [f"Edit(//{h}/{p})" for p in (".git/**", ".specialist/**", ".claude/settings.json",
                                         ".claude/settings.local.json")]


def test_main_and_breakout_get_the_services_and_a_send_always_asks():
    s = make_specialist()
    for kind in ("main", "breakout"):
        sp = launch.spec(f"{s.id}:{kind}", s.home)
        json.dumps(sp)  # the run dir stores it
        assert sp["kind"] == kind and sp["policy_env"] == {}
        assert set(_deny(s.home)) <= set(sp["deny"]) and not any("memory.md" in r for r in sp["deny"])
        assert sp["ask"] == ["mcp__specialist_sends"]
        assert set(sp["mcp_servers"]) == set(services.SERVERS)
        entry = sp["mcp_servers"]["specialist_sends"]
        assert "botmcp" in os.path.basename(entry["args"][0]) and entry["timeout"] > 60_000
        token = entry["args"][3]
        assert entry["args"][4:] == [f"api/specialists/{s.id}/specialist_sends", "specialist_sends"]
        assert launch.check_token(s.id, token) and not launch.check_token("other", token)
    assert "breakout" in sp["append"] and "do not edit memory.md" in sp["append"]


def test_the_update_runs_unattended_with_no_servers():
    s = make_specialist()
    sp = launch.spec(f"{s.id}:update", s.home)
    assert sp["mcp_servers"] == {} and sp["ask"] == []
    assert sp["policy_env"] == {"FUSED_SPECIALIST_UNATTENDED": "1"}
    assert set(_deny(s.home)) <= set(sp["deny"])


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

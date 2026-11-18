"""The launch spec of a main chat / breakout (Q: the rules that lock memory in a breakout) and the session token."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os

from fused_render.specialists import launch


def test_main_spec(app_home):
    s = make_specialist()
    sp = launch.spec(f"{s.id}:main", s.home)
    assert "error" not in sp, sp
    json.dumps(sp)  # the run dir stores it
    h = s.home
    assert f"Edit(/{h}/.git/**)" in sp["deny"] and f"Edit(/{h}/said.md)" in sp["deny"]
    assert f"Edit(/{h}/inbox/**)" in sp["deny"] and f"Edit(/{h}/.specialist/**)" in sp["deny"]
    assert not any("memory.md" in r for r in sp["deny"])  # main chat may edit memory at once (F)
    assert sp["ask"] == ["mcp__specialist_services"]
    assert sp["allow"] == ["mcp__specialist__note"]
    assert set(sp["mcp_servers"]) == {"specialist", "specialist_services"}
    entry = sp["mcp_servers"]["specialist"]
    assert "sessmcp" in os.path.basename(entry["args"][0]) and entry["args"][2:] == [s.id, "main", sp["token"], "specialist"]
    assert entry["alwaysLoad"] is True and entry["timeout"] > 60_000
    assert sp["hooks"] == {"Stop": [{"hooks": [{"specialist_hook": "stop"}]}]}
    assert "main chat" in sp["append"]
    assert sp["policy_env"]["FUSED_SPECIALIST_POLICY"].endswith(f"|{s.id}|main")
    assert launch.check_token(s.id, "main", sp["token"])["kind"] == "main"
    assert launch.check_token(s.id, "breakout", sp["token"]) is None
    assert launch.check_token("other", "main", sp["token"]) is None


def test_breakout_spec_locks_everything_it_knows(app_home, monkeypatch, tmp_path):
    s = make_specialist()
    parent = tmp_path / "proj"
    parent.mkdir()
    from fused_render.specialists import breakouts

    monkeypatch.setattr(breakouts, "get", lambda sid, bid: {
        "id": bid, "status": "open", "parent": {"file": str(parent), "session_id": "", "title": "Publish Q4"}},
        raising=False)
    sp = launch.spec(f"{s.id}:breakout:b-1", s.home)
    assert "error" not in sp, sp
    h = s.home
    for p in ("CLAUDE.md", "memory.md", "said.md", "knowledge/**", ".claude/**", "people.md", "triggers.json",
              "specialist.json", "inbox/**", ".specialist/**", ".git/**"):
        assert f"Edit(/{h}/{p})" in sp["deny"], p
    assert f"Read(/{parent}/**)" in sp["allow"] and "mcp__specialist__note" in sp["allow"]
    assert "Publish Q4" in sp["append"] and "locked" in sp["append"]
    assert sp["breakout_id"] == "b-1" and sp["kind"] == "breakout"
    assert launch.check_token(s.id, "breakout", sp["token"])["breakout_id"] == "b-1"


def test_refusals(app_home, monkeypatch, tmp_path):
    s = make_specialist()
    assert launch.spec(f"{s.id}:main", str(tmp_path))["reason"] == "wrong_folder"
    assert launch.spec("nobody:main", s.home)["reason"] == "no_specialist"
    assert launch.spec(f"{s.id}:sideways", s.home)["reason"] == "bad_key"
    from fused_render.specialists import breakouts

    monkeypatch.setattr(breakouts, "get", lambda sid, bid: None, raising=False)
    assert launch.spec(f"{s.id}:breakout:b-9", s.home)["reason"] == "no_breakout"
    monkeypatch.setattr(breakouts, "get", lambda sid, bid: {"status": "deleted", "parent": {}}, raising=False)
    assert launch.spec(f"{s.id}:breakout:b-9", s.home)["reason"] == "no_breakout"
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert launch.spec(f"{s.id}:main", s.home)["reason"] == "specialists_disabled"


def test_symlinked_home_is_the_home(app_home, tmp_path):
    s = make_specialist()
    link = tmp_path / "link"
    os.symlink(s.home, link)
    assert "error" not in launch.spec(f"{s.id}:main", str(link))


def test_token_expiry(app_home, monkeypatch):
    s = make_specialist()
    tok = launch.new_token(s.id, "main")
    import time as _t

    real = _t.time()
    monkeypatch.setattr(launch.time, "time", lambda: real + launch.TOKEN_TTL_S + 1)
    assert launch.check_token(s.id, "main", tok) is None

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
    assert "sessmcp" in os.path.basename(entry["args"][0]) and entry["args"][2:] == [s.id, "main", "-", "specialist"]
    assert sp["token"] not in json.dumps(entry["args"]) and entry["env"]["FUSED_SPECIALIST_TOKEN"] == sp["token"]
    for p in (".claude/settings.json", ".claude/settings.local.json"):
        assert f"Edit(/{h}/{p})" in sp["deny"], p
    assert not any(r.endswith("/.claude/skills/**)") for r in sp["deny"])  # main may change its skills (F)
    assert sp["policy_env"] == {"FUSED_SPECIALIST_POLICY": sp["policy_env"]["FUSED_SPECIALIST_POLICY"]}
    assert "grant" not in launch.check_token(s.id, "main", sp["token"])  # no secret a session could read (C2)
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
        "id": bid, "status": "open", "work": True,
        "parent": {"file": str(parent), "session_id": "", "title": "Publish Q4"}}, raising=False)
    work = breakouts.cwd(s.id, "b-1")
    assert work == os.path.join(s.home, ".specialist", "breakouts", "b-1", "work")
    assert launch.spec(f"{s.id}:breakout:b-1", s.home)["reason"] == "wrong_folder"
    sp = launch.spec(f"{s.id}:breakout:b-1", work)
    assert "error" not in sp, sp
    assert os.path.isfile(os.path.join(work, "memory.md"))  # the workroom is synced before the start
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
    # a closed or discarded breakout takes no new turn without Reopen (fix wave item 11)
    for status in ("closed", "discarded"):
        monkeypatch.setattr(breakouts, "get", lambda sid, bid, st=status: {"id": bid, "status": st, "work": True,
                                                                           "parent": {}}, raising=False)
        assert launch.spec(f"{s.id}:breakout:b-9", breakouts.cwd(s.id, "b-9"))["reason"] == "breakout_closed"
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


def test_every_session_start_rewrites_the_home_settings(app_home):
    """R4: whatever a session wrote to the home's project settings is gone before the next session starts."""
    s = make_specialist()
    p = os.path.join(s.home, ".claude", "settings.json")
    with open(p, "w") as f:
        json.dump({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "touch /tmp/pwn"}]}]}}, f)
    with open(os.path.join(s.home, ".claude", "settings.local.json"), "w") as f:
        f.write("{}")
    assert "error" not in launch.spec(f"{s.id}:main", s.home)
    with open(p) as f:
        assert json.load(f) == {"autoMemoryEnabled": False}
    assert not os.path.exists(os.path.join(s.home, ".claude", "settings.local.json"))

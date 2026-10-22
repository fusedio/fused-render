"""Old `fused app serve <dir>` MCP entries are pointed at the fused-render wrapper."""

import json
import os

import pytest

from fused_render import app_serve_migration as mig


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    fused_home = tmp_path / "fr"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(mig.lib, "claude_available", lambda: True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(fused_home))
    home.mkdir()
    old = os.path.join(str(fused_home), "fused-bin", "fused")
    calls = []

    def fake_main(action, name="", **kw):
        calls.append((action, name, json.loads(kw["json"])))
        return {"ok": True}

    from fused_render.claude_config import mcp

    monkeypatch.setattr(mcp, "main", fake_main)

    def write(servers):
        (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}))

    return old, calls, write


def test_an_old_entry_is_rewritten_keeping_its_other_fields(world):
    old, calls, write = world
    write({
        "docs-mcp": {"command": old, "args": ["app", "serve", "/a"], "env": {"K": "v"}, "type": "stdio"},
        "other": {"command": "npx", "args": ["x"]},
        "someone-elses": {"command": "/other/fused", "args": ["app", "serve", "/b"]},
        "wrong-shape": {"command": old, "args": ["canvas", "push"]},
    })
    assert mig.migrate("/new/fused-render") == ["docs-mcp"]
    assert calls == [("edit", "docs-mcp", {
        "command": "/new/fused-render", "args": ["app", "serve", "/a"],
        "env": {"K": "v"}, "type": "stdio"})]


def test_a_rewritten_entry_is_left_alone_next_time(world):
    old, calls, write = world
    write({"docs-mcp": {"command": "/new/fused-render", "args": ["app", "serve", "/a"]}})
    assert mig.migrate("/new/fused-render") == []
    assert calls == []


def test_a_failed_edit_is_reported_not_raised(world, monkeypatch):
    old, calls, write = world
    from fused_render.claude_config import mcp

    monkeypatch.setattr(mcp, "main", lambda **kw: {"ok": False, "stderr": "no claude"})
    write({"docs-mcp": {"command": old, "args": ["app", "serve", "/a"]}})
    assert mig.migrate("/new/fused-render") == []


def test_no_config_file_is_nothing_to_do(world):
    assert mig.migrate("/new/fused-render") == []


def test_start_spawns_no_thread_without_an_old_entry(world, monkeypatch):
    old, calls, write = world
    started = []
    monkeypatch.setattr(mig.threading.Thread, "start", lambda self: started.append(self))
    write({"x": {"command": "npx", "args": []}})
    mig.start("/new/fused-render")
    mig.start(None)
    assert started == []
    write({"docs-mcp": {"command": old, "args": ["app", "serve", "/a"]}})
    mig.start("/new/fused-render")
    assert len(started) == 1


def test_entries_are_read_from_claude_config_dir_when_set(world, tmp_path, monkeypatch):
    old, calls, write = world
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(json.dumps({"mcpServers": {
        "docs-mcp": {"command": old, "args": ["app", "serve", "/a"]}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert mig.migrate("/new/fused-render") == ["docs-mcp"]


def test_start_does_nothing_without_the_claude_cli(world, monkeypatch):
    old, calls, write = world
    started = []
    monkeypatch.setattr(mig.threading.Thread, "start", lambda self: started.append(self))
    monkeypatch.setattr(mig.lib, "claude_available", lambda: False)
    write({"docs-mcp": {"command": old, "args": ["app", "serve", "/a"]}})
    mig.start("/new/fused-render")
    assert started == []


def test_a_refused_edit_restores_the_old_entry(tmp_path, monkeypatch):
    from fused_render.claude_config import lib, mcp

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    spec = {"command": "/old", "args": ["app", "serve", "/a"]}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"docs-mcp": spec}}))
    ran = []

    def cli(*args, **kw):
        ran.append(args)
        refused = args[1] == "add-json" and len(ran) == 2
        return {"ok": not refused, "stdout": "", "stderr": "boom" if refused else ""}

    monkeypatch.setattr(lib, "claude_cli", cli)
    out = mcp.main(action="edit", name="docs-mcp", json=json.dumps({**spec, "command": "/new"}))
    assert out["ok"] is False
    assert ran[0][:2] == ("mcp", "remove")
    assert ran[2][:3] == ("mcp", "add-json", "docs-mcp")
    assert json.loads(ran[2][3]) == spec

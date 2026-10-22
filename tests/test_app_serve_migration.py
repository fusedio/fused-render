"""Old `fused app serve <dir>` MCP entries are pointed at the fused-render wrapper."""

import json
import os

import pytest

from fused_render import app_serve_migration as mig


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    fused_home = tmp_path / "fr"
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

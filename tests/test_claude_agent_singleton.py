"""The server holds ONE agent module.

Before fused_render/claude_agent existed, every in-process reader exec'd
templates/claude/agent.py afresh (`claude_spawn.load_agent`, the project
queue's cache, canvases' own cache), so the server carried several copies of
the module's state: its echo cache, its inbox-ordering counter. Two copies of
a counter that orders inbox rows is a bug waiting for a busy afternoon. Every
loader now delegates to `claude_agent.agent_module()`, and identity is the
property worth pinning.
"""
import sys

from fused_render import canvases, claude_agent, claude_spawn, project_queue


def test_every_loader_returns_the_same_module_object():
    mod = claude_agent.agent_module()
    assert mod is claude_agent.agent_module()
    assert claude_spawn.load_agent() is mod
    assert project_queue.agent_module() is mod
    assert canvases._agent_module() is mod


def test_the_module_is_the_package_import_not_a_by_path_copy():
    mod = claude_agent.agent_module()
    assert mod.__name__ == "fused_render.claude_agent.agent"
    assert sys.modules["fused_render.claude_agent.agent"] is mod


def test_the_paths_the_children_load_by_sit_beside_the_module():
    import os

    mod = claude_agent.agent_module()
    assert os.path.samefile(claude_agent.AGENT_PATH, mod.__file__)
    for path in (claude_agent.SESSION_HOST_PATH, claude_agent.PERMISSION_SERVER_PATH):
        assert os.path.isfile(path), path
        assert os.path.dirname(path) == os.path.dirname(claude_agent.AGENT_PATH)

"""Connected services in a specialist's chats: one server, reads and sends alike."""
from _specialists_conftest import *  # noqa: F401,F403
from types import SimpleNamespace

import pytest

from fused_render.bots import apptools
from fused_render.specialists import services

SPEC = SimpleNamespace(id="canvas", home="/w/specialists/canvas")


def _rec(app, name, read_only):
    return apptools.ToolRec(app=app, app_dir="connect:" + app, name=name, description="d", params=[],
                            spec=SimpleNamespace(read_only=read_only, input_schema={"type": "object"}))


@pytest.fixture
def ran(monkeypatch):
    recs = [_rec("notion", "search", True), _rec("notion", "create_page", False)]
    monkeypatch.setattr(apptools, "registry", lambda force=False: recs)
    out = []
    monkeypatch.setattr(apptools, "run_tool", lambda rec, args, timeout_s=0: (
        out.append(rec.name) or apptools.RunResult(True, "ran " + rec.name)))
    return out


def test_one_server_runs_reads_and_sends(ran):
    tools = services.mcp_tools("specialist_services")
    assert [t["name"] for t in tools] == ["notion_search", "notion_create_page"]
    assert tools[0]["description"].startswith("[")
    assert services.call(SPEC, "specialist_services", "notion_create_page", {}) == {
        "content": [{"type": "text", "text": "ran create_page"}], "isError": False}
    assert services.call(SPEC, "specialist_services", "nope", {})["isError"] and ran == ["create_page"]


def test_tool_names_fit_mcp():
    assert services.tool_name("app:my.app", "do it") == "app_my_app_do_it"
    long = services.tool_name("k" * 40, "t" * 40)
    assert len(long) == 64 and long != services.tool_name("k" * 40, "t" * 41)


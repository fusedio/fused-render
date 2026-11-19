"""Connected services in a specialist's chats: reads and sends are two servers, and a send runs only by spending
the one-time grant recorded when the user allowed its card."""
from _specialists_conftest import *  # noqa: F401,F403
import json
from types import SimpleNamespace

import pytest

from fused_render.bots import apptools
from fused_render.specialists import paths, services

SPEC = SimpleNamespace(id="canvas", home="/w/specialists/canvas")


def _rec(app, name, read_only):
    return apptools.ToolRec(app=app, app_dir="connect:" + app, name=name, description="d", params=[],
                            spec=SimpleNamespace(read_only=read_only, input_schema={"type": "object"}))


@pytest.fixture
def ran(monkeypatch):
    monkeypatch.setattr(services, "_GRANTS", {})
    recs = [_rec("notion", "search", True), _rec("notion", "create_page", False)]
    monkeypatch.setattr(apptools, "registry", lambda force=False: recs)
    out = []
    monkeypatch.setattr(apptools, "run_tool", lambda rec, args, timeout_s=0: (
        out.append(rec.name) or apptools.RunResult(True, "ran " + rec.name)))
    return out


@pytest.fixture
def card(monkeypatch, tmp_path):
    """`card(tool)` parks an allowed card in a run of the specialist's home and returns (run_id, request_id)."""
    from fused_render.claude_agent import agent_module

    monkeypatch.setattr(agent_module(), "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(paths, "spec_of_path", lambda p: "canvas" if p == SPEC.home else None)

    def park(tool, n=[0]):
        n[0] += 1
        run_dir = tmp_path / "runs" / "run1"
        (run_dir / "perm").mkdir(parents=True, exist_ok=True)
        (run_dir / "meta.json").write_text(json.dumps({"file": SPEC.home}))
        (run_dir / "perm" / f"req{n[0]}.req.json").write_text(json.dumps({"tool": tool}))
        return "run1", f"req{n[0]}"
    return park


def test_reads_and_sends_are_two_servers(ran):
    reads = services.mcp_tools(SPEC, "specialist_services")
    sends = services.mcp_tools(SPEC, "specialist_sends")
    assert [t["name"] for t in reads] == ["notion_search"] and reads[0]["description"].startswith("[")
    assert [t["name"] for t in sends] == ["notion_create_page"]
    assert services.call(SPEC, "specialist_services", "notion_create_page", {})["isError"]


def test_a_read_needs_no_grant(ran):
    assert services.call(SPEC, "specialist_services", "notion_search", {}) == {
        "content": [{"type": "text", "text": "ran search"}], "isError": False}


def test_a_send_spends_one_grant(ran, card):
    out = services.call(SPEC, "specialist_sends", "notion_create_page", {})
    assert out["content"][0]["text"] == services.NEEDS_CARD and ran == []
    run, req = card("mcp__specialist_sends__notion_create_page")
    services.record_grant(run, req)
    services.record_grant(run, req)  # the same card reported twice grants once
    assert services.call(SPEC, "specialist_sends", "notion_create_page", {})["isError"] is False
    assert services.call(SPEC, "specialist_sends", "notion_create_page", {})["content"][0]["text"] == \
        services.NEEDS_CARD
    services.record_grant(run, req)  # nor after it was spent
    assert services.call(SPEC, "specialist_sends", "notion_create_page", {})["isError"]
    assert ran == ["create_page"]


def test_a_grant_is_for_its_tool_and_specialist_and_expires(ran, card, monkeypatch):
    services.record_grant(*card("mcp__specialist_services__notion_search"))  # not a send: no grant
    services.record_grant(*card("mcp__specialist_sends__notion_create_page"))
    other = SimpleNamespace(id="data", home="/w/specialists/data")
    assert services.call(other, "specialist_sends", "notion_create_page", {})["isError"]
    now = services.time.time()
    monkeypatch.setattr(services.time, "time", lambda: now + services.GRANT_TTL_S + 1)
    assert services.call(SPEC, "specialist_sends", "notion_create_page", {})["isError"]
    assert ran == []


def test_tool_names_fit_mcp():
    assert services.tool_name("app:my.app", "do it") == "app_my_app_do_it"
    long = services.tool_name("k" * 40, "t" * 40)
    assert len(long) == 64 and long != services.tool_name("k" * 40, "t" * 41)


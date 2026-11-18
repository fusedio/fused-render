"""Connected services: the roster per kind, tool names, the policy matrix (W) and calls."""
from _specialists_conftest import *  # noqa: F401,F403
from types import SimpleNamespace

import pytest

from fused_render.bots import apptools
from fused_render.specialists import services, store


def _rec(app, name, desc="", connect=False, read_only=False):
    return apptools.ToolRec(app=app, app_dir=("connect:" + app) if connect else f"/apps/{app}", name=name,
                            description=desc, params=[], spec=SimpleNamespace(read_only=read_only,
                                                                              input_schema={"type": "object"}))


@pytest.fixture
def roster(monkeypatch):
    recs = [_rec("notion", "search", connect=True, read_only=True),
            _rec("notion", "create_page", connect=True, read_only=False),
            _rec("linear", "get_issue", connect=True, read_only=True),
            _rec("weather-app", "forecast", "Get the forecast.")]
    monkeypatch.setattr(apptools, "registry", lambda force=False: recs)
    ran = []
    monkeypatch.setattr(apptools, "run_tool", lambda rec, args, timeout_s=0: (
        ran.append((rec.app, rec.name, args)) or apptools.RunResult(True, f"ran {rec.name}")))
    return ran


def test_tool_names():
    assert services.tool_name("notion", "search") == "notion_search"
    assert services.tool_name("app:my.app", "do it") == "app_my_app_do_it"
    long = services.tool_name("k" * 40, "t" * 40)
    assert len(long) == 64 and long != services.tool_name("k" * 40, "t" * 41)


def test_roster_by_kind(app_home, roster):
    s = make_specialist()
    s = store.update(s.id, connections=["notion"])
    main = {e["name"] for e in services.roster(s, "main")}
    assert main == {"notion_search", "notion_create_page", "linear_get_issue", "app_weather-app_forecast"}
    assert {e["name"] for e in services.roster(s, "nightly")} == {"notion_search"}
    assert services.roster(s, "check") == [] and services.roster(s, "reviewer") == []
    tools = {t["name"]: t for t in services.mcp_tools(s, "main")}
    assert tools["notion_create_page"]["description"].startswith("[Notion, writes]")
    assert tools["notion_search"]["description"].startswith("[Notion, read-only]")


def test_policy_matrix(app_home, roster, monkeypatch):
    s = make_specialist()
    store.update(s.id, connections=["notion", "app:weather-app"])
    P = services.TOOL_PREFIX
    pol = lambda kind, tool, sess="s1": services.policy(s.id, kind, sess, P + tool)  # noqa: E731
    # main: allowed -> allow (read or write), not allowed -> ask
    assert pol("main", "notion_search") == "allow"
    assert pol("main", "notion_create_page") == "allow"
    assert pol("main", "linear_get_issue") == "ask"
    assert pol("main", "app_weather-app_forecast") == "allow"
    # breakout: allowed read -> allow, any write -> ask_always, not allowed read -> ask
    assert pol("breakout", "notion_search") == "allow"
    assert pol("breakout", "notion_create_page") == "ask_always"
    assert pol("breakout", "linear_get_issue") == "ask"
    # unknown tool / specialist / kind fail closed to a card
    assert pol("main", "nope") == "ask" and pol("breakout", "nope") == "ask_always"
    assert services.policy("nobody", "main", "s", P + "notion_search") == "ask"
    assert pol("nightly", "notion_search") == "ask"
    # a write while a draft send is pending in this session asks
    from fused_render.specialists import drafts

    monkeypatch.setattr(drafts, "send_pending", lambda spec_id, sid: sid == "s1", raising=False)
    assert pol("main", "notion_create_page") == "ask" and pol("main", "notion_create_page", "s2") == "allow"
    assert pol("main", "notion_search") == "allow"


def test_dont_ask_again_allows(app_home, roster):
    s = make_specialist()
    P = services.TOOL_PREFIX
    assert services.policy(s.id, "main", "", P + "linear_get_issue") == "ask"
    assert services.set_allowed(s.id, add=["Linear"]) == ["linear"]
    assert services.policy(s.id, "main", "", P + "linear_get_issue") == "allow"
    assert services.set_allowed(s.id, remove=["linear"]) == []


def test_calls(app_home, roster):
    s = store.update(make_specialist().id, connections=["notion"])
    out = services.call(s, "main", "notion_create_page", {"title": "x"})
    assert out == {"content": [{"type": "text", "text": "ran create_page"}], "isError": False}
    out = services.call(s, "nightly", "notion_create_page", {})
    assert out["isError"] and "read-only" in out["content"][0]["text"]
    assert services.call(s, "nightly", "notion_search", {})["isError"] is False
    assert services.call(s, "main", "nope", {})["isError"]
    assert [r[1] for r in roster] == ["create_page", "search"]


def test_connections_listing(app_home, roster):
    s = store.update(make_specialist().id, connections=["notion", "gone-service"])
    rows = {r["key"]: r for r in services.connections(s)["services"]}
    assert rows["app:weather-app"] == {"key": "app:weather-app", "title": "weather-app", "kind": "app",
                                       "connected": True, "allowed": False}
    assert rows["gone-service"]["allowed"] is True and rows["gone-service"]["connected"] is False

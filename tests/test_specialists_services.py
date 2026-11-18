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
    # main: allowed -> allow (read or write); not allowed -> ask_always (the chat's "Always approve" cannot
    # silence it; only the specialist's own "don't ask again" can)
    assert pol("main", "notion_search") == "allow"
    assert pol("main", "notion_create_page") == "allow"
    assert pol("main", "linear_get_issue") == "ask_always"
    assert pol("main", "app_weather-app_forecast") == "allow"
    # breakout: allowed read -> allow, any write -> ask_always, not allowed -> ask_always
    assert pol("breakout", "notion_search") == "allow"
    assert pol("breakout", "notion_create_page") == "ask_always"
    assert pol("breakout", "linear_get_issue") == "ask_always"
    # unknown tool / specialist / kind fail closed to a card
    assert pol("main", "nope") == "ask_always" and pol("breakout", "nope") == "ask_always"
    assert services.policy("nobody", "main", "s", P + "notion_search") == "ask_always"
    assert pol("nightly", "notion_search") == "ask_always"
    # a write while a Review-and-send is pending in this session: a card every time
    from fused_render.specialists import drafts

    monkeypatch.setattr(drafts, "send_pending", lambda spec_id, sid: sid == "s1", raising=False)
    assert pol("main", "notion_create_page") == "ask_always" and pol("main", "notion_create_page", "s2") == "allow"
    assert pol("main", "notion_search") == "allow"


def test_dont_ask_again_allows(app_home, roster):
    s = make_specialist()
    P = services.TOOL_PREFIX
    assert services.policy(s.id, "main", "", P + "linear_get_issue") == "ask_always"
    assert services.set_allowed(s.id, add=["Linear"]) == ["linear"]
    assert services.policy(s.id, "main", "", P + "linear_get_issue") == "allow"
    assert services.set_allowed(s.id, remove=["linear"]) == []


def test_calls(app_home, roster):
    s = store.update(make_specialist().id, connections=["notion"])
    out = services.call(s, "main", "notion_create_page", {"title": "x"})  # allowed: no grant needed
    assert out == {"content": [{"type": "text", "text": "ran create_page"}], "isError": False}
    out = services.call(s, "nightly", "notion_create_page", {})
    assert out["isError"] and "read-only" in out["content"][0]["text"]
    assert services.call(s, "nightly", "notion_search", {})["isError"] is False
    assert services.call(s, "main", "nope", {})["isError"]
    assert [r[1] for r in roster] == ["create_page", "search"]


def test_a_call_that_needs_a_card_runs_only_with_a_grant(app_home, roster, monkeypatch):
    from fused_render.specialists import breakouts, launch

    monkeypatch.setattr(breakouts, "get", lambda sid, bid: {"status": "open", "parent": {}}, raising=False)

    s = store.update(make_specialist().id, connections=["notion"])
    sp = launch.spec(f"{s.id}:main", s.home)
    secret = sp["policy_env"]["FUSED_SPECIALIST_GRANT"]
    assert secret and launch.check_token(s.id, "main", sp["token"])["grant"] == secret
    # linear is not allowed: ask_always -> no grant, nothing runs
    out = services.call(s, "main", "linear_get_issue", {}, grant_secret=secret)
    assert out == {"content": [{"type": "text", "text": services.NEEDS_CARD}], "isError": True} and roster == []
    assert not launch.record_grant(s.id, "main", "sess", "mcp__specialist_services__linear_get_issue", "wrong")
    assert not launch.record_grant(s.id, "breakout", "sess", "mcp__specialist_services__linear_get_issue", secret)
    assert launch.record_grant(s.id, "main", "sess", "mcp__specialist_services__linear_get_issue", secret)
    # a grant for another tool or another run's secret does not count
    assert services.call(s, "main", "notion_search", {}, grant_secret="other")["isError"] is False  # allowed
    assert services.call(s, "main", "linear_get_issue", {}, grant_secret="other")["content"][0]["text"] == \
        services.NEEDS_CARD
    out = services.call(s, "main", "linear_get_issue", {}, grant_secret=secret)
    assert out["isError"] is False and roster[-1][1] == "get_issue"
    # one-time
    assert services.call(s, "main", "linear_get_issue", {}, grant_secret=secret)["content"][0]["text"] == \
        services.NEEDS_CARD
    # breakout write of an allowed service still needs its grant
    bsp = launch.spec(f"{s.id}:breakout:b-1", s.home)
    bsecret = bsp["policy_env"]["FUSED_SPECIALIST_GRANT"]
    assert services.call(s, "breakout", "notion_create_page", {}, grant_secret=bsecret)["isError"]
    launch.record_grant(s.id, "breakout", "", "notion_create_page", bsecret)
    assert services.call(s, "breakout", "notion_create_page", {}, grant_secret=bsecret)["isError"] is False


def test_grants_expire(app_home, roster, monkeypatch):
    from fused_render.specialists import launch

    s = make_specialist()
    secret = launch.spec(f"{s.id}:main", s.home)["policy_env"]["FUSED_SPECIALIST_GRANT"]
    assert launch.record_grant(s.id, "main", "", "linear_get_issue", secret)
    real = launch.time.time()
    monkeypatch.setattr(launch.time, "time", lambda: real + launch.GRANT_TTL_S + 1)
    assert not launch.take_grant(s.id, secret, "linear_get_issue")


def test_connections_tools_match_the_session_names(app_home, roster):
    s = store.update(make_specialist().id, connections=["notion"])
    rows = {r["key"]: r for r in services.connections(s)["services"]}
    listed = {t["name"]: t["write"] for r in rows.values() for t in r["tools"]}
    session = {services.TOOL_PREFIX + t["name"] for t in services.mcp_tools(s, "main")}
    assert set(listed) == session
    assert listed[services.TOOL_PREFIX + "notion_create_page"] is True
    assert listed[services.TOOL_PREFIX + "notion_search"] is False
    assert rows["app:weather-app"]["tools"] == [{"name": services.TOOL_PREFIX + "app_weather-app_forecast",
                                                 "write": False}]


def test_connections_listing(app_home, roster):
    s = store.update(make_specialist().id, connections=["notion", "gone-service"])
    rows = {r["key"]: r for r in services.connections(s)["services"]}
    assert {k: v for k, v in rows["app:weather-app"].items() if k != "tools"} == {
        "key": "app:weather-app", "title": "weather-app", "kind": "app", "connected": True, "allowed": False}
    assert rows["gone-service"]["allowed"] is True and rows["gone-service"]["connected"] is False
    assert rows["gone-service"]["tools"] == []

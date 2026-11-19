"""Scenario A (backend): with the flag off no route answers, no folder or thread appears, and the seams are inert.
Also the pref (`specialists_enabled`) and its env override."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from fused_render.specialists import flag, paths


def _call(client, method, path):
    body = b"{}" if method in ("POST", "PUT", "PATCH") else None
    r = urllib.request.Request(client.base + path, data=body, method=method,
                               headers={"Content-Type": "application/json", "X-Fused": "1"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _specialist_routes():
    from fused_render.server.app import create_app  # noqa: F401 — the routers it includes

    import importlib

    out = []
    for mod in ("routes", "tools"):
        for route in importlib.import_module(f"fused_render.specialists.{mod}").router.routes:
            for m in sorted(getattr(route, "methods", None) or ()):
                if m in ("HEAD", "OPTIONS"):
                    continue
                out.append((m, route.path.replace("{spec_id}", "abc").replace("{id}", "abc")
                            .replace("{path:path}", "x").replace("{", "").replace("}", "")))
    return out


def test_flag_env_and_pref(app_home, monkeypatch):
    from fused_render.shell import prefs

    assert flag.on()
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    prefs.write_pref("specialists_enabled", True)
    assert not flag.on() and not prefs.specialists_enabled()
    monkeypatch.delenv("FUSED_RENDER_SPECIALISTS")
    assert flag.on() and prefs.specialists_enabled()
    prefs.write_pref("specialists_enabled", "yes")  # only a stored true is on
    assert not flag.on() and not prefs.specialists_enabled()
    with pytest.raises(flag.Off):
        flag.require()


def test_flag_off_every_route_is_404_and_nothing_is_created(client, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    routes = _specialist_routes()
    assert ("GET", "/api/specialists") in routes and ("POST", "/api/specialists") in routes
    threads_before = {t.name for t in threading.enumerate()}
    for method, path in routes:
        st, body = _call(client, method, path)
        assert st == 404, (method, path, st, body)
        assert json.loads(body) == {"detail": "Not Found"}, (method, path, body)
    # a malformed body is still the unknown-path 404, not a 422
    r = urllib.request.Request(client.base + "/api/specialists", data=b"{not json", method="POST",
                               headers={"Content-Type": "application/json", "X-Fused": "1"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r)
    assert e.value.code == 404 and json.loads(e.value.read()) == {"detail": "Not Found"}
    unknown = _call(client, "POST", "/api/no-such-route-at-all")
    assert unknown[0] == 404 and json.loads(unknown[1]) == {"detail": "Not Found"}
    assert not os.path.exists(paths.root())
    new = {t.name for t in threading.enumerate()} - threads_before
    assert not any("special" in n.lower() for n in new), new
    # page routes answer as an unknown path does
    for p in ("/specialists", "/specialists/abc"):
        st, body = _call(client, "GET", p)
        assert st == 404 and json.loads(body) == json.loads(_call(client, "GET", "/no-such-page-at-all")[1])


def test_prefs_get_and_put(client, monkeypatch):
    from fused_render.specialists import scheduler, seed

    calls = []
    monkeypatch.setattr(seed, "seed", lambda: calls.append("seed") or [])
    monkeypatch.setattr(scheduler, "start", lambda: calls.append("start"))
    monkeypatch.setattr(scheduler, "stop", lambda budget_s=2.0: calls.append("stop"))
    monkeypatch.delenv("FUSED_RENDER_SPECIALISTS")
    st, body = _call(client, "GET", "/api/prefs")
    assert st == 200 and json.loads(body)["specialists"] == {"enabled": False}
    r = urllib.request.Request(client.base + "/api/prefs", data=json.dumps({"specialists_enabled": True}).encode(),
                               method="PUT", headers={"Content-Type": "application/json", "X-Fused": "1"})
    with urllib.request.urlopen(r) as resp:
        assert json.loads(resp.read())["specialists"] == {"enabled": True}
    assert calls == ["seed", "start"]
    r = urllib.request.Request(client.base + "/api/prefs", data=json.dumps({"specialists_enabled": False}).encode(),
                               method="PUT", headers={"Content-Type": "application/json", "X-Fused": "1"})
    with urllib.request.urlopen(r) as resp:
        assert json.loads(resp.read())["specialists"] == {"enabled": False}
    assert calls[-1] == "stop"
    r = urllib.request.Request(client.base + "/api/prefs", data=json.dumps({"specialists_enabled": 1}).encode(),
                               method="PUT", headers={"Content-Type": "application/json", "X-Fused": "1"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r)
    assert e.value.code == 400


def test_router_post_hook_only_for_a_specialist_start(monkeypatch):
    import sys
    import types

    from fused_render.server.routers import claude_agent

    monkeypatch.delitem(sys.modules, "fused_render.specialists.agent_hooks", raising=False)
    claude_agent._specialist_after({"action": "start", "specialist": "a:main"}, {})  # not loaded: nothing
    seen = []
    fake = types.ModuleType("fused_render.specialists.agent_hooks")
    fake.after_agent_call = lambda action, params, env: seen.append(action)
    monkeypatch.setitem(sys.modules, "fused_render.specialists.agent_hooks", fake)
    claude_agent._specialist_after({"action": "poll"}, {})
    claude_agent._specialist_after({"action": "start"}, {"result": {}})
    assert seen == []
    claude_agent._specialist_after({"action": "start", "specialist": "a:main"}, {"result": {}})
    assert seen == ["start"]

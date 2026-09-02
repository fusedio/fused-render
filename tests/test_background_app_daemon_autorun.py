"""`fused.daemon.run(params)` and `fused.daemon.call(path, body)` (the
`main =`/`daemon =` convenience methods proxied through
`_daemonProxyPost`, SKILL.md, ENGINE_HOST_DESIGN.md) must both transparently
bring the daemon up rather than reject when it isn't known to be running yet
— a page's first-ever call (nothing has started it yet), or an app the idle
reaper retired since the last `status()`/`watch()` poll. Each also refuses
the OTHER method's folder shape: `run()` against a `daemon =` folder would
otherwise POST /call to a server that doesn't serve it, and `call()` against
a `main =` folder would otherwise "work" but hand back the raw envelope
instead of the unwrapped result — both silently wrong instead of naming the
folder's actual declared protocol and the method to use instead.

Same node-harness style as test_background_app_daemon_guard.py: the named
functions are lifted out of runtime.js by source text and driven under node
with their closure (`window`, `location`, `fetch`, `ownQuery`, `callHeaders`)
stubbed.
"""
import json
import os
import shutil
import subprocess

import pytest

_RUNTIME = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "static", "runtime.js")

_FLAG_START = '  var PREVIEW_PARAM = "_preview";'
_FLAG_END = "  var IS_THUMBNAIL = selfOrAncestorHasFlag(PREVIEW_PARAM);"
_DAEMON_START = "  let _daemonEngineId = null;"
_DAEMON_END = "    watch: daemonWatch,\n  };"


def _slice(source, start_marker, end_marker):
    start = source.index(start_marker)
    end = source.index(end_marker, start) + len(end_marker)
    return source[start:end]


def _guard_source():
    with open(_RUNTIME, encoding="utf-8") as f:
        source = f.read()
    flags = _slice(source, _FLAG_START, _FLAG_END)
    daemon = _slice(source, _DAEMON_START, _DAEMON_END)
    return flags + "\n" + daemon


def _run(method, args_js, responses):
    """Call `fused.daemon.<method>(args_js)` under node, routing `fetch` by
    matching each request URL against a substring key in `responses` (each
    value a dict with `ok`/`json`). Returns
    {ok, value_or_message, calls: [urls in order]}.
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run runtime.js's daemon run() harness")
    guard = _guard_source()
    routes_js = ",\n".join(
        f'[{json.dumps(key)}, {{ok: {str(resp["ok"]).lower()}, '
        f'json: {json.dumps(resp["json"])}}}]'
        for key, resp in responses.items()
    )
    prelude = f"""
      const location = {{search: "?path=/apps/x/index.html"}};
      const window = {{location}};
      window.parent = window;
      function ownQuery(key) {{
        try {{ return new URLSearchParams(window.location.search).get(key); }}
        catch (e) {{ return null; }}
      }}
      function callHeaders(extra) {{ return Object.assign({{}}, extra || {{}}); }}
      const routes = new Map([{routes_js}]);
      let calls = [];
      globalThis.fetch = (url, opts) => {{
        calls.push(String(url));
        let match = null;
        for (const [key, resp] of routes) {{
          if (String(url).includes(key)) {{ match = resp; break; }}
        }}
        if (!match) return Promise.reject(new Error("no route for " + url));
        return Promise.resolve({{
          ok: match.ok,
          json: () => Promise.resolve(match.json),
        }});
      }};
    """
    call = f"""
      fused_daemon_{method}({args_js}).then(
        (value) => console.log(JSON.stringify({{ok: true, value, calls}})),
        (err) => console.log(JSON.stringify({{ok: false, message: err.message, calls}})),
      );
    """
    alias = f"const fused_daemon_{method} = daemon{'.' + method};"
    out = subprocess.run(["node", "-e", prelude + guard + "\n" + alias + call],
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_run_starts_the_daemon_on_the_very_first_call_instead_of_rejecting():
    # No page has called status()/start() yet in this session, and the app
    # has never been started server-side either (status reports not running).
    result = _run("run", "{}", {
        "/api/apps/background/status": {
            "ok": True,
            "json": {"engine_id": "e1", "running": False},
        },
        "/api/apps/background/start": {
            "ok": True,
            "json": {"engine_id": "e1", "pid": 123, "version": "v1"},
        },
        "/api/engines/e1/proxy/call": {
            "ok": True,
            "json": {"ok": True, "result": {"x": 1}},
        },
    })
    assert result["ok"] is True
    assert result["value"] == {"x": 1}
    # It actually brought the daemon up rather than merely trusting a stale cache.
    assert any("/api/apps/background/start" in u for u in result["calls"])
    assert any("/api/engines/e1/proxy/call" in u for u in result["calls"])


def test_run_skips_the_extra_start_round_trip_once_known_running():
    # status() already reports it running (e.g. after an earlier run()/start()
    # this session) — run() must not pay an extra start() round trip every call.
    result = _run("run", "{}", {
        "/api/apps/background/status": {
            "ok": True,
            "json": {"engine_id": "e1", "running": True},
        },
        "/api/engines/e1/proxy/call": {
            "ok": True,
            "json": {"ok": True, "result": {"x": 2}},
        },
    })
    assert result["ok"] is True
    assert result["value"] == {"x": 2}
    assert not any("/api/apps/background/start" in u for u in result["calls"])


def test_call_starts_the_daemon_on_the_very_first_call_instead_of_rejecting():
    # call() used to require an explicit start() first and reject with "call
    # start() first" — the preview guard (D507/D508), not a start-first gate,
    # is the actual safety boundary, and engine_forward already heals a
    # dead-but-running child on any proxied call regardless.
    result = _run("call", '"/count", {}', {
        "/api/apps/background/status": {
            "ok": True,
            "json": {"engine_id": "e1", "running": False, "protocol": "daemon"},
        },
        "/api/apps/background/start": {
            "ok": True,
            "json": {"engine_id": "e1", "pid": 123, "version": "v1"},
        },
        "/api/engines/e1/proxy/count": {
            "ok": True,
            "json": {"n": 1},
        },
    })
    assert result["ok"] is True
    assert result["value"] == {"n": 1}
    assert any("/api/apps/background/start" in u for u in result["calls"])
    assert any("/api/engines/e1/proxy/count" in u for u in result["calls"])


def test_call_skips_the_extra_start_round_trip_once_known_running():
    result = _run("call", '"/count", {}', {
        "/api/apps/background/status": {
            "ok": True,
            "json": {"engine_id": "e1", "running": True, "protocol": "daemon"},
        },
        "/api/engines/e1/proxy/count": {
            "ok": True,
            "json": {"n": 2},
        },
    })
    assert result["ok"] is True
    assert result["value"] == {"n": 2}
    assert not any("/api/apps/background/start" in u for u in result["calls"])


def test_run_rejects_a_daemon_folder_naming_call_instead():
    result = _run("run", "{}", {
        "/api/apps/background/status": {
            "ok": True,
            "json": {"engine_id": "e1", "running": True, "protocol": "daemon"},
        },
    })
    assert result["ok"] is False
    assert "fused.daemon.run" in result["message"]
    assert "daemon" in result["message"]
    assert "fused.daemon.call" in result["message"]
    # Refused before ever touching the proxy path.
    assert not any("/api/engines/" in u for u in result["calls"])


def test_call_rejects_a_main_folder_naming_run_instead():
    result = _run("call", '"/call", {}', {
        "/api/apps/background/status": {
            "ok": True,
            "json": {"engine_id": "e1", "running": True, "protocol": "main"},
        },
    })
    assert result["ok"] is False
    assert "fused.daemon.call" in result["message"]
    assert "main" in result["message"]
    assert "fused.daemon.run" in result["message"]
    assert not any("/api/engines/" in u for u in result["calls"])


def test_run_and_call_are_unaffected_by_a_null_protocol():
    # A folder with no valid manifest at all reports protocol: null — neither
    # method should add a new rejection on top of whatever error path already
    # covers that folder (the proxy 404 speaks for itself).
    run_result = _run("run", "{}", {
        "/api/apps/background/status": {
            "ok": True,
            "json": {"engine_id": "e1", "running": True, "protocol": None},
        },
        "/api/engines/e1/proxy/call": {
            "ok": True,
            "json": {"ok": True, "result": {"x": 3}},
        },
    })
    assert run_result["ok"] is True
    assert run_result["value"] == {"x": 3}

    call_result = _run("call", '"/whatever", {}', {
        "/api/apps/background/status": {
            "ok": True,
            "json": {"engine_id": "e1", "running": True, "protocol": None},
        },
        "/api/engines/e1/proxy/whatever": {
            "ok": True,
            "json": {"y": 4},
        },
    })
    assert call_result["ok"] is True
    assert call_result["value"] == {"y": 4}

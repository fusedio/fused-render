"""`fused.daemon`'s preview guard (D507, SPEC.md §46, skills/fused-render-
background-apps/SKILL.md): a page must not start a background daemon merely
by being rendered as a card thumbnail or hover peek.

`AppPreviewCard.tsx` mounts `entry_html` live in a sandboxed iframe
(`allow-scripts allow-same-origin`), and `thumbFrame`/`withPreviewFlag`
(frontend/src/platform/lib/thumb-frame.ts, router.ts) stamp `_preview=1`
straight onto the `/render?path=...` URL that becomes that iframe's own
`src`. `fused_render/server/routers/render.py` serves the app's HTML at
exactly that URL with no redirect, so the flag lands in the rendered
document's OWN `location.search` — the same fact `runtime.js` already
computes for the focus contract (`IS_THUMBNAIL`, mirroring
`router.ancestorIsPreview`/`IS_PREVIEW`). `enable()`/`restart()` guard on it
directly; `call()` does too, because `engine_forward.py`'s `_forward` heals a
dead-but-enabled child back to life on ANY proxied call — a preview render
that calls `call()` against an app some other session already enabled can
resurrect its daemon exactly like `enable()` would. `stop()` and `disable()`
are gated the same way: a card thumbnail mounts `entry_html` live with
`allow-scripts`, so an app whose init path calls `fused.daemon.disable()`
could un-persist a running daemon just because its card scrolled past or was
hovered — worse than the enable bug this guard exists for, because
`disable()` survives a server restart. `status()` is the one method
deliberately left open: it is read-only (and the pattern the rejection
message points authors at).

Same node-harness style as the `aiTranscribe`/`aiImage` suites in
test_ai_runtime.py: named functions are lifted out of runtime.js by their
source text and driven under node with their closure (`window`, `location`,
`fetch`, `ownQuery`, `callHeaders`) stubbed, because what matters is the
decision reached, not the DOM it reached it in.
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
_DAEMON_END = "    call: daemonCall,\n  };"


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


def _run_daemon(method, args_js="", search="?path=/apps/x/index.html",
                ancestor_search=None, fetch_json='{"engine_id": "e1", "running": true}',
                fetch_ok=True):
    """Call `fused.daemon.<method>(args_js)` under node with `location.search`
    set to `search` (the frame's own URL) and, if given, one same-origin
    ancestor whose `location.search` is `ancestor_search` (the nested-preview
    case `selfOrAncestorHasFlag`/`IS_PREVIEW` climbs for). Returns
    {ok, message, fetchCount, urls}.
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run runtime.js's daemon guard")
    guard = _guard_source()
    ancestor_js = (
        f'const ancestorWindow = {{location: {{search: {json.dumps(ancestor_search)}}}}}; '
        f'ancestorWindow.parent = ancestorWindow;'
        if ancestor_search is not None else
        "const ancestorWindow = null;"
    )
    prelude = f"""
      {ancestor_js}
      const location = {{search: {json.dumps(search)}}};
      const window = {{
        location,
        parent: ancestorWindow ? ancestorWindow : undefined,
      }};
      if (!window.parent) window.parent = window;
      function ownQuery(key) {{
        try {{ return new URLSearchParams(window.location.search).get(key); }}
        catch (e) {{ return null; }}
      }}
      function callHeaders(extra) {{ return Object.assign({{}}, extra || {{}}); }}
      let fetchCalls = [];
      globalThis.fetch = (url, opts) => {{
        fetchCalls.push(String(url));
        return Promise.resolve({{
          ok: {str(fetch_ok).lower()},
          json: () => Promise.resolve({fetch_json}),
        }});
      }};
    """
    call = f"""
      fused_daemon_{method}({args_js}).then(
        (value) => console.log(JSON.stringify(
          {{ok: true, value, fetchCount: fetchCalls.length, urls: fetchCalls}})),
        (err) => console.log(JSON.stringify(
          {{ok: false, message: err.message, fetchCount: fetchCalls.length, urls: fetchCalls}})),
      );
    """
    # The lifted source defines daemonEnable/daemonRestart/daemonCall/etc as
    # plain function declarations — alias the one under test to a name this
    # harness can call without pulling in the whole `daemon` object literal
    # (which is fine to also define, just unused).
    alias = f"const fused_daemon_{method} = daemon{'.' + method};"
    out = subprocess.run(["node", "-e", prelude + guard + "\n" + alias + call],
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_enable_is_refused_when_this_frame_is_a_preview_thumbnail():
    result = _run_daemon("enable", search="?path=/apps/x/index.html&_preview=1")
    assert result["ok"] is False
    assert "fused.daemon.enable" in result["message"]
    assert "refused" in result["message"]
    assert "preview" in result["message"].lower()
    # The whole point: no request ever leaves the page.
    assert result["fetchCount"] == 0


def test_restart_is_refused_when_this_frame_is_a_preview_thumbnail():
    result = _run_daemon("restart", search="?path=/apps/x/index.html&_preview=1")
    assert result["ok"] is False
    assert "fused.daemon.restart" in result["message"]
    assert result["fetchCount"] == 0


def test_call_is_refused_when_this_frame_is_a_preview_thumbnail():
    result = _run_daemon("call", args_js='"do_thing", {}',
                         search="?path=/apps/x/index.html&_preview=1")
    assert result["ok"] is False
    assert "fused.daemon.call" in result["message"]
    assert result["fetchCount"] == 0


def test_enable_is_refused_when_an_ancestor_frame_is_the_preview():
    """The nested case: a card iframes a shell page that itself iframes this
    app (IS_THUMBNAIL/selfOrAncestorHasFlag's whole reason to climb) — this
    frame's own URL carries no `_preview`, only its parent's does."""
    result = _run_daemon(
        "enable",
        search="?path=/apps/x/index.html",
        ancestor_search="?path=/explorer/embed/apps/x&_preview=1",
    )
    assert result["ok"] is False
    assert result["fetchCount"] == 0


def test_enable_still_works_outside_preview():
    """The guard must not break the legitimate path: a normal (non-preview)
    render can still enable."""
    result = _run_daemon("enable", search="?path=/apps/x/index.html")
    assert result["ok"] is True
    assert result["fetchCount"] == 1
    assert "/api/apps/background/enable" in result["urls"][0]


def test_restart_still_works_outside_preview():
    result = _run_daemon("restart", search="?path=/apps/x/index.html")
    assert result["ok"] is True
    assert result["fetchCount"] == 1


def test_stop_is_refused_when_this_frame_is_a_preview_thumbnail():
    """`stop()` only turns a daemon OFF, but a card thumbnail mounts
    `entry_html` live with `allow-scripts` — an app whose init path calls
    `fused.daemon.stop()` must not be able to kill a real user's daemon just
    because their card scrolled past. Gate it exactly like `enable()`."""
    result = _run_daemon("stop", search="?path=/apps/x/index.html&_preview=1")
    assert result["ok"] is False
    assert "fused.daemon.stop" in result["message"]
    assert "refused" in result["message"]
    assert result["fetchCount"] == 0


def test_disable_is_refused_when_this_frame_is_a_preview_thumbnail():
    """`disable()` is worse than `enable()` if left open: it un-persists a
    running daemon, and that survives a server restart — a preview must not
    be able to reach it either."""
    result = _run_daemon("disable", search="?path=/apps/x/index.html&_preview=1")
    assert result["ok"] is False
    assert "fused.daemon.disable" in result["message"]
    assert "refused" in result["message"]
    assert result["fetchCount"] == 0


def test_stop_still_works_outside_preview():
    result = _run_daemon("stop", search="?path=/apps/x/index.html")
    assert result["ok"] is True
    assert result["fetchCount"] == 1
    assert "/api/apps/background/stop" in result["urls"][0]


def test_disable_still_works_outside_preview():
    result = _run_daemon("disable", search="?path=/apps/x/index.html")
    assert result["ok"] is True
    assert result["fetchCount"] == 1
    assert "/api/apps/background/disable" in result["urls"][0]


def test_status_is_never_gated_even_in_preview():
    result = _run_daemon("status", search="?path=/apps/x/index.html&_preview=1")
    assert result["ok"] is True
    assert result["fetchCount"] == 1
    assert "/api/apps/background/status" in result["urls"][0]

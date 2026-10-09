"""`fused.daemon.watch()` (fused_render/static/runtime.js, SPEC.md §46): the
page-side watcher a background app's own page uses to follow its daemon's
status. Since the events bus it is one `apps.background` subscription behind
the same signature (D15): the callback hears the first frame and every CHANGE
to the watched fields, and the unsubscribe releases the subscription. The
hidden-window policy is the events client's (D7, tested in bun), not this
function's, so there is no timer and no visibility listener here any more.

The functions are lifted out of runtime.js by source text and driven under
node with `document`/`window`/`fetch` stubbed and `subscribeTopic` faked (a
scripted subscription whose frames the test pushes), because what matters is
the observable contract of `watch()` against the bus, not the DOM.
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


# One JS "test rig": fakes document/window/fetch/setInterval, runs a supplied
# JS `body` (which may call fused_daemon_watch, dispatch visibility/focus
# events, and tick the fake timer), and prints whatever it JSON.stringifies
# to `RESULT` at the end.
_HARNESS_PRELUDE = """
  const location = {search: SEARCH};
  const window = {location, parent: undefined};
  window.parent = window;
  function ownQuery(key) {
    try { return new URLSearchParams(window.location.search).get(key); }
    catch (e) { return null; }
  }
  function callHeaders(extra) { return Object.assign({}, extra || {}); }

  // fetch (the thumbnail's one status() read): each call returns the next
  // entry of STATUSES (JSON), sticking on the last one once exhausted.
  let _statusIdx = 0;
  let fetchCalls = [];
  globalThis.fetch = (url, opts) => {
    fetchCalls.push(String(url));
    const body = STATUSES[Math.min(_statusIdx, STATUSES.length - 1)];
    _statusIdx++;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  };

  // The events-bus client, faked: `subscribeTopic` records the subscription
  // and delivers STATUSES[0] as the snapshot at once; `push()` hands the next
  // scripted status over as a fresh snapshot, the way the server would after
  // a change. `subscriptions` counts what is open.
  const subs = [];
  function subscribeTopic(topic, params, cb, opts) {
    const entry = { topic, params, cb, open: true };
    subs.push(entry);
    cb(STATUSES[Math.min(_statusIdx++, STATUSES.length - 1)], null, { gen: null });
    return () => { entry.open = false; };
  }
  function push() {
    const body = STATUSES[Math.min(_statusIdx++, STATUSES.length - 1)];
    subs.filter((s) => s.open).forEach((s) => s.cb(body, null, { gen: null }));
  }
  function subscriptions() { return subs.filter((s) => s.open).length; }

  const document = { visibilityState: "visible", addEventListener() {}, removeEventListener() {} };
  window.addEventListener = () => {};
  window.removeEventListener = () => {};

  const calls = [];
"""


def _run(body, search="?path=/apps/x/index.html", statuses=None):
    if not shutil.which("node"):
        pytest.skip("node is needed to run runtime.js's watch() harness")
    guard = _guard_source()
    statuses = statuses or [{"running": True, "autostart": False, "pid": 1,
                             "version": "v1", "engine_id": "e1"}]
    prelude = (
        f"const SEARCH = {json.dumps(search)};\n"
        f"const STATUSES = {json.dumps(statuses)};\n"
        + _HARNESS_PRELUDE
    )
    script = prelude + guard + "\n" + body + """
      // Drain the microtask queue (real Promise chains from daemonStatus())
      // before reporting, without any real timer delay.
      setTimeout(() => {
        setTimeout(() => {
          console.log(JSON.stringify({
            calls, fetchCount: fetchCalls.length, subscriptions: subscriptions(),
            topics: subs.map((s) => s.topic), params: subs.map((s) => s.params),
          }));
        }, 0);
      }, 0);
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_watch_calls_back_once_on_the_first_frame():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
    """)
    # One `apps.background` subscription for this page's folder, no fetch.
    assert result["fetchCount"] == 0
    assert result["subscriptions"] == 1
    assert result["topics"] == ["apps.background"]
    assert result["params"][0]["html"] == "%2Fapps%2Fx%2Findex.html"
    assert len(result["calls"]) == 1
    assert result["calls"][0]["running"] is True


def test_watch_does_not_fire_again_when_a_frame_reports_the_same_state():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
      push();
    """, statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
    ])
    assert len(result["calls"]) == 1


def test_watch_fires_when_a_watched_field_moves():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
      push();
      push();
    """, statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": True, "autostart": True, "pid": 1, "version": "v1"},
        {"running": False, "autostart": True, "pid": 0, "version": ""},
    ])
    assert [c["autostart"] for c in result["calls"]] == [False, True, True]
    assert result["calls"][-1]["running"] is False


def test_watch_unsubscribe_releases_the_subscription():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
      unsub();
      push();
    """, statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": False, "autostart": False, "pid": 0, "version": ""},
    ])
    # Only the first frame — nothing after unsub(), and nothing left open.
    assert len(result["calls"]) == 1
    assert result["subscriptions"] == 0


def test_watch_in_a_preview_thumbnail_does_a_single_read_with_no_subscription():
    """Preview guard: watch() is status() underneath, and status() is the one
    fused.daemon method a thumbnail may call — so watch() must not reject —
    but it must not leave a subscription running in a sandboxed preview iframe
    either. One read, no subscription, unsubscribe is a no-op."""
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
      unsub();
    """, search="?path=/apps/x/index.html&_preview=1", statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": False, "autostart": False, "pid": 0, "version": ""},
    ])
    assert result["fetchCount"] == 1
    assert len(result["calls"]) == 1
    assert result["subscriptions"] == 0


def test_watch_rejects_a_non_function_callback():
    if not shutil.which("node"):
        pytest.skip("node is needed to run runtime.js's watch() harness")
    guard = _guard_source()
    script = (
        f'const SEARCH = "?path=/apps/x/index.html";\n'
        f'const STATUSES = [{{"running": true}}];\n'
        + _HARNESS_PRELUDE
        + guard
        + """
      let threw = null;
      try { daemon.watch(null); } catch (e) { threw = e.message; }
      console.log(JSON.stringify({ threw }));
    """
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result["threw"] and "callback" in result["threw"]

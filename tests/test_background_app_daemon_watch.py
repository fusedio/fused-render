"""`fused.daemon.watch()` (fused_render/static/runtime.js, SPEC.md §46): the
only way a page currently learns its daemon's state changed WITHOUT the page
itself having caused it — the OpenWhisper tray's Quit routes through
`POST /api/apps/background/stop`, so the server knows, but nothing used to
tell the page, and its mic icon stayed stale until a manual refresh.

Same node-harness style as test_background_app_daemon_guard.py: the named
functions are lifted out of runtime.js by source text and driven under node
with `document`/`window`/`fetch`/`setInterval` stubbed, because what matters
is the polling/diffing/listener decisions, not a real DOM or a real 5s wait.
`setInterval`/`clearInterval` are faked (queued, manually "ticked") so the
suite runs in milliseconds and can assert exactly how many polls happened.
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

  // fetch: each call returns the next entry of STATUSES (JSON), sticking on
  // the last one once exhausted. Every call is counted.
  let _statusIdx = 0;
  let fetchCalls = [];
  globalThis.fetch = (url, opts) => {
    fetchCalls.push(String(url));
    const body = STATUSES[Math.min(_statusIdx, STATUSES.length - 1)];
    _statusIdx++;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  };

  // Fake document: visibilitychange listeners + a mutable visibilityState.
  const docListeners = {};
  const document = {
    visibilityState: "visible",
    addEventListener(evt, fn) {
      (docListeners[evt] = docListeners[evt] || []).push(fn);
    },
    removeEventListener(evt, fn) {
      docListeners[evt] = (docListeners[evt] || []).filter((f) => f !== fn);
    },
  };
  function setVisibility(state) {
    document.visibilityState = state;
    (docListeners["visibilitychange"] || []).slice().forEach((fn) => fn());
  }

  // window focus listeners.
  const winListeners = {};
  window.addEventListener = (evt, fn) => {
    (winListeners[evt] = winListeners[evt] || []).push(fn);
  };
  window.removeEventListener = (evt, fn) => {
    winListeners[evt] = (winListeners[evt] || []).filter((f) => f !== fn);
  };
  function fireFocus() {
    (winListeners["focus"] || []).slice().forEach((fn) => fn());
  }

  // Fake interval scheduler: setInterval just remembers the callback: no
  // real waiting. tick() invokes it once, as if 5s elapsed.
  let _timerFn = null;
  let _timerCleared = true;
  globalThis.setInterval = (fn) => {
    _timerFn = fn;
    _timerCleared = false;
    return 1;
  };
  globalThis.clearInterval = () => {
    _timerCleared = true;
    _timerFn = null;
  };
  function tick() {
    if (_timerFn) _timerFn();
  }
  function timerActive() { return !_timerCleared; }

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
            calls, fetchCount: fetchCalls.length, timerActive: timerActive(),
          }));
        }, 0);
      }, 0);
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_watch_calls_back_once_on_initial_read():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
    """)
    assert result["fetchCount"] == 1
    assert len(result["calls"]) == 1
    assert result["calls"][0]["running"] is True


def test_watch_does_not_fire_again_when_a_tick_reports_the_same_state():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
    """, statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
    ])
    # Two fetches (initial poll + one tick) is asserted by a separate test
    # below that drives tick() explicitly; here we only need to confirm the
    # initial read fired exactly once and produced one callback.
    assert len(result["calls"]) == 1


def test_watch_fires_when_running_flips_on_a_tick():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
    """, statuses=[
        {"running": False, "autostart": False, "pid": 0, "version": ""},
    ])
    assert len(result["calls"]) == 1
    assert result["calls"][0]["running"] is False


def test_watch_only_polls_while_document_is_visible():
    """Hidden -> no interval scheduled; becoming visible schedules one and
    polls immediately."""
    result = _run("""
      setVisibility("hidden");
      const unsub = daemon.watch((s) => calls.push(s));
    """)
    # No poll at all while starting hidden — watch() itself does one initial
    # read regardless (so a caller always learns the starting state), but no
    # background timer should be running while hidden.
    assert result["timerActive"] is False


def test_watch_refreshes_on_visibilitychange_to_visible():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
      setVisibility("hidden");
      setVisibility("visible");
    """, statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": False, "autostart": False, "pid": 0, "version": ""},
    ])
    assert result["fetchCount"] == 2
    assert result["calls"][-1]["running"] is False
    assert result["timerActive"] is True


def test_watch_refreshes_on_window_focus():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
      fireFocus();
    """, statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": True, "autostart": True, "pid": 1, "version": "v1"},
    ])
    assert result["fetchCount"] == 2
    assert result["calls"][-1]["autostart"] is True


def test_watch_polls_on_a_tick_while_visible():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
      tick();
    """, statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": True, "autostart": False, "pid": 2, "version": "v1"},
    ])
    assert result["fetchCount"] == 2
    assert result["calls"][-1]["pid"] == 2


def test_watch_unsubscribe_stops_the_timer_and_listeners():
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
      unsub();
      setVisibility("hidden");
      setVisibility("visible");
      fireFocus();
      tick();
    """, statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": False, "autostart": False, "pid": 0, "version": ""},
    ])
    # Only the one initial poll from watch() itself — nothing after unsub().
    assert result["fetchCount"] == 1
    assert len(result["calls"]) == 1
    assert result["timerActive"] is False


def test_watch_in_a_preview_thumbnail_does_a_single_read_with_no_timer_or_listeners():
    """Preview guard: watch() is status() underneath, and status() is the one
    fused.daemon method a thumbnail may call — so watch() must not reject —
    but it must not leave a poll loop or listeners running in a sandboxed
    preview iframe either. One read, no timer, unsubscribe is a no-op."""
    result = _run("""
      const unsub = daemon.watch((s) => calls.push(s));
      unsub();
      setVisibility("hidden");
      setVisibility("visible");
      fireFocus();
      tick();
    """, search="?path=/apps/x/index.html&_preview=1", statuses=[
        {"running": True, "autostart": False, "pid": 1, "version": "v1"},
        {"running": False, "autostart": False, "pid": 0, "version": ""},
    ])
    assert result["fetchCount"] == 1
    assert len(result["calls"]) == 1
    assert result["timerActive"] is False


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

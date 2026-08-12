"""`fused.fileIndex.*` — the file index, on the injected runtime bridge.

Two kinds of check, both over the shipped `static/runtime.js`:

* STRING CONTRACTS, the D137 style of tests/test_runtime_cancellation.py and
  tests/test_runtime_upload.py: what is on the `window.fused` surface (a
  function defined but not exported is the whole failure mode), and what is
  deliberately NOT — which is now most of the index API. `search` and `query`
  are the surface; scanning, config, repos, delete and ask are raw-fetch-only.

* THE BLOCK, RUN UNDER NODE, like tests/test_claude_app_state.py's `_node`:
  the readiness envelope, the X-Fused header on the POST and the error
  normalization are BEHAVIOUR, and a grep for `"X-Fused"` cannot tell whether
  the header reaches the request that needed it. The block between the
  `fused-file-index:start` / `fused-file-index:end` sentinels is self-contained
  — it reaches only for `fetch` and `callHeaders` — so it runs against a stubbed
  server with no DOM at all.
"""
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import fused_render

RUNTIME = (Path(fused_render.__file__).parent / "static" / "runtime.js").read_text(
    encoding="utf-8")
BLOCK = (RUNTIME.split("// fused-file-index:start", 1)[1]
         .split("// fused-file-index:end", 1)[0])

# The stubbed server. One canned body per route, plus a log of every request the
# block made — which is how "the header was sent" and "one extra status GET" are
# checked at all. Only three routes: the two the bridge calls and the probe.
_PRELUDE = """
const CALLS = [];
let ROUTES = {
  "/api/index/search": {status: 200, body: {ok: true, covered: true, fresh: true,
                                            updated: 1, age_s: 2, entries: []}},
  "/api/index/query": {status: 200, body: {ok: true, columns: [], rows: [],
                                           truncated: false}},
  "/api/index/status": {status: 200, body: {ok: true, indexed: true, has_index: true,
                                            scanning: false}},
};
function fetch(url, init) {
  CALLS.push({url: url, init: init || {}});
  const route = ROUTES[String(url).split("?")[0]];
  if (!route) throw new Error("unstubbed route: " + url);
  return Promise.resolve({
    ok: route.status < 400,
    status: route.status,
    json: () => ("body" in route
      ? Promise.resolve(route.body)
      : Promise.reject(new Error("not JSON"))),
  });
}
// The real one merges call-log attribution; only the merge matters here.
function callHeaders(extra) {
  return Object.assign({}, extra || {}, {"X-Fused-Call": "c1"});
}
function out(v) { console.log(JSON.stringify(v)); }
"""

# Both methods, with an argument object that exercises their options.
_METHODS = """
const METHODS = [
  ["search", () => fileIndex.search({root: "/x", q: "a", limit: 5})],
  ["query", () => fileIndex.query({sql: "select 1", limit: 3})],
];
"""


def _node(call: str, prelude: str = "") -> Any:
    """Run the block under node and parse what it printed.

    `Any`, not `object`: every caller subscripts the parsed JSON, and the shape
    differs per test — the alternative is a cast in each one, which says nothing.
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run the index block")
    script = _PRELUDE + prelude + BLOCK + "\n" + call
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ------------------------------------------------------------ string contracts

def test_the_file_index_is_on_the_fused_surface():
    surface = RUNTIME.split("window.fused = {", 1)[1].split("};", 1)[0]
    assert "fileIndex," in surface
    # `index` was the name this shipped under for a few hours; nothing should
    # answer to it, and there is deliberately no back-compat alias.
    assert "index," not in surface


def test_the_contract_comment_documents_the_two_methods():
    header = RUNTIME.split("*/", 1)[0]
    assert "fused.fileIndex.*" in header
    # Both methods with their option names — the header is where an author reads
    # the contract, so a method added without a line here is undocumented.
    assert "search({root, q, limit})" in header
    assert "query({sql, limit})" in header
    # The envelope, which is the reason the API exists.
    assert "ready: {indexed, scanning, stale, reason}" in header
    # The omissions are documented AS omissions, so nobody has to guess: the two
    # that were never wrapped, and the narrowing that took the rest off.
    assert "/api/index/delete" in header
    assert "/api/index/ask" in header
    assert "X-Fused: 1" in header


def test_only_search_and_query_are_wrapped():
    """The narrowing, pinned. `query` is read-only SQL over the same
    `files`/`dirs` views /api/index/stats and /lookup read, so wrapping those was
    a second spelling of one capability; readiness rides on every response, so
    `status` was surface without capability. The rest manage the index, which is
    a shell action — still reachable by raw fetch with `X-Fused: 1`.
    """
    # Code only: the comments name the dropped routes to explain WHY they went.
    code = "\n".join(line for line in BLOCK.splitlines()
                     if not line.lstrip().startswith("//"))
    assert "/api/index/search" in code
    assert "/api/index/query" in code
    # The probe survives as a probe, not as a method.
    assert "/api/index/status" in code
    assert "fileIndex = { search:" in code
    for route in ("/api/index/stats", "/api/index/lookup", "/api/index/scan",
                  "/api/index/cancel", "/api/index/config", "/api/index/delete",
                  "/api/index/ask", "/api/git-repos"):
        assert route not in code, route


# ------------------------------------------------------- the readiness envelope

def test_both_methods_answer_with_a_readiness_envelope():
    """The single most important property of this API.

    A caller must never have to read zero rows as an answer without being able
    to ask whether the index was ever built (routers/git_repos.py's "original
    silent lie"), so both methods carry the same normalized object.
    """
    got = _node(_METHODS + """
Promise.all(METHODS.map(([name, run]) => run().then((r) => [name, r.ready])))
  .then((pairs) => out(pairs.reduce((a, [k, v]) => (a[k] = v, a), {})));
""")
    assert set(got) == {"search", "query"}
    for name, ready in got.items():
        assert set(ready) == {"indexed", "scanning", "stale", "reason"}, name
        assert ready["indexed"] is True, name


def test_query_piggybacks_the_status_probe():
    # /api/index/query answers columns and rows and nothing about readiness, so
    # one extra cheap GET beats shipping a result the caller cannot interpret.
    got = _node("""
fileIndex.query({sql: "select 1"}).then(() =>
  out(CALLS.map((c) => String(c.url).split("?")[0])));
""")
    assert got == ["/api/index/query", "/api/index/status"]


def test_search_does_not_double_its_request_count():
    # The per-keystroke corpus path. `scanning` is the ONE field allowed to come
    # back null, and this is the trade that buys it.
    got = _node("""
fileIndex.search({root: "/x"}).then((r) =>
  out({urls: CALLS.map((c) => String(c.url).split("?")[0]), ready: r.ready}));
""")
    assert got["urls"] == ["/api/index/search"]
    assert got["ready"]["scanning"] is None


def test_search_separates_no_index_from_a_root_it_never_visited():
    """`covered: false` collapses three conditions for the search box; the
    envelope un-collapses the one that matters."""
    got = _node("""
ROUTES["/api/index/search"] = {status: 200, body:
  {ok: true, covered: false, fresh: false, updated: null, age_s: null, entries: []}};
fileIndex.search({root: "/x"}).then((a) => {
  ROUTES["/api/index/search"] = {status: 200, body:
    {ok: true, covered: false, fresh: false, updated: 1700, age_s: 9, entries: []}};
  return fileIndex.search({root: "/x"}).then((b) => out([a.ready, b.ready]));
});
""")
    never_built, not_covered = got
    assert never_built["indexed"] is False and never_built["reason"] == "no-index"
    assert not_covered["indexed"] is True and not_covered["reason"] == "not-covered"


def test_a_failed_status_probe_says_nothing_rather_than_something_wrong():
    # The probe must not fail the call it describes, and must not answer for it
    # either: all-null is "this response cannot say".
    got = _node("""
ROUTES["/api/index/status"] = {status: 500};
fileIndex.query({sql: "select 1"}).then((r) => out(r.ready));
""")
    assert got == {"indexed": None, "scanning": None, "stale": None, "reason": None}


def test_a_failed_run_does_not_blind_the_envelope():
    """`error` is part of /api/index/status's SUCCESS shape.

    derive_state (index/runner.py) seeds `error: None` and fills it from
    `run_end`; _with_liveness writes the abandoned-worker sentence into it. If
    the probe refused that 200, one crashed scan would hand `query` — which has
    no readiness of its own — an all-null envelope until the dead run was pruned,
    and zero rows would again be unreadable as "no match" or "no index".
    """
    got = _node("""
ROUTES["/api/index/status"] = {status: 200, body: {ok: true, indexed: true,
  has_index: true, scanning: true, running: false, run_id: "r3",
  error: "the scan worker died without finishing (no activity for 300s)"}};
fileIndex.query({sql: "select 1"}).then((r) => out(r.ready),
                                        (e) => out({rejected: e.message}));
""")
    assert got == {"indexed": True, "scanning": True, "stale": True, "reason": None}


def test_a_2xx_error_from_a_read_route_still_rejects():
    # The guard is load-bearing on the routes that mean `error` as a failure: an
    # error rendered as an empty result is the failure this API exists to
    # prevent. Only the status probe — where `error` is a success field —
    # tolerates it.
    got = _node("""
ROUTES["/api/index/query"] = {status: 200, body: {error: "Binder Error: nope", rows: []}};
ROUTES["/api/index/search"] = {status: 200, body: {error: "unreadable manifest"}};
fileIndex.query({sql: "select 1"}).then(() => "resolved", (e) => e.message).then((q) =>
  fileIndex.search({root: "/x"}).then(() => "resolved", (e) => e.message)
    .then((s) => out([q, s])));
""")
    assert got == ["Binder Error: nope", "unreadable manifest"]


# ------------------------------------------------- the probe is not an app call

def test_the_readiness_probe_is_not_billed_as_a_call():
    """Every `query` fires one status GET, so logging it would double both the
    call log and the per-page rate spend for a page that polls.

    Same reasoning D244 applied to /api/jobs (BG-9): bookkeeping ABOUT a call is
    not a call. Prefix-skipped rather than header-stripped, so the shell's own
    status polling is covered by the same rule.
    """
    from fused_render import calls

    assert "/api/index/status".startswith(calls.SKIP_PREFIXES)
    # Only the status route: a real read the page asked for stays a logged call.
    assert not "/api/index/query".startswith(calls.SKIP_PREFIXES)
    assert not "/api/index/search".startswith(calls.SKIP_PREFIXES)


# ------------------------------------------------------------- the X-Fused header

def test_the_query_post_carries_the_x_fused_header():
    """`_require_fused` (server/common.py) 403s a POST without it. `query` is the
    only POST the bridge makes now; baking the header in keeps the convention in
    one place instead of in every app."""
    got = _node(_METHODS + """
Promise.all(METHODS.map(([, run]) => run())).then(() =>
  out(CALLS.filter((c) => c.init.method === "POST").map(
    (c) => [String(c.url), c.init.headers["X-Fused"],
            c.init.headers["Content-Type"]])));
""")
    assert got == [["/api/index/query", "1", "application/json"]]


def test_reads_are_plain_gets():
    got = _node(_METHODS + """
Promise.all(METHODS.map(([, run]) => run())).then(() =>
  out(CALLS.filter((c) => !c.init.method).map((c) => String(c.url).split("?")[0])));
""")
    assert set(got) == {"/api/index/search", "/api/index/status"}


def test_an_omitted_option_means_the_servers_default():
    # `root=` on /api/index/search is a 400 and an empty `limit=` fails the int
    # parse, so an unset option must drop out of the query string entirely.
    got = _node("""
fileIndex.search({root: "/x"}).then(() => out(CALLS.map((c) => String(c.url))));
""")
    assert got == ["/api/index/search?root=%2Fx"]


# --------------------------------------------------------- error normalization

def test_a_flat_error_becomes_the_message():
    got = _node("""
ROUTES["/api/index/query"] = {status: 400, body: {error: "Binder Error: no such column: nope"}};
fileIndex.query({sql: "select nope"}).then(
  () => out({ok: true}),
  (e) => out({message: e.message, type: e.type, status: e.status}));
""")
    assert got == {"message": "Binder Error: no such column: nope",
                   "type": "bad_request", "status": 400}


def test_the_nested_relay_envelope_does_not_render_as_an_object():
    # `{error: {type, message}}` read naively is "[object Object]" — the exact
    # bug frontend/src/platform/lib/index-query.ts exists to prevent.
    got = _node("""
ROUTES["/api/index/query"] = {status: 502, body:
  {error: {type: "ai_unavailable", message: "the claude CLI is not installed"}}};
fileIndex.query({sql: "select 1"}).then(
  () => out({ok: true}),
  (e) => out({message: e.message, type: e.type}));
""")
    assert got == {"message": "the claude CLI is not installed",
                   "type": "ai_unavailable"}


def test_the_x_fused_refusal_is_typed_forbidden():
    got = _node("""
ROUTES["/api/index/query"] = {status: 403, body: {error: "missing or invalid X-Fused header"}};
fileIndex.query({sql: "select 1"}).then(() => out({ok: true}), (e) => out({type: e.type}));
""")
    assert got == {"type": "forbidden"}


def test_a_body_that_is_not_json_still_rejects_with_a_sentence():
    got = _node("""
ROUTES["/api/index/search"] = {status: 500};
fileIndex.search({root: "/x"}).then(() => out({ok: true}), (e) => out({message: e.message}));
""")
    assert got == {"message": "HTTP 500"}

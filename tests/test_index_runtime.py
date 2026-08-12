"""`fused.index.*` — the file index, on the injected runtime bridge.

Two kinds of check, both over the shipped `static/runtime.js`:

* STRING CONTRACTS, the D137 style of tests/test_runtime_cancellation.py and
  tests/test_runtime_upload.py: what is on the `window.fused` surface (a
  function defined but not exported is the whole failure mode), and what is
  deliberately NOT (`/api/index/delete`, `/api/index/ask`).

* THE BLOCK, RUN UNDER NODE, like tests/test_claude_app_state.py's `_node`:
  the readiness envelope, the X-Fused header on every POST and the error
  normalization are BEHAVIOUR, and a grep for `"X-Fused"` cannot tell whether
  the header reaches the request that needed it. The block between the
  `fused-index:start` / `fused-index:end` sentinels is self-contained — it
  reaches only for `fetch` and `callHeaders` — so it runs against a stubbed
  server with no DOM at all.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import fused_render

RUNTIME = (Path(fused_render.__file__).parent / "static" / "runtime.js").read_text(
    encoding="utf-8")
BLOCK = RUNTIME.split("// fused-index:start", 1)[1].split("// fused-index:end", 1)[0]

# The stubbed server. One canned body per route, plus a log of every request the
# block made — which is how "the header was sent" and "one extra status GET" are
# checked at all.
_PRELUDE = """
const CALLS = [];
let ROUTES = {
  "/api/index/stats": {status: 200, body: {ok: true, empty: false, rows: 5, updated: 1}},
  "/api/index/lookup": {status: 200, body: {ok: true, empty: false, rows: [], total: 0}},
  "/api/index/search": {status: 200, body: {ok: true, covered: true, fresh: true,
                                            updated: 1, age_s: 2, entries: []}},
  "/api/index/query": {status: 200, body: {ok: true, columns: [], rows: [],
                                           truncated: false}},
  "/api/index/status": {status: 200, body: {ok: true, indexed: true, has_index: true,
                                            scanning: false}},
  "/api/index/scan": {status: 200, body: {ok: true, run_id: "r1"}},
  "/api/index/cancel": {status: 200, body: {ok: true, cancelled: true}},
  "/api/index/config": {status: 200, body: {ok: true, roots: ["/x"], ignore: []}},
  "/api/git-repos": {status: 200, body: {indexed: true, reason: null, scanning: false,
                                         stale: false, repos: []}},
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

# Every method, with an argument object that exercises its options.
_METHODS = """
const METHODS = [
  ["stats", () => index.stats({root: "/x"})],
  ["lookup", () => index.lookup({q: "a", limit: 10, offset: 20, sort: "size"})],
  ["search", () => index.search({root: "/x", q: "a", limit: 5})],
  ["query", () => index.query({sql: "select 1", limit: 3})],
  ["status", () => index.status()],
  ["scan", () => index.scan({root: "/x", full: true})],
  ["cancel", () => index.cancel({runId: "r1"})],
  ["repos", () => index.repos()],
  ["config.get", () => index.config.get()],
  ["config.set", () => index.config.set({roots: ["/x"], ignore: ["node_modules"]})],
];
"""


def _node(call: str, prelude: str = "") -> object:
    if not shutil.which("node"):
        pytest.skip("node is needed to run the index block")
    script = _PRELUDE + prelude + BLOCK + "\n" + call
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ------------------------------------------------------------ string contracts

def test_index_is_on_the_fused_surface():
    surface = RUNTIME.split("window.fused = {", 1)[1].split("};", 1)[0]
    assert "index," in surface


def test_the_contract_comment_documents_the_index():
    header = RUNTIME.split("*/", 1)[0]
    assert "fused.index.*" in header
    # Every method, with its option names — the header is where an author reads
    # the contract, so a method added without a line here is undocumented.
    for signature in ("stats({root})", "lookup({q, limit, offset, sort})",
                      "search({root, q, limit})", "query({sql, limit})",
                      "status({runId, since})", "scan({root, full})", "cancel({runId})",
                      "config.get()", "config.set({roots, ignore})", "repos()"):
        assert signature in header, signature
    # The envelope, which is the reason the API exists.
    assert "ready: {indexed, scanning, stale, reason}" in header
    # The two omissions are documented AS omissions, so nobody has to guess.
    assert "/api/index/delete" in header
    assert "/api/index/ask" in header


def test_destructive_and_billed_routes_are_not_wrapped():
    # Wiping the user's index on a page load, and spending AI credits per call,
    # are shell-level user actions — a raw fetch is still available to a page
    # that truly means it.
    assert "/api/index/delete" not in BLOCK
    assert "/api/index/ask" not in BLOCK


# ------------------------------------------------------- the readiness envelope

def test_every_method_answers_with_a_readiness_envelope():
    """The single most important property of this API.

    A caller must never have to read zero rows as an answer without being able
    to ask whether the index was ever built (routers/git_repos.py's "original
    silent lie"), so every method carries the same normalized object.
    """
    got = _node(_METHODS + """
Promise.all(METHODS.map(([name, run]) => run().then((r) => [name, r.ready])))
  .then((pairs) => out(pairs.reduce((a, [k, v]) => (a[k] = v, a), {})));
""")
    assert set(got) == {"stats", "lookup", "search", "query", "status", "scan",
                        "cancel", "repos", "config.get", "config.set"}
    for name, ready in got.items():
        assert set(ready) == {"indexed", "scanning", "stale", "reason"}, name
        assert ready["indexed"] is True, name


def test_lookup_and_query_piggyback_the_status_probe():
    # Neither response carries `scanning`, so one extra cheap GET beats shipping
    # a result the caller cannot interpret.
    got = _node("""
Promise.all([index.lookup({}), index.query({sql: "select 1"})]).then(() =>
  out(CALLS.map((c) => String(c.url).split("?")[0])));
""")
    assert got.count("/api/index/status") == 2


def test_search_does_not_double_its_request_count():
    # The per-keystroke corpus path. `scanning` is the ONE field allowed to come
    # back null, and this is the trade that buys it.
    got = _node("""
index.search({root: "/x"}).then((r) =>
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
index.search({root: "/x"}).then((a) => {
  ROUTES["/api/index/search"] = {status: 200, body:
    {ok: true, covered: false, fresh: false, updated: 1700, age_s: 9, entries: []}};
  return index.search({root: "/x"}).then((b) => out([a.ready, b.ready]));
});
""")
    never_built, not_covered = got
    assert never_built["indexed"] is False and never_built["reason"] == "no-index"
    assert not_covered["indexed"] is True and not_covered["reason"] == "not-covered"


def test_an_unbuilt_index_reports_indexed_false_from_its_own_answer():
    # `stats` and `lookup` both answer `empty` — the same read that produced the
    # numbers, so it outranks the probe.
    got = _node("""
ROUTES["/api/index/stats"] = {status: 200, body: {ok: true, empty: true, rows: 0}};
ROUTES["/api/index/status"] = {status: 200, body:
  {ok: true, indexed: true, has_index: true, scanning: false}};
index.stats({}).then((r) => out(r.ready));
""")
    assert got["indexed"] is False
    assert got["reason"] == "no-index"


def test_repos_readiness_passes_straight_through():
    # /api/git-repos already answers the whole triple, and its `reason` is a
    # distinction a UI renders: "outdated" means a rebuild is coming.
    got = _node("""
ROUTES["/api/git-repos"] = {status: 200, body:
  {indexed: false, reason: "outdated", scanning: true, stale: false, repos: []}};
index.repos().then((r) => out({ready: r.ready, urls: CALLS.map((c) => c.url)}));
""")
    assert got["ready"] == {"indexed": False, "reason": "outdated",
                            "scanning": True, "stale": False}
    assert got["urls"] == ["/api/git-repos"]


def test_a_failed_status_probe_says_nothing_rather_than_something_wrong():
    # The probe must not fail the call it describes, and must not answer for it
    # either: all-null is "this response cannot say".
    got = _node("""
ROUTES["/api/index/status"] = {status: 500};
index.query({sql: "select 1"}).then((r) => out(r.ready));
""")
    assert got == {"indexed": None, "scanning": None, "stale": None, "reason": None}


def test_a_started_scan_reports_itself_as_scanning():
    got = _node("""
index.scan({}).then((r) => out(r.ready));
""")
    assert got["scanning"] is True
    assert got["stale"] is True  # there IS a list, and it is now behind a scan


def test_a_config_save_that_started_rescans_reports_scanning():
    # A save starts one reconciling rescan per stale root (api_index_config,
    # server/routers/index.py), and the parallel probe routinely resolves before
    # runner.start has filed anything — the same race scan() forces past.
    got = _node("""
ROUTES["/api/index/config"] = {status: 200, body: {ok: true, roots: ["/x"], ignore: [],
  needs_rescan: true, rescan_run_id: "r7", rescan_run_ids: ["r7"]}};
index.config.set({ignore: ["node_modules"]}).then((r) => out(r.ready));
""")
    assert got["scanning"] is True
    assert got["stale"] is True  # there IS a list, and it is now behind a scan


def test_a_config_save_that_started_nothing_does_not_claim_scanning():
    # The override is driven off the response's own evidence: a save that
    # reconciled nothing must not invent a scan for a UI to wait on.
    got = _node("""
ROUTES["/api/index/config"] = {status: 200, body: {ok: true, roots: ["/x"], ignore: [],
  needs_rescan: false, rescan_run_id: null, rescan_run_ids: []}};
index.config.set({ignore: []}).then((r) => out(r.ready));
""")
    assert got["scanning"] is False
    assert got["stale"] is False


def test_a_failed_run_is_data_on_the_status_route_not_a_rejection():
    """`error` is part of /api/index/status's SUCCESS shape.

    derive_state (index/runner.py) seeds `error: None` and fills it from
    `run_end`; _with_liveness writes the abandoned-worker sentence into it. The
    status readout exists to RENDER that, so a 200 carrying it must resolve.
    """
    got = _node("""
ROUTES["/api/index/status"] = {status: 200, body: {ok: true, indexed: true,
  has_index: true, scanning: false, running: false, run_id: "r3",
  error: "the scan worker died without finishing (no activity for 300s)"}};
index.status().then((r) => out({error: r.error, ready: r.ready}),
                    (e) => out({rejected: e.message}));
""")
    assert got["error"] == ("the scan worker died without finishing "
                            "(no activity for 300s)")
    assert got["ready"] == {"indexed": True, "scanning": False, "stale": False,
                            "reason": None}


def test_a_failed_run_does_not_blind_the_envelope_of_every_other_call():
    # The mechanical consequence of the above: the piggybacked probe rejecting
    # would hand `query` — which has no `empty` of its own — an all-null
    # envelope, so it would render zero rows with no way to tell "no match" from
    # "no index". That is the silent lie this API exists to prevent.
    got = _node("""
ROUTES["/api/index/status"] = {status: 200, body: {ok: true, indexed: true,
  has_index: true, scanning: true, running: true, error: "the previous run died"}};
Promise.all([index.query({sql: "select 1"}), index.stats({root: "/x"})]).then(
  (rs) => out(rs.map((r) => r.ready)));
""")
    for ready in got:
        assert ready["indexed"] is True
        assert ready["scanning"] is True


def test_a_2xx_error_from_a_reading_route_still_rejects():
    # The guard is load-bearing everywhere else: an error rendered as an empty
    # result is the failure this API exists to prevent, so only the status
    # route — where `error` is a documented success field — tolerates it.
    got = _node("""
ROUTES["/api/index/query"] = {status: 200, body: {error: "Binder Error: nope", rows: []}};
ROUTES["/api/index/stats"] = {status: 200, body: {error: "unreadable manifest"}};
index.query({sql: "select 1"}).then(() => "resolved", (e) => e.message).then((q) =>
  index.stats({}).then(() => "resolved", (e) => e.message).then((s) => out([q, s])));
""")
    assert got == ["Binder Error: nope", "unreadable manifest"]


def test_a_genuinely_broken_status_probe_is_still_isolated():
    # Tolerating a 2xx `error` must not weaken the other half of the rule: a
    # probe that really fails still neither fails the call nor answers for it.
    got = _node("""
ROUTES["/api/index/status"] = {status: 503, body: {error: "index config unreadable"}};
index.query({sql: "select 1"}).then((r) => out(r.ready), (e) => out({rejected: e.message}));
""")
    assert got == {"indexed": None, "scanning": None, "stale": None, "reason": None}


def test_status_can_follow_the_run_a_scan_just_returned():
    # Without these the events/cursor stream is unreachable from the bridge, and
    # the rootless form reports the most recent RUNNING run — which is the wrong
    # one as soon as two roots scan concurrently.
    got = _node("""
index.status({runId: "r9", since: 12}).then(() =>
  index.status().then(() => out(CALLS.map((c) => String(c.url)))));
""")
    assert got[0] == "/api/index/status?run_id=r9&since=12"
    assert got[1] == "/api/index/status"


# ------------------------------------------------------------- the X-Fused header

def test_every_post_carries_the_x_fused_header():
    """`_require_fused` (server/common.py) 403s a POST without it. Baking it in
    keeps the convention in one place instead of in every app."""
    got = _node(_METHODS + """
Promise.all(METHODS.map(([, run]) => run())).then(() =>
  out(CALLS.filter((c) => c.init.method === "POST").map(
    (c) => [String(c.url), c.init.headers["X-Fused"],
            c.init.headers["Content-Type"]])));
""")
    posts = {url for url, _, _ in got}
    assert posts == {"/api/index/query", "/api/index/scan", "/api/index/cancel",
                     "/api/index/config"}
    for url, fused, ctype in got:
        assert fused == "1", url
        assert ctype == "application/json", url


def test_reads_are_plain_gets():
    got = _node(_METHODS + """
Promise.all(METHODS.map(([, run]) => run())).then(() =>
  out(CALLS.filter((c) => !c.init.method).map((c) => String(c.url).split("?")[0])));
""")
    assert "/api/index/stats" in got
    assert "/api/index/search" in got
    assert "/api/git-repos" in got


def test_the_wire_spelling_of_a_run_id_is_snake_case():
    # `runId` in JS, `run_id` on the wire — the same camel-to-snake trip every
    # other option in this bridge makes.
    got = _node("""
index.cancel({runId: "r9"}).then(() => out(
  CALLS.filter((c) => c.init.method === "POST").map((c) => JSON.parse(c.init.body))));
""")
    assert got == [{"run_id": "r9"}]


# ------------------------------------------------- the probe is not an app call

def test_the_readiness_probe_is_not_billed_as_a_call():
    """Every index call fires one status GET, so logging it would double both the
    call log and the per-page rate spend for a search box at 5 keystrokes/s.

    Same reasoning D244 applied to /api/jobs (BG-9): bookkeeping ABOUT a call is
    not a call. Prefix-skipped rather than header-stripped, so the shell's own
    status polling is covered by the same rule.
    """
    from fused_render import calls

    assert "/api/index/status".startswith(calls.SKIP_PREFIXES)
    # Only the status route: a real read the page asked for stays a logged call.
    assert not "/api/index/lookup".startswith(calls.SKIP_PREFIXES)
    assert not "/api/index/scan".startswith(calls.SKIP_PREFIXES)


# --------------------------------------------------------- error normalization

def test_a_flat_error_becomes_the_message():
    got = _node("""
ROUTES["/api/index/query"] = {status: 400, body: {error: "Binder Error: no such column: nope"}};
index.query({sql: "select nope"}).then(
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
index.query({sql: "select 1"}).then(
  () => out({ok: true}),
  (e) => out({message: e.message, type: e.type}));
""")
    assert got == {"message": "the claude CLI is not installed",
                   "type": "ai_unavailable"}


def test_the_x_fused_refusal_is_typed_forbidden():
    got = _node("""
ROUTES["/api/index/scan"] = {status: 403, body: {error: "missing or invalid X-Fused header"}};
index.scan({}).then(() => out({ok: true}), (e) => out({type: e.type}));
""")
    assert got == {"type": "forbidden"}


def test_a_body_that_is_not_json_still_rejects_with_a_sentence():
    got = _node("""
ROUTES["/api/index/stats"] = {status: 500};
index.stats({}).then(() => out({ok: true}), (e) => out({message: e.message}));
""")
    assert got == {"message": "HTTP 500"}

"""`rewritePath`/`snapshotWritable` (static/runtime.js) — the mechanism that
makes an app folder materialised at a commit (`/api/git/snapshot`) reach
every read a template makes: `readFile`, `rawUrl`, `stat` and `runPython`'s
`params` all rewrite an absolute path at or under the live app folder to the
same relative path under the extracted tree, and leave everything else alone.

Executed under node (like tests/test_ask_claude_hop.py's harness) rather than
merely grepped: what matters is the VALUE `rewritePath` returns for a given
`resolvedSnapshot`, not the shape of the source that decides it. `resolvedSnapshot`
is set directly here rather than driven through the real (async) resolve —
that resolve is exercised end to end by `tests/test_git_snapshot.py` (the
route it calls) and `frontend/src/apps/explorer` (the shell side that
populates it); this file is the served-runtime slice pinning the rewrite rule
itself once resolution has produced a value.
"""
import json
import os
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNTIME = os.path.join(_ROOT, "fused_render", "static", "runtime.js")


@pytest.fixture(scope="module")
def runtime_source() -> str:
    with open(_RUNTIME, encoding="utf-8") as f:
        return f.read()


def _extract(source: str, signature: str) -> str:
    start = source.index(signature)
    end = source.index("\n  }\n", start) + len("\n  }")
    return source[start:end]


def _extract_range(source: str, start_signature: str, end_signature: str) -> str:
    """Like `_extract`, but bounded by a second SIGNATURE rather than the
    first `\n  }\n` after the start — for a slice spanning several
    declarations that must run in the source's own order (the boot path)."""
    start = source.index(start_signature)
    end = source.index(end_signature, start)
    end = source.index("\n  }\n", end) + len("\n  }")
    return source[start:end]


@pytest.fixture(scope="module")
def rewrite_path_src(runtime_source: str) -> str:
    return _extract(runtime_source, "function rewritePath(path)")


@pytest.fixture(scope="module")
def snapshot_writable_src(runtime_source: str) -> str:
    # Starts at `_dirname`, not `snapshotWritable` itself: the write gate's
    # sibling-in-the-anchor's-own-directory check (finding [5], round 2
    # review) calls it, so a caller extracting `snapshotWritable` alone would
    # hit a ReferenceError the moment that branch runs.
    return _extract_range(runtime_source, "function _dirname(p)", "function snapshotWritable(path)")


@pytest.fixture(scope="module")
def snapshot_boot_src(runtime_source: str) -> str:
    """The REAL boot path, source-sliced rather than reassembled by hand:
    `snapshotSha`'s own parse, the `_snapshot_dir`/`_snapshot_app` synchronous
    read, `resolvedSnapshot`'s initial value, the fallback `snapshotReady`
    resolve, `rewritePath` and `snapshotWritable` — in the exact order and
    shape the shipped runtime executes them in, module-init included. Used by
    the tests below that exercise the actual race (finding B1), rather than
    setting `resolvedSnapshot` directly the way `rewrite_path_src`/
    `snapshot_writable_src` above do — those pin the REWRITE RULE once a
    value exists; these pin that a caller cannot observe a value BEFORE it
    should exist.
    """
    return _extract_range(
        runtime_source,
        "const snapshotSha = (function () {",
        "function snapshotWritable(path)",
    )


@pytest.fixture(scope="module")
def raw_url_src(runtime_source: str) -> str:
    return _extract(runtime_source, "function rawUrl(path)")


@pytest.fixture(scope="module")
def read_file_src(runtime_source: str) -> str:
    return _extract(runtime_source, "function readFile(path)")


def _run(script: str):
    if not shutil.which("node"):
        pytest.skip("node is needed to run runtime.js's snapshot rewrite")
    out = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


_SNAP = '{ sha: "abc1234", dir: "/cache/key/abc1234", app_dir: "/repo/myapp" }'


def test_a_path_under_app_dir_rewrites(rewrite_path_src):
    harness = """
let resolvedSnapshot = %s;
%s
console.log(JSON.stringify([
  rewritePath("/repo/myapp/reader.py"),
  rewritePath("/repo/myapp/sub/data.parquet"),
  rewritePath("/repo/myapp"),
]));
""" % (_SNAP, rewrite_path_src)
    result = _run(harness)
    assert result == [
        "/cache/key/abc1234/reader.py",
        "/cache/key/abc1234/sub/data.parquet",
        "/cache/key/abc1234",
    ]


def test_a_path_outside_app_dir_is_left_alone(rewrite_path_src):
    harness = """
let resolvedSnapshot = %s;
%s
console.log(JSON.stringify([
  rewritePath("/repo/otherapp/reader.py"),
  rewritePath("/elsewhere/file.txt"),
  // A same-string-prefix sibling is NOT inside the app folder — the same
  // segment-boundary discipline mount.py::_is_under_snapshot_root applies.
  rewritePath("/repo/myapp-notes/file.txt"),
]));
""" % (_SNAP, rewrite_path_src)
    result = _run(harness)
    assert result == [
        "/repo/otherapp/reader.py",
        "/elsewhere/file.txt",
        "/repo/myapp-notes/file.txt",
    ]


def test_a_relative_path_is_left_alone(rewrite_path_src):
    """Resolved against the PAGE's own directory (SPEC RH-1) — the template's
    own install-tree assets, never the target repository."""
    harness = """
let resolvedSnapshot = %s;
%s
console.log(JSON.stringify(rewritePath("./reader.py")));
""" % (_SNAP, rewrite_path_src)
    assert _run(harness) == "./reader.py"


def test_an_absent_snapshot_rewrites_nothing(rewrite_path_src):
    """No `_snapshot` on this frame at all — `resolvedSnapshot` stays its
    initial `null`."""
    harness = """
let resolvedSnapshot = null;
%s
console.log(JSON.stringify(rewritePath("/repo/myapp/reader.py")));
""" % rewrite_path_src
    assert _run(harness) == "/repo/myapp/reader.py"


def test_a_failed_resolve_leaves_reads_live_not_half_rewritten(rewrite_path_src):
    """`resolvedSnapshot` is only ever set on a SUCCESSFUL resolve (see the
    module docstring above `rewritePath` in runtime.js) — a failed one (no
    app folder, a mount-backed path, git trouble) or one still in flight both
    leave it `null`, which reads exactly like "no snapshot" rather than
    partially rewriting some reads and not others."""
    harness = """
let resolvedSnapshot = null; // what a failed/in-flight resolve leaves it as
%s
console.log(JSON.stringify([
  rewritePath("/repo/myapp/reader.py"),
  rewritePath("/repo/myapp"),
]));
""" % rewrite_path_src
    assert _run(harness) == ["/repo/myapp/reader.py", "/repo/myapp"]


def test_non_string_and_non_absolute_values_pass_through(rewrite_path_src):
    """runPython rewrites EVERY params value uniformly (no named-key allowlist
    — see the module comment above runPython) — a number, a bool, an object,
    or a plain relative-looking string must all come back unchanged rather
    than throwing."""
    harness = """
let resolvedSnapshot = %s;
%s
console.log(JSON.stringify([
  rewritePath(42),
  rewritePath(true),
  rewritePath(null),
  rewritePath(undefined) === undefined,
  rewritePath("engine"),
]));
""" % (_SNAP, rewrite_path_src)
    result = _run(harness)
    assert result == [42, True, None, True, "engine"]


# ------------------------------------------------------------ the write gate


def test_writes_under_app_dir_are_refused(snapshot_writable_src):
    harness = """
let resolvedSnapshot = %s;
let snapshotSha = "abc1234";
let snapshotAnchor = null;
%s
console.log(JSON.stringify([
  snapshotWritable("/repo/myapp/reader.py"),
  snapshotWritable("/repo/myapp"),
]));
""" % (_SNAP, snapshot_writable_src)
    assert _run(harness) == [False, False]


def test_writes_under_the_extracted_dir_are_also_refused(snapshot_writable_src):
    """Regression for finding [7], round 2 review: a `_render` frame's OWN
    `path` is rewritten to the EXTRACTED file before this frame's src is ever
    built (Preview.tsx), so it never again matches `resolvedSnapshot.app_dir`
    once resolved. Without also checking `resolvedSnapshot.dir`, the
    friendlier, sha-naming refusal silently stopped firing for exactly the
    one frame shape that most needs it — the server still refuses the write
    regardless (`mount.py::_is_under_snapshot_root`), so nothing was ever
    actually at risk, but the user saw a bare `readonly` instead."""
    harness = """
let resolvedSnapshot = %s;
let snapshotSha = "abc1234";
let snapshotAnchor = null;
%s
console.log(JSON.stringify([
  snapshotWritable("/cache/key/abc1234/reader.py"),
  snapshotWritable("/cache/key/abc1234"),
]));
""" % (_SNAP, snapshot_writable_src)
    assert _run(harness) == [False, False]


def test_writes_outside_app_dir_stay_writable(snapshot_writable_src):
    harness = """
let resolvedSnapshot = %s;
let snapshotSha = "abc1234";
let snapshotAnchor = null;
%s
console.log(JSON.stringify(snapshotWritable("/repo/otherapp/file.txt")));
""" % (_SNAP, snapshot_writable_src)
    assert _run(harness) is True


def test_writes_are_unaffected_with_no_active_snapshot(snapshot_writable_src):
    harness = """
let resolvedSnapshot = null;
let snapshotSha = null;
let snapshotAnchor = null;
%s
console.log(JSON.stringify(snapshotWritable("/repo/myapp/reader.py")));
""" % snapshot_writable_src
    assert _run(harness) is True


def test_a_pending_resolve_refuses_a_write_that_could_plausibly_be_the_app_folder(
    snapshot_writable_src,
):
    """Regression for finding B2: a write must be refused for the length of
    a pending (or, per the test below, a definitively FAILED) fallback
    resolve, not only once `resolvedSnapshot` has actually landed — the
    previous shape let `resolvedSnapshot === null` mean "writable"
    unconditionally once the fallback promise settled, which is also what a
    failed resolve looks like forever after. `snapshotAnchor` (the path this
    fallback resolve is climbing from) is what makes each of these three
    targets plausible: the anchor itself, an ancestor of it (every directory
    the climb could stop at), and a sibling in the anchor's own immediate
    directory (the innermost candidate app_dir's own contents)."""
    harness = """
let resolvedSnapshot = null;
let snapshotSha = "abc1234";
let snapshotAnchor = "/repo/myapp/reader.py";
%s
console.log(JSON.stringify([
  snapshotWritable("/repo/myapp/reader.py"),
  snapshotWritable("/repo/myapp"),
  snapshotWritable("/repo/myapp/other.py"),
]));
""" % snapshot_writable_src
    assert _run(harness) == [False, False, False]


def test_a_pending_resolve_permits_a_write_that_could_not_plausibly_be_the_app_folder(
    snapshot_writable_src,
):
    """Regression for finding [5], round 2 review: the pending/failed window
    used to refuse EVERY absolute path, not just ones that could plausibly be
    the (not-yet-known) app folder — a template's own unrelated scratch
    file, or another app's folder entirely, got the "this pane is read-only"
    refusal for no reason."""
    harness = """
let resolvedSnapshot = null;
let snapshotSha = "abc1234";
let snapshotAnchor = "/repo/myapp/reader.py";
%s
console.log(JSON.stringify(snapshotWritable("/scratch/unrelated.txt")));
""" % snapshot_writable_src
    assert _run(harness) is True


def test_a_failed_resolve_with_no_anchor_permits_writes(snapshot_writable_src):
    """An edge case with nothing to reason about at all — this frame's src
    carried `_snapshot` with neither `_file` nor `path` to climb an app
    folder from, so nothing is ever going to resolve and there is no anchor
    to narrow a refusal to. Falls back to the same "live" posture reads
    already take in this shape (`rewritePath` is a no-op with no
    `resolvedSnapshot` either)."""
    harness = """
let resolvedSnapshot = null;
let snapshotSha = "abc1234";
let snapshotAnchor = null;
%s
console.log(JSON.stringify(snapshotWritable("/repo/myapp/reader.py")));
""" % snapshot_writable_src
    assert _run(harness) is True


# ------------------------------------------------- the real boot path (B1/B2)


def test_the_primary_path_resolves_synchronously_with_no_fetch(
    snapshot_boot_src, read_file_src, raw_url_src
):
    """The headline fix: when this frame's own src carries the precomputed
    `_snapshot_dir`/`_snapshot_app` (what Preview.tsx now always supplies,
    see Preview.tsx's own comment on the `_render`/template src),
    `resolvedSnapshot` is populated the instant this script's module-init
    code runs — before `readFile` is ever called, and with NO fetch to
    `/api/git/snapshot` at all. This is what makes the once-real race
    (finding B1) structurally impossible in the ordinary path, rather than
    merely narrow."""
    query = {
        "_snapshot": "abc1234",
        "_snapshot_dir": "/cache/key/abc1234",
        "_snapshot_app": "/repo/myapp",
        "_file": "/repo/myapp/reader.py",
    }
    harness = """
const _query = %s;
function ownQuery(key) { return Object.prototype.hasOwnProperty.call(_query, key) ? _query[key] : null; }
function callHeaders() { return {}; }
global.window = { location: { search: "" } };

const fetchLog = [];
global.fetch = function (url) {
  fetchLog.push(url);
  return Promise.resolve({ ok: true, text: () => Promise.resolve("RAW-CONTENT") });
};

%s
%s
%s

// Read once, immediately, with no synchronous wait: the primary path must
// not need one.
readFile("/repo/myapp/reader.py").then((text) => {
  console.log(JSON.stringify({ fetchLog, text, resolvedSnapshot }));
});
""" % (json.dumps(query), snapshot_boot_src, raw_url_src, read_file_src)
    result = _run(harness)
    assert result is not None  # narrows for pyright; `_run` prints JSON or nothing
    # Exactly one fetch — the raw read itself — against the REWRITTEN path.
    # No call to /api/git/snapshot: nothing needed resolving over the network.
    assert result["fetchLog"] == ["/api/fs/raw?path=%2Fcache%2Fkey%2Fabc1234%2Freader.py"]
    assert result["text"] == "RAW-CONTENT"
    assert result["resolvedSnapshot"] == {
        "sha": "abc1234", "dir": "/cache/key/abc1234", "app_dir": "/repo/myapp",
    }


def test_the_fallback_path_waits_for_the_resolve_before_reading(
    snapshot_boot_src, read_file_src, raw_url_src
):
    """THE regression test for finding B1's race, exercising the real boot
    path rather than a hand-set `resolvedSnapshot` (which structurally cannot
    catch this — see this module's own docstring). This frame's src carries
    `_snapshot` with NO precomputed `_snapshot_dir`/`_snapshot_app` (the
    fallback shape: a manually-typed url, an embed context Preview.tsx does
    not build the src for). `readFile` is called SYNCHRONOUSLY, in the same
    tick module-init runs in — exactly the shape a template's own boot-time
    `fused.readFile(fused.params.get("_file"))` takes. The fake
    `/api/git/snapshot` fetch resolves only after a macrotask delay
    (`setTimeout`), standing in for a real network round trip.

    Before this fix, `readFile` called the SYNCHRONOUS `rawUrl` (hence
    `rewritePath`) immediately — `resolvedSnapshot` was still `null` at that
    exact moment, so the raw read went out against the LIVE path, well before
    the snapshot resolve could possibly land. This test's fake raw-read
    `fetch` records the path it was actually asked for; the fix must show the
    REWRITTEN (extracted) path there, proving `readFile` genuinely waited.
    """
    query = {"_snapshot": "abc1234", "_file": "/repo/myapp/reader.py"}
    harness = """
const _query = %s;
function ownQuery(key) { return Object.prototype.hasOwnProperty.call(_query, key) ? _query[key] : null; }
function callHeaders() { return {}; }
global.window = { location: { search: "" } };

const fetchLog = [];
let resolveSnapshotFetch;
global.fetch = function (url) {
  fetchLog.push(url);
  if (url.indexOf("/api/git/snapshot") === 0) {
    return new Promise((resolve) => {
      resolveSnapshotFetch = () => resolve({
        ok: true,
        json: () => Promise.resolve({ ok: true, dir: "/cache/key/abc1234", app_dir: "/repo/myapp" }),
      });
    });
  }
  return Promise.resolve({ ok: true, text: () => Promise.resolve("RAW-CONTENT") });
};

%s
%s
%s

// Called in the SAME TICK as module-init, before any await has had a chance
// to run — the shape a template's own synchronous boot-time call takes.
const readPromise = readFile("/repo/myapp/reader.py");
// Captured synchronously, right after the call above returned: proves the
// resolve genuinely had not landed yet at the moment readFile was invoked.
const resolvedAtCallTime = resolvedSnapshot;

// Stand in for the network round trip settling well after this tick.
setTimeout(() => { resolveSnapshotFetch(); }, 20);

readPromise.then((text) => {
  console.log(JSON.stringify({ resolvedAtCallTime, fetchLog, text }));
});
""" % (json.dumps(query), snapshot_boot_src, raw_url_src, read_file_src)
    result = _run(harness)
    assert result is not None
    assert result["resolvedAtCallTime"] is None  # nothing had resolved yet
    assert result["fetchLog"][0].startswith("/api/git/snapshot?")
    # The raw read must have waited for the resolve — it is asked for the
    # REWRITTEN (extracted) path, not the live one.
    assert result["fetchLog"][1] == "/api/fs/raw?path=%2Fcache%2Fkey%2Fabc1234%2Freader.py"
    assert result["text"] == "RAW-CONTENT"


# --------------------------------------------------- the write gate (round 2)


def test_a_failed_fallback_resolve_permanently_refuses_writes_under_the_anchor(
    snapshot_boot_src,
):
    """THE regression test for finding B2 reopened at round 2 review,
    exercising the REAL boot path (module-init through the real, async
    `snapshotReady`/`fetch` chain) rather than a hand-assigned
    `resolvedSnapshot`/flag — a test built by hand-assigning state can only
    assert what its author already believes those states settle to, which is
    exactly how this gap survived a round: the previous round's own test,
    `test_a_failed_resolve_leaves_reads_live_not_half_rewritten` above, only
    ever asserted on `rewritePath` (reads), never on `snapshotWritable`
    (writes) at all.

    A 404 from `/api/git/snapshot` (no app folder here) leaves
    `resolvedSnapshot` `null` FOREVER — the previous shape's
    `snapshotFallbackPending` flag reset to `false` the instant this promise
    settled regardless of why, and `snapshotWritable` read `!(null && …)` as
    "writable" from that point on: a write went straight through to the LIVE
    file while this frame's own URL (`_snapshot=abc1234`) still claimed a
    read-only commit, silently, with no refusal at all.
    """
    query = {"_snapshot": "abc1234", "_file": "/repo/myapp/reader.py"}
    harness = """
const _query = %s;
function ownQuery(key) { return Object.prototype.hasOwnProperty.call(_query, key) ? _query[key] : null; }
function callHeaders() { return {}; }
global.window = { location: { search: "" } };

global.fetch = function (url) {
  return Promise.resolve({
    ok: false, status: 404,
    json: () => Promise.resolve({ ok: false, error: "no app folder encloses this path" }),
  });
};

%s

snapshotReady.then((snap) => {
  console.log(JSON.stringify({
    snap,
    writableUnderAnchor: snapshotWritable("/repo/myapp/reader.py"),
    writableAncestor: snapshotWritable("/repo/myapp"),
    writableUnrelated: snapshotWritable("/scratch/other.txt"),
  }));
});
""" % (json.dumps(query), snapshot_boot_src)
    result = _run(harness)
    assert result is not None
    assert result["snap"] is None  # the resolve genuinely, definitively failed
    # The URL still claims `_snapshot=abc1234` — a write to anything that
    # could plausibly BE the (never-confirmed) app folder must stay refused
    # forever, not just for the length of the resolve.
    assert result["writableUnderAnchor"] is False
    assert result["writableAncestor"] is False
    # But an unrelated path is not held hostage by a resolve about a
    # completely different subtree (finding [5]).
    assert result["writableUnrelated"] is True


def test_the_fallback_fetch_is_bounded_by_a_timeout(snapshot_boot_src):
    """Regression for finding [4]: an unbounded `fetch` meant `snapshotReady`
    — and therefore every one of `readFile`/`stat`/`runPython`, which all now
    chain off it — could hang FOREVER if the request never settled at all (a
    dropped connection, a server wedged mid-restart). `setTimeout` is faked to
    fire on the next tick rather than after the real, fixed delay (so this
    test does not itself hang for the length of that timeout); the real
    `AbortController`/`signal` plumbing is exercised for real, proving the
    abort genuinely fires and `snapshotReady` settles rather than hanging."""
    query = {"_snapshot": "abc1234", "_file": "/repo/myapp/reader.py"}
    harness = """
const _query = %s;
function ownQuery(key) { return Object.prototype.hasOwnProperty.call(_query, key) ? _query[key] : null; }
function callHeaders() { return {}; }
global.window = { location: { search: "" } };

const realSetTimeout = setTimeout;
let capturedDelay = null;
global.setTimeout = (fn, delay) => { capturedDelay = delay; return realSetTimeout(fn, 0); };

let sawAbort = false;
global.fetch = function (url, opts) {
  // Never settles on its own -- only the abort should ever end this.
  return new Promise((resolve, reject) => {
    opts.signal.addEventListener("abort", () => {
      sawAbort = true;
      reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    });
  });
};

%s

snapshotReady.then((snap) => {
  console.log(JSON.stringify({ snap, sawAbort, capturedDelay }));
});
""" % (json.dumps(query), snapshot_boot_src)
    result = _run(harness)
    assert result is not None
    assert result["capturedDelay"] == 25000
    assert result["sawAbort"] is True
    assert result["snap"] is None

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
    return _extract(runtime_source, "function snapshotWritable(path)")


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
let snapshotFallbackPending = false;
%s
console.log(JSON.stringify([
  snapshotWritable("/repo/myapp/reader.py"),
  snapshotWritable("/repo/myapp"),
]));
""" % (_SNAP, snapshot_writable_src)
    assert _run(harness) == [False, False]


def test_writes_outside_app_dir_stay_writable(snapshot_writable_src):
    harness = """
let resolvedSnapshot = %s;
let snapshotFallbackPending = false;
%s
console.log(JSON.stringify(snapshotWritable("/repo/otherapp/file.txt")));
""" % (_SNAP, snapshot_writable_src)
    assert _run(harness) is True


def test_writes_are_unaffected_with_no_active_snapshot(snapshot_writable_src):
    harness = """
let resolvedSnapshot = null;
let snapshotFallbackPending = false;
%s
console.log(JSON.stringify(snapshotWritable("/repo/myapp/reader.py")));
""" % snapshot_writable_src
    assert _run(harness) is True


def test_a_pending_fallback_resolve_refuses_writes(snapshot_writable_src):
    """Regression for finding B2: a write must be refused for the length of
    a pending (or definitively in-flight) fallback resolve, not only once
    `resolvedSnapshot` has actually landed — the previous shape let
    `resolvedSnapshot === null` mean "writable" unconditionally, which is
    also what a resolve still in flight looks like."""
    harness = """
let resolvedSnapshot = null;
let snapshotFallbackPending = true;
%s
console.log(JSON.stringify(snapshotWritable("/repo/otherapp/file.txt")));
""" % snapshot_writable_src
    assert _run(harness) is False


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
    assert result["resolvedAtCallTime"] is None  # nothing had resolved yet
    assert result["fetchLog"][0].startswith("/api/git/snapshot?")
    # The raw read must have waited for the resolve — it is asked for the
    # REWRITTEN (extracted) path, not the live one.
    assert result["fetchLog"][1] == "/api/fs/raw?path=%2Fcache%2Fkey%2Fabc1234%2Freader.py"
    assert result["text"] == "RAW-CONTENT"

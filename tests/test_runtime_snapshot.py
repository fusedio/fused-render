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


@pytest.fixture(scope="module")
def rewrite_path_src(runtime_source: str) -> str:
    return _extract(runtime_source, "function rewritePath(path)")


@pytest.fixture(scope="module")
def snapshot_writable_src(runtime_source: str) -> str:
    return _extract(runtime_source, "function snapshotWritable(path)")


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
%s
console.log(JSON.stringify(snapshotWritable("/repo/otherapp/file.txt")));
""" % (_SNAP, snapshot_writable_src)
    assert _run(harness) is True


def test_writes_are_unaffected_with_no_active_snapshot(snapshot_writable_src):
    harness = """
let resolvedSnapshot = null;
%s
console.log(JSON.stringify(snapshotWritable("/repo/myapp/reader.py")));
""" % snapshot_writable_src
    assert _run(harness) is True

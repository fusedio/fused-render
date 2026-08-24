"""`noteAskClaude` (static/runtime.js), the ancestor-window hop the git
template's "Fix with AI" button uses to hand its prompt to a Claude sidebar.

review #804 finding 6: `noteRevSelected` (the `_rev` sibling of this hop)
deliberately calls its hook on EVERY same-origin ancestor that has one,
because "which commit is previewed" is idempotent to repeat. Sending a prompt
is not idempotent — each delivery starts a real agent run with write access to
the repository — so `noteAskClaude` must stop at the FIRST ancestor that can
act, not broadcast to all of them. Executed under node (like
test_claude_app_state.py's `_node` harness) rather than merely grepped, because
what matters here is which of several mocked ancestors actually got called,
not the shape of the source that decides it.
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


@pytest.fixture(scope="module")
def note_ask_claude_src(runtime_source: str) -> str:
    start = runtime_source.index("function noteAskClaude(text)")
    end = runtime_source.index("\n  }\n", start) + len("\n  }")
    return runtime_source[start:end]


def _run(script: str):
    if not shutil.which("node"):
        pytest.skip("node is needed to run runtime.js's ancestor hop")
    out = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


# Three windows, chained: `window` (the git template's own frame, no hook of
# its own) -> `mid` (an inner embed shell, HAS the hook) -> `top` (the outer
# shell, ALSO has the hook — the exact double-listener shape finding 6 warns
# about, whether or not it is reachable through today's React guards). Each
# call records itself so the test can see how many fired and in what order.
_HARNESS = """
const calls = [];
const top = {{ parent: null, location: {{ href: "http://x/top" }},
              _fusedClaudeAsk: (t) => calls.push(["top", t]) }};
const mid = {{ parent: top, location: {{ href: "http://x/mid" }},
              _fusedClaudeAsk: (t) => calls.push(["mid", t]) }};
top.parent = top; // top of the chain
const window = {{ parent: mid, location: {{ href: "http://x/leaf" }} }};
{fn}
noteAskClaude("fix it");
console.log(JSON.stringify(calls));
"""


def test_only_the_nearest_ancestor_with_the_hook_is_called(note_ask_claude_src):
    calls = _run(_HARNESS.format(fn=note_ask_claude_src))
    assert calls == [["mid", "fix it"]], calls


def test_a_lone_listener_still_gets_delivered_to(note_ask_claude_src):
    """Not a change in the ordinary (single-listener) case: the common
    "one shell, one sidebar" configuration must keep working exactly as
    before."""
    harness = """
const calls = [];
const top = { parent: null, location: { href: "http://x/top" },
              _fusedClaudeAsk: (t) => calls.push(["top", t]) };
top.parent = top;
const window = { parent: top, location: { href: "http://x/leaf" } };
%s
noteAskClaude("fix it");
console.log(JSON.stringify(calls));
""" % note_ask_claude_src
    calls = _run(harness)
    assert calls == [["top", "fix it"]], calls


def test_no_listener_anywhere_is_a_quiet_no_op(note_ask_claude_src):
    harness = """
const calls = [];
const top = { parent: null, location: { href: "http://x/top" } };
top.parent = top;
const window = { parent: top, location: { href: "http://x/leaf" } };
%s
noteAskClaude("fix it");
console.log(JSON.stringify(calls));
""" % note_ask_claude_src
    calls = _run(harness)
    assert calls == [], calls


def test_an_empty_ask_never_reaches_any_ancestor(note_ask_claude_src):
    harness = """
const calls = [];
const top = { parent: null, location: { href: "http://x/top" },
              _fusedClaudeAsk: (t) => calls.push(["top", t]) };
top.parent = top;
const window = { parent: top, location: { href: "http://x/leaf" } };
%s
noteAskClaude("");
noteAskClaude(null);
noteAskClaude(undefined);
console.log(JSON.stringify(calls));
""" % note_ask_claude_src
    calls = _run(harness)
    assert calls == [], calls

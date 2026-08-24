"""`noteAskClaude`/`pullClaudeAsk` (static/runtime.js), the ancestor-window hop
the git template's "Fix with AI" button uses to hand its prompt to a Claude
sidebar, and the claude template's own half that collects it.

review #804 round 1 finding 6: `noteRevSelected` (the `_rev` sibling of this
hop) deliberately calls its hook on EVERY same-origin ancestor that has one,
because "which commit is previewed" is idempotent to repeat. Sending a prompt
is not idempotent — each delivery starts a real agent run with write access to
the repository — so `noteAskClaude` must stop at the FIRST ancestor that can
act, not broadcast to all of them.

review #804 round 2: the prompt no longer rides the claude iframe's `src` at
all (a `_fused_ask` query param, one-shot only by a cache that turned out to
replay on every remount — findings 1/2/3). It is handed to the host as
in-memory state (`_fusedClaudeAsk`) and PULLED by the claude template at its
own boot (`pullClaudeAsk`/`_fusedClaudeAskTake`) — consumption happens in the
frame that actually uses it, at the only moment that can matter, so there is
nothing left to cache and nothing that can replay on a later remount.
`noteAskClaude` also now RETURNS whether it found a listener (round 2 finding
4): the export is always present on every framed window, so "does
`_fusedAskClaude` exist" cannot tell the git template whether anyone is
listening — only the return value can.

Executed under node (like test_claude_app_state.py's `_node` harness) rather
than merely grepped, because what matters here is which of several mocked
ancestors actually got called (and what a query got back), not the shape of
the source that decides it.
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
def note_ask_claude_src(runtime_source: str) -> str:
    return _extract(runtime_source, "function noteAskClaude(text)")


@pytest.fixture(scope="module")
def pull_claude_ask_src(runtime_source: str) -> str:
    return _extract(runtime_source, "function pullClaudeAsk()")


def _run(script: str):
    if not shutil.which("node"):
        pytest.skip("node is needed to run runtime.js's ancestor hop")
    out = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


# ------------------------------------------------------------- noteAskClaude

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
const delivered = noteAskClaude("fix it");
console.log(JSON.stringify({{ calls, delivered }}));
"""


def test_only_the_nearest_ancestor_with_the_hook_is_called(note_ask_claude_src):
    result = _run(_HARNESS.format(fn=note_ask_claude_src))
    assert result["calls"] == [["mid", "fix it"]], result
    assert result["delivered"] is True


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
const delivered = noteAskClaude("fix it");
console.log(JSON.stringify({ calls, delivered }));
""" % note_ask_claude_src
    result = _run(harness)
    assert result["calls"] == [["top", "fix it"]], result
    assert result["delivered"] is True


def test_no_listener_anywhere_reports_undelivered(note_ask_claude_src):
    """review #804 round 2 finding 4: this is the case `askClaudeOnError` must
    be able to tell apart from success — nothing calls `_fusedClaudeAsk`
    anywhere in the chain, so the return value must say so."""
    harness = """
const calls = [];
const top = { parent: null, location: { href: "http://x/top" } };
top.parent = top;
const window = { parent: top, location: { href: "http://x/leaf" } };
%s
const delivered = noteAskClaude("fix it");
console.log(JSON.stringify({ calls, delivered }));
""" % note_ask_claude_src
    result = _run(harness)
    assert result["calls"] == [], result
    assert result["delivered"] is False


def test_an_empty_ask_never_reaches_any_ancestor(note_ask_claude_src):
    harness = """
const calls = [];
const top = { parent: null, location: { href: "http://x/top" },
              _fusedClaudeAsk: (t) => calls.push(["top", t]) };
top.parent = top;
const window = { parent: top, location: { href: "http://x/leaf" } };
%s
const results = [noteAskClaude(""), noteAskClaude(null), noteAskClaude(undefined)];
console.log(JSON.stringify({ calls, results }));
""" % note_ask_claude_src
    result = _run(harness)
    assert result["calls"] == [], result
    assert result["results"] == [False, False, False], result


# -------------------------------------------------------------- pullClaudeAsk

def test_pull_returns_the_nearest_ancestors_answer(pull_claude_ask_src):
    harness = """
const top = { parent: null, location: { href: "http://x/top" },
              _fusedClaudeAskTake: () => "top's answer" };
const mid = { parent: top, location: { href: "http://x/mid" },
              _fusedClaudeAskTake: () => "mid's answer" };
top.parent = top;
const window = { parent: mid, location: { href: "http://x/leaf" } };
%s
console.log(JSON.stringify(pullClaudeAsk()));
""" % pull_claude_ask_src
    assert _run(harness) == "mid's answer"


def test_pull_stops_at_the_first_ancestor_even_when_it_answers_null(pull_claude_ask_src):
    """A nearer ancestor that HAS the hook but has nothing pending (its own
    `_fusedClaudeAskTake` returns null) is still the answer — this is a query
    to ONE listener, not a search for the first non-null one across several."""
    harness = """
let topCalled = false;
const top = { parent: null, location: { href: "http://x/top" },
              _fusedClaudeAskTake: () => { topCalled = true; return "top's answer"; } };
const mid = { parent: top, location: { href: "http://x/mid" },
              _fusedClaudeAskTake: () => null };
top.parent = top;
const window = { parent: mid, location: { href: "http://x/leaf" } };
%s
const answer = pullClaudeAsk();
console.log(JSON.stringify({ answer, topCalled }));
""" % pull_claude_ask_src
    result = _run(harness)
    assert result["answer"] is None
    assert result["topCalled"] is False


def test_pull_with_no_ancestor_hook_answers_null(pull_claude_ask_src):
    harness = """
const top = { parent: null, location: { href: "http://x/top" } };
top.parent = top;
const window = { parent: top, location: { href: "http://x/leaf" } };
%s
console.log(JSON.stringify(pullClaudeAsk()));
""" % pull_claude_ask_src
    assert _run(harness) is None

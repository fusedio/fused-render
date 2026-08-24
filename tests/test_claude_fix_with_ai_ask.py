"""The claude template's half of "Fix with AI": a one-shot seeded prompt,
PULLED from the shell at boot rather than read off this document's own URL.

review #804 round 1: the git sidebar's "Fix with AI" button
(fused_render/templates/git/template.html `askClaudeOnError`) has no chat of
its own, so it hands its prompt to whichever ancestor owns a Claude sidebar
through the runtime's ancestor-window hop (static/runtime.js `noteAskClaude` /
`window._fusedClaudeAsk`, installed by Preview.tsx and Listing.tsx).

review #804 round 2: the prompt used to become `_fused_ask` on this iframe's
own src, read directly off `location.search` — a one-shot query param, kept
one-shot by a shell-side cache keyed on "has the src's ask-less base changed".
That shape had a hole no cache design closed: ANY remount of this iframe for a
reason that has nothing to do with a new ask (the sidebar toggled away and
back, the folder pane closed and reopened) rebuilds the identical cached src
and replays a stale error into a brand-new conversation — a `src` is an
address, and "follow this part of the address only the first time" cannot be
expressed by a URL however it is cached.

So the prompt is no longer a URL param anywhere. It lives on the HOST as
plain in-memory state, and THIS document PULLS it at its own boot through the
runtime's other ancestor hop (`window._fusedTakeClaudeAsk`, static/runtime.js
`pullClaudeAsk`) — a query that also CLEARS whatever it returns, in the host,
in the same step. Consumption is then a property of WHEN a pull happens (this
frame's own boot, the one moment it can matter) rather than something a src
string has to encode and a cache has to keep stable. See
tests/test_ask_claude_hop.py for `pullClaudeAsk`'s own behavior, and
test_git_scope.py / test_sessions_inbox_open_dir.py for the shell's (push)
half of the hop.

Structural assertions over the template source, the same approach
test_claude_message_anchor.py and test_claude_schedule_pill.py take: inline
vanilla JS in a very large document, so what is pinned is that the wiring
exists and that the properties easy to get wrong stay true.
"""
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_ROOT, "fused_render", "templates", "claude",
                         "template.html")


@pytest.fixture(scope="module")
def source() -> str:
    with open(_TEMPLATE, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def code(source) -> str:
    """Comments stripped, so a decision RECORDED in prose cannot satisfy a
    search for the thing it describes (same helper as
    test_claude_message_anchor.py's `code` fixture)."""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_html = re.sub(r"<!--.*?-->", "", without_block, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_html, flags=re.M)


def _boot(code: str) -> str:
    boot = code[code.rindex("(async () => {"):]
    return boot[:boot.index("\n})();")]


def test_the_ask_is_pulled_from_the_host_not_read_off_this_documents_url(code):
    """No `_fused_ask` query param anywhere — the whole point of round 2's
    redesign is that this text never touches a URL at all."""
    assert "_fused_ask" not in code
    boot = _boot(code)
    assert (
        "const ask = typeof window._fusedTakeClaudeAsk === \"function\"\n"
        "    ? window._fusedTakeClaudeAsk()\n"
        "    : null;"
    ) in boot


def test_the_pull_is_guarded_the_same_way_every_ancestor_hop_call_is(code):
    """A window with no `_fusedTakeClaudeAsk` hook (this template opened
    standalone, under the hosted runtime, or in a frame the shell's ancestor
    plumbing was never injected into) has nothing to pull — guarded exactly
    like the git template's own call to `window._fusedAskClaude` is."""
    boot = _boot(code)
    assert 'typeof window._fusedTakeClaudeAsk === "function"' in boot


def test_a_present_ask_starts_a_fresh_chat_and_sends_it(code):
    """The boot IIFE: a pulled `ask` wins outright, ahead of the session_id/run
    resume branch."""
    boot = _boot(code)
    assert "if (ask) {" in boot
    ask_branch = boot[boot.index("if (ask) {"):boot.index("} else if (session_id")]
    assert "enterChat();" in ask_branch
    assert "sendMessage(ask);" in ask_branch


def test_the_ask_branch_disowns_a_leftover_session_before_sending(code):
    """`session_id`/`run` are read through `fused.params`, which (this page
    sets no `_fusedParamBoundary`) targets the SHELL's own address bar, not
    this iframe's own src — and closing the sidebar (Preview.tsx/Listing.tsx)
    never clears them. So a PAST conversation on this same file can leave a
    `session_id`/`run` sitting on that address bar, and `sendMessage` re-reads
    `fused.params.get("session_id")` independently at send time (its own
    `agent.py` "start" call) regardless of anything decided in the boot IIFE.

    Without disowning them first, a pulled ask would still boot an empty
    transcript (this branch never calls loadHistory/resumeRun) while silently
    appending the fresh ask's message to the STALE conversation server-side,
    and would abandon any real in-flight `run` with nothing re-attaching it.
    Enforced the same way "New chat" enforces it before starting one (the
    `back` button's click handler): disown both with
    `{history: "replace", default: ""}`, in the ask branch, before
    `sendMessage` runs.
    """
    boot = _boot(code)
    ask_branch = boot[boot.index("if (ask) {"):boot.index("} else if (session_id")]
    clear_session = 'fused.params.set("session_id", "", { history: "replace", default: "" });'
    clear_run = 'fused.params.set("run", "", { history: "replace", default: "" });'
    assert clear_session in ask_branch
    assert clear_run in ask_branch
    # And both clears land BEFORE sendMessage — clearing after the send has
    # already read the (still stale) session_id would be no fix at all.
    assert ask_branch.index(clear_session) < ask_branch.index("sendMessage(ask)")
    assert ask_branch.index(clear_run) < ask_branch.index("sendMessage(ask)")


def test_the_ask_branch_bounds_its_wait_for_model_and_effort_detection(code):
    """`curModel()`/`curEffort()` (inside `sendMessage`'s `agent.py` "start"
    call) rank `detectedModel`/`prefModel` above the DEFAULT_MODEL/
    DEFAULT_EFFORT constants, but both are filled by ASYNC calls
    (`action: "defaults"` and `GET /api/prefs`) that are only kicked off, never
    awaited, at the top of the script. An ordinary visit never notices: a
    person reads the page and types before their first send, which is normally
    slower than either settling. The ask branch has no person in between — it
    sends the instant boot reaches it — so without awaiting the same two
    promises first, every "Fix with AI" run would launch on the bare defaults.

    review #804 round 2 finding 7: an UNBOUNDED await here is its own defect —
    `action: "defaults"` is a `fused.runPython` call that can take tens of
    seconds on a cold project venv, during which the user would see an empty
    transcript with no indication anything is queued. So the wait is bounded
    by `ASK_DETECTION_TIMEOUT_MS` — sending on whatever is known so far once
    the bound passes, rather than waiting indefinitely for detection.
    """
    assert "const detectionReady = fused.runPython(AGENT, { action: \"defaults\"" in code
    assert 'const prefsReady = fetch("/api/prefs")' in code
    assert re.search(r"const ASK_DETECTION_TIMEOUT_MS = \d+;", code)
    boot = _boot(code)
    ask_branch = boot[boot.index("if (ask) {"):boot.index("} else if (session_id")]
    assert "Promise.race([" in ask_branch
    assert "Promise.all([detectionReady, prefsReady])" in ask_branch
    assert "setTimeout(resolve, ASK_DETECTION_TIMEOUT_MS)" in ask_branch
    # And the wait happens BEFORE the send it exists to fix.
    assert ask_branch.index("Promise.race([") < ask_branch.index("sendMessage(ask)")


def test_the_ask_is_never_reported_to_the_model_as_an_app_param(source):
    """There is nothing to strip any more (round 2): the ask is not a URL
    param at all, so it cannot leak into a framed app's reported params
    through CHAT_PARAMS the way `_file`/`chat_only` are guarded against —
    re-checked on raw `source` so a stray reintroduction as a param is still
    caught. (The bare mention of `_fused_ask` survives in this file's own
    history-recording comments — see this module's docstring — so the literal
    QUOTED param form is what is checked, not the bare word.)"""
    assert '"_fused_ask"' not in source
    params = source[source.index("const CHAT_PARAMS"):]
    params = params[:params.index("]);")]
    assert '"_fused_ask"' not in params

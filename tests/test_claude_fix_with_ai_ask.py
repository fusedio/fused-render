"""The claude template's half of "Fix with AI": a one-shot seeded prompt.

The git sidebar's "Fix with AI" button (fused_render/templates/git/template.html
`askClaudeOnError`) has no chat of its own, so it hands its prompt to whichever
ancestor owns a Claude sidebar through the runtime's ancestor-window hop
(static/runtime.js `noteAskClaude` / `window._fusedClaudeAsk`, installed by
Preview.tsx and Listing.tsx). By the time it reaches THIS document it has
become `_fused_ask` on the iframe's own src — see test_git_scope.py and
test_sessions_inbox_open_dir.py for the shell's half of that hop.

Structural assertions over the template source, the same approach
test_claude_message_anchor.py and test_claude_schedule_pill.py take: inline
vanilla JS in a very large document, so what is pinned is that the wiring
exists and that the properties easy to get wrong stay true — chiefly that the
param is read as a HOST fact (`location.search`, never `fused.params`, the same
distinction `chat_only`/`_file` already make and the same reason: it describes
how this document was opened, not state that belongs on the shell's own URL)
and that it is one-shot (read exactly once, at boot).
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


def test_the_ask_is_read_off_this_frames_own_url(code):
    """Through `location.search`, not `fused.params` — the same distinction
    `CHAT_ONLY` makes a few lines above it, and for the same reason: this param
    says how the HOST built this one iframe, which is exactly wrong to read
    through the params helper that targets the shell's own URL."""
    assert 'const ASK = new URLSearchParams(location.search).get("_fused_ask")' in code
    assert 'fused.params.get("_fused_ask")' not in code


def test_the_ask_is_stripped_from_a_framed_apps_own_params(code):
    """Left out of CHAT_PARAMS it would reach the model as a param the framed
    app is running with — the same mistake `_file` and `chat_only` are in that
    set to avoid (test_claude_message_anchor.py pins `msg` the same way)."""
    params = code[code.index("const CHAT_PARAMS"):]
    params = params[:params.index("]);")]
    assert '"_fused_ask"' in params


def test_a_present_ask_starts_a_fresh_chat_and_sends_it(code):
    """The boot IIFE: `ASK` wins outright, ahead of the session_id/run resume
    branch — a present ask never coincides with either, because the host only
    builds this src when switching the sidebar TO claude for the first time
    over one error, but the ordering also has to be right regardless of that
    invariant holding."""
    boot = code[code.rindex("(async () => {"):]
    boot = boot[:boot.index("\n})();")]
    assert "if (ASK) {" in boot
    ask_branch = boot[boot.index("if (ASK) {"):boot.index("} else if (session_id")]
    assert "enterChat();" in ask_branch
    assert "sendMessage(ASK);" in ask_branch


def test_the_ask_is_never_reported_to_the_model_as_an_app_param(source):
    """Same guarantee test_the_ask_is_stripped_from_a_framed_apps_own_params
    pins on the stripped-comments `code` fixture; re-checked on raw `source`
    (with comments) so a future rewrite that only updates the doc comment above
    CHAT_PARAMS and forgets the set itself is still caught."""
    params = source[source.index("const CHAT_PARAMS"):]
    params = params[:params.index("]);")]
    assert '"_fused_ask"' in params

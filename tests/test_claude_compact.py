"""The `claude` template's COMPACT mode (`?compact=1`) — the transcript alone.

Its one and only host is the Tasks page's Cards view (frontend/src/shell/
TaskCards.tsx): a grid of these documents, one per running task, each in a 420px
tile whose own head already says which task it is and when it last moved. What
the tile wants from this page is the turns, arriving live — and nothing else. The
top bar repeats the tile's head, the annotate strip annotates a pane this layout
does not have, and the composer is a textarea inside a read-only card, which is
an affordance that lies.

Everything below is a pin on the MECHANISM rather than on the look, because each
one is a way this could be implemented that would break something else in the
file:

* it is a HOST FACT, so it is read off this frame's own URL. Read through
  `fused.params` instead and it would land on the shell's URL, where it would
  outlive the host that chose it — a bookmark that opens a crippled chat.
* the hiding is a STYLESHEET rule. An inline `display` under the body element is
  banned by tests/test_claude_narrow.py (the narrow layout's collapse cannot
  outrank one, and the force flag that could is banned by D146), and a `[hidden]`
  the script sets is a second piece of state to keep in step with the class.
* it is a CLASS AND NOTHING ELSE. Nothing is disabled, no poll changes, no
  keystroke is refused — so there is no mode to get out of sync, and the same
  document is a whole chat again the moment the class is off.
"""
import os
import re

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "claude", "template.html")


@pytest.fixture(scope="module")
def html():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _style_block(html):
    """The template's own <style> text (there is exactly one)."""
    start = html.index("<style>")
    return html[start:html.index("</style>", start)]


def _code(html):
    """The template with its comment prose removed.

    This file's comments RECORD rejected designs at length — that is the repo's
    convention — so a raw grep would let prose about what was NOT done read as
    the implementation. Same treatment tests/test_claude_kind.py `_pane_code`
    gives it, for the same reason.
    """
    src = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("//"))


def test_compact_is_read_off_this_frames_own_url(html):
    """The same shape `chat_only` has, and for the argument spelled out above it:
    the host decided this when it built the iframe src."""
    code = _code(html)
    assert 'const COMPACT = new URLSearchParams(location.search).get("compact") === "1";' in code
    # And NOT through the params bridge, in either direction — a write would put
    # a host's layout choice on the shell URL for good.
    assert 'params.get("compact")' not in code
    assert 'params.set("compact")' not in code


def test_the_class_is_stamped_before_anything_below_can_paint(html):
    """A tile that showed a composer for a frame and then took it away would be
    the one flicker on a wall of twelve, so this is not deferred to a boot
    function."""
    code = _code(html)
    assert 'if (COMPACT) document.body.classList.add("chat-compact");' in code


def test_compact_hides_the_bar_the_strip_the_composers_and_the_footnote(html):
    """The five elements that are either duplicated by the host or unusable in
    it. Asserted as one stylesheet rule listing all five, because they go
    together or the mode is half-applied — `#footnote` is the proof: it is a
    SIBLING of `#home`, so hiding the landing view left its standing line about
    approvals under a transcript nobody can reply to."""
    style = _style_block(html)
    m = re.search(r"((?:\s*body\.chat-compact #[a-z]+,)+\s*body\.chat-compact #[a-z]+)\s*\{"
                  r"\s*display: none;\s*\}", style)
    assert m, "compact hides its chrome from the stylesheet, in one rule"
    hidden = set(re.findall(r"body\.chat-compact #([a-z]+)", m.group(1)))
    assert hidden == {"topbar", "anntools", "inputbox", "home", "footnote"}


def test_compact_never_reaches_for_an_inline_style_or_the_force_flag(html):
    """Both are what the narrow layout's collapse (test_claude_narrow) and D146
    already forbid in this file; a new mode is exactly where they get
    reintroduced."""
    style = _style_block(html)
    compact_rules = "".join(
        m.group(0) for m in re.finditer(r"body\.chat-compact[^{]*\{[^}]*\}", style))
    assert compact_rules, "there are compact rules to check"
    assert "!" + "important" not in compact_rules
    # Nothing toggles the class from script either — it is stamped ONCE, at boot,
    # and the mode is that one write plus the rules above. A `remove`, a
    # `toggle`, or a second `add` would be runtime state to keep in step with a
    # host fact that cannot change while the document is alive.
    script = _code(html)
    script = script[script.index("</style>"):]
    assert script.count("chat-compact") == 1
    assert 'classList.remove("chat-compact")' not in script
    assert 'classList.toggle("chat-compact"' not in script


def test_the_transcript_starts_off_the_edge_and_not_under_a_missing_bar(html):
    """#log's 24px of top padding is the gap under the bar's border; with the bar
    gone it is dead space above the first line of the only thing the tile is
    for. Halved, not dropped."""
    assert "body.chat-compact #log { padding-top: 12px; }" in _style_block(html)


def test_compact_is_ours_and_is_never_described_as_the_framed_apps_state(html):
    """CHAT_PARAMS is the set of query keys that belong to THIS page rather than
    to whatever it is framing; a key missing from it is reported to the model as
    a param the framed app is running with, which describes our layout as its
    state."""
    code = _code(html)
    chat_params = code[code.index("const CHAT_PARAMS = new Set(["):]
    chat_params = chat_params[:chat_params.index("]);")]
    assert '"compact"' in chat_params
    # Its neighbour, so the two host facts are filed together rather than one of
    # them being remembered and the other not.
    assert '"chat_only"' in chat_params


def test_compact_and_chat_only_are_orthogonal(html):
    """Two different questions — "the host is already showing the file" and "you
    are a tile" — and a card passes both while a sidebar passes only the first.
    Neither may be implemented in terms of the other."""
    code = _code(html)
    assert "COMPACT = " in code and "CHAT_ONLY = " in code
    # No rule and no branch makes one imply the other.
    assert "COMPACT && CHAT_ONLY" not in code
    assert "CHAT_ONLY && COMPACT" not in code
    assert "body.nopane.chat-compact" not in code

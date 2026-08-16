"""Scroll-to-message: `?msg=<transcript uuid>` lands on ONE turn.

The Tasks list links a message, not just a session — its rows carry the uuid of
the transcript record the server read the prompt out of, and the link is
`…?_side=claude&session_id=<id>&msg=<uuid>` (spec §7, and
frontend/src/shell/tasks-lib.ts `messageHref`). Without a reader on the other
end the link opened the right conversation at the top, which is the half of the
feature that silently does nothing.

Structural assertions over the template source, the same approach
test_claude_schedule_pill.py takes: inline vanilla JS in a 12000-line document,
so what can be pinned is that the wiring exists and that the properties it would
be easy to get wrong stay true. The properties that matter here are all about
degrading quietly — a stale link must never break a chat — and about the
retry, which is the difference between the feature working and the feature
appearing to work.
"""
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_ROOT, "fused_render", "templates", "claude",
                         "template.html")
_AGENT = os.path.join(_ROOT, "fused_render", "templates", "claude", "agent.py")


@pytest.fixture(scope="module")
def source() -> str:
    with open(_TEMPLATE, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def code(source) -> str:
    """The template with comments stripped — this file's comments RECORD the
    decisions and would otherwise satisfy a search for the thing they describe."""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_html = re.sub(r"<!--.*?-->", "", without_block, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_html, flags=re.M)


@pytest.fixture(scope="module")
def agent() -> str:
    with open(_AGENT, encoding="utf-8") as f:
        return f.read()


def test_the_anchor_is_read_off_the_shell_url(code):
    """Through `fused.params`, not `location.search`.

    This page sets no `_fusedParamBoundary`, so its params live on the SHELL's
    url — which is where the Tasks list wrote this one. Read off the iframe's own
    search string (the way `chat_only` correctly is, because THAT one describes
    the iframe) the param is simply never there, and the feature is a no-op that
    looks implemented.
    """
    assert 'const MESSAGE_ANCHOR = fused.params.get("msg")' in code
    # ...and never the other spelling.
    assert 'URLSearchParams(location.search).get("msg")' not in code


def test_the_anchor_is_not_reported_to_the_model_as_an_app_param(code):
    """`msg` is this chat's own navigation bookkeeping. Left out of CHAT_PARAMS
    it reaches the model as a param the APP is running with, which describes our
    chrome as its state — the same mistake `split` and `leftmode` are in that set
    to avoid."""
    params = code[code.index("const CHAT_PARAMS"):]
    params = params[:params.index("]);")]
    assert '"msg"' in params


def test_a_restored_user_turn_carries_its_transcript_uuid(code, agent):
    """The uuid has to reach the DOM for anything to match against.

    Two halves, and both are load-bearing: `_history` returns it on user turns,
    and `addUser` writes it onto the element. It is set BEFORE the append —
    the anchor watcher fires on that append, and a turn whose id lands a tick
    later is one the watcher looks straight past.
    """
    assert '"uuid": str(row.get("uuid") or "")' in agent
    body = code[code.index("function addUser(text, uuid)"):]
    body = body[:body.index("\n}")]
    assert "d.dataset.msg = uuid" in body
    assert body.index("d.dataset.msg") < body.index("log.appendChild")
    # The history restore is the caller that knows it.
    assert "addUser(stripBlocks(t.text), t.uuid)" in code


def test_a_missing_or_foreign_anchor_still_lands_at_the_bottom(code):
    """`consumeAnchor` reports whether it found anything, and the restore falls
    through to its own scroll when it did not — a uuid from another transcript,
    or none at all, must leave the chat exactly as it would have been."""
    assert "if (!consumeAnchor()) scrollBottom();" in code


def test_the_uuid_is_compared_never_interpolated_into_a_selector(code):
    """It arrives off a url and is written by whoever holds the transcript.
    `querySelector('[data-msg="' + it + '"]')` throws on a quote, which would
    take the whole restore down with it — hence a string compare over the
    already-rendered turns."""
    body = code[code.index("function anchoredTurn()"):]
    body = body[:body.index("\n}")]
    assert 'log.querySelectorAll(".turn[data-msg]")' in body
    assert "el.dataset.msg === anchorPending" in body
    assert '"[data-msg=' not in code


def test_the_scroll_and_the_flare_respect_reduced_motion(code, source):
    """Both halves, and there is only ONE scroll call site to keep honest.

    Every scroll this feature makes — the first one, and each re-centre while
    the transcript settles — goes through `scrollToAnchor`, so the
    `prefers-reduced-motion` branch lives there instead of being repeated at
    each caller and forgotten at one of them. The flare's keyframes are then
    switched off on their own selector, because `.turn.is-anchored` outranks the
    `.turn { animation: none }` the reduce block already carries.
    """
    scroll = code[code.index("function scrollToAnchor(el, still)"):]
    scroll = scroll[:scroll.index("\n}")]
    assert 'still ? "auto" : "smooth"' in scroll
    # `still` is decided once, off the media query, and handed down.
    reveal = code[code.index("function revealAnchor()"):]
    reveal = reveal[:reveal.index("\n}")]
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in reveal
    assert "scrollToAnchor(el, still)" in reveal
    # Nothing in this feature scrolls the anchor except through that helper —
    # scoped to the anchor's own functions, because the page has an unrelated
    # `scrollIntoView` of its own elsewhere.
    settle = code[code.index("function settleAnchor(el, still)"):]
    settle = settle[:settle.index("\n}\n")]
    for name, region in (("revealAnchor", reveal), ("settleAnchor", settle)):
        assert "scrollIntoView(" not in region, \
            f"{name} must scroll through scrollToAnchor, not directly"
        assert "scrollToAnchor(el, still)" in region
    assert scroll.count("scrollIntoView(") == 2, \
        "the preferred form and the no-options fallback, and nothing else"
    assert re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{ \.turn\.is-anchored \{ animation: none; \} \}",
        source,
    )


def test_the_highlight_is_a_flare_and_not_a_selection(code, source):
    """It comes off on a timer. A permanent tint would sit there implying
    something is still selected and that clearing it is the reader's job."""
    body = code[code.index("function revealAnchor()"):]
    body = body[:body.index("\n}")]
    assert 'el.classList.add("is-anchored")' in body
    assert 'el.classList.remove("is-anchored")' in body
    assert "ANCHOR_FLARE_MS" in body
    # Drawn with box-shadow only: a padded, backgrounded turn would nudge every
    # turn under it the moment the class landed.
    flare = source[source.index(".turn.is-anchored {"):]
    flare = flare[:flare.index("}")]
    assert "box-shadow" in flare
    assert "padding" not in flare


def test_only_the_history_restore_can_render_an_anchorable_turn(code):
    """Why there is no watcher, pinned so it stays true.

    "Wait for the turn to appear" is the obvious shape for this and it is the
    wrong one: a turn can only be anchored if it carries a transcript uuid, and
    `loadHistory` is the ONLY `addUser` call site that passes one. Every other
    caller — the live send, `resumeRun`'s repair branches — is inventing a turn
    this frame, with no transcript record behind it and so no id any anchor could
    match. A watcher would be waiting on appends that can never match.

    This is the assertion that would fail if that stopped being true. Someone
    adding a second uuid-passing caller has to come here and decide, rather than
    getting a feature that silently misses the turn.
    """
    calls = re.findall(r"(?<!function )addUser\(([^\n]*)\)", code)
    assert calls, "addUser call sites not found"
    with_uuid = [c for c in calls if "," in c]
    assert with_uuid == ["stripBlocks(t.text), t.uuid"], with_uuid
    # And that one restore renders every turn synchronously before it looks, so
    # there is no moment where the turn exists but has not been looked for.
    load = code[code.index("const { turns } = await fused.runPython(AGENT"):]
    load = load[:load.index("} catch (err) {")]
    assert "for (const t of turns) {" in load
    assert "await" not in load.split("for (const t of turns) {")[1], \
        "the render loop must not yield before consumeAnchor runs"
    assert "if (!consumeAnchor()) scrollBottom();" in load


def test_the_anchor_holds_its_place_while_the_transcript_settles(code):
    """The real progressive-render hazard, and the one that silently misplaces
    the scroll.

    Turns arrive in one batch, but LAYOUT does not: a restored turn's pictures
    are `<img>`s pointed at /api/fs/raw with no intrinsic height until they load,
    and every one that lands above the anchor pushes it off screen. So the link
    that works in a text-only chat quietly lands in the wrong place in one with
    screenshots. `settleAnchor` re-centres over a bounded, decaying set of
    checks — and only when the turn has actually left the box, since a turn that
    is merely off-centre has already done its job.
    """
    body = code[code.index("function settleAnchor(el, still)"):]
    body = body[:body.index("\n}\n")]
    assert "ANCHOR_SETTLE_MS" in body
    assert "getBoundingClientRect()" in body
    assert "seen.bottom <= box.top || seen.top >= box.bottom" in body, \
        "re-centre only when the turn has left the scroller, not merely drifted"
    assert "el.isConnected" in body, "a turn dropped by a re-render is not chased"
    # Bounded: a fixed set of checks, not a loop or an interval.
    settle = code[code.index("const ANCHOR_SETTLE_MS = ["):]
    settle = settle[:settle.index("]")]
    assert settle.count(",") >= 1 and "setInterval" not in body


def test_the_reader_always_wins_the_scroll(code):
    """Any deliberate scroll cancels every pending re-centre. Once the reader has
    taken the wheel nothing here may take it back — a page that tugs the
    viewport after the user has started reading is worse than one that landed
    slightly off."""
    body = code[code.index("function settleAnchor(el, still)"):]
    body = body[:body.index("\n}\n")]
    for ev in ("wheel", "touchstart", "pointerdown", "keydown"):
        assert f'"{ev}"' in body, f"{ev} must cancel the settle"
    assert "removeEventListener" in body, "the listeners come off with the timers"
    assert "clearTimeout" in body


def test_the_anchor_gives_up(code):
    """A uuid that matches nothing here must not stay armed.

    There is no deadline because nothing is waiting on a clock — the give-up is
    a state change at the one moment that can decide it. Left armed, a stale uuid
    would sit until the next `loadHistory` and flare whichever turn of a
    DIFFERENT conversation carried that id, which is a worse failure than the
    link doing nothing.
    """
    body = code[code.index("function consumeAnchor()"):]
    body = body[:body.index("\n}")]
    assert "revealAnchor()" in body
    assert 'anchorPending = ""' in body
    # Cleared on the MISS as well as the hit — the `if` runs when revealAnchor
    # left it set, which is exactly the case where nothing matched.
    assert "if (anchorPending) {" in body
    assert 'fused.params.set("msg", ""' in body


def test_the_anchor_is_spent_once(code):
    """Held as state, and taken off the url with `replace` once it is spent.

    Re-read from the url on each attempt, a later session switch would flare
    whichever turn of a DIFFERENT transcript happened to collide. Left ON the
    url, a reload would re-scroll a reader who has since scrolled away and a
    Back would re-fire a history entry nobody made.

    Both live in `consumeAnchor` rather than `revealAnchor`, which is the whole
    point of splitting them: `revealAnchor` answers "is it here", and only the
    caller that knows this was the last chance may spend it.
    """
    spend = code[code.index("function consumeAnchor()"):]
    spend = spend[:spend.index("\n}")]
    assert 'fused.params.set("msg", "", { history: "replace", default: "" })' in spend
    reveal = code[code.index("function revealAnchor()"):]
    reveal = reveal[:reveal.index("\n}")]
    assert 'anchorPending = ""' not in reveal, \
        "revealAnchor must be safe to call without spending the anchor"
    # The url is written in exactly one place, so the two cannot disagree.
    assert code.count('fused.params.set("msg"') == 1

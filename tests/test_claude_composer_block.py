"""A pending scheduled message closes this chat's composer.

The reason is CONTEXT POLLUTION, and it is the whole feature: a task IS a Claude
session, so a message scheduled to run in this conversation reads whatever is in
it when it fires. Anything typed first joins that task's context, and nothing can
take it back out once the turn has read it (Akshil, 2026-08-17).

The block is the PENDENCY, not the proximity. A one-hour window was offered and
refused — "why can't we block it until the next task is scheduled? why only one
hour?" — because a message due tomorrow pollutes exactly as much as one due in
ten minutes.

Structural assertions over the template source, the same approach
test_claude_message_anchor.py and test_claude_schedule_pill.py take: inline
vanilla JS in a 12000-line document, so what can be pinned is that the wiring
exists and that the properties it would be easy to get wrong stay true. Here
those are: it blocks on a pending entry naming THIS session, it does NOT block
when the schedule cannot be read, it names the soonest of several, and the
recurring case says what it really costs.
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
    """The template with comments stripped — its comments RECORD the decisions and
    would otherwise satisfy a search for the thing they describe."""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_html = re.sub(r"<!--.*?-->", "", without_block, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_html, flags=re.M)


def _fn(code: str, opening: str) -> str:
    body = code[code.index(opening):]
    return body[:body.index("\n}")]


# ------------------------------------------------------- what blocks the box


def test_a_pending_message_naming_this_session_blocks_the_composer(code):
    """The two ids mean the same thing here: `session_id` is what the entry was
    told to resume, `claude_session_id` is what the run reported it landed in, and
    either naming the session on screen means these words are going into THIS
    thread."""
    body = _fn(code, "function schedPendingHere(")
    assert 'fused.params.get("session_id")' in body
    assert 'e.state === "pending"' in body
    assert "e.session_id === mine" in body
    assert "e.claude_session_id === mine" in body
    # and nothing else: a folder-wide task that names no session is not this
    # thread's business
    assert "if (!mine) return [];" in body


def test_the_box_is_shut_and_the_send_path_is_guarded(code):
    """Disabled for the eye, guarded for the hand: an annotation- or
    screenshot-only send needs no typing at all and would walk straight past a
    disabled textarea."""
    state = _fn(code, "function applyComposerBlockState(")
    assert "box.disabled = blocked" in state
    submit = _fn(code, "function submitChat()")
    assert "if (schedBlocked()) return;" in submit
    # ABOVE the queue branch: parking text for the live turn to drain puts it in
    # the very context the block exists to keep clean
    assert submit.index("schedBlocked()") < submit.index("if (activeRun)")


def test_the_send_button_is_never_disabled(code):
    """While a run is live that button is the STOP button, and a chat that cannot
    stop its own running turn is a worse state than the one this prevents."""
    state = _fn(code, "function applyComposerBlockState(")
    assert "disabled = blocked" in state
    assert state.count("disabled = blocked") == 1, "only the textarea"


def test_there_is_no_time_window_anywhere(code):
    """The rejected design. A block that expires on a clock would reopen the box
    while the message is still pending, which is the pollution it prevents."""
    for gone in ("BLOCK_WINDOW", "blockWindow", "ONE_HOUR", "BLOCK_LEAD"):
        assert gone not in code, f"{gone} is a time window on the block"
    # the decision itself: what is in the list is every pending entry for this
    # session, filtered by nothing but the session
    body = _fn(code, "function schedPendingHere(")
    assert "Date.now()" not in body, "the block must not consult the clock"


# ------------------------------------------------------- what does NOT block


def test_an_unreadable_schedule_blocks_nothing(code):
    """A chat that locks itself because one fetch failed is worse than the
    pollution the lock prevents: the user loses a working composer over a blip,
    and the schedule is the only thing that could ever explain it."""
    clear = _fn(code, "function clearComposerBlock(")
    assert "schedBlockers = []" in clear
    poll = code[code.index("async function pollScheduledRuns("):]
    poll = poll[:poll.index("\npollScheduledRuns();")]
    # both failure paths — a non-2xx answer and a throw
    assert "if (!res.ok) { clearComposerBlock(); return; }" in poll
    assert poll.count("clearComposerBlock()") >= 2


def test_the_landing_page_composer_is_never_blocked(code):
    """No session on the URL means no conversation to pollute yet — the landing
    page starts one that does not exist."""
    body = _fn(code, "function schedPendingHere(")
    assert "if (!mine) return [];" in body


def test_the_block_reads_the_poll_that_already_runs(code):
    """No second poll and no endpoint of its own: a pending message is one of the
    entries the run-watcher already fetches. It is applied BEFORE the home-view
    return and before the baseline — the block is a fact about the schedule, not
    about what this frame has rendered."""
    poll = code[code.index("async function pollScheduledRuns("):]
    poll = poll[:poll.index("\npollScheduledRuns();")]
    assert "applyComposerBlock(data.entries || []);" in poll
    assert poll.index("applyComposerBlock(") < poll.index('classList.contains("home")')
    assert poll.index("applyComposerBlock(") < poll.index("scheduleBaselined")


# ------------------------------------------------------- what the banner says


def test_it_is_a_banner_above_the_chat_and_not_a_modal(source):
    """The transcript stays readable and the session list stays reachable. It sits
    above the LOG rather than above the composer so it never moves the box the
    reader is looking at."""
    assert '<div id="schedblock"' in source
    assert source.index('<div id="schedblock"') < source.index('<div id="logwrap">')
    banner = source[source.index('<div id="schedblock"'):]
    banner = banner[:banner.index("\n    </div>")]
    assert 'id="sb-cancel"' in banner and 'id="sb-resched"' in banner


def test_it_names_the_soonest_and_counts_the_rest(code):
    """Naming one is what makes the banner answer "what is coming?"; a list of five
    would be the Schedule page in a strip above a chat."""
    sort = _fn(code, "function schedPendingHere(")
    assert 'String(a.due || "").localeCompare(String(b.due || ""))' in sort
    render = _fn(code, "function renderSchedBlock(")
    assert "const next = schedBlockers[0];" in render
    assert "const others = schedBlockers.length - 1;" in render
    assert "more message" in render and "more messages" in render


def test_the_recurring_case_does_not_read_like_a_short_wait(code):
    """A repeat always has a next occurrence, so the thread is read-only until the
    repeat itself changes — and cancelling one run does not do that, it skips one
    run and the server arms the next. Saying "cancel and carry on" here would be
    the banner lying about its own button."""
    render = _fn(code, "function renderSchedBlock(")
    assert "repeats — the next run is" in render
    assert "read-only for as long as the repeat is on" in render
    assert "moves the block to the next one" in render
    # and the button says what it really does to a repeat
    assert '"Cancel this run" : "Cancel message"' in render
    # a recurring OCCURRENCE carries template_id; the template itself repeats/rule
    is_repeat = _fn(code, "function schedIsRepeat(")
    for field in ("template_id", "repeats", "rule"):
        assert field in is_repeat


def test_the_why_line_says_context_and_not_politeness(code):
    """The user's own reason. A banner that said "please wait" would be asking for
    manners; this one is stating a consequence."""
    render = _fn(code, "function renderSchedBlock(")
    assert render.count("would join this task's context") == 2


# ------------------------------------------------------------- the two escapes


def test_cancel_is_the_one_write_and_it_carries_the_guard(code):
    """The template's only write to the schedule store, and it exists because the
    block it lifts is this template's own. X-Fused is the guard every mutating
    schedule POST requires (D3)."""
    assert '"/api/schedule/cancel"' in code
    handler = code[code.index('schedBlockCancel.addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    assert '"X-Fused": "1"' in handler
    assert 'method: "POST"' in handler
    assert 'JSON.stringify({ id: next.id })' in handler
    # applied locally so the box opens on the click, then reconciled — for a
    # repeat, the reconcile is what re-blocks on the occurrence armed next
    assert "schedBlockers = schedBlockers.slice(1);" in handler
    assert "pollScheduledRuns();" in handler


def test_a_refused_cancel_survives_the_poll_that_follows_it(code):
    """A cancel that raced the send is refused, and saying so once is not enough:
    the same click asks for a reconciling read, and a message written straight onto
    the banner was wiped by the re-render a second later. So the refusal is
    recorded against the ENTRY and re-stated every render until that entry is no
    longer the one holding the composer shut."""
    handler = code[code.index('schedBlockCancel.addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    assert "schedCancelRefused = String(next.id);" in handler
    assert 'schedCancelRefused = "";' in handler, "a cancel that worked clears it"
    render = _fn(code, "function renderSchedBlock(")
    assert "if (schedCancelRefused === next.id)" in render
    assert "may already be running" in render


def test_reschedule_reuses_the_composer_s_own_deep_link(code):
    """One form, one route: `/scheduled?new=1&…` is what the composer's calendar
    button already hands the Schedule page, and `edit` only says which stored entry
    the form is opening on."""
    handler = code[code.index('schedBlockResched.addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    assert "openScheduler(" in handler
    assert "next.id" in handler
    opener = _fn(code, "function openScheduler(")
    assert 'SCHEDULE_URL + "?new=1"' in opener
    assert '"&edit=" + encodeURIComponent(editId)' in opener
    assert "/scheduled" in code[code.index("const SCHEDULE_URL"):code.index("const SCHEDULE_URL") + 60]


def test_the_block_goes_with_the_conversation_it_belongs_to(code):
    """Switching sessions must not leave the previous thread's banner hanging over
    the next one for a poll interval. Unblocking is the safe direction to be
    briefly wrong in."""
    reset = _fn(code, "function scheduleResetForNewTranscript(")
    assert "clearComposerBlock();" in reset
    assert "pollScheduledRuns();" in reset

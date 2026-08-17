"""A pending scheduled message closes this chat's composer.

The reason is CONTEXT POLLUTION, and it is the whole feature: a task IS a Claude
session, so a message scheduled to run in this conversation reads whatever is in
it when it fires. Anything typed first joins that task's context, and nothing can
take it back out once the turn has read it (Akshil, 2026-08-17).

The block is the PENDENCY, not the proximity. A one-hour window was offered and
refused — "why can't we block it until the next task is scheduled? why only one
hour?" — because a message due tomorrow pollutes exactly as much as one due in
ten minutes.

The ESCAPE is not the same control in the two cases, and getting that wrong was
the banner's first shape (Akshil, 2026-08-17). A one-off unblocks when it is
cancelled. A repeat does not: `_materialize` arms the next occurrence, so
cancelling one run moves the block instead of lifting it, and rescheduling leaves
the message pending so the block survives that too. The only thing that reopens
the box for a repeat is stopping the repeat — `schedule.cancel(template_id)` —
and because that spends every future run, it takes two presses.

Structural assertions over the template source, the same approach
test_claude_message_anchor.py and test_claude_schedule_pill.py take: inline
vanilla JS in a 12000-line document, so what can be pinned is that the wiring
exists and that the properties it would be easy to get wrong stay true. Here
those are: it blocks on a pending entry naming THIS session, it does NOT block
when the schedule cannot be read, it shows the scheduled message, it sits against
the composer it is shutting, and its one action actually unblocks.
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


def test_the_banner_sits_directly_above_the_composer(source):
    """It explains a box that will not type, so it belongs against that box. Above
    the LOG it was a pane-height away from the thing it was about (Akshil,
    2026-08-17) — a notice about the composer, docked to the top bar."""
    assert '<div id="schedblock"' in source
    at = source.index('<div id="schedblock"')
    assert source.index('<div id="logwrap">') < at, "below the transcript"
    assert at < source.index('<form id="inputbox"'), "above the composer"
    # and it rides the COMPOSER'S column, not the pane's width: same measure and
    # same gutter as #composer-chat, so it reads as a lid on that box rather than
    # as a band across the pane
    card = source[source.index("  #schedblock {"):source.index("  #schedblock[hidden]")]
    composer = source[source.index("  #composer-chat {"):]
    composer = composer[:composer.index("\n  }")]
    for shared in ("max-width: 720px;", "width: 100%;", "margin: 0 auto;"):
        assert shared in card and shared in composer
    assert "20px" in card, "the composer's own side gutter"


def test_the_arriving_banner_does_not_move_the_composer(code, source):
    """The one thing a card directly above the input must never do. It does not,
    by LAYOUT rather than by reserved space: #logwrap is #chat's only `flex: 1`
    child, so the height comes out of the transcript and the composer stays put.
    The transcript's last line is what moves, so a reader who was at the bottom is
    put back there — measured before the render, applied on the shown edge only."""
    state = _fn(code, "function applyComposerBlockState(")
    assert "const wasHidden = schedBlockEl.hidden;" in state
    assert "const pinned = wasHidden && nearBottom();" in state
    assert state.index("nearBottom()") < state.index("renderSchedBlock()")
    assert "if (pinned && !schedBlockEl.hidden) scrollBottom();" in state
    # no reserved strip and no entry animation: #schedblock is `display: none`
    # when hidden, so an unblocked composer pays nothing for it
    assert "#schedblock[hidden] { display: none; }" in source
    css = source[source.index("  #schedblock {"):source.index("  #schedblock[hidden]")]
    assert "animation" not in css and "height" not in css


def test_the_body_is_the_scheduled_message_itself(code):
    """"Instead of this big text, show the schedule message" (Akshil, 2026-08-17).
    Reading the words that are about to run is what makes the shut box make sense;
    a paragraph about context pollution only described it. One ellipsed line, with
    the whole text on the title."""
    render = _fn(code, "function renderSchedBlock(")
    assert "schedBlockMsg.textContent = schedMsgLine(next);" in render
    assert 'schedBlockMsg.title = String(next.message || "");' in render
    line = _fn(code, "function schedMsgLine(")
    # whitespace COLLAPSES rather than the first line winning: a prompt opening
    # "Read the following and:" would otherwise preview as its own preamble
    assert 'replace(/\\s+/g, " ")' in line
    # and nothing is cut at a fixed character count — the CSS ellipses it
    assert "slice(" not in line and "length > " not in line


def test_it_names_the_soonest_and_counts_the_rest(code):
    """Naming one is what makes the banner answer "what is coming?"; a list of five
    would be the Tasks page in a strip above a chat. The count is a fragment, not
    a sentence with a plural branch — "1 more after it." reads fine."""
    sort = _fn(code, "function schedPendingHere(")
    assert 'String(a.due || "").localeCompare(String(b.due || ""))' in sort
    render = _fn(code, "function renderSchedBlock(")
    assert "const next = schedBlockers[0];" in render
    assert "const others = schedBlockers.length - 1;" in render
    assert 'why += " " + others + " more after it.";' in render


def test_the_reason_is_one_line_and_states_the_consequence(code):
    """The user's own reason, in a clause. Four lines of explanation for one fact
    was the complaint (Akshil, 2026-08-17 — "use less words"), and a banner that
    said "please wait" would be asking for manners rather than naming a cost."""
    render = _fn(code, "function renderSchedBlock(")
    assert render.count("anything you type joins it.") == 2
    assert "Repeats here, next " in render
    assert "Runs here " in render
    # the four lines it replaced are gone
    for gone in ("read-only for as long as", "moves the block to the next one",
                 "would join this task's context", "Reschedule to change"):
        assert gone not in code, f"the old paragraph survives: {gone!r}"


def test_a_recurring_occurrence_is_recognised(code):
    """A recurring OCCURRENCE carries `template_id`; a template itself carries
    `repeats` or `rule`. Which it is decides both the copy and — the substantive
    part — which id the one write posts."""
    is_repeat = _fn(code, "function schedIsRepeat(")
    for field in ("template_id", "repeats", "rule"):
        assert field in is_repeat


# ---------------------------------------------------------------- the escape


def test_the_primary_action_is_the_one_that_actually_unblocks(code):
    """THE substantive fix. Cancelling one occurrence of a repeat unblocks nothing
    — `_materialize` arms the next one and it blocks again — so the id posted for a
    repeat is the TEMPLATE's. `schedule.cancel` reads a template id as "no further
    runs" and cancels the materialized occurrence with it, which is exactly the
    escape this case needs; one endpoint still serves both."""
    target = _fn(code, "function schedStopTarget(")
    assert "schedIsRepeat(entry) && entry.template_id" in target
    assert "String(entry.template_id)" in target
    handler = code[code.index('schedBlockStop.addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    assert "const target = schedStopTarget(next);" in handler
    assert "JSON.stringify({ id: target })" in handler
    # and the label names the two different things it does
    render = _fn(code, "function renderSchedBlock(")
    assert '"Cancel every future run" : "Stop the repeat"' in render
    assert '"Cancel this message"' in render


def test_stopping_a_repeat_takes_two_presses_and_the_second_names_the_cost(code):
    """Not undoable from here: the write spends every run the task would ever have
    made, and no control on this page puts them back. The template's own idiom —
    the button becomes the question in place (snapAction, the ✕ / "Back to chat"
    pair) — rather than a dialog over a chat the reader may keep using. A ONE-OFF
    is a single press: it loses one message that can be scheduled again."""
    handler = code[code.index('schedBlockStop.addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    arm = handler.index('if (repeat && schedStopArmed !== next.id) {')
    assert arm < handler.index('fetch("/api/schedule/cancel"'), "arms before it writes"
    assert "schedStopArmed = String(next.id);" in handler
    render = _fn(code, "function renderSchedBlock(")
    # the armed label is the CONSEQUENCE, not "Confirm" or "Really?"
    assert '"Cancel every future run"' in render
    # and there is a way back out of the armed state
    assert 'armed ? "Keep it" : "Edit schedule"' in render
    back = code[code.index('schedBlockEdit.addEventListener("click"'):]
    back = back[:back.index("\n});")]
    assert "if (schedStopArmed === next.id) {" in back
    assert back.index("schedStopArmed") < back.index("openScheduler(")


def test_the_armed_press_survives_the_poll_and_belongs_to_its_entry(code):
    """The card re-renders every 15s under the poll. An armed button that quietly
    disarmed itself a few seconds after the press would be worse than no confirm at
    all — so it is page state keyed by id, not a class the render throws away. A
    different message arriving in front clears it: that press was aimed at the
    repeat that was on screen."""
    render = _fn(code, "function renderSchedBlock(")
    assert 'if (schedStopArmed && schedStopArmed !== next.id) schedStopArmed = "";' in render
    assert "const armed = schedStopArmed === next.id;" in render
    assert 'schedBlockStop.classList.toggle("armed", armed);' in render
    # and an emptied blocker list disarms rather than leaving it hot: a hidden
    # card holding an armed press is a destructive control one poll away from
    # being on screen again
    empty = render[:render.index("const others")]
    assert 'schedStopArmed = "";' in empty
    assert 'schedBlockStop.classList.remove("armed");' in empty


def test_the_stop_is_the_one_write_and_it_carries_the_guard(code):
    """The template's only write to the schedule store, and it exists because the
    block it lifts is this template's own. X-Fused is the guard every mutating
    schedule POST requires (D3)."""
    assert '"/api/schedule/cancel"' in code
    assert code.count('fetch("/api/schedule/cancel"') == 1
    handler = code[code.index('schedBlockStop.addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    assert '"X-Fused": "1"' in handler
    assert 'method: "POST"' in handler
    # applied locally so the box opens on the click, then reconciled
    assert "pollScheduledRuns();" in handler
    # a STOPPED repeat takes every blocker that belongs to it, not just the first:
    # the template is gone, so an occurrence of it left in the list would have the
    # banner naming a run the server has already dropped
    assert "schedBlockers = repeat" in handler
    assert "schedBlockers.filter((e) => schedStopTarget(e) !== target)" in handler
    assert "schedBlockers.slice(1)" in handler


def test_a_refused_stop_survives_the_poll_that_follows_it(code):
    """A cancel that raced the send is refused, and saying so once is not enough:
    the same click asks for a reconciling read, and a message written straight onto
    the banner was wiped by the re-render a second later. So the refusal is
    recorded against the ENTRY and re-stated every render until that entry is no
    longer the one holding the composer shut."""
    handler = code[code.index('schedBlockStop.addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    assert "schedStopRefused = String(next.id);" in handler
    assert 'schedStopRefused = "";' in handler, "a stop that worked clears it"
    render = _fn(code, "function renderSchedBlock(")
    assert "schedStopRefused === next.id" in render
    assert "may already be running" in render
    # the repeat's refusal says the repeat is still on, not "still scheduled":
    # what failed there was stopping a job, not skipping a message
    assert "The repeat is still on" in render


def test_editing_the_schedule_is_offered_but_never_as_the_way_out(code, source):
    """It is a legitimate thing to want while looking at the task holding the chat,
    and it is NOT an escape: an edited schedule leaves the message pending, so the
    block survives the trip. Offering it as the way out was the old banner's lie.
    So it is shaped as a quiet link against one bordered primary, and its own
    tooltip says the chat stays blocked."""
    handler = code[code.index('schedBlockEdit.addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    assert "openScheduler(" in handler
    assert "next.id" in handler
    render = _fn(code, "function renderSchedBlock(")
    assert "editing it leaves this chat blocked" in render
    # one bordered control in the row, and it is the one that unblocks
    banner = source[source.index('<div id="schedblock"'):]
    banner = banner[:banner.index('<form id="inputbox"')]
    assert banner.count("<button") == 2
    assert 'id="sb-stop" type="button"' in banner
    assert 'id="sb-edit" class="sb-quiet"' in banner
    assert "#schedblock .sb-acts .sb-quiet {" in source
    quiet = source[source.index("#schedblock .sb-acts .sb-quiet {"):]
    assert "border-color: transparent;" in quiet[:quiet.index("}")]


def test_the_edit_link_reuses_the_composer_s_own_deep_link(code):
    """One form, one route: `/scheduled?new=1&…` is what the composer's calendar
    button already hands the Tasks page, and `edit` only says which stored entry
    the form is opening on. The OCCURRENCE's id travels even for a repeat, because
    the Tasks page resolves an occurrence to its template before opening the
    form."""
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

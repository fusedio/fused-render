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

WHAT IT SHOWS is the Tasks list view's own row, and that was the third shape.
Prose plus a bare dump of the message underneath was rejected outright (Akshil,
2026-08-17 — "this UI is not good... this UI does not make sense to me, it does
not give me what I want"), so the banner reuses the vocabulary of the page this
object already lives on: a status ring, TASK-nnn, the name, and the state and time
at the right end. It does NOT show the folder or the session id — "you will not
show the folder because you are already in that folder, you are not showing the
session ID because you are already there" — and the row is the LINK to the task,
which is what let "Edit schedule" go: two doors to one room, and the banner's own
door could not unblock.

Structural assertions over the template source, the same approach
test_claude_message_anchor.py and test_claude_schedule_pill.py take: inline
vanilla JS in a 12000-line document, so what can be pinned is that the wiring
exists and that the properties it would be easy to get wrong stay true. Here
those are: it blocks on a pending entry naming THIS session, it does NOT block
when the schedule cannot be read, the row carries four facts and no fifth, it
sits against the composer it is shutting, and its one action actually unblocks.
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


def test_the_body_is_the_tasks_list_views_own_row(code, source):
    """"This UI is not good... it does not give me what I want" (Akshil,
    2026-08-17) — of a banner that explained the block in prose and then dumped the
    message under it as bare left-aligned text. So the body is the shape the Tasks
    LIST view already uses for exactly this object: a status ring, TASK-nnn, the
    name, one spacer, and the state and time at the right end. A reader who knows
    that page knows how to read this row."""
    banner = source[source.index('<div id="schedblock"'):]
    banner = banner[:banner.index('<form id="inputbox"')]
    for cell in ('class="sb-ring"', 'class="sb-id"', 'class="sb-name"',
                 'class="sb-grow"', 'class="sb-meta"'):
        assert cell in banner, f"the row is missing {cell}"
    # ONE spacer, the list view's own house rule: a second `margin-left: auto`
    # centres the right-hand group instead of pinning it
    css = source[source.index("  #schedblock .sb-row {"):source.index("  #schedblock .sb-note")]
    assert "margin-left: auto;" not in css
    assert "#schedblock .sb-grow { flex: 1 1 auto;" in source
    render = _fn(code, "function renderSchedBlock(")
    assert "schedBlockId.textContent = (rec && rec.task_id)" in render
    assert "schedBlockName.textContent = (rec && rec.title) || schedMsgLine(next);" in render
    assert 'schedBlockMeta.textContent = label + " · " + schedWhenText(next.due);' in render
    assert 'schedBlockRing.className = "sb-ring sb-ring--"' in render
    # the geometry is COPIED, never linked — a template cannot import the shell's
    # stylesheet, so the ring is drawn here the way schedule.css draws it
    ring = source[source.index("  #schedblock .sb-ring {"):]
    ring = ring[:ring.index("\n  }")]
    assert "border: 2px solid currentColor;" in ring
    assert "border-radius: 999px;" in ring
    line = _fn(code, "function schedMsgLine(")
    # whitespace COLLAPSES rather than the first line winning: a prompt opening
    # "Read the following and:" would otherwise preview as its own preamble
    assert 'replace(/\\s+/g, " ")' in line
    # and nothing is cut at a fixed character count — the CSS ellipses it
    assert "slice(" not in line and "length > " not in line


def test_the_row_shows_four_facts_and_the_folder_is_not_one_of_them(code, source):
    """"You will not show the folder because you are already in that folder, you
    are not showing the session ID because you are already there" (Akshil,
    2026-08-17). The list view carries both because it spans projects; inside the
    conversation they are the context and not news."""
    banner = source[source.index('<div id="schedblock"'):]
    banner = banner[:banner.index('<form id="inputbox"')]
    render = _fn(code, "function renderSchedBlock(")
    for leak in ("target", "project", "session_id", "claude_session_id", "folder"):
        assert leak not in render, f"the row is showing {leak!r}"
    assert "basename" not in render and "tilde" not in render
    # the four it DOES show
    assert "rec.task_id" in render      # which task
    assert "rec.title" in render        # what it is called
    assert "SB_STATES[state]" in render  # what state it is in
    assert "schedWhenText(next.due)" in render  # when it runs


def test_the_state_and_the_time_are_the_shells_own_vocabulary(code):
    """Five states and the words the board puts on them (schedule-lib.ts
    BOARD_COLUMNS), and the clock-then-relative-day shape tasks-lib.ts writes into
    the list's time cell. Unknown reads as Upcoming here rather than the shell's
    Done: every entry this banner can be looking at is PENDING, so a state that did
    not parse is a listing that has not answered yet, not a finished job."""
    states = code[code.index("const SB_STATES = {"):]
    states = states[:states.index("\n};")]
    for key, label in (("upcoming", "Upcoming"), ("in_progress", "In Progress"),
                       ("done", "Done"), ("failed", "Failed"),
                       ("archived", "Archive")):
        assert f'{key}: "{label}"' in states
    render = _fn(code, "function renderSchedBlock(")
    assert 'SB_STATES[rec.status] ? rec.status : "upcoming"' in render
    assert '(rec && rec.failed) ? "Failed"' in render
    when = _fn(code, "function schedWhenText(")
    assert 'return at + " today";' in when
    assert 'return at + " tomorrow";' in when
    # 24-hour, like the cell it is copying: a 12-hour locale renders "02:00 PM",
    # three characters wider in a pane that is often 340px, and it would not match
    # the time on the page this row is quoting
    assert 'schedPad2(d.getHours()) + ":" + schedPad2(d.getMinutes())' in when
    assert "toLocaleTimeString" not in when
    # past due is not a time in the past: a queued message waits for the sweep
    assert 'return "any moment now";' in when


def test_the_number_and_the_name_come_from_the_tasks_listing_and_fail_open(code):
    """TASK-nnn is allocated per project by tasks_store and only /api/tasks hands it
    out, so the row is filled from two reads. The BLOCK still depends on exactly one
    of them: an unreadable listing costs the number and the state, never the row and
    never the block."""
    load = _fn(code, "async function schedLoadTaskRow(")
    assert 'fetch("/api/tasks")' in load
    # no `schedTaskRowFor` recorded on a failure, so the next poll tries again
    fail = load[load.index("} catch (err) {"):]
    assert "schedTaskRow = null;" in fail
    assert "schedTaskRowFor" not in fail
    # cached against the blocking entry: the listing carries every task on the
    # machine and this poll runs every 15s, while a number and a name do not change
    assert "schedTaskRowFor === id" in load
    rec = _fn(code, "function schedTaskRec(")
    assert 'schedTaskRowFor === String(entry.id)' in rec
    # and it is fetched only once the box is already shut
    state = _fn(code, "function applyComposerBlockState(")
    assert "if (blocked) schedLoadTaskRow(schedBlockers[0]);" in state
    assert state.index("box.disabled") < state.index("schedLoadTaskRow")


def test_the_listing_row_is_found_by_the_session_before_the_message(code):
    """A scheduled entry naming a session is folded into that session's task
    (routers/tasks.py `_collect`), so the session id is the cheapest and most
    reliable key. The message scan is LAST because the listing carries only each
    task's three newest messages — it is the lookup that can legitimately miss."""
    find = _fn(code, "function schedFindTask(")
    assert 'const pending = "pending:" + id;' in find
    assert "task.key === mine" in find
    assert "task.key === pending" in find
    assert find.index("task.key === mine") < find.index("m.entry_id === id")


def test_the_message_renders_once_and_the_stray_copy_is_gone(code):
    """The duplicate the user saw next to the buttons was the native tooltip: the
    old card wrote the body onto `title` unconditionally, so a message short enough
    to fit rendered once in the row and again under the pointer. Now the title is
    the CLIPPED text only, and whether it clipped is measured rather than guessed at
    a character count."""
    title = _fn(code, "function schedNameTitle(")
    assert "schedBlockName.scrollWidth > schedBlockName.clientWidth + 1" in title
    assert "schedBlockName.title = schedBlockName.textContent;" in title
    assert 'schedBlockName.removeAttribute("title");' in title
    # measured WHEN THE POINTER ARRIVES, not when the row is drawn: at render time
    # the pane may still be laying out and reports every cell as clipped, and a
    # title written then outlives the layout that justified it — the same bug with
    # an extra step
    assert 'schedBlockRow.addEventListener("pointerenter", schedNameTitle);' in code
    assert 'schedBlockRow.addEventListener("focus", schedNameTitle);' in code
    render = _fn(code, "function renderSchedBlock(")
    assert "scrollWidth" not in render
    # a render replaces the text, so it drops the title that belonged to the old one
    assert 'schedBlockName.removeAttribute("title");' in render
    # and the message is never written to a title unconditionally again
    assert 'title = String(next.message' not in render


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


def test_the_reason_is_one_short_line_and_nothing_else_in_words(code):
    """ONE reason, and the row underneath carries the facts. "Too much prose" was
    the complaint twice over (Akshil, 2026-08-17 — "use less words", then "this UI
    is not good"): the first banner spent four lines on context pollution and the
    second still spent a sentence on it. The state and the time live on the row now,
    so the line only has to say the box is shut and why."""
    render = _fn(code, "function renderSchedBlock(")
    assert '"Blocked — a scheduled message runs in this chat."' in render
    assert '"Blocked — a repeating message runs in this chat."' in render
    # the sentences it replaced are gone, in both generations
    for gone in ("read-only for as long as", "moves the block to the next one",
                 "would join this task's context", "Reschedule to change",
                 "anything you type joins it", "Repeats here, next ",
                 "Runs here "):
        assert gone not in code, f"the old copy survives: {gone!r}"


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


def test_backing_out_of_an_armed_stop_is_the_pages_own_dismissal_contract(code):
    """There is no second button to be the "Keep it" any more — "Edit schedule" was
    the way back out and it is gone — so the escape is the GESTURE instead: a press
    outside the card, or Escape. That is .selpop's contract, which is what the rest
    of this template teaches a reader to expect. Escape is taken in the capture
    phase and CONSUMED, because the document-level binding kills a live run and
    backing out of a half-pressed confirm must not also do that."""
    disarm = _fn(code, "function schedDisarmStop(")
    assert "if (!schedStopArmed) return;" in disarm
    assert 'schedStopArmed = "";' in disarm
    assert "applyComposerBlockState();" in disarm
    assert "if (schedStopArmed && !schedBlockEl.contains(ev.target)) schedDisarmStop();" in code
    esc = code[code.index('if (ev.key !== "Escape" || !schedStopArmed) return;'):]
    esc = esc[:esc.index("}, true);")]
    assert "ev.stopPropagation();" in esc
    assert "ev.preventDefault();" in esc
    assert "schedDisarmStop();" in esc


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


def test_edit_schedule_is_gone_and_the_row_is_the_door(code, source):
    """It was put there so a schedule could be changed without losing the message.
    The row now lands on the task where Edit lives with its full popover, so the
    banner was two doors to one room — and its own door could not unblock (Akshil,
    2026-08-17). TWO controls in the card: the row, and the one that unblocks."""
    assert "sb-edit" not in source
    assert "sb-quiet" not in source
    assert "schedBlockEdit" not in source
    banner = source[source.index('<div id="schedblock"'):]
    banner = banner[:banner.index('<form id="inputbox"')]
    assert banner.count("<button") == 2
    assert 'id="sb-row" class="sb-row" type="button"' in banner
    assert 'id="sb-stop" type="button"' in banner
    # the ROW comes first, so it is reachable before the destructive button
    assert banner.index('id="sb-row"') < banner.index('id="sb-stop"')
    # nothing in this template deep-links into the edit form any more
    assert "openScheduler(String(next.message" not in code


def test_the_row_is_keyboard_reachable_before_the_destructive_button(source):
    """A real <button>, so Enter and Space work without a keydown handler of its
    own, with the page's visible focus ring — and FIRST in the DOM, so a keyboard
    reader arrives at "look at the task" before "cancel every future run of it"."""
    banner = source[source.index('<div id="schedblock"'):]
    banner = banner[:banner.index('<form id="inputbox"')]
    assert banner.index('id="sb-row"') < banner.index('id="sb-stop"')
    assert 'type="button"' in banner[banner.index('id="sb-row"'):
                                     banner.index('id="sb-row"') + 60]
    focus = source[source.index("  #schedblock .sb-row:focus-visible {"):]
    focus = focus[:focus.index("\n  }")]
    assert "outline: 2px solid var(--accent);" in focus
    assert "outline-offset: 2px;" in focus
    # motion is opt-IN: the only animated thing here is the in-progress ring, and
    # it is inside a `no-preference` query rather than switched off in a `reduce` one
    assert "@media (prefers-reduced-motion: no-preference) {" in source
    css = source[source.index("  #schedblock {"):source.index("  #box:disabled")]
    assert "transition: all" not in css
    assert css.count("animation") == 1, "only the in-progress ring"


def test_the_row_lands_on_the_task_and_prefers_the_calendar(code):
    """Clicking the row goes to the task, and the calendar is the view that answers
    what a blocked chat is asking — "when does this let go?". Scheduled.tsx has no
    URL param for its view, only the remembered-view row it reads on mount, so the
    hop writes that row: the same gesture as pressing the page's Calendar button.
    A denied store leaves the page on whichever view the reader last used — one
    press from the right one, never a dead end. It cannot scroll to the chip;
    nothing in that page's URL addresses one."""
    opener = _fn(code, "function openTaskOnCalendar(")
    assert 'window.top.localStorage.setItem(SCHEDULE_VIEW_KEY, "calendar")' in opener
    assert "catch (err)" in opener, "a denied or cross-origin store costs the view only"
    # the same top-window hop openScheduler makes: a shell ROUTE, so pushState plus
    # the shell's own navigate event, and a real navigation as the fallback
    assert 'host.history.pushState(null, "", SCHEDULE_URL);' in opener
    assert 'host.dispatchEvent(new Event("fused:navigate"));' in opener
    assert "window.top.location.href = SCHEDULE_URL;" in opener
    assert 'const SCHEDULE_VIEW_KEY = "fused-render:scheduled-view";' in code
    # and NOT the new-task deep link: the row opens the task, it does not open a form
    assert "?new=1" not in opener
    handler = code[code.index('schedBlockRow.addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    assert "if (!schedBlockers[0]) return;" in handler
    assert "openTaskOnCalendar();" in handler


def test_the_block_goes_with_the_conversation_it_belongs_to(code):
    """Switching sessions must not leave the previous thread's banner hanging over
    the next one for a poll interval. Unblocking is the safe direction to be
    briefly wrong in."""
    reset = _fn(code, "function scheduleResetForNewTranscript(")
    assert "clearComposerBlock();" in reset
    assert "pollScheduledRuns();" in reset

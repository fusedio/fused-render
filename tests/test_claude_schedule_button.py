"""The composer's calendar BUTTON — the hop from the chat to the Schedule page
(templates/claude/template.html -> frontend/src/shell/Scheduled.tsx).

There used to be a "Send now" pill beside it that deferred ONE message from the
composer itself, with no title, no description and no repeat rule (its own suite
was test_claude_schedule_pill.py). It is gone (Akshil, 2026-08-16): a row of
send-later presets sat permanently in front of every user to serve the rarest
thing they do with a draft, and everything past a bare deferral was the Schedule
page's form anyway. So the composer keeps one button whose whole job is the
HANDOFF — and the handoff carries the three things the page cannot know and this
template always does: the folder, the words already typed, and the conversation
they were written in.

Structural assertions over the two sources, the same approach test_claude_kind.py
takes: this is inline vanilla JS in a 12000-line document on one side and a React
page on the other, so what can be pinned is that the wiring exists and that the
contract between the two halves agrees. The contract IS duplicated — five param
names spelled in two files (D146: a duplicated rule needs a test, not a comment) —
which is most of what is below.
"""
import os
import re

import pytest

_TEMPLATE = os.path.join("fused_render", "templates", "claude", "template.html")
_PAGE = os.path.join("frontend", "src", "shell", "Scheduled.tsx")
_MODAL = os.path.join("frontend", "src", "shell", "NewJobModal.tsx")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def source() -> str:
    return _read(_TEMPLATE)


@pytest.fixture(scope="module")
def code(source) -> str:
    """The template with comments stripped — every "X is not there" assertion
    needs it, because this file's comments RECORD the decisions and would
    otherwise satisfy a search for the thing they say was rejected."""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_html = re.sub(r"<!--.*?-->", "", without_block, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_html, flags=re.M)


@pytest.fixture(scope="module")
def page() -> str:
    return _read(_PAGE)


@pytest.fixture(scope="module")
def modal() -> str:
    return _read(_MODAL)


def _open_scheduler(code: str) -> str:
    body = code[code.index("function openScheduler("):]
    return body[:body.index("\n}")]


# ------------------------------------------------------------- the template


def test_the_button_sits_immediately_left_of_send(code):
    """That seat is the whole idea: these two are the ways a draft leaves the box —
    now, or as a task — so they sit together after the spacer, apart from the three
    selects that only describe how a run behaves. Nothing may come between them, or
    the pairing stops reading as one."""
    rows = re.findall(r'<div class="composer-row">.*?</div>', code, flags=re.S)
    assert len(rows) == 2, "the chat composer and the home card each have one row"
    for row in rows:
        sched = re.search(r'<button [^>]*class="pill schedbtn"', row)
        assert sched, "no calendar button in this composer row"
        send = row.index('<button class="send"')
        assert sched.start() < send
        between = row[row.index(">", sched.start()):send]
        assert "<button" not in between, "something got between the calendar and Send"


def test_both_composers_carry_one(code):
    """A FIRST message is exactly as schedulable as a follow-up — the same reason
    both rows carry a screenshot button — so which of the two you are writing must
    not decide whether the control exists."""
    assert code.count('class="pill schedbtn"') == 2
    assert 'id="schedbtn"' in code
    assert 'id="hschedbtn"' in code


def test_it_is_a_named_icon_button_and_not_a_submit(code):
    """type="button" for the reason every control in a <form> composer carries it,
    and a monochrome inline svg on `currentColor` like .viewshot beside it: `.pill`
    paints only `color`, so an emoji would be the one loud thing in the row. The
    name is spoken, since the glyph cannot be."""
    for match in re.findall(r'<button [^>]*class="pill schedbtn"[^>]*>', code):
        assert 'type="button"' in match
        assert 'aria-label="Schedule this as a task"' in match
    # a calendar, drawn here rather than fetched: the template is one file
    svg = code[code.index('id="schedbtn"'):]
    svg = svg[:svg.index("</button>")]
    assert "<svg" in svg and "currentColor" in svg


def test_the_composer_no_longer_offers_to_send_later(code):
    """The pill and its two panels are gone, and so is everything that only served
    them — the preset list, the resolvers, the cron builder, the deferred-approvals
    substitution, and the composer's own POST to the schedule store. What is left
    is a send button that always sends."""
    for gone in ("when-sel", "WHEN_OPTIONS", "WHEN_AT", "WHEN_REPEAT",
                 "whenChoice(", "whenPlan(", "resetWhen(", "scheduledPerm(",
                 "renderWhenPanels(", "whenpanel", "whenfield", "Send now",
                 "Pick a time", "Repeats…", 'id="when"', 'id="hwhen"'):
        assert gone not in code, f"{gone} outlived the pill"
    # no date field anywhere in the composer any more
    assert "datetime-local" not in code
    # The composer still CREATES nothing: no POST that schedules anything, so a
    # send is a send and a schedule is the hop to the Schedule page.
    #
    # TWO requests now, not one (Akshil, 2026-08-17): the watcher's read, and the
    # block banner's cancel. The cancel is a write, and it is allowed here for a
    # reason the pill never had — it schedules nothing, it withdraws the message
    # holding this composer shut, so the control that explains the block and the
    # control that lifts it are the same one. See test_claude_composer_block.py.
    assert code.count("/api/schedule") == 2
    assert 'fetch("/api/schedule")' in code
    assert '"/api/schedule/cancel"' in code


def test_the_kebab_no_longer_carries_a_schedule_item(code):
    """It lived there for two days (2026-08-14 → 16) and could offer the FOLDER and
    nothing else, so the user retyped on the Schedule page the message they had
    already written. The composer holds the draft, so the composer is what hands it
    over — and the menu is back to its one item."""
    assert "schedopt" not in code
    pop = re.search(r'<div id="kebabpop".*?</div>', code, flags=re.S).group(0)
    assert 'id="terminalopt"' in pop
    assert pop.count("<button") == 1


# ------------------------------------------------------------- the confirm


def test_the_click_confirms_before_it_navigates(code):
    """The button sits 34px from Send and the click LEAVES the conversation, so a
    miss is expensive in a way a mistyped pill never was. The button opens the
    confirm and nothing else; only Continue navigates."""
    wiring = code[code.index('document.querySelectorAll(".schedbtn")'):]
    wiring = wiring[:wiring.index("\n});")]
    assert "openSchedConfirm(" in wiring
    assert "openScheduler(" not in wiring, "the button must not navigate on its own"

    go = code[code.index('document.getElementById("schedpop-go").addEventListener'):]
    go = go[:go.index("\n});")]
    assert "openScheduler(" in go


def test_no_route_through_this_button_survives_a_blocked_composer(code):
    """"When we have a blocked panel we don't allow to type… we should not allow to
    schedule the task as well" (Akshil, 2026-08-17). A session already holding a
    pending scheduled message takes no further input, and a task queued from here
    lands in that message's context exactly as a typed reply would.

    What this suite owns is the ROUTE COUNT: the handoff is two clicks and one
    Continue, and the block has to reach all three — the `disabled` attribute plus
    the click guard for the buttons, and a guard on Continue because the 15s poll
    can shut the chat while the confirm is still on screen. The state itself, both
    directions and the wording live in tests/test_claude_composer_block.py; there
    is exactly ONE of them and this reads the same one."""
    state = code[code.index("function applyComposerBlockState("):]
    state = state[:state.index("\n}")]
    assert "schedBtn.disabled = blocked" in state
    assert "const blocked = schedBlocked();" in state
    clicks = code[code.index('document.querySelectorAll(".schedbtn")'):]
    clicks = clicks[:clicks.index("\n});")]
    assert "if (schedBlocked()) return;" in clicks
    go = code[code.index('document.getElementById("schedpop-go").addEventListener'):]
    go = go[:go.index("\n});")]
    assert "if (schedBlocked()) return;" in go
    assert go.index("schedBlocked()") < go.index("openScheduler(")
    # and there is no fourth way in for it to have missed: no shortcut key, no
    # kebab item (deleted 2026-08-16, above), and only these two callers
    assert code.count("openSchedConfirm(") == 2
    assert code.count("openScheduler(") == 2


def test_the_confirm_says_what_travels(source):
    """The one thing the icon cannot say. A user who does not know the draft comes
    along will retype it on the other side, which is the failure the whole handoff
    exists to prevent."""
    pop = source[source.index('<div id="schedpop"'):]
    pop = pop[:pop.index("\n  </div>")]
    assert "Schedule this as a task?" in pop
    assert "This task will be scheduled to run at a specific time." in pop
    assert ">Continue<" in pop and ">Cancel<" in pop
    # the primary is the one that commits, like .send in the composer
    assert 'class="schedpop-btn go"' in pop


def test_escape_and_a_click_away_cancel(code):
    """.selpop's dismissal contract, because this is a note pinned to a button and
    not a modal. Escape is CONSUMED, or dismissing the confirm would also kill a
    live run through the document-level binding."""
    esc = code[code.index('if (ev.key !== "Escape" || !schedPopFor) return;'):]
    esc = esc[:esc.index("}, true);")]
    assert "ev.stopPropagation();" in esc
    assert "closeSchedConfirm();" in esc
    away = code[code.index('document.addEventListener("pointerdown", (ev) => {\n  if (schedPopFor'):]
    away = away[:away.index("\n});")]
    assert "!schedPop.contains(ev.target)" in away
    assert "closeSchedConfirm();" in away
    # a click into the preview iframe never reaches this document but does blur
    assert 'window.addEventListener("blur", closeSchedConfirm)' in code


def test_the_draft_is_read_when_continue_is_pressed(code):
    """Not when the confirm opened: a paste made with the question already on
    screen still has to travel, and reading at open time is how it would not."""
    go = code[code.index('document.getElementById("schedpop-go").addEventListener'):]
    go = go[:go.index("\n});")]
    assert ".value.trim()" in go
    assert "homebox" in go and "box" in go, "the composer that was clicked, not a guess"


# ------------------------------------------------------------- the hop


def test_the_hop_carries_the_target_the_draft_the_session_and_the_way_back(code):
    """Everything the page cannot know and this template always does. `back` is how
    the Schedule page offers "Back to chat" — without it the hop is one-way and the
    reader has to find their conversation again by hand."""
    body = _open_scheduler(code)
    assert "new=1" in body
    assert '"&target=" + encodeURIComponent(FILE)' in body
    assert '"&message=" + encodeURIComponent(draft || "")' in body
    assert '"&session_id=" + encodeURIComponent(fused.params.get("session_id") || "")' in body
    assert '"&back=" + encodeURIComponent(backHere())' in body


def test_an_empty_draft_is_still_a_valid_hop(code):
    """The page's form is where the description gets written, and arriving there
    with a blank field is exactly what "+ New task" already does. Nothing is
    validated here — this navigates, it does not create."""
    body = _open_scheduler(code)
    assert 'draft || ""' in body
    for refusal in ("if (!draft) return", "addNote(", "needs some text"):
        assert refusal not in body


def test_the_way_back_is_the_shells_url_and_not_this_frames(code):
    """Framed by the React shell, `location` here is the view's own inner URL, and
    a Back button pointing at it would return the reader to a bare template outside
    the app they left from. Same split as every other navigation in this file."""
    body = code[code.index("function backHere()"):]
    body = body[:body.index("\n}")]
    assert "window.top" in body
    assert "host.location.pathname + host.location.search" in body
    # and the fallback for a cross-origin or absent parent, where the read throws
    assert "location.pathname + location.search" in body


def test_the_configuration_of_this_chat_does_not_travel(code):
    """A task runs unattended, and the page owns those answers for its own reasons —
    "ask every time" cannot work with nobody watching. Sending the chat's own model,
    effort or approvals mode would make the modal a second composer with a worse box
    and a rule it did not choose."""
    body = _open_scheduler(code)
    for leaked in ("curModel(", "curEffort(", "permission_mode", "perm-sel",
                   "repeats", "due"):
        assert leaked not in body, f"{leaked} has no business in the handoff"


def test_it_navigates_the_TOP_window_through_the_shell(code):
    """The target is a shell ROUTE, not a param on this view and not an fs path,
    and this page sets no param boundary — so fused.params addresses the shell's
    query string but has nothing to say about its path. Same standard-break, and
    the same two lines, as markdown/graph's `navigateShell`."""
    body = _open_scheduler(code)
    assert "window.top" in body
    assert "history.pushState" in body
    assert '"fused:navigate"' in body        # the in-app hop, not a page load
    assert "location.href = url" in body     # the cross-origin / no-parent fallback


def test_the_button_does_not_reimplement_scheduling(code):
    """One writer for the schedule store, and it is the page. If this grew a fetch
    it would be a second, competing create path — with a different set of defaults,
    in a file that used to have one and was cut back for exactly that reason."""
    body = _open_scheduler(code)
    assert "fetch(" not in body
    assert "/api/schedule" not in body


# ------------------------------------------------------------- the page


def test_the_page_reads_the_params_the_template_writes(code, page):
    """The contract, spelled in two files. A rename on either side leaves the
    button navigating to a Schedule page that simply ignores it — no error, no
    modal, nothing to debug from."""
    assert 'SCHEDULE_URL = "/scheduled"' in code
    assert 'q.get("new") !== "1"' in page
    for param in ("target", "message", "session_id", "back"):
        assert f'q.get("{param}")' in page, f"the page ignores {param}"


def test_the_link_opens_the_form_immediately(page):
    """Landing on a page with a button still to press would make one control
    read as two."""
    effect = page[page.index('q.get("new")'):]
    effect = effect[:effect.index("}, []);")]
    assert "setCreating(new Date(" in effect
    assert "setNewTarget(" in effect


def test_the_prefilled_time_is_valid_the_moment_it_opens(page):
    """The field is minute-precision and the form refuses a due time at or
    before now, so a value inside the CURRENT minute opens the modal already
    complaining about a time the user never picked."""
    assert "NEW_LINK_LEAD_MS" in page
    lead = re.search(r"const NEW_LINK_LEAD_MS = ([0-9_]+);", page)
    assert lead, "the lead constant moved"
    assert int(lead.group(1).replace("_", "")) >= 60_000


def test_the_params_are_consumed_not_just_read(page):
    """Otherwise a reload — or Back to here from wherever the user went next —
    reopens the modal forever. replaceState, not push: the deep-linked URL is
    not a place worth keeping in the history."""
    effect = page[page.index('q.get("new")'):]
    effect = effect[:effect.index("}, []);")]
    for param in ("new", "target", "message", "session_id", "back"):
        assert f'q.delete("{param}")' in effect, f"{param} outlives its own navigation"
    assert "history.replaceState(" in effect
    assert "pushState" not in effect


def test_the_deep_linked_values_do_not_outlive_their_own_modal(page):
    """Left standing they would prefill the next "+ New task" with a folder, a
    draft and a session the user arrived from some time ago — the same class of bug
    as a When pill that survived its send."""
    close = page[page.index("onClose={() => {"):]
    close = close[:close.index("}}")]
    for setter in ("setNewTarget(null)", "setNewMessage(null)",
                   "setNewSession(null)", "setNewBack(null)"):
        assert setter in close, f"{setter} missing from the modal's close"


# ------------------------------------------------------------- the modal


def test_the_link_beats_the_guess_and_an_edit_beats_the_link(modal):
    """Three sources for one field, in order: a stored target (Edit), the folder
    a link named, and only then DEFAULT_TARGET_SUFFIX — which is a guess, and a
    guess is what you offer when nobody said."""
    assert modal.count('editing?.target ?? initialTarget ?? ""') == 2, \
        "the state and the dirty baseline must be the same expression"
    # the async default only fills a still-EMPTY field, which is what keeps it
    # from clobbering the link's target when getConfig resolves
    effect = modal[modal.index("getConfig().then("):]
    effect = effect[:effect.index("}, []);")]
    assert 'prev === "" ? fallback : prev' in effect
    assert 'prev.target === "" ?' in effect


def test_a_prefilled_target_does_not_read_as_dirty(modal):
    """The chassis' close-twice guard must fire on "the user typed something".
    Counting a prefill as dirty is what made ✕ look broken (QA 2026-08-14),
    twice already — this is the same bug one prefill earlier."""
    baseline = modal[modal.index("const [initial, setInitial] = useState(() => ({"):]
    baseline = baseline[:baseline.index("}));")]
    assert "initialTarget" in baseline

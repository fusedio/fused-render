"""The composer's Schedule BUTTON — the hop from the chat to the Schedule page
(templates/claude/template.html -> frontend/src/shell/Scheduled.tsx).

Not to be confused with the "Send now" pill beside it, which
test_claude_schedule_pill.py covers: the pill defers ONE message from here, with
no title, no description and no repeat rule. Everything past that is a form, and
the form lives on the Schedule page. The button exists because the page cannot
know the one thing this template always knows — which folder — so it carries
exactly that across and nothing else.

Structural assertions over the two sources, the same approach
test_claude_kind.py and the pill's own suite take: this is inline vanilla JS in a
10000-line document on one side and a React page on the other, so what can be
pinned is that the wiring exists and that the contract between the two halves
agrees. The contract IS duplicated — two param names spelled in two files (D146:
a duplicated rule needs a test, not a comment) — which is most of what is below.
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


# ------------------------------------------------------------- the template


def test_the_scheduler_lives_in_the_kebab_not_the_composer(code):
    """Moved out of the composer's pill row into the ⋮ menu, under the terminal
    item (Akshil, 2026-08-14): planning a task is a step OUT of the
    conversation, and the menu of ways out is where a step out belongs. The
    composer keeps only what acts on THIS message — so no sched pill remains,
    and the menu item sits inside #kebabpop after #terminalopt."""
    assert "sched-btn" not in code
    assert 'id="schedopt"' in code
    pop = re.search(r'<div id="kebabpop".*?</div>', code, flags=re.S).group(0)
    assert 'id="schedopt"' in pop
    assert pop.index('id="terminalopt"') < pop.index('id="schedopt"')


def test_the_menu_item_is_a_button_and_closes_the_menu(code):
    """type="button" for the composer-submit reason every control here carries,
    and the click closes the kebab before navigating — a menu left open under a
    page that is going away is the kind of thing only a test remembers."""
    match = re.search(r"<button[^>]*id=\"schedopt\"[^>]*>", code, flags=re.S).group(0)
    assert 'type="button"' in match
    wiring = code[code.index('getElementById("schedopt")'):]
    wiring = wiring[:wiring.index("});")]
    assert "kebabClose()" in wiring
    assert "openScheduler()" in wiring


def test_the_hop_carries_the_target_and_nothing_else(code):
    """The chat knows the folder; the page owns the composing. Sending the draft,
    the model or the approvals mode along would make the modal a second chat
    composer with a worse box."""
    body = code[code.index("function openScheduler()"):]
    body = body[:body.index("\n}")]
    assert "encodeURIComponent(FILE)" in body
    assert "new=1" in body
    assert "target=" in body
    for leaked in ("box.value", "homebox.value", "whenChoice(", "permission_mode"):
        assert leaked not in body, f"{leaked} has no business in the handoff"


def test_it_navigates_the_TOP_window_through_the_shell(code):
    """The target is a shell ROUTE, not a param on this view and not an fs path,
    and this page sets no param boundary — so fused.params addresses the shell's
    query string but has nothing to say about its path. Same standard-break, and
    the same two lines, as markdown/graph's `navigateShell`."""
    body = code[code.index("function openScheduler()"):]
    body = body[:body.index("\n}")]
    assert "window.top" in body
    assert "history.pushState" in body
    assert '"fused:navigate"' in body        # the in-app hop, not a page load
    assert "location.href = url" in body     # the cross-origin / no-parent fallback


def test_the_button_does_not_reimplement_the_pills_scheduling(code):
    """One writer for the schedule store. The button is a LINK; if it grew a
    fetch it would be a second, competing create path — with a different set of
    defaults, in a file that already has one."""
    body = code[code.index("function openScheduler()"):]
    body = body[:body.index("\n}")]
    assert "fetch(" not in body
    assert "/api/schedule" not in body


# ------------------------------------------------------------- the page


def test_the_page_reads_the_params_the_template_writes(code, page):
    """The contract, spelled in two files. A rename on either side leaves the
    button navigating to a Schedule page that simply ignores it — no error, no
    modal, nothing to debug from."""
    assert '"/scheduled"' in code or "SCHEDULE_URL = \"/scheduled\"" in code
    assert 'q.get("new") !== "1"' in page
    assert 'q.get("target")' in page


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
    assert 'q.delete("new")' in effect
    assert 'q.delete("target")' in effect
    assert "history.replaceState(" in effect
    assert "pushState" not in effect


def test_the_deep_linked_folder_does_not_outlive_its_own_modal(page):
    """Left standing it would prefill the next "+ New task" with a folder the
    user arrived from some time ago — the same class of bug as a When pill that
    survived its send."""
    close = page[page.index("onClose={() => { setCreating(null)"):]
    close = close[:close.index("}}")]
    assert "setNewTarget(null)" in close


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

"""Things about the scheduled-messages list that only CSS can get wrong.

Nothing in the suite renders CSS, so layout claims the markup cannot carry are
pinned here by reading the stylesheet's own numbers rather than restating them
— tuning a value keeps the test honest and only breaking the *invariant*
fails it.

1. **The wide sections stay wide, the prose stays narrow.** The page is built
   out of the settings vocabulary, and `.prefs-page > *` caps children at
   760px — right for text, 108px too narrow for the calendar/board surfaces,
   which is how the first card grid silently sat two-up. The opt-out and the
   prose re-cap have to survive together.

2. **A failed run stays legible.** The list's rows carry error/missed as a
   coloured edge; if those modifiers stop painting one, the only remaining
   signal is a small pill in a column of identical rows.

`tests/test_theme.py` established reading stylesheet source this way.
"""
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CSS = os.path.join(REPO_ROOT, "frontend", "src", "styles", "schedule.css")
_PAGE = os.path.join(REPO_ROOT, "frontend", "src", "shell", "Scheduled.tsx")
_PREFS_CSS = os.path.join(REPO_ROOT, "frontend", "src", "styles", "preferences.css")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _decl(css: str, selector: str, prop: str) -> str | None:
    """The value of `prop` in the block for exactly `selector`, or None.

    Deliberately literal: it matches the selector as written, so
    `.schedule-row` and `.schedule-row--error` are two different blocks and a
    rule cannot be found by accident through a prefix."""
    pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    for body in re.findall(pattern, css):
        found = re.search(rf"(?<![\w-]){re.escape(prop)}\s*:\s*([^;]+);", body)
        if found:
            return found.group(1).strip()
    return None


# -- 1. two widths, both on purpose -------------------------------------------

def test_the_sections_opt_out_of_the_prose_column():
    granted = _decl(_read(_CSS), ".schedule-page > .prefs-section", "max-width")
    assert granted, (
        "the schedule sections must opt out of the .prefs-page content column; "
        "without this rule the calendar and board inherit 760px")
    # Full width now (Akshil, 2026-08-16): the opt-out is `none`, or any cap
    # comfortably past the 760px prose column.
    assert granted == "none" or int(re.match(r"(\d+)px", granted).group(1)) > 760


def test_the_page_actually_carries_the_class_that_rule_hangs_off():
    """A stylesheet rule for a class no element has is a rule that does nothing —
    and looks entirely correct in review."""
    assert 'className="prefs-page schedule-page"' in _read(_PAGE)


def test_the_column_cap_this_works_around_is_still_there():
    """The whole point of the widening rule is that something else narrows the
    page. If that cap is ever lifted, this file's premise is stale and the
    override is dead weight — better to be told than to leave it lying around."""
    assert _decl(_read(_PREFS_CSS), ".prefs-page > *", "max-width") == "760px"


def test_prose_in_the_widened_sections_keeps_the_narrow_measure():
    """Widening the section must not widen the paragraphs inside it — a line of
    body text running the full 1120px is worse than a cramped calendar was."""
    assert _decl(_read(_CSS), ".schedule-page .prefs-section > p", "max-width")


# -- 2. failure states stay legible in the tree/board views --------------------
# The Inbox-style row list is gone (the List view is the flow-app tree, the
# Board its kanban — ScheduleTaskViews.tsx). A failed run now folds into the
# Done column, so the ONLY thing separating it from a clean run is the red
# status ring — which makes that ring the invariant worth pinning.

def test_a_failed_run_does_not_wear_the_done_green():
    """`.schedule-ring--failed` must repaint the ring via its own token; if it
    ever stops, a dead turn reads as a clean run in a column of green rings."""
    css = _read(_CSS)
    failed = _decl(css, ".schedule-ring--failed,\n.schedule-ring--failed.schedule-ring--done", "color")
    assert failed and "var(--status-failed)" in failed, (
        "the failed ring must carry var(--status-failed), or a failed run is "
        "indistinguishable from a clean one inside the Done column")


def test_the_failed_override_outweighs_the_done_hue():
    """Both classes sit on one element; the override only wins while its
    selector stays MORE specific than `.schedule-ring--done`'s. The compound
    selector is that guarantee — pin its presence, not the cascade."""
    assert ".schedule-ring--failed.schedule-ring--done" in _read(_CSS)


def test_tree_titles_hold_one_line():
    """The tree's scan column: a multi-line prompt must clamp to one ellipsised
    line or long tasks push every row below them out of rhythm."""
    css = _read(_CSS)
    assert _decl(css, ".schedule-tv-title", "white-space") == "nowrap"
    assert _decl(css, ".schedule-tv-title", "text-overflow") == "ellipsis"


# -- 3. the calendar popover and its chips -------------------------------------
# The popover is the calendar's whole detail surface, and three of its claims
# are pixels rather than markup.

_CAL = os.path.join(REPO_ROOT, "frontend", "src", "shell", "ScheduleCalendar.tsx")
_TOKENS = os.path.join(REPO_ROOT, "frontend", "src", "styles", "tokens.css")


def test_the_popover_title_outranks_the_facts_under_it():
    """Title over description as ONE written block, and the title a real step
    bigger than the 12px fact rows below — at 14px against 12px it was the same
    size as its own metadata, which is not a hierarchy."""
    css = _read(_CSS)
    size = _decl(css, ".schedule-pop-write .schedule-pop-title", "font-size")
    assert size and int(re.match(r"(\d+)px", size).group(1)) >= 16
    rows = _decl(css, ".schedule-pop-rows", "font-size")
    assert rows == "12px"
    assert _decl(css, ".schedule-pop-desc", "color") == "var(--fg-muted)"


def test_the_description_is_prose_and_wears_no_icon():
    """It is read, not recognised, so it does not belong in the icon column with
    the folder and the repeat rule."""
    cal = _read(_CAL)
    assert '<p className="schedule-pop-desc">{task.description}</p>' in cal
    # The notes glyph is no longer led by the description row anywhere.
    assert "{ICON_NOTES}" not in cal


def test_the_occurrence_row_does_not_repeat_the_panel():
    """A popover about ONE task printed its title again on the row and its
    status word again beside it. The ring carries the state; the tooltip carries
    the word."""
    cal = _read(_CAL)
    assert '<span className="schedule-cal-msg-state">' not in cal
    # The body is kept only when it says something the title has not.
    assert 'const showBody = body && body !== headline ? body : "";' in cal
    assert "status.label" in cal, "the word must survive in the row's tooltip"


@pytest.mark.parametrize("status,token", [
    ("upcoming", "--status-upcoming"),
    ("in_progress", "--status-progress"),
    ("done", "--status-done"),
    ("failed", "--status-failed"),
    ("archived", "--status-archived"),
])
def test_every_status_pill_wears_its_own_status_token(status, token):
    """One vocabulary means one palette. The pill and the ring beside it read
    the SAME token, so green cannot mean two things two lines apart."""
    css = _read(_CSS)
    assert _decl(css, f".schedule-state--{status}", "--pill") == f"var({token})"
    # And the token is real, not a name nothing defines.
    assert f"{token}:" in _read(_TOKENS)


def test_the_pill_tints_rather_than_fills():
    """Text first: the label keeps the hue at full strength and the hue sits
    behind it at a weight that survives both themes. A saturated fill would need
    a second text colour per status per theme."""
    css = _read(_CSS)
    selector = (".schedule-state--upcoming,\n.schedule-state--in_progress,\n"
                ".schedule-state--done,\n.schedule-state--failed,\n"
                ".schedule-state--archived")
    assert _decl(css, selector, "color") == "var(--pill)"
    background = _decl(css, selector, "background")
    assert background and background.startswith("color-mix(")


def test_a_running_chip_says_so_and_stops_saying_it_on_request():
    """The calendar has no In Progress lane, so the chip itself has to carry
    the one fact that is only true while you are looking at it. Motion is the
    signal — every static property on a chip is already spent on the project
    hue, the projected dashes and the past fade — and a reader who has asked for
    less of it still gets a static highlight."""
    css = _read(_CSS)
    assert "@keyframes schedule-cal-shimmer" in css
    running = ".schedule-cal .schedule-cal-chip.is-running::after"
    assert _decl(css, running, "animation") == "schedule-cal-shimmer 2s linear infinite"
    # It must not eat the chip's own clicks: the chip is a button.
    assert _decl(css, running, "pointer-events") == "none"
    # The fallback is INSIDE a reduced-motion block, not merely after one: the
    # whole point is that it applies only to the reader who asked for it.
    blocks = re.findall(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}\n",
        css, re.DOTALL)
    guarded = [b for b in blocks if "is-running" in b]
    assert guarded, "the static fallback must sit inside a reduced-motion block"
    assert "animation: none" in guarded[0]
    # And the rule hangs off a class the view actually sets.
    #
    # Asked of the CHIP's whole day, not of its anchor (bugbot, 2026-08-18). A
    # chip is one task on one day, anchored at that day's EARLIEST message, so
    # the anchor is the wrong occurrence twice over: a day whose 05:00 run has
    # finished and whose 14:00 run is in flight was asking about the finished
    # one, and the mark landed on whichever day held the task's newest row —
    # routinely tomorrow's pending occurrence. tasks-lib.isRunningIn asks every
    # message under the chip; the class, the CSS above and the reduced-motion
    # fallback are untouched.
    assert 'isRunningIn(chip.task, chip.messages) ? " is-running" : ""' in _read(_CAL)

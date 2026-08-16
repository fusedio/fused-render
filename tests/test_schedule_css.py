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
    assert int(re.match(r"(\d+)px", granted).group(1)) > 760


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


# -- 2. failure states stay legible in the list --------------------------------

@pytest.mark.parametrize("state,token", [("error", "--error"), ("missed", "--warning")])
def test_row_failure_states_paint_an_edge(state, token):
    """The list view's rows flag error/missed with a coloured left stripe; a
    pill alone disappears in a column of rows that are otherwise identical."""
    stripe = _decl(_read(_CSS), f".schedule-row--{state}", "border-left")
    assert stripe, f".schedule-row--{state} must paint its edge"
    assert f"var({token})" in stripe, (
        f"the {state} stripe is {stripe!r}, which does not carry var({token}) — "
        f"the state is no longer legible at a glance")


def test_row_titles_hold_one_line():
    """The row's scan column: a multi-line prompt must clamp to one ellipsised
    line or long tasks push every row below them out of rhythm."""
    css = _read(_CSS)
    assert _decl(css, ".schedule-row-title", "white-space") == "nowrap"
    assert _decl(css, ".schedule-row-title", "text-overflow") == "ellipsis"

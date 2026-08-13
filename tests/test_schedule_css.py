"""Two things about the scheduled-messages cards that only CSS can get wrong.

Both were shipped and both were invisible to every other test in the suite,
because nothing here renders CSS — the page's markup was correct in each case.
They are pinned by reading the stylesheet's own numbers rather than restating
them, so tuning a value keeps the test honest and only breaking the *invariant*
fails it.

1. **The grid gets the width its column count needs.** The page is built out of
   the settings vocabulary, and `.prefs-page > *` caps children at 760px. A
   `minmax(280px, …)` auto-fill grid needs 868px before a third track appears, so
   the cards quietly sat two-up on any window — the layout was right, the
   container was not. Arithmetic between two rules in two files is exactly the
   kind of thing nobody re-checks after changing one of them.

2. **A tinted border survives hover.** `.schedule-card:hover` is two selectors to
   `.schedule-card--error`'s one, so the generic hover border wins and pointing at
   a failed card erases the mark that says it failed.

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
    `.schedule-card` and `.schedule-card:hover` are two different blocks and a
    rule cannot be found by accident through a prefix."""
    pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    for body in re.findall(pattern, css):
        found = re.search(rf"(?<![\w-]){re.escape(prop)}\s*:\s*([^;]+);", body)
        if found:
            return found.group(1).strip()
    return None


# -- 1. the cards get the width they are laid out for -------------------------

def test_card_section_is_wide_enough_for_the_grid_it_declares():
    """The section holding the cards must fit the column count the grid asks for.

    Both numbers are read out of the source: the grid's own min track size and
    gap, and the width the page grants the section. If someone widens the track
    without widening the section, the third column silently disappears again —
    which is the bug this pins, not a hypothetical."""
    css = _read(_CSS)

    tracks = _decl(css, ".schedule-cards", "grid-template-columns")
    assert tracks, ".schedule-cards must declare its columns"
    min_track = re.search(r"minmax\(\s*(\d+)px", tracks)
    assert min_track, f"expected a minmax() min track size, got {tracks!r}"
    min_px = int(min_track.group(1))

    gap = _decl(css, ".schedule-cards", "gap")
    gap_px = int(re.match(r"(\d+)px", gap).group(1))

    granted = _decl(css, ".schedule-page > .prefs-section", "max-width")
    assert granted, (
        "the card sections must opt out of the .prefs-page content column; "
        "without this rule they inherit 760px and the grid loses a column")
    granted_px = int(re.match(r"(\d+)px", granted).group(1))

    # Three columns is the claim the stylesheet comment makes; hold it to that.
    needed = 3 * min_px + 2 * gap_px
    assert granted_px >= needed, (
        f"{granted_px}px holds only {(granted_px + gap_px) // (min_px + gap_px)} "
        f"tracks of {min_px}px; three need {needed}px")


def test_the_page_actually_carries_the_class_that_rule_hangs_off():
    """A stylesheet rule for a class no element has is a rule that does nothing —
    and looks entirely correct in review."""
    assert 'className="prefs-page schedule-page"' in _read(_PAGE)


def test_the_column_cap_this_works_around_is_still_there():
    """The whole point of the widening rule is that something else narrows the
    page. If that cap is ever lifted, this file's arithmetic is stale and the
    override is dead weight — better to be told than to leave it lying around."""
    assert _decl(_read(_PREFS_CSS), ".prefs-page > *", "max-width") == "760px"


def test_prose_in_the_widened_sections_keeps_the_narrow_measure():
    """Widening the section must not widen the paragraphs inside it — a line of
    body text running the full 1120px is worse than the two-up grid was."""
    assert _decl(_read(_CSS), ".schedule-page .prefs-section > p", "max-width")


# -- 2. state tints survive hover ---------------------------------------------

def _state_modifiers(css: str) -> list[str]:
    """Every `.schedule-card--x` that paints a border, from the source itself."""
    found = re.findall(r"\.schedule-card--([a-z]+)\s*\{([^}]*)\}", css)
    return sorted({name for name, body in found if "border-color" in body})


def test_there_are_state_modifiers_to_check():
    """A guard on the guard: if the modifiers are ever renamed, the loop below
    would pass by iterating over nothing."""
    assert _state_modifiers(_read(_CSS)) == ["error", "missed"]


@pytest.mark.parametrize("state", ["error", "missed"])
def test_state_border_is_not_erased_by_hover(state):
    css = _read(_CSS)
    assert _decl(css, ".schedule-card:hover", "border-color"), (
        "this test only means something while the generic card hover repaints "
        "the border")
    assert _decl(css, f".schedule-card--{state}:hover", "border-color"), (
        f".schedule-card--{state} sets a border-color, but nothing restates it "
        f"on hover: `.schedule-card:hover` is the more specific selector, so "
        f"hovering the card replaces the state tint with the neutral one")


@pytest.mark.parametrize("state", ["error", "missed"])
def test_hover_state_border_still_reads_as_that_state(state):
    """Restating the tint on hover only helps if it is still the SAME colour —
    a hover rule that hard-coded the neutral mix would pass the test above and
    reintroduce the bug."""
    css = _read(_CSS)
    resting = _decl(css, f".schedule-card--{state}", "border-color")
    hovered = _decl(css, f".schedule-card--{state}:hover", "border-color")
    if hovered is None:
        # The test above is the one that reports a missing rule; this one has
        # nothing to say about it, and should not add a second confusing failure
        # (or a TypeError) to the same root cause.
        pytest.skip("no hover rule to check — see test_state_border_is_not_erased_by_hover")
    token = re.search(r"var\(--[\w-]+\)", resting).group(0)
    assert token in hovered, (
        f"hovered {state} border is {hovered!r}, which does not mention "
        f"{token} — the state is no longer legible under the cursor")

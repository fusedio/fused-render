"""Things about the New task card's WRITING SURFACE that only CSS can get wrong.

The title and the description share one borderless area (`.new-task-write` in
`frontend/src/styles/new-task.css`), which means the usual signals a field has —
a border, a box, a focus ring — are deliberately absent and the few that remain
carry the whole design. Nothing in the suite renders CSS, so the invariants are
pinned by reading the stylesheet's own numbers rather than restating them:
retuning a value keeps these tests honest, and only breaking the *invariant*
fails one.

Four invariants, all from the same run of reviews (Akshil, 2026-08-17):

1. **The surface starts FLUSH.** There is no left gutter and no accent rule
   standing in one. The pair used to sit 10px in behind a 2px accent bar drawn
   in a strip a negative margin opened up; that read as a gutter nothing else on
   the card has.

2. **Neither field has a fill.** Not at rest, not on hover, and — the last one
   to go — not on focus: "don't do this background highlight thing for the input
   fields of title and description". A tinted rectangle is a box drawn in one
   colour, so a `--ctl-quiet-bg` wash undid the borderlessness the rest of this
   file buys. Focus is the caret now, which is what WCAG 2.4.7 accepts for a text
   field; the one ring left is the forced-colours one, where the caret is drawn in
   a palette this stylesheet cannot check. Pinned as "no background", not as
   "no rules" — the whole point is that removing the wash did not quietly bring
   back a border, a shadow or a second fill under another name.

3. **The caret is the ordinary one.** No `caret-color` override anywhere on this
   surface — a text cursor takes `color`, the way `.field-control` leaves it, and
   the accent override drew a yellow-green cursor found nowhere else in the app.
   Load-bearing twice over now that the caret is the entire focus signal.

4. **The description reads as body text under a heading.** Two faces, not two
   sizes of the same face: the numbers are read out of the file and compared, so
   the test says "clearly smaller" rather than "13px".

`tests/test_theme.py` established reading stylesheet source this way, and
`tests/test_schedule_css.py` is the sibling suite for the list page.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CSS = os.path.join(REPO_ROOT, "frontend", "src", "styles", "new-task.css")

_FIELD = ".new-task-write .new-task-field"
_TITLE = ".new-task-write .new-task-title"
_ASK = ".new-task-write .new-task-ask"


def _source() -> str:
    """The stylesheet with its comments removed.

    Not optional here: this file's comments quote the very declarations these
    tests assert are gone (they explain what replaced `margin-inline: -10px` and
    why `caret-color` went), so a parser that read comments would find every
    removed property still present."""
    with open(_CSS, encoding="utf-8") as f:
        return re.sub(r"/\*.*?\*/", "", f.read(), flags=re.S)


def _block(css: str, selector: str) -> str:
    """The body of the rule for exactly `selector`, or "".

    Deliberately literal, like test_schedule_css._decl: `…new-task-field` and
    `…new-task-field:focus` are two different blocks and neither can be found
    by accident through the other."""
    found = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return found.group(1) if found else ""


def _decl(css: str, selector: str, prop: str) -> str | None:
    body = _block(css, selector)
    found = re.search(rf"(?<![\w-]){re.escape(prop)}\s*:\s*([^;]+);", body)
    return found.group(1).strip() if found else None


def _fills(css: str) -> list[tuple[str, str]]:
    """Every background declaration in the file, paired with the selector of the
    rule it sits in.

    Broader than `_decl` on purpose: invariant 2 is about the whole surface, so a
    fill smuggled in under `:hover`, under a grouped selector or inside an
    at-rule has to be caught as readily as one on the plain rule. The selector is
    the text between the rule's own `{` and whatever ended the thing before it —
    the previous rule's `}` or an enclosing at-rule's `{`, whichever is nearer,
    which is what makes a rule nested in `@media` report its own selector rather
    than the media query's."""
    out = []
    for found in re.finditer(r"(?<![\w-])background(?:-color|-image)?\s*:\s*([^;}]+)", css):
        brace = css.rfind("{", 0, found.start())
        start = max(css.rfind("}", 0, brace), css.rfind("{", 0, brace)) + 1
        out.append((" ".join(css[start:brace].split()), found.group(1).strip()))
    return out


_SURFACE = ("new-task-field", "new-task-title", "new-task-ask")


def _px(value: str) -> int:
    """A length in px. A bare `0` is a length too — CSS drops the unit on zero,
    and `padding: 6px 0` is exactly how "no horizontal padding" is written."""
    found = re.fullmatch(r"(\d+)(?:px)?", value.strip())
    assert found, f"not a px length: {value!r}"
    return int(found.group(1))


# -- 1. no gutter, and nothing standing in one --------------------------------

def test_the_surface_has_no_horizontal_padding_to_hang_a_rule_in():
    padding = _decl(_source(), _FIELD, "padding")
    assert padding, "the writing surface must still declare its own padding"
    parts = padding.split()
    # One value is symmetric and fine; two means "vertical horizontal", and the
    # horizontal half has to be zero for the text to sit on the card's gutter.
    assert len(parts) <= 2, f"unexpected padding shorthand: {padding!r}"
    if len(parts) == 2:
        assert _px(parts[1]) == 0, (
            "the fields must start flush with the card's 16px gutter; horizontal "
            f"padding here reopens the left gutter (padding: {padding})")


def test_nothing_pulls_the_fields_back_out_into_a_gutter():
    css = _source()
    body = _block(css, _FIELD)
    assert body, "the shared field rule went missing"
    # The negative margin existed only to cancel the horizontal padding above and
    # to open a strip for the focus bar. Both are gone; a negative inline margin
    # now would push the text off the card's own line.
    for prop in ("margin-inline", "margin-left", "margin-inline-start"):
        assert _decl(css, _FIELD, prop) is None, (
            f"{prop} on the writing surface reintroduces the left gutter")


# -- 2. no fill, in any state -------------------------------------------------

def test_the_fields_declare_themselves_unfilled():
    # `transparent` is DECLARED rather than left off: deploy.css paints
    # `background: var(--bg)` onto every unarmored field in a modal, so silence
    # here is a recessed box, not a bare surface.
    css = _source()
    assert _decl(css, _FIELD, "background") == "transparent", (
        "the writing surface must declare `background: transparent` — omitting it "
        "lets deploy.css's modal-field fill through")
    assert _decl(css, _FIELD, "background-color") is None, (
        "one background declaration, not two arguing")


def test_focus_is_not_a_background():
    css = _source()
    focus = _block(css, _FIELD + ":focus")
    # The rule still exists — it declares `outline: none`, which is the statement
    # that this surface does not ring. What it must not declare is a fill.
    assert focus, (
        "the :focus rule states outline:none deliberately; deleting it hands the "
        "decision to whatever deploy.css says")
    for prop in ("background", "background-color", "background-image"):
        assert _decl(css, _FIELD + ":focus", prop) is None, (
            f"{prop} on focus is the --ctl-quiet-bg wash coming back: a tinted "
            "rectangle is a box, which is the one thing these fields must not "
            "grow when you type in them")
    # And the chrome the wash itself replaced stays gone — a removed fill must not
    # be swapped for the bar, a shadow or an underline.
    assert _decl(css, _FIELD + ":focus", "box-shadow") is None, (
        "the 2px accent bar down the left edge was removed with the gutter it "
        "was drawn in; a box-shadow is how it would come back")
    for prop in ("border-left", "border-inline-start", "border", "border-bottom"):
        assert _decl(css, _FIELD + ":focus", prop) is None, (
            f"{prop} puts the box back on a surface whose whole point is not "
            "having one")


def test_no_rule_anywhere_fills_the_writing_surface():
    # The broad form of the two above, so a fill cannot slip in under `:hover`, a
    # grouped selector, or a state nobody thought to write a test for.
    for selector, value in _fills(_source()):
        if not any(name in selector for name in _SURFACE):
            continue
        assert value in ("transparent", "none"), (
            f"{selector} fills a writing-surface field ({value}); title and "
            "description sit directly on the card in every state")


def test_forced_colours_still_rings_the_focused_field():
    # The one ring that survives, and the only state where a ring is right: the
    # caret carries focus everywhere else, but in forced-colours mode it is drawn
    # by the OS in a palette this stylesheet cannot see, let alone verify.
    css = _source()
    cut = css.find("@media (forced-colors: active)")
    assert cut != -1, "the forced-colours handling for this surface went missing"
    outline = _decl(css[cut:], _FIELD + ":focus", "outline")
    assert outline and "Highlight" in outline, (
        "forced-colours focus must ring the field in the system Highlight colour, "
        f"not in a token the mode discards (outline: {outline})")


def test_no_accent_is_painted_on_the_writing_surface_at_all():
    # The broad version of the three above: the surface used to be the one place
    # in the app that wore --accent as chrome rather than as a control's fill.
    css = _source()
    for selector in (_FIELD, _FIELD + ":focus", _TITLE, _ASK):
        assert "--accent" not in _block(css, selector), (
            f"{selector} paints the accent colour; the writing surface is quiet "
            "now — the accent belongs to the checkboxes and the Save button")


# -- 2. the ordinary caret ----------------------------------------------------

def test_the_caret_is_whatever_every_other_field_gets():
    # `.field-control` never sets caret-color, so a caret takes `color` — and the
    # writing surface declares `color: var(--fg)`. Any override here is a cursor
    # this app has in exactly one place.
    assert "caret-color" not in _source(), (
        "no caret-color override on the New task fields: the caret follows "
        "`color`, the way every other input in the app leaves it")


# -- 3. a heading and body text, not two headings -----------------------------

def test_the_description_is_clearly_smaller_than_the_title():
    css = _source()
    title = _decl(css, _TITLE, "font-size")
    ask = _decl(css, _ASK, "font-size")
    assert title and ask, "both fields declare their own face; one stopped"
    # Read, not restated: retuning either size keeps this passing as long as the
    # hierarchy survives. A couple of steps down the repo's ladder — the title is
    # a heading, the description is body copy at the same size as the rest of the
    # form's controls — which is comfortably more than a single step.
    assert _px(ask) <= _px(title) - 5, (
        f"the description ({ask}) has to read as body text under the title "
        f"({title}), not as a second heading")


def test_the_ask_clamps_are_expressed_in_its_own_face():
    # The floor and the ceiling are line counts of the description's face, so
    # they have to move WITH the font-size — a stale 14px in the calc would size
    # the block for a face the field no longer has.
    css = _source()
    ask_size = _decl(css, _ASK, "font-size")
    for prop in ("min-height", "max-height"):
        clamp = _decl(css, _ASK, prop)
        assert clamp and ask_size in clamp, (
            f"{prop} on the description must be computed from its own "
            f"{ask_size} face, not from whatever it used to be ({clamp!r})")

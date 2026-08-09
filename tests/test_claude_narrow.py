"""The `claude` template's NARROW layout (one view at a time below 800px).

Three separate defects live here, and each of them is invisible to a reading of
the stylesheet alone — which is why the pins below are about the *mechanism*, not
about the presence of a rule:

* **the collapse must not be outrankable.** Preview view keeps only the
  `#anntools` strip of the chat column, and it does that with
  `body.view-preview #chat > *:not(#anntools) { display: none }`. An INLINE
  `style="display:…"` on any of those children beats that rule outright, and the
  usual escape hatch is closed: `!important` is FORBIDDEN in this file (D146,
  enforced by tests/test_claude_shots.py). The composer form carried exactly such
  an inline declaration, so the whole composer — textarea, three selects, the
  the send button — rendered under the strip and ate the height
  the preview was supposed to get. The rule is now a stylesheet rule the media
  block can override, and the test is the general one: no child of `#chat` may
  carry an inline `display`.
* **crossing the breakpoint is not a view flip, and the reset has to fire for
  it too.** Arming annotate mode in the WIDE layout and then narrowing the pane
  hides the control while leaving the state armed. The reset guard used to test
  `narrowShown === true`, which only ever describes a Preview→Chat flip; a media
  crossing arrives with `narrowShown === false` from the wide boot call. It now
  tests `narrowShown !== null`, which still excludes BOOT — writing `annmode=0`
  there would clobber the ON-by-default the wide layout shares.
* **an auto-submitted note must not park a permission card in a hidden `#log`.**
  Saving an annotation sends immediately, and in Preview view the transcript is
  collapsed with the rest of the chat column. A permission card IS the prompt —
  the subprocess is blocked on it — so a card landing in a hidden `#log` stalls
  the turn with nothing on screen to click.

The node harness is the same extraction the other claude suites use (see
tests/test_claude_shots.py `_node`): named top-level functions are lifted out of
the page and run under node against stubs, because what matters is the decision
the function reaches, not the DOM it reached it in.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "claude", "template.html")


@pytest.fixture(scope="module")
def html():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _style_block(html):
    """The template's own <style> text (there is exactly one)."""
    start = html.index("<style>")
    return html[start:html.index("</style>", start)]


def _narrow_block(html):
    """The `@media (max-width: 800px)` block's body."""
    m = re.search(r"@media \(max-width: 800px\) \{", html)
    assert m, "the 800px media block is what makes the narrow layout exist"
    depth, i = 1, m.end()
    while depth:
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
        i += 1
    return html[m.end():i - 1]


# ------------------------------------------------ 1. the collapse cannot be outranked

def test_no_child_of_the_chat_column_carries_an_inline_display(html):
    """An inline declaration outranks every stylesheet rule short of `!important`,
    and `!important` is banned in this file (D146) — so an inline `display` on a
    child of #chat makes `body.view-preview #chat > *:not(#anntools)` a dead
    letter for that child, with no way to fix it in the media block.

    Asserted over the whole document rather than over the four children that
    exist today: the next element added to the column has to obey the same rule,
    and the failure mode (the preview silently loses its height) does not look
    like a CSS bug when it happens.
    """
    body = html[html.index("<body>"):]
    offenders = [m.group(0) for m in re.finditer(r"<[a-z]+[^>]*\bstyle=\"[^\"]*\"[^>]*>", body)
                 if "display" in m.group(0).split('style="')[1].split('"')[0]]
    assert offenders == [], (
        "inline display on markup inside the chat column: the narrow Preview "
        "collapse cannot override it and !important is forbidden here (D146)\n"
        + "\n".join(offenders))


def test_the_composer_form_gets_its_display_from_the_stylesheet(html):
    """`display: contents` on the form is what keeps #composer-chat a flex child of
    #chat (the form itself must not become a box). It is still needed — it is just
    a stylesheet rule now, so the media block's higher-specificity `display: none`
    can win over it."""
    style = _style_block(html)
    assert re.search(r"#inputbox\s*\{[^}]*display:\s*contents", style), \
        "#inputbox needs display:contents from the stylesheet, not from a style attribute"


def test_preview_view_hides_every_chat_child_but_the_strip(html):
    narrow = _narrow_block(html)
    assert "body.view-preview #chat > *:not(#anntools) { display: none; }" in narrow


# ------------------------------- 2. crossing the breakpoint resets the armed controls

def _apply_narrow_view(html, prelude, call):
    """Run applyNarrowView() out of the page under node."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own narrow-layout glue")
    start = html.index("function applyNarrowView()")
    end = html.index("\n}\n", start) + 3
    script = prelude + "\n" + html[start:end] + "\n" + call
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# Everything applyNarrowView touches, stubbed so the only real logic left is the
# reset guard. `narrow` and `paneview` are the two inputs that decide it.
_STUBS = """
let narrow = true, paneview = "chat", annOn = false, noPane = false;
const calls = [];
const NARROW_MQ = { get matches() { return narrow; } };
const fused = { params: { get: (k) => (k === "paneview" ? paneview : null) } };
const viewBtn = { textContent: "", setAttribute() {} };
document = { body: { classList: { toggle() {} } } };
function annSetMode(v) { annOn = v; calls.push("annSetMode:" + v); }
function renderAnn() {}
function requestAnimationFrame() {}
let narrowShown = null;
"""


def test_boot_never_touches_the_armed_controls(html):
    """The one call that must NOT reset: boot is never a crossing, and disarming
    there would write `annmode=0` the first time this view opened in a narrow
    pane. `narrowShown === null` is what "boot" means here."""
    out = _apply_narrow_view(html, _STUBS, """
narrow = true; paneview = "chat"; annOn = true;
applyNarrowView();
console.log(JSON.stringify({ calls, annOn, narrowShown }));
""")
    assert out["calls"] == []
    assert out["annOn"] is True


def test_narrowing_a_wide_pane_disarms_the_now_hidden_controls(html):
    """The regression this test exists for: a WIDE boot leaves narrowShown false,
    so a later media crossing is not a flip. Arming annotate mode wide and then
    narrowing the pane hid the switch and kept the mode armed, swallowing clicks
    in a document the user could not see."""
    out = _apply_narrow_view(html, _STUBS, """
narrow = false; paneview = "chat";
applyNarrowView();                       // boot, wide
annOn = true;                            // the user arms it while both columns show
narrow = true;                           // the divider drag crosses 800px
applyNarrowView();
console.log(JSON.stringify({ calls, annOn }));
""")
    assert out["annOn"] is False, "annotate mode stayed armed behind a hidden #annbtn"


def test_the_preview_to_chat_flip_still_disarms(html):
    out = _apply_narrow_view(html, _STUBS, """
narrow = true; paneview = "preview";
applyNarrowView();                       // boot, narrow, in Preview
annOn = true;
paneview = "chat";
applyNarrowView();
console.log(JSON.stringify({ calls, annOn }));
""")
    assert out["annOn"] is False


def test_arming_inside_preview_view_is_left_alone(html):
    """Preview is where a person arms these deliberately — the frame is on screen
    and the pill is what says "this send carries a picture of it". Only a call
    that lands on the CHAT view resets."""
    out = _apply_narrow_view(html, _STUBS, """
narrow = true; paneview = "chat";
applyNarrowView();
paneview = "preview";
applyNarrowView();
annOn = true;
applyNarrowView();                       // e.g. a `split` param change
console.log(JSON.stringify({ calls, annOn }));
""")
    assert out["annOn"] is True


def test_a_wide_pane_keeps_both_armed(html):
    """Above the breakpoint both halves are on screen, so armed is correct and
    nothing is hidden behind anything."""
    out = _apply_narrow_view(html, _STUBS, """
narrow = false; paneview = "chat";
applyNarrowView();
annOn = true;
applyNarrowView();
console.log(JSON.stringify({ calls, annOn }));
""")
    assert out["annOn"] is True


# --------------------- 3. an auto-submitted note cannot hide a permission card

def test_a_permission_card_pulls_the_narrow_layout_back_to_the_chat(html):
    """A card is the prompt: the subprocess is blocked until it is answered. In
    narrow Preview view #logwrap is collapsed with the rest of the column, so a
    card appended to #log is a turn stalled behind nothing the user can see or
    click. syncPermissions leaves Preview for the chat when it connects one."""
    start = html.index("function syncPermissions(")
    body = html[start:html.index("\n}\n", start)]
    assert "showPermissionCard" in body, \
        "connecting a card must route through the one function that also " \
        "surfaces it in the narrow layout"
    fn = html.index("function showPermissionCard(")
    surface = html[fn:html.index("\n}\n", fn)]
    assert "paneview" in surface and "chat" in surface, \
        "showPermissionCard has to leave Preview view, or the card lands in a hidden #log"


def test_auto_submit_is_still_unconditional(html):
    """The fix is NOT "stop auto-submitting from Preview view": a saved note going
    to Claude immediately is the feature. Pinned so the card fix above cannot be
    "simplified" into an off switch."""
    start = html.index("function annAutoSubmit()")
    body = html[start:html.index("\n}\n", start)]
    assert "paneview" not in body and "NARROW_MQ" not in body

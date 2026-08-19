"""claude's annotation modes: element notes, point notes, and the walkthrough.

The shape of the feature (D345/D346, redrawn 2026-08-19):

* **one Comment mode; the TOOL picks the target.** The strip's Element/Point
  picker is on screen whenever the mode is armed — typed comment and recording
  alike — and says what a click pins. Alt is the momentary override in both
  directions; empty background is always a spot. No second mode toggle exists
  to be discovered or forgotten.
* **the composer is just the words.** The anchor chip and the per-note mic
  left the card: the picker owns the choice up front, speaking is the
  walkthrough's job, and the placeholder still names the target's KIND.
* **voice is the walkthrough (annrec).** Talk while clicking; stopping
  transcribes, fills the notes, auto-sends once words actually land, and puts
  the whole mode away.
* **a point note's crop is captured at SAVE time and awaited.** Auto-send fires
  the moment the save returns; a fire-and-forget capture would race it and put
  `shot: null` on the wire for a picture a beat away from existing.

What node cannot cover and is NOT asserted here: real mic capture, real
transcription, cursor rendering. The tests pin the pure helpers, the wiring
facts, and the ordering that makes auto-send safe.
"""
import json
import os
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join("fused_render", "templates", "claude", "template.html")


@pytest.fixture
def html():
    return open(TEMPLATE, encoding="utf-8").read()


def _node(fn_names, call, html, prelude=""):
    """Run named top-level functions/consts out of template.html under node.
    Same extraction as tests/test_claude_shots.py's `_node`; kept as its own
    copy because the suites are independent."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own helpers")
    chunks = []
    for name in fn_names:
        start = html.index(name)
        if name.startswith("function") or name.startswith("async function"):
            end = html.index("\n}\n", start) + 3
            chunks.append(html[start:end])
            continue
        taken = []
        for line in html[start:].split("\n"):
            taken.append(line)
            if line.split("//")[0].rstrip().endswith(";"):
                break
        chunks.append("\n".join(taken))
    script = prelude + "\n" + "\n".join(chunks) + "\n" + call
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _block(html, start, end):
    """The source between two unique markers — for asserting ordering facts
    inside one function without running it."""
    i = html.index(start)
    return html[i:html.index(end, i)]


# --------------------------------------------------- the composer's one line

def test_the_placeholder_names_the_kind_of_target(html):
    """The anchor chip left the card (2026-08-19); the placeholder is what
    still says whether the note is about a box or a spot."""
    body = _block(html, "function annPaintPlaceholder(", "\n}\n")
    assert '"What about this spot?"' in body
    assert '"What about this element?"' in body


def test_the_anchor_chip_and_composer_mic_are_gone(html):
    """Both removed 2026-08-19: what a click pins is chosen up front in the
    strip's picker (no post-click flip), and speaking is the walkthrough's job.
    Gone from the markup, both stylesheets (the page's own and the shadow copy
    the portal carries), and the script."""
    for needle in ["annanchor", "annAnchor", "annmic", "annMic", "annDraftAlt"]:
        assert needle not in html, needle


# ------------------------------------------- the gesture picks the target

def test_a_manual_background_click_opens_a_point_composer(html):
    """Manual mode used to swallow background clicks (only the recorder could
    make a point note); now the composer opens with a point anchor."""
    body = _block(html, "const pointAnchor = { kind:", "annPlaceHl(el);")
    assert body.count("annOpenComposer(e.clientX, e.clientY, pointAnchor)") == 2


def test_a_point_click_over_an_element_opens_a_point_composer(html):
    body = _block(html, "// A point click over an element:", "// Re-anchor the ring")
    assert "annOpenComposer(e.clientX, e.clientY, pointAnchor)" in body


def test_an_element_click_opens_an_element_composer(html):
    assert "annOpenComposer(e.clientX, e.clientY, anchor);" in html


def test_the_tool_decides_and_alt_is_a_two_way_override(html):
    """XOR, not OR: Element tool + Alt is a spot, and Point tool + Alt is the
    element again — the exception works in both directions, in the click
    handler and the cursor alike."""
    assert 'const wantPoint = (annTool === "point") !== e.altKey;' in html
    assert 'const aimPoint = (annTool === "point") !== e.altKey;' in html


def test_the_tool_applies_inside_a_recording_too(html):
    body = _block(html, "if (annRecOn) {\n      if (wantPoint)", "annRecMark(anchor);")
    assert "annRecMarkPoint(e.clientX, e.clientY, win)" in body


def test_the_tool_picker_follows_the_mode(html):
    """Shown whenever the mode is armed — typed comment AND recording alike
    (Akshil, 2026-08-19; it used to follow the recording only, and a typed
    comment pinned the default tool with no say) — and put away on disarm,
    through annSetMode, the one door. A cross-origin target (annXO, D355)
    hides it in every state: no element can ever be resolved over there, so
    there is no choice to offer — every click is a spot."""
    assert 'id="anntool" role="radiogroup"' in html
    mode = _block(html, "function annSetMode(on) {", "\nannBtn.addEventListener")
    assert "if (!annOn || annXO) annToolHide();" in mode
    assert "else annToolShow();" in mode
    # arming by mic keeps the picker up too — and cancels a pending exit glide
    begin = _block(html, "async function annRecBegin()", "\n}\n")
    assert "annToolShow();" in begin
    # stopping a walkthrough does NOT hide the picker by hand: the disarm at
    # the end goes through annSetMode, which owns the hide — and the selected
    # tool survives, a visible fact now rather than a hidden trap
    end = _block(html, "async function annRecEnd()", "\n}\n")
    assert "annToolHide();" not in end
    assert 'annSetTool("element");' not in end
    # stopping the walkthrough puts the whole mode away, both exits — but only
    # the SAME arming it stopped: the epoch guard leaves a mode the user
    # re-armed during transcription alone (Bugbot, PR #644)
    assert end.count("if (annOn && annArmEpoch === armed) annSetMode(false);") == 2, end
    # the cross-origin refusal lives in the one door that can raise the picker
    assert "if (annXO) return;" in _block(html, "function annToolShow()", "\n}\n")


def test_the_tool_picker_survives_the_hosted_layout(html):
    """enterNoPane removes the pane's controls; the picker, like #annbtn and
    #annrec, acts on the HOST's pane in CHAT_ONLY and must stay."""
    body = _block(html, "const hostedControls = new Set(", "}\n")
    assert '"anntool"' in body


def test_a_point_anchor_is_page_coordinates_and_carries_no_crop(html):
    """Same convention annRecMarkPoint set: page coords (stable across scroll,
    annPointXY converts back). No `shot` of its own — the send-time overview
    badges the spot, the same picture every other note on the message shares."""
    body = _block(html, "const pointAnchor = { kind:", "};")
    assert '"point"' in body
    assert "scrollX" in body and "scrollY" in body
    assert "shot" not in body


def test_a_point_aim_shows_a_crosshair_and_hides_the_ring(html):
    body = _block(html, "const annAimCursor = (on) =>", "annPlaceHl(el);")
    assert 'root.style.cursor = "crosshair"' in body
    assert 'if (aimPoint) { if (annHl) annHl.style.display = "none"; return; }' in body


# --------------------------------------------------- one commit path, awaited

def test_typed_notes_save_through_the_one_commit_path(html):
    """The Enter key calls annCommit — the one save-and-autosend path."""
    enter = _block(html, 'if (e.key === "Enter" && !e.shiftKey)', "});")
    assert "annCommit();" in enter


def test_a_note_saves_without_any_capture_of_its_own(html):
    """The save path takes no picture: typed and spoken, element and point, all
    ride the ONE send-time overview (annCaptureOverview), so a save is instant
    and cannot race a capture."""
    body = _block(html, "async function annCommit()", "\n}\n")
    assert "shotPane" not in body
    assert "annRecPointShot" not in body
    assert "annAutoSubmit()" in body


# ------------------------------------------ two buttons, one mode at a time

def test_the_other_button_is_always_the_way_out(html):
    """Comment armed: the mic seat acts as Done (send pending notes, disarm).
    Recording: the Comment seat is the stop control. Both are wired at the two
    click handlers, off the two state flags the stylesheet also reads."""
    assert ('annBtn.addEventListener("click",'
            " () => (annRecOn ? annRecEnd() : annSetMode(!annOn)));") in html
    assert ("() => (annRecOn ? annRecEnd() :"
            " annOn ? annDone() : annRecBegin()));") in html


def test_done_flushes_pending_notes_before_disarming(html):
    """Done is the exit that also delivers: an open composer's words are
    committed first (through the same annCommit the Enter key uses), notes
    the auto-send guards left pending ride out through the same
    prefill-and-submit, then the mode goes away."""
    body = _block(html, "async function annDone()", "\n}\n")
    assert "await annCommit();" in body
    # one at a time: annCommit's await could span a re-entrant Done click,
    # which would double-send the pending notes — the guard makes it a no-op
    assert "if (annDoneBusy) return;" in body
    assert "annDoneBusy = false;" in body
    assert "a.content && !a.sent" in body
    # the flush is a bare auto-submit: annPrefillComposer returns nothing now,
    # so gating on its return value would silently never send (Bugbot, #661)
    assert "if (pending && !sending) annAutoSubmit();" in body
    assert "annPrefillComposer" not in body, \
        "there is no canned prompt to seed — the notes are the content"
    assert "annSetMode(false);" in body
    commit = body.index("await annCommit();")
    flush = body.index("annAutoSubmit()")
    disarm = body.index("annSetMode(false);")
    assert commit < flush < disarm, "save, send, then disarm"


def test_a_mousedown_on_the_strip_does_not_drop_the_open_draft(html):
    """The outside-click dismissal used to fire on ✓ Done's mousedown and
    close the composer before the click reached annDone — silently dropping
    the words the user was about to send (Bugbot, PR #664). The strip's own
    controls are exempt, like the pins and chips."""
    body = _block(html, 'document.addEventListener("mousedown", (e) => {', "});")
    assert 't.closest("#anncta")' in body
    assert 't.closest("#anntool")' in body


def test_the_live_recording_is_never_announced_as_done(html):
    """annSetMode(true) names the mic seat Done (annRecOn is still false when
    annRecBegin arms the mode), so the recording writers must claim the
    aria-label too, not just the tooltip (Bugbot, PR #664)."""
    begin = _block(html, "async function annRecBegin()", "\n}\n")
    assert 'annRecBtn.setAttribute("aria-label", "Stop the recording");' in begin
    end = _block(html, "async function annRecEnd()", "\n}\n")
    assert 'annRecBtn.setAttribute("aria-label", "Record a spoken walkthrough");' in end


def test_the_seats_swap_faces_off_the_two_state_classes(html):
    """Every glyph and word is baked into the markup; the stylesheet derives
    each button's face from #annbtn.on / #annrec.on via :has on the wrapper —
    no third writer to fall out of step."""
    # comment armed (and only then): the mic seat wears ✓ Done
    assert ("#anncta:has(#annbtn.on):not(:has(#annrec.on))"
            " #annrec .rec-done { display: block; }") in html
    # recording: the Comment seat wears ■ plus the clock, in --error ink
    assert "#anncta:has(#annrec.on) #annbtn .cmt-stop { display: block; }" in html
    stop = _block(html, "#anncta:has(#annrec.on) #annbtn.on {", "}")
    assert "var(--error)" in stop
    # transcribing: annRecEnd stamps .busy, the word yields to the status
    assert "#anncta.busy #annbtn .cmt-word { display: none; }" in html
    end = _block(html, "async function annRecEnd()", "\n}\n")
    assert 'annCta.classList.add("busy");' in end
    assert 'annCta.classList.remove("busy");' in end


def test_the_words_yield_only_when_they_truly_collide(html):
    """Icon plus word while the strip has room, icon only when it does not —
    detected by MEASURING (annFitStrip: words on, does content overflow the
    box?), not by a breakpoint (Akshil, 2026-08-19: a fixed width dropped the
    words while there was still room). Wired to a ResizeObserver (the box
    changes) and mutation observers on the two content clusters (the picker
    appears, a seat swaps faces, the clock ticks wider) — but never on
    #anntools itself, whose class list annFitStrip writes: observing the node
    it writes would make every verdict schedule the next. The clock is state,
    not a label, and never hides."""
    body = _block(html, "function annFitStrip()", "\n}\n")
    assert "need > annToolsEl.clientWidth" in body
    assert 'classList.remove("tight");' in body
    # NOT scrollWidth: a flex row's scrollWidth floors at clientWidth, so the
    # picker reserve added to it would read as overflow on a half-empty strip
    assert "scrollWidth" not in body
    # the OFF-screen picker's width is reserved, so arming (which slides it
    # in) can never flip the words under the click — the row folds early
    assert "const reserve = annToolReserve();" in body
    probe = _block(html, "function annToolReserve()", "\n}\n")
    assert "if (!annToolEl.hidden) return 0;" in probe
    assert "annXO || !annToolEl.isConnected || annBtn.hidden" in probe
    assert "annToolEl.offsetWidth" in probe
    assert "@container" not in html, "collision detection, not a breakpoint"
    assert "container-type" not in html
    assert "new ResizeObserver(annFitStrip).observe(annToolsEl);" in html
    assert "const mo = new MutationObserver(annFitStrip);" in html
    assert "for (const el of [annToolEl, annCta])" in html
    # ...and the classes that show/hide #back (home ⇄ chat, the narrow views)
    # change the row's content without touching box or clusters (Bugbot, #664)
    assert "for (const el of [document.body, chatEl])" in html
    # the verdict reaches the words through the one token every .lbl reads
    assert "#anntools.tight #anncta, #anntools.tight #anntool { --annlbl: none; }" in html
    assert "#anncta .lbl, #anntool .lbl { display: var(--annlbl, inline); }" in html
    assert 'class="lbl cmt-word"' in html
    assert 'class="lbl rec-word"' in html
    assert 'id="annreclbl"' in html
    assert 'class="lbl" id="annreclbl"' not in html, "the clock is not a label"


# ------------------------------------------------- the walkthrough auto-sends

def test_a_transcribed_walkthrough_autosends_only_when_words_landed(html):
    """`record, talk, stop` reaches Claude without a fourth step — but a
    transcription that assigned nothing leaves the clicks pending and editable
    rather than sending empty notes. Words landing as INTRO (spoken before the
    first click) count: they are the message's own prompt."""
    body = _block(html, "async function annRecEnd()", "\n}\n")
    assign = body.index("annRecAssign(ids, rec.segments)")
    gate = body.index("c && c.content && !c.sent")
    send = body.index("if (spoke || intro) {")
    assert assign < gate < send
    # the intro is seeded OUTSIDE the !sending gate: a walkthrough that ends
    # during a live run parks its words in the composer to ride the next send
    # instead of evaporating (Bugbot, #661) — only the auto-submit is gated
    seed = body.index("annPrefillComposer(intro);")
    submit = body.index("if (!sending) annAutoSubmit();")
    assert send < seed < submit


def test_speech_before_the_first_click_becomes_the_prompt_not_a_note(html):
    """Everything said before the first click is the user framing the task —
    it seeds the composer as the message's own words instead of being pulled
    onto click 1 by the nearest-segment match."""
    out = _node(["function annRecAssign("], """
var annotations = [{id: "a", t: 10}, {id: "b", t: 20}];
function annSave() {}
const intro = annRecAssign(["a", "b"], [
  {start: 1, text: "overall make it cleaner"},
  {start: 4, text: "and use our colors"},
  {start: 10.5, text: "this button is wrong"},
  {start: 19, text: "this chart too"},
]);
console.log(JSON.stringify({intro: intro,
  a: annotations[0].content, b: annotations[1].content}));
""", html)
    assert out["intro"] == "overall make it cleaner and use our colors"
    assert out["a"] == "this button is wrong"
    assert out["b"] == "this chart too"


def test_the_intro_joins_a_typed_draft_and_no_canned_prefill_exists(html):
    """An annotation-only send needs no prompt — the comments are the content,
    so the old "apply the comments" prefill is gone. What the user adds rides
    along: the spoken intro seeds an empty composer, and joins (never clobbers)
    a draft they already typed."""
    out = _node(["function annPrefillComposer("], """
var chatEl = {classList: {contains: () => false}};
var homebox = {value: ""};
var box = {value: ""};
function growBox() {}
function growHome() {}
annPrefillComposer("make the header sticky");
const seededValue = box.value;
box.value = "my own half-typed draft";
annPrefillComposer("and the intro too");
const joined = box.value;
annPrefillComposer("");
console.log(JSON.stringify({seededValue: seededValue, joined: joined,
  untouched: box.value}));
""", html)
    assert out["seededValue"] == "make the header sticky"
    assert out["joined"] == "my own half-typed draft\n\nand the intro too"
    assert out["untouched"] == "my own half-typed draft\n\nand the intro too", \
        "no seed means the composer is left exactly as the user had it"


def test_a_new_note_autosends_bare_with_no_canned_message(html):
    """Saving a note fires the send even with an empty composer: the message
    may be empty, the annotations carry the content, and whatever the user had
    typed is theirs and goes along as the message's own words."""
    body = _block(html, "async function annCommit()", "\n}\n")
    assert "if (isNew && !sending) annAutoSubmit();" in body
    assert "annPrefillComposer" not in body, \
        "the save path seeds nothing — there is no canned prompt to seed"


# ------------------------------------------------ warming the transcriber

def test_the_recorder_warms_the_transcriber_at_start(html):
    """The load runs while the reader is still talking — the dead time it
    fits in — instead of the words waiting on a cold model at stop."""
    rec = _block(html, "async function annRecBegin()", "\n}\n")
    assert "annWarmTranscriber();" in rec


def test_the_warm_up_is_opportunistic_never_load_bearing(html):
    """transcribe loads a cold model inside its own job anyway, so the warm-up
    swallows every failure, skips a resident model, and respects an engine
    with no recommendation."""
    body = _block(html, "function annWarmTranscriber()", "\n}\n")
    assert "m.loaded" in body
    assert "if (!asr.default) return;" in body
    assert '{ capability: "automatic-speech-recognition" }' in body
    assert "console.warn" in body


# --------------------------------- the cross-origin target's point overlay

def test_a_cross_origin_target_gets_a_point_overlay_in_the_host_document(html):
    """D355: a marked frame whose document is out of reach (the /canvases
    workspace marks its HOSTED workbench iframe) used to be the worst state —
    annCapable() said yes off the mark alone, the layer injection silently
    failed, and the switch armed a mode whose clicks went nowhere. Now that
    state is a mode of its own: an overlay in the HOST's document (same
    origin — it marked the frame for us), laid over the iframe's box, where
    every armed click is a `kind: "point"` note. Element anchors and the hover
    ring stay off — both need a document cross-origin forbids; the pictures
    come off the TAB instead (see the tab-capture test below)."""
    # The state is derived in the ONE sync everything else already trusts,
    # and the overlay replaces the injected layer through the same variable.
    assert "annXO = !!(next && !doc);" in html
    assert ("const layer = doc ? annInjectLayer(doc)"
            " : (annXO ? annXOLayer() : null);") in html
    body = _block(html, "function annXOLayer()", "\n}\n\n")
    # Same {root, pins, hl, stage} shape annInjectLayer answers, so the pins,
    # the composer portal and annPlacePop need no second code path.
    assert "stage: host" in body
    # The click is a point note from birth — overlay-relative, `win` null (no
    # scroll to fold in), shot null until the save-time crop — and a recording
    # click takes the same walkthrough path a same-origin point does.
    assert "annRecMarkPoint(x, y, null)" in body
    assert 'annOpenComposer(x, y, { kind: "point", x, y, shot: null });' in body
    # Disarmed, the overlay must not eat a single event.
    assert 'catcher.style.display = annOn ? "" : "none";' in body


def test_the_point_overlay_coordinates_resolve_through_a_zero_scroll_stub(html):
    """annPointXY is the ONE converter from a point's stored coords to pixels,
    and it refuses a null window. The overlay's coords are overlay-relative
    with no scroll to convert through, so both readers (the pin painter and
    the chip's edit-click) hand it the zero-scroll stand-in rather than
    growing a second converter."""
    assert "const ANN_XO_SCROLL = { scrollX: 0, scrollY: 0 };" in html
    assert html.count("annXO ? ANN_XO_SCROLL : null") == 3


def test_the_overlay_is_torn_down_with_the_layer_it_stands_in_for(html):
    """The overlay is OUR node in the PARENT's document — the same orphaning
    risk the injected layer's pagehide teardown exists for, so it is removed
    there too, and whenever the target stops being cross-origin (the mark
    moved to a same-origin frame, or away entirely)."""
    assert "if (!annXO) annXORemove();" in html
    teardown = _block(html, 'window.addEventListener("pagehide"', "\n  });")
    assert "annXORemove();" in teardown


def test_a_cross_origin_capture_comes_off_the_tab_not_the_document(html):
    """The screenshots work over the cross-origin target too: shotPane — the
    ONE rasteriser every capture path shares — branches to shotXOPane, which
    grabs a frame off a getDisplayMedia tab share and crops the marked
    iframe's rect out of it. Same {canvas, width, height, ...} shape, clean
    doubt fields (no style walk, no image inlining, no WebGL readback), so
    the overview's badge burner and the whole-pane shot run unchanged."""
    pane = _block(html, "async function shotPane(deadline)", "const clone")
    assert "if (annXO) return shotXOPane();" in pane
    body = _block(html, "async function shotXOPane()", "\n}\n\n")
    assert "getBoundingClientRect()" in body
    assert "blanks: []" in body and "incomplete: false" in body
    # The share prompt is paid ONCE — the stream is cached and reused — and
    # raised at ARM time, where the user activation actually is (a mic commit
    # or a walkthrough click's fire-and-forget crop may have none).
    assert "if (annOn && annXO) annXOStreamGet().catch" in html
    getter = _block(html, "async function annXOStreamGet()", "\n}\n")
    assert 'readyState === "live"' in getter
    assert "preferCurrentTab: true" in getter
    # And released with the overlay: ended share, target no longer
    # cross-origin, pagehide (annXORemove covers all three).
    remove = _block(html, "function annXORemove()", "\n}\n")
    assert "annXOStreamStop();" in remove
    # The overview's point badges resolve through the same zero-scroll stub
    # the pin painter uses.
    capture = _block(html, "async function annCaptureOverview(", "\n}\n")
    assert "annXO ? ANN_XO_SCROLL : null" in capture


def test_the_capture_stream_state_is_declared_before_its_boot_time_teardown(html):
    """Regression: annXORemove (which calls annXOStreamStop) runs during the
    script's own boot — annSetMode → renderAnn → annSyncTarget — so the
    stream's `let` must be ABOVE it. It was first declared 2700 lines later
    in the screenshot section, and the temporal-dead-zone ReferenceError
    killed the whole script at boot: empty model pills, no target poll, an
    overlay stuck armed over the workbench."""
    assert html.index("let annXOStream = null;") \
        < html.index("function annXORemove()") \
        < html.index('annSetMode(fused.params.get("annmode") === "1");')

"""claude's annotation modes: element notes, point notes, and voice — typed or
spoken, every saved note auto-sends.

The shape of the feature (D345/D346):

* **one Comment mode; the GESTURE picks the target.** A click on an element
  anchors to the element (the ring said so); a click on empty background — or an
  Alt-click anywhere — pins the exact spot instead (the crosshair cursor said
  so). No second mode toggle exists to be discovered or forgotten.
* **the composer names the target before the words commit.** The anchor chip
  reads "<button> Save" or "⌖ exact spot", and when the click had both readings
  it is a real button that flips between them.
* **voice is a property of the composer, not a mode.** The mic button records
  one note; stopping transcribes and commits through the same `annCommit` the
  Enter key uses — so typed and spoken notes cannot save (or auto-send)
  differently. The walkthrough recorder (annrec) stays the batch flavour and
  now auto-sends too, once its transcript actually lands words.
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


# ------------------------------------------------------------- the anchor chip

def test_the_chip_names_a_point_as_the_exact_spot(html):
    out = _node(["function annAnchorLabel("],
                'console.log(JSON.stringify(annAnchorLabel({kind: "point", x: 3, y: 4})));',
                html)
    assert out == "⌖ exact spot"


def test_the_chip_names_an_element_by_tag_and_leading_text(html):
    out = _node(["function annAnchorLabel("],
                'console.log(JSON.stringify(annAnchorLabel({tag: "button", text: "Save"})));',
                html)
    assert out == "<button> Save"


def test_the_chips_element_text_is_truncated_not_unbounded(html):
    """The wire carries up to 80 chars of leading text; a chip is one line in a
    280px card and cannot."""
    long = "x" * 80
    out = _node(["function annAnchorLabel("],
                'console.log(JSON.stringify(annAnchorLabel({tag: "p", text: %s})));'
                % json.dumps(long), html)
    assert out.endswith("…") and len(out) < 80


def test_an_anchorless_element_still_gets_a_name(html):
    out = _node(["function annAnchorLabel("],
                "console.log(JSON.stringify(annAnchorLabel({anchorId: 'a'})));",
                html)
    assert out == "<element>"


def test_the_flip_swaps_draft_and_alt_and_never_fires_one_sided(html):
    """The chip is a flip only while BOTH readings of the click exist; editing
    a saved note (alt null) it must be inert."""
    body = _block(html, 'annAnchorBtn.addEventListener("click"', "});")
    assert "if (!annDraft || !annDraftAlt) return;" in body
    assert "annDraft = annDraftAlt;" in body


# ------------------------------------------- the gesture picks the target

def test_a_manual_background_click_opens_a_point_composer(html):
    """Manual mode used to swallow background clicks (only the recorder could
    make a point note); now the composer opens with a point anchor."""
    body = _block(html, "const pointAnchor = { kind:", "annPlaceHl(el);")
    assert body.count("annOpenComposer(e.clientX, e.clientY, pointAnchor, null)") == 2


def test_a_point_click_over_an_element_keeps_the_element_as_the_flip(html):
    body = _block(html, "// A point click over an element:", "// Re-anchor the ring")
    assert "annOpenComposer(e.clientX, e.clientY, pointAnchor, anchor)" in body


def test_an_element_click_carries_the_point_reading_as_the_flip_target(html):
    assert "annOpenComposer(e.clientX, e.clientY, anchor, pointAnchor)" in html


def test_the_tool_decides_and_alt_is_a_two_way_override(html):
    """XOR, not OR: Element tool + Alt is a spot, and Point tool + Alt is the
    element again — the exception works in both directions, in the click
    handler and the cursor alike."""
    assert 'const wantPoint = (annTool === "point") !== e.altKey;' in html
    assert 'const aimPoint = (annTool === "point") !== e.altKey;' in html


def test_the_tool_applies_inside_a_recording_too(html):
    body = _block(html, "if (annRecOn) {\n      if (wantPoint)", "annRecMark(anchor);")
    assert "annRecMarkPoint(e.clientX, e.clientY, win)" in body


def test_the_tool_picker_follows_the_recording(html):
    """Shown while a walkthrough RECORDS, hidden otherwise (Akshil,
    2026-08-19): a typed comment pins the default tool and never asks, so the
    picker is a property of recorded clicks — annRecBegin reveals it,
    annRecEnd and annSetMode's disarm put it away. A cross-origin target
    (annXO, D355) hides it even mid-recording: no element can ever be resolved
    over there, so there is no choice to offer — every click is a spot."""
    assert 'id="anntool" role="radiogroup"' in html
    assert "if (!(annOn && annRecOn)) || annXO" not in html  # guard shape below
    assert "if (!(annOn && annRecOn) || annXO) annToolHide();" in html
    begin = _block(html, "async function annRecBegin()", "\n}\n")
    assert "annToolShow();" in begin
    end = _block(html, "async function annRecEnd()", "\n}\n")
    assert "annToolHide();" in end
    # stopping the walkthrough puts the whole mode away, both exits
    assert end.count("if (annOn) annSetMode(false);") == 2, end
    # the cross-origin refusal lives in the one door that can raise the picker
    assert "if (annXO) return;" in _block(html, "function annToolShow()", "\n}\n")


def test_the_tool_picker_survives_the_hosted_layout(html):
    """enterNoPane removes the pane's controls; the picker, like #annbtn and
    #annrec, acts on the HOST's pane in CHAT_ONLY and must stay."""
    body = _block(html, "const hostedControls = new Set(", "}\n")
    assert '"anntool"' in body


def test_a_point_anchor_is_page_coordinates_with_shot_null_from_the_start(html):
    """Same convention annRecMarkPoint set: page coords (stable across scroll,
    annPointXY converts back) and a `shot` key that is present-but-null."""
    body = _block(html, "const pointAnchor = { kind:", "};")
    assert '"point"' in body
    assert "scrollX" in body and "scrollY" in body
    assert "shot: null" in body


def test_a_point_aim_shows_a_crosshair_and_hides_the_ring(html):
    body = _block(html, "const annAimCursor = (on) =>", "annPlaceHl(el);")
    assert 'root.style.cursor = "crosshair"' in body
    assert 'if (aimPoint) { if (annHl) annHl.style.display = "none"; return; }' in body


# --------------------------------------------------- one commit path, awaited

def test_typed_and_spoken_notes_save_through_the_same_commit(html):
    """The Enter key and the mic's transcription both call annCommit — the two
    ways of producing the words cannot save or auto-send differently."""
    enter = _block(html, 'if (e.key === "Enter" && !e.shiftKey)', "});")
    assert "annCommit();" in enter
    mic = _block(html, "async function annMicStop()", "\n}\n")
    assert "annCommit();" in mic


def test_a_point_notes_crop_is_awaited_before_autosend(html):
    """Ordering, not just presence: auto-send composes the wire immediately, so
    the crop must exist (or have failed) before it fires."""
    body = _block(html, "async function annCommit()", "\n}\n")
    crop = body.index("await annRecPointShot(saved, p.x, p.y)")
    send = body.index("annAutoSubmit()")
    assert crop < send


# --------------------------------------------------------------- the mic

def test_typing_while_the_mic_is_live_cancels_it(html):
    assert 'annTa.addEventListener("input", () => annMicCancel());' in html


def test_closing_the_composer_cancels_a_live_mic(html):
    body = _block(html, "function annCloseComposer()", "\n}\n")
    assert "annMicCancel();" in body


def test_transcribed_words_only_land_on_the_note_they_were_spoken_for(html):
    """The composer can close, or move to another note, while transcription
    runs; the words are discarded, never written into whatever is open now.
    The anchor-chip flip is the one legal move (same click, other reading)."""
    body = _block(html, "async function annMicStop()", "\n}\n")
    assert "annDraft === draft || annDraft === alt" in body
    assert 'annPop.style.display === "block"' in body


def test_the_mic_never_transcribes_silence(html):
    body = _block(html, "async function annMicStop()", "\n}\n")
    assert "if (!blob.size) { annMicReset(); return; }" in body


# ------------------------------------------------- the walkthrough auto-sends

def test_a_transcribed_walkthrough_autosends_only_when_words_landed(html):
    """`record, talk, stop` reaches Claude without a fourth step — but a
    transcription that assigned nothing leaves the clicks pending and editable
    rather than sending empty notes."""
    body = _block(html, "async function annRecEnd()", "\n}\n")
    assign = body.index("annRecAssign(ids, rec.segments);")
    gate = body.index("c && c.content && !c.sent")
    send = body.index("if (spoke && !sending && annPrefillComposer()) annAutoSubmit();")
    assert assign < gate < send


# --------------------------------------------- the two stylesheets stay paired

def test_the_chip_and_mic_are_styled_in_both_stylesheets(html):
    """The composer is PORTALED into the target document behind a shadow root
    (annPortalPop), where the page's own <style> cannot reach — every composer
    rule exists twice, and a control styled in one copy arrives unstyled on
    the other path."""
    for sel in ["#annanchor", "#annmic"]:
        # the page's own stylesheet
        assert ("  %s {" % sel) in html, sel
        # the shadow copy (string list, #annpop-prefixed)
        assert ('"#annpop %s {' % sel) in html, sel


# ------------------------------------------------ warming the transcriber

def test_both_recorders_warm_the_transcriber_at_start(html):
    """The load runs while the reader is still talking — the dead time it
    fits in — instead of the words waiting on a cold model at stop."""
    rec = _block(html, "async function annRecBegin()", "\n}\n")
    mic = _block(html, "async function annMicBegin()", "\n}\n")
    assert "annWarmTranscriber();" in rec
    assert "annWarmTranscriber();" in mic


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
    assert 'annOpenComposer(x, y, { kind: "point", x, y, shot: null }, null);' in body
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
    the crops, the crosshair burner and the whole-pane shot run unchanged."""
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
    # The save-time crop fires for an overlay note too, through the same
    # zero-scroll stub the pin painter uses.
    commit = _block(html, "async function annCommit()", "\n}\n")
    assert "annXO ? ANN_XO_SCROLL : null" in commit


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

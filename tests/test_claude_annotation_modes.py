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
    """And it carries `nearPath` — the element the forced point landed OVER. The
    spot is still the note (annResolve must never resolve a point to an element,
    or a note about the gap beside a button becomes a note about the button), but
    naming what it sits inside is the difference between a coordinate the model
    can only look at on the overview and one it can edit around."""
    body = _block(html, "// A point click over an element:", "// Re-anchor the ring")
    assert "Object.assign({ nearPath }, pointAnchor)" in body


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
    assert "annRecMarkPoint(e.clientX, e.clientY, win, nearPath)" in body


def test_a_forced_point_names_the_element_it_landed_over(html):
    """ONE field for both anchor shapes — an id spelled as a selector, or
    annPathOf's path — because both are spellings of the same scheme (D146
    forbids a second implementation, not a second spelling). Absent for a click
    with nothing under it: the two branches above this one pass no `nearPath`."""
    assert ('const nearPath = anchor.anchorId ? "#" + anchor.anchorId '
            ': anchor.anchorPath;') in html
    body = _block(html, "function annRecMarkPoint(", "annRecPaint();")
    assert "if (nearPath) c.nearPath = nearPath;" in body
    # The click with nothing under it, and the cross-origin overlay, both still
    # mark with three arguments — there is no element to name.
    assert "annRecMarkPoint(e.clientX, e.clientY, win); return; }" in html
    assert "annRecMarkPoint(x, y, null); return; }" in html


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
    """The Enter key calls annCommit — the one SAVE path. It never sends."""
    enter = _block(html, 'if (e.key === "Enter" && !e.shiftKey)', "});")
    assert "annCommit();" in enter
    assert "annAutoSubmit" not in enter


def test_a_portaled_composer_keeps_its_keystrokes(html):
    """Hosted, the composer is a node of the APP's document, so a keystroke in
    it bubbled into the app's own listeners — an app that focuses its search
    box on any key stole the letters (Akshil, 2026-09-04). Every key-family
    event stops at the composer, and focus is taken back on keydown in case a
    capture-phase listener already moved it. Bubble phase, or Enter/Escape
    would never reach the textarea's own handler."""
    body = _block(html, "const ANN_KEY_EVENTS = [", "\n}\n")
    for ev in ("keydown", "keyup", "keypress", "input", "beforeinput",
               "compositionstart", "compositionend"):
        assert '"%s"' % ev in body
    assert "if (!annPortaled() && !(e.view && e.view !== window)) return;" in body, \
        ("the parked seat's bindings are the chat's own — leave them alone; but "
         "Enter unportals mid-dispatch, so the event's window decides too")
    assert "e.stopPropagation();" in body
    assert "annTa.focus();" in body
    assert "host.shadowRoot.activeElement" in body, \
        "activeElement retargets to the layer host; drill into the shadow root"
    assert "}, true);" not in body, "bubble phase, not capture"


def test_done_during_a_live_run_sends_the_notes_as_a_follow_up(html):
    """Done (and a stopped recording) during a live run used to hold the notes
    back behind a `!sending` gate and leave them as chips the reader had to
    Enter out by hand (Akshil, 2026-09-04). Both now hand them to submitChat,
    whose live-run branch sends an annotation-only follow-up to the running
    claude (sendFollowUp, PR #979). The home form's Stop is never pressed by
    an auto-send."""
    done = _block(html, "async function annDone()", "\n}\n")
    assert "if (pending && (activeRun || !sending)) annAutoSubmit();" in done
    rec = _block(html, "async function annRecEnd()", "\n}\n")
    assert "if (activeRun || !sending) annAutoSubmit();" in rec
    submit = _block(html, "function submitChat()", "\n}\n")
    live = submit[submit.index("if (activeRun) {"):]
    assert "if (!message && !annPending().length && !shotAttached.length) return;" in live
    assert "sendFollowUp(message);" in live
    auto = _block(html, "function annAutoSubmit()", "\n}\n")
    assert 'contains("home") && !activeRun' in auto
    # nothing page-side parks notes any more
    assert "annQueued" not in html and "annQueueChips" not in html


def test_a_sent_note_draws_no_pin(html):
    """Once a note has gone to Claude its pin leaves the app (Akshil,
    2026-09-04): the muted ✓ that used to linger until the run resolved meant
    the next round was commented over last round's marks. The note is kept —
    the receipt and a failed send's un-marking still read it."""
    body = _block(html, "  annotations.forEach((c, i) => {", "\n  });\n")
    gate = "if (c.sent || (c.createdAt || 0) < annRoundStart) return;"
    assert body.index(gate) < body.index("annPins.ownerDocument.createElement")


def test_each_arm_starts_a_clean_round_of_pins(html):
    """Arming the mode stamps the round (Akshil, 2026-09-04); only notes saved
    since get pins. Older notes — queued behind a live run, or left pending —
    keep their chips and still send, but no longer mark the page."""
    assert "if (on) { annArmEpoch += 1; annRoundStart = Date.now(); }" in html
    assert "let annRoundStart = 0;" in html


def test_a_failed_follow_up_gives_the_notes_back(html):
    """sendFollowUp stamps the notes `sent` before the inbox write; a run that
    is gone, a send the session never took, or a thrown send are all failures
    the claude never saw, so the notes go back to pending (chips return, Done
    can resend) and the receipt is pulled — the rollback sendMessage already
    does for a run that never launched (Bugbot, PR #996)."""
    body = _block(html, "async function sendFollowUp(text)", "\n}\n")
    assert "const giveBack = () => {" in body
    give = _block(body, "const giveBack = () => {", "\n  };\n")
    # back INTO the list, not just un-marked: annResolveSent has usually already
    # dropped a sent note by the time a follow-up fails (session ended)
    assert "if (!annotations.some((a) => a.id === c.id)) annotations.push(c);" in give
    assert "c.sent = 0;" in give
    assert "if (receipt) receipt.remove();" in give
    assert "if (bareTurn) bareTurn.remove();" in give, "the wordless ghost row goes too"
    assert "shotAttached = pics.concat(shotAttached); renderAnn();" in give, \
        "attached pictures come back to the composer, as sendMessage's rollback does"
    assert body.count("giveBack();") == 3, "no run, not sent, and thrown"
    # the stream-split counter bumps only once the inbox took the message — a
    # rolled-back send must not read to pollLoop as a landed follow-up
    assert body.count("followupSeq++;") == 1
    assert body.index("followupSeq++;") > body.index('"Could not send: the session ended')
    assert body.index("giveBack();") < body.index('"Could not send: no run to attach')


def test_a_saved_note_pools_until_done(html):
    """Saving a typed note sends nothing (Akshil, 2026-09-04): the first note
    of a review used to start a run while the reader was still giving
    feedback. Notes pool as pending and go out together on Done — the shape a
    recorded walkthrough already has (many marks, one send at stop)."""
    body = _block(html, "async function annCommit()", "\n}\n")
    assert "annAutoSubmit" not in body
    assert "submitChat" not in body
    assert "requestSubmit" not in body
    assert "annSave();" in body
    assert "annCloseComposer();" in body


def test_a_note_saves_without_any_capture_of_its_own(html):
    """The save path takes no picture: typed and spoken, element and point, all
    ride the ONE send-time overview (annCaptureOverview), so a save is instant
    and cannot race a capture."""
    body = _block(html, "async function annCommit()", "\n}\n")
    assert "shotPane" not in body
    assert "annRecPointShot" not in body


# ------------------------------------------ two buttons, one mode at a time

def test_the_comment_seat_is_the_modes_one_control(html):
    """A mode ON takes the strip down to one button (Akshil, 2026-08-19): the
    mic hides, and the Comment seat's click matches its face — stop a live
    recording, Done for an armed comment mode, arm from rest. Cancelling
    without sending is Esc's job now."""
    assert ("() => (annRecOn ? annRecEnd() :"
            " annOn ? annDone() : annSetMode(true)));") in html
    # the mic only ever starts a walkthrough (the recording leg guards a
    # click racing the hide, it is not a second stop control)
    assert ("() => (annRecOn ? annRecEnd() : annRecBegin()));") in html


def test_done_flushes_pending_notes_before_disarming(html):
    """Done is the exit that also delivers — the typed mode's ONE send: an open
    composer's words are committed first (through the same annCommit the Enter
    key uses), then every pending note goes out in one bare auto-submit, then
    the mode goes away."""
    body = _block(html, "async function annDone()", "\n}\n")
    assert "await annCommit();" in body
    # one at a time: annCommit's await could span a re-entrant Done click,
    # which would double-send the pending notes — the guard makes it a no-op
    assert "if (annDoneBusy) return;" in body
    assert "annDoneBusy = false;" in body
    assert "a.content && !a.sent" in body
    # the flush is a bare auto-submit: annPrefillComposer returns nothing now,
    # so gating on its return value would silently never send (Bugbot, #661).
    # A live run is not a gate: submitChat parks the notes in the queue.
    assert "if (pending && (activeRun || !sending)) annAutoSubmit();" in body
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


def test_discard_throws_the_walkthrough_away(html):
    """An outline trash seat, recording only, LEFT of the ■/clock (Akshil,
    2026-08-19): the recording stops with nothing kept — no transcription, no
    auto-send — and the clicks' empty marks are deleted with it. annRecEnd
    no-ops after it, so a stop click racing the discard cannot resurrect the
    walkthrough."""
    assert "#anncta:has(#annrec.on) #anndiscard { display: inline-flex;" in html
    # two ids on the hide: a bare #anndiscard loses to the base
    # `#anncta button` display rule on specificity, and the trash sat on the
    # strip in every state
    assert "#anncta #anndiscard { display: none; }" in html
    assert "\n  #anndiscard { display: none; }" not in html
    view = _block(html, '<div id="anncta">', "</div>")
    assert view.index('id="anndiscard"') < view.index('id="annbtn"')
    body = _block(html, "async function annRecDiscard()", "\n}\n")
    # cancel(), not stop(): the capture handle's cancel is the ending that
    # DELETES the file (SPEC CP-4), which is what a discard means — a stop
    # would leave the audio in <home>/recordings with a row pointing at it
    assert "await handle.cancel()" in body
    assert "handle.stop()" not in body
    # the SESSION is snapshotted before the await — flags, face, handle, ids,
    # timer, epoch — because a new recording can begin while this one's ending
    # settles, and a global read after the await would be the new session's
    # (its marks deleted, its timer killed, its transcript overwritten). Both
    # enders follow the same order (Bugbot, #665, three rounds of it).
    for fn, ending in ((body, "await handle.cancel()"),
                       (_block(html, "async function annRecEnd()", "\n}\n"),
                        "await handle.stop()")):
        stop = fn.index(ending)
        assert fn.index("annRecOn = false;") < stop
        assert fn.index("clearInterval(annRecTimerId);") < stop
        assert fn.index("const armed = annArmEpoch;") < stop
        assert fn.index("annRecIds = [];") < stop
        assert fn.index("annRecHandle = null;") < stop
    assert body.index('annRecBtn.setAttribute("aria-label", "Annotate with a spoken walkthrough");') \
        < body.index("await handle.cancel()")
    assert "annotations = annotations.filter((a) => !ids.has(a.id));" in body
    assert "renderAnn();" in body, "discarded pins leave the screen even if Esc already disarmed"
    assert "fused.ai.transcribe" not in body and "annAutoSubmit" not in body
    assert "if (annOn && annArmEpoch === armed) annSetMode(false);" in body, \
        "a new arming that slipped into the settle is not this discard's to close"
    assert 'annDiscardBtn.addEventListener("click", () => annRecDiscard());' in html


def test_the_busy_seat_is_a_status_not_a_button(html):
    """While "Transcribing…" is the whole face, the seat must not still act
    as Done (Bugbot, PR #665): disabled and named for what is happening; the
    finally re-enables it and the disarm renames it."""
    end = _block(html, "async function annRecEnd()", "\n}\n")
    assert "annBtn.disabled = true;" in end
    assert 'annBtn.setAttribute("aria-label", "Transcribing the walkthrough");' in end
    assert "annBtn.disabled = false;" in end
    # ...and any mode TRANSITION ends the status's claim early: Esc during a
    # transcription disarms through annSetMode, which must not leave the seat
    # inert until the finally, or the epoch-guarded re-arm is blocked
    mode = _block(html, "function annSetMode(on) {", "\nannBtn.addEventListener")
    assert "annBtn.disabled = false;" in mode


def test_a_noun_resolving_mid_mode_does_not_rename_the_done_seat(html):
    """applyPaneNoun's aria write is gated on the mode being OFF, like its
    title write (Bugbot, PR #665): the armed writers own the armed names."""
    body = _block(html, "function applyPaneNoun()", "\n}\n")
    assert 'if (!annOn) annBtn.setAttribute("aria-label", "Comment on the " + paneNoun);' in body


def test_the_live_recording_is_never_announced_as_done(html):
    """annSetMode(true) names the mic seat Done (annRecOn is still false when
    annRecBegin arms the mode), so the recording writers must claim the
    aria-label too, not just the tooltip (Bugbot, PR #664)."""
    begin = _block(html, "async function annRecBegin()", "\n}\n")
    assert 'annRecBtn.setAttribute("aria-label", "Stop the recording");' in begin
    end = _block(html, "async function annRecEnd()", "\n}\n")
    assert 'annRecBtn.setAttribute("aria-label", "Annotate with a spoken walkthrough");' in end


def test_the_seat_swaps_faces_off_the_two_state_classes(html):
    """Every glyph and word is baked into the markup; the stylesheet derives
    the seat's face from #annbtn.on / #annrec.on via :has on the wrapper —
    no third writer to fall out of step. The mic hides whenever a mode is on,
    so the strip is ONE control while armed or recording."""
    assert "#anncta:has(#annbtn.on) #annrec { display: none; }" in html
    # comment armed (and only then, and not while transcribing): ✓ Done
    assert ("#anncta:not(.busy):has(#annbtn.on):not(:has(#annrec.on))"
            " #annbtn .cmt-done { display: block; }") in html
    # recording: the seat wears ■ plus the clock, in --error ink
    assert "#anncta:has(#annrec.on) #annbtn .cmt-stop { display: block; }" in html
    stop = _block(html, "#anncta:has(#annrec.on) #annbtn.on {", "}")
    assert "var(--error)" in stop
    # transcribing: annRecEnd stamps .busy and the status stands ALONE — the
    # Done face is gated off busy at its own (higher-specificity) show rules,
    # not fought with a weaker hide ("✓ Done Transcribing…", 2026-08-19)
    assert ("#anncta:not(.busy):has(#annbtn.on):not(:has(#annrec.on))"
            " #annbtn .cmt-done { display: block; }") in html
    assert ("#anncta:not(.busy):has(#annbtn.on):not(:has(#annrec.on))"
            " #annbtn .done-word { display: var(--annlbl, inline); }") in html
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
    submit = body.index("if (activeRun || !sending) annAutoSubmit();")
    assert send < seed < submit


def test_speech_before_the_first_click_becomes_the_prompt_not_a_note(html):
    """Everything said before the first click is the user framing the task —
    it seeds the composer as the message's own words instead of being pulled
    onto click 1 by the nearest-segment match."""
    out = _node(["function annRecAssign("], """
var annotations = [{id: "a", t: 10}, {id: "b", t: 20}];
function annSave() {}
const intro = annRecAssign(["a", "b"], [
  {startSecond: 1, text: "overall make it cleaner"},
  {startSecond: 4, text: "and use our colors"},
  {startSecond: 10.5, text: "this button is wrong"},
  {startSecond: 19, text: "this chart too"},
]);
console.log(JSON.stringify({intro: intro,
  a: annotations[0].content, b: annotations[1].content}));
""", html)
    assert out["intro"] == "overall make it cleaner and use our colors"
    assert out["a"] == "this button is wrong"
    assert out["b"] == "this chart too"


def test_a_click_mid_sentence_splits_that_sentence_by_word_timings(html):
    """The reason word timings were asked for (D392/D393): a whisper segment is
    a sentence or several, so a segment-grained match hands the WHOLE sentence
    to whichever click its first word started nearest — and clicking mid-thought
    ("this button is wrong" click "and this chart too") is the ordinary way a
    walkthrough is spoken. With `words` on the segment, the sentence splits at
    the click that interrupted it."""
    out = _node(["function annRecAssign("], """
var annotations = [{id: "a", t: 1.0}, {id: "b", t: 3.0}];
function annSave() {}
const intro = annRecAssign(["a", "b"], [
  {startSecond: 1.1, text: "this button is wrong and this chart too", words: [
    {startSecond: 1.1, endSecond: 1.4, word: " this"},
    {startSecond: 1.4, endSecond: 1.8, word: " button"},
    {startSecond: 1.8, endSecond: 2.0, word: " is"},
    {startSecond: 2.0, endSecond: 2.4, word: " wrong"},
    {startSecond: 3.1, endSecond: 3.3, word: " and"},
    {startSecond: 3.3, endSecond: 3.5, word: " this"},
    {startSecond: 3.5, endSecond: 3.9, word: " chart"},
    {startSecond: 3.9, endSecond: 4.2, word: " too"},
  ]},
]);
console.log(JSON.stringify({intro: intro,
  a: annotations[0].content, b: annotations[1].content}));
""", html)
    assert out["a"] == "this button is wrong"
    assert out["b"] == "and this chart too"
    assert out["intro"] == ""


def test_words_before_the_first_click_are_the_intro_at_word_grain(html):
    """The intro rule is unchanged by word timings — it just cuts where the
    speaking actually crossed the first click, so framing said in the same
    breath as the first comment no longer drags the comment's words with it."""
    out = _node(["function annRecAssign("], """
var annotations = [{id: "a", t: 2.0}];
function annSave() {}
const intro = annRecAssign(["a"], [
  {startSecond: 0.0, text: "make this cleaner this button is wrong", words: [
    {startSecond: 0.0, endSecond: 0.4, word: " make"},
    {startSecond: 0.4, endSecond: 0.7, word: " this"},
    {startSecond: 0.7, endSecond: 1.2, word: " cleaner"},
    {startSecond: 2.1, endSecond: 2.3, word: " this"},
    {startSecond: 2.3, endSecond: 2.7, word: " button"},
    {startSecond: 2.7, endSecond: 2.9, word: " is"},
    {startSecond: 2.9, endSecond: 3.3, word: " wrong"},
  ]},
]);
console.log(JSON.stringify({intro: intro, a: annotations[0].content}));
""", html)
    assert out["intro"] == "make this cleaner"
    assert out["a"] == "this button is wrong"


def test_a_reply_with_no_word_timings_still_matches_by_segment(html):
    """`words: true` is answered best-effort and never refused (D392): an engine
    that has none leaves the key OFF, and a MIXED reply is possible too. Each
    segment is matched at whatever grain it arrived with — and a segment whose
    words are not all timed falls back whole rather than dropping words."""
    out = _node(["function annRecAssign("], """
var annotations = [{id: "a", t: 1.0}, {id: "b", t: 3.0}];
function annSave() {}
const intro = annRecAssign(["a", "b"], [
  {startSecond: 1.1, text: "worded segment splits here", words: [
    {startSecond: 1.1, endSecond: 1.5, word: " worded"},
    {startSecond: 1.5, endSecond: 1.9, word: " segment"},
    {startSecond: 3.1, endSecond: 3.4, word: " splits"},
    {startSecond: 3.4, endSecond: 3.6, word: " here"},
  ]},
  {startSecond: 1.2, text: "no words at all", words: []},
  {startSecond: 1.3, text: "words with no times",
   words: [{word: " words"}, {word: " with"}]},
]);
console.log(JSON.stringify({intro: intro,
  a: annotations[0].content, b: annotations[1].content}));
""", html)
    # segment 1 split by its words; segments 2 and 3 landed WHOLE on click a
    assert out["a"] == "worded segment no words at all words with no times"
    assert out["b"] == "splits here"
    assert out["intro"] == ""


def test_the_walkthrough_asks_for_word_timings(html):
    """The one transcribe call on the page sends `words: true` — the matcher
    above is only finer-grained when the reply carries them."""
    body = _block(html, "async function annRecEnd()", "\n}\n")
    assert "fused.ai.transcribe({ path, words: true })" in body


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


def test_done_sends_bare_with_no_canned_message(html):
    """Done fires the send even with an empty composer: the message may be
    empty, the annotations carry the content, and whatever the user had typed
    is theirs and goes along as the message's own words."""
    body = _block(html, "async function annDone()", "\n}\n")
    assert "if (pending && (activeRun || !sending)) annAutoSubmit();" in body
    assert "annPrefillComposer" not in body, \
        "the send path seeds nothing — there is no canned prompt to seed"


# ------------------------------------------- the app records, not this page

def test_the_walkthrough_records_through_fused_capture(html):
    """`fused.capture.audio` (SPEC §45), not a MediaRecorder: the app writes
    the file and NAMES IT before the first sample, so the stop hands
    `fused.ai.transcribe({path})` a path instead of this page uploading a blob
    it had to guess a container for."""
    begin = _block(html, "async function annRecBegin()", "\n}\n")
    assert "await fused.capture.audio(" in begin
    end = _block(html, "async function annRecEnd()", "\n}\n")
    assert "const out = await handle.stop()" in end
    assert "const path = out.path;" in end
    # the whole walkthrough path is off the browser recorder now — no blob, no
    # upload, and no extension for this page to guess
    for fn in (begin, end, _block(html, "async function annRecDiscard()", "\n}\n")):
        # call forms, not words: the comments name what this path stopped
        # doing and why, which is the part worth keeping
        assert "new MediaRecorder(" not in fn
        assert "navigator.mediaDevices" not in fn
        assert "fused.uploadFile(" not in fn
        assert "new Blob(" not in fn
    assert "function annRecExt(" not in html


def test_the_walkthrough_names_no_path_of_its_own(html):
    """The container is the BACKEND's to name (CP-5) — .m4a natively,
    fragmented mp4 or WebM where the browser encodes — and a caller `path`
    whose extension contradicts it is refused, so the default timestamped name
    under <home>/recordings is the only portable ask."""
    begin = _block(html, "async function annRecBegin()", "\n}\n")
    call = begin[begin.index("fused.capture.audio("):]
    call = call[:call.index(")")]
    assert "path" not in call, call
    assert "title:" in call


def test_the_mic_seat_is_drawn_off_the_probe(html):
    """`sources()` never prompts (CP-7), so the seat is drawn off its answer
    rather than off a click that could only ever alert — and a probe that
    FAILED is not a refusal, so the seat stays."""
    body = _block(html, "  try {\n    const src = await fused.capture.sources();",
                  "\n})();")
    assert "src.audio.available === false" in body
    assert "annRecBtn.remove();" in body
    assert 'console.warn("capture probe skipped:"' in body


def test_the_teardown_ends_the_recording_without_transcribing(html):
    """A document going away must not leave the microphone on — but the
    ending is stop(), which KEEPS the file (CP-4): a teardown the user did not
    ask for must not throw their walkthrough away."""
    body = _block(html, 'window.addEventListener("pagehide", () => {',
                  "annXORemove();")
    assert "if (handle) handle.stop().catch(() => {});" in body
    assert "cancel()" not in body
    assert "fused.ai.transcribe" not in body


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
    # or a walkthrough click's fire-and-forget crop may have none). Only where
    # the native screen shot is off, though: with it there is no prompt at all.
    assert "if (annOn && annXO && shotNativeOff) annXOStreamGet().catch" in html
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

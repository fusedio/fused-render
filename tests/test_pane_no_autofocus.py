"""A preview in the explorer's pane does not take the keyboard.

The pane is a same-origin iframe, so a page that focuses an input on boot pulls
document focus out of the shell — and the listing's arrow keys stand down the
moment focus leaves it, so previewing a file stopped the user browsing file to
file from the keyboard. It surfaced with the claude template, whose composer
autofocused, but nothing about it was claude-specific: any template with an
input would do the same.

So it is a CONTRACT, and this pins the three pieces of it that live outside the
shell's own TypeScript (frontend/.../platform/lib/frame-focus.test.ts covers that
half):

  * the param has ONE spelling, read out of the shell's own source rather than
    typed here a second time — runtime.js and the shell agree or this fails;
  * runtime.js's reader answers the way the shell's does, run for real under
    node rather than eyeballed;
  * the claude template carries no `autofocus` attribute — the one thing no
    gate inside the page can catch, since the browser queues the candidate when
    the element is inserted;
  * every live thumbnail frame is built by the one helper that makes a frame a
    picture (`thumbFrame`), counted against the iframes in the file.

Repo memory is explicit that a headless probe cannot see focus at all, which is
exactly why these are the decisions being tested and not the behaviour: the
behaviour was verified by hand in a browser, and what is left to protect is that
the decisions keep agreeing with each other.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join("fused_render", "templates", "claude", "template.html")
RUNTIME = os.path.join("fused_render", "static", "runtime.js")
SHELL = os.path.join("frontend", "src", "platform", "lib", "frame-focus.ts")


def _node(script):
    """Run a JS snippet and return its parsed stdout."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own functions")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _fn(path, opener, end):
    """The source of a function, sliced out of the file that defines it."""
    src = open(path, encoding="utf-8").read()
    a = src.index(opener)
    return src[a : src.index(end, a) + len(end)]


def _shell_param():
    """The param name the shell writes, read out of the shell's own source."""
    src = open(SHELL, encoding="utf-8").read()
    m = re.search(r'NO_FOCUS_PARAM = "([^"]+)"', src)
    assert m, "the shell no longer names the no-focus param at all"
    return m.group(1)


def test_the_shell_and_the_runtime_spell_the_param_the_same_way():
    """A signal only one side recognises is not a signal."""
    runtime = open(RUNTIME, encoding="utf-8").read()
    m = re.search(r'NO_FOCUS_PARAM = "([^"]+)"', runtime)
    assert m, "runtime.js no longer names the no-focus param"
    assert m.group(1) == _shell_param() == "_nofocus"


def test_the_runtime_reads_the_signal_the_way_the_shell_writes_it():
    """runtime.js's own reader, run for real. `_nofocus=0` is not a request —
    the value matters, not the mere presence of the key."""
    # From the constant through the end of the reader — the function is about
    # that name, so running it without it would be running something else.
    # The whole reader block: one generic `flagRequested` plus the named wrapper
    # this asks about, which is why the slice runs to the block's own end marker
    # rather than to the first closing brace.
    fn = _fn(RUNTIME, "var NO_FOCUS_PARAM", "// --- end of the flag readers ---")
    cases = ["?path=x&_nofocus=1", "_nofocus=1", "?path=x", "", "_nofocus=0"]
    got = _node(
        fn + "\nconsole.log(JSON.stringify(%s.map((s) => noFocusRequested(s))));" % json.dumps(cases)
    )
    assert got == [True, True, False, False, False]


def test_the_claude_composer_no_longer_autofocuses():
    """The attribute is browser-applied, so no gate can catch it — it has to be
    gone. Its job is done by the boot path's own (gated) focus instead."""
    html = open(TEMPLATE, encoding="utf-8").read()
    assert "autofocus" not in html


# -- The card grids (D348) -----------------------------------------------------
#
# The same contract, one surface further out: a LIVE CARD THUMBNAIL is a picture
# of an app, and focus inside a frame scrolls that frame into view — a scroll
# that propagates out to the grid's own scroller. A thumbnail that took focus as
# it mounted therefore jumped /apps to whatever row its card sits in, mid-scroll.
# So every display-only thumbnail stamps `_nofocus=1` exactly as the pane does,
# and these pin that none of the three places that build such a URL forgets it.

# platform/ui, not apps/builder: the card moved when a third app began drawing
# it (#765) and this path did not move with it, so the read raised
# FileNotFoundError on every python job.
CARD = os.path.join("frontend", "src", "platform", "ui", "AppPreviewCard.tsx")
BOOKMARK_CARDS = os.path.join("frontend", "src", "apps", "explorer", "BookmarkCards.tsx")
EMBED_SHELL = os.path.join("frontend", "src", "apps", "explorer", "Preview.tsx")


def test_every_live_thumbnail_frame_goes_through_the_one_helper():
    """`thumbFrame` is what makes a frame a picture — both URL stamps, the
    sandbox/permissions seal, and the markup that keeps it out of the tab order
    and off the scrollbar. Asserted as a COUNT against the iframes in the file,
    so a third thumbnail branch cannot ship with four of the five parts, which
    is exactly how D348 shipped `_preview` without `_nofocus`."""
    for path in (CARD, BOOKMARK_CARDS):
        src = open(path, encoding="utf-8").read()
        assert src.count("{...thumbFrame(") == src.count("<iframe"), path
        # Nothing builds a thumbnail URL by hand any more.
        assert "withNoFocus(" not in src, path
        assert "src={`/render" not in src, path


def test_the_embed_shell_forwards_the_stamp_with_the_thumbnail_flag():
    """A card peek loads the EMBED shell, and the app itself is the frame that
    shell renders inside — so the signal has to travel one hop further. It rides
    on the thumbnail flag, which already inherits through nested frames."""
    src = open(EMBED_SHELL, encoding="utf-8").read()
    assert '"&_preview=1&_nofocus=1"' in src

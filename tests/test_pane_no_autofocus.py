"""A preview in the explorer's pane does not take the keyboard.

The pane is a same-origin iframe, so a page that focuses an input on boot pulls
document focus out of the shell — and the listing's arrow keys stand down the
moment focus leaves it, so previewing a file stopped the user browsing file to
file from the keyboard. It surfaced with the claude template, whose composer
autofocused, but nothing about it was claude-specific: any template with an
input would do the same.

So it is a CONTRACT, and this pins the three pieces of it that live outside the
shell's own TypeScript (frontend/.../listing/frame-focus.test.ts covers that
half):

  * the param has ONE spelling, read out of the shell's own source rather than
    typed here a second time — runtime.js and the shell agree or this fails;
  * runtime.js's reader answers the way the shell's does, run for real under
    node rather than eyeballed;
  * the claude template no longer focuses on its own initiative — no
    `autofocus` attribute, and every composer focus goes through its `focusBox`
    gate, which is likewise run under node against the flag both ways.

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
SHELL = os.path.join(
    "frontend", "src", "apps", "explorer", "listing", "frame-focus.ts"
)


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
    fn = _fn(RUNTIME, "var NO_FOCUS_PARAM", "\n  }\n")
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


def test_every_composer_focus_goes_through_the_gate():
    """Not just the boot ones: after the first gesture focusBox IS a plain
    focus(), so a second spelling buys nothing and is the obvious way for the
    next boot-path focus to re-break the pane."""
    html = open(TEMPLATE, encoding="utf-8").read()
    assert not re.search(r"(?<![\w.])(home)?box\.focus\(\)", html)


def test_the_gate_is_silent_in_the_pane_and_ordinary_everywhere_else():
    """The template's real focusBox, run under node against the flag both ways.
    A standalone page never sees the flag; the pane's page sees it until the
    reader touches the document, at which point runtime.js clears it."""
    fn = _fn(TEMPLATE, "function focusBox(", "\n}\n")
    script = fn + """
const calls = [];
const el = { focus: () => calls.push("focused") };
globalThis.window = {};
focusBox(el);                       // standalone: no flag at all
window.__fusedNoAutofocus = true;   // embedded in the pane, untouched
focusBox(el);
window.__fusedNoAutofocus = false;  // the reader has interacted
focusBox(el);
console.log(JSON.stringify(calls));
"""
    assert _node(script) == ["focused", "focused"]

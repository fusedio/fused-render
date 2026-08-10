"""The chat pane's mode picker labels the `app` mode the way the shell does.

`paneModeLabel` is a deliberate four-line reimplementation of the shell's
`modeTitle` (frontend/src/platform/lib/mode-name.ts) — a template is a
standalone document and cannot import the shell's TS. The cost of that
duplication is exactly this: when the shell renamed the `app` mode's label from
"View" to "Preview", the template kept falling through to its capitalize and
said "App", so the pane picker and the explorer's mode switcher named the same
view two different things a strip apart.

So the agreement is pinned rather than commented: the label the template
produces for `app` is read out of the shell's own table, not typed in here a
second time — the next rename fails this test in the template that has to follow
it. Keys the two surfaces disagree on ON PURPOSE (the template says "History"
where the shell humanizes "versions") are not swept into this: agreeing about
everything is not the claim, agreeing about `app` is.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join("fused_render", "templates", "claude", "template.html")
SHELL = os.path.join("frontend", "src", "platform", "lib", "mode-name.ts")


def _shell_label(mode):
    """The shell's display name for `mode`, read out of its own source."""
    src = open(SHELL, encoding="utf-8").read()
    m = re.search(r'^\s*%s: "([^"]+)",' % re.escape(mode), src, re.M)
    assert m, "the shell no longer names %r at all" % mode
    return m.group(1)


def _pane_label(modes):
    """Run the template's real `paneModeLabel` over a list of mode keys."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own label function")
    html = open(TEMPLATE, encoding="utf-8").read()
    a = html.index("function paneModeLabel(")
    fn = html[a:html.index("\n}\n", a) + 2]
    script = fn + "\nconsole.log(JSON.stringify(%s.map(paneModeLabel)));" % json.dumps(modes)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_app_mode_is_labelled_the_way_the_shell_labels_it():
    """Both spellings: `app` is the registry key, `_app` the pane-only sentinel,
    and they are the same surface seen from two routes."""
    want = _shell_label("app")
    assert want == "Preview", "read from the shell; update the template if this changed"
    assert _pane_label(["app", "_app"]) == [want, want]


def test_the_labels_the_picker_already_agreed_on_still_hold():
    """The mapping is a lookup table, and adding a row to one is how you break
    another. `_render` is the shell's sentinel name; an unknown key still gets
    the humanizer rather than a guess."""
    assert _pane_label(["_render", "duckdb"]) == ["Rendered", "Duckdb"]

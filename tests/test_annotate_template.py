"""Source-contract tests for the annotate template's anchor strategies (§17).

The template picks a strategy per framed view — code-editor views anchor on a
LINE NUMBER, everything else on element paths and quoted text — and picking the
wrong one is silent: the gesture handlers `preventDefault()` first and bail on an
unresolvable anchor, so a mis-detected view swallows every click and drops every
selection with no error anywhere. That is exactly what happened to the markdown
notes view, a CodeMirror editor whose lineNumbers gutter is present but
**hidden**, so these pin the detection rule and the container rule that keep it
working.

The sidecar writer next door has its own behavioural tests
(tests/test_annotate_comments.py).
"""
import os

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "annotate",
    "template.html")


@pytest.fixture(scope="module")
def source():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


def test_the_line_strategy_needs_a_gutter_that_is_actually_laid_out(source):
    # `.cm-editor` alone was the probe, and the markdown notes view matched it:
    # cmVisibleLines() was then always empty, so clicks were swallowed by the
    # `!hit` bail after preventDefault, selections dropped, and stored comments
    # never painted. Testing for `.cm-lineNumbers` is NOT enough either — that
    # view ships basicSetup, so the gutter element EXISTS; it is `display:none`
    # (prose, MD-18a), which is exactly why cmVisibleLines pairs only gutter
    # elements with real height. The probe has to make the same measurement, or
    # it disagrees with the code that consumes it.
    body = source[source.index("const isCmDoc = (doc) => {"):]
    body = body[:body.index("\n    };")]
    assert '.cm-editor' in body
    assert '.cm-lineNumbers' in body
    assert "getBoundingClientRect().height > 0" in body
    # And the gutter query the strategy itself reads is the same one, filtered
    # the same way.
    assert '.cm-lineNumbers .cm-gutterElement' in source
    assert "gr.height > 0" in source


def test_a_quote_in_a_gutterless_editor_anchors_on_the_stable_content_element(
        source):
    # A gutterless editor falls through to element/quote anchors, where the
    # natural container is a `.cm-line` div — whose nth-of-type index shifts as
    # lines mount and unmount on scroll, so the path drifts onto another line.
    # `.cm-content` is structurally stable; the quote and its occurrence index do
    # the locating inside it.
    body = source[source.index('doc.addEventListener("mouseup"'):]
    body = body[:body.index('doc.addEventListener("click"')]
    assert 'cont.closest(".cm-content")' in body
    assert "if (content) cont = content;" in body

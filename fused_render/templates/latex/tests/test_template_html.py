"""Static regression guards for template.html behaviour that has no JS harness.

These are structural assertions parsed from the source, not a browser test — the
viewer can't run without a fused-render server. They pin down the exact bug
Bugbot caught: a pane-width change (sidebar / mode / splitter) must NOT refetch
and re-render the PDF (renderPdf cache-busts, destroys the doc, and blanks the
page), because PDF canvases are sized from S.scale, not pane width. Only a new
compile or a zoom may re-render.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/latex/tests/test_template_html.py -o addopts=""
"""
import os
import re

import pytest

_LATEX = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HTML = os.path.join(_LATEX, "template.html")


@pytest.fixture(scope="module")
def src():
    with open(_HTML, encoding="utf-8") as f:
        return f.read()


def _fn_body(src, name):
    m = re.search(r"function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"{name}() not found in template.html"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"{name}() has unbalanced braces")


def test_width_change_paths_do_not_re_render_the_pdf(src):
    for name in ("relayoutPanes", "setSideCollapsed", "setMode"):
        assert "renderPdf" not in _fn_body(src, name), \
            f"{name} must not re-render the PDF on a width change"


def test_side_and_mode_toggles_go_through_relayout(src):
    for name in ("setSideCollapsed", "setMode"):
        assert "relayoutPanes()" in _fn_body(src, name)


def test_splitter_drag_relayouts_without_re_rendering(src):
    m = re.search(r"// splitter drag(.*?)\}\)\(\);", src, re.S)
    assert m, "splitter drag block not found"
    block = m.group(1)
    assert "relayoutPanes()" in block and "renderPdf" not in block


def test_compile_and_zoom_still_re_render(src):
    assert "await renderPdf(" in src, "a fresh compile must render its PDF"
    assert "renderPdf" in _fn_body(src, "setZoom"), "a zoom change must re-render at the new scale"


def test_relayout_only_remeasures_the_editor(src):
    body = _fn_body(src, "relayoutPanes")
    assert "requestMeasure" in body   # CodeMirror is the only width-sensitive pane

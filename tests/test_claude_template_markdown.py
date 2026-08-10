"""Node-probe tests for the claude template's real-markdown renderMd (task 1).

`renderMd` used to be a hand-rolled regex markdown-lite (no tables, no fenced
code language classes, no link-target hardening beyond a manual escape). It
is now marked + DOMPurify: marked does the parsing (GFM tables, fenced code
with a `language-xxx` class, a custom `link` renderer that force-opens new
tabs), DOMPurify is the only thing between marked's raw output and
`bodyEl.innerHTML` — marked itself never touches model-authored raw HTML, so
an `<img onerror=…>` sails through marked unchanged and DOMPurify is what has
to catch it.

Node has no DOM, and DOMPurify's UMD factory knows it: called with no
`window` global, it falls back to a stub exposing only
`{version, removed, isSupported}` — no `.sanitize` at all (calling it throws
`TypeError: ... is not a function`, confirmed by hand against the vendored
purify.min.js before writing this file). Building a real DOM by hand to get
a working `.sanitize()` in node is exactly the jsdom-shaped dependency this
task was told not to add, and the one already living in
tests/test_claude_app_state.py's `_DOM` is a fixed fake element tree for an
outline walker — nowhere near enough surface for DOMPurify's own HTML
parsing/serialization.

So the probe spies DOMPurify instead of loading purify.min.js: a passthrough
`sanitize(dirty, opts)` that returns `dirty` unchanged and records every call.
That lets the marked-driven parts of the pipeline run for real (tables,
fenced-code language classes, the custom link renderer, fence-mid-stream
tolerance) and lets the *wiring* into DOMPurify be asserted directly (is
sanitize even called? with what config?) without re-proving DOMPurify's own
already-audited stripping behaviour, which is a third-party guarantee, not
this template's code. `test_raw_html_is_neutralized` is adapted along these
lines — see its docstring — and noted as live-check-covered in the task
report.
"""
import json
import os
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "claude",
    "template.html")
VENDOR_DIR = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "claude",
    "vendor")


@pytest.fixture(scope="module")
def source():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


def _block(src, start, end):
    """The shipping source from `start` up to and including `end`, verbatim."""
    i = src.index(start)
    j = src.index(end, i) + len(end)
    return src[i:j]


# Anchors into the shipping renderMd/attachCodeCopy block (template.html,
# ~4434-4470 pre-task, replaced by Step 3). Unique to the NEW implementation —
# absent from the old hand-rolled renderMd — so this errors (RED) until the
# replacement lands, rather than silently extracting the wrong function.
_RENDER_START = "let _md;  // configured once, lazily"
_RENDER_END = "pre.appendChild(b);\n  });\n}"


def _node(script, tmp_path):
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the template's own JS")
    harness = tmp_path / "harness.mjs"
    harness.write_text(script, encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class _NodeProbe:
    """Runs the shipping renderMd block under node, marked+hljs for real,
    DOMPurify spied (see module docstring)."""

    def __init__(self, block, tmp_path):
        self._block = block
        self._tmp_path = tmp_path
        with open(os.path.join(VENDOR_DIR, "marked.min.js"), encoding="utf-8") as h:
            self._marked_src = h.read()
        with open(os.path.join(VENDOR_DIR, "highlight.min.js"), encoding="utf-8") as h:
            self._hljs_src = h.read()
        self.last_sanitize_calls = None

    def render_md(self, text):
        script = self._marked_src + "\n" + self._hljs_src + "\n" + f"""
const _sanitizeCalls = [];
global.DOMPurify = {{
  sanitize(dirty, opts) {{ _sanitizeCalls.push({{ dirty, opts }}); return dirty; }},
}};
const DOMPurify = global.DOMPurify;
// No `document` shim: window.hljs stays undefined, so renderMd's own
// `if (window.hljs)` guard skips the highlightElement/box branch — same
// guard that keeps this safe in node as it is in a real (DOM-having)
// browser. What these tests check (language-xxx classes, tables, link
// targets, fence tolerance) comes from marked's own output, not hljs.
global.window = {{}};
{self._block}
const html = renderMd({json.dumps(text)});
console.log(JSON.stringify({{ html, sanitizeCalls: _sanitizeCalls }}));
"""
        result = _node(script, self._tmp_path)
        self.last_sanitize_calls = result["sanitizeCalls"]
        return result["html"]


@pytest.fixture()
def node_probe(source, tmp_path):
    block = _block(source, _RENDER_START, _RENDER_END)
    return _NodeProbe(block, tmp_path)


def test_tables_render_as_html_tables(node_probe):
    html = node_probe.render_md("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table" in html and "<td>1</td>" in html


def test_raw_html_is_neutralized(node_probe):
    # marked itself does NOT strip this (verified by hand against the
    # vendored marked.min.js: raw inline HTML passes through unchanged) — the
    # only thing standing between it and innerHTML is DOMPurify.sanitize. The
    # spy is a passthrough (no real DOM in node, see module docstring), so
    # this asserts the WIRING — sanitize is actually called, on text that
    # still contains the raw attack — not DOMPurify's own stripping, which
    # is covered by the live check.
    node_probe.render_md('hello <img src=x onerror=alert(1)> world')
    calls = node_probe.last_sanitize_calls
    assert any("onerror" in c["dirty"] for c in calls), (
        "renderMd must hand DOMPurify.sanitize the raw marked output "
        "(unstripped) — sanitize is the only thing that removes onerror="
    )
    # The restrictive config travels with every call, not just this one.
    doc_call = max(calls, key=lambda c: len(c["dirty"]))
    assert "iframe" in (doc_call["opts"] or {}).get("FORBID_TAGS", [])


def test_unclosed_fence_mid_stream_still_renders(node_probe):
    html = node_probe.render_md("before\n```py\nprint(1)")
    assert "<code" in html and "print(1)" in html


def test_fenced_code_gets_language_class_for_hljs(node_probe):
    html = node_probe.render_md("```python\nx = 1\n```")
    assert 'language-python' in html or 'hljs' in html


def test_links_open_in_new_tab(node_probe):
    html = node_probe.render_md("[x](https://example.com)")
    assert 'target="_blank"' in html and 'rel="noopener' in html


def test_link_href_is_run_through_sanitize(node_probe):
    # The custom link renderer sanitizes href itself (marked's own link
    # escaping is not a substitute for DOMPurify on a URL that could be
    # `javascript:` or carry a quote-breaking payload) — pin that it's
    # actually called for the href, not just the final document.
    node_probe.render_md("[x](https://example.com)")
    hrefs = [c["dirty"] for c in node_probe.last_sanitize_calls]
    assert "https://example.com" in hrefs

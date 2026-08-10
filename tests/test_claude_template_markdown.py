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
this template's code.

Config-level vs behavior-level, so it's clear which is which at a glance:
  - Behavior-level (real assertions about the actual output): tables,
    fenced-code language classes, the link renderer's target/rel injection
    and href attribute-escaping, fence-mid-stream tolerance. These would
    fail if the template's own code regressed, independent of DOMPurify.
  - Config-level (wiring only, per the passthrough-spy limitation above):
    `test_raw_html_reaches_sanitize_unstripped_with_forbid_config` checks
    that the dangerous text reaches DOMPurify.sanitize un-stripped, with the
    restrictive FORBID_TAGS config attached — NOT that it comes out clean.
    Actual neutralization of onerror=/<script>/<iframe> is DOMPurify's own
    (already-audited) contract, exercised for real only by the live check
    (task-1-report.md, skipped in this environment per orchestrator
    instruction — the controller runs it separately).
"""
import json
import os
import re
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
# attachCodeCopy alone (a suffix of the block above) — for tests that only
# need the copy/highlight pass, not marked/DOMPurify at all.
_ATTACH_START = "function attachCodeCopy(rootEl) {"
_ATTACH_END = "pre.appendChild(b);\n  });\n}"


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


def test_raw_html_reaches_sanitize_unstripped_with_forbid_config(node_probe):
    # CONFIG-LEVEL, not behavior-level (see module docstring): the spy is a
    # passthrough, so this does NOT prove onerror= gets removed — it proves
    # renderMd hands DOMPurify.sanitize the raw, still-dangerous marked
    # output (marked itself does not strip this — verified by hand against
    # the vendored marked.min.js: raw inline HTML passes through unchanged),
    # together with the restrictive FORBID_TAGS config. Real neutralization
    # is DOMPurify's own contract, exercised by the live check only.
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


def test_href_is_attribute_escaped_not_sanitized(node_probe):
    # BEHAVIOR-LEVEL. DOMPurify.sanitize() sanitizes HTML fragments, not
    # URLs — calling it per-href (the original implementation) sailed a
    # `javascript:` scheme through unchanged AND double-encoded a literal
    # `&` in an otherwise-ordinary URL. The href's real protection is the
    # single document-level DOMPurify.sanitize() call (its allowed-URI
    # check), covered by the live check for the same reason as the test
    # above; the template's own job is just not letting the raw href break
    # out of the `"..."` attribute it's interpolated into. Pin both halves:
    # exactly one sanitize call per render (the document, not the href), and
    # the href itself is `&`/`"`-escaped in the output.
    html = node_probe.render_md('[x](https://example.com/?a=1&b="2")')
    assert len(node_probe.last_sanitize_calls) == 1
    assert 'href="https://example.com/?a=1&amp;b=&quot;2&quot;"' in html


def test_javascript_href_is_not_specially_encoded_by_the_link_renderer(node_probe):
    # BEHAVIOR-LEVEL, narrow scope: pins that the link renderer no longer
    # runs href through DOMPurify.sanitize (which would have re-serialized
    # it as inert-looking text without blocking the scheme). Whether the
    # DOWNSTREAM document-level sanitize actually strips a javascript: href
    # is DOMPurify's own contract — not re-tested here, see the live check.
    html = node_probe.render_md("[x](javascript:alert(1))")
    assert len(node_probe.last_sanitize_calls) == 1
    assert 'href="javascript:alert(1)"' in html


# --- review-fix coverage (findings 1/2/3/6) ---------------------------------


def test_hljs_theme_padding_and_scroll_are_overridden_for_the_assistant_pre(
        source):
    # BEHAVIOR-LEVEL (CSS, source-contract style — no DOM to lay out in
    # node). vendor/hljs.css ships its own unmodified `pre code.hljs{
    # padding:1em;overflow-x:auto}` (github/github-dark). Its <link> is
    # appended after this inline <style> at runtime and would otherwise win
    # the cascade over `.assistant pre code{padding:0}`, stacking an extra
    # 1em of padding and nesting a second horizontal scrollbar inside this
    # pre's own overflow-x:auto. Pin the override exists with both
    # properties fixed, scoped to .hljs specifically (not all pre code).
    style = source[source.index("<style>"):source.index("</style>")]
    rule = re.search(r"\.assistant pre code\.hljs\s*\{([^}]*)\}", style)
    assert rule, "no .assistant pre code.hljs override rule found"
    assert re.search(r"padding:\s*0\b", rule.group(1))
    assert re.search(r"overflow-x:\s*visible\b", rule.group(1))


def test_attach_code_copy_skips_languages_hljs_does_not_have(source, tmp_path):
    # FIX for finding 2 (and half of finding 6): the hljs "common" bundle is
    # missing plenty of languages Claude reaches for routinely (dockerfile,
    # mermaid, hcl/terraform, powershell, elixir, scala, latex, ...) —
    # highlightElement console.warns once per call for any of them.
    # getLanguage() is the documented pre-check; verify attachCodeCopy
    # actually uses it by spying highlightElement itself (real getLanguage,
    # real vendored highlight.min.js) against a registered vs. two
    # unregistered languages, through a hand-built minimal element stub (no
    # jsdom: only the handful of DOM properties/methods attachCodeCopy
    # itself touches — className, classList.contains, querySelector,
    # appendChild — same narrow-stub approach as tests/test_claude_app_state
    # .py's `_DOM`).
    with open(os.path.join(VENDOR_DIR, "highlight.min.js"), encoding="utf-8") as h:
        hljs_src = h.read()
    block = _block(source, _ATTACH_START, _ATTACH_END)
    script = hljs_src + "\n" + f"""
const highlighted = [];
hljs.highlightElement = (el) => highlighted.push(el.className);
global.window = {{ hljs }};
// attachCodeCopy's copy-button pass (unrelated to this test) still runs and
// calls document.createElement — a plain settable-properties stub is all
// that needs (no jsdom: not laid out, not rendered, just assigned to).
global.document = {{ createElement: () => ({{}}) }};

function fakeCodeEl(cls) {{
  const classes = cls.split(/\\s+/);
  return {{ className: cls, classList: {{ contains: (c) => classes.includes(c) }} }};
}}
function fakePre() {{ return {{ querySelector: () => null, appendChild: () => {{}} }}; }}
const codeEls = [
  fakeCodeEl("language-python"),      // registered — must highlight
  fakeCodeEl("language-mermaid"),     // NOT in the common bundle — must skip
  fakeCodeEl("language-dockerfile"),  // NOT in the common bundle — must skip
];
const rootEl = {{
  querySelectorAll(sel) {{ return sel === "pre code" ? codeEls : [fakePre()]; }},
}};
{block}
attachCodeCopy(rootEl);
console.log(JSON.stringify({{ highlighted }}));
"""
    got = _node(script, tmp_path)
    assert got["highlighted"] == ["language-python"]


def test_attach_code_copy_is_idempotent_against_double_highlighting(
        source, tmp_path):
    # An element already carrying the `hljs` class (a prior attachCodeCopy
    # pass) must not be re-highlighted — hljs.highlightElement on an
    # already-highlighted element is redundant work at best.
    with open(os.path.join(VENDOR_DIR, "highlight.min.js"), encoding="utf-8") as h:
        hljs_src = h.read()
    block = _block(source, _ATTACH_START, _ATTACH_END)
    script = hljs_src + "\n" + f"""
let calls = 0;
hljs.highlightElement = () => {{ calls++; }};
global.window = {{ hljs }};
function fakeCodeEl(cls) {{
  const classes = cls.split(/\\s+/);
  return {{ className: cls, classList: {{ contains: (c) => classes.includes(c) }} }};
}}
const rootEl = {{
  querySelectorAll(sel) {{
    return sel === "pre code" ? [fakeCodeEl("language-python hljs")] : [];
  }},
}};
{block}
attachCodeCopy(rootEl);
console.log(JSON.stringify({{ calls }}));
"""
    got = _node(script, tmp_path)
    assert got["calls"] == 0


def test_render_md_never_invokes_hljs_even_when_present(source, tmp_path):
    # FIX for finding 6: hljs highlighting must live only in
    # attachCodeCopy's finish/static pass, never in renderMd's own
    # per-frame path (called on every animation frame while streaming —
    # unprofiled real DOM work, plus color flicker as a partial fence's
    # language guess changes). Prove it structurally: even with a
    # `window.hljs` present and ready to fire, renderMd's output pipeline
    # never touches it.
    with open(os.path.join(VENDOR_DIR, "marked.min.js"), encoding="utf-8") as h:
        marked_src = h.read()
    block = _block(source, _RENDER_START, _RENDER_END)
    script = marked_src + "\n" + f"""
let hljsCalls = 0;
global.window = {{ hljs: {{ getLanguage: () => true, highlightElement: () => {{ hljsCalls++; }} }} }};
global.DOMPurify = {{ sanitize(dirty, opts) {{ return dirty; }} }};
const DOMPurify = global.DOMPurify;
{block}
renderMd("```python\\nx = 1\\n```");
console.log(JSON.stringify({{ hljsCalls }}));
"""
    got = _node(script, tmp_path)
    assert got["hljsCalls"] == 0


def test_render_md_falls_back_to_escaped_text_when_vendor_libs_missing(
        source, tmp_path):
    # FIX for finding 3 (renderMd half): a failed or still-in-flight vendor
    # load must never throw inside the typer's rAF tick — that would kill
    # the drain loop with `raf` already nulled, leaving the reply
    # permanently blank with nothing in the DOM to explain why. renderMd
    # checks for marked/DOMPurify before touching either identifier (a bare
    # reference to an undeclared global throws; `typeof` does not) and
    # falls back to escaped plain text — never blank, never formatted.
    # Deliberately no marked, no DOMPurify, no window at all: the worst case.
    block = _block(source, _RENDER_START, _RENDER_END)
    script = f"""
{block}
const html = renderMd("hello <b>world</b> & friends");
console.log(JSON.stringify({{ html }}));
"""
    got = _node(script, tmp_path)
    assert got["html"] == "<pre>hello &lt;b&gt;world&lt;/b&gt; &amp; friends</pre>"


def test_vendor_load_failure_surfaces_a_visible_error(source):
    # FIX for finding 3 (loader half, source-contract style: the loader is
    # boot-time wiring against `fused`/`document`, not practically
    # executable in a bare node probe). The IIFE used to have no .catch at
    # all — a failed script load meant every future renderMd() call hit the
    # escape-only fallback above with nothing on screen explaining why. Pin
    # that a failure is now caught and surfaced.
    loader = _block(source, "// A failed load must not fail SILENTLY", "})();")
    assert "try {" in loader
    assert "catch (err)" in loader
    assert "addError(" in loader

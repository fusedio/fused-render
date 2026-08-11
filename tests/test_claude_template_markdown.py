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
// No `document` shim needed: renderMd itself no longer touches
// hljs/window at all (that moved to attachCodeCopy's finish/static-render
// pass — see test_render_md_never_invokes_hljs_even_when_present below).
// `window` is set here only because attachCodeCopy is textually part of
// this extracted block (a function declaration — its body, which does
// reference `window.hljs`, never runs unless called, and these tests never
// call it) and because it's harmless, defensive insurance against that
// changing. What these tests check (language-xxx classes, tables, link
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


def test_the_dompurify_config_is_exactly_the_intended_one(node_probe):
    # CONFIG-LEVEL, and the reason it is pinned to the byte: every probe in
    # this file stubs DOMPurify (see the module docstring), so a config that
    # silently WIDENS — one more allowed tag, an `ADD_TAGS`, a loosened
    # `ALLOWED_URI_REGEXP` — changes nothing any other test here can see, and
    # the real stripping is only ever exercised by the live browser check. So
    # the wiring itself is the contract: exact sets, and explicit ABSENCE
    # assertions for the two keys that would re-open what FORBID_TAGS closes.
    #   - FORBID_TAGS: the four tags a model-authored reply has no business
    #     emitting — `style` (it can restyle the whole app), `form`/`input`
    #     (a credential prompt drawn inside the transcript), `iframe`.
    #   - ADD_ATTR: exactly target+rel, and only because the link renderer
    #     emits `target="_blank" rel="noopener noreferrer"` itself; DOMPurify
    #     would otherwise strip the very hardening that renderer adds.
    #   - NO ADD_TAGS: re-allowing anything by name is how `style`/`iframe`
    #     come back through the front door.
    #   - NO ALLOWED_URI_REGEXP: absent means DOMPurify's own default URI
    #     allow-list applies, which is what blocks a `javascript:` href (see
    #     test_javascript_href_is_not_specially_encoded_by_the_link_renderer:
    #     the template deliberately does NOT neutralize the scheme itself, so
    #     this key is load-bearing by its absence).
    node_probe.render_md("hello [x](https://example.com)")
    calls = node_probe.last_sanitize_calls
    assert len(calls) == 1, "one document-level sanitize call per render"
    opts = calls[0]["opts"] or {}
    assert set(opts["FORBID_TAGS"]) == {"style", "form", "input", "iframe"}
    assert opts["ADD_ATTR"] == ["target", "rel"]
    assert "ADD_TAGS" not in opts, (
        "an ADD_TAGS entry re-allows by name what FORBID_TAGS just closed"
    )
    assert "ALLOWED_URI_REGEXP" not in opts, (
        "DOMPurify's default URI allow-list is what blocks javascript: hrefs — "
        "overriding it is how a scheme the link renderer passes through lands"
    )
    # ...and nothing else was configured: a new key is a deliberate decision,
    # not something that arrives unnoticed with an unrelated edit.
    assert set(opts) == {"FORBID_TAGS", "ADD_ATTR"}, sorted(opts)


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


def _style(source):
    """The template's inline stylesheet, comments stripped.

    The comments matter: several of them QUOTE css (the hljs override's own
    comment contains ``pre code.hljs{padding:1em}``), so any brace-counting
    walk over the raw text finds rules that do not exist.
    """
    style = source[source.index("<style>"):source.index("</style>")]
    return re.sub(r"/\*.*?\*/", "", style, flags=re.S)


def _rules(style, needle):
    """Every rule in `style` whose selector list mentions `needle`, as
    (list-of-selectors, declarations)."""
    out = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", style):
        selectors = [s.strip() for s in match.group(1).split(",") if s.strip()]
        if any(needle in s for s in selectors):
            out.append((selectors, match.group(2)))
    return out


def _class_likes(sel):
    # class-likes: .class, [attr=...], :root (this codebase's only
    # zero-argument pseudo-class in play here) — each worth one, same as
    # a real browser's specificity algorithm.
    return (len(re.findall(r"\.[\w-]+", sel))
            + len(re.findall(r"\[[^\]]+\]", sel))
            + len(re.findall(r":root\b", sel)))


def test_hljs_theme_padding_and_scroll_are_overridden_in_every_scope(source):
    # BEHAVIOR-LEVEL (CSS specificity, source-contract style — no DOM to lay
    # out in node). vendor/hljs.css ships TWO same-purpose rules, and each
    # needs its own same-or-higher-scope override or the doubled-padding +
    # nested-scrollbar bug reproduces in that theme:
    #   - dark/default (unscoped):                   pre code.hljs
    #   - light (:root[data-theme="light"]-scoped):   ...  pre code.hljs
    # A first-pass fix added only the dark-scope override (.assistant +
    # .hljs — 2 class-likes), which beats the dark vendor rule's 1 but NOT
    # the light vendor rule's 3 (:root + [data-theme="light"] + .hljs) — on
    # light theme the vendor rule still won and the bug reproduced there
    # even though dark/default was fixed.
    #
    # And it must hold for BOTH SCOPES a highlighted <pre> can appear in, not
    # just the transcript: `buildPlanCard` runs the same renderMd +
    # attachCodeCopy pair over `input.plan` inside a `.perm` card (D246), so
    # an `.assistant`-only override left a plan's code blocks with the vendor
    # padding unopposed — the identical bug, one call site over. Four
    # overrides, therefore: {.assistant, .perm} x {dark, light}. Each is
    # checked against its same-theme vendor counterpart on specificity alone
    # (a pure count for these flat, combinator-free selectors), which holds
    # regardless of which stylesheet the browser loads/parses later — the
    # whole point of fixing it this way rather than relying on source order.
    with open(os.path.join(VENDOR_DIR, "hljs.css"), encoding="utf-8") as h:
        vendor_css = h.read()
    vendor_dark_sel = "pre code.hljs"
    vendor_light_sel = ':root[data-theme="light"] pre code.hljs'
    assert vendor_dark_sel + "{display:block;overflow-x:auto;padding:1em}" in vendor_css
    assert vendor_light_sel + "{display:block;overflow-x:auto;padding:1em}" in vendor_css

    rules = _rules(_style(source), "pre code.hljs")
    assert rules, "no `pre code.hljs` override left in the template at all"
    overrides = []
    for selectors, decls in rules:
        assert re.search(r"padding:\s*0\b", decls), (selectors, decls)
        assert re.search(r"overflow-x:\s*visible\b", decls), (selectors, decls)
        overrides += selectors
    light = ':root[data-theme="light"] '
    assert set(overrides) == {
        ".assistant pre code.hljs",
        ".perm pre code.hljs",
        light + ".assistant pre code.hljs",
        light + ".perm pre code.hljs",
    }, sorted(overrides)

    # .hljs -> 1; :root + attr + .hljs -> 3
    assert (_class_likes(vendor_dark_sel), _class_likes(vendor_light_sel)) == (1, 3)
    for scope in (".assistant", ".perm"):
        dark_sel = "%s pre code.hljs" % scope
        light_sel = light + dark_sel
        # scope + .hljs -> 2; :root + attr + scope + .hljs -> 4
        assert (_class_likes(dark_sel), _class_likes(light_sel)) == (2, 4)
        assert _class_likes(dark_sel) > _class_likes(vendor_dark_sel), (
            "%s dark-scope override no longer beats the dark vendor rule" % scope
        )
        assert _class_likes(light_sel) > _class_likes(vendor_light_sel), (
            "%s light-scope override does not beat the light vendor rule "
            "on specificity" % scope
        )


def test_the_copy_button_is_styled_in_every_scope_attach_code_copy_runs_in(source):
    # The other half of the same finding: attachCodeCopy(body) is called on a
    # PLAN card, and every `.copybtn` rule was `.assistant`-scoped — so a plan
    # containing a code block got a real button with no styling at all, an
    # unstyled inline "copy" word sitting in the middle of the plan's prose
    # (and, unpositioned, in normal flow rather than pinned to the pre).
    # Every `.copybtn` rule must therefore cover `.perm pre` wherever it
    # covers `.assistant pre`, and `.perm pre` must be a positioning context
    # for the absolute placement to resolve against.
    style = _style(source)
    rules = _rules(style, ".copybtn")
    assert rules, "no .copybtn rules at all"
    for selectors, decls in rules:
        variants = {s.replace(".assistant", "").replace(".perm", "")
                    for s in selectors}
        for shape in variants:
            assert any(s == ".assistant" + shape for s in selectors), (
                "no .assistant variant of %r" % shape)
            assert any(s == ".perm" + shape for s in selectors), (
                "a .copybtn rule reaches the transcript but not a plan card: "
                "%r (finding 2 reproducing)" % (selectors,))
        assert decls.strip()
    perm_pre = _rules(style, ".perm pre")
    box = [decls for selectors, decls in perm_pre if ".perm pre" in selectors]
    assert box, "no `.perm pre` box rule"
    assert re.search(r"position:\s*relative\b", box[0]), (
        "`.perm pre` is not a positioning context, so the copy button escapes "
        "to the nearest positioned ancestor instead of its own pre"
    )


def test_block_markdown_does_not_render_under_pre_wrap(source):
    # marked emits BLOCK html separated by literal newlines — verified against
    # the vendored marked.min.js: "a\n\nb\n\n- x" parses to
    # `"<p>a</p>\n<p>b</p>\n<ul>\n<li>x</li>\n</ul>\n"`. Under `white-space:
    # pre-wrap` every one of those newlines is a rendered blank line: between
    # paragraphs, INSIDE the list, and one trailing the reply. `.toolchip,
    # .thinking` already carried `white-space: normal` as a local fix for
    # exactly this; the two elements renderMd's output actually lands in are
    # `.assistant .body` (the streaming target and addAssistantTurn's) and
    # `.seg-text` (one prose segment), and they must not be pre-wrap either.
    # `.assistant pre` keeps its own `white-space: pre` — that is the one place
    # literal whitespace IS the content, and renderMd's vendor-less fallback
    # relies on it.
    style = _style(source)
    for selector in (".assistant .body", ".seg-text"):
        applies = [decls for selectors, decls in _rules(style, selector)
                   if selector in selectors]
        assert applies, "no rule for %s" % selector
        declared = " ".join(applies)
        assert re.search(r"white-space:\s*normal\b", declared), (
            "%s must declare white-space: normal — pre-wrap turns marked's "
            "block separators into visible blank lines" % selector)
        assert "pre-wrap" not in declared, (
            "%s is still pre-wrap (finding 1 reproducing)" % selector)
    # ...and a <p> margin, or the UA default 1em rules the paragraph gap in a
    # column whose headings use 10px and whose lists use 4px.
    para = [decls for selectors, decls in _rules(style, ".assistant p")
            if ".assistant p" in selectors]
    assert para, "no `.assistant p` rule: marked's paragraphs get the UA 1em"
    assert re.search(r"margin:", para[0])


def test_a_single_newline_still_breaks_the_line_without_pre_wrap(node_probe):
    # The compensating half of dropping pre-wrap, and why it loses nothing: a
    # lone newline INSIDE a paragraph is a line break Claude typed, and
    # `breaks: true` renders it as a real <br> rather than leaving it to the
    # cascade. So the newlines that stop rendering are exactly the ones marked
    # inserted BETWEEN blocks (not content), while the model's own survive as
    # markup — which is what makes `white-space: normal` the right fix rather
    # than a trade.
    assert node_probe.render_md("a\nb\n\nc") == "<p>a<br>b</p>\n<p>c</p>\n"


def test_marked_block_output_really_is_newline_separated(source, tmp_path):
    # The premise of the test above, taken from the vendored library rather
    # than from a comment: if a future marked stopped separating blocks with
    # literal newlines the whitespace rule would be harmless rather than
    # load-bearing, and this test says which world we are in.
    with open(os.path.join(VENDOR_DIR, "marked.min.js"), encoding="utf-8") as h:
        marked_src = h.read()
    script = marked_src + """
global.window = {};
global.DOMPurify = { sanitize: (d) => d };
const DOMPurify = global.DOMPurify;
console.log(JSON.stringify({ html: marked.parse("a\\n\\nb\\n\\n- x") }));
"""
    got = _node(script, tmp_path)
    assert got["html"] == "<p>a</p>\n<p>b</p>\n<ul>\n<li>x</li>\n</ul>\n"


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
// The copy-button pass reads a block's text before the button joins it
// (querySelector/querySelectorAll) and inserts the button FIRST when there is a
// first child to insert before — see attachCodeCopy. None of that is what this
// test measures; the surface just has to exist.
function fakePre() {{
  return {{
    querySelector: () => null, querySelectorAll: () => [],
    textContent: "", firstChild: null,
    appendChild: () => {{}}, insertBefore: () => {{}},
  }};
}}
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


def test_the_copy_button_copies_the_block_not_its_own_label(source, tmp_path):
    # TWO bugs at one seam, which is why they are pinned together.
    #
    # 1. The button used to read `pre.textContent` INSIDE its own click handler,
    #    by which time the button was a child of that pre — so every block with
    #    no `<code>` element (renderMd's vendor-less fallback, and every tool
    #    chip's payload: a diff, a command, an output dump) copied its own
    #    contents with the word "copy" glued to the end. The text is captured
    #    before the button is inserted now.
    # 2. An Edit chip's diff is one `<span>` PER LINE and carries no newline
    #    characters at all — the spans are `display: block`, and a "\n" text node
    #    between two blocks renders as an extra empty line inside every coloured
    #    band. The line breaks a COPY needs are therefore put back here, which is
    #    what keeps a copied diff a diff instead of one run-together line.
    #
    # Also pins that the button is inserted FIRST: inside a scrolling pre it is
    # `position: sticky`, and sticky can only hold an element at the top of the
    # scrollport from a flow position at the top of the box.
    block = _block(source, _ATTACH_START, _ATTACH_END)
    script = f"""
const copied = [];
// defineProperty, not assignment: node ships its own read-only `navigator`.
Object.defineProperty(globalThis, "navigator", {{
  value: {{ clipboard: {{ writeText: (t) => copied.push(t) }} }},
  configurable: true,
}});
global.window = {{}};   // no hljs: this test is about the copy pass only
const buttons = [];
global.document = {{ createElement: () => {{
  const b = {{}};
  buttons.push(b);
  return b;
}} }};
function span(t) {{ return {{ textContent: t }}; }}
// A diff pre, as fillToolChipBody builds it: span-per-line, no newlines.
const diffPre = {{
  kids: [span("- old"), span("+ new")],
  textContent: "- old+ new",
  firstChild: {{}},
  querySelector: () => null,
  querySelectorAll(sel) {{ return sel === ":scope > span" ? this.kids : []; }},
  insertBefore(n) {{ this.first = n; }},
  appendChild(n) {{ this.last = n; }},
}};
// A plain payload pre (a Bash command, an output dump): text, no children.
const textPre = {{
  textContent: "ls -la",
  firstChild: {{ nodeType: 3 }},   // a real <pre>text</pre> has a text node
  querySelector: () => null,
  querySelectorAll: () => [],
  insertBefore(n) {{ this.first = n; }},
  appendChild(n) {{ this.last = n; }},
}};
// A markdown code block: the <code> child is the authority.
const codePre = {{
  textContent: "x = 1copy",
  firstChild: {{}},
  querySelector: (sel) => (sel === "code" ? {{ textContent: "x = 1" }} : null),
  querySelectorAll: () => [],
  insertBefore(n) {{ this.first = n; }},
  appendChild(n) {{ this.last = n; }},
}};
const pres = [diffPre, textPre, codePre];
const rootEl = {{
  querySelectorAll: (sel) => (sel === "pre code" ? [] : pres),
}};
{block}
attachCodeCopy(rootEl);
buttons.forEach((b) => b.onclick());
console.log(JSON.stringify({{
  copied,
  first: pres.map((p) => p.first === buttons[pres.indexOf(p)]),
}}));
"""
    got = _node(script, tmp_path)
    assert got["copied"] == ["- old\n+ new", "ls -la", "x = 1"]
    assert got["first"] == [True, True, True], (
        "the copy button must be inserted first — sticky cannot pin it otherwise")


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

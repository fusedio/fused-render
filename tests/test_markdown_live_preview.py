"""Behavioural tests for the markdown template's Live Preview (SPEC §32, MD-18a).

Unlike tests/test_markdown_template.py — which pins template *source* the way
the runtime.js wiring assertions do (D137) — these run the real decoration
builder against the real vendored markdown grammar, through
scripts/vendor-codemirror/live-preview-probe.mjs.

That is deliberate. buildDecorations is the one part of this template whose
correctness is invisible in a diff, because it depends entirely on what the
grammar calls each range, and the grammar surprised us twice:

* `---\\ntitle: x\\n---` does NOT parse as frontmatter — it parses as a
  HorizontalRule followed by a SetextHeading2, so a YAML block rendered as a
  horizontal rule plus a large heading until the builder learned to suppress
  the tree inside it;
* the inner brackets of `[[Wiki]]` parse as a `Link` node (and `![[embed]]` as
  an `Image`), so an "is this code?" guard that listed Link/Image silently
  stopped every wikilink from rendering at all.

Neither is expressible as an assertion about the source. Both are one line here.

The probe needs node and scripts/vendor-codemirror/node_modules, which is
gitignored — these skip rather than fail where the vendor deps were never
installed, and the source-contract tests still cover the rest.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "scripts", "vendor-codemirror")
PROBE = os.path.join(VENDOR, "live-preview-probe.mjs")
TEMPLATE = os.path.join(
    ROOT, "fused_render", "templates", "markdown", "template.html")

NOTE = """---
title: Front Matter
tags: [a, b]
---

# Heading one

Some **bold** and *ital* and ~~strike~~ and `inline` text.

> a quote

- [ ] todo item
- [x] done item

---

| a | b |
|---|--:|
| 1 | 2 |

![alt](./img.png) and [lbl](../CONTRIBUTING.md#Install) and [ext](https://x.com)

A [[Wiki Link|label]], a ![[embed.png]], a [[Ghost]] and a #tag here.

```python
x = "[[not a link]] #nottag"
```
"""


def _require_node():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    if not os.path.isdir(os.path.join(VENDOR, "node_modules")):
        pytest.skip("scripts/vendor-codemirror/node_modules is absent "
                    "(gitignored; run the vendor install to enable this test)")


@pytest.fixture(scope="module")
def note_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("md") / "note.md"
    path.write_text(NOTE, encoding="utf-8")
    return str(path)


def decorate(note_file, caret=0):
    """The decoration set the template would render, with the caret at `caret`.

    Reaching a parsed result at all is itself an assertion: CodeMirror rejects a
    decoration set whose replacements overlap, so every caret position exercised
    here also proves the tree walk and the two regex passes do not collide.
    """
    _require_node()
    proc = subprocess.run(
        ["node", PROBE, TEMPLATE, note_file, str(caret)],
        cwd=VENDOR, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["decorations"]


def at(decorations, text, kind=None):
    """Every decoration whose covered text is exactly `text`."""
    return [d for d in decorations
            if d["text"] == text and (kind is None or d["kind"] == kind)]


# ------------------------------------------------------ the reveal rule (MD-18a)


def test_markup_is_hidden_away_from_the_caret(note_file):
    plain = decorate(note_file, caret=0)
    assert at(plain, "**", "hide"), "bold markers should be replaced away"
    assert at(plain, "# ", "hide"), "the heading marker should be replaced away"
    assert at(plain, "`", "hide")
    assert at(plain, "> ", "hide")
    # And the text itself still carries the styling those markers described.
    assert at(plain, "**bold**", "mark")[0]["cls"] == "lp-bold"
    assert at(plain, "# Heading one", "mark")[0]["cls"] == "lp-h1"


def test_the_caret_s_line_shows_its_source_again(note_file):
    caret = NOTE.index("**bold**") + 3
    revealed = decorate(note_file, caret=caret)
    # Same range, no longer replaced — shown dimmed instead.
    assert not at(revealed, "**", "hide")
    assert at(revealed, "**", "mark")[0]["cls"] == "lp-mark"
    # Still bold: revealing the markers must not un-style the text.
    assert at(revealed, "**bold**", "mark")[0]["cls"] == "lp-bold"
    # A line the caret is NOT on keeps its markup hidden.
    assert at(revealed, "# ", "hide")


def test_a_heading_reveals_only_its_own_line(note_file):
    revealed = decorate(note_file, caret=NOTE.index("# Heading one") + 3)
    # `#` shown, and the space after it is NOT swallowed while revealed — that
    # swallow exists only so hidden-marker text does not start indented.
    assert at(revealed, "#", "mark")
    assert not at(revealed, "# ", "hide")


# ------------------------------------------------------------ what renders


def test_wikilinks_embeds_and_ghosts_all_render(note_file):
    plain = decorate(note_file, caret=0)
    # The regression this guards: lezer wraps the inner brackets of `[[Wiki]]`
    # in a Link node and `![[embed]]` in an Image node, so a code-guard that
    # listed those node names silently rendered no wikilinks at all.
    assert at(plain, "[[Wiki Link|label]]", "widget")
    assert at(plain, "![[embed.png]]", "widget")
    assert at(plain, "[[Ghost]]", "widget")
    assert at(plain, "#tag", "widget")


def test_links_images_tables_rules_and_tasks_all_render(note_file):
    plain = decorate(note_file, caret=0)
    assert at(plain, "[lbl](../CONTRIBUTING.md#Install)", "widget")
    assert at(plain, "[ext](https://x.com)", "widget")
    assert at(plain, "![alt](./img.png)", "widget")
    assert at(plain, "---", "widget"), "the horizontal rule"
    assert at(plain, "| a | b |\n|---|--:|\n| 1 | 2 |", "widget")
    assert at(plain, "[ ]", "widget") and at(plain, "[x]", "widget")


def test_a_replacement_spans_a_line_break_which_is_why_it_is_a_state_field(note_file):
    """The behavioural half of why the decorations cannot be a ViewPlugin.

    CM refuses a plugin-provided replacement that crosses a newline, and this one
    does — a table is replaced whole. As a plugin that threw during the view
    update as soon as a table scrolled in, leaving blank regions on screen. The
    source half of this pair is in tests/test_markdown_template.py.
    """
    plain = decorate(note_file, caret=0)
    multiline = [d for d in plain
                 if d["kind"] in ("widget", "hide") and "\n" in d["text"]]
    assert multiline, "expected at least one replacement crossing a newline"
    assert [d["text"] for d in multiline] == ["| a | b |\n|---|--:|\n| 1 | 2 |"]


def test_a_checkbox_stays_rendered_under_the_caret(note_file):
    # Obsidian keeps the checkbox a control even with the caret on its line: it
    # is not markup you edit by hand. Everything else on the line reveals.
    revealed = decorate(note_file, caret=NOTE.index("- [ ] todo item") + 8)
    assert at(revealed, "[ ]", "widget")


def test_a_table_and_a_rule_yield_to_the_caret(note_file):
    inside = decorate(note_file, caret=NOTE.index("| 1 | 2 |") + 2)
    assert not at(inside, "| a | b |\n|---|--:|\n| 1 | 2 |", "widget")
    # A different line's rule is unaffected — reveal is per-line, not per-doc.
    assert at(inside, "---", "widget")


# --------------------------------------------------- what must NOT render


def test_a_fenced_block_is_never_a_link_or_a_tag(note_file):
    plain = decorate(note_file, caret=0)
    # MD-3's code-masking rule, holding on this side too: graph.py would not
    # call these edges, so the page must not draw them.
    assert not at(plain, "[[not a link]]")
    assert not at(plain, "#nottag")
    # The fence itself is styled as code, and keeps its own markers visible.
    assert [d for d in plain if d["cls"] == "lp-fence-line"]
    assert not at(plain, "```", "hide")


def test_frontmatter_is_dimmed_and_never_a_rule_or_a_heading(note_file):
    plain = decorate(note_file, caret=0)
    close = NOTE.index("---", 4)
    # Nothing the grammar said about that range was acted on: no HorizontalRule
    # widget for the opening `---`, and no heading mark for `title: …`.
    inside = [d for d in plain if d["from"] < close and d["kind"] != "line"]
    assert inside == [], inside
    fm = [d for d in plain if d["cls"] == "lp-fm-line"]
    assert len(fm) == 4, fm  # ---, title, tags, ---


def test_a_bare_url_fragment_is_not_a_tag(tmp_path):
    path = tmp_path / "u.md"
    # The caret must sit on a different line from the content, or the reveal
    # rule correctly un-renders everything and the test proves nothing.
    path.write_text("top\n\nSee [docs](https://x.com/a#section) and #real\n",
                    encoding="utf-8")
    plain = decorate(str(path), caret=0)
    assert at(plain, "#real", "widget"), "the sanity half: a real tag renders"
    assert not at(plain, "#section")

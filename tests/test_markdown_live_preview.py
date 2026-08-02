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

![](assets/pasted-20260802-143022.mp4)

A [[Wiki Link|label]], a ![[embed.png]] and a [[Ghost]] here.

Back up to [[#Heading one]] in this same note.

Bare https://example.com/a here.

Angle <https://example.com/b> here.

An inline `https://example.com/d` stays code.

```python
x = "[[not a link]]"
y = "https://example.com/e"
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


def decorate(note_file, caret=0, scanned=False, params=None):
    """The decoration set the template would render, with the caret at `caret`.

    Reaching a parsed result at all is itself an assertion: CodeMirror rejects a
    decoration set whose replacements overlap, so every caret position exercised
    here also proves the tree walk and the two regex passes do not collide.

    `scanned` decides whether graph.py answered with a real scan. The default is
    UNSCANNED, because that is what a mount-backed root, a refused scan and a
    failed one all produce — and it is the state most of this file runs in.
    `params` drives fused.params, so a param-held mode can be exercised. With no
    params the template opens READ-ONLY (MD-1a), which is why every test about
    the caret reveal passes `EDITING` — the reveal is an editing behaviour.
    """
    _require_node()
    opts = {"scanned": bool(scanned), "params": params or {}}
    proc = subprocess.run(
        ["node", PROBE, TEMPLATE, note_file, str(caret), json.dumps(opts)],
        cwd=VENDOR, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["decorations"]


EDITING = {"edit": "1"}   # the non-default mode a caret reveal needs (MD-1a)


def at(decorations, text, kind=None):
    """Every decoration whose covered text is exactly `text`."""
    return [d for d in decorations
            if d["text"] == text and (kind is None or d["kind"] == kind)]


def dom(decorations, text):
    """The DOM one widget renders — what a link IS, not merely where it is."""
    found = at(decorations, text, "widget")
    assert len(found) == 1, found
    return found[0]["dom"]


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
    revealed = decorate(note_file, caret=caret, params=EDITING)
    # Same range, no longer replaced — shown dimmed instead.
    assert not at(revealed, "**", "hide")
    assert at(revealed, "**", "mark")[0]["cls"] == "lp-mark"
    # Still bold: revealing the markers must not un-style the text.
    assert at(revealed, "**bold**", "mark")[0]["cls"] == "lp-bold"
    # A line the caret is NOT on keeps its markup hidden.
    assert at(revealed, "# ", "hide")


def test_a_heading_reveals_only_its_own_line(note_file):
    revealed = decorate(note_file, caret=NOTE.index("# Heading one") + 3,
                        params=EDITING)
    # `#` shown, and the space after it is NOT swallowed while revealed — that
    # swallow exists only so hidden-marker text does not start indented.
    assert at(revealed, "#", "mark")
    assert not at(revealed, "# ", "hide")


def test_read_only_mode_reveals_nothing_wherever_the_caret_is(note_file):
    """In read-only mode the document reads as fully rendered (MD-1a).

    `editable.of(false)` leaves no caret, so nothing would reveal anyway — but a
    browser text selection inside a non-editable CM view still lands in the
    state's selection, and that would un-render whatever the user swiped over.
    One guard in `selectedLines` makes the mode deterministic instead of
    dependent on how the browser reports a selection.
    """
    caret = NOTE.index("**bold**") + 3
    # The control: in editing mode this exact caret reveals the markers.
    assert at(decorate(note_file, caret=caret, params=EDITING), "**", "mark")
    # No `edit` param at all — read-only is the DEFAULT a note opens in, so this
    # pins the default and the mode in one call. `"0"` is the same state.
    reading = decorate(note_file, caret=caret)
    assert reading == decorate(note_file, caret=caret, params={"edit": "0"})
    assert at(reading, "**", "hide"), "read-only mode must not reveal source"
    assert not at(reading, "**", "mark")
    # And the rest of the document renders exactly as it does in editing mode:
    # a mode changes writability, never appearance.
    assert at(reading, "**bold**", "mark")[0]["cls"] == "lp-bold"
    assert at(reading, "# Heading one", "mark")[0]["cls"] == "lp-h1"


# ------------------------------------------------------------ what renders


def test_wikilinks_embeds_and_ghosts_all_render(note_file):
    plain = decorate(note_file, caret=0)
    # The regression this guards: lezer wraps the inner brackets of `[[Wiki]]`
    # in a Link node and `![[embed]]` in an Image node, so a code-guard that
    # listed those node names silently rendered no wikilinks at all.
    assert at(plain, "[[Wiki Link|label]]", "widget")
    assert at(plain, "![[embed.png]]", "widget")
    assert at(plain, "[[Ghost]]", "widget")


def test_links_images_tables_rules_and_tasks_all_render(note_file):
    plain = decorate(note_file, caret=0)
    assert at(plain, "[lbl](../CONTRIBUTING.md#Install)", "widget")
    assert at(plain, "[ext](https://x.com)", "widget")
    assert at(plain, "![alt](./img.png)", "widget")
    assert at(plain, "![](assets/pasted-20260802-143022.mp4)", "widget")
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
    inside = decorate(note_file, caret=NOTE.index("| 1 | 2 |") + 2,
                      params=EDITING)
    assert not at(inside, "| a | b |\n|---|--:|\n| 1 | 2 |", "widget")
    # A different line's rule is unaffected — reveal is per-line, not per-doc.
    assert at(inside, "---", "widget")


# ------------------------------- unknown is not missing (MD-11) --------------
#
# Every test above runs UNSCANNED, which is what a mount-backed root gives: no
# walk happened, so no target's resolution is known. That must not be rendered
# as "this note does not exist" — the page would be asserting something it never
# checked, and offering to create a note that may well be sitting right there.


def test_an_unscanned_note_renders_its_wikilinks_inert(note_file):
    unknown = decorate(note_file, caret=0)
    # It still renders as a wikilink rather than raw `[[…]]` source…
    link = dom(unknown, "[[Wiki Link|label]]")
    assert link["text"] == "label"
    # …but it claims nothing about the target, and cannot create it.
    assert "wl-ghost" not in link["cls"]
    assert "create" not in link["data"]
    assert "path" not in link["data"]
    assert "not resolved" in link["title"].lower()
    # The one that WOULD be a ghost after a scan is treated identically here:
    # unknown is unknown, whatever the name.
    assert dom(unknown, "[[Ghost]]")["cls"] == dom(unknown, "[[Wiki Link|label]]")["cls"]


def test_an_unscanned_embed_does_not_claim_the_target_is_missing(note_file):
    embed = dom(decorate(note_file, caret=0), "![[embed.png]]")
    assert "Missing" not in embed["text"]
    assert "create" not in embed["data"]
    assert "not resolved" in embed["title"].lower()


def test_a_scanned_note_resolves_links_and_ghosts_the_rest(note_file):
    scanned = decorate(note_file, caret=0, scanned=True)
    link = dom(scanned, "[[Wiki Link|label]]")
    assert link["data"]["path"] == "/vault/Wiki Link.md"
    assert link["cls"] == "wl"
    # A ghost is the CORRECT rendering once a scan has actually looked: dashed,
    # and clicking it creates the note (MD-4).
    ghost = dom(scanned, "[[Ghost]]")
    assert "wl-ghost" in ghost["cls"]
    assert ghost["data"]["create"] == "Ghost"
    assert "click to create" in ghost["title"].lower()
    # And a resolved embed is the image itself.
    assert dom(scanned, "![[embed.png]]")["tag"] == "img"


def test_a_same_note_anchor_is_a_link_not_an_offer_to_create_a_file(note_file):
    """`[[#Heading]]` points inside this very note, so it is a LINK.

    It rendered as a dead ghost: `splitInner` gives it an empty target,
    graph.py's `_resolved_links` deliberately skips empty targets (it is not an
    edge), so nothing resolved it — and with a scan in hand the widget took its
    "scanned, no path" branch and drew the dashed create-me affordance, with
    `data-create=""`. An empty attribute value still matches
    `closest("[data-create]")`, so clicking it ran createGhost("") and hit the
    degenerate-name guard: an offer to create a file that could never be made.
    """
    for scanned in (False, True):
        anchor = dom(decorate(note_file, caret=0, scanned=scanned),
                     "[[#Heading one]]")
        where = "scanned" if scanned else "unscanned"
        assert "wl-ghost" not in anchor["cls"], where
        assert "create" not in anchor["data"], where
        # It scrolls to the heading in this document rather than navigating: the
        # target note is the one already open.
        assert anchor["data"]["anchor"] == "Heading one", where
        assert "path" not in anchor["data"], where
        # And it reads as the heading, not as an empty name with a separator.
        assert anchor["text"] == "#Heading one", where


def test_the_two_states_are_told_apart_by_the_widget_key(note_file):
    # Widgets are reused across rebuilds by key, so the key has to change when a
    # scan lands or a stale inert link would survive the refresh (MD-9).
    unknown = at(decorate(note_file, caret=0), "[[Ghost]]", "widget")[0]
    scanned = at(decorate(note_file, caret=0, scanned=True), "[[Ghost]]", "widget")[0]
    assert unknown["cls"] != scanned["cls"]


# ------------------------------------------- bare and angle autolinks (MD-24)
#
# The grammar has parsed these all along — a bare `https://…` is a `URL` node
# and `<https://…>` an `Autolink` — and the builder simply ignored both node
# names, so a URL someone typed or pasted rendered as unclickable grey prose
# next to an explicit `[lbl](url)` that rendered as a link. The fix is
# display-only (D200): no document text changes, so it also repairs the URLs in
# notes people already wrote.


def test_a_bare_url_renders_as_a_link_without_rewriting_it(note_file):
    plain = decorate(note_file, caret=0)
    link = at(plain, "https://example.com/a", "mark")
    assert link, "a bare URL should be decorated"
    assert link[0]["cls"] == "lp-link", link
    # A MARK, not a widget: the text under it is untouched, so the document
    # still says exactly what the user typed and the caret can still sit in it.
    assert link[0]["tag"] == "a"
    assert link[0]["attrs"]["href"] == "https://example.com/a"
    assert link[0]["attrs"]["target"] == "_blank"


def test_an_angle_autolink_hides_its_brackets_and_gives_them_back(note_file):
    plain = decorate(note_file, caret=0)
    # Unlike a bare URL, `<…>` HAS markup worth hiding, so the reveal rule
    # applies to it exactly as it does to `**` or `# `.
    assert at(plain, "<", "hide"), "the opening angle bracket should be hidden"
    assert at(plain, ">", "hide")
    assert at(plain, "https://example.com/b", "mark")[0]["cls"] == "lp-link"

    caret = NOTE.index("<https://example.com/b>") + 4
    revealed = decorate(note_file, caret=caret, params=EDITING)
    assert not at(revealed, "<", "hide")
    assert at(revealed, "<", "mark")[0]["cls"] == "lp-mark"
    # Still a link while revealed: showing the source must not un-style it.
    assert at(revealed, "https://example.com/b", "mark")[0]["cls"] == "lp-link"


def test_a_schemeless_autolink_gets_an_href_that_leaves_this_page(tmp_path):
    """GFM autolinks three shapes, and two of them are not URLs yet.

    `www.x.com` and `me@x.com` are `URL` nodes just like `https://…` is, so
    using the matched text as the href verbatim would produce a relative link
    that resolves against /render?path=… — the same trap MD-4a records.
    """
    path = tmp_path / "u.md"
    # The caret sits on line 1, away from the content: see the note in
    # test_a_hashtag_is_left_as_prose.
    path.write_text("top\n\nSee www.example.com or me@example.com.\n",
                    encoding="utf-8")
    plain = decorate(str(path), caret=0)
    assert at(plain, "www.example.com", "mark")[0]["attrs"]["href"] \
        == "https://www.example.com"
    assert at(plain, "me@example.com", "mark")[0]["attrs"]["href"] \
        == "mailto:me@example.com"


def test_an_explicit_link_is_still_one_widget_and_not_also_a_url_mark(note_file):
    """The URL inside `[lbl](url)` is markup the Link widget already replaced.

    Decorating it a second time as a bare URL would be both wrong (it is not
    shown) and a collision risk, so the URL pass has to leave a Link's own
    children alone.
    """
    plain = decorate(note_file, caret=0)
    assert at(plain, "[ext](https://x.com)", "widget")
    assert not at(plain, "https://x.com")
    assert not at(plain, "../CONTRIBUTING.md#Install")


# --------------------------------------------------- what must NOT render


def test_a_fenced_block_is_never_a_link_or_a_tag(note_file):
    plain = decorate(note_file, caret=0)
    # MD-3's code-masking rule, holding on this side too: graph.py would not
    # call these edges, so the page must not draw them.
    assert not at(plain, "[[not a link]]")
    # And a URL in that fence is a string literal, not a link (MD-24).
    assert not at(plain, "https://example.com/e")
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


def test_a_url_in_inline_code_is_code_not_a_link(note_file):
    """Same line as MD-3's code masking, on the inline side.

    Read the module docstring before touching the guard that makes this pass:
    an over-broad "is this code?" list once silently stopped every wikilink
    from rendering, and that regression is invisible in a diff.
    """
    plain = decorate(note_file, caret=0)
    assert not at(plain, "https://example.com/d")
    # The span is still rendered as code, so this is the absence of a link
    # rather than the absence of any decoration at all.
    assert at(plain, "`https://example.com/d`", "mark")[0]["cls"] == "lp-code"


def test_a_hashtag_is_left_as_prose(tmp_path):
    """The tag concept is gone (D165): a `#word` mid-line is text, not a chip.

    The link decoration next to it still renders, so this is the absence of the
    tag pass rather than the absence of the whole decoration set.
    """
    path = tmp_path / "u.md"
    # The caret must sit on a different line from the content, or the reveal
    # rule correctly un-renders everything and the test proves nothing.
    path.write_text("top\n\nSee [docs](https://x.com/a#section) and #real\n",
                    encoding="utf-8")
    plain = decorate(str(path), caret=0)
    assert at(plain, "[docs](https://x.com/a#section)", "widget")
    assert not at(plain, "#real")


def test_a_pasted_video_renders_as_a_player_not_a_broken_image(note_file):
    """Markdown has no video syntax, so a dropped clip is written as `![](…)`
    (MD-23) — the same markup Obsidian writes. The widget therefore has to
    choose its element off the extension, or every pasted video would render as
    an <img> with a source no browser can decode: a broken-image icon.
    """
    plain = decorate(note_file, caret=0)
    video = dom(plain, "![](assets/pasted-20260802-143022.mp4)")
    assert video["tag"] == "video"
    # Resolved against the note's folder, exactly as the image branch does.
    assert "assets/pasted-20260802-143022.mp4" in video["src"]
    # Its own class beside lp-img, so the stylesheet can size a player.
    assert video["cls"] == "lp-video"


def test_an_image_is_still_an_image(note_file):
    plain = decorate(note_file, caret=0)
    assert dom(plain, "![alt](./img.png)")["tag"] == "img"

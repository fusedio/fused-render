"""Source-contract tests for the markdown template (SPEC §32).

These pin the invariants that are easy to regress silently and impossible to
see in a diff review of a long template, the way the `runtime.js` wiring
assertions do (D137):

* one surface (MD-1): the template is a Live Preview editor and nothing else —
  no reading/source mode switcher, no toolbar, no ⌘E, and no second render
  pipeline over the same document;
* the Obsidian save model (MD-16): no save button, no dirty indicator, an idle
  timer plus blur/tab-switch, and ⌘S as a flush rather than *the* save;
* the one deviation (MD-17): a dirty buffer whose mtime moved gets a
  reload-or-keep banner instead of last-write-wins;
* read-only comes off `stat.writable` — the shell's persisted `read_only` flag —
  never `os.access` (MD-15);
* the link layer asks the vendored grammar what a range is, so the code-masking
  rule in `graph.py` has no second parser in JS (MD-3).

Behavioural coverage lives next door and does not stop at the source:
tests/test_markdown_live_preview.py runs the real decoration builder against
the real grammar, and tests/test_markdown_graph.py covers what a link IS and
where it points, against the Python that decides both.
"""
import os

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "markdown",
    "template.html")


@pytest.fixture(scope="module")
def source():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


# ------------------------------------------------------- one surface (MD-1)


def test_there_is_no_reading_or_source_mode_switcher(source):
    # The template is a single Live Preview surface. A mode switcher would mean
    # two renderings of one document and two places for the caret to be.
    assert "data-view" not in source
    assert "applyMode" not in source
    assert 'fused.params.set("view"' not in source


def test_there_is_no_toolbar(source):
    # The shell's breadcrumb already names the file and Obsidian shows no save
    # state, so the bar had nothing left to hold (MD-2a). The reload-or-keep
    # banner is not a toolbar: it appears only while a conflict is unresolved.
    assert 'id="bar"' not in source
    assert 'id="filename"' not in source
    assert 'id="conflict"' in source


def test_there_is_no_second_render_pipeline(source):
    # marked and the HTML rewrites it needed are gone: a Live Preview decorates
    # the source document, so there is no rendered copy to post-process.
    assert "marked" not in source
    assert "rewriteRelativeImages" not in source
    assert "rewriteRelativeLinks" not in source
    assert "innerHTML = marked" not in source


def test_the_editor_opts_out_of_the_runtime_reload(source):
    # Our own autosave moves the mtime on every write, and a page reload would
    # drop the caret — this view owns the reload rule (MD-17) instead.
    assert "fused.autoReload(false)" in source


def test_saving_is_an_idle_timer_plus_blur_and_tab_switch(source):
    assert "const AUTOSAVE_MS = 2000" in source
    assert 'window.addEventListener("blur"' in source
    assert 'window.addEventListener("pagehide"' in source
    assert 'document.addEventListener("visibilitychange"' in source


def test_there_is_no_save_button_and_no_dirty_indicator(source):
    # The absence of save ceremony is the behaviour being copied, so a Save
    # control creeping back in is a regression, not an addition.
    assert 'id="save"' not in source
    assert ">Save<" not in source


def test_cmd_s_forces_the_flush_rather_than_being_the_save(source):
    assert 'event.key.toLowerCase() !== "s"' in source
    assert "void save();" in source
    # And there is no ⌘E: with one surface there is nothing to toggle to.
    assert 'key === "e"' not in source


def test_writes_are_locked_to_the_last_known_mtime(source):
    assert "expectedMtime: mtime" in source
    # And the conflict that lock produces raises the banner, never an overwrite.
    assert 'err.type === "conflict"' in source
    assert "showConflict(" in source


def test_a_clean_buffer_reloads_silently_and_a_dirty_one_gets_the_banner(source):
    body = source[source.index("async function checkExternalChange"):]
    body = body[:body.index("\n    }")]
    assert "if (!dirty)" in body
    assert "reloadFromDisk()" in body
    assert "showConflict(" in body


def test_the_banner_offers_reload_or_keep_and_keep_drops_the_lock(source):
    assert 'id="reload"' in source and 'id="keep"' in source
    keep = source[source.index("async function keepMine"):]
    keep = keep[:keep.index("\n    }")]
    # No expectedMtime: the user has been shown the conflict and chosen.
    assert "expectedMtime" not in keep


def test_read_only_comes_from_stat_writable_not_os_access(source):
    assert "st.writable !== false" in source
    assert "CM.EditorState.readOnly.of(true)" in source
    assert "CM.EditorView.editable.of(false)" in source
    # A `readonly` rejection from the server locks the surface too, rather than
    # leaving the user typing into a buffer that can never land.
    assert 'err.type === "readonly"' in source
    assert "lockEditor(" in source


def test_the_link_layer_asks_the_grammar_what_a_range_is(source):
    # Wikilinks and tags are not in the markdown grammar, so they are matched by
    # regex — but whether a match COUNTS is answered by the tree, not by a
    # second block parser. That is what keeps graph.py's code-masking rule
    # (MD-3) from having a rival implementation in JS.
    assert "CM.syntaxTree(state)" in source
    assert "within(tree, start + 2, CODE_NODES)" in source
    assert "within(tree, start + 1, URL_NODES)" in source
    # And the guard must not list Link/Image: the grammar wraps `[[Wiki]]`'s
    # inner brackets in a Link node, so doing so hides every wikilink. The
    # behavioural half of this is in test_markdown_live_preview.py.
    code_nodes = source[source.index("const CODE_NODES"):]
    code_nodes = code_nodes[:code_nodes.index("]);")]
    assert '"Link"' not in code_nodes and '"Image"' not in code_nodes


def test_the_decorations_come_from_a_state_field_not_a_view_plugin(source):
    """A ViewPlugin may not provide a replacement that spans a line break.

    CM throws "Decorations that replace line breaks may not be specified via
    plugins", and the table widget replaces a multi-line `Table` node with one
    element. As a plugin it therefore threw during the view update the moment a
    table scrolled into the viewport, abandoning the rest of the render and
    leaving unpainted white regions — reported as "parts of the page go blank
    while scrolling". A StateField is allowed to span line breaks.

    Pinned in the source because the failure only appears in a real EditorView:
    the probe calls the builder directly, so it cannot see this. Its half of the
    pair is test_markdown_live_preview.py's assertion that the table widget's
    range really does cross a newline.
    """
    assert "CM.StateField.define" in source
    assert "CM.EditorView.decorations.from(" in source
    # No plugin anywhere may hand decorations to the view. (The word itself is
    # allowed in prose — the template says at length why it must not be one.)
    assert "CM.ViewPlugin" not in source
    assert "decorations: (plugin)" not in source
    # And the builder takes a state, because a state field has no viewport.
    assert "function buildDecorations(state)" in source
    assert "visibleRanges" not in source


def test_resolution_is_never_recomputed_in_the_page(source):
    # The page maps a raw target to whatever graph.py resolved it to; it must
    # not contain a second resolution rule (MD-6).
    assert 'action: "note"' in source
    assert "resolved.set(link.target, link)" in source


def test_an_unscanned_note_says_so_instead_of_showing_an_empty_panel(source):
    # A hidden backlinks section reads as "no backlinks", which is an answer that
    # was never computed — the sidebar says why instead, in graph.py's own words
    # (MD-11). The existing #notice styling carries it.
    body = source[source.index("function renderSidebar"):]
    body = body[:body.index("\n    async function refreshLinks")]
    assert "linksEl.hidden = true" not in body
    assert '"Backlinks need a folder scan. " + scanNotice' in body
    assert "scanNotice = data.message" in source


def test_the_page_never_invents_a_resolution_when_no_scan_ran(source):
    # MD-6: resolution is graph.py's. The unknown state must not be papered over
    # with a fallback rule here — the only allowed answers are graph.py's map and
    # "we do not know".
    widget = source[source.index("function wikilinkWidget"):]
    widget = widget[:widget.index("\n    // `[label](target)`")]
    assert "const known = resolved !== null;" in widget
    assert "a.className = \"wl wl-unknown\";" in widget
    # No create offer and no path guess in the unknown branch.
    unknown = widget[widget.index("if (!known) {"):widget.index("} else if (!path)")]
    assert "dataset" not in unknown
    assert "resolvePath" not in unknown


def test_editing_and_saving_do_not_depend_on_the_scan(source):
    # The "view works on a mount" half (MD-11): one bounded read and one bounded
    # write, neither of which consults the link layer.
    save = source[source.index("async function doSave"):]
    save = save[:save.index("\n    function showConflict")]
    assert "fused.writeFile(file, body, { expectedMtime: mtime })" in save
    assert "notes" not in save.split("await refreshLinks();")[0]
    # And a relative markdown link resolves in the page against the note's own
    # folder, which needs no scan at all (MD-4a).
    link = source[source.index("function markdownLinkWidget"):]
    link = link[:link.index("\n    // `![alt](src)`")]
    assert "resolvePath(noteDir()" in link
    assert "resolved" not in link


def test_the_editor_follows_the_shell_appearance_without_a_rebuild(source):
    assert "CM.StateEffect.reconfigure.of" in source
    assert 'attributeFilter: ["data-theme"]' in source


def test_leaving_the_note_flushes_before_navigating(source):
    nav = source[source.index("async function navigateShell"):]
    nav = nav[:nav.index("\n    }")]
    assert "__fusedFlushEdits()" in nav
    assert "if (!flushed.ok) return;" in nav


# ------------------------------------------------- editing behaviours (MD-18)


def test_smart_lists_come_from_the_markdown_keymap_not_a_reimplementation(source):
    # Enter-continues-the-marker, renumbering and blockquote continuation are
    # `markdownKeymap` — the same code Obsidian's editor runs. A hand-rolled
    # copy here would be a second, worse implementation of a solved problem.
    assert "CM.markdownKeymap.concat(editorKeymap)" in source
    assert "CM.Prec.high(CM.keymap.of(" in source


def test_the_formatting_keys_are_toggles(source):
    for key in ["Mod-b", "Mod-i", "Mod-k", "Mod-Enter", "Tab"]:
        assert f'key: "{key}"' in source, key
    # toggleWrap unwraps when the markers are already there.
    body = source[source.index("function toggleWrap"):]
    body = body[:body.index("\n    }")]
    assert "pre === marker && post === marker" in body


def test_pasting_a_url_over_a_selection_makes_a_link(source):
    body = source[source.index("const pasteHandler"):]
    body = body[:body.index("\n    });")]
    assert "if (from === to) return false;" in body
    assert "`[${selected}](${url})`" in body


def test_the_popup_offers_notes_headings_and_tags_from_the_same_scan(source):
    body = source[source.index("async function markdownCompletions"):]
    body = body[:body.index("\n    function editorExtensions")]
    assert "/\\[\\[([^\\[\\]\\n]*)$/" in body      # `[[` and `![[`
    assert "headingOptions(headings, notePart)" in body
    assert "data.tags.map" in body
    assert 'action: "candidates"' in source


def test_the_popup_inserts_the_form_graph_py_says_resolves(source):
    # `note.link` is _link_form's output, which is verified against resolve_link
    # itself in tests/test_markdown_graph.py — the page must not compute its own.
    assert "label: note.link" in source


def test_a_checkbox_writes_back_and_is_locked_when_read_only(source):
    body = source[source.index("function taskWidget"):]
    body = body[:body.index("\n    }")]
    assert "box.disabled = !writable;" in body
    # The position comes from the DOM at click time. The previous rendering
    # counted markers in the source and matched the nth checkbox to the nth
    # marker, which an edit between render and click could get wrong.
    assert "editorView.posAtDOM(box)" in body
    assert "void save();" in body


# ------------------------------------------------------- graph panel (MD-19)

SHARED_CANVAS = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "shared",
    "graph-canvas.js")


@pytest.fixture(scope="module")
def canvas_source():
    with open(SHARED_CANVAS, encoding="utf-8") as handle:
        return handle.read()


def test_the_graph_panel_state_lives_in_params_so_it_is_shareable(source):
    assert 'fused.params.set("graph"' in source
    assert 'fused.params.set("depth"' in source
    assert 'fused.params.get("graph") === "1"' in source


def test_backlinks_and_the_graph_are_one_sidebar_behind_one_toggle(source):
    # Obsidian's right sidebar, and the only chrome this view has: a 26px
    # toggle pinned to the right edge, plus the panel it opens.
    assert 'id="side"' in source
    assert 'id="toggle-graph"' in source
    assert 'id="links"' in source and 'id="graph-canvas"' in source
    body = source[source.index("function applySidebar"):]
    body = body[:body.index("\n    }")]
    assert 'sideEl.classList.toggle("on", on)' in body
    assert 'toggleEl.setAttribute("aria-pressed", String(on))' in body


def test_the_panel_asks_for_a_bounded_neighbourhood(source):
    body = source[source.index("async function loadGraph"):]
    body = body[:body.index("\n    toggleEl.addEventListener")]
    assert 'action: "graph"' in body
    assert "depth: String(graphDepth())" in body
    # A refused root is reported, not drawn as an empty graph.
    assert "graphNoteEl.textContent = data.message" in body


def test_both_graph_surfaces_share_one_canvas_implementation(source, canvas_source):
    # Extracted when the second surface appeared, so the sim and the
    # interaction rules cannot drift into two versions.
    assert '/template-shared/graph-canvas.js' in source
    assert "fusedGraph.create({" in source
    assert "window.fusedGraph = { create: create };" in canvas_source


def test_graph_colours_are_read_at_draw_time_not_baked(canvas_source):
    # var() cannot resolve inside a canvas fillStyle, so a theme flip has to
    # redraw with freshly-read tokens (SPEC §30).
    assert "function token(name)" in canvas_source
    assert "getPropertyValue(name)" in canvas_source
    assert 'attributeFilter: ["data-theme"] });' in canvas_source


def test_the_graph_behaviours_obsidian_has_are_present(canvas_source):
    assert "function radius(node)" in canvas_source          # radius from degree
    assert "var labels = zoom > 0.75" in canvas_source       # labels fade
    assert "drag.pinned = true" in canvas_source             # drag-to-pin
    assert "neighbours(hover)" in canvas_source              # hover highlighting
    assert "[3, 3]" in canvas_source                         # dashed ghost edges


def test_a_saved_note_refreshes_the_open_graph(source):
    assert "if (graphOn()) void loadGraph();" in source
    assert "candidates = null;" in source


# ------------------------------------------ relative markdown links (MD-4a)


@pytest.fixture(scope="module")
def link_widget(source):
    """The body of markdownLinkWidget, so the assertions below are local."""
    body = source[source.index("function markdownLinkWidget"):]
    return body[:body.index("\n    }")]


def test_a_relative_link_navigates_through_the_wikilink_handler(link_widget):
    # `[CONTRIBUTING](../CONTRIBUTING.md)` is authored against the note's own
    # folder, but this document is served at /render?path=…, so the browser
    # would resolve it against the server root and miss the file entirely
    # (MD-4a). data-path is what the one delegated click handler already
    # listens for, so a relative link and a wikilink reach the shell by the
    # same code path — including the flush that protects unsaved edits.
    assert "a.dataset.path = path;" in link_widget
    # A real href too, so hover preview and ⌘-click behave like normal links.
    assert "urlForFsPath(" in link_widget
    assert "resolvePath(noteDir()," in link_widget


def test_external_and_in_page_links_are_left_alone(link_widget):
    # Absolute paths, any scheme (http:, mailto:, data:) and a bare #anchor are
    # not vault paths; rewriting them would break them. MD-3 draws the same
    # line for what counts as an edge.
    assert r"^(#|\/|[a-z][a-z0-9+.-]*:)" in link_widget
    assert 'a.target = "_blank"' in link_widget


def test_a_percent_escaped_link_resolves_to_the_real_path(source, link_widget):
    # An authored `[x](./My%20Note.md)` has to be decoded before it is joined
    # onto the note's folder, or the path won't exist.
    assert "decodeMaybe(" in link_widget
    # And a malformed escape must not throw the whole decoration pass away.
    assert "function decodeMaybe(value)" in source
    assert "catch (e) { return value; }" in source


def test_a_relative_link_can_carry_a_heading(link_widget):
    # `](./other.md#Install)` hands the heading over as a param, exactly as a
    # `[[Note#Heading]]` wikilink does (MD-4).
    assert "a.dataset.heading = heading;" in link_widget


def test_an_arriving_heading_scrolls_without_moving_the_caret(source):
    # There are no rendered headings to scan any more, so the document is the
    # index. The caret must NOT be moved onto the heading: that would reveal
    # its markup the instant you arrived.
    body = source[source.index("function scrollToHeading"):]
    body = body[:body.index("\n    }")]
    assert "CM.EditorView.scrollIntoView(" in body
    assert "selection" not in body

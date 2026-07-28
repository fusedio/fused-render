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


def test_the_mode_is_the_same_two_facets_the_unwritable_path_uses(source):
    # A read-only *mode* and a read-only *file* are one mechanism (MD-1a): the
    # same two CM facets over the same decorations. No second render pipeline,
    # no different typography — the mode changes writability, never appearance.
    body = source[source.index("function editorExtensions"):]
    body = body[:body.index("\n    function buildEditor")]
    assert "if (!editing())" in body
    assert "CM.EditorView.editable.of(false)" in body
    assert "CM.EditorState.readOnly.of(true)" in body
    # One definition of the mode, and the file's real writability is a factor in
    # it — a preference can never grant write access the file does not have.
    assert "function editing() { return writable && editWanted(); }" in source


def test_the_mode_lives_in_a_param_so_it_survives_a_refresh(source):
    # The same shape `graph` and `depth` use (MD-20), so the mode is
    # refresh-proof and the URL is shareable. Editing is the default: an absent
    # param must not silently make notes read-only.
    assert 'fused.params.get("edit") !== "0"' in source
    assert 'fused.params.set("edit", next)' in source


def test_the_mode_toggle_is_a_corner_button_not_a_toolbar(source):
    # MD-2a still holds: a second 26px button in the same cluster, not a row.
    assert 'id="toggle-edit"' in source
    assert 'id="bar"' not in source
    assert 'aria-pressed' in source[source.index('id="toggle-edit"'):][:400]


def test_the_accent_marks_read_only_not_the_default(source):
    # Editing is the default (MD-1a), so accenting it would leave the corner
    # permanently lit and say nothing. `aria-pressed` tracks read-only, and the
    # glyph follows: pencil while editing, padlock when locked.
    assert 'editToggleEl.setAttribute("aria-pressed", String(!on));' in source
    assert '#toggle-edit[aria-pressed="true"] .icon-edit { display: none; }' in source
    assert 'class="icon-lock"' in source


def test_an_unwritable_file_cannot_be_toggled_into_editing(source):
    # The mode is a preference layered on top of the file's real writability,
    # never a way around it (MD-15).
    body = source[source.index("function applyEditMode"):]
    body = body[:body.index("\n    function ")]
    assert "editToggleEl.disabled = !writable" in body
    assert "can't be written" in body


def test_a_mode_switch_keeps_the_caret_and_the_scroll_position(source):
    # A mode switch rebuilds the view (editable is chosen at construction), so
    # buildEditor carrying both across is what makes the rebuild invisible —
    # load-bearing for MD-1a rather than a nicety.
    body = source[source.index("function buildEditor"):]
    body = body[:body.index("\n      // A live appearance change")]
    assert "previous ? previous.state.selection.main.head : cursorMemory()" in body
    assert "previous ? previous.scrollDOM.scrollTop : 0" in body
    assert "view.dispatch({ selection: { anchor: at } });" in body
    assert "view.scrollDOM.scrollTop = scroll;" in body


def test_switching_to_read_only_flushes_pending_edits_first(source):
    # Same reason navigation flushes (MD-16): the idle timer may not have fired.
    body = source[source.index("editToggleEl.addEventListener"):]
    body = body[:body.index("\n    depthEl.addEventListener")]
    assert "await save();" in body


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


@pytest.fixture(scope="module")
def create_ghost(source):
    body = source[source.index("async function createGhost"):]
    return body[:body.index("\n    // One delegated handler")]


def test_creating_a_ghost_resolves_it_the_way_graph_py_does(create_ghost):
    """The page computes exactly one path of its own, and it must agree with
    `resolve_link`: the linking note's own folder first, the vault root second.

    Reported: clicking `../examples/Nope.md` from `docs/` joined the target onto
    the vault ROOT and tried to write one level above it. Same class of
    divergence MD-3 exists to prevent.
    """
    assert "const relative = /(^|\\/)\\.\\.(\\/|$)/.test(clean);" in create_ghost
    assert "const base = relative || !clean.includes(\"/\") ? noteDir() : notes.root;" \
        in create_ghost


def test_creating_a_ghost_never_writes_outside_the_scan_root(create_ghost):
    # A note above the root is invisible to the graph that offered to create it,
    # and `..` in a target is exactly how a write escapes upwards. The boundary
    # slash keeps a sibling folder with a shared prefix out.
    assert 'const root = notes.root.replace(/\\/+$/, "");' in create_ghost
    assert 'if (path !== root && !path.startsWith(root + "/"))' in create_ghost
    assert "outside" in create_ghost
    # The refusal happens BEFORE the write, not as a caught failure.
    assert create_ghost.index("!path.startsWith(root") < create_ghost.index("fused.writeFile")


def test_creating_a_ghost_refuses_a_degenerate_name(create_ghost):
    # A directory target used to derive a file called literally `.md`. graph.py no
    # longer makes such a ghost; this refuses to act on one anyway.
    assert 'if (!name.replace(/\\.(md|markdown)$/i, "").split("/").pop()) return;' \
        in create_ghost


def test_resolving_a_path_leaves_a_windows_drive_root_alone(source):
    """`/C:/Users/…` is not a path anything here can write.

    A leading "/" is right for POSIX and wrong for Windows, whose canonical form
    in this app is the drive path `C:/Users/…` — and the wrongness is not
    cosmetic: `createGhost` compares its result against `notes.root`, so a
    spurious leading slash failed the boundary check and refused EVERY create on
    Windows. Both surfaces that resolve a path carry the guard, and the docs,
    excel and slides templates carry the same idiom.
    """
    graph_template = os.path.join(
        os.path.dirname(TEMPLATE), "..", "graph", "template.html")
    with open(graph_template, encoding="utf-8") as handle:
        graph_source = handle.read()
    guard = 'return /^[A-Za-z]:$/.test(out[0] || "") ? joined : "/" + joined;'
    for text, where in ((source, "the note view"), (graph_source, "the graph mode")):
        assert "function resolvePath" in text, where
        assert guard in text, where
        # The unconditional prefix this replaced.
        assert 'return "/" + out.join("/");' not in text, where


def test_the_create_path_reads_the_ghost_target_not_its_label(source, canvas_source):
    # `label` is a display string (a real note's is its title). Driving a write
    # off it is fragile by construction.
    assert 'found.node.kind === "ghost" && found.node.target' in canvas_source
    assert "target: node.target" in canvas_source
    assert "onCreateGhost(found.node.label)" not in canvas_source


def test_the_editor_follows_the_shell_appearance_without_a_rebuild(source):
    assert "CM.StateEffect.reconfigure.of" in source
    assert 'attributeFilter: ["data-theme"]' in source
    # The swap is still oneDark-shaped, so the reconfigure has something real to
    # do: it is kept for fenced-code colours, with its surface overridden below.
    assert "e !== CM.oneDark" in source
    assert "next ? [CM.oneDark] : []" in source


def test_the_editing_surface_is_the_same_colour_as_the_page(source):
    """oneDark's own surface (#282c34, a blue cursor, a blue-grey selection) made
    the editor a second, lighter dark slab inside this one. Obsidian's editor is
    the same colour as the app around it.

    Every override is a token, never a literal — the tier-one theme test enforces
    that separately, and this pins that the overrides exist at all.
    """
    style = source[source.index("<style>"):source.index("</style>")]
    assert ".cm-editor { background: var(--bg); color: var(--fg); }" in style
    assert "border-left-color: var(--accent);" in style
    assert "background: var(--selection);" in style
    assert ".cm-editor .cm-tooltip {" in style
    # And oneDark must not colour PROSE like source code: headings red, markdown
    # markers green, URLs cyan. Fenced code keeps its highlighting.
    assert ".cm-line:not(.lp-fence-line)" in style
    assert "color: inherit;" in style


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
    # Locked by the MODE, which already includes the file's writability
    # (`editing()` is `writable && editWanted()`, MD-1a) — so a note being read
    # is not a note whose checkboxes can still be ticked.
    assert "box.disabled = !editing();" in body
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


def test_the_graph_count_compares_notes_with_notes(source):
    """`total_notes` counts notes; `nodes` also holds tag and ghost nodes.

    Comparing the two read "221 of 205 notes" once the `all` depth existed, and
    was quietly wrong at every depth before that. The count has to filter by
    kind, so a bare `data.nodes.length` in this string is the regression.
    """
    lines = source.splitlines()
    at = next(n for n, ln in enumerate(lines) if "of ${" in ln and "notes`" in ln)
    block = "\n".join(lines[max(0, at - 6):at + 1])
    assert 'kind === "note"' in block, block
    assert "${data.nodes.length}" not in block, block


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


def test_the_graph_gets_the_panel_s_leftover_space_not_the_backlinks(source):
    # The obvious "simplification" is to let the scrollable list flex and cap
    # the canvas, which leaves four backlinks holding 300px of nothing while the
    # graph's labels overlap. The split only works in this direction.
    links = source[source.index("  #links {"):]
    links = links[:links.index("\n")]
    assert "flex: none" in links and "max-height:" in links
    assert "overflow: auto" in links and "min-height: 0" in links
    graph = source[source.index("  #graph-sec {"):]
    graph = graph[:graph.index("\n  }")]
    assert "flex: 1" in graph and "min-height:" in graph
    assert "height: 45%" not in graph


def test_a_backlink_row_keeps_its_path_visible_without_overflowing(source):
    # Two notes can share a title; the rel path is the only disambiguator, so it
    # sits inline next to the title, clips rather than spilling, and is never
    # flung to the far edge where it reads as a second column.
    rules = source[source.index("  .bl-title,"):]
    rules = rules[:rules.index("\n  .bl-empty")]
    assert "text-overflow: ellipsis" in rules and "min-width: 0" in rules
    assert "text-align: right" not in rules
    assert '<span class="bl-path">${escapeHtml(row.rel)}</span>' in source


def test_backlink_rows_read_as_a_list_and_still_answer_the_keyboard(source):
    # Borderless rows, so the two affordances that replace the border have to
    # stay distinguishable: a fill on hover, an outline on keyboard focus. A
    # single shared rule here would leave keyboard users with no signal at all.
    row = source[source.index("  .bl {"):]
    row = row[:row.index("\n  .bl-title,")]
    assert "border: none" in row and "background: none" in row
    hover = row[row.index(".bl:hover"):]
    hover = hover[:hover.index("\n")]
    assert "background: var(--bg)" in hover and "outline" not in hover
    focus = row[row.index(".bl:focus-visible"):]
    focus = focus[:focus.index("\n")]
    assert "outline: 2px solid var(--accent)" in focus


def test_the_panel_asks_for_a_bounded_neighbourhood(source):
    body = source[source.index("async function loadGraph"):]
    body = body[:body.index("\n    toggleEl.addEventListener")]
    assert 'action: "graph"' in body
    assert "depth: String(graphDepth())" in body
    # A refused root is reported, not drawn as an empty graph.
    assert "graphNoteEl.textContent = data.message" in body


def test_the_depth_select_offers_the_whole_vault(source):
    # A BFS neighbourhood was the only thing the panel could show, so a 205-note
    # vault rendered as "13 of 205 notes" with no way to the rest. `-1` is the
    # sentinel graph.py reads as "skip the neighbourhood filter" (MD-20).
    select = source[source.index('<select id="depth">'):]
    select = select[:select.index("</select>")]
    assert '<option value="-1">all</option>' in select
    body = source[source.index("function graphDepth"):]
    body = body[:body.index("\n    }")]
    assert "n >= -1" in body


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
    assert "var labels = zoom > 0.7" in canvas_source        # labels fade
    assert "drag.pinned = true" in canvas_source             # drag-to-pin
    assert "neighbours(hover)" in canvas_source              # hover highlighting
    assert "[3, 3]" in canvas_source                         # dashed ghost edges


def test_the_layout_leaves_room_for_labels_and_then_frames_itself(canvas_source):
    """The two halves of "the graph is too dense to read", which only work
    together: spacing wide enough for an 11px label, and a fit-to-canvas so the
    wider layout does not simply fall off the edge of a narrow sidebar.

    One sim serves both surfaces (D157), and the spacing is deliberately NOT
    scaled per surface — uniformly scaling a layout that gets fitted to the
    canvas changes nothing on screen. The zoom is what varies.
    """
    assert "var REST = 135;" in canvas_source
    assert "var REPULSION = 4600;" in canvas_source
    assert "function frameAll()" in canvas_source
    # Only ever zooms out, and never past a floor.
    assert "zoom = Math.min(1.5, Math.max(0.15," in canvas_source
    # And it yields to the user the moment they touch the canvas.
    assert "if (userFramed || !nodes.length) return;" in canvas_source
    assert canvas_source.count("userFramed = true") == 2      # pan/drag, and wheel


def test_a_big_folder_graph_cannot_fly_apart(canvas_source):
    # The same sim serves a folder of hundreds. Velocity accumulates across steps
    # and the repulsion sum grows with node count, so without a ceiling the first
    # few frames threw nodes thousands of pixels out and the sim cooled before it
    # could recover. Seeding on a disc sized to the node count is the other half.
    assert "var MAX_SPEED = 22;" in canvas_source
    assert "if (speed > MAX_SPEED)" in canvas_source
    assert "Math.sqrt(count / Math.PI)" in canvas_source


def test_the_panel_width_is_a_preference_not_view_state(source):
    # localStorage, NOT fused.params: params are what a shared URL reproduces
    # (MD-20), and window furniture is not that.
    assert 'id="side-grip"' in source
    body = source[source.index("const gripEl"):]
    body = body[:body.index("function graphOn()")]
    assert 'localStorage.setItem(SIDE_KEY' in body
    assert 'fused.params.set("side' not in source
    assert "const SIDE_MIN = 15 * 16;" in body and "const SIDE_MAX = 45 * 16;" in body
    # The canvas is a bitmap and has to be told its box changed.
    assert "canvas.nudge();" in body
    # Keyboard-reachable, since the handle is a real button already.
    assert 'event.key === "ArrowLeft"' in body


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

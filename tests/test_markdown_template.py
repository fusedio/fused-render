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
import re

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "markdown",
    "template.html")


@pytest.fixture(scope="module")
def source():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def graph_source():
    """The folder-level graph mode, which shares this template's create path."""
    path = os.path.join(os.path.dirname(TEMPLATE), "..", "graph", "template.html")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def runtime_source():
    path = os.path.join(
        os.path.dirname(TEMPLATE), "..", "..", "static", "runtime.js")
    with open(path, encoding="utf-8") as handle:
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
    # refresh-proof and the URL is shareable. Read-only is the default: an absent
    # param opens the note locked, and only an explicit "1" grants editing, so a
    # stray keystroke on a note you opened to READ cannot rewrite it.
    assert 'fused.params.get("edit") === "1"' in source
    assert 'fused.params.set("edit", next)' in source
    # Every other reader of the param agrees on that default, or a fresh load
    # would disagree with the first onChange about which mode it is in.
    assert 'lastEdit = fused.params.get("edit") || "0";' in source
    assert 'const edit = params.edit || "0";' in source


def test_the_mode_toggle_is_a_corner_button_not_a_toolbar(source):
    # MD-2a still holds: a second 26px button in the same cluster, not a row.
    assert 'id="toggle-edit"' in source
    assert 'id="bar"' not in source
    assert 'aria-pressed' in source[source.index('id="toggle-edit"'):][:400]


def test_the_accent_marks_editing_not_the_default(source):
    # Read-only is the default (MD-1a), so accenting it would leave the corner
    # permanently lit and say nothing. `aria-pressed` tracks EDITING, and the
    # glyph names the current mode: padlock while locked, pencil while editing.
    assert 'editToggleEl.setAttribute("aria-pressed", String(on));' in source
    assert '#toggle-edit .icon-edit { display: none; }' in source
    assert '#toggle-edit[aria-pressed="true"] .icon-edit { display: block; }' in source
    assert '#toggle-edit[aria-pressed="true"] .icon-lock { display: none; }' in source
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
    # Wikilinks are not in the markdown grammar, so they are matched by regex —
    # but whether a match COUNTS is answered by the tree, not by a second block
    # parser. That is what keeps graph.py's code-masking rule (MD-3) from having
    # a rival implementation in JS.
    assert "CM.syntaxTree(state)" in source
    assert "within(tree, start + 2, CODE_NODES)" in source
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


@pytest.fixture(scope="module")
def graph_create_ghost(graph_source):
    body = graph_source[graph_source.index("async function createGhost"):]
    return body[:body.index("\n    const canvas = fusedGraph.create")]


def test_a_ghost_click_on_an_existing_note_opens_it_instead_of_clobbering_it(
        create_ghost, graph_create_ghost):
    """A ghost whose target DOES exist must never be written over.

    `resolved` only holds the last scan's answers, so a `[[Note]]` you just
    typed renders as a ghost until the next scan lands, and an AMBIGUOUS target
    is a ghost by design (`_only` returns None for two same-named notes) —
    precisely the case where a real file sits at the computed path. An
    unconditional `writeFile` there replaced the note with a one-line stub.

    The guard is the server's `create` flag, not a stat-then-write: there is no
    window between the check and the write for the file to appear in, and a
    failure that is NOT "already exists" (a directory at the path, a
    permissions error) reports itself instead of reading as "absent, go ahead".
    """
    for body, where, nav in (
        (create_ghost, "the note view", 'openNote(path, "")'),
        (graph_create_ghost, "the graph mode", "navigateShell(path)"),
    ):
        assert body.count("fused.writeFile") == 1, where
        assert "{ create: true }" in body, where
        # The write is create-only, so nothing here stats first and then trusts
        # the answer.
        assert "fused.stat" not in body, where
        # "It already exists" is the one failure that navigates instead of
        # reporting; everything else is reported and stops.
        assert 'if (err.type !== "exists")' in body, where
        assert body.index('err.type !== "exists"') < body.rindex(nav), where


def test_a_reload_applies_the_writability_it_just_read(source):
    """`reloadFromDisk` updated `writable` and then did nothing with it.

    The editor's `editable`/`readOnly` facets are chosen when the view is built
    and the corner button's disabled state and title are set by applyEditMode,
    so reassigning the variable alone left both showing the OLD answer: a file
    that had become read-only stayed typeable (and the edits then failed at save
    time), and one that had become writable stayed locked until the iframe
    reloaded. This is the silent-reload path, so nothing else was going to
    notice.
    """
    body = source[source.index("async function reloadFromDisk"):]
    body = body[:body.index('\n    // "Keep my version"')]
    assert "writable = st.writable !== false;" in body
    # The rebuild is what applies the facets; the badge is what says so.
    assert "applyEditMode(true)" in body
    assert "fusedRoBadge.update" in body
    # Only when it actually changed — a rebuild on every reload would be churn.
    assert "writable !== wasWritable" in body


def test_the_write_bridge_can_refuse_to_clobber_an_existing_file(runtime_source):
    # createGhost's guard is only as good as the bridge under it: `create` has
    # to reach the server, and its 409 has to be distinguishable from the
    # optimistic-lock 409 (which means "changed", not "exists").
    assert "payload.create = true" in runtime_source
    assert 'err.type = "exists"' in runtime_source


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


def test_the_create_path_reads_the_ghost_target_not_its_label(canvas_source):
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


def test_strikethrough_and_inline_code_toggle_the_way_bold_does(source):
    """Two more markers through the same marker-agnostic toggleWrap.

    Both already render in Live Preview (`Strikethrough` and `InlineCode`,
    MD-18a), so neither needs a decoration — which is exactly why `==` highlight
    is NOT here: the vendored grammar has no rule for it, so the markers would
    stay bare on the page (D189).
    """
    assert 'key: "Mod-Shift-x", run: whenWritable((v) => toggleWrap(v, "~~"))' \
        in " ".join(source.split())
    assert 'key: "Mod-Shift-e", run: whenWritable((v) => toggleWrap(v, "`"))' \
        in " ".join(source.split())
    assert 'toggleWrap(v, "==")' not in source
    # And no binding collides with another: one key, one entry.
    keymap = source[source.index("const editorKeymap = ["):]
    keymap = keymap[:keymap.index("\n    ];")]
    keys = re.findall(r'key: "([^"]+)"', keymap)
    assert sorted(keys) == sorted(set(keys)), keys


def test_a_toggle_with_no_selection_wraps_the_word_under_the_caret(source):
    """Obsidian's ⌘B with nothing selected bolds the word, not nothing.

    The expansion is also what makes the unwrap reachable from a bare caret, and
    the word comes from CM's own `wordAt` so this template holds no second
    definition of a word. Where there is none — whitespace, an empty line — the
    old empty-pair-with-the-caret-inside behaviour is still right.
    """
    body = source[source.index("function toggleWrap"):]
    body = body[:body.index("\n    }")]
    assert "let { from, to } = state.selection.main;" in body
    assert "if (from === to) {" in body
    assert "const word = state.wordAt(from);" in body
    assert "if (word) {" in body


def test_cmd_k_inside_an_existing_link_edits_its_target(source):
    """⌘K on a caret inside `[a](b)` must not nest a second link.

    Which ranges are links comes from `syntaxTree` (MD-3), but the tree alone is
    a trap: the vendored grammar wraps a `[[wikilink]]`'s brackets in
    `Link`/`Image` nodes too (MD-18a), so the enclosing node must ALSO parse as a
    plain inline link — the same second test the decoration builder applies
    before it draws a link widget, not a second rule (D189).
    """
    body = source[source.index("function enclosingLinkTarget"):]
    body = body[:body.index("\n    }\n")]
    assert "CM.syntaxTree(state)" in body
    assert 'if (node.name !== "Link") continue;' in body
    # The plain-inline-link test, character for character the builder's.
    assert r"/^\[([^\]]*)\]\(([^()\s]*)\)$/" in body
    assert r"/^(!?)\[([^\]]*)\]\(([^()\s]*)\)$/" in source, "the builder's own"
    assert "if (!match) continue;" in body
    # And ⌘K selects that target rather than rewriting anything.
    link = source[source.index("function insertLink"):]
    link = link[:link.index("\n    }\n")]
    assert "const existing = enclosingLinkTarget(editorView.state, to);" in link
    assert "selection: { anchor: existing.from, head: existing.to }" in link


def test_tab_takes_an_open_completion_before_it_indents(source):
    # CM's completionKeymap binds only Enter to acceptCompletion, and a
    # Tab-completing habit reaches for Tab. acceptCompletion returns false with
    # no popup open, so indenting stays Tab's ordinary meaning.
    assert "CM.acceptCompletion(v) || CM.indentMore(v)" in source
    entry = os.path.join(
        os.path.dirname(TEMPLATE), "..", "..", "..", "scripts",
        "vendor-codemirror", "entry.js")
    with open(entry, encoding="utf-8") as handle:
        entry_source = handle.read()
    # MD-13: anything not re-exported is tree-shaken, so the gate is entry.js.
    assert "acceptCompletion" in entry_source
    bundle = os.path.join(
        os.path.dirname(TEMPLATE), "..", "vendor", "codemirror.bundle.js")
    with open(bundle, encoding="utf-8") as handle:
        assert "acceptCompletion" in handle.read(), "bundle not rebuilt"


def test_no_new_binding_can_write_to_a_read_only_buffer(source):
    """MD-1a/MD-15: read-only mode is the two facets, and neither filters a
    hand-built dispatch — which is all these commands make.

    `editable.of(false)` is what stops the key being delivered at all, but that
    is a property of the view, so each writing command asks the state as well.
    """
    assert "editorView.state.readOnly ? true : command(editorView)" in source
    keymap = source[source.index("const editorKeymap = ["):]
    keymap = keymap[:keymap.index("\n    ];")]
    runs = re.findall(r"(?:run|shift): (.+?),?\n", keymap)
    for run in runs:
        assert run.startswith("whenWritable("), run


def test_pasting_a_url_over_a_selection_makes_a_link(source):
    body = source[source.index("const pasteHandler"):]
    body = body[:body.index("\n    });")]
    assert "if (from === to) return false;" in body
    assert "`[${selected}](${url})`" in body


def test_the_popup_offers_notes_and_headings_from_the_same_scan(source):
    body = source[source.index("async function wikilinkCompletions"):]
    body = body[:body.index("\n    function editorExtensions")]
    assert "/\\[\\[([^\\[\\]\\n]*)$/" in body      # `[[` and `![[`
    assert "headingOptions(headings, notePart)" in body
    assert 'action: "candidates"' in source


def test_the_popup_inserts_the_form_graph_py_says_resolves(source):
    # `note.link` / `note.embed` are _link_form's output, which is verified
    # against resolve_link itself in tests/test_markdown_graph.py — the page must
    # not compute its own, and must not insert a form that is absent.
    body = source[source.index("async function wikilinkCompletions"):]
    body = body[:body.index("\n    // ---- `](…`")]
    assert "const form = embedding ? note.embed : note.link;" in body
    assert "if (form) options.push({ label: form" in body
    # And the note it is inserted INTO is sent, because tier 1 of resolution is
    # relative to that note's folder (MD-14).
    assert 'action: "candidates", root, file' in source


def test_an_embed_can_complete_an_asset_and_a_plain_wikilink_cannot(source):
    # `![[image.png]]` is the common Obsidian embed and an embed resolves through
    # the ASSET index, so the assets the payload already carries belong in the
    # popup — but only when the `!` is there.
    body = source[source.index("async function wikilinkCompletions"):]
    body = body[:body.index("\n    // ---- `](…`")]
    assert 'const embedding = before[wiki.index - 1] === "!";' in body
    assert "for (const asset of data.assets)" in body
    # The asset's own resolver-validated form, never the bare path: graph.py runs
    # every candidate form through `resolve_link` against the index the embed
    # resolver uses, and a page-side shortening rule is the divergence MD-14
    # exists to prevent. No form at all is `null`, and a null row is DROPPED
    # rather than inserted as a path that resolves elsewhere.
    assert "if (asset.embed) {" in body
    assert 'label: asset.embed, detail: "embed"' in body
    assert "label: rel" not in body


def test_an_inline_link_target_completes_from_notes_and_assets(source):
    body = source[source.index("async function inlinePathCompletions"):]
    body = body[:body.index("\n    // ---- `](#…`")]
    assert "/(!?)\\[[^\\]\\n]*\\]\\(([^)\\s]*)$/" in body
    assert "pathOptions(data, dir, inline[1] === \"!\")" in body
    # Local filtering, so typing does not re-run the source per keystroke.
    assert "validFor: /^[^)\\s]*$/" in body
    options = source[source.index("function pathOptions"):]
    options = options[:options.index("\n    }")]
    # Every note plus every asset — and only images where only an image renders.
    # An asset row is `{rel, embed}` now; this context computes its own relative
    # form, so it reads `rel` and ignores the wikilink form entirely.
    assert "const assetRels = data.assets.map((asset) => asset.rel);" in options
    assert "assetRels.filter((rel) => IMAGE_EXT_RE.test(rel))" in options
    assert "data.notes.map((note) => note.rel).concat(assetRels)" in options


def test_a_completed_path_is_relative_to_the_note_and_percent_encoded(source):
    # `[x](my file.png)` is not a link to the GFM parser at all, so the readable
    # form is displayed and the encoded form is what lands in the document.
    options = source[source.index("function pathOptions"):]
    options = options[:options.index("\n    }")]
    assert "label: encodeTarget(readable)," in options
    assert "displayLabel: readable," in options
    assert "const readable = relativeTarget(dir, rel);" in options
    encode = source[source.index("function encodeTarget"):]
    encode = encode[:encode.index("\n    }")]
    assert "encodeURIComponent(segment)" in encode
    # A parenthesis closes the target early, and encodeURIComponent leaves both.
    assert '.replace(/\\(/g, "%28").replace(/\\)/g, "%29")' in encode
    relative = source[source.index("function relativeTarget"):]
    relative = relative[:relative.index("\n    }")]
    # A sibling is `img.png`, never `../folder/img.png`: `../` only where the
    # target really is above the note.
    assert '"../".repeat(from.length - shared)' in relative
    # Pure string work, and it never rebuilds an absolute path — the Windows
    # drive form `C:/…` must not gain the leading slash resolvePath documents.
    vault = source[source.index("function vaultRelDir"):]
    vault = vault[:vault.index("\n    }")]
    assert "file.startsWith(base + \"/\")" in vault
    for forbidden in ("readFile", "runPython", "await "):
        assert forbidden not in vault + relative + encode


def test_an_inline_link_can_complete_an_anchor_in_this_note(source):
    body = source[source.index("async function headingAnchorCompletions"):]
    body = body[:body.index("\n    function editorExtensions")]
    assert "/\\]\\((#[^)\\s]*)$/" in body
    # The same headings `[[#` offers, encoded — `](#My Heading)` is not a link.
    assert 'headingOptions(notes.headings, "", true)' in body
    assert "validFor: /^#[^)\\s]*$/" in body
    # And the path source must not also claim this context.
    inline = source[source.index("async function inlinePathCompletions"):]
    assert 'if (typed.startsWith("#")) return null;' in inline[:inline.index("\n    }")]


def test_a_scan_that_never_ran_says_so_instead_of_showing_no_matches(source):
    # MD-11a: unknown is not missing. Returning null looked exactly like an empty
    # vault on the one path where the scan cannot succeed (a mount-backed root,
    # which graph.py refuses, MD-11).
    notice = source[source.index("function scanNoticeOptions"):]
    notice = notice[:notice.index("\n    }")]
    assert "filter: false," in notice           # cannot be typed away
    assert "apply: () => {}," in notice         # and cannot be inserted
    assert "message || candidatesNotice || UNRESOLVED_HERE" in notice
    ensure = source[source.index("async function ensureCandidates"):]
    ensure = ensure[:ensure.index("\n    // One informational row")]
    # graph.py's own words, so a mount refusal reads as itself.
    assert "candidatesNotice = data.message || UNRESOLVED_HERE;" in ensure
    # The failure is cached as hard as a success: one run per TTL, not per
    # keystroke, on a root that can never answer.
    assert "if (Date.now() - candidatesAt < CANDIDATES_TTL_MS) return candidates;" in ensure
    assert "candidatesAt = Date.now();" in ensure
    for name in ("wikilinkCompletions", "inlinePathCompletions"):
        body = source[source.index("async function " + name):]
        body = body[:body.index("\n    // ---- ")]
        assert "if (!data) return scanNoticeOptions(from);" in body


def test_the_popup_is_themed_through_the_editor_so_a_theme_flip_keeps_it(source):
    """The popup's look is a CM theme extension, not page CSS.

    An appearance flip dispatches `StateEffect.reconfigure` over the extension
    array (MD-14, and the reconfigure test above), so a theme extension in that
    array is reinstalled with everything else; a page rule would instead have to
    keep out-specifying whatever oneDark reinstates. And because every colour is
    a var() token read off <html>, one theme is correct in both palettes — there
    is no dark copy of these rules to drift.
    """
    theme = source[source.index("const completionTheme = CM.EditorView.theme({"):]
    theme = theme[:theme.index("\n    });")]
    # It rides in the SAME array the reconfigure rebuilds from.
    extensions = source[source.index("function editorExtensions"):]
    extensions = extensions[:extensions.index("\n    function buildEditor")]
    assert "completionTheme," in extensions
    # Tokens, never literal colours — the flip works by var() resolving again.
    for token in ("var(--bg-alt)", "var(--border)", "var(--fg)", "var(--accent)",
                  "var(--fg-muted)"):
        assert token in theme
    # And the full-bleed accent bar this replaced is gone from the page style.
    style = source[source.index("<style>"):source.index("</style>")]
    assert "cm-tooltip-autocomplete" not in style
    assert "background: var(--accent);\n    color: var(--on-accent);" not in style
    # The generic tooltip shell stays a page rule (the search panel's tooltip is
    # one too), which is exactly why the popup's rules out-specify it.
    assert ".cm-editor .cm-tooltip {" in style


def test_a_row_shows_a_dimmed_detail_column_beside_its_label(source):
    # `docs/other.mddocs/other.md` and `a.pngembed` were the label and the detail
    # rendered with nothing between them. The detail is a column of its own now:
    # pushed right, dimmed and smaller, so it qualifies the row instead of
    # extending it.
    theme = source[source.index("const completionTheme = CM.EditorView.theme({"):]
    theme = theme[:theme.index("\n    });")]
    detail = theme[theme.index(".cm-completionDetail"):]
    detail = detail[:detail.index("},")]
    assert 'marginLeft: "auto"' in detail
    assert 'color: "var(--fg-muted)"' in detail
    assert 'fontStyle: "normal"' in detail        # CM's base italicises it
    # The label is the flexible half and clips rather than wrapping, and its
    # leading spaces survive: a heading row indents by nesting depth, the same
    # rule the sidebar outline uses (MD-19b's language).
    label = theme[theme.index(".cm-completionLabel"):]
    label = label[:label.index("},")]
    assert 'whiteSpace: "pre"' in label
    assert 'textOverflow: "ellipsis"' in label
    headings = source[source.index("function headingOptions"):]
    headings = headings[:headings.index("\n    }")]
    assert '"  ".repeat(h.depth) + h.text' in headings
    assert 'detail: "H" + h.level' in headings
    # Not the literal hashes it used to print into the label.
    assert '"#".repeat(h.level)' not in headings


def test_the_typed_substring_is_emphasised_in_every_row(source):
    # These lists are every note plus every asset in the vault, so the matched
    # characters are the only thing saying why a row is still in the list. CM
    # marks them with `.cm-completionMatchedText` and styles them not at all.
    theme = source[source.index("const completionTheme = CM.EditorView.theme({"):]
    theme = theme[:theme.index("\n    });")]
    matched = theme[theme.index(".cm-completionMatchedText"):]
    matched = matched[:matched.index("},")]
    assert 'color: "var(--accent)"' in matched
    assert 'fontWeight: "700"' in matched
    # The selected row is not colour alone: an accent left edge and bold weight,
    # in a border slot that is transparent when the row is not selected so
    # selecting it does not shift the text.
    selected = theme[theme.index("> ul > li[aria-selected]"):]
    selected = selected[:selected.index("},")]
    assert 'borderLeftColor: "var(--accent)"' in selected
    assert 'fontWeight: "600"' in selected
    assert 'borderLeft: "2px solid transparent"' in theme
    # And the emphasis has to be MAPPED, not assumed: CM matches against `label`
    # and refuses to guess where those characters landed in a `displayLabel`
    # (`sortOptions` hands an empty match to any option that has one), which is
    # every path row and every heading row here — so every result that displays a
    # form of its own supplies `getMatch`. Found here by driving the live page:
    # without it the popup rendered no matched span at all.
    match = source[source.index("function displayMatch"):]
    match = match[:match.index("\n    }")]
    assert "const shown = completion.displayLabel;" in match
    assert "lower.indexOf(ch.toLowerCase(), at)" in match
    # A character the display drops (a heading's `#`) is skipped, never fudged
    # into a range over the wrong letters.
    assert "if (found === -1) continue;" in match
    assert source.count("getMatch: displayMatch") == 3


def test_a_completed_path_carries_no_detail_that_repeats_its_own_label(source):
    # `pathOptions` set `detail: rel` while the label displayed the same path
    # relative to the note — identical for any target in or under the note's own
    # folder, which is most of them, so the row read the path twice. The readable
    # form is still DISPLAYED and the percent-encoded form is still what inserts
    # (MD-14): that pair is load-bearing and must not be touched by presentation.
    options = source[source.index("function pathOptions"):]
    options = options[:options.index("\n    }")]
    assert "detail:" not in options
    assert "label: encodeTarget(readable)," in options
    assert "displayLabel: readable," in options
    # The one context whose detail says something the label does not keeps it.
    wiki = source[source.index("async function wikilinkCompletions"):]
    wiki = wiki[:wiki.index("\n    // ---- `](…`")]
    assert 'label: asset.embed, detail: "embed"' in wiki


def test_the_popup_has_no_icon_column(source):
    """CM's glyph for a `text` completion is the literal string "abc".

    It appeared on every path row, naming nothing. The row already says its kind
    — an extension, an indented heading, an `embed` detail — and with two `type`
    values across four contexts an icon could only repeat that or blur it (D192).
    `icons: false` removes the element rather than hiding it, so no empty column
    is left holding width in a popup that is already narrow.
    """
    extensions = source[source.index("function editorExtensions"):]
    extensions = extensions[:extensions.index("\n    function buildEditor")]
    assert "icons: false," in extensions


def test_the_popup_is_shaped_like_the_sidebar_next_to_it(source):
    # Prose font (not CM's monospace), the sidebar's radius, a real shadow, and a
    # capped height so a vault-sized list scrolls instead of filling the window.
    theme = source[source.index("const completionTheme = CM.EditorView.theme({"):]
    theme = theme[:theme.index("\n    });")]
    shell = theme[theme.index('".cm-tooltip.cm-tooltip-autocomplete": {'):]
    shell = shell[:shell.index("},")]
    assert 'borderRadius: "8px"' in shell
    assert "boxShadow:" in shell
    listing = theme[theme.index('> ul": {'):]
    listing = listing[:listing.index("},")]
    assert 'fontFamily: "inherit"' in listing
    assert 'maxHeight: "16em"' in listing
    assert 'overflow: "hidden auto"' in listing
    # A row is a row of prose, not CM's 1px-padded line.
    row = theme[theme.index('> ul > li": {'):]
    row = row[:row.index("},")]
    assert 'padding: "4px 7px"' in row
    assert 'borderRadius: "5px"' in row


def test_each_completion_context_is_its_own_source(source):
    # One function per context rather than one with a chain of regexes, still
    # overriding CM's own sources at high precedence.
    extensions = source[source.index("function editorExtensions"):]
    extensions = extensions[:extensions.index("\n    function buildEditor")]
    assert "CM.Prec.high(CM.autocompletion({" in extensions
    assert ("override: [\n            wikilinkCompletions, inlinePathCompletions, "
            "headingAnchorCompletions,\n          ],") in extensions


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
    """`total_notes` counts notes; `nodes` also holds ghost nodes.

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


def test_the_outline_is_a_section_of_the_one_sidebar_not_a_second_panel(source):
    # MD-19b/MD-19a: one right sidebar behind one toggle, and MD-2a forbids the
    # toolbar row that a second control would want. The outline is the first
    # section of the panel that already exists, so it must also be the one
    # clearing the floating corner cluster.
    assert 'id="outline-head"' in source and 'id="outline"' in source
    assert source.index('id="outline"') < source.index('id="links"')
    assert "  #outline-head { padding-right: 72px; }" in source
    assert "#links-head { padding-right: 72px; }" not in source
    # No toggle and no param of its own: the outline has no state to keep, so
    # MD-20 has nothing to carry (a second toggle would break MD-19a anyway).
    assert "toggle-outline" not in source
    assert 'params.get("outline")' not in source
    # Sized like #links and for the same reason: it must not starve the canvas.
    outline = source[source.index("  #outline {"):]
    outline = outline[:outline.index("\n")]
    assert "flex: none" in outline and "max-height:" in outline
    assert "overflow: auto" in outline and "min-height: 0" in outline


def test_the_outline_reads_the_live_document_not_the_saved_payload(source):
    # The payload only re-parses on save (MD-9), so an outline fed by
    # `notes.headings` would lag every heading typed by up to one autosave
    # interval. It reads the doc, on the docChanged the editor already reports —
    # and nothing here polls (MD-17's stat-storm lesson).
    body = source[source.index("function documentHeadings"):]
    body = body[:body.index("\n    // The last outline drawn")]
    assert "view.state.doc" in body
    assert "notes.headings" not in body
    render = source[source.index("function renderOutline"):]
    render = render[:render.index("\n    }")]
    for forbidden in ("setInterval", "setTimeout", "notes.headings", "runPython"):
        assert forbidden not in render + body
    listener = source[source.index("CM.EditorView.updateListener.of((update) => {"):]
    listener = listener[:listener.index("\n        }),")]
    assert "if (!update.docChanged) return;" in listener
    assert "renderOutline();" in listener
    # And every path that swaps the whole document redraws it.
    build = source[source.index("function buildEditor"):]
    build = build[:build.index("\n      themeObserver?.disconnect();")]
    assert "outlineKey = null;" in build and "renderOutline();" in build
    assert "renderOutline();" in source[source.index("function applySidebar"):]


def test_the_outline_masks_code_the_way_graph_py_does(source):
    # A `# not a heading` inside a fenced block is in no other heading surface
    # (the `[[#` popup, the graph payload), so it must not be in this one:
    # graph.py's `_mask_code` rule, same fence and frontmatter handling, and ATX
    # only — which is all `_HEADING` and `scrollToHeading` know.
    body = source[source.index("function documentHeadings"):]
    body = body[:body.index("\n    // The last outline drawn")]
    assert "/^ {0,3}(`{3,}|~{3,})/" in body
    assert "/^(---|\\.\\.\\.)[ \\t]*$/" in body
    assert "/^(#{1,6})[ \\t]+(.+?)[ \\t]*#*[ \\t]*$/" in body
    # Not the syntax tree: `syntaxTree(state)` is only parsed as far as CM has
    # got, so a long note would silently lose its tail headings.
    assert "syntaxTree" not in body


def test_an_outline_row_scrolls_to_its_own_line_and_reads_when_locked(source):
    # It built the row from that line, so it scrolls to that line: matching by
    # text would hand a second `## Notes` to the first one. And the whole thing
    # is a reading affordance, so it goes through the one delegated click handler
    # and never asks whether the buffer is editable (MD-1a).
    render = source[source.index("function renderOutline"):]
    render = render[:render.index("\n    }")]
    assert 'data-line="${row.line}"' in render
    assert "--ol-depth: ${row.depth}" in render
    # Nesting depth from the levels present, not from the hash count — and it
    # comes from the shared rule, not a copy of it (see the popup's test).
    assert "withDepth(rows)" in render
    # Same voice as the backlinks empty state, and the same row class, so one
    # panel has one row style.
    assert '<div class="bl-empty">No headings in this note.</div>' in render
    assert 'class="bl ol"' in render
    for forbidden in ("editing()", "writable", "readOnly"):
        assert forbidden not in render
    click = source[source.index('const outlined = event.target.closest("[data-line]");'):]
    click = click[:click.index("const create = ")]
    assert 'scrollToLine(parseInt(outlined.getAttribute("data-line"), 10));' in click


def test_an_unclosed_frontmatter_block_is_not_frontmatter(source):
    # Reported in review. The scan used to set a flag on a first-line `---` and
    # skip every line until a closer, so from the keystroke that opened the block
    # to the one that closed it the whole outline was empty — while graph.py's
    # `_frontmatter_span` (no closer, no span) and the decoration scan above (which
    # looks for the end BEFORE it dims anything) both kept those headings. Three
    # surfaces for one note's headings, so it is one rule: find the closer first.
    body = source[source.index("function documentHeadings"):]
    body = body[:body.index("\n    // The last outline drawn")]
    opened = body.index("let frontmatterEnd = 0;")
    loop = body.index("for (let n = frontmatterEnd + 1; n <= doc.lines; n++) {")
    assert opened < loop, "the closer must be found before any line is skipped"
    # No standing flag can outlive the block any more.
    assert "let frontmatter = false;" not in body
    assert "frontmatter = true" not in body


def test_the_popup_indents_a_heading_by_the_same_rule_as_the_outline(source):
    # Reported in review: the popup indented by raw level while the outline
    # indented by nesting depth, so a note starting at `##` — or one that skips a
    # level — was drawn two ways, against what MD-14 says. One function now, used
    # by both, which is the only version of "they match" that stays true.
    depth = source[source.index("function withDepth(headings) {"):]
    depth = depth[:depth.index("\n    }")]
    assert "while (stack.length && stack[stack.length - 1] >= h.level) stack.pop();" in depth
    options = source[source.index("function headingOptions(headings, prefix, encode) {"):]
    options = options[:options.index("\n    }")]
    assert "withDepth(headings)" in options
    assert '"  ".repeat(h.depth)' in options
    # The raw level survives only as the dimmed marker, where it is the point.
    assert 'detail: "H" + h.level' in options
    assert "h.level - 1" not in options
    assert source.count("stack[stack.length - 1] >=") == 1, "one rule, not two"


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


def test_a_note_linking_here_three_times_is_one_row_with_a_count(source):
    # graph.py reports one backlink per LINK, and rendering that verbatim showed
    # "rendering" three times in a row — three rows that read as three different
    # notes until the paths were compared. The list groups by the linking note's
    # path (first-seen order — not adjacency, which would depend on how graph.py
    # happens to sort) and the multiplicity survives as a muted ×N on the row.
    body = source[source.index("const byPath = new Map()"):]
    body = body[:body.index("bl-empty")]
    assert "seen.count += 1" in body
    assert "byPath.set(row.path, { ...row, count: 1 })" in body
    assert 'row.count > 1 ? `<span class="bl-count">' in body
    # The header counts the grouped rows — notes, not links.
    assert "`Backlinks (${rows.length})`" in body


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
    assert "drag.userPinned = true" in canvas_source         # drag-to-pin
    assert "neighbours(hover)" in canvas_source              # hover highlighting
    assert "[3, 3]" in canvas_source                         # dashed ghost edges


def test_the_layout_gives_every_label_its_measured_width_then_frames_itself(canvas_source):
    """The two halves of "the graph is too dense to read", which only work
    together: each label owning a slot as wide as it measures (so two labels
    cannot be assigned overlapping ground), and a fit-to-canvas so the sized
    layout does not simply fall off the edge of a narrow sidebar.

    One layout serves both surfaces (D157), and the spacing is deliberately NOT
    scaled per surface — uniformly scaling a layout that gets fitted to the
    canvas changes nothing on screen. The zoom is what varies.
    """
    assert "var SLOT_PAD = 18;" in canvas_source
    assert "var SLOT_MIN = 60;" in canvas_source
    assert "function measureLabels(list)" in canvas_source
    assert "function frameAll()" in canvas_source
    # Only ever zooms out, and never past a floor.
    assert "zoom = Math.min(1.5, Math.max(0.15," in canvas_source
    # And it yields to the user the moment they touch the canvas.
    assert "if (userFramed || !nodes.length) return;" in canvas_source
    assert canvas_source.count("userFramed = true") == 2      # pan/drag, and wheel


def test_positions_are_assigned_not_simulated(canvas_source):
    # The spring sim is gone, and must stay gone: its two forces (label spacing
    # and column alignment) pull opposite ways in a hub-shaped graph, and the
    # alignment side winning is what printed "configuratiobservability" across
    # the panel. The barycenter ordering plus measured slots answer both wants
    # without a fight; nothing here may reintroduce a force term.
    assert "REPULSION" not in canvas_source
    assert "_bary" in canvas_source                      # the ordering sweeps
    assert "bezierCurveTo" in canvas_source              # edges curve, not rope
    # The scale half is asserted by RUNNING the layout, in
    # test_graph_canvas.py::test_a_big_folder_graph_stays_banded_and_bounded.


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
    assert "scrollToLine(n);" in body
    # One place owns the rule, so the outline's rows obey it too (MD-19b).
    jump = source[source.index("function scrollToLine"):]
    jump = jump[:jump.index("\n    }")]
    assert "CM.EditorView.scrollIntoView(" in jump
    assert "selection" not in body + jump


# --------------------------------------------- pasted and dropped media (MD-23)


@pytest.fixture(scope="module")
def media_helper(source):
    """`insertMediaFiles`, the one path both paste and drop go through."""
    body = source[source.index("async function insertMediaFiles("):]
    return body[:body.index("\n    }\n")]


@pytest.fixture(scope="module")
def paste_handler(source):
    body = source[source.index("const pasteHandler = CM.EditorView.domEventHandlers("):]
    return body[:body.index("\n    });")]


def test_paste_takes_the_bytes_off_the_clipboard(paste_handler):
    # Browser "Copy image" puts image BYTES on the clipboard, not a file
    # reference, so this is the ordinary paste event's `files` list — there is
    # nothing to read off a path and no file picker involved.
    assert "event.clipboardData" in paste_handler
    assert ".files" in paste_handler
    assert "insertMediaFiles(" in paste_handler


def test_pasting_media_is_a_no_op_on_a_read_only_note(paste_handler):
    # Same posture as whenWritable (MD-1a/MD-15): bail and return false so the
    # paste falls through to the browser default untouched, rather than
    # half-running and failing at the write.
    assert "state.readOnly" in paste_handler


def test_media_lands_in_an_assets_folder_beside_the_note(media_helper):
    # A shared assets/ next to the note, created on demand. The 409 "exists"
    # from the second paste onwards is the expected case, not an error.
    assert '"/assets"' in media_helper
    assert "fused.mkdir(" in media_helper
    assert 'err.type !== "exists"' in media_helper


def test_the_file_name_is_a_timestamp_so_pastes_never_collide(source, media_helper):
    # Timestamps mean no directory scan and no prompt before a paste lands.
    name = source[source.index("function mediaName("):]
    name = name[:name.index("\n    }")]
    assert '"pasted-" + stamp' in name
    # The stamp is only the STARTING point — see the collision test below for
    # what makes it actually unique.
    assert "freeMediaName(" in media_helper


def test_the_upload_is_awaited_before_the_link_is_inserted(media_helper):
    # Order is the contract: inserting first would leave a link pointing at a
    # file that failed to write.
    upload_at = media_helper.index("await fused.uploadFile(")
    assert "dispatch(" not in media_helper[:upload_at]
    assert "dispatch(" in media_helper[upload_at:]


def test_a_failed_upload_surfaces_instead_of_vanishing(media_helper):
    # Reported through mediaNotice — the template's existing error surface,
    # the same one a failed save uses — never a silently swallowed catch.
    assert "mediaNotice(" in media_helper


def test_media_is_inserted_as_ordinary_markdown_at_the_given_position(media_helper):
    # `![](…)` and a normal dispatch, so undo removes it in one step and every
    # other markdown tool can still read the note.
    assert "![](" in media_helper
    assert "dispatch(" in media_helper
    # The position is a PARAMETER, not the selection: drop passes the drop
    # point. Reading the selection in here would silently ignore it. `to` is a
    # parameter for the same reason — a paste replaces the selection, a drop
    # must not, and only the caller knows which gesture it is.
    assert "function insertMediaFiles(view, files, pos, to)" in media_helper
    assert "state.selection" not in media_helper


def test_a_media_paste_replaces_the_selection_but_a_drop_does_not(source, media_helper):
    # Found in review. The dispatch set only `from`, so a media paste over a
    # selection left the selected text sitting beside the new image link —
    # unlike every other paste, including the URL-over-a-selection branch in the
    # same handler.
    assert "changes: { from: pos, to: replaceTo, insert }" in media_helper
    # The paste hands over the selection's whole range…
    paste = source[source.index("if (media.length)"):]
    assert "insertMediaFiles(target, media, sel.from, sel.to)" in paste[:400]
    # …and the drop hands over one point, so `to` defaults to it and nothing is
    # replaced. Both call sites are checked, since the whole bug was one of them
    # passing the wrong thing.
    calls = re.findall(r"insertMediaFiles\([^)]*\)", source)
    assert calls == [
        "insertMediaFiles(view, files, pos, to)",   # the definition
        "insertMediaFiles(target, media, sel.from, sel.to)",   # paste
        "insertMediaFiles(target, media, pos)",   # drop
    ], calls
def test_dragover_prevents_default_or_the_drop_never_happens(source, paste_handler):
    """Without preventDefault on dragover the browser refuses the drop.

    It then navigates the webview to the dropped file instead, which looks
    exactly like the editor vanishing. Gated to a drag CARRYING FILES so
    CodeMirror's own text drag-and-drop is untouched.
    """
    dragover = paste_handler[paste_handler.index("dragover("):]
    dragover = dragover[:dragover.index("\n      }")]
    assert "preventDefault()" in dragover
    assert "dragHasFiles(event)" in dragover
    # `Files` in the type list is the only thing askable this early —
    # dataTransfer.files is empty during a dragover — and drop asks the SAME
    # question, so the two cannot disagree about which drags are ours.
    helper = source[source.index("function dragHasFiles("):]
    helper = helper[:helper.index("\n    }")]
    assert '"Files"' in helper and "dataTransfer" in helper


def test_drop_reads_the_files_and_reuses_the_paste_pipeline(paste_handler):
    drop = paste_handler[paste_handler.index("\n      drop("):]
    assert "event.dataTransfer" in drop
    assert "insertMediaFiles(" in drop
    # No second copy of the ensure-dir/upload/insert work.
    assert "fused.uploadFile(" not in drop


def test_a_drop_lands_at_the_pointer_not_at_the_caret(paste_handler):
    # Dropping into the middle of a note should insert where the pointer is;
    # posAtCoords returns null for a drop outside any line, which falls back
    # to the caret.
    drop = paste_handler[paste_handler.index("\n      drop("):]
    assert "posAtCoords(" in drop
    assert "clientX" in drop and "clientY" in drop
    assert "state.selection.main.head" in drop


def test_dropping_media_is_a_no_op_on_a_read_only_note(paste_handler):
    drop = paste_handler[paste_handler.index("\n      drop("):]
    assert "state.readOnly" in drop


def test_every_file_drop_prevents_default_whatever_it_carried(paste_handler):
    """A PDF dropped on a note must not navigate the webview away.

    `dragover` commits to owning EVERY drag carrying files — per-file MIME is
    not readable that early, so it cannot be choosier. CodeMirror only calls
    preventDefault for a handler returning true, so a `return false` in `drop`
    hands the event back to the browser, whose default action for a file drop
    is to navigate to the file: the editor vanishes, taking any edits since the
    last autosave with it. So `drop` prevents FIRST and asks questions after.
    """
    drop = paste_handler[paste_handler.index("\n      drop("):]
    prevent = drop.index("event.preventDefault()")
    # Nothing is allowed to return before the preventDefault except the
    # not-a-file-drag fall-through.
    before = drop[:prevent]
    assert "dragHasFiles(event)" in before
    assert "mediaFiles(" not in before, "the media filter must come AFTER"
    assert "readOnly" not in before, "the read-only bail must come AFTER"


def test_a_text_drag_still_falls_through_to_codemirror(paste_handler):
    # Dragging selected text within the note is CM's own behaviour and must be
    # untouched — the question is whether the drag carried FILES, never whether
    # media matched.
    drop = paste_handler[paste_handler.index("\n      drop("):]
    head = drop[:drop.index("event.preventDefault()")]
    assert "if (!dragHasFiles(event)) return false;" in head


def test_a_non_media_file_drop_says_so_instead_of_doing_nothing(paste_handler):
    drop = paste_handler[paste_handler.index("\n      drop("):]
    tail = drop[drop.index("event.preventDefault()"):]
    assert tail.count("mediaNotice(") >= 2, (
        "both refusals — read-only, and nothing droppable in the drag — report")
    assert "Only images and video" in tail


def test_the_notice_is_the_same_surface_a_failed_save_uses(source):
    notice = source[source.index("function mediaNotice("):]
    notice = notice[:notice.index("\n    }")]
    assert "saveStateEl.textContent" in notice
    assert 'saveStateEl.classList.add("error")' in notice


def test_two_pastes_in_the_same_second_do_not_overwrite_each_other(source, media_helper):
    """The timestamp alone is not unique.

    Two pastes within one second produce the same name, and the upload does an
    unconditional os.replace — so the first file would be silently destroyed.
    The name is probed for existence and bumped until it is free, which covers
    the several-files-in-one-gesture case as well (each upload is awaited, so
    file 1 is already on disk when file 2 is named).
    """
    assert "await freeMediaName(" in media_helper
    free = source[source.index("async function freeMediaName("):]
    free = free[:free.index("\n    }")]
    assert "mediaExists(" in free
    # Bounded: a stat that always answers "yes" must not spin forever.
    assert "n < 100" in free
    exists = source[source.index("async function mediaExists("):]
    exists = exists[:exists.index("\n    }")]
    assert "fused.stat(" in exists


def test_every_video_mime_maps_to_an_extension_the_widget_plays(source):
    """The trap: `video/x-m4v` fell through to the MIME subtype and produced
    `pasted-….x-m4v`, which VIDEO_EXT does not match — so the clip rendered as
    a broken <img>. The two tables have to agree, for every entry."""
    table = source[source.index("const MEDIA_EXT = {"):]
    table = table[:table.index("};")]
    video_ext = re.search(r"const VIDEO_EXT = /\\\.\(([a-z0-9|]+)\)\$/i", source)
    assert video_ext, "VIDEO_EXT should still be an extension alternation"
    playable = set(video_ext.group(1).split("|"))
    mapped = re.findall(r'"video/[^"]+": "([a-z0-9]+)"', table)
    assert mapped
    assert set(mapped) <= playable, set(mapped) - playable
    assert '"video/x-m4v"' in table


def test_an_unmapped_x_prefixed_mime_does_not_become_the_extension(source):
    # `image/x-foo` must not produce `name.x-foo`; the fallback strips the
    # `x-` vendor prefix so an unmapped type still lands with a usable name.
    name = source[source.index("function mediaName("):]
    name = name[:name.index("\n    }")]
    assert 'replace(/^x-/, "")' in name


# ---- ordered-list renumbering (MD-25) --------------------------------------
# The behaviour itself is tested by running it, in tests/test_markdown_renumber.py.
# What only the source can say is that the filter is actually installed in the
# editor the page builds — the probe there constructs its own extension list, so
# it would happily pass on a filter that no real editor ever sees.


def test_the_renumber_filter_is_installed_in_the_editor(source):
    extensions = source[source.index("function editorExtensions()"):]
    extensions = extensions[:extensions.index("\n    }")]
    assert "renumberFilter," in extensions


def test_renumbering_excludes_undo_so_it_cannot_fight_the_history(source):
    # Undoing back to a deliberately odd numbering must not be corrected right
    # back. The filter opts IN to the user events it handles rather than
    # excluding undo by name, so this pins the allow-list.
    body = source[source.index("const renumberFilter ="):]
    body = body[:body.index("\n    });")]
    assert re.findall(r'isUserEvent\("(\w+)"\)', body) == ["input", "delete", "move"]
    # Appended to the same transaction, not dispatched after it, so one undo
    # takes back the edit and its renumbering together.
    assert "sequential: true" in body


def test_no_line_decoration_ever_carries_a_vertical_margin(source):
    """Margin on a `.cm-line` desynchronises CodeMirror's height map.

    CM measures each line from its bounding rect. Padding is inside that rect;
    margin is outside it, and adjacent margins collapse besides. A margin
    therefore makes CM believe a line is shorter than the space it occupies, and
    every coordinate-based operation reading that map — `posAtCoords`, so mouse
    clicks and arrow up/down both — lands on the wrong line, drifting further
    with each spaced block above it.

    This shipped once, in the first draft of MD-26, and the editor became
    unusable below the first heading. It is invisible in a screenshot and no
    probe can see it (the probe has no layout), so the guard is here.
    """
    css = source[source.index("/* ---- vertical rhythm (MD-26)"):]
    css = css[:css.index("/* Faint (--text-faint)")]
    offenders = re.findall(r"^\s*[^/*\n]*\bmargin[-a-z]*\s*:.*$", css, re.M)
    assert offenders == [], offenders
    # And the rules are really there — an empty block would pass the above.
    assert "padding-top" in css

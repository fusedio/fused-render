"""Source-contract tests for the markdown template's editor (SPEC §32).

Nothing in the suite executes template JS, so — like the `runtime.js` wiring
assertions (D137) — these pin the invariants that are easy to regress silently
and impossible to see in a diff review of a 900-line template:

* the Obsidian save model (MD-16): no save button, no dirty indicator, an idle
  timer plus blur/tab-switch, and ⌘S as a flush rather than *the* save;
* the one deviation (MD-17): a dirty buffer whose mtime moved gets a
  reload-or-keep banner instead of last-write-wins;
* read-only comes off `stat.writable` — the shell's persisted `read_only` flag —
  never `os.access` (MD-15);
* the link layer tokenizes through marked's extension hooks, so the
  code-masking rule in `graph.py` has no second copy in JS (MD-3).

The behavioural coverage for what a link IS and where it points lives in
tests/test_markdown_graph.py, against the Python that decides both.
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
    assert 'key === "s"' in source
    assert "void save();" in source


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


def test_the_link_layer_tokenizes_through_marked_extensions(source):
    # A pre-pass over the source would need a second copy of graph.py's
    # code-masking rule; marked already knows what is inside a fence.
    assert "marked.use({ extensions: [wikilinkExtension, tagExtension]" in source
    assert 'level: "inline"' in source


def test_resolution_is_never_recomputed_in_the_page(source):
    # The page maps a raw target to whatever graph.py resolved it to; it must
    # not contain a second resolution rule (MD-6).
    assert 'action: "note"' in source
    assert "resolved.set(link.target, link)" in source


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


def test_a_rendered_checkbox_writes_back_and_is_locked_when_read_only(source):
    body = source[source.index("function enableTaskBoxes"):]
    body = body[:body.index("\n    }")]
    assert "box.disabled = !writable;" in body
    assert "toggleTaskAt(position, box.checked)" in body


# ------------------------------------------------------- graph panel (MD-19)


def test_the_graph_panel_state_lives_in_params_so_it_is_shareable(source):
    assert 'fused.params.set("graph"' in source
    assert 'fused.params.set("depth"' in source
    assert 'fused.params.get("graph") === "1"' in source


def test_the_panel_asks_for_a_bounded_neighbourhood(source):
    body = source[source.index("async function loadGraph"):]
    body = body[:body.index("\n    function neighbours")]
    assert 'action: "graph"' in body
    assert "depth: String(graphDepth())" in body


def test_graph_colours_are_read_at_draw_time_not_baked(source):
    # var() cannot resolve inside a canvas fillStyle, so a theme flip has to
    # redraw with freshly-read tokens (SPEC §30).
    assert "function token(name)" in source
    assert "getPropertyValue(name)" in source
    assert 'attributeFilter: ["data-theme"] });' in source


def test_the_graph_behaviours_obsidian_has_are_present(source):
    assert "function radius(node)" in source          # radius ∝ degree
    assert "const labels = G.zoom > 0.75" in source    # labels fade past a zoom
    assert "node.pinned = true" in source              # drag-to-pin
    assert "neighbours(G.hover)" in source             # hover highlighting


def test_a_saved_note_refreshes_the_open_graph(source):
    assert "if (graphOn()) void loadGraph();" in source
    assert "candidates = null;" in source

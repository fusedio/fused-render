"""The markdown template's graph.py — link parsing, resolution, backlinks.

Obsidian-parity semantics are pinned here rather than in the template's JS,
because both surfaces (the note view and the graph views) read the same rows
and must agree on what a link is and where it points (SPEC §32, MD-3/MD-4).

Two rules get the most tests, because they are the ones everything else
depends on:

* **Links are parsed from the source with code elided.** A `[[Note]]` inside a
  fenced block or an inline code span is a code sample, not an edge.
* **Resolution happens at assembly time, never at index time** (MD-6/D154), so
  every resolution test drives `resolve_link` against a candidate set rather
  than against anything stored.
"""
import contextlib
import importlib.util
import os
from unittest import mock

import pytest

GRAPH = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "markdown", "graph.py"
)


@pytest.fixture(scope="module")
def graph():
    spec = importlib.util.spec_from_file_location("markdown_graph", GRAPH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _targets(parsed):
    return [link["target"] for link in parsed["links"]]


# ------------------------------------------------------------------ parsing


def test_wikilink_forms_are_parsed(graph):
    parsed = graph.parse_note(
        "See [[Note]], [[Other|the other one]], [[Deep#Heading]] and "
        "[[Deep#Heading|labelled]].\n"
    )
    assert _targets(parsed) == ["Note", "Other", "Deep", "Deep"]
    assert [link["label"] for link in parsed["links"]] == [
        None, "the other one", None, "labelled"]
    assert [link["heading"] for link in parsed["links"]] == [
        None, None, "Heading", "Heading"]
    assert not any(link["embed"] for link in parsed["links"])


def test_embeds_are_marked_and_keep_their_extension(graph):
    parsed = graph.parse_note("![[diagram.png]] and ![[Other Note]]\n")
    assert _targets(parsed) == ["diagram.png", "Other Note"]
    assert [link["embed"] for link in parsed["links"]] == [True, True]


def test_ordinary_relative_markdown_links_are_edges_but_urls_are_not(graph):
    parsed = graph.parse_note(
        "[rel](./sibling.md) [up](../up/other.md) [web](https://example.com/x.md) "
        "[mail](mailto:a@b.c) [anchor](#section)\n"
    )
    assert _targets(parsed) == ["./sibling.md", "../up/other.md"]


def test_links_inside_code_are_not_edges(graph):
    parsed = graph.parse_note(
        "real [[Yes]]\n"
        "```\n"
        "[[Fenced]] #fencedtag\n"
        "```\n"
        "inline `[[Spanned]] #spannedtag` done\n"
        "~~~md\n"
        "[[Tilde]]\n"
        "~~~\n"
    )
    assert _targets(parsed) == ["Yes"]
    assert parsed["tags"] == []


def test_indented_code_block_links_are_not_edges(graph):
    parsed = graph.parse_note("text\n\n    [[Indented]]\n\nreal [[Yes]]\n")
    assert _targets(parsed) == ["Yes"]


def test_tags_are_collected_and_headings_are_not_tags(graph):
    parsed = graph.parse_note(
        "# Heading Not A Tag\n"
        "body #alpha and #nested/child and #with-dash_1\n"
        "not a tag: # spaced, or #1234, or a#b\n"
    )
    assert parsed["tags"] == ["alpha", "nested/child", "with-dash_1"]


def test_headings_are_extracted_with_levels(graph):
    parsed = graph.parse_note(
        "# One\n"
        "## Two\n"
        "```\n"
        "# Fenced heading\n"
        "```\n"
        "###### Six\n"
        "####### Seven is not a heading\n"
    )
    assert parsed["headings"] == [
        {"level": 1, "text": "One"},
        {"level": 2, "text": "Two"},
        {"level": 6, "text": "Six"},
    ]


def test_title_prefers_frontmatter_then_first_h1(graph):
    fm = graph.parse_note("---\ntitle: From Frontmatter\n---\n# Ignored\n")
    assert fm["title"] == "From Frontmatter"
    h1 = graph.parse_note("intro\n# The Heading\n")
    assert h1["title"] == "The Heading"
    assert graph.parse_note("just text\n")["title"] is None


def test_frontmatter_tags_join_body_tags_and_frontmatter_is_not_scanned(graph):
    parsed = graph.parse_note(
        "---\n"
        "title: T\n"
        "tags: [one, two/three]\n"
        "aliases: not-a-link [[NotALink]]\n"
        "---\n"
        "body #four\n"
    )
    assert parsed["tags"] == ["four", "one", "two/three"]
    assert _targets(parsed) == []


def test_frontmatter_tags_accept_a_comma_string_and_a_yaml_list(graph):
    assert graph.parse_note("---\ntags: a, b\n---\n")["tags"] == ["a", "b"]
    assert graph.parse_note("---\ntags:\n  - a\n  - b\n---\n")["tags"] == ["a", "b"]


# --------------------------------------------------------------- resolution


NOTES = [
    "index.md",
    "docs/Design.md",
    "docs/deep/Design.md",
    "docs/Unique.md",
    "notes/Daily.md",
]


def test_resolution_matches_an_exact_relative_path_with_or_without_extension(graph):
    assert graph.resolve_link("docs/Design.md", "index.md", NOTES) == "docs/Design.md"
    assert graph.resolve_link("docs/Design", "index.md", NOTES) == "docs/Design.md"


def test_resolution_prefers_a_sibling_over_the_root(graph):
    # "Design" from docs/deep/ is the sibling, not docs/Design.md.
    assert graph.resolve_link("Design", "docs/deep/Other.md", NOTES) == "docs/deep/Design.md"


def test_resolution_falls_back_to_a_path_suffix(graph):
    assert graph.resolve_link("deep/Design", "index.md", NOTES) == "docs/deep/Design.md"


def test_resolution_matches_a_unique_basename_from_anywhere(graph):
    assert graph.resolve_link("Unique", "notes/Daily.md", NOTES) == "docs/Unique.md"
    assert graph.resolve_link("Daily", "index.md", NOTES) == "notes/Daily.md"


def test_an_ambiguous_basename_stays_unresolved(graph):
    # Two notes share the basename and the link carries no path to choose with,
    # so it is a ghost rather than a guess (MD-4).
    assert graph.resolve_link("Design", "index.md", NOTES) is None


def test_resolution_is_case_insensitive_and_tolerates_leading_dot_slash(graph):
    assert graph.resolve_link("dOcS/uNiQuE", "index.md", NOTES) == "docs/Unique.md"
    assert graph.resolve_link("./notes/Daily.md", "index.md", NOTES) == "notes/Daily.md"


def test_an_unknown_target_resolves_to_nothing(graph):
    assert graph.resolve_link("Nope", "index.md", NOTES) is None
    assert graph.resolve_link("", "index.md", NOTES) is None


# ---------------------------------------------------------------- the scan


def _vault(tmp_path, files):
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return str(tmp_path)


def test_scan_collects_notes_and_assets_and_skips_noise(graph, tmp_path):
    root = _vault(tmp_path, {
        "a.md": "[[b]]\n",
        "sub/b.markdown": "# B\n",
        "sub/pic.png": "not markdown",
        ".hidden/c.md": "hidden\n",
        "node_modules/d.md": "vendored\n",
        ".git/e.md": "gitdir\n",
    })
    scan = graph.scan_root(root)
    assert sorted(scan["notes"]) == ["a.md", "sub/b.markdown"]
    assert scan["assets"] == ["sub/pic.png"]


def test_scan_skips_a_file_too_large_to_be_a_note_and_reports_it(graph, tmp_path):
    root = _vault(tmp_path, {"small.md": "hi\n", "huge.md": "x" * 16})
    scan = graph.scan_root(root, max_bytes=8)
    assert list(scan["notes"]) == ["small.md"]
    assert scan["skipped_large"] == ["huge.md"]


def test_scan_stops_at_the_file_cap_and_says_so(graph, tmp_path):
    root = _vault(tmp_path, {f"n{i}.md": "x\n" for i in range(6)})
    scan = graph.scan_root(root, max_files=3)
    assert len(scan["notes"]) == 3
    assert scan["truncated"] is True
    assert graph.scan_root(root)["truncated"] is False


# --------------------------------------------------------------- the note API


def test_note_reports_outbound_links_backlinks_and_ghosts(graph, tmp_path):
    root = _vault(tmp_path, {
        "Hub.md": "# Hub\nlinks to [[Leaf]] and [[Missing]]\n",
        "Other.md": "points at [[Hub|the hub]]\n",
        "Leaf.md": "# Leaf\nand back to [[Hub]]\n",
    })
    out = graph.main(action="note", file=os.path.join(root, "Hub.md"), root=root)
    assert out["title"] == "Hub"
    assert [(link["target"], link["path"]) for link in out["links"]] == [
        ("Leaf", os.path.join(root, "Leaf.md")),
        ("Missing", None),
    ]
    assert [b["path"] for b in out["backlinks"]] == [
        os.path.join(root, "Leaf.md"),
        os.path.join(root, "Other.md"),
    ]
    assert out["backlinks"][1]["label"] == "the hub"


def test_note_falls_back_to_its_own_directory_when_no_marker_is_found(graph, tmp_path):
    root = _vault(tmp_path, {"sub/A.md": "[[B]]\n", "sub/B.md": "back to [[A]]\n"})
    out = graph.main(action="note", file=os.path.join(root, "sub", "A.md"))
    assert out["root"] == os.path.join(root, "sub")
    assert [b["path"] for b in out["backlinks"]] == [os.path.join(root, "sub", "B.md")]


def test_an_embed_resolves_against_the_assets_in_the_scan(graph, tmp_path):
    root = _vault(tmp_path, {"A.md": "![[pic.png]] ![[gone.png]]\n", "img/pic.png": "x"})
    out = graph.main(action="note", file=os.path.join(root, "A.md"), root=root)
    assert [(link["target"], link["path"]) for link in out["links"]] == [
        ("pic.png", os.path.join(root, "img", "pic.png")),
        ("gone.png", None),
    ]


def test_a_missing_file_is_an_error_not_an_empty_note(graph, tmp_path):
    out = graph.main(action="note", file=str(tmp_path / "nope.md"), root=str(tmp_path))
    assert out["error"] == "not_found"


# ------------------------------------------------------- the default scan root
#
# The note's own folder was the old default, and it was too narrow to be useful:
# every link leaving the folder rendered as a ghost and every inbound link from
# outside it was invisible (MD-12). The default now climbs to the nearest
# ancestor carrying a vault marker — and the climb itself has to be as cheap and
# as mount-safe as the folder gate (CT-12), which is what most of these pin.


@contextlib.contextmanager
def _no_enumeration():
    """Any directory enumeration inside the ascent is a test failure.

    The same discipline tests/test_graph_condition.py applies to the folder
    gate: a fixed set of `isdir`/`isfile` probes per level is constant-time
    however many entries a level holds, and a listing is not. Patched around the
    CALL, because pytest's own tmp_path machinery lists directories.
    """
    import glob as glob_mod

    def forbidden(*args, **kwargs):
        raise AssertionError("the ascent must never enumerate a directory")

    with mock.patch.object(os, "listdir", forbidden), \
            mock.patch.object(os, "scandir", forbidden), \
            mock.patch.object(os, "walk", forbidden), \
            mock.patch.object(glob_mod, "glob", forbidden), \
            mock.patch.object(glob_mod, "iglob", forbidden):
        yield


@pytest.mark.parametrize("marker", [".obsidian/config", ".fused-graph.json", ".git/HEAD"])
def test_the_default_root_climbs_to_the_nearest_vault_marker(graph, tmp_path, marker):
    root = _vault(tmp_path, {
        marker: "x\n",
        "docs/note.md": "see [../spec/overview.md](../spec/overview.md)\n",
        "spec/overview.md": "back to [[note]]\n",
    })
    with _no_enumeration():
        chosen = graph.vault_root(os.path.join(root, "docs"))
    assert chosen == root

    # And the point of it: the cross-folder link resolves and the inbound one
    # from a sibling folder shows up, neither of which the old default could do.
    out = graph.main(action="note", file=os.path.join(root, "docs", "note.md"))
    assert out["root"] == root
    assert [link["path"] for link in out["links"]] == [
        os.path.join(root, "spec", "overview.md")]
    assert [b["rel"] for b in out["backlinks"]] == ["spec/overview.md"]


def test_a_git_worktree_marker_is_a_file_not_a_directory(graph, tmp_path):
    # `.git` is a directory in a clone and a FILE in a worktree, and this repo is
    # checked out as one — probing only for a directory would miss it.
    root = _vault(tmp_path, {".git": "gitdir: /elsewhere\n", "docs/note.md": "x\n"})
    with _no_enumeration():
        assert graph.vault_root(os.path.join(root, "docs")) == root


def test_the_marker_can_be_several_levels_up(graph, tmp_path):
    root = _vault(tmp_path, {".obsidian/app.json": "{}", "a/b/c/d/note.md": "x\n"})
    with _no_enumeration():
        assert graph.vault_root(os.path.join(root, "a", "b", "c", "d")) == root


def test_the_climb_is_bounded_and_gives_up_rather_than_reaching_the_top(graph, tmp_path):
    # Nine levels below the marker: past the bound, so the note's own folder
    # wins. Never $HOME and never `/` — a runaway ascent would put a scan of
    # someone's whole home directory behind opening one note.
    deep = "/".join(["l%d" % n for n in range(9)])
    root = _vault(tmp_path, {".obsidian/app.json": "{}", deep + "/note.md": "x\n"})
    start = os.path.join(root, *deep.split("/"))
    with _no_enumeration():
        assert graph.vault_root(start) == start
    # One level shallower is inside the bound, which is what makes the bound the
    # thing being tested rather than the tree shape.
    assert graph.vault_root(os.path.dirname(start)) == root


def test_the_climb_stops_at_a_mount_boundary(graph, tmp_path, monkeypatch):
    """A remote mount must not become the scan root through the ascent (MD-11).

    The refusal in `_refuse_mounts` would catch it afterwards, but then opening a
    perfectly local note under a mounted folder would answer `mount_unsupported`
    instead of scanning the folder it is actually in.
    """
    root = _vault(tmp_path, {".obsidian/app.json": "{}", "docs/note.md": "x\n"})
    start = os.path.join(root, "docs")
    assert graph.vault_root(start) == root  # the control: same tree, no mount

    from fused_render.shell import mounts

    # Only the ancestor is mount-backed: `is_mount_backed` is prefix-based, so a
    # real mounts dir at `root` would make `start` mount-backed too and the test
    # would prove the wrong thing.
    monkeypatch.setattr(
        mounts, "is_mount_backed", lambda path: os.path.abspath(path) == root)
    with _no_enumeration():
        assert graph.vault_root(start) == start


def test_an_unavailable_mount_detector_does_not_climb(graph, tmp_path, monkeypatch):
    # "Cannot tell" reads as "do not ascend", the same way the gate and
    # `_refuse_mounts` read it as "refuse".
    import builtins

    root = _vault(tmp_path, {".obsidian/app.json": "{}", "docs/note.md": "x\n"})
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "fused_render.shell.mounts":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert graph.vault_root(os.path.join(root, "docs")) == os.path.join(root, "docs")


def test_an_explicit_root_still_wins_over_the_marker(graph, tmp_path):
    root = _vault(tmp_path, {".obsidian/app.json": "{}", "docs/note.md": "x\n"})
    out = graph.main(action="note", file=os.path.join(root, "docs", "note.md"),
                     root=os.path.join(root, "docs"))
    assert out["root"] == os.path.join(root, "docs")


def test_the_chosen_root_always_contains_the_note(graph, tmp_path, monkeypatch):
    # The invariant every `rel` depends on: a root that did not contain the file
    # would turn the note view into an `outside_root` error. Checked rather than
    # assumed, so a change to the ascent cannot break it silently.
    root = _vault(tmp_path, {"docs/note.md": "x\n"})
    monkeypatch.setattr(graph, "vault_root", lambda start: str(tmp_path / "elsewhere"))
    out = graph.main(action="note", file=os.path.join(root, "docs", "note.md"))
    assert out["error"] is None
    assert out["root"] == os.path.join(root, "docs")


def test_a_wider_root_still_reports_the_file_cap(graph, tmp_path, monkeypatch):
    # A wider default root makes MAX_FILES matter more, not less — so the notice
    # the sidebar renders has to keep coming back (MD-10).
    files = {".obsidian/app.json": "{}", "docs/note.md": "x\n"}
    files.update({"other/n%d.md" % n: "x\n" for n in range(6)})
    root = _vault(tmp_path, files)
    real = graph.scan_indexed
    monkeypatch.setattr(graph, "scan_indexed", lambda where: real(where, max_files=3))
    out = graph.main(action="note", file=os.path.join(root, "docs", "note.md"))
    assert out["root"] == root
    assert out["truncated"] is True
    assert out["notes"] == 3


def test_main_coerces_a_string_depth(graph, chain):
    """`depth` arrives from the template as `String(graphDepth())`, and
    `_neighbourhood` compares it with an int — `range(max(0, "2"))` raises."""
    root = chain
    focus = os.path.join(root, "A.md")
    out = graph.main(action="graph", file=focus, root=root, depth="2")
    assert out["error"] is None
    assert out["depth"] == 2
    assert out["nodes"] == graph.main(
        action="graph", file=focus, root=root, depth=2)["nodes"]
    # Nonsense falls back rather than throwing: this is a URL param.
    assert graph.main(action="graph", file=focus, root=root, depth="x")["depth"] == 1


# ------------------------------------------------------------ mount refusal


def test_a_mount_backed_root_is_refused_outright(graph, tmp_path, monkeypatch):
    """The recursive walk is exactly the shape that wedges an rclone NFS mount,
    so the graph refuses a mount-backed root instead of bounding the risk
    (MD-11/D156). A clear result, never a partial walk."""
    root = _vault(tmp_path, {"A.md": "[[B]]\n"})
    from fused_render.shell import mounts

    monkeypatch.setattr(mounts, "mounts_dir", lambda: root)
    out = graph.main(action="note", file=os.path.join(root, "A.md"), root=root)
    assert out["error"] == "mount_unsupported"
    # And no walk happened: scan_root itself refuses, so nothing can slip past
    # a caller that forgot to check.
    with pytest.raises(graph.MountUnsupported):
        graph.scan_root(root)


def test_a_local_root_is_not_refused_when_a_mounts_dir_merely_exists(
        graph, tmp_path, monkeypatch):
    root = _vault(tmp_path, {"A.md": "hi\n"})
    from fused_render.shell import mounts

    monkeypatch.setattr(mounts, "mounts_dir", lambda: str(tmp_path / "elsewhere"))
    assert graph.main(action="note", file=os.path.join(root, "A.md"), root=root)["error"] is None


# ------------------------------------------------------- autocomplete candidates


def test_candidates_lists_notes_headings_tags_and_assets(graph, tmp_path):
    root = _vault(tmp_path, {
        "Hub.md": "---\ntags: [proj]\n---\n# Hub\n## Details\n[[Leaf]] #inline\n",
        "sub/Leaf.md": "# Leaf\n",
        "sub/pic.png": "x",
    })
    out = graph.main(action="candidates", root=root)
    assert out["error"] is None
    assert [n["rel"] for n in out["notes"]] == ["Hub.md", "sub/Leaf.md"]
    hub = out["notes"][0]
    assert hub["title"] == "Hub"
    assert [h["text"] for h in hub["headings"]] == ["Hub", "Details"]
    assert out["tags"] == ["inline", "proj"]
    assert out["assets"] == ["sub/pic.png"]


def test_a_candidates_link_form_is_the_shortest_unambiguous_one(graph, tmp_path):
    # A unique basename inserts as the basename; a shared one must carry enough
    # path to resolve, or the inserted link would be a ghost (MD-4/MD-14).
    root = _vault(tmp_path, {
        "Unique.md": "x\n",
        "a/Shared.md": "x\n",
        "b/Shared.md": "x\n",
    })
    forms = {n["rel"]: n["link"] for n in graph.main(action="candidates", root=root)["notes"]}
    assert forms == {
        "Unique.md": "Unique",
        "a/Shared.md": "a/Shared",
        "b/Shared.md": "b/Shared",
    }


def test_every_candidate_link_form_resolves_back_to_its_own_note(graph, tmp_path):
    # The property that matters: what the popup inserts is what resolution
    # finds. Asserted rather than reasoned about, because the two rules are
    # separate code.
    root = _vault(tmp_path, {
        "Top.md": "x\n", "a/Same.md": "x\n", "b/Same.md": "x\n", "a/deep/Leaf.md": "x\n",
    })
    out = graph.main(action="candidates", root=root)
    paths = [n["rel"] for n in out["notes"]]
    for note in out["notes"]:
        assert graph.resolve_link(note["link"], "Top.md", paths) == note["rel"], note["link"]


def test_candidates_refuses_a_mount_backed_root(graph, tmp_path, monkeypatch):
    root = _vault(tmp_path, {"A.md": "x\n"})
    from fused_render.shell import mounts

    monkeypatch.setattr(mounts, "mounts_dir", lambda: root)
    assert graph.main(action="candidates", root=root)["error"] == "mount_unsupported"


# --------------------------------------------------------------- the index


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the index at a tmp home, the way every other home-backed test does.

    Also asserts the thing the fixture exists for: the index resolves against
    `home_dir()` on each call, so FUSED_RENDER_HOME overrides work (MD-7).
    """
    h = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return str(h)


def test_the_index_lives_under_home_dir_keyed_by_the_real_root(graph, tmp_path, home):
    root = _vault(tmp_path / "vault", {"A.md": "x\n"})
    path = graph.index_path(root)
    assert path.startswith(os.path.join(home, "graph") + os.sep)
    assert path.endswith(".sqlite")
    # A symlink to the same folder is the same index — the key is realpath.
    link = str(tmp_path / "alias")
    try:
        os.symlink(root, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert graph.index_path(link) == path


def test_a_warm_scan_reparses_nothing(graph, tmp_path, home, monkeypatch):
    root = _vault(tmp_path, {"A.md": "[[B]]\n", "B.md": "# B\n"})
    first = graph.scan_indexed(root)
    assert sorted(first["notes"]) == ["A.md", "B.md"]

    reads = []
    real = graph._read_text
    monkeypatch.setattr(graph, "_read_text", lambda p: (reads.append(p), real(p))[1])
    warm = graph.scan_indexed(root)
    assert reads == []
    assert warm["notes"] == first["notes"]


def test_a_changed_note_is_the_only_one_reparsed(graph, tmp_path, home, monkeypatch):
    root = _vault(tmp_path, {"A.md": "[[B]]\n", "B.md": "# B\n"})
    graph.scan_indexed(root)
    changed = tmp_path / "A.md"
    changed.write_text("[[B]] and [[C]]\n", encoding="utf-8")
    os.utime(changed, (0, 0))  # a different mtime_ns is the whole invalidation test

    reads = []
    real = graph._read_text
    monkeypatch.setattr(graph, "_read_text", lambda p: (reads.append(p), real(p))[1])
    warm = graph.scan_indexed(root)
    assert reads == [str(changed)]
    assert [link["target"] for link in warm["notes"]["A.md"]["links"]] == ["B", "C"]


def test_a_deleted_note_leaves_the_assembly_and_the_index(graph, tmp_path, home):
    root = _vault(tmp_path, {"A.md": "x\n", "B.md": "x\n"})
    graph.scan_indexed(root)
    os.remove(tmp_path / "B.md")
    assert sorted(graph.scan_indexed(root)["notes"]) == ["A.md"]
    assert sorted(graph.index_rows(root)) == ["A.md"]


def test_bumping_the_parser_version_invalidates_every_row(graph, tmp_path, home, monkeypatch):
    root = _vault(tmp_path, {"A.md": "x\n"})
    graph.scan_indexed(root)
    monkeypatch.setattr(graph, "PARSER_VERSION", graph.PARSER_VERSION + 1)

    reads = []
    real = graph._read_text
    monkeypatch.setattr(graph, "_read_text", lambda p: (reads.append(p), real(p))[1])
    graph.scan_indexed(root)
    assert reads == [str(tmp_path / "A.md")]


def test_the_index_stores_its_root_so_a_wrong_db_is_detectable(graph, tmp_path, home):
    root = _vault(tmp_path / "one", {"A.md": "x\n"})
    graph.scan_indexed(root)
    assert graph.index_meta(root)["root"] == os.path.realpath(root)

    # Simulate a moved folder / hash collision: the same db file, a different
    # root. The rows must be discarded rather than attributed to this vault.
    other = _vault(tmp_path / "two", {"Z.md": "x\n"})
    import shutil

    shutil.copyfile(graph.index_path(root), graph.index_path(other))
    assert sorted(graph.scan_indexed(other)["notes"]) == ["Z.md"]
    assert graph.index_meta(other)["root"] == os.path.realpath(other)


def test_a_corrupt_index_falls_back_to_a_full_walk(graph, tmp_path, home):
    root = _vault(tmp_path, {"A.md": "# A\n"})
    graph.scan_indexed(root)
    with open(graph.index_path(root), "wb") as handle:
        handle.write(b"not a database")
    # The index is a cache: an unusable one costs a walk, never a failure.
    assert list(graph.scan_indexed(root)["notes"]) == ["A.md"]


def test_the_index_is_never_touched_for_a_mount_backed_root(graph, tmp_path, home, monkeypatch):
    root = _vault(tmp_path, {"A.md": "x\n"})
    from fused_render.shell import mounts

    monkeypatch.setattr(mounts, "mounts_dir", lambda: root)
    with pytest.raises(graph.MountUnsupported):
        graph.scan_indexed(root)
    assert not os.path.exists(os.path.join(home, "graph"))


# ---------------------------------------------------------- graph assembly


@pytest.fixture()
def chain(tmp_path, home):
    """A.md -> B.md -> C.md, plus a ghost and a tag hanging off A."""
    return _vault(tmp_path, {
        "A.md": "# A\n[[B]] and [[Nope]] #alpha\n",
        "B.md": "# B\n[[C]]\n",
        "C.md": "# C\n",
    })


def _ids(payload):
    return sorted(node["id"] for node in payload["nodes"])


def test_the_whole_vault_graph_carries_notes_ghosts_and_tags(graph, chain):
    out = graph.main(action="graph", root=chain)
    assert out["error"] is None
    assert _ids(out) == ["A.md", "B.md", "C.md", "ghost:nope", "tag:alpha"]
    kinds = {node["id"]: node["kind"] for node in out["nodes"]}
    assert kinds["ghost:nope"] == "ghost"
    assert kinds["tag:alpha"] == "tag"
    assert sorted((e["source"], e["target"]) for e in out["edges"]) == [
        ("A.md", "B.md"), ("A.md", "ghost:nope"), ("A.md", "tag:alpha"), ("B.md", "C.md")]


def test_node_degree_counts_both_directions(graph, chain):
    degrees = {n["id"]: n["degree"] for n in graph.main(action="graph", root=chain)["nodes"]}
    assert degrees["A.md"] == 3   # B, the ghost, the tag
    assert degrees["B.md"] == 2   # A and C
    assert degrees["C.md"] == 1


def test_a_repeated_link_is_one_edge(graph, tmp_path, home):
    root = _vault(tmp_path, {"A.md": "[[B]] then [[B]] again\n", "B.md": "x\n"})
    assert len(graph.main(action="graph", root=root)["edges"]) == 1


def test_the_local_graph_is_bounded_by_depth(graph, chain):
    focus = os.path.join(chain, "A.md")
    d0 = graph.main(action="graph", root=chain, file=focus, depth=0)
    assert _ids(d0) == ["A.md"]
    d1 = graph.main(action="graph", root=chain, file=focus, depth=1)
    assert _ids(d1) == ["A.md", "B.md", "ghost:nope", "tag:alpha"]
    d2 = graph.main(action="graph", root=chain, file=focus, depth=2)
    assert _ids(d2) == ["A.md", "B.md", "C.md", "ghost:nope", "tag:alpha"]
    assert d1["focus"] == "A.md" and d1["depth"] == 1


def test_the_local_graph_only_keeps_edges_between_kept_nodes(graph, chain):
    out = graph.main(action="graph", root=chain, file=os.path.join(chain, "A.md"), depth=1)
    kept = _ids(out)
    for edge in out["edges"]:
        assert edge["source"] in kept and edge["target"] in kept


def test_a_link_to_a_directory_is_not_a_ghost_note(graph, tmp_path, home):
    """A ghost is a promise: click it and that note appears. So a target that can
    never BE a note must not become one.

    Reported: `[`../examples/`](../examples/)` produced a ghost labelled
    `../examples/`, and clicking it tried to create a file called `.md` one level
    above the vault root. Judged on the target string — a trailing slash names a
    directory — with no stat and no listing, because this runs per link per note.
    """
    root = _vault(tmp_path, {
        ".obsidian/app.json": "{}",
        "docs/note.md": "[dir](../examples/) and [gone](../examples/Nope.md)\n",
    })
    out = graph.main(action="graph", file=os.path.join(root, "docs", "note.md"), root=root)
    ghosts = [n["label"] for n in out["nodes"] if n["kind"] == "ghost"]
    assert ghosts == ["../examples/Nope.md"], ghosts
    # And no edge left dangling at the node that was not created.
    assert all(edge["target"] in {n["id"] for n in out["nodes"]} for edge in out["edges"])


def test_a_link_to_a_non_note_file_is_not_a_ghost_note(graph, tmp_path, home):
    # `../scripts/run.py` can only ever be a file that exists or does not; it can
    # never be a note, so "click to create" would be a lie. A version-like target
    # keeps its ghost — the suffix has to start with a letter to count.
    root = _vault(tmp_path, {
        "note.md": "[s](./scripts/run.py) and [[Chapter 1.2]] and [[Plain]]\n",
    })
    out = graph.main(action="graph", file=os.path.join(root, "note.md"), root=root)
    ghosts = sorted(n["label"] for n in out["nodes"] if n["kind"] == "ghost")
    assert ghosts == ["Chapter 1.2", "Plain"], ghosts


def test_a_ghost_node_carries_the_target_not_just_a_label(graph, tmp_path, home):
    # The label is a DISPLAY string (a real note's is its title), so the create
    # path must not be driven by it.
    root = _vault(tmp_path, {"docs/note.md": "[gone](../examples/Nope.md)\n"})
    out = graph.main(action="graph", file=os.path.join(root, "docs", "note.md"), root=root)
    ghost = [n for n in out["nodes"] if n["kind"] == "ghost"][0]
    assert ghost["target"] == "../examples/Nope.md"
    assert all("target" not in n for n in out["nodes"] if n["kind"] == "note")


def test_an_embedded_asset_is_not_a_graph_node(graph, tmp_path, home):
    # A picture is not a note; it would otherwise dominate a vault of screenshots.
    root = _vault(tmp_path, {"A.md": "![[pic.png]]\n", "pic.png": "x"})
    assert _ids(graph.main(action="graph", root=root)) == ["A.md"]


def test_the_graph_refuses_a_mount_backed_root(graph, tmp_path, home, monkeypatch):
    root = _vault(tmp_path, {"A.md": "x\n"})
    from fused_render.shell import mounts

    monkeypatch.setattr(mounts, "mounts_dir", lambda: root)
    out = graph.main(action="graph", root=root)
    assert out["error"] == "mount_unsupported"
    assert "remote mounts" in out["message"]


def test_an_unwritable_index_home_still_answers_from_a_plain_walk(graph, tmp_path, home):
    """A cache that cannot be opened costs a walk, never an error.

    Regression: `conn` was bound only after `sqlite3.connect` succeeded, so a
    failing `os.makedirs`/`connect` made the except branch's `conn.close()`
    raise NameError — out of a cache helper, into the caller's run.
    """
    root = _vault(tmp_path, {"A.md": "# A\n[[B]]\n", "B.md": "# B\n"})
    # A FILE where the graph directory belongs: makedirs raises, and so does the
    # discard-and-retry unlink.
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "graph"), "w", encoding="utf-8") as handle:
        handle.write("not a directory")
    assert graph._connect(root) is None
    assert sorted(graph.scan_indexed(root)["notes"]) == ["A.md", "B.md"]
    out = graph.main(action="note", file=os.path.join(root, "A.md"), root=root)
    assert out["error"] is None
    assert [b["rel"] for b in out["backlinks"]] == []

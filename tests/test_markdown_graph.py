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
import importlib.util
import os

import pytest

GRAPH = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "markdown", "graph.py"
)


@pytest.fixture(scope="module")
def graph():
    spec = importlib.util.spec_from_file_location("markdown_graph", GRAPH)
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


def test_note_defaults_its_root_to_the_files_own_directory(graph, tmp_path):
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

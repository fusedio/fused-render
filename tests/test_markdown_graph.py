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

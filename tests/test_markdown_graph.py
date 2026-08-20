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
import inspect
import os
import sys
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


@pytest.fixture()
def appenv(graph):
    """`templates/shared/appenv.py`, the module graph.py asks about mounts.

    Importing `graph` puts `../shared` on sys.path, so this is the SAME module
    object the template's lazy `from appenv import is_mount_backed` resolves to —
    patching an attribute here is patching what the template sees. Most mount
    tests below drive `FUSED_RENDER_MOUNTS_DIR` instead, which is the real
    contract; this exists for the one case that needs a detector saying "this
    exact path and nothing under it"."""
    import appenv as mod

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


def test_indented_code_block_links_are_not_edges(graph):
    parsed = graph.parse_note("text\n\n    [[Indented]]\n\nreal [[Yes]]\n")
    assert _targets(parsed) == ["Yes"]


def test_a_hashtag_is_not_parsed_at_all(graph):
    """The tag concept is gone (D165): a `#word` is prose, and nothing about it
    reaches a payload. A leading `#` is still a heading."""
    parsed = graph.parse_note("# Heading\nbody #alpha and #nested/child\n")
    assert "tags" not in parsed
    assert [h["text"] for h in parsed["headings"]] == ["Heading"]


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


def test_parse_note_emits_no_title_at_all(graph):
    # A note is named by its FILE, which parse_note cannot see — so it does not
    # guess. Frontmatter `title:` and a leading `# H1` are both inert here; the
    # H1 survives only as a heading.
    parsed = graph.parse_note("---\ntitle: From Frontmatter\n---\n# The Heading\n")
    assert "title" not in parsed
    assert [h["text"] for h in parsed["headings"]] == ["The Heading"]


def test_display_name_is_the_filename_not_the_frontmatter_title_or_h1(graph, tmp_path):
    root = _vault(tmp_path, {
        "Real Name.md": "---\ntitle: Frontmatter Name\n---\n# H1 Name\n",
        "Other.md": "see [[Real Name]]\n",
    })
    note = graph.main(action="note", file=os.path.join(root, "Real Name.md"), root=root)
    assert note["title"] == "Real Name"
    # And every surface that names the note agrees — the link row pointing at it,
    # its backlink row, the graph node label, and the `[[` popup.
    assert graph.main(action="note", file=os.path.join(root, "Other.md"),
                      root=root)["links"][0]["title"] == "Real Name"
    assert note["backlinks"][0]["title"] == "Other"
    graph_out = graph.main(action="graph", root=root, depth=0)
    assert sorted(n["label"] for n in graph_out["nodes"]) == ["Other", "Real Name"]
    candidates = graph.main(action="candidates", root=root)
    assert sorted(n["title"] for n in candidates["notes"]) == ["Other", "Real Name"]


def test_frontmatter_is_not_scanned_for_links(graph):
    """No frontmatter *value* is read any more (D165 took `tags` with it), and
    the block is still masked out, so a `[[…]]` inside it is not an edge."""
    parsed = graph.parse_note(
        "---\n"
        "title: T\n"
        "tags: [one, two/three]\n"
        "aliases: not-a-link [[NotALink]]\n"
        "---\n"
        "body [[Real]]\n"
    )
    assert _targets(parsed) == ["Real"]


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


def test_the_resolver_documents_exactly_the_fallbacks_it_has(graph):
    """The docstring promised a fourth step that could never run.

    "then as a bare basename" was guarded by `if "/" in lowered: return None`,
    so by the time it was reached `lowered` had no slash and
    `rsplit("/", 1)[-1]` was `lowered` itself — a repeat of the root lookup that
    had already failed. Always None. It was also redundant: a bare basename is
    already found by the root lookup when the note is top-level and by the
    suffix step when it is not (the test above), so the branch was deleted
    rather than made live, which would have CHANGED resolution.
    """
    doc = " ".join((graph.resolve_link.__doc__ or "").split())
    assert "then from the vault root, then as a path suffix." in doc
    assert "Three steps, not four" in doc
    # And the branch itself is gone rather than merely unreachable.
    assert 'rsplit("/", 1)[-1]' not in inspect.getsource(graph.resolve_link)
    # The behaviour that step pretended to add, still working through the ones
    # that are real.
    assert graph.resolve_link("Unique", "index.md", NOTES) == "docs/Unique.md"


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


def _client(path):
    """The forward-slash form graph.py's client-facing payload fields carry
    (`note["root"]`, every link/backlink `path` — see graph.py's
    `_client_path`/`_client_join`, and `test_every_path_a_payload_carries_
    uses_forward_slashes` above). An expected value built with `os.path.join`
    or a raw `tmp_path` string is native-separated, which is backslashed on
    Windows and would never match those fields there — this converts it the
    same way graph.py does, rather than the test predicting a native path a
    forward-slash-only payload was never going to return."""
    return path.replace("\\", "/") if path else path


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


def test_a_large_note_is_scanned_and_fully_linkable(graph, tmp_path):
    # There is no size cap. A big note used to be dropped from the scan into a
    # `skipped_large` list, which put it in NEITHER index — so a link aimed at
    # it drew a ghost, indistinguishable from a link to a note that does not
    # exist. It is an ordinary note now, whatever its size.
    big = "# Big\n" + ("filler paragraph text\n" * 20_000)  # ~440 KB
    root = _vault(tmp_path, {"Big.md": big, "Other.md": "see [[Big]]\n"})
    scan = graph.scan_root(root)
    assert sorted(scan["notes"]) == ["Big.md", "Other.md"]
    assert scan["truncated"] is False
    assert "skipped_large" not in scan

    # The link resolves to the real file rather than a ghost, and the big note
    # has working backlinks of its own.
    other = graph.main(action="note", file=os.path.join(root, "Other.md"), root=root)
    assert other["links"][0]["path"] == _client(os.path.join(root, "Big.md"))
    big_note = graph.main(action="note", file=os.path.join(root, "Big.md"), root=root)
    assert [b["rel"] for b in big_note["backlinks"]] == ["Other.md"]
    # It is a real node in the graph, not a ghost.
    nodes = {n["id"]: n for n in graph.main(action="graph", root=root, depth=0)["nodes"]}
    assert nodes["Big.md"]["kind"] == "note"


def test_scan_stops_at_the_file_cap_and_says_so(graph, tmp_path):
    root = _vault(tmp_path, {f"n{i}.md": "x\n" for i in range(6)})
    scan = graph.scan_root(root, max_files=3)
    assert len(scan["notes"]) == 3
    assert scan["truncated"] is True
    assert graph.scan_root(root)["truncated"] is False


@contextlib.contextmanager
def _counting_scandir(counter):
    """Count every directory entry the walk actually pulls off the disk.

    `os.walk` drives `os.scandir`, so wrapping it measures the real work rather
    than what came back in the payload — which is the whole point: a cap that
    bounds only what is RECORDED leaves the walk enumerating the rest of the
    tree for nothing.
    """
    real = os.scandir

    class Counting:  # scandir's iterator is also a context manager
        def __init__(self, it):
            self._it = it

        def __enter__(self):
            self._it.__enter__()
            return self

        def __exit__(self, *exc):
            return self._it.__exit__(*exc)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self._it)
            counter.append(entry.name)
            return entry

        def close(self):
            self._it.close()

    with mock.patch.object(os, "scandir",
                           lambda path=".", *a, **k: Counting(real(path, *a, **k))):
        yield


def test_the_walk_stops_enumerating_once_its_entry_budget_is_spent(graph, tmp_path):
    """The budget has to bound what is WALKED, not just what is recorded.

    MAX_FILES only ever counted notes, so a tree of generated files beside one
    note was `readdir`'d in full — cost O(entire tree) — and still came back
    `truncated=False`. This walk runs on every `.md` open and a warm open is
    stat-only, so it IS the steady-state cost of opening a note: if the bound on
    entries visited regresses, opening one note in a monorepo pays for the whole
    monorepo, silently.
    """
    files = {"note.md": "[[note]]\n"}
    files.update({"gen/d%02d/f%02d.js" % (d, f): "x"
                  for d in range(40) for f in range(30)})
    root = _vault(tmp_path, files)  # 1200 generated files, 40 directories

    seen = []
    with _counting_scandir(seen):
        scan = graph.scan_root(root, max_entries=100)
    assert scan["truncated"] is True
    # A directory's listing is read whole (sorted order is the determinism
    # guarantee), so the bound is per-directory-coarse, not exact — but it must
    # be a small multiple of the budget rather than the size of the tree.
    assert len(seen) < 3 * 100, len(seen)

    # The control: the same tree under the real budget is scanned in full, so
    # what the test pins is the budget and not the tree's shape.
    seen_full = []
    with _counting_scandir(seen_full):
        full = graph.scan_root(root)
    assert full["truncated"] is False
    assert len(seen_full) > 1200
    assert len(full["assets"]) == 1200


def test_the_entry_budget_bounds_a_tree_of_directories_with_no_files(graph, tmp_path):
    """Directories cost a listing each, whether or not they hold any files.

    The budget counted them (`entries += len(dirnames)`) but only TESTED itself
    inside the per-file loop, so a subtree of empty directories never reached
    the test: 30,300 of them walked in full, in 573 ms, and still reported
    `truncated=False`. Silently walking past a cap is the specific thing MD-10
    forbids — and the shape that missed it is exactly this one, which is why the
    file-heavy test above is not enough on its own.
    """
    root = _vault(tmp_path, {"note.md": "# n\n"})
    for outer in range(30):
        for inner in range(30):
            os.makedirs(os.path.join(root, "gen", "d%02d" % outer, "e%02d" % inner))

    seen = []
    with _counting_scandir(seen):
        scan = graph.scan_root(root, max_entries=100)
    assert scan["truncated"] is True
    # Per-directory-coarse, as above: a small multiple of the budget, not the
    # size of the tree (931 directories here).
    assert len(seen) < 3 * 100, len(seen)

    # The control, again: under the real budget this tree is walked whole, so
    # what the test pins is the budget rather than the tree.
    full = graph.scan_root(root)
    assert full["truncated"] is False
    assert len(full["notes"]) == 1


def test_a_dropped_asset_is_reported_as_truncation_too(graph, tmp_path):
    """Hitting the asset cap used to leave `truncated` False.

    Assets past MAX_ASSETS were dropped on the floor while the walk carried on
    recording nothing, so by the walk's own accounting nothing had been lost and
    the "partial" notice both views render off `truncated` never fired. Silent
    truncation is exactly what MD-10 forbids.
    """
    files = {"note.md": "![[f00.js]]\n"}
    files.update({"gen/f%02d.js" % n: "x" for n in range(40)})
    root = _vault(tmp_path, files)
    scan = graph.scan_root(root, max_assets=2)
    assert len(scan["assets"]) == 2
    assert scan["truncated"] is True


@pytest.mark.parametrize("generated", [
    "build", "dist", "out", "target", "_build", "_site", "vendor",
    "bower_components", "Pods", "coverage", "htmlcov",
])
def test_a_generated_tree_is_never_indexed_as_a_note(graph, tmp_path, generated):
    """Markdown under a build/vendor output directory is not the author's note.

    Indexed, it becomes a real graph node — a vendored README drawn as though
    you wrote it — and its whole subtree is walked on every open. `.gitignore` is
    deliberately NOT consulted (a vault often lives outside a repo, where
    gitignore silently does nothing), so this name list is the entire defence
    and has to be pinned.
    """
    root = _vault(tmp_path, {"note.md": "x\n", "%s/gen.md" % generated: "generated\n"})
    scan = graph.scan_root(root)
    assert list(scan["notes"]) == ["note.md"]
    assert scan["assets"] == []


@pytest.mark.parametrize("authored", [
    "docs", "notes", "content", "site", "public", "static", "assets", "src",
    "journal", "archive", "output",
])
def test_a_plausibly_authored_directory_is_still_scanned(graph, tmp_path, authored):
    """The skip list must stay conservative.

    Every name on it is invisible with no notice at all — a folder of real notes
    silently missing from the graph is worse than a vendored one showing up. Any
    name a person might plausibly keep their own writing in stays off the list.
    """
    root = _vault(tmp_path, {"%s/mine.md" % authored: "mine\n"})
    assert list(graph.scan_root(root)["notes"]) == ["%s/mine.md" % authored]


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
        ("Leaf", _client(os.path.join(root, "Leaf.md"))),
        ("Missing", None),
    ]
    assert [b["path"] for b in out["backlinks"]] == [
        _client(os.path.join(root, "Leaf.md")),
        _client(os.path.join(root, "Other.md")),
    ]
    assert out["backlinks"][1]["label"] == "the hub"


def test_note_falls_back_to_its_own_directory_when_no_marker_is_found(graph, tmp_path):
    root = _vault(tmp_path, {"sub/A.md": "[[B]]\n", "sub/B.md": "back to [[A]]\n"})
    out = graph.main(action="note", file=os.path.join(root, "sub", "A.md"))
    assert out["root"] == _client(os.path.join(root, "sub"))
    assert [b["path"] for b in out["backlinks"]] == [
        _client(os.path.join(root, "sub", "B.md"))
    ]


def test_an_embed_resolves_against_the_assets_in_the_scan(graph, tmp_path):
    root = _vault(tmp_path, {"A.md": "![[pic.png]] ![[gone.png]]\n", "img/pic.png": "x"})
    out = graph.main(action="note", file=os.path.join(root, "A.md"), root=root)
    assert [(link["target"], link["path"]) for link in out["links"]] == [
        ("pic.png", _client(os.path.join(root, "img", "pic.png"))),
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


@pytest.mark.parametrize("marker", [".obsidian/config", ".git/HEAD"])
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
    assert out["root"] == _client(root)
    assert [link["path"] for link in out["links"]] == [
        _client(os.path.join(root, "spec", "overview.md"))]
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


def test_the_climb_stops_at_a_mount_boundary(graph, appenv, tmp_path, monkeypatch):
    """A remote mount must not become the scan root through the ascent (MD-11).

    The refusal in `_refuse_mounts` would catch it afterwards, but then opening a
    perfectly local note under a mounted folder would answer `mount_unsupported`
    instead of scanning the folder it is actually in.
    """
    root = _vault(tmp_path, {".obsidian/app.json": "{}", "docs/note.md": "x\n"})
    start = os.path.join(root, "docs")
    assert graph.vault_root(start) == root  # the control: same tree, no mount

    # Only the ancestor is mount-backed: `is_mount_backed` is prefix-based, so a
    # real mounts dir at `root` would make `start` mount-backed too and the test
    # would prove the wrong thing.
    monkeypatch.setattr(
        appenv, "is_mount_backed", lambda path: os.path.abspath(path) == root)
    with _no_enumeration():
        assert graph.vault_root(start) == start


def test_a_note_on_a_mount_is_never_probed_at_all(graph, tmp_path, monkeypatch):
    """The ascent must not touch a mount-backed path even ONCE.

    `test_the_climb_stops_at_a_mount_boundary` covers the note that merely lives
    under a mounted ancestor. This covers the note that is itself on the mount,
    where the bound alone would not save us: 8 levels of `isdir`/`isfile` against
    a remote is 8 levels too many. Each probe is a kernel GETATTR on a live NFS
    mount, and this repo has already wedged one that way — so this asserts the
    absence of the syscall, not merely that the answer came out right.

    Every filesystem primitive the probes could reach is counted, because
    `_has_vault_marker` is free to change which one it calls; what may not change
    is that none of them see the mount.
    """
    root = _vault(tmp_path, {".obsidian/app.json": "{}", "docs/note.md": "x\n"})
    start = os.path.join(root, "docs")
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", root)

    touched = []
    real = {name: getattr(os.path, name) for name in ("isdir", "isfile", "exists")}

    def watched(name):
        def probe(path, *args, **kwargs):
            if str(path).startswith(root):
                touched.append((name, str(path)))
            return real[name](path, *args, **kwargs)
        return probe

    with mock.patch.object(os.path, "isdir", watched("isdir")), \
            mock.patch.object(os.path, "isfile", watched("isfile")), \
            mock.patch.object(os.path, "exists", watched("exists")), \
            _no_enumeration():
        # `start` is its own root, so nothing above it is consulted...
        assert graph.vault_root(start) == start
    assert touched == [], touched

    # ...and the refusal is what the user actually sees, so the walk never runs
    # even though the ascent handed back a mount-backed path.
    out = graph.main(action="note", file=os.path.join(start, "note.md"))
    assert out["error"] == "mount_unsupported"


@contextlib.contextmanager
def _no_appenv():
    """Make `from appenv import ...` fail, i.e. mount detection unavailable.

    This is a real shape, not a hypothetical: a copy of the template folder taken
    without its `../shared/` sibling has no appenv at all. It used to be the
    EVERYDAY shape under the fused engine (PYTHONPATH stripped => the old
    `from fused_render.shell.mounts import ...` always failed), so the fail-closed
    branch has to keep working now that it is the rare one."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "appenv":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    saved = sys.modules.pop("appenv", None)
    builtins.__import__ = blocked
    try:
        yield
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["appenv"] = saved


def test_an_unavailable_mount_detector_does_not_climb(graph, tmp_path):
    # "Cannot tell" reads as "do not ascend", the same way the gate and
    # `_refuse_mounts` read it as "refuse".
    root = _vault(tmp_path, {".obsidian/app.json": "{}", "docs/note.md": "x\n"})
    with _no_appenv():
        assert graph.vault_root(os.path.join(root, "docs")) == os.path.join(root, "docs")


def test_an_unavailable_mount_detector_refuses_the_walk(graph, tmp_path):
    """The other half of MD-11's fail-closed rule: no detector => no walk.

    `vault_root` merely declining to climb is not enough — `scan_root` is what
    would actually `readdir` a remote, so it must refuse outright, and `main`
    must surface that as `mount_unsupported` rather than a partial graph."""
    root = _vault(tmp_path, {"A.md": "[[B]]\n"})
    with _no_appenv():
        with pytest.raises(graph.MountUnsupported):
            graph.scan_root(root)
        out = graph.main(action="note", file=os.path.join(root, "A.md"), root=root)
        assert out["error"] == "mount_unsupported"
        assert graph.main(action="candidates", root=root)["error"] == "mount_unsupported"


def test_an_explicit_root_still_wins_over_the_marker(graph, tmp_path):
    root = _vault(tmp_path, {".obsidian/app.json": "{}", "docs/note.md": "x\n"})
    out = graph.main(action="note", file=os.path.join(root, "docs", "note.md"),
                     root=os.path.join(root, "docs"))
    assert out["root"] == _client(os.path.join(root, "docs"))


def test_the_chosen_root_always_contains_the_note(graph, tmp_path, monkeypatch):
    # The invariant every `rel` depends on: a root that did not contain the file
    # would turn the note view into an `outside_root` error. Checked rather than
    # assumed, so a change to the ascent cannot break it silently.
    root = _vault(tmp_path, {"docs/note.md": "x\n"})
    monkeypatch.setattr(graph, "vault_root", lambda start: str(tmp_path / "elsewhere"))
    out = graph.main(action="note", file=os.path.join(root, "docs", "note.md"))
    assert out["error"] is None
    assert out["root"] == _client(os.path.join(root, "docs"))


def test_a_wider_root_still_reports_the_file_cap(graph, tmp_path, monkeypatch):
    # A wider default root makes MAX_FILES matter more, not less — so the notice
    # the sidebar renders has to keep coming back (MD-10).
    files = {".obsidian/app.json": "{}", "docs/note.md": "x\n"}
    files.update({"other/n%d.md" % n: "x\n" for n in range(6)})
    root = _vault(tmp_path, files)
    real = graph.scan_indexed
    monkeypatch.setattr(graph, "scan_indexed", lambda where: real(where, max_files=3))
    out = graph.main(action="note", file=os.path.join(root, "docs", "note.md"))
    assert out["root"] == _client(root)
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
    # The contract path: the server exports the resolved mounts dir and appenv
    # reads it, so pointing the var at `root` makes the whole vault mount-backed.
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", root)
    out = graph.main(action="note", file=os.path.join(root, "A.md"), root=root)
    assert out["error"] == "mount_unsupported"
    # And no walk happened: scan_root itself refuses, so nothing can slip past
    # a caller that forgot to check.
    with pytest.raises(graph.MountUnsupported):
        graph.scan_root(root)


def test_a_local_root_is_not_refused_when_a_mounts_dir_merely_exists(
        graph, tmp_path, monkeypatch):
    root = _vault(tmp_path, {"A.md": "hi\n"})
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", str(tmp_path / "elsewhere"))
    assert graph.main(action="note", file=os.path.join(root, "A.md"), root=root)["error"] is None


# ------------------------------------------------------- autocomplete candidates


def test_candidates_lists_notes_headings_and_assets(graph, tmp_path):
    root = _vault(tmp_path, {
        "Hub.md": "# Hub\n## Details\n[[Leaf]]\n",
        "sub/Leaf.md": "# Leaf\n",
        "sub/pic.png": "x",
    })
    out = graph.main(action="candidates", root=root)
    assert out["error"] is None
    assert [n["rel"] for n in out["notes"]] == ["Hub.md", "sub/Leaf.md"]
    hub = out["notes"][0]
    assert hub["title"] == "Hub"
    assert [h["text"] for h in hub["headings"]] == ["Hub", "Details"]
    assert "tags" not in out
    # An asset row is `{rel, embed}`: the path, and the form `![[…]]` may insert
    # for it (MD-14). Here nothing shadows it, so the basename is enough.
    assert out["assets"] == [{"rel": "sub/pic.png", "embed": "pic.png"}]


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


def test_a_link_form_is_validated_from_the_note_it_will_be_inserted_into(graph, tmp_path):
    # `file` names the note the popup is running in, and tier 1 resolves relative
    # to that note's folder — so the sibling is reachable as the bare basename
    # from there, while the far one has to carry its folder. Validated from the
    # ROOT (no `file`) the two are indistinguishable and both carry a folder.
    root = _vault(tmp_path, {
        "docs/n.md": "x\n", "docs/Same.md": "x\n", "b/Same.md": "x\n",
    })
    forms = {n["rel"]: n["link"] for n in graph.main(
        action="candidates", root=root, file=os.path.join(root, "docs", "n.md"))["notes"]}
    assert forms["docs/Same.md"] == "Same"
    assert forms["b/Same.md"] == "b/Same"
    rooted = {n["rel"]: n["link"] for n in graph.main(action="candidates", root=root)["notes"]}
    assert rooted["docs/Same.md"] == "docs/Same"


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


def test_an_asset_embed_form_is_the_shortest_one_that_resolves_back(graph, tmp_path):
    # `![[img/a.png]]` written in `docs/` binds to `docs/img/a.png` the moment
    # that file exists, because tier 1 of resolution is relative to the linking
    # note's own folder (MD-4). So the root-relative path is NOT a safe form to
    # insert, and the payload must offer whatever does resolve back: `../` for the
    # one at the root, the plain path for the one in the note's own subtree.
    root = _vault(tmp_path, {
        "docs/n.md": "x\n",
        "img/a.png": "x",
        "docs/img/a.png": "x",
        "docs/solo.png": "x",
    })
    out = graph.main(action="candidates", root=root,
                     file=os.path.join(root, "docs", "n.md"))
    assert {a["rel"]: a["embed"] for a in out["assets"]} == {
        "docs/img/a.png": "img/a.png",
        "docs/solo.png": "solo.png",
        "img/a.png": "../img/a.png",
    }


def test_an_asset_with_no_unambiguous_form_says_so_instead_of_lying(graph, tmp_path):
    # The stem-key collision: `_candidate_index` files a note under its stem, so
    # the note `img/a.png.md` and the asset `img/a.png` share the key `img/a.png`
    # and `_only` refuses to pick between them (MD-4). No form of either resolves
    # back, so both carry a null form — the popup drops the row rather than
    # inserting a path the resolver reads as somebody else's file.
    root = _vault(tmp_path, {"n.md": "x\n", "img/a.png": "x", "img/a.png.md": "x\n"})
    out = graph.main(action="candidates", root=root)
    assert {a["rel"]: a["embed"] for a in out["assets"]} == {"img/a.png": None}
    shadow = [n for n in out["notes"] if n["rel"] == "img/a.png.md"][0]
    assert shadow["embed"] is None
    # The note is still reachable by a plain `[[…]]`: that resolves through the
    # note index, where the asset is not a candidate at all.
    assert shadow["link"] == "a.png"


def test_every_candidate_embed_form_resolves_back_to_its_own_file(graph, tmp_path):
    # The asset half of the property above, and the reason `embed` exists as a
    # second field: an embed resolves through notes PLUS assets, so a form
    # validated against the note index is not validated for `![[…]]`. Every
    # non-null form must round-trip from the note the popup ran in.
    root = _vault(tmp_path, {
        "docs/n.md": "x\n",
        "docs/Same.md": "x\n",
        "b/Same.md": "x\n",
        "img/a.png": "x",
        "docs/img/a.png": "x",
        "deep/nest/pic.png": "x",
        "pic.png": "x",
    })
    rel = "docs/n.md"
    out = graph.main(action="candidates", root=root, file=os.path.join(root, rel))
    paths = [n["rel"] for n in out["notes"]] + [a["rel"] for a in out["assets"]]
    rows = [(a["rel"], a["embed"]) for a in out["assets"]]
    rows += [(n["rel"], n["embed"]) for n in out["notes"]]
    assert [form for _, form in rows if form], "every form was null — vacuous"
    for target, form in rows:
        if form is None:
            continue
        assert graph.resolve_link(form, rel, paths) == target, (target, form)


def test_the_suffix_index_answers_exactly_what_the_endswith_scan_answered(graph):
    # `_suffix_index` is a memoisation of resolution's third tier and nothing
    # else — ~1.9s of `endswith` at the caps became a ~40ms payload (D191) — so
    # the only thing to assert is that it changed no answer, ambiguous ones
    # included.
    paths = ["a.md", "a/b.md", "c/a/b.md", "img/a.png", "img/a.png.md", "d/e/f/g.md"]
    index = graph._candidate_index(paths)
    suffixes = graph._suffix_index(index)
    targets = ["a", "b", "a/b", "c/a/b", "a.png", "img/a.png", "g", "f/g",
               "e/f/g", "nope", "b.md"]
    for from_rel in ("", "a/b.md", "c/a/b.md", "d/e/f/g.md"):
        for target in targets:
            assert (graph.resolve_link(target, from_rel, paths, index, suffixes)
                    == graph.resolve_link(target, from_rel, paths, index)), (target, from_rel)


def test_candidates_refuses_a_mount_backed_root(graph, tmp_path, monkeypatch):
    root = _vault(tmp_path, {"A.md": "x\n"})
    # The contract path: the server exports the resolved mounts dir and appenv
    # reads it, so pointing the var at `root` makes the whole vault mount-backed.
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", root)
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
    # The contract path: the server exports the resolved mounts dir and appenv
    # reads it, so pointing the var at `root` makes the whole vault mount-backed.
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", root)
    with pytest.raises(graph.MountUnsupported):
        graph.scan_indexed(root)
    assert not os.path.exists(os.path.join(home, "graph"))


# ---------------------------------------------------------- graph assembly


@pytest.fixture()
def chain(tmp_path, home):
    """A.md -> B.md -> C.md, plus a ghost hanging off A."""
    return _vault(tmp_path, {
        "A.md": "# A\n[[B]] and [[Nope]]\n",
        "B.md": "# B\n[[C]]\n",
        "C.md": "# C\n",
    })


def _ids(payload):
    return sorted(node["id"] for node in payload["nodes"])


def test_the_whole_vault_graph_carries_notes_and_ghosts(graph, chain):
    out = graph.main(action="graph", root=chain)
    assert out["error"] is None
    assert _ids(out) == ["A.md", "B.md", "C.md", "ghost:nope"]
    kinds = {node["id"]: node["kind"] for node in out["nodes"]}
    assert kinds["ghost:nope"] == "ghost"
    assert sorted((e["source"], e["target"]) for e in out["edges"]) == [
        ("A.md", "B.md"), ("A.md", "ghost:nope"), ("B.md", "C.md")]


def test_a_hashtag_makes_no_node(graph, tmp_path, home):
    """The tag concept is gone (D165). A `#word` in prose — the shape that used
    to litter a code-adjacent vault with junk nodes — contributes nothing."""
    root = _vault(tmp_path, {"A.md": "# A\n#backer and #dsamed-environments\n"})
    out = graph.main(action="graph", root=root)
    assert _ids(out) == ["A.md"]
    assert out["edges"] == []


def test_node_degree_counts_both_directions(graph, chain):
    degrees = {n["id"]: n["degree"] for n in graph.main(action="graph", root=chain)["nodes"]}
    assert degrees["A.md"] == 2   # B and the ghost
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
    assert _ids(d1) == ["A.md", "B.md", "ghost:nope"]
    d2 = graph.main(action="graph", root=chain, file=focus, depth=2)
    assert _ids(d2) == ["A.md", "B.md", "C.md", "ghost:nope"]
    assert d1["focus"] == "A.md" and d1["depth"] == 1


def test_a_negative_depth_is_the_whole_vault_even_with_a_focus(graph, tmp_path, home):
    """The panel's `all` option. A *negative* depth is the sentinel rather than
    `0`, because `0` already means something on this path (just the focus) and
    the folder graph already sends `"0"` to mean "no focus, so no filter"."""
    root = _vault(tmp_path, {"A.md": "[[B]]\n", "B.md": "x\n", "Island.md": "x\n"})
    focus = os.path.join(root, "A.md")
    # No finite depth reaches Island: nothing links to it.
    assert _ids(graph.main(action="graph", root=root, file=focus, depth=3)) == ["A.md", "B.md"]
    out = graph.main(action="graph", root=root, file=focus, depth=-1)
    assert _ids(out) == ["A.md", "B.md", "Island.md"]
    # The focus survives the unfiltered view — the canvas still draws it apart.
    assert out["focus"] == "A.md" and out["depth"] == -1
    # As the template actually sends it: `String(graphDepth())`.
    assert _ids(graph.main(action="graph", root=root, file=focus, depth="-1")) == _ids(out)


def test_the_folder_graph_keeps_asking_for_everything_with_depth_zero(graph, chain):
    """The folder mode sends `depth: "0"` and no `file`; `all` must not have
    changed what that means."""
    out = graph.main(action="graph", root=chain, depth="0")
    assert out["focus"] is None and out["depth"] == 0
    assert _ids(out) == ["A.md", "B.md", "C.md", "ghost:nope"]


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
    # The contract path: the server exports the resolved mounts dir and appenv
    # reads it, so pointing the var at `root` makes the whole vault mount-backed.
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", root)
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


# --------------------------------------------- client-facing paths (Windows)


def test_a_payload_path_is_forward_slash_even_from_a_windows_root(graph):
    """Every path in a payload is POSIX-shaped, whatever the platform.

    The filesystem side rightly uses native separators — `os.path.abspath`
    returns `C:\\Users\\me\\vault` on Windows — but the template splits and
    re-joins these values on "/", and the shell's canonical form for a Windows
    file is the drive path `C:/Users/…`. A native separator crossing the
    boundary is silently mis-split at the other end, so it is converted once,
    here. Driven directly rather than through a payload so the Windows shape is
    exercised on every platform.
    """
    assert graph._client_path("C:\\Users\\me\\vault") == "C:/Users/me/vault"
    assert graph._client_join("C:\\Users\\me\\vault", "docs/a.md") == \
        "C:/Users/me/vault/docs/a.md"
    # A POSIX root is already in that form and must come back untouched.
    assert graph._client_join("/home/me/vault", "docs/a.md") == \
        "/home/me/vault/docs/a.md"
    # No empty segment when the root carries a trailing slash, and no trailing
    # slash when there is no rel to append.
    assert graph._client_join("/home/me/vault/", "a.md") == "/home/me/vault/a.md"
    assert graph._client_join("/home/me/vault", "") == "/home/me/vault"


def test_every_path_a_payload_carries_uses_forward_slashes(graph, tmp_path, home):
    """The contract at the boundary, over all three payloads at once."""
    root = _vault(tmp_path, {
        "A.md": "# A\n[[docs/B]] and ![[missing.png]]\n",
        "docs/B.md": "# B\n[[A]]\n",
    })
    note = graph.main(action="note", file=os.path.join(root, "A.md"), root=root)
    found = [note["root"]]
    found += [link["path"] for link in note["links"] if link["path"]]
    found += [back["path"] for back in note["backlinks"] if back["path"]]
    cands = graph.main(action="candidates", root=root)
    found += [cands["root"]] + [row["path"] for row in cands["notes"]]
    graphed = graph.main(action="graph", root=root)
    found += [graphed["root"]]
    found += [node["path"] for node in graphed["nodes"] if node["path"]]
    assert found, "the payloads carried no paths at all — the assertion is empty"
    assert [p for p in found if "\\" in p] == []

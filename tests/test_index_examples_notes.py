"""The one app-authored example indexer (SPEC-index-plugins.md decisions
#6/#9): a reference implementation, exercised here end to end — its own
manifest parses, its own extract() behaves, and it is loadable the exact
way a real third-party folder would be (by path, via its parsed
manifest's `module`), never by a package-style import. Documented and
tested; never surfaced to end users as something to enable.
"""
import importlib.util
import os

from fused_render.index.ignore import norm
from fused_render.index.manifest import load_manifest

EXAMPLE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "index", "examples", "notes_indexer",
)


def _load_example_module():
    """Load the example the same way a real caller would: parse its
    manifest, then import exactly the file the manifest names — never a
    package-style `import`, since a third party's folder is not on
    sys.path and is not part of this codebase."""
    m = load_manifest(EXAMPLE_DIR)
    assert m is not None
    spec = importlib.util.spec_from_file_location("notes_indexer_example", m.module)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return m, mod


def test_the_example_folder_carries_a_valid_manifest():
    m = load_manifest(EXAMPLE_DIR)
    assert m is not None
    assert m.kind == "notes"
    assert m.module == os.path.join(os.path.abspath(EXAMPLE_DIR), "indexer.py")


def test_the_loaded_module_declares_the_kind_its_manifest_promises():
    m, mod = _load_example_module()
    assert mod.KIND.name == m.kind


def test_extract_ignores_non_markdown_files(tmp_path):
    _, mod = _load_example_module()
    p = tmp_path / "notes.txt"
    p.write_text("# Title\nbody", encoding="utf-8")
    assert mod.extract(str(p), os.stat(str(p))) is None


def test_extract_uses_the_first_heading_as_title(tmp_path):
    _, mod = _load_example_module()
    p = tmp_path / "a.md"
    p.write_text("intro line\n# Real Title\nmore words here\n", encoding="utf-8")
    row = mod.extract(str(p), os.stat(str(p)))
    assert row["title"] == "Real Title"
    assert row["path"] == norm(os.path.abspath(str(p)))
    assert row["word_count"] == len("intro line\n# Real Title\nmore words here\n".split())


def test_extract_falls_back_to_the_filename_without_a_heading(tmp_path):
    _, mod = _load_example_module()
    p = tmp_path / "untitled-note.md"
    p.write_text("just some prose, no heading here\n", encoding="utf-8")
    row = mod.extract(str(p), os.stat(str(p)))
    assert row["title"] == "untitled-note"


def test_extract_never_raises_on_an_unreadable_file():
    _, mod = _load_example_module()
    ghost = "/no/such/path/ever.md"
    assert mod.extract(ghost, os.stat_result((0,) * 10)) is None


def test_register_kind_adds_the_notes_kind_to_the_registry():
    from fused_render.index.kinds import get, registered

    _, mod = _load_example_module()
    mod.register_kind(replace=True)
    assert "notes" in registered()
    assert get("notes") is mod.KIND

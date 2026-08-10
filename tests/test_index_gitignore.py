"""Gitignore parity for the index corpus: the walk prunes gitignored entries
and the index-first swap must not change what search shows.

See fused_render/server/index_gitignore.py.
"""
import os
import subprocess

from fused_render.server import index_gitignore
from fused_render.server.index_gitignore import filter_corpus


def _entry(rel, is_dir=False):
    return {"rel": rel, "is_dir": is_dir, "size": None if is_dir else 1,
            "mtime": 1.0}


def _out(root, rels, updated=123.0):
    return {"covered": True, "fresh": True, "updated": updated,
            "root": root, "entries": [_entry(r) for r in rels],
            "truncated": False, "total": len(rels)}


def _fresh_cache(monkeypatch):
    monkeypatch.setattr(index_gitignore, "_cache", type(index_gitignore._cache)())


def test_a_standalone_gitignore_prunes_the_corpus(tmp_path, monkeypatch):
    """No repo anywhere — a bare .gitignore marks an ignore root, exactly as
    the walk treats it (the empty-GIT_DIR graft)."""
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("dist/\n*.log\n", encoding="utf-8")
    out = filter_corpus(_out(str(tmp_path), [
        "proj/.gitignore", "proj/src/main.py", "proj/dist/bundle.js",
        "proj/debug.log", "top.txt"]))
    rels = [e["rel"] for e in out["entries"]]
    assert "proj/src/main.py" in rels
    assert "top.txt" in rels
    assert "proj/dist/bundle.js" not in rels
    assert "proj/debug.log" not in rels
    assert out["total"] == len(rels)


def test_a_root_inside_a_repo_uses_the_toplevel_rules(tmp_path, monkeypatch):
    """Searching a SUBFOLDER of a repo: no .gitignore is visible in the
    corpus, but the toplevel's rules still apply (one oracle at the
    toplevel; git cascades from there)."""
    _fresh_cache(monkeypatch)
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("*.gen\n", encoding="utf-8")
    out = filter_corpus(_out(str(repo / "sub"), ["keep.py", "junk.gen"]))
    assert [e["rel"] for e in out["entries"]] == ["keep.py"]


def test_no_ignore_roots_is_a_no_op(tmp_path, monkeypatch):
    _fresh_cache(monkeypatch)
    out = _out(str(tmp_path), ["a.txt", "b/c.txt"])
    assert filter_corpus(out)["entries"] == out["entries"]


def test_an_uncovered_response_is_untouched(tmp_path, monkeypatch):
    _fresh_cache(monkeypatch)
    out = {"covered": False, "entries": [], "root": str(tmp_path)}
    assert filter_corpus(out) is out


def test_the_verdict_is_cached_per_index_generation(tmp_path, monkeypatch):
    """The git queries run once per (root, updated) — every later search of
    the same generation is a cache hit; a new compaction re-filters."""
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    calls = []
    real = index_gitignore._apply

    def counting(root, entries):
        calls.append(root)
        return real(root, entries)

    monkeypatch.setattr(index_gitignore, "_apply", counting)
    rels = ["proj/.gitignore", "proj/a.log", "proj/a.py"]
    first = filter_corpus(_out(str(tmp_path), rels, updated=1.0))
    again = filter_corpus(_out(str(tmp_path), rels, updated=1.0))
    assert len(calls) == 1
    assert [e["rel"] for e in again["entries"]] == [e["rel"] for e in first["entries"]]
    filter_corpus(_out(str(tmp_path), rels, updated=2.0))
    assert len(calls) == 2


def test_a_query_filtered_response_is_never_cached(tmp_path, monkeypatch):
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    subset = _out(str(tmp_path), ["proj/.gitignore", "proj/a.log"])
    filter_corpus(subset, cacheable=False)
    full = filter_corpus(_out(str(tmp_path), [
        "proj/.gitignore", "proj/a.log", "proj/a.py"]))
    assert "proj/a.py" in [e["rel"] for e in full["entries"]]


def test_nested_markers_defer_to_the_outermost(tmp_path, monkeypatch):
    """An inner .gitignore is cascaded by its ancestor's oracle; only the
    outermost marker gets an oracle, and BOTH levels' rules still apply."""
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / ".gitignore").write_text("*.outer\n", encoding="utf-8")
    (proj / "sub" / ".gitignore").write_text("*.inner\n", encoding="utf-8")
    out = filter_corpus(_out(str(tmp_path), [
        "proj/.gitignore", "proj/sub/.gitignore",
        "proj/x.outer", "proj/sub/y.inner", "proj/sub/z.py"]))
    rels = [e["rel"] for e in out["entries"]]
    assert "proj/x.outer" not in rels
    assert "proj/sub/y.inner" not in rels
    assert "proj/sub/z.py" in rels

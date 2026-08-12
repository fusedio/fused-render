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


def _counting_queries(monkeypatch):
    """Every rel `_ignored` actually asks git about, one list per call."""
    asked = []
    real = index_gitignore._ignored

    def counting(root, entries, top, deciders, want):
        asked.append(sorted(entries[i]["rel"] for i in want))
        return real(root, entries, top, deciders, want)

    monkeypatch.setattr(index_gitignore, "_ignored", counting)
    return asked


def test_a_repeated_search_asks_git_nothing(tmp_path, monkeypatch):
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    asked = _counting_queries(monkeypatch)
    rels = ["proj/.gitignore", "proj/a.log", "proj/a.py"]
    first = filter_corpus(_out(str(tmp_path), rels, updated=1.0))
    again = filter_corpus(_out(str(tmp_path), rels, updated=1.0))
    assert len(asked) == 1
    assert [e["rel"] for e in again["entries"]] == [e["rel"] for e in first["entries"]]
    assert "proj/a.log" not in [e["rel"] for e in first["entries"]]


def test_a_grown_corpus_only_asks_about_the_paths_it_has_not_decided(
        tmp_path, monkeypatch):
    """A completed scan must not cost a full re-sweep.

    A scan finishing moves `updated`, and the old cache threw everything away
    and re-ran check-ignore over the whole corpus — ~1s per 74k entries, on the
    very next keystroke, while the scan's workers were still competing for IO.
    The generation is not part of the pool key at all now; what a new one
    brings is new PATHS, and only those are asked about.
    """
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    asked = _counting_queries(monkeypatch)
    rels = ["proj/.gitignore", "proj/a.log", "proj/a.py"]
    filter_corpus(_out(str(tmp_path), rels, updated=1.0))
    out = filter_corpus(_out(str(tmp_path), rels + ["proj/b.log", "proj/b.py"], updated=2.0))
    assert asked[1] == ["proj/b.log", "proj/b.py"]
    # ...and the newly-appeared ones are filtered on the same pass, so a build
    # directory that appeared since the last scan never leaks into search.
    assert [e["rel"] for e in out["entries"]] == ["proj/.gitignore", "proj/a.py", "proj/b.py"]


def test_verdicts_survive_a_new_generation_until_they_are_too_old(
        tmp_path, monkeypatch):
    """Reuse across an index generation, and its bound: a path that BECAME
    gitignored is served until the pool ages out, and not past that."""
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("nothing-here\n", encoding="utf-8")
    rels = ["proj/.gitignore", "proj/a.log"]
    assert len(filter_corpus(_out(str(tmp_path), rels, updated=1.0))["entries"]) == 2
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    # Still served from the pooled verdicts — that is the bounded staleness.
    assert len(filter_corpus(_out(str(tmp_path), rels, updated=2.0))["entries"]) == 2
    for pool in index_gitignore._cache.values():
        pool.swept_at -= index_gitignore.VERDICT_MAX_AGE_S + 1
    assert [e["rel"] for e in filter_corpus(
        _out(str(tmp_path), rels, updated=3.0))["entries"]] == ["proj/.gitignore"]


def test_a_folder_is_answered_out_of_its_index_root_pool(tmp_path, monkeypatch):
    """Item 4: browsing folders must not evict each other.

    The old cache was keyed on the REQUESTED root, so five folders opened in a
    row evicted the first and re-paid a full check-ignore sweep of its whole
    recursive subtree. Verdicts pool per index root, so a subfolder's corpus is
    answered from what the root already asked.
    """
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    root = str(tmp_path)
    filter_corpus(_out(root, ["proj/.gitignore", "proj/a.log", "proj/a.py"]),
                  index_root=root)
    asked = _counting_queries(monkeypatch)
    # Now the in-folder search of proj/, whose rels are relative to proj/.
    out = filter_corpus(_out(str(proj), [".gitignore", "a.log", "a.py"]),
                        index_root=root)
    assert asked == []  # every verdict was already pooled
    assert [e["rel"] for e in out["entries"]] == [".gitignore", "a.py"]
    assert out["total"] == 2
    assert len(index_gitignore._cache) == 1


def test_a_root_outside_the_index_root_gets_its_own_pool(tmp_path, monkeypatch):
    """A mis-stated index_root must not silently re-base rels onto it."""
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    out = filter_corpus(_out(str(proj), [".gitignore", "a.log", "a.py"]),
                        index_root="/somewhere/else")
    assert [e["rel"] for e in out["entries"]] == [".gitignore", "a.py"]


def test_a_scope_that_cannot_see_the_rules_pools_nothing(tmp_path, monkeypatch):
    """A NEGATIVE verdict is only worth pooling when the request could actually
    have found the rule that would have made it positive.

    Searching inside a gitignored folder puts the deciding `.gitignore` ABOVE
    the request's corpus, so the no-repo branch finds no marker and answers
    "nothing ignored". Written into the pool as fact, that answer then
    suppressed every later, wider request from ever asking — so the home search
    started serving the build directory that `origin/main` filtered out, for
    the rest of the server's life.
    """
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    (proj / "dist").mkdir(parents=True)
    (proj / ".gitignore").write_text("dist/\n", encoding="utf-8")
    root = str(tmp_path)
    # The in-folder search of proj/dist: no marker in sight, nothing filtered.
    inner = filter_corpus(_out(str(proj / "dist"), ["x.js"]), index_root=root)
    assert [e["rel"] for e in inner["entries"]] == ["x.js"]
    # The home search still filters it, because the pool never accepted the
    # inner request's blind answer.
    out = filter_corpus(_out(root, [
        "proj/.gitignore", "proj/dist/x.js", "proj/keep.py"]), index_root=root)
    assert [e["rel"] for e in out["entries"]] == ["proj/.gitignore", "proj/keep.py"]


def test_a_narrowed_payload_pools_nothing_it_could_not_decide(tmp_path, monkeypatch):
    """Same hazard through the other door: a `q`- or `limit`-narrowed response
    can omit the `.gitignore` that decides its own entries."""
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    root = str(tmp_path)
    filter_corpus(_out(root, ["proj/a.log"]), index_root=root)
    out = filter_corpus(_out(root, ["proj/.gitignore", "proj/a.log", "proj/a.py"]),
                        index_root=root)
    assert [e["rel"] for e in out["entries"]] == ["proj/.gitignore", "proj/a.py"]


def test_a_narrower_marker_does_not_answer_for_the_outer_one(tmp_path, monkeypatch):
    """A request that sees only the INNER `.gitignore` decides its entries
    under rules the outer one would have overridden, so its verdicts must not
    be reused by a request that can see both."""
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / ".gitignore").write_text("*.outer\n", encoding="utf-8")
    (proj / "sub" / ".gitignore").write_text("*.inner\n", encoding="utf-8")
    root = str(tmp_path)
    # Only the inner marker is visible here, so x.outer reads clean.
    inner = filter_corpus(_out(root, ["proj/sub/.gitignore", "proj/sub/x.outer"]),
                          index_root=root)
    assert "proj/sub/x.outer" in [e["rel"] for e in inner["entries"]]
    out = filter_corpus(_out(root, [
        "proj/.gitignore", "proj/sub/.gitignore", "proj/sub/x.outer",
        "proj/sub/keep.py"]), index_root=root)
    assert [e["rel"] for e in out["entries"]] == [
        "proj/.gitignore", "proj/sub/.gitignore", "proj/sub/keep.py"]


def test_the_pool_is_re_swept_once_it_is_too_old_whatever_the_generation(
        tmp_path, monkeypatch):
    """The staleness bound has to be a bound. Gated on a generation change as
    well, it never fired at all on a settled index — and a rule REMOVED from a
    .gitignore kept its paths invisible to search for the server's lifetime."""
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    rels = ["proj/.gitignore", "proj/a.log"]
    root = str(tmp_path)
    assert len(filter_corpus(_out(root, rels), index_root=root)["entries"]) == 1
    (proj / ".gitignore").write_text("nothing-here\n", encoding="utf-8")
    # Same generation, so nothing else would ever re-ask.
    assert len(filter_corpus(_out(root, rels), index_root=root)["entries"]) == 1
    for pool in index_gitignore._cache.values():
        pool.swept_at -= index_gitignore.VERDICT_MAX_AGE_S + 1
    assert len(filter_corpus(_out(root, rels), index_root=root)["entries"]) == 2


def test_a_narrowed_payload_cannot_masquerade_as_the_corpus(tmp_path, monkeypatch):
    """A `q`-filtered or capped response is a SUBSET. It may contribute
    verdicts (a verdict is a fact about one path, true for every request), but
    a later full request must still get every row it asked about."""
    _fresh_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8")
    subset = _out(str(tmp_path), ["proj/.gitignore", "proj/a.log"])
    subset["truncated"] = True
    filter_corpus(subset)
    full = filter_corpus(_out(str(tmp_path), [
        "proj/.gitignore", "proj/a.log", "proj/a.py"]))
    assert [e["rel"] for e in full["entries"]] == ["proj/.gitignore", "proj/a.py"]
    assert full["total"] == len(full["entries"])


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

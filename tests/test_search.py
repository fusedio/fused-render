"""Unit tests for /api/search/files' spec validation and query building.

The mdfind query string is a security boundary (model-originated values are
embedded in it), so the escaping and allowlist tests here are the load-bearing
ones — see search.py's module docstring.
"""

import pytest

from fused_render.server.routers.search import (
    _drop_gitignored,
    _junk_path,
    _match_walk_entry,
    _mdfind_query,
    _nearest_repo,
    _parse_spec,
)


def spec(**over):
    base = {
        "name_terms": [],
        "extensions": [],
        "kind": "any",
        "modified_within_days": None,
        "min_size_bytes": None,
        "max_size_bytes": None,
    }
    base.update(over)
    return base


# -- _parse_spec ---------------------------------------------------------------

def test_parse_spec_cleans_terms_extensions_and_numbers():
    parsed = _parse_spec(
        {
            "name_terms": ['we"ather\\', "  ", "ok"],
            "extensions": [".MOV", "tar.gz", "c/v", "mp4"],
            "kind": "file",
            "modified_within_days": 1,
            "min_size_bytes": 10,
        }
    )
    # quote/backslash stripped (mdfind string-literal escape chars), blanks dropped
    assert parsed["name_terms"] == ["weather", "ok"]
    # dot peeled + lowercased; anything outside [a-z0-9]{1,12} dropped
    assert parsed["extensions"] == ["mov", "mp4"]
    assert parsed["kind"] == "file"
    assert parsed["modified_within_days"] == 1
    assert parsed["min_size_bytes"] == 10


@pytest.mark.parametrize(
    "body",
    [
        {"name_terms": "notalist"},
        {"name_terms": [1]},
        {"kind": "everything"},
        {"modified_within_days": -1},
        {"min_size_bytes": True},
    ],
)
def test_parse_spec_rejects_malformed_bodies(body):
    with pytest.raises(ValueError):
        _parse_spec(body)


# -- _mdfind_query -------------------------------------------------------------

def test_mdfind_query_composes_all_constraints():
    q = _mdfind_query(
        spec(
            name_terms=["report"],
            extensions=["mov", "mp4"],
            kind="file",
            modified_within_days=1,
            min_size_bytes=100,
        )
    )
    assert '(kMDItemFSName = "*report*"cd)' in q
    assert '(kMDItemFSName = "*.mov"cd || kMDItemFSName = "*.mp4"cd)' in q
    assert 'kMDItemContentType != "public.folder"' in q
    assert "kMDItemFSContentChangeDate >= $time.now(-86400)" in q
    assert "kMDItemFSSize >= 100" in q
    assert " && " in q


def test_mdfind_query_requires_a_narrowing_constraint():
    # Nothing at all, and kind-only, would both match half the disk.
    assert _mdfind_query(spec()) is None
    assert _mdfind_query(spec(kind="dir")) is None
    assert _mdfind_query(spec(kind="dir", name_terms=["x"])) is not None


# -- _match_walk_entry (non-darwin fallback filters) ----------------------------

def walk_entry(rel, is_dir=False, size=1000, mtime=1000.0):
    return {"rel": rel, "is_dir": is_dir, "size": None if is_dir else size, "mtime": mtime}


def test_match_walk_entry_enforces_hard_filters():
    s = spec(extensions=["csv"], kind="file", modified_within_days=1, min_size_bytes=500)
    now = 1000.0 + 3600
    assert _match_walk_entry(walk_entry("data/report.csv"), s, now)
    assert not _match_walk_entry(walk_entry("data/report.txt"), s, now)
    assert not _match_walk_entry(walk_entry("data", is_dir=True), s, now)
    assert not _match_walk_entry(walk_entry("old.csv", mtime=now - 90000), s, now)
    assert not _match_walk_entry(walk_entry("tiny.csv", size=10), s, now)


def test_match_walk_entry_dirs_skip_extension_and_size():
    s = spec(extensions=["csv"], min_size_bytes=500)
    assert _match_walk_entry(walk_entry("data", is_dir=True), s, 2000.0)


# -- Spotlight hit screening (junk, hidden, gitignored) --------------------------

def test_junk_path_drops_ignore_dirs_and_hidden_segments():
    assert _junk_path("/Users/me/proj/node_modules/a/b.mov")
    assert _junk_path("/Users/me/proj/.venv/lib/x.mp4")
    assert _junk_path("/Users/me/.config/thing.mov")  # hidden segment
    assert not _junk_path("/Users/me/Downloads/clip.mov")


def test_nearest_repo_memoizes_ancestors(tmp_path):
    repo = tmp_path / "repo"
    (repo / "a" / "b").mkdir(parents=True)
    (repo / ".git").mkdir()
    memo = {}
    assert _nearest_repo(str(repo / "a" / "b"), memo) == str(repo)
    # every probed ancestor is memoized, including the hit itself
    assert memo[str(repo / "a")] == str(repo)
    outside = tmp_path / "plain"
    outside.mkdir()
    assert _nearest_repo(str(outside), memo) is None


def test_drop_gitignored_filters_repo_ignored_hits(tmp_path):
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("out/\n")
    (repo / "out").mkdir()
    (repo / "out" / "junk.mov").write_bytes(b"x")
    (repo / "keep.mov").write_bytes(b"x")
    loose = tmp_path / "loose.mov"
    loose.write_bytes(b"x")

    entries = [
        {"path": str(repo / "out" / "junk.mov"), "is_dir": False, "size": 1, "mtime": 1.0},
        {"path": str(repo / "keep.mov"), "is_dir": False, "size": 1, "mtime": 1.0},
        {"path": str(loose), "is_dir": False, "size": 1, "mtime": 1.0},
    ]
    kept = _drop_gitignored(entries)
    assert [e["path"] for e in kept] == [str(repo / "keep.mov"), str(loose)]

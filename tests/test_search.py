"""Unit tests for /api/search/files' spec validation and hit screening.

The spec is model-originated, so it reaches the endpoint as untrusted input and
every field is re-validated here — the allowlist tests are the load-bearing
ones (see search.py's module docstring). The engine that executes a validated
spec is the SQL index, covered in test_search_index.py.
"""

import pytest

from fused_render.server.routers.search import (
    _day_bound_epoch,
    _drop_gitignored,
    _junk_path,
    _nearest_repo,
    _parse_spec,
)


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
    # quote/backslash stripped, blanks dropped
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
        {"modified_after": "last week"},
        {"modified_before": "2026-02-31"},
        {"modified_before": 20260805},
    ],
)
def test_parse_spec_rejects_malformed_bodies(body):
    with pytest.raises(ValueError):
        _parse_spec(body)


@pytest.mark.parametrize("key", ["created_after", "created_before"])
def test_parse_spec_refuses_a_creation_date_filter(key):
    """The index stores `mtime` and no birth time, and it is the only engine —
    so a created_* filter is unanswerable. It is REFUSED rather than dropped:
    silently searching by modification date instead would answer a different
    question than the caller asked. Only a stale client can still send one."""
    with pytest.raises(ValueError, match="created"):
        _parse_spec({"name_terms": ["x"], key: "2026-06-01"})
    # ...and the key is not in the parsed spec at all
    assert key not in _parse_spec({"name_terms": ["x"]})


def test_day_bounds_are_inclusive_local_days():
    """`after` is that day's local midnight, `before` the midnight AFTER the
    named day — so both ends of a range include the day the user named."""
    assert (_day_bound_epoch("2026-06-30", True)
            - _day_bound_epoch("2026-06-30", False)) == 86400.0


# -- hit screening (junk, hidden, gitignored) ----------------------------------

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

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
            "min_size_bytes": 10,
        }
    )
    # quote/backslash stripped, blanks dropped
    assert parsed["name_terms"] == ["weather", "ok"]
    # dot peeled + lowercased; anything outside [a-z0-9]{1,12} dropped
    assert parsed["extensions"] == ["mov", "mp4"]
    assert parsed["kind"] == "file"
    assert parsed["min_size_bytes"] == 10


def test_parse_spec_carries_path_hints_cleaned_and_capped():
    """`path_hints` is a real engine constraint (it narrows to a path SEGMENT),
    so it is carried — and cleaned exactly like `name_terms`, because it lands
    in a LIKE pattern the same way."""
    parsed = _parse_spec(
        {"path_hints": ['down"loads\\', "  ", "desktop", "a", "b", "c", "d"]})
    assert parsed["path_hints"] == ["downloads", "desktop", "a", "b"]


@pytest.mark.parametrize(
    "body",
    [
        {"name_terms": "notalist"},
        {"path_hints": "notalist"},
        {"path_hints": [1]},
        {"name_terms": [1]},
        {"kind": "everything"},
        {"min_size_bytes": -1},
        {"min_size_bytes": True},
        {"modified_after": "last week"},
        {"modified_before": "2026-02-31"},
        {"modified_before": 20260805},
    ],
)
def test_parse_spec_rejects_malformed_bodies(body):
    with pytest.raises(ValueError):
        _parse_spec(body)


@pytest.mark.parametrize(
    "key", ["created_after", "created_before", "modified_within_days"])
def test_parse_spec_carries_no_legacy_keys(key):
    """The only client of this endpoint is the explorer's AI search, and it
    sends neither a creation-date filter (the index records mtime only) nor the
    old `modified_within_days` shape. Both are gone from the spec entirely —
    unknown keys in the body are simply not carried, like any other."""
    assert key not in _parse_spec({"name_terms": ["x"], key: "2026-06-01"})


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

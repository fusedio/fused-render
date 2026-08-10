"""Ignore-rule compilation for the file index (fused_render/index/ignore.py).

The grammar is directory-oriented: a bare name matches at any depth, a name
glob matches one segment, and anything containing a slash is a PATH pattern
compiled to a regex (fnmatch's `*` crosses `/`, which would be wrong here).
"""
import os

from fused_render.index.ignore import (
    IgnoreRules,
    clean_patterns,
    default_ignore,
    ignore_sig,
)


def test_clean_patterns_trims_drops_comments_and_dedupes():
    assert clean_patterns(
        ["  node_modules/ ", "", "# a comment", ".git", "node_modules"]
    ) == ["node_modules", ".git"]


def test_bare_name_matches_at_any_depth():
    r = IgnoreRules(["node_modules"])
    assert r.is_ignored("/a/b/node_modules")
    assert r.is_ignored("/node_modules")
    assert not r.is_ignored("/a/node_modules_x")


def test_name_glob_matches_one_segment_only():
    r = IgnoreRules(["*.egg-info"])
    assert r.is_ignored("/a/thing.egg-info")
    assert not r.is_ignored("/a/thing.egg-info/inner")


def test_path_pattern_double_star_slash_spans_zero_or_more_levels():
    r = IgnoreRules(["/home/u/.fused-render/**/mounts"])
    assert r.is_ignored("/home/u/.fused-render/mounts")
    assert r.is_ignored("/home/u/.fused-render/branches/x/mounts")
    assert not r.is_ignored("/home/u/.fused-render/branches")


def test_single_star_stays_inside_one_segment():
    r = IgnoreRules(["/a/*/c"])
    assert r.is_ignored("/a/b/c")
    assert not r.is_ignored("/a/b/x/c")


def test_is_ignored_tree_matches_inside_an_ignored_folder():
    r = IgnoreRules(["node_modules", "/a/cache"])
    # is_ignored only tests the directory itself (the walk pruned its parents)
    assert not r.is_ignored("/x/node_modules/pkg/lib")
    # is_ignored_tree also matches paths INSIDE an ignored folder — the cached
    # rows and the FSEvents journal arrive without their ancestors checked.
    assert r.is_ignored_tree("/x/node_modules/pkg/lib")
    assert r.is_ignored_tree("/a/cache/inner")
    assert not r.is_ignored_tree("/a/cached")


def test_keep_subdirs_drops_ignored_and_hardcoded_skips():
    r = IgnoreRules(["node_modules"])
    assert r.keep_subdirs(["/x/src", "/x/node_modules", "/proc", "/dev"]) == ["/x/src"]


def test_expanduser_applies_to_path_patterns():
    r = IgnoreRules(["~/Library/Caches"])
    assert r.is_ignored(os.path.expanduser("~/Library/Caches"))


def test_ignore_sig_is_order_sensitive_and_stable():
    assert ignore_sig(["a", "b"]) == ignore_sig(["a", "b"])
    assert ignore_sig(["a", "b"]) != ignore_sig(["b", "a"])


def test_default_ignore_covers_the_mounts_dir_under_the_current_home(monkeypatch, tmp_path):
    """The mounts dir must be ignored wherever FUSED_RENDER_HOME puts it — a
    hardcoded ~/.fused-render/**/mounts would miss a redirected home entirely,
    and walking a mount means kernel I/O on an rclone NFS path."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    r = IgnoreRules(default_ignore())
    assert r.is_ignored(str(tmp_path / "home" / "mounts"))
    assert r.is_ignored_tree(str(tmp_path / "home" / "mounts" / "s3" / "deep"))
    # branch-nested checkouts get their own mounts folder
    assert r.is_ignored(str(tmp_path / "home" / "branches" / "featureX" / "mounts"))

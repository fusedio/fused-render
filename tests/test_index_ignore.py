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
    norm,
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
    # `IgnoreRules.__init__` compiles the pattern through `norm(expanduser(...))`
    # (forward-slashed even on Windows, where `expanduser` itself hands back
    # `C:\Users\...`); the path being tested against that regex has to make
    # the same trip or a native separator never matches at all.
    assert r.is_ignored(norm(os.path.expanduser("~/Library/Caches")))


def test_ignore_sig_is_order_sensitive_and_stable():
    assert ignore_sig(["a", "b"]) == ignore_sig(["a", "b"])
    assert ignore_sig(["a", "b"]) != ignore_sig(["b", "a"])


def test_default_ignore_covers_the_mounts_dir_under_the_current_home(monkeypatch, tmp_path):
    """The mounts dir must be ignored wherever FUSED_RENDER_HOME puts it — a
    hardcoded ~/.fused-render/**/mounts would miss a redirected home entirely,
    and walking a mount means kernel I/O on an rclone NFS path."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    r = IgnoreRules(default_ignore())
    # `default_ignore()` compiles a `**/mounts` GLOB that is already `norm`ed
    # (ignore.py) — the paths tested against it need the same forward-slash
    # form, not the native (backslash, on Windows) spelling `str(Path)` gives.
    assert r.is_ignored(norm(str(tmp_path / "home" / "mounts")))
    assert r.is_ignored_tree(norm(str(tmp_path / "home" / "mounts" / "s3" / "deep")))
    # branch-nested checkouts get their own mounts folder
    assert r.is_ignored(norm(str(tmp_path / "home" / "branches" / "featureX" / "mounts")))


def test_the_walk_and_the_index_share_one_ignore_floor():
    """The two corpus sources must not disagree about what exists. Search is
    answered by the live walk or by the index depending on whether a scan has
    reached the folder, so a name pruned by one and kept by the other flips
    results between two sources meant to be interchangeable — the same
    inconsistency server/index_gitignore.py exists to prevent for gitignored
    entries. One definition, imported by both."""
    from fused_render.index.ignore import DEFAULT_IGNORE_NAMES, SHARED_IGNORE_DIRS
    from fused_render.server.walk import WALK_IGNORE_DIRS

    assert WALK_IGNORE_DIRS == set(SHARED_IGNORE_DIRS)
    # the index may prune MORE (a background crawl of the whole home), but
    # never less: everything the walk hides has to be hidden by the index too
    assert set(WALK_IGNORE_DIRS) <= set(DEFAULT_IGNORE_NAMES)


def test_the_walk_and_the_index_share_one_leaf_rule():
    """Same argument as the ignore floor, for the OTHER structural rule. It
    matters more here: `.git` is a leaf rather than an ignore entry precisely so
    the index carries a row for it, and a walk that kept pruning the name would
    disagree with the index about whether `.git` exists."""
    from fused_render.index.ignore import LEAF_DIR_NAMES, LEAF_DIR_SUFFIXES
    from fused_render.server.walk import (
        WALK_LEAF_DIR_NAMES,
        WALK_LEAF_DIR_SUFFIXES,
    )

    assert WALK_LEAF_DIR_NAMES == LEAF_DIR_NAMES
    assert WALK_LEAF_DIR_SUFFIXES == LEAF_DIR_SUFFIXES


def test_dot_git_is_a_leaf_not_an_ignore_entry():
    """It must be in EXACTLY one of the two mechanisms. In the ignore list it
    would leave no row for /api/git-repos to read (and would still index the ~15
    loose files sitting directly in `.git`, since ignore rules prune
    subdirectories but not files)."""
    from fused_render.index.ignore import (
        DEFAULT_IGNORE_NAMES,
        SHARED_IGNORE_DIRS,
        is_leaf_dir,
    )
    from fused_render.server.walk import WALK_IGNORE_DIRS

    assert ".git" not in SHARED_IGNORE_DIRS
    assert ".git" not in DEFAULT_IGNORE_NAMES
    assert ".git" not in WALK_IGNORE_DIRS
    assert is_leaf_dir("/x/proj/.git")


def test_a_bare_repo_named_foo_dot_git_is_NOT_a_leaf():
    """Why `.git` is a NAME rule and not another LEAF_DIR_SUFFIXES entry: that
    tuple is matched with endswith, and a bare repository is conventionally
    `foo.git` — treating those as opaque would hide the whole repository."""
    from fused_render.index.ignore import is_inside_leaf_dir, is_leaf_dir

    assert not is_leaf_dir("/srv/git/foo.git")
    assert not is_inside_leaf_dir("/srv/git/foo.git/refs")
    # and the real thing still is one, at any depth
    assert is_leaf_dir("/x/.git")
    assert is_inside_leaf_dir("/x/.git/objects/ab")


def test_ignored_for_index_is_the_one_predicate_all_three_gates_share():
    """The leaf exemption is a rule about what may EXIST as a row, and there are
    three gates that filter by the ignore list. Applying it at one of them was a
    data-loss bug (see test_index_scan's purge test), so it lives in exactly one
    predicate and every gate routes through it."""
    from fused_render.index.ignore import IgnoreRules, ignored_for_index

    rules = IgnoreRules([".git", "node_modules"])

    # the leaf's OWN name never forbids its row, under either flavour
    assert not ignored_for_index(rules, "/w/proj/.git", tree=False)
    assert not ignored_for_index(rules, "/w/proj/.git", tree=True)
    # ...but an ignored ANCESTOR still does, and only the tree flavour sees it
    assert ignored_for_index(rules, "/w/node_modules/pkg/.git", tree=True)
    # ordinary dirs are unaffected in both flavours
    assert ignored_for_index(rules, "/w/node_modules", tree=False)
    assert ignored_for_index(rules, "/w/node_modules/pkg/lib", tree=True)
    assert not ignored_for_index(rules, "/w/src", tree=True)


def test_tree_false_does_not_judge_ancestors_so_a_root_inside_a_named_dir_works():
    """Why keep_subdirs must NOT use the tree flavour: a scan root that itself sits
    inside a directory matching an ignore NAME would otherwise have every path
    under it forbidden, and the whole subtree would silently vanish."""
    from fused_render.index.ignore import IgnoreRules, ignored_for_index

    rules = IgnoreRules(["venv"])
    assert not ignored_for_index(rules, "/home/me/venv/myproject/src", tree=False)
    assert ignored_for_index(rules, "/home/me/venv/myproject/src", tree=True)


def test_leaf_rules_are_part_of_the_applied_signature():
    """The signature means "the rules this index was built under", and the leaf
    rules decide index content just as the patterns do. If they were left out, an
    index predating the `.git` leaf rule would keep matching and never rescan, so
    /api/git-repos would report zero repositories forever."""
    from fused_render.index.ignore import IgnoreRules, ignore_sig

    assert IgnoreRules(["a", "b"]).sig() != ignore_sig(["a", "b"])
    # still a pure function of the rules: same patterns, same signature
    assert IgnoreRules(["a", "b"]).sig() == IgnoreRules(["a", "b"]).sig()
    assert IgnoreRules(["a"]).sig() != IgnoreRules(["b"]).sig()

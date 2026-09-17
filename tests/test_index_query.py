"""Reading the index: partition pruning and stats. See
fused_render/index/specs/query.md.

The raw `sql` action OpenIndex exposed is deliberately absent from THIS module —
arbitrary duckdb from a client is an arbitrary read/write surface. The confined
one lives in `guarded_query.py` (tests/test_index_guarded_query.py), which is
why the assertion below is about `query.py` specifically.
"""
import os
import posixpath

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index import query as index_query
from fused_render.index.cancel import CancelToken, Cancelled
from fused_render.index.config import IndexConfig
from fused_render.index.ignore import norm
from fused_render.index.query import (
    _glob_to_regex,
    expand_whitespace_query,
    prune,
    resolve_query,
    stats,
)
from fused_render.index.store import Sink, compact


def _cfg(tmp_path):
    return IndexConfig(dir=str(tmp_path / "ix"))


def _index(tmp_path, root, paths, sizes=None, mtimes=None):
    """Build a real index over `paths` (absolute, already canonical)."""
    cfg = _cfg(tmp_path)
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    by_dir = {}
    for i, p in enumerate(paths):
        d, name = p.rsplit("/", 1)
        ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
        size = (sizes or {}).get(p, 10)
        mtime = (mtimes or {}).get(p, 100.0 + i)
        by_dir.setdefault(d, []).append((p, d, name, ext, size, mtime))
    for d, rows in by_dir.items():
        sink.add(d, "s", ("sig", rows, sum(r[4] for r in rows), 1, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


# -- partition pruning ---------------------------------------------------------

def test_prune_keeps_only_overlapping_partitions():
    parts = [{"min": "/a/1", "max": "/a/9"},
             {"min": "/b/1", "max": "/b/9"},
             {"min": "/c/1", "max": "/c/9"}]
    assert prune(parts, "/b") == [parts[1]]
    assert prune(parts, "") == parts


def test_prune_drops_a_partition_with_no_range():
    assert prune([{"min": None, "max": None}], "/a") == []


def test_prune_is_case_insensitive_like_the_match_it_gates():
    """The match is ILIKE, so the prune that gates it has to fold case too —
    otherwise /users/... rules out every /Users/... partition byte-wise and
    the anchored query returns nothing while the unanchored one finds it.
    The folded bounds are their own aggregate: byte order and folded order
    disagree, so lower(min) is NOT the folded minimum."""
    parts = [{"min": "/Users/a", "max": "/Users/z",
              "min_lower": "/users/a", "max_lower": "/users/z"}]
    assert prune(parts, "/Users/me") == parts
    assert prune(parts, "/users/me") == parts
    assert prune(parts, "/USERS/me") == parts
    assert prune(parts, "/etc/me") == []


def test_prune_falls_back_to_the_byte_test_without_folded_bounds():
    """A manifest written before the folded bounds keeps exactly the old
    behaviour — the status quo for data already on disk, not a new hole.
    Every compaction rewrites the manifest, so this heals on the next scan."""
    parts = [{"min": "/Users/a", "max": "/Users/z"}]
    assert prune(parts, "/Users/me") == parts
    assert prune(parts, "/users/me") == []
    assert prune(parts, "/zzz") == []


# -- glob translation -----------------------------------------------------------

@pytest.mark.parametrize("pattern,rel,expected", [
    ("*.csv", "report.csv", True),
    ("*.csv", "a/report.csv", False),
    ("**/*.csv", "report.csv", True),
    ("**/*.csv", "a/b/report.csv", True),
    ("*/*.csv", "a/report.csv", True),
    ("*/*.csv", "report.csv", False),
    ("*/*.csv", "a/b/report.csv", False),
    ("a/b/*.c", "a/b/x.c", True),
    ("a/b/*.c", "a/b/c/x.c", False),
    ("draft*", "draft1.txt", True),
    ("draft*", "a/draft1.txt", False),
    ("report?.csv", "report1.csv", False),  # ? is a literal character
    ("report?.csv", "report?.csv", True),
    ("[abc].csv", "[abc].csv", True),
    ("[abc].csv", "a.csv", False),
])
def test_glob_to_regex_full_match_semantics(pattern, rel, expected):
    import re as _re

    regex = _glob_to_regex(pattern.lower())
    assert bool(_re.fullmatch(regex[1:-1], rel.lower())) is expected


# -- whitespace as wildcard (SPEC-search-space-wildcard.md) --------------------
#
# `expand_whitespace_query` is the one shared transform both `resolve_query`
# and `search_under` run the raw typed string through, before either does
# anything else with it. It is pure string manipulation — no filesystem
# access — so it is testable directly, without a `root`/`_home` fixture.

def test_expand_whitespace_query_is_a_no_op_without_whitespace():
    assert expand_whitespace_query("report") == "report"
    assert expand_whitespace_query("*.pdf") == "*.pdf"
    assert expand_whitespace_query("src/**/*.ts") == "src/**/*.ts"
    assert expand_whitespace_query("") == ""


def test_expand_whitespace_query_wraps_the_final_segment():
    assert expand_whitespace_query("hello world") == "*hello*world*"
    assert expand_whitespace_query("hello world.pdf") == "*hello*world.pdf*"


def test_expand_whitespace_query_collapses_a_run_of_whitespace_to_one_star():
    """Two spaces must behave identically to one — `**` is a different,
    cross-directory wildcard in this grammar (spec §1.2), so a stray extra
    space must not silently widen the match."""
    assert expand_whitespace_query("hello  world") == expand_whitespace_query("hello world")
    assert expand_whitespace_query("hello   world") == "*hello*world*"


def test_expand_whitespace_query_trims_before_wildcarding():
    """A trailing space mid-typing must not produce a stray wildcard."""
    assert expand_whitespace_query("hello world ") == "*hello*world*"
    assert expand_whitespace_query(" hello world") == "*hello*world*"


def test_expand_whitespace_query_converts_earlier_segments_without_wrapping():
    """Segments before the last get the space->`*` conversion but no added
    leading/trailing wrap of their own."""
    assert expand_whitespace_query("~/My Documents/report") == "~/My*Documents/*report*"


def test_expand_whitespace_query_does_not_wrap_a_final_segment_with_a_user_star():
    """When the final segment already has a user-typed `*`, rule 3's implied
    wrap does not apply to it."""
    assert expand_whitespace_query("*.pdf") == "*.pdf"


def test_expand_whitespace_query_never_stacks_a_star_beside_a_user_star():
    """Code review finding: a whitespace run directly beside a literal `*`
    the user already typed must not insert a SECOND `*` next to it —
    `report *.pdf` collapsing to `report**.pdf` would cross a folder
    boundary (`**`) this query never asked for (spec: whitespace implies a
    single-segment `*`, never `**`). The user's own `*` already does the
    whitespace run's job, so the run is dropped instead of replaced.

    (Reverses the earlier "accepted edge case" note that used to sit on
    `test_expand_whitespace_query_does_not_wrap_a_final_segment_with_a_user_star`
    above — see DECISIONS.md.)"""
    assert expand_whitespace_query("report *.pdf") == "report*.pdf"
    # No wrap is added here either — the final segment already has a
    # user-typed `*`, so rule 4's implied wrap does not apply to it.
    assert expand_whitespace_query("*.pdf report") == "*.pdf*report"
    assert expand_whitespace_query("a * b") == "a*b"


@pytest.mark.parametrize("filename,expected", [
    ("hello-world.txt", True),
    ("hello world.txt", True),
    ("hello_world.py", True),
    ("my_hello_big_world.py", True),
    ("sub/hello world.txt", True),
    ("HELLO World.txt", True),
    ("hello world extra.txt", True),
    ("world-hello.txt", False),
    ("hello.world", True),
])
def test_hello_world_behavior_table(filename, expected):
    """The spec's required-behavior table (§1), proven directly against
    `_glob_to_regex` the same way `test_glob_to_regex_full_match_semantics`
    already does, independent of any filesystem-backed base resolution."""
    import re as _re

    pattern = "**/" + expand_whitespace_query("hello world")
    regex = _glob_to_regex(pattern.lower())
    assert bool(_re.fullmatch(regex[1:-1], filename.lower())) is expected


def test_hello_world_two_spaces_matches_the_same_set_as_one_space():
    import re as _re

    one = _re.fullmatch(
        _glob_to_regex(("**/" + expand_whitespace_query("hello world")).lower())[1:-1],
        "hello-world.txt")
    two = _re.fullmatch(
        _glob_to_regex(("**/" + expand_whitespace_query("hello  world")).lower())[1:-1],
        "hello-world.txt")
    assert bool(one) == bool(two) is True


# -- query resolution ------------------------------------------------------------
#
# The behaviour table from the spec, turned into tests: every row names a
# typed string, the base it resolves against, and (via `mode`/`pattern`) how
# far it reaches. `_home` below is a real directory tree on disk — base
# resolution walks the filesystem, so a fake string root would silently
# short-circuit every case that depends on a segment actually existing.

@pytest.fixture()
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "a" / "b").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(home) if p in ("~", "~/") else p)
    # `resolve_query` returns every base through `norm()` (forward slashes
    # only, the canonical form everything in the index stores and compares
    # paths as) — normalized here too, so the comparison isn't just re-
    # asserting `str(home)`'s own platform-native spelling back at itself.
    return norm(str(home))


def test_resolve_bare_substring_query_is_any_depth_at_the_box_root():
    out = resolve_query("/box", ".csv")
    assert out == {"base": "/box", "pattern": ".csv", "mode": "substring"}
    out = resolve_query("/box", "report")
    assert out == {"base": "/box", "pattern": "report", "mode": "substring"}


def test_resolve_star_with_no_slash_gets_the_implicit_any_depth_prefix():
    out = resolve_query("/box", "*.csv")
    assert out == {"base": "/box", "pattern": "**/*.csv", "mode": "glob"}
    out = resolve_query("/box", "draft*")
    assert out == {"base": "/box", "pattern": "**/draft*", "mode": "glob"}


def test_resolve_leading_slash_anchors_at_depth_one():
    out = resolve_query("/box", "/*.csv")
    assert out == {"base": "/box", "pattern": "*.csv", "mode": "glob"}


def test_resolve_one_slash_inside_reaches_exactly_two():
    out = resolve_query("/box", "*/*.csv")
    assert out == {"base": "/box", "pattern": "*/*.csv", "mode": "glob"}


def test_resolve_explicit_any_depth_form_is_unchanged():
    out = resolve_query("/box", "**/*.csv")
    assert out == {"base": "/box", "pattern": "**/*.csv", "mode": "glob"}


def test_resolve_multi_word_query_becomes_an_ordered_glob():
    out = resolve_query("/box", "hello world")
    assert out == {"base": "/box", "pattern": "**/*hello*world*", "mode": "glob"}


def test_resolve_two_spaces_resolves_identically_to_one():
    assert resolve_query("/box", "hello world") == resolve_query("/box", "hello  world")


def test_resolve_single_word_query_is_unaffected_by_the_whitespace_rule():
    out = resolve_query("/box", "report")
    assert out == {"base": "/box", "pattern": "report", "mode": "substring"}


def test_resolve_tilde_star_escapes_to_home_at_depth_one(_home):
    out = resolve_query("/box", "~/*.csv")
    assert out == {"base": _home, "pattern": "*.csv", "mode": "glob"}


def test_resolve_tilde_path_walks_to_the_deepest_real_directory(_home):
    out = resolve_query("/box", "~/a/b/*.c")
    assert out == {"base": _home + "/a/b", "pattern": "*.c", "mode": "glob"}


def test_resolve_tilde_path_stops_at_the_first_glob_segment(_home):
    out = resolve_query("/box", "~/a/*/b.csv")
    assert out == {"base": _home + "/a", "pattern": "*/b.csv", "mode": "glob"}


def test_resolve_a_space_in_a_folder_segment_stops_the_walk_there(_home):
    """Spec §2: the rule applies inside leading folder paths too. `My
    Documents` becomes `My*Documents`, a glob segment, so `_walk_from` never
    walks into it as a literal directory (even though `~/a/b` on disk here
    has no such folder to walk into either way) — base stops at home,
    exactly as it would for any other glob segment."""
    out = resolve_query("/box", "~/My Documents/report")
    assert out == {"base": _home, "pattern": "My*Documents/*report*", "mode": "glob"}


def test_resolve_a_missing_named_folder_widens_instead_of_failing(_home):
    out = resolve_query("/box", "~/nope/x.csv")
    assert out == {"base": _home, "pattern": "nope/x.csv", "mode": "substring"}


def test_resolve_never_stats_a_path_under_a_blocked_mount(_home, monkeypatch, tmp_path):
    """A wedged rclone/NFS mount parks `os.path.isdir` forever
    (SPEC-index-search-wedge.md item 1) — a `guard` handed to `resolve_query`
    must refuse a candidate under a blocked tree BEFORE the walk ever calls
    `os.path.isdir` on it, not merely fail to expand it. Monkeypatched the
    same way `test_app_listing.py`'s `test_nothing_under_a_mount_is_even_stat_ed`
    proves the ordering: the stand-in fails the test outright if the guarded
    path is ever stat'ed."""
    from fused_render.index.ignore import MountGuard

    guard = MountGuard(mounts_dir=str(tmp_path / "mounts"), home_dirs=[_home])
    real_isdir = os.path.isdir
    monkeypatch.setattr(
        os.path, "isdir",
        lambda p, _r=real_isdir: (
            pytest.fail(f"os.path.isdir on a guarded path: {p}")
            if _home in str(p) else _r(p)))
    out = resolve_query("/box", "~/a/b/*.c", guard=guard)
    assert out == {"base": _home, "pattern": "a/b/*.c", "mode": "glob"}


def test_resolve_captures_the_blocked_candidate_even_though_base_stops_short(
        _home, tmp_path):
    """D878 (review finding C): `guard.blocks()` is pure string comparison,
    so a query that escapes into a guarded subtree makes `_walk_from` stop
    ONE SEGMENT SHORT of it — `base` lands on the last UNBLOCKED ancestor,
    never on the guarded tree itself. The `blocked_out` side channel exists
    so a caller (`routers/index.py`'s `_rank_body`/`_rank_reason`) can still
    answer "mount" correctly even though `base` alone would miss it.

    D882 (index-search-wedge FIX round): the regression test for this exact
    contract, `tests/test_index_api.py::
    test_rank_reason_is_mount_for_a_typed_path_the_guarded_walk_stopped_short_of`,
    failed on Windows CI — but the cause traced to that test's OWN
    HOME-mocking shim (`_point_home_at`), which never redirected Windows'
    `ntpath.expanduser` for a compound `"~/..."` path, not to any defect in
    this logic. This test exercises the same base-shortfall/blocked_out
    contract directly against an explicit `guard` (no HOME monkeypatching
    of any kind, beyond what `_home` already does for the literal `"~"`
    `resolve_query` itself calls) so a REAL regression here would still fail
    on any platform, including this one."""
    from fused_render.index.ignore import MountGuard

    guard = MountGuard(mounts_dir=str(tmp_path / "mounts"),
                       home_dirs=[_home + "/guarded"])
    blocked_out: list = []
    out = resolve_query("/box", "~/guarded/x.csv", guard=guard,
                        blocked_out=blocked_out)
    # base stops at the ordinary, unguarded ancestor (`_home`) — never at
    # `_home/guarded`, the tree the guard actually names.
    assert out == {"base": _home, "pattern": "guarded/x.csv", "mode": "substring"}
    # ...but the blocked candidate is still captured, string-only, no
    # further syscall paid to get it.
    assert blocked_out == [_home + "/guarded"]


def test_resolve_with_no_guard_behaves_exactly_as_before(_home):
    """The default (`guard=None`) must preserve today's behaviour exactly —
    every existing caller of `resolve_query` omits it."""
    out = resolve_query("/box", "~/a/b/*.c")
    assert out == {"base": _home + "/a/b", "pattern": "*.c", "mode": "glob"}


def test_resolve_an_uncancelled_token_changes_nothing(_home):
    """`token=CancelToken()` (never cancelled) must resolve identically to
    `token=None` — every existing caller of `resolve_query`/`_walk_from`
    omits it, so an uncancelled token cannot be allowed to change the
    walk's outcome."""
    token = CancelToken()
    with_token = resolve_query("/box", "~/a/b/*.c", token=token)
    without_token = resolve_query("/box", "~/a/b/*.c")
    assert with_token == without_token


def test_resolve_a_token_cancelled_before_the_call_raises_promptly(_home):
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        resolve_query("/box", "~/a/b/*.c", token=token)


def test_walk_from_a_token_cancelled_before_the_call_raises_promptly(_home):
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        index_query._walk_from(_home, "a/b", token=token)


def test_resolve_cancels_between_segments_not_mid_segment(_home, monkeypatch):
    """The token is checked BETWEEN segments, before each `os.path.isdir` —
    never during one, since a single `isdir` call cannot be interrupted
    (this is the walk's honest, documented limit). Cancelling the token from
    inside the FIRST `isdir` call must still let that first segment finish
    (`a` gets consumed) and only stop the walk before the second
    (`b`'s `isdir` must never run)."""
    token = CancelToken()
    real_isdir = os.path.isdir

    def fake_isdir(p, _r=real_isdir):
        if str(p).rstrip("/").endswith("/a"):
            result = _r(p)
            token.cancel()
            return result
        pytest.fail(f"os.path.isdir ran on {p} after cancellation")

    monkeypatch.setattr(os.path, "isdir", fake_isdir)
    with pytest.raises(Cancelled):
        resolve_query("/box", "~/a/b/*.c", token=token)


def test_resolve_with_no_token_behaves_exactly_as_before(_home):
    """The default (`token=None`) must preserve today's behaviour exactly —
    every existing caller of `resolve_query` omits it."""
    out = resolve_query("/box", "~/a/b/*.c")
    assert out == {"base": _home + "/a/b", "pattern": "*.c", "mode": "glob"}


def test_resolve_absolute_path_walks_the_filesystem(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    out = resolve_query("/box", f"{etc}/*/x.conf")
    assert out == {"base": norm(str(etc)), "pattern": "*/x.conf", "mode": "glob"}


def test_resolve_windows_drive_letter_path_walks_the_filesystem(monkeypatch):
    """A raw typed string can start with a drive letter instead of `/` (a
    pasted or typed Windows absolute path) — unambiguously absolute, unlike a
    bare leading `/`, so there is no depth-1-anchor fallback to consider.

    Exercised against a faked directory tree (`os.path.isdir` monkeypatched)
    rather than a real one: a Windows drive letter has no counterpart on the
    POSIX filesystem this suite otherwise runs against, real or via tmp_path,
    so this is the only way to run `_walk_from`'s directory-existence walk
    over one on any platform."""
    real_dirs = {"C:/Users", "C:/Users/example"}
    monkeypatch.setattr(os.path, "isdir", lambda p: p in real_dirs)
    out = resolve_query("/box", "C:\\Users\\example\\*.conf")
    # The implicit `**/` decision reads the RAW typed string looking for `/`
    # specifically (spec: "a slash is the only thing that limits depth") —
    # an all-backslash Windows path has none, so it widens to any depth under
    # the resolved base exactly like a slash-free POSIX query does.
    assert out == {"base": "C:/Users/example", "pattern": "**/*.conf",
                   "mode": "glob"}


def test_resolve_windows_drive_letter_path_with_forward_slashes(monkeypatch):
    """The same absolute-path recognition fires whichever separator the
    drive-letter string uses past the colon — `C:/` is as legitimate a
    Windows spelling as `C:\\`."""
    real_dirs = {"C:/Users", "C:/Users/example"}
    monkeypatch.setattr(os.path, "isdir", lambda p: p in real_dirs)
    out = resolve_query("/box", "C:/Users/example/*.conf")
    assert out == {"base": "C:/Users/example", "pattern": "*.conf",
                   "mode": "glob"}


def test_resolve_windows_drive_letter_path_with_a_space_still_recognized(monkeypatch):
    """Code review finding: `expand_whitespace_query` used to run BEFORE
    `_DRIVE_ABS`'s check, so a drive path with a space in one of its
    segments (`C:\\My Files\\rep`) got whitespace-collapsed while its
    backslashes were still backslashes — the collapse treated the whole
    string as one opaque segment (no `/` in it yet), wrapped a leading `*`
    onto the front, and destroyed the `C:` prefix `_DRIVE_ABS` looks for —
    the path silently stopped resolving as absolute at all (it would fall
    through to being treated as a bare relative query instead). `_DRIVE_ABS`
    must be checked, and backslashes folded to `/`, before
    `expand_whitespace_query` ever runs, so the drive prefix survives and
    this still resolves as a Windows absolute path — even though the walk
    itself cannot advance PAST a segment the whitespace collapse turned into
    a glob (`My Files` -> `My*Files`; `_walk_from` only walks literal,
    `*`-free segments), so `base` stays at the drive root and the glob-ified
    rest becomes the pattern instead."""
    real_dirs = {"C:/My Files", "C:/My Files/rep"}
    monkeypatch.setattr(os.path, "isdir", lambda p: p in real_dirs)
    out = resolve_query("/box", "C:\\My Files\\rep")
    # The all-backslash typed string has no literal "/" in it (spec: "a
    # slash is the only thing that limits depth"), so it also widens to any
    # depth the same as `test_resolve_windows_drive_letter_path_walks_the_
    # filesystem` above — this decision is read from the RAW typed string,
    # not from the "/"-joined form the drive-normalization step produces.
    assert out == {"base": "C:/", "pattern": "**/My*Files/*rep*", "mode": "glob"}


def test_resolve_windows_drive_letter_path_with_a_space_only_in_the_pattern(monkeypatch):
    """When the space is confined to the segment PAST the real directories
    (the ones `_walk_from` can still walk literally), the walk advances all
    the way to `example`, and the space-as-wildcard grammar applies only to
    what's left over as the pattern."""
    real_dirs = {"C:/Users", "C:/Users/example"}
    monkeypatch.setattr(os.path, "isdir", lambda p: p in real_dirs)
    out = resolve_query("/box", "C:\\Users\\example\\hello world")
    # Same all-backslash-typed-string widening as the test above.
    assert out == {"base": "C:/Users/example", "pattern": "**/*hello*world*",
                   "mode": "glob"}


def test_resolve_bare_windows_drive_root_backslash_normalizes_to_slash_form():
    """A drive letter with nothing after the separator (`C:\\`, the whole
    drive) walks no segments at all — `_walk_from`'s own bare-root collapse
    (`rest` empty) leaves `base` as the bare `"C:"` `_BARE_DRIVE` matches,
    which has to come back out as `"C:/"`: canonical_root() (index/runner.py)
    stores this drive under `"C:/"`, never the bare `"C:"` spelling."""
    out = resolve_query("/box", "C:\\")
    assert out == {"base": "C:/", "pattern": "", "mode": "substring"}


def test_resolve_bare_windows_drive_root_forward_slash_normalizes_to_slash_form():
    """The forward-slash spelling of the same bare drive root."""
    out = resolve_query("/box", "C:/")
    assert out == {"base": "C:/", "pattern": "", "mode": "substring"}


def test_resolve_relative_dotdot_walks_up_the_filesystem(_home):
    """A bare relative query with a `..` segment escapes the box's own root
    the same way `~` and a leading `/` already do — it does not stay
    anchored at `root` matching the literal string `..` against an index
    that never stores that segment."""
    box = _home + "/a/b"
    out = resolve_query(box, "../*.c")
    assert out == {"base": _home + "/a", "pattern": "*.c", "mode": "glob"}


def test_resolve_relative_dotdot_can_walk_back_to_where_it_started(_home):
    box = _home + "/a/b"
    out = resolve_query(box, "../../a/b/*.c")
    assert out == {"base": box, "pattern": "*.c", "mode": "glob"}


def test_resolve_relative_dotdot_to_a_missing_folder_widens_instead_of_failing(_home):
    box = _home + "/a/b"
    out = resolve_query(box, "../nope/x.csv")
    assert out == {"base": _home + "/a", "pattern": "nope/x.csv",
                   "mode": "substring"}


def test_resolve_relative_dotdot_clamps_at_the_filesystem_root(monkeypatch):
    """A run of `..` longer than the tree is deep keeps landing on real
    directories the whole way (`/..` is `/`, same as `cd`), so the walk
    never manufactures a fictitious base — it just stops climbing once it
    is at the root, same as every other consumed segment."""
    real_dirs = {"/", "/etc"}
    monkeypatch.setattr(
        os.path, "isdir",
        # The fake filesystem is described in POSIX terms, so it has to be
        # collapsed with POSIX rules too — `os.path.normpath` follows the
        # host platform's separator conventions and would fold "/.." into
        # "\\etc"-shaped strings on Windows, matching nothing in `real_dirs`.
        lambda p: posixpath.normpath(p) in real_dirs)
    out = resolve_query("/", "../../../etc/*.conf")
    assert out == {"base": "/etc", "pattern": "*.conf", "mode": "glob"}


def test_resolve_leading_slash_with_no_real_directory_stays_anchored():
    """The disambiguation's other branch: a leading `/` whose first segment
    is not a real directory (here, none of `/nonexistent-xyz` exists) is read
    as the depth-1 anchor, not an absolute path."""
    out = resolve_query("/box", "/nonexistent-xyz/*.csv")
    assert out == {"base": "/box", "pattern": "nonexistent-xyz/*.csv",
                   "mode": "glob"}


# -- stats ---------------------------------------------------------------------

def test_stats_totals_without_breakdown_by_default(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.txt", "/r/c.bin"],
                 sizes={"/r/a.txt": 1, "/r/b.txt": 2, "/r/c.bin": 100})
    out = stats(cfg)
    assert out["rows"] == 3
    assert out["total_size"] == 103
    assert out["last_root"] == "/r"
    assert out["types"] == []


def test_stats_extension_breakdown_when_asked(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.txt", "/r/c.bin"],
                 sizes={"/r/a.txt": 1, "/r/b.txt": 2, "/r/c.bin": 100})
    out = stats(cfg, breakdown=True)
    assert out["rows"] == 3
    assert out["total_size"] == 103
    assert out["types"][0] == {"ext": "bin", "n": 1, "size": 100}


def test_stats_is_scoped_to_one_subtree(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    compact_cfg = _index(tmp_path, "/other", ["/other/b.txt"])
    assert stats(compact_cfg, root="/r")["rows"] == 1
    assert stats(compact_cfg, root="/other")["rows"] == 1


def test_stats_on_an_empty_index(tmp_path):
    out = stats(_cfg(tmp_path))
    assert out["empty"] is True
    assert out["location"] == str(tmp_path / "ix")


def test_there_is_no_raw_sql_surface():
    """The `sql` action was fine inside a trusted local page and is not fine
    behind an HTTP route: duckdb with unrestricted SQL reads and writes
    anything the user's account can. The guarded replacement is a separate
    module on purpose, so this assertion keeps meaning what it meant."""
    assert not hasattr(index_query, "sql")
    assert "sql" not in dir(index_query)


def test_stats_does_not_count_a_lookalike_underscore_sibling(tmp_path):
    """`_` matches any char in LIKE: stats for /x/my_dir must not include
    /x/my-dir's rows (the subtree prefix is escaped)."""
    _index(tmp_path, "/x/my_dir", ["/x/my_dir/real.txt"])
    cfg = _index(tmp_path, "/x/my-dir", ["/x/my-dir/fake.txt"])
    assert stats(cfg, root="/x/my_dir")["rows"] == 1


# -- cancellation: a `token` handed to stats -----------------------------------
#
# `stats` follows `search_ranked`'s contract exactly (see
# tests/test_index_search.py's "cancellation: a `token` handed to
# search_ranked" section for the full case list against the reference
# implementation) — bind right after connect, `token.check()` before each
# real query, an InterruptException attributed to this token becomes
# `Cancelled`. These are scoped to stats's own wrinkle: two possible query
# sites (`n_dirs`, and `by_ext` only when `breakdown=True`).

def test_stats_an_uncancelled_token_changes_nothing(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.bin"],
                sizes={"/r/a.txt": 1, "/r/b.bin": 2})
    token = CancelToken()
    assert stats(cfg, breakdown=True, token=token) == stats(cfg, breakdown=True)


def test_stats_a_token_cancelled_before_the_call_raises_promptly(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        stats(cfg, token=token)


def test_stats_an_interrupt_during_the_breakdown_becomes_cancelled(tmp_path, monkeypatch):
    import duckdb as duckdb_module

    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.bin"])
    token = CancelToken()

    class _SpyConnection:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if "GROUP BY" in sql:
                token.cancel()
                raise duckdb_module.InterruptException("simulated cross-thread interrupt")
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = duckdb_module.connect
    monkeypatch.setattr(duckdb_module, "connect",
                        lambda *a, **kw: _SpyConnection(real_connect(*a, **kw)))
    with pytest.raises(Cancelled):
        stats(cfg, breakdown=True, token=token)


def test_stats_an_interrupt_with_no_token_is_not_swallowed(tmp_path, monkeypatch):
    import duckdb as duckdb_module

    cfg = _index(tmp_path, "/r", ["/r/a.txt", "/r/b.bin"])

    class _SpyConnection:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if "GROUP BY" in sql:
                raise duckdb_module.InterruptException("simulated, not this token's doing")
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = duckdb_module.connect
    monkeypatch.setattr(duckdb_module, "connect",
                        lambda *a, **kw: _SpyConnection(real_connect(*a, **kw)))
    with pytest.raises(duckdb_module.InterruptException):
        stats(cfg, breakdown=True)


def test_stats_a_cancel_after_return_does_not_touch_the_closed_connection(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    token = CancelToken()
    stats(cfg, token=token)
    # Must not raise.
    token.cancel()
    assert token.cancelled is True

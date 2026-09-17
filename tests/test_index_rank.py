"""Index-backed ranking, entirely in SQL.

`fused_render/index/rank.py` — the line-for-line Python port of the browser
ranker (`frontend/src/platform/lib/fuzzy.ts`) — is DELETED. Index-backed
search is substring-only now (an owner-accepted feature loss: the fuzzy
subsequence escalation `rank.py` used to fall back to is gone, so
`indexstore` no longer matches `index/specs/index-store.md` on an indexed
folder), and the whole filter/score/order/cut lives in one SQL statement
(`query.py`'s `_rank_sql`, called from `search_ranked`). `fuzzy.ts` itself is
UNCHANGED — it still ranks the live streamed walk for folders no scan will
ever cover, so `tests/fixtures/rank-parity.json` (generated from `fuzzy.ts`,
`bun scripts/gen-rank-fixture.ts`) is still the authority this file pins
against, restricted to the rows a SUBSTRING match can reach: a substring hit
always has `longest_run = len(q)`, the maximum a subsequence-only hit can
never reach, so `rank_compare`/`_sort_key` always ranked every substring hit
ahead of every subsequence-only one — dropping the subsequence pass removes a
distinct TAIL, it does not reorder what remains. The mirror-image JS test
(frontend/src/apps/explorer/listing/rank-parity.test.ts) is untouched and
still tests `fuzzy.ts` in full, subsequence pass included.

`query_wants_hidden`/`is_hidden_rel` moved from the deleted `rank.py` into
`query.py` itself (its only importer) — covered here since they still gate
which rows `search_ranked` can return.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index.config import IndexConfig
from fused_render.index.query import (
    MAX_GLOB_RANK_LIMIT,
    MAX_RANK_LIMIT,
    search_ranked,
)
from fused_render.index.runner import canonical_root
from fused_render.index.store import Sink, compact


def _index(tmp_path, root, files, dirs=()):
    """Build a real index over `paths` (absolute, already canonical). Same
    helper as test_index_search.py's `_index` — see that module's docstring
    on it for why every stored key is routed through `canonical_root`."""
    cfg = IndexConfig(dir=str(tmp_path / "ix"))
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    root = canonical_root(root)
    dirs = [canonical_root(d) for d in dirs]
    by_dir = {d: [] for d in dirs}
    by_dir.setdefault(root, [])
    for i, raw in enumerate(files):
        p = canonical_root(raw)
        d, name = p.rsplit("/", 1)
        ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
        by_dir.setdefault(d, []).append((p, d, name, ext, 10 + i, 100.0 + i))
    for d, rows in by_dir.items():
        sink.add(d, "s", ("sig", rows, sum(r[4] for r in rows), 1_000_000_000, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


# -- the golden corpus (replaces tests/fixtures/rank-parity.json) ----------
#
# `tests/fixtures/rank-parity.json` (a flat 64-entry corpus + 25 queries,
# generated FROM `frontend/src/platform/lib/fuzzy.ts` by
# `bun scripts/gen-rank-fixture.ts`) and its consuming test
# (`test_sql_ranking_matches_the_js_ranker_on_substring_hits`) are DELETED,
# not merely unused. Two independent reasons, either alone sufficient:
#
# 1. `scripts/gen-rank-fixture.ts` imports a module that no longer exists
#    (the JS-side rank-parity harness was cut along with the pieces of
#    `fuzzy.ts` this round's SQL rewrite has no equivalent for), so the
#    fixture can never be regenerated — keeping the stale JSON around would
#    make it a silently-frozen snapshot of a ranker `query.py` no longer
#    implements, not a living contract.
# 2. Even if it could still be regenerated, comparing SQL's order against
#    `fuzzy.ts`'s is no longer the right authority for the SUBSET of
#    behavior this round changed. `fuzzy.ts` never had a `_TAIL_BONUS`, a
#    2-vs-3-tier collapse, or a dropped camelCase-hump bonus — that parity
#    test was already only checking `_rank_sql`'s PORTED pieces
#    (`_is_segment_start`, `_name_tier`, `_sort_key`'s run/depth terms)
#    against their JS originals, and this round intentionally diverges from
#    several of those originals (see `_lex_order_and_score`'s docstring).
#    Continuing to grade `query.py` against `fuzzy.ts` on the exact
#    dimensions this round deliberately changed would fail by design, not
#    by regression. See DECISIONS.md.
#
# The golden queries below replace it: HAND-REASONED (not captured from
# whatever the implementation happens to emit) expected top orders, derived
# directly from `_lex_order_and_score`'s documented column vector
# (`nm_exact`, `prefix`, `suffix`, `contains`, `boundary`, `depth`,
# `length(nm)`, `lower(rel)`, `rel`) rather than from any other ranker's
# output. Each
# query's assertion is filtered to an explicit ALLOWLIST of the paths that
# query is about (the same technique the deleted fixture test used via its
# own `fixture_rels` filter) rather than asserting on the full result set,
# because a real on-disk index — unlike a flat list — structurally creates a
# dirs-table row for every ANCESTOR directory a stored file implies
# (`index/store.py`'s `Sink.add`, one call per directory that holds files),
# and some of those implied rows are themselves real, correctly-ranked
# matches for a query (a directory literally named `js` is a legitimate
# EXACT match for query "js") that would make a full-result-set comparison
# fragile and beside the point of what each query below is testing.
_NOISE = [f"noise/d{i}/f{i}.dat" for i in range(280)]
_GROUPS = {
    # The `js`/`json` extension-vs-substring probe (search-architecture-
    # review.md's own worked example, §10.1): a bare `js` query used to grade
    # `lib/app.js`, `js/lib/app.js` and `json/script.js` off whichever
    # occurrence `strpos` found FIRST, tying two of them at the same score
    # purely because their first "js" happens to sit in a directory segment.
    "js": [
        "g_ext/src/app.js",
        "g_ext/node_modules/react.js/dist/bundle.js",
        "g_probe/lib/app.js",
        "g_probe/js/lib/app.js",
        "g_probe/json/script.js",
    ],
    "config": ["g_config/config.json", "g_config/app-config"],
    "readme": ["g_readme/README.md", "g_readme/docs/README.md",
               "g_readme/a/b/c/README.md"],
    "index": ["g_index/index", "g_index/sub/index.md", "g_index/sub2/index.js"],
    "alpha": ["g_alpha/alpha.txt", "g_alpha/a/b/c/d/e/f/alpha-deep.txt"],
}
_GOLDEN_EXPECTED = {
    # query -> expected order, filtered to _GROUPS[query]
    #
    # "js": all five basenames end with "js" (suffix=True) and contain it
    # (contains=True) — none is a prefix or exact match — so every row ties
    # on the four leading predicate columns and `depth ASC` decides first:
    # the three depth-3 files (`app.js` x2, `script.js`) sort before the
    # depth-4 `js/lib/app.js` before the depth-5 `node_modules` bundle. The
    # depth-3 trio then breaks on `length(nm) ASC` ("app.js" — 6 chars —
    # before "script.js" — 9 chars), and the app.js/app.js tie breaks on
    # `lower(rel) ASC` ("g_ext/..." sorts before "g_probe/..."). This is the
    # exact reordering search-architecture-review.md's probe called out as
    # missing: the shallow `lib/app.js` files now correctly outrank both the
    # `.json`-lookalike-named directory hit AND the ancestor-named `js/`
    # decoy, instead of tying with them.
    "js": ["g_ext/src/app.js", "g_probe/lib/app.js", "g_probe/json/script.js",
           "g_probe/js/lib/app.js",
           "g_ext/node_modules/react.js/dist/bundle.js"],
    # "config": `config.json` is a basename PREFIX match (+500); `app-config`
    # is a basename SUFFIX match only (+250) — the reported regression this
    # round exists to fix (`3622523ad`'s `_TAIL_BONUS` tied these at the same
    # score because it weighted a tail match exactly as heavily as a prefix
    # one).
    "config": ["g_config/config.json", "g_config/app-config"],
    # "readme": all three are basename-prefix matches with an identical `nm`
    # LENGTH ("README.md" folds to the same 9-character `nm` regardless of
    # depth), so `depth ASC` alone orders them shallowest first.
    "readme": ["g_readme/README.md", "g_readme/docs/README.md",
               "g_readme/a/b/c/README.md"],
    # "index": `g_index/index` is an EXACT basename match (nm == "index"),
    # the one predicate column no other candidate here can share — it wins
    # regardless of depth. The remaining two tie on prefix/contains and on
    # `length(nm)` ("index.md"/"index.js" are both 8 characters), so
    # `lower(rel) ASC` decides: "g_index/sub/index.md" sorts before
    # "g_index/sub2/index.js" because `/` (0x2F) sorts before `2` (0x32) at
    # the first differing byte.
    "index": ["g_index/index", "g_index/sub/index.md", "g_index/sub2/index.js"],
    # "alpha": both are basename-prefix matches with the same predicate
    # profile, so `depth ASC` alone separates the shallow file from the one
    # nested six directories deeper.
    "alpha": ["g_alpha/alpha.txt", "g_alpha/a/b/c/d/e/f/alpha-deep.txt"],
}


def _golden_index(tmp_path):
    files = list(_NOISE)
    for group in _GROUPS.values():
        files.extend(group)
    return _index(tmp_path, "/r", [f"/r/{f}" for f in files])


@pytest.mark.parametrize("query", sorted(_GROUPS))
def test_golden_corpus_pins_the_hand_reasoned_top_order(tmp_path, query):
    cfg = _golden_index(tmp_path)
    allowed = set(_GROUPS[query])
    out = search_ranked(cfg, "/r", query, limit=200)
    got = [h["rel"] for h in out["hits"] if h["rel"] in allowed]
    assert got == _GOLDEN_EXPECTED[query]


def test_golden_corpus_noise_never_leaks_into_a_targeted_query(tmp_path):
    """None of the 280 noise paths (`noise/dN/fN.dat`) contain any of the
    golden queries' substrings — a sanity check on the corpus itself, so a
    query's allowlist-filtered assertion above is provably not silently
    passing because unrelated noise rows swamped the real candidates."""
    cfg = _golden_index(tmp_path)
    for query in _GROUPS:
        out = search_ranked(cfg, "/r", query, limit=1000)
        noisy = [h["rel"] for h in out["hits"] if h["rel"].startswith("noise/")]
        assert noisy == []


def test_an_empty_query_ranks_nothing(tmp_path):
    cfg = _golden_index(tmp_path)
    assert search_ranked(cfg, "/r", "")["hits"] == []
    assert search_ranked(cfg, "/r", "   ")["hits"] == []


def test_hits_carry_the_wire_fields(tmp_path):
    """The endpoint returns these fields verbatim to the client
    (listing/ranked-hits.ts's `IndexRankHit`); the SQL rewrite must not drop
    any of them."""
    cfg = _index(tmp_path, "/r", ["/r/environment.yml"])
    [hit] = search_ranked(cfg, "/r", "environment.yml")["hits"]
    assert hit["rel"] == "environment.yml"
    assert hit["is_dir"] is False
    assert hit["size"] == 10 and hit["mtime"] == 100.0
    assert hit["score"] > 100  # exact-name bonus applied
    assert hit["tier"] == 1 and hit["depth"] == 1
    assert hit["longest_run"] == len("environment.yml")


def test_the_query_is_lowercased_in_sql_to_agree_with_lower_rel(tmp_path):
    """`lrel` (the candidate filter's other side of `LIKE`) is `lower(rel)`,
    computed by DuckDB. If the query is instead lowercased in PYTHON
    (`qs.lower()`) before being embedded as a literal, the two sides can use
    DIFFERENT lowering rules for the same character and silently disagree.
    U+0130 (LATIN CAPITAL LETTER I WITH DOT ABOVE, 'İ') is a real case of
    this: Python's `str.lower()` folds it to two characters ('i' + a
    combining dot, U+0307), but DuckDB's `lower()` folds it to plain 'i' —
    so a query lowered in Python could never appear as a substring of a rel
    DuckDB lowered, even though the user's search for the "same" letter
    obviously should match. Lowering the query in SQL too (the same `lower()`
    call that already produces `lrel`) makes the two sides agree by
    construction, whichever way `lower()` happens to fold any given
    character."""
    cfg = _index(tmp_path, "/r", ["/r/uİmax.txt"])
    rels = [h["rel"] for h in search_ranked(cfg, "/r", "İ")["hits"]]
    assert rels == ["uİmax.txt"]


def test_hidden_entries_need_a_dot_leading_query_segment(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/.env", "/r/environment.yml"])
    rels = [h["rel"] for h in search_ranked(cfg, "/r", "env")["hits"]]
    assert ".env" not in rels and "environment.yml" in rels
    rels = [h["rel"] for h in search_ranked(cfg, "/r", ".env")["hits"]]
    assert ".env" in rels


def test_glob_mode_hides_dotfiles_the_same_way_rank_mode_does(tmp_path):
    """Code review finding: `_glob_sql` used to skip the hidden-file filter
    `_rank_sql` applies, so a multi-word glob query (which, per
    `expand_whitespace_query`, is any query with more than one whitespace-
    separated word) could surface dotfiles a single-word query on the same
    directory would hide. The rule has to be the SAME `query_wants_hidden`
    check in both modes — a glob query with no dot-leading segment must
    still hide `.env`, and one that does have a dot-leading segment must
    still be able to reach it, exactly like substring mode above."""
    cfg = _index(tmp_path, "/r", ["/r/.env", "/r/environment.yml"])
    rels = [h["rel"] for h in
            search_ranked(cfg, "/r", "*env*", glob=True)["hits"]]
    assert ".env" not in rels and "environment.yml" in rels
    rels = [h["rel"] for h in
            search_ranked(cfg, "/r", ".env*", glob=True)["hits"]]
    assert ".env" in rels


def test_case_only_ties_get_a_deterministic_final_order(tmp_path):
    """`notes/Alpha.txt` and `notes/alpha.txt` are a tie on every column
    `_rank_sql`'s ORDER BY had before `rel ASC` was added (tier, score,
    depth, and `lower(rel)` — the two rels are equal under `lower()`), so a
    multi-threaded top-N in DuckDB is free to resolve the tie arbitrarily and
    the pair could silently swap order between two otherwise-identical runs.
    `rel ASC` is a free, byte-comparable final tie-break that makes the whole
    ORDER BY total: run the same query several times and the order must not
    move, and it must match Python's own `sorted()` over the tied rels
    (case-sensitive, ASCII byte order — `"Alpha.txt" < "alpha.txt"` since
    `A` (0x41) sorts before `a` (0x61))."""
    cfg = _index(tmp_path, "/r", ["/r/notes/Alpha.txt", "/r/notes/alpha.txt",
                                  "/r/xutils.ts", "/r/Xutils.ts"])
    for _ in range(5):
        out = search_ranked(cfg, "/r", "alpha")
        rels = [h["rel"] for h in out["hits"] if "alpha" in h["rel"].lower()]
        assert rels == sorted(rels)
    for _ in range(5):
        out = search_ranked(cfg, "/r", "utils")
        rels = [h["rel"] for h in out["hits"] if "utils" in h["rel"].lower()]
        assert rels == sorted(rels)


# -- pinned directly: the pieces `_rank_sql` ports from the deleted rank.py --

def test_a_shallow_name_match_beats_a_deep_ancestor_only_match(tmp_path):
    """`tier` (1 = name match) sits above `score` in the ORDER BY — a tier-3
    (ancestor-only) hit must not outrank a tier-2 (fuzzy-on-the-name, here
    substring-on-the-name) hit just because its path is longer."""
    cfg = _index(tmp_path, "/r", ["/r/a/b/c/d/e/f/g/h/notes.txt",
                                  "/r/cfg-manager.txt"])
    out = search_ranked(cfg, "/r", "cfg")
    assert out["hits"][0]["rel"] == "cfg-manager.txt"


def test_depth_breaks_a_same_predicate_tie_by_shallowness(tmp_path):
    """Two ancestor-only (tier 3) hits whose basenames neither start with,
    end with, nor contain "xxxxxxxx" at all — every `_name_predicate_sql`
    column is `false` for both, so every ORDER BY column ahead of `depth`
    ties. `depth ASC` is what stops the deeper one from winning purely by
    having accumulated more path — the position-free redesign's replacement
    for the deleted `_DEPTH_PENALTY` arithmetic (search-architecture-
    review.md §6): a plain ordering column, not a subtracted constant, so
    there is no formula left to pin an exact number against — only the
    ORDER, and that `score` (a display-only weighted sum with no depth
    term stronger than `- depth`) is strictly higher for the shallower row
    purely because `depth` is smaller, not because of any predicate."""
    deep = "xxxxxxxx/xxxxxxxx/xxxxxxxx/xxxxxxxx/xxxxxxxx/deep.txt"  # depth 6
    shallow = "xxxxxxxx/shallow.txt"  # depth 2
    cfg = _index(tmp_path, "/r", [f"/r/{deep}", f"/r/{shallow}"])
    out = search_ranked(cfg, "/r", "xxxxxxxx")
    # Every "xxxxxxxx" ANCESTOR directory is itself an indexed, searchable
    # row (and an exact-name tier-1 match at that, for the same query) —
    # real, correct behaviour, just not what this test is isolating. Files
    # only.
    files = [h for h in out["hits"] if not h["is_dir"]]
    assert [h["rel"] for h in files] == [shallow, deep]
    assert files[0]["tier"] == files[1]["tier"] == 3
    by_rel = {h["rel"]: h for h in out["hits"]}
    # No predicate is true for either file's basename ("shallow.txt" / "
    # deep.txt" neither start with, end with, nor contain "xxxxxxxx"), so
    # `score` is exactly `-depth` for both.
    assert by_rel[shallow]["score"] == -2
    assert by_rel[deep]["score"] == -6


def test_score_never_inverts_the_real_order_at_depth(tmp_path):
    """Finding 4: `score` (the debug/display weighted sum) used to subtract
    `depth` UNBOUNDED, so at a large enough depth it could invert the real
    order — a `contains`-only basename match at depth 601 scored
    `100 - 601 = -501`, BELOW a same-tier ancestor-only match at depth 2
    scoring `0 - 2 = -2`, even though the real `ORDER BY` vector (`tier`/
    `contains` alone) ranks the basename match first. `_lex_order_and_score`
    now caps the subtracted term at `_SCORE_DEPTH_CAP` (99, strictly less
    than the smallest gap between adjacent predicate weights), so `score`
    can no longer invert the true order this way, though it remains a
    coarse display value, not a second ranking mechanism (see its
    docstring)."""
    deep_name = "/".join(["d"] * 601) + "/xxxconfigxxx.txt"  # depth 601,
                                                              # "config" mid-word
    shallow_ancestor = "config/unrelated.txt"                # depth 2,
                                                              # ancestor-only
    cfg = _index(tmp_path, "/r", [f"/r/{deep_name}", f"/r/{shallow_ancestor}"])
    hits = search_ranked(cfg, "/r", "config")["hits"]
    files = [h for h in hits if not h["is_dir"] and h["rel"] in
             (deep_name, shallow_ancestor)]
    by_rel = {h["rel"]: h for h in files}
    # Real order: the basename match (tier 1) outranks the ancestor-only
    # match (tier 3) regardless of depth.
    assert by_rel[deep_name]["tier"] == 1
    assert by_rel[shallow_ancestor]["tier"] == 3
    rels_in_order = [h["rel"] for h in hits if h["rel"] in
                     (deep_name, shallow_ancestor)]
    assert rels_in_order == [deep_name, shallow_ancestor]
    # The display `score` must agree with that real order, not invert it.
    assert by_rel[deep_name]["score"] > by_rel[shallow_ancestor]["score"]


def test_the_exact_name_bonus_survives_the_depth_penalty(tmp_path):
    """+100 for an exact basename match must still beat a shallow fuzzy
    (here: substring) match after DEPTH_PENALTY is subtracted."""
    cfg = _index(tmp_path, "/r", ["/r/a/b/c/d/e/f/g/h/exact-match.txt",
                                  "/r/s/fuzzy-only.txt"])
    out = search_ranked(cfg, "/r", "exact-match.txt")
    assert out["hits"][0]["rel"] == "a/b/c/d/e/f/g/h/exact-match.txt"


def test_the_prefix_name_bonus(tmp_path):
    """+25 for a basename PREFIX (not exact) match."""
    cfg = _index(tmp_path, "/r", ["/r/readme.md.bak", "/r/unrelated.txt"])
    out = search_ranked(cfg, "/r", "readme.md")
    assert out["hits"][0]["rel"] == "readme.md.bak"
    assert out["hits"][0]["score"] > 25  # base score plus the prefix bonus


def test_the_basename_suffix_bonus_ranks_a_tail_match_above_an_interior_one(
    tmp_path,
):
    """Substring-mode counterpart to the reported `*.js`-vs-`.json` bug
    (glob mode, see the two glob tests below): a match that reaches the END
    of the basename is exactly as good a signal as one that reaches its
    START (the existing +25 `name_bonus` prefix case) and deserves the same
    kind of credit, but nothing awarded it before this. Every OTHER term is
    made IDENTICAL between the two candidates on purpose (same run length,
    same zero segment-start count, same zero depth penalty since both are
    <= SHALLOW_FREE) so the only thing that can separate them is the new
    suffix bonus."""
    cfg = _index(tmp_path, "/r", [
        "/r/xxxjsxxx.txt",       # "js" interior, not at the basename's end
        "/r/d1/d2/d3/xxxjs",     # "js" is the LAST two chars of the basename
    ])
    files = search_ranked(cfg, "/r", "js")["hits"]
    rels = [h["rel"] for h in files]
    assert rels[0] == "d1/d2/d3/xxxjs"
    by_rel = {h["rel"]: h for h in files}
    assert by_rel["d1/d2/d3/xxxjs"]["score"] > by_rel["xxxjsxxx.txt"]["score"]


def test_the_basename_suffix_bonus_does_not_reorder_an_exact_match_below_a_tail_match(
    tmp_path,
):
    """The new suffix bonus must not be large enough to put a mere tail
    match ahead of a TRUE exact-basename match (+100) at the same depth —
    guards against picking a bonus so large it "swamps" the existing name
    bonuses the way the brief warns against."""
    cfg = _index(tmp_path, "/r", [
        "/r/config",           # exact basename match: gets +100 AND the new
                                # suffix bonus (an exact match also reaches
                                # the basename's end, by construction)
        "/r/app-config",       # tail match only: ends with "config" but is
                                # not equal to it
    ])
    out = search_ranked(cfg, "/r", "config")["hits"]
    assert [h["rel"] for h in out] == ["config", "app-config"]


def test_tier_1_and_3_boundaries_including_a_match_straddling_the_basename(
    tmp_path,
):
    """tier 1: the query is fully explainable within the basename alone
    (`_name_predicate_sql`'s `contains`). tier 3: everything else, including
    both an ancestor-only match AND a match that STRADDLES the `/` boundary.

    The old 3-tier scheme (1/2/3, `_name_tier` in the deleted rank.py) had a
    dedicated tier 2 for the straddle case ("oo/ba" against "foo/bar.txt",
    which starts inside "foo" and ends inside "bar.txt") because it read a
    single contiguous match window (`p0..p0+n`) and could ask "does this
    window cross the boundary". The position-free redesign has no window to
    ask that question of — `contains` is a pure existence test against `nm`
    alone, so a straddling match (which by definition needs characters
    OUTSIDE the basename) can never satisfy it, landing it in tier 3 with
    every other non-name match. This is a deliberate collapse, not a gap:
    `tier` is no longer a primary sort key (the lexicographic predicate
    vector ahead of it already separates match quality more finely — see
    `_lex_order_and_score`'s docstring), so the 3-way split had nothing left
    to buy that `contains` alone doesn't already provide. Recorded in
    DECISIONS.md."""
    cfg = _index(tmp_path, "/r", ["/r/name-has-alpha.txt",  # tier 1: "alpha" in name-has-alpha.txt
                                  "/r/alpha/unrelated.txt",  # tier 3: match ends before "unrelated.txt"
                                  "/r/foo/bar.txt"])         # tier 3: "oo/ba" straddles the "/"
    by_rel = {h["rel"]: h for h in search_ranked(cfg, "/r", "alpha")["hits"]}
    assert by_rel["name-has-alpha.txt"]["tier"] == 1
    assert by_rel["alpha/unrelated.txt"]["tier"] == 3
    straddle = search_ranked(cfg, "/r", "oo/ba")["hits"]
    assert [h["rel"] for h in straddle] == ["foo/bar.txt"]
    assert straddle[0]["tier"] == 3


def test_boundary_bonus_ranks_a_word_boundary_match_above_a_mid_word_one(
    tmp_path,
):
    """Finding 3: the old word-boundary/segment-start bonus was dropped
    without replacement, so two ties (same `contains`, same depth, same
    `length(nm)`) fell through to `lower(rel) ASC` — pure alphabetical
    order, not match quality. Reproduced exactly: `aaaconfig.py` (12 chars,
    "config" mid-word, right after another letter) sorted ahead of
    `zz_config.py` (12 chars, "config" right after a `_` separator) purely
    because `'a' < 'z'`. The new `boundary` predicate (a word/segment-start
    existence test, position-free) fixes this without reintroducing a
    position read."""
    cfg = _index(tmp_path, "/r", ["/r/aaaconfig.py", "/r/zz_config.py"])
    hits = search_ranked(cfg, "/r", "config")["hits"]
    assert [h["rel"] for h in hits] == ["zz_config.py", "aaaconfig.py"]


def test_boundary_predicate_escapes_regex_metacharacters(tmp_path):
    """The `boundary` predicate is regex-escaped (`re.escape`), not
    LIKE-escaped, because it is embedded in a `regexp_matches` pattern. A
    query containing a regex metacharacter (here `.`) must not have that
    character read back as "any character" — if it were, a DECOY substring
    elsewhere in a basename (one that only coincidentally resembles the
    boundary pattern once `.` is treated as a wildcard) could make an
    otherwise mid-word match look boundary-true.

    `xa.b_azb.txt` (basename, query "a.b"): the real, literal "a.b" occurs
    at "x[a.b]_azb.txt" — mid-word, preceded by "x", correctly
    boundary-false. But it also contains a decoy "_azb" — preceded by a
    real separator "_", then "a", then "z", then "b" — which an UNESCAPED
    "." (matching "any character") would misread as a boundary-true
    occurrence of "a.b". Compared against `_a.b_extra_padding_here.txt`
    (a genuine boundary-true match, deliberately made LONGER so that
    `length(nm) ASC` — the next tie-break after `boundary` — would favor
    the WRONG (decoy) candidate if `boundary` failed to separate them; only
    a correctly-escaped `boundary` predicate produces the right order here)."""
    decoy = "xa.b_azb.txt"                        # boundary-false (12 chars)
    genuine = "_a.b_extra_padding_here.txt"        # boundary-true (27 chars)
    cfg = _index(tmp_path, "/r", [f"/r/{decoy}", f"/r/{genuine}"])
    hits = search_ranked(cfg, "/r", "a.b")["hits"]
    rels = [h["rel"] for h in hits if h["rel"] in (decoy, genuine)]
    assert rels == [genuine, decoy]


def test_glob_final_segment_tier_fix_for_path_shaped_patterns(tmp_path):
    """Finding 2: `**/src/*.ts` used to be tier 3 for EVERY hit, no matter
    how good the basename match, because `_glob_literal_runs` ran on the
    WHOLE pattern produced `["src/", ".ts"]` — `"src/"` is a directory-
    segment literal that can never appear in `nm`, a slash-free basename,
    so `contains` (built by chaining every literal run) was false for every
    row (search-architecture-review.md §10.3). `_final_segment_pattern`
    fixes this by scoring only the pattern's FINAL segment (`.ts` here, from
    `*.ts`) against `nm`. Both a directly-matching file (`src/main.ts`) and
    one where "src" is merely an ANCESTOR directory several levels up
    (`a/b/src/deep.ts`) must come back tier 1: the fix is about what's
    tested against `nm`, not about requiring "src" to be the immediate
    parent."""
    cfg = _index(tmp_path, "/r", ["/r/src/main.ts", "/r/a/b/src/deep.ts"])
    hits = search_ranked(cfg, "/r", "**/src/*.ts**", glob=True)["hits"]
    by_rel = {h["rel"]: h for h in hits}
    assert by_rel["src/main.ts"]["tier"] == 1
    assert by_rel["a/b/src/deep.ts"]["tier"] == 1


def test_glob_final_segment_tier_ancestor_only_stays_tier_3(tmp_path):
    """Regression guard for the fix above: an ancestor-only glob whose FINAL
    segment is a bare `*` (`**/alpha/*`, matching "everything directly
    inside an `alpha` directory") has zero literal runs once confined to its
    final segment — there is nothing left to test against `nm` at all, so
    it must stay tier 3 (the unscored/no-literal-runs branch), not be
    accidentally promoted to tier 1 by the final-segment fix."""
    cfg = _index(tmp_path, "/r", ["/r/alpha/unrelated.txt"])
    hits = search_ranked(cfg, "/r", "**/alpha/*", glob=True)["hits"]
    by_rel = {h["rel"]: h for h in hits}
    assert by_rel["alpha/unrelated.txt"]["tier"] == 3


# The old camelCase-hump segment-start bonus (`_is_segment_start`, ported
# from the deleted rank.py) has no test here any more — it was DROPPED, not
# reimplemented in occurrence-independent form. It read the matched window's
# position against the ORIGINAL-case `rel` (`substr(rel, i, 1)` at the
# match's own start/end), which is exactly the "which occurrence" question
# this round's redesign exists to eliminate, and `nm` — the only column
# every remaining predicate is built from — is stored already-lowercased, so
# recovering case information for a hump test would need a brand new
# original-case basename column. Left out as an explicit, reported
# deviation from the old ranker's feature set rather than reintroduced as
# another positional read. See DECISIONS.md and this round's report.


def test_no_more_than_limit_rows_come_back_from_the_database(tmp_path):
    files = [f"/r/alpha-{i}.txt" for i in range(50)]
    cfg = _index(tmp_path, "/r", files)
    out = search_ranked(cfg, "/r", "alpha", limit=10)
    assert len(out["hits"]) == 10
    assert out["truncated"] is True
    assert out["total"] == 10


def test_glob_mode_clamps_to_its_own_wider_ceiling(tmp_path):
    files = [f"/r/alpha-{i}.txt" for i in range(50)]
    cfg = _index(tmp_path, "/r", files)
    # A substring request asking past MAX_RANK_LIMIT is clamped to it...
    out = search_ranked(cfg, "/r", "alpha", limit=MAX_GLOB_RANK_LIMIT)
    assert len(out["hits"]) == min(50, MAX_RANK_LIMIT)
    # ...but a glob request for the SAME count of matches is not: every glob
    # hit is an equal match with no tail to trim, so its own ceiling is wider.
    out = search_ranked(cfg, "/r", "*.txt", limit=MAX_GLOB_RANK_LIMIT, glob=True)
    assert len(out["hits"]) == 50
    assert out["truncated"] is False


# -- glob mode is ranked too (Part B) --------------------------------------

def test_glob_ranking_a_tight_match_beats_a_long_wildcard_swallow(tmp_path):
    """`icon copy.png` (a `**icon**copy**` glob's stars swallowing almost
    nothing) must outrank `icon-a-very-long-thing-copy.png` (the same two
    literal runs, `icon` and `copy`, both present, but with the stars
    swallowing 18 extra characters between them). Every OTHER scoring term
    (the two runs' own `n + 3*(n-1)`, their segment-start count — both
    filenames start a run right at position 0 and right after a separator —
    and the basename-prefix name bonus for `icon`) is IDENTICAL between the
    two candidates, so this only passes if the wildcard-swallow penalty is
    actually doing something: without it, this pair would tie and the tie-
    break (`rel ASC`) would put the LONGER name first, which is backwards."""
    cfg = _index(tmp_path, "/r", [
        "/r/icon copy.png", "/r/icon-a-very-long-thing-copy.png"])
    hits = search_ranked(cfg, "/r", "**/**icon**copy**", glob=True)["hits"]
    assert [h["rel"] for h in hits] == [
        "icon copy.png", "icon-a-very-long-thing-copy.png"]


def test_glob_ranked_hits_carry_the_wire_fields_and_order_by_length(tmp_path):
    """Ranked glob hits carry the same key set (`rel`, `is_dir`, `size`,
    `mtime`, `score`, `longest_run`, `tier`, `depth`) the other two modes'
    hits do, and `tier` is no longer a fixed `0` placeholder — both basenames
    here contain both literal runs (`icon`, `copy`), so both are tier 1, same
    rule `_rank_sql` uses (query is fully explainable within the basename).

    `icon copy.png` and `icon-a-very-long-thing-copy.png` tie on every
    `_name_predicate_sql` column (both start with "icon", neither ends with
    "copy", both contain the literal chain) — under the position-free
    redesign there is no wildcard-swallow penalty left to break that tie on
    `score` (deliberately: matched length/span is not a quality signal for a
    glob, per the deleted `_glob_score_sql`'s own reasoning, which this
    round keeps but implements as an absence of a term rather than a
    penalty), so the two DO legitimately share a `score` now. The order
    still comes out right — `icon copy.png` first — via `length(nm) ASC`,
    the next column in `_lex_order_and_score`'s vector after the tied
    predicates: this is the load-bearing proof that ORDER is decided by the
    vector, not by `score DESC`, exactly as `_lex_order_and_score`'s
    docstring warns a caller not to assume."""
    cfg = _index(tmp_path, "/r", ["/r/icon copy.png",
                                  "/r/icon-a-very-long-thing-copy.png"])
    hits = search_ranked(cfg, "/r", "**/**icon**copy**", glob=True)["hits"]
    assert len(hits) == 2
    assert [h["rel"] for h in hits] == [
        "icon copy.png", "icon-a-very-long-thing-copy.png"]
    assert hits[0]["score"] == hits[1]["score"]  # tied on every predicate
    for h in hits:
        assert set(h) == {"rel", "is_dir", "size", "mtime",
                          "score", "longest_run", "tier", "depth"}
        assert h["tier"] == 1


def test_glob_single_literal_run_score_matches_rank_sql_substring_score(tmp_path):
    """The whole point of confining the swallow penalty to INTERIOR gaps: a
    pattern with exactly ONE literal run has no interior gap at all (the
    leading/trailing `**` cross no other literal), so its glob score must be
    IDENTICAL to `_rank_sql`'s score for the equivalent substring query on
    the very same rows — not merely close. Includes a folder-name collision
    (`icons/` itself is a candidate row since every folder holding files gets
    its own dirs-table row — the fixture gotcha) which is excluded, since its
    own exact-name +100 bonus would dominate both real rows if left in."""
    cfg = _index(tmp_path, "/r", [
        "/r/icon.png",
        "/r/src/icon.png",
        "/r/icons/scratch/tmpfile-zzzz.txt",
        "/r/Projects/website/assets/images/branding/icon.png",
    ])
    substring_hits = {h["rel"]: h["score"]
                      for h in search_ranked(cfg, "/r", "icon")["hits"]}
    glob_hits = {h["rel"]: h["score"]
                 for h in search_ranked(cfg, "/r", "**icon**", glob=True)["hits"]
                 if h["rel"] != "icons"}
    assert glob_hits  # sanity: rows actually matched
    assert glob_hits.keys() == substring_hits.keys()
    for rel, score in glob_hits.items():
        assert score == substring_hits[rel], rel


def test_glob_reported_bug_extension_match_beats_a_json_file_that_merely_contains_js(
    tmp_path,
):
    """The reported bug: typing `*.js` (which resolves to the glob pattern
    `**/*.js**`, a SINGLE literal run `[".js"]`) used to rank `.json` files
    above real `.js` files. `.json` starts with the literal ".js", so it is
    an equally good match for that one literal run as an actual `.js`
    extension is — same run length, same tier (both are substrings of their
    own basename) — and with only one literal run the interior-swallow
    penalty is always 0 (nothing is "between" a single run), so nothing
    differentiated them except the depth tie-break, which favored the
    shallower `.json` files. A bonus for the run reaching the actual END of
    the basename (true only for the real `.js` file, never for `.json`,
    since `.json` has two more characters after the matched `.js`) fixes
    it."""
    cfg = _index(tmp_path, "/r", [
        "/r/Downloads.json",
        "/r/Work.json",
        "/r/Downloads/canvas_39.json",
        "/r/Downloads/Archive/script.js",
    ])
    hits = search_ranked(cfg, "/r", "**/*.js**", glob=True)["hits"]
    files = [h["rel"] for h in hits if not h["is_dir"]]
    assert files[0] == "Downloads/Archive/script.js"


def test_glob_suffix_bonus_does_not_reorder_an_exact_match_below_a_tail_match(
    tmp_path,
):
    """Glob-mode counterpart of the substring-mode guard test above: the
    bonus must not swamp a true exact-basename match even when both
    candidates satisfy the new suffix condition."""
    cfg = _index(tmp_path, "/r", [
        "/r/config",       # exact basename match for "config"
        "/r/app-config",   # tail match only
    ])
    hits = search_ranked(cfg, "/r", "**config**", glob=True)["hits"]
    assert [h["rel"] for h in hits] == ["config", "app-config"]


def test_glob_tier_restores_correct_order_over_the_swallow_penalty(tmp_path):
    """Before the interior-only fix, the swallow penalty was charged over the
    WHOLE root-relative path, so a shallow, non-exact match
    (`xreport.txt`) could outrank a deep, EXACT basename match
    (`deeply/nested/path/report`) purely because the deep one's path is
    longer. With the penalty confined to interior gaps (zero here — one
    literal run), the exact-basename `name_bonus` decides it correctly."""
    cfg = _index(tmp_path, "/r", [
        "/r/xreport.txt",
        "/r/deeply/nested/path/report",
    ])
    hits = search_ranked(cfg, "/r", "**report**", glob=True)["hits"]
    assert [h["rel"] for h in hits] == [
        "deeply/nested/path/report", "xreport.txt"]


def test_glob_tier_generalization_ancestor_only_ranks_below_a_name_match(tmp_path):
    """`tier` is restored as the PRIMARY sort key for glob hits too, computed
    by running the resolved pattern's regex against `nm` (the basename)
    instead of `lrel`: a match there is tier 1, else tier 3 — same two
    values `_rank_sql` uses for "in the name" vs "ancestor only". A tier-3
    ancestor-only hit must never outrank a tier-1 name match, however the raw
    score compares. `alpha/` itself is excluded from the result: it holds a
    file, so it gets its own row in the dirs table (`Sink.add`), and its own
    exact-name +100 bonus (tier 1 too) would otherwise sit in the results
    without being the pair this test means to compare."""
    cfg = _index(tmp_path, "/r", [
        "/r/name-has-alpha.txt",   # tier 1: "alpha" is in the basename
        "/r/alpha/unrelated.txt",  # tier 3: match is ancestor-only
    ])
    all_hits = search_ranked(cfg, "/r", "**alpha**", glob=True)["hits"]
    hits = [h for h in all_hits if h["rel"] != "alpha"]
    by_rel = {h["rel"]: h for h in hits}
    assert by_rel["name-has-alpha.txt"]["tier"] == 1
    assert by_rel["alpha/unrelated.txt"]["tier"] == 3
    assert [h["rel"] for h in hits] == [
        "name-has-alpha.txt", "alpha/unrelated.txt"]


def test_glob_unranked_reproduces_the_old_depth_then_alpha_order(tmp_path):
    """`ranked=False` for a glob query must still answer `depth ASC,
    lower(rel) ASC, rel ASC` — the exact order glob mode always used before
    this round, byte-for-byte — even on a pair where the DEFAULT `ranked=True`
    scored order disagrees with it: `aaa-icon-thing-copy-with-huge-padding.png`
    sorts alphabetically FIRST (starts with "a") but has a much bigger
    wildcard-swallow penalty than `icon copy.png`, so the scored order puts
    the tight match first while the unranked, alphabetical order puts the
    "a"-leading name first."""
    cfg = _index(tmp_path, "/r", [
        "/r/aaa-icon-thing-copy-with-huge-padding.png",
        "/r/icon copy.png",
    ])
    pattern = "**/**icon**copy**"
    ranked = search_ranked(cfg, "/r", pattern, glob=True)["hits"]
    unranked = search_ranked(cfg, "/r", pattern, glob=True,
                             ranked=False)["hits"]
    assert [h["rel"] for h in ranked] == [
        "icon copy.png",
        "aaa-icon-thing-copy-with-huge-padding.png"]
    assert [h["rel"] for h in unranked] == [
        "aaa-icon-thing-copy-with-huge-padding.png",
        "icon copy.png"]


def test_glob_unranked_sql_has_no_scoring_apparatus(tmp_path):
    """Mirrors `test_unranked_sql_has_no_scoring_apparatus` for `_rank_sql`:
    the unranked glob branch must not compute score/p0/segment_starts and
    then discard them — the whole scoring apparatus must be ABSENT from the
    generated SQL text. `literals=[]` is what `search_ranked` passes both
    when the caller asked for `ranked=False` and when a glob pattern (e.g. a
    bare `*`) has no literal run at all to score."""
    from fused_render.index.query import _glob_sql

    sql = _glob_sql("SELECT 1 AS rel, 1 AS size, 1 AS mtime, false AS is_dir, "
                    "1 AS depth, 'x' AS nm, 'x' AS lrel", "^.*$", "", 10,
                    literals=[])
    lowered = sql.lower()
    for banned in ("score", "segment_starts", "p0", "strpos", "name_bonus", "tier"):
        assert banned not in lowered, f"{banned!r} leaked into the unscored glob SQL"
    assert "order by depth asc, lower(rel) asc, rel asc" in lowered


def test_glob_pattern_with_no_literal_runs_uses_the_unscored_order(tmp_path):
    """A pattern with nothing but stars (`**/*`, what a bare `*` query
    resolves to) has no literal run for the scoring apparatus to locate —
    `search_ranked` must fall back to the same unscored `depth ASC,
    lower(rel) ASC, rel ASC` order `ranked=False` uses, not divide by zero
    or score every row identically (which would make the ORDER BY's tie-
    break do all the work silently instead of failing loudly)."""
    cfg = _index(tmp_path, "/r", ["/r/b.txt", "/r/a.txt"])
    hits = search_ranked(cfg, "/r", "**/*", glob=True)["hits"]
    assert [h["rel"] for h in hits] == ["a.txt", "b.txt"]
    for h in hits:
        assert h["score"] == 0


def test_like_metacharacters_in_the_query_match_only_the_literal_filename(tmp_path):
    """`_rank_sql`'s `WHERE lrel LIKE '%' || lower(ql) || '%' ESCAPE '\\'` is
    built from `like_literal(qs)` (store.py), which escapes `\\`, `%` and `_`
    before the query is spliced into the pattern — the whole point being that
    a `%` or `_` the USER typed is matched as a literal character, not read
    back by LIKE as its own wildcard. D712 wraps that same literal in SQL's
    `lower(...)` rather than lowering it in Python first, but the escaping
    `like_literal` does is unchanged and this test's job is unchanged with it:
    pin that a `%`/`_` in `qs` cannot leak into the pattern as a wildcard and
    match a sibling file it has no business matching. Each decoy below is a
    file a LEAKED wildcard (an un-escaped `%` or `_` read back by LIKE as
    "any run of characters" / "any one character") WOULD match but the
    literal query must not."""
    cfg = _index(tmp_path, "/r", [
        "/r/100%done.txt", "/r/100xdone.txt", "/r/100done.txt",
        "/r/a_b.txt", "/r/aXb.txt",
    ])

    def rels(q):
        return {h["rel"] for h in search_ranked(cfg, "/r", q)["hits"]}

    # A literal "%" must not act as LIKE's own "match anything" wildcard.
    assert rels("100%done") == {"100%done.txt"}
    assert rels("%done") == {"100%done.txt"}
    # A literal "_" must not act as LIKE's own "match any one character"
    # wildcard.
    assert rels("a_b") == {"a_b.txt"}
    assert rels("_b") == {"a_b.txt"}


def test_a_quote_in_the_query_does_not_break_the_sql(tmp_path):
    """`like_literal`/`_q` (store.py/query.py) double every single quote so
    the query can never close the SQL string literal it is spliced into.
    Without it, a query containing `'` would either break the generated SQL
    outright or silently match the wrong rows — this pins that the quote
    still round-trips to an exact, literal match. `'` is a legal character
    in a Windows filename too, so this half runs on every platform."""
    cfg = _index(tmp_path, "/r", ["/r/it's.txt", "/r/back-slash.txt"])
    assert {h["rel"] for h in search_ranked(cfg, "/r", "it's")["hits"]} == \
        {"it's.txt"}


@pytest.mark.skipif(
    os.name == "nt",
    reason="a backslash cannot appear in a Windows filename (it's a path "
           "separator there), so there is no literal `back\\slash.txt` for "
           "the query to match — this half of the coverage is POSIX-only",
)
def test_a_backslash_filename_is_matched_by_the_same_literal_query(tmp_path):
    """Companion to `test_a_backslash_in_the_query_does_not_break_the_sql`:
    on POSIX, `\\` is an ordinary filename character, so this additionally
    pins that `like_literal`'s escaping doesn't just avoid breaking the SQL
    — the escaped `\\` still round-trips to an exact, literal match against
    a real `\\`-containing filename."""
    cfg = _index(tmp_path, "/r", ["/r/it's.txt", "/r/back\\slash.txt"])
    assert {h["rel"] for h in search_ranked(cfg, "/r", "back\\slash")["hits"]} == \
        {"back\\slash.txt"}


def test_a_backslash_in_the_query_does_not_break_the_sql(tmp_path):
    """`like_literal` (store.py) escapes a literal backslash (`\\` ->
    `\\\\`) so it isn't misread as the start of an ESCAPE sequence for the
    character that follows it. Without it, a query containing `\\` would
    either break the generated SQL outright or silently match the wrong
    rows. `\\` can't appear in a Windows filename, so unlike the POSIX-only
    companion test above, this pins the platform-independent half: querying
    with a `\\` must not raise and must not match unrelated files, on every
    platform including Windows."""
    cfg = _index(tmp_path, "/r", ["/r/back-slash.txt", "/r/backXslash.txt"])
    assert {h["rel"] for h in search_ranked(cfg, "/r", "back\\slash")["hits"]} == set()


# -- unranked mode (`ranked=False`) --

def test_unranked_returns_the_same_set_of_rows_ordered_depth_then_rel(tmp_path):
    """`ranked=False` keeps the exact same substring filter — same rows
    match — but orders `depth ASC, rel ASC` instead of scoring. Built on the
    golden corpus: gather the SET of rels the ranked branch returns for a
    query, then check the unranked branch returns the identical set, just
    reordered."""
    cfg = _golden_index(tmp_path)
    for query in sorted(_GROUPS):
        ranked_rels = {h["rel"] for h in search_ranked(cfg, "/r", query, limit=200)["hits"]}
        out = search_ranked(cfg, "/r", query, limit=200, ranked=False)
        got = [h["rel"] for h in out["hits"]]
        assert set(got) == ranked_rels
        # depth ASC, then rel ASC (byte order) is a TOTAL order over `rel`,
        # which is unique across the files+dirs union (no file and directory
        # can share a path on a real filesystem, and the parquet stores are
        # keyed on that same uniqueness) — so this is the one true order,
        # not merely "a" valid one.
        depths = {h["rel"]: h["depth"] for h in out["hits"]}
        assert got == sorted(got, key=lambda rel: (depths[rel], rel))


def test_unranked_sql_has_no_scoring_apparatus(tmp_path):
    """The unranked branch must not compute score/tier/segment_starts/p0 and
    then merely discard them — the whole scoring apparatus must be ABSENT
    from the generated SQL text, so a future refactor that computes-then-
    ignores fails this test."""
    from fused_render.index.query import _rank_sql

    sql = _rank_sql("SELECT 1 AS rel, 1 AS size, 1 AS mtime, false AS is_dir, "
                     "1 AS depth, 'x' AS nm, 'x' AS lrel", "", "q", "q", 1, 10,
                     ranked=False)
    lowered = sql.lower()
    for banned in ("score", "tier", "segment_starts", "p0", "strpos", "name_bonus"):
        assert banned not in lowered, f"{banned!r} leaked into the unranked SQL"
    assert "order by depth asc, rel asc" in lowered


def test_unranked_mode_still_applies_the_limit_and_reports_truncation(tmp_path):
    files = [f"/r/alpha-{i}.txt" for i in range(50)]
    cfg = _index(tmp_path, "/r", files)
    out = search_ranked(cfg, "/r", "alpha", limit=10, ranked=False)
    assert len(out["hits"]) == 10
    assert out["truncated"] is True
    assert out["total"] == 10


def test_unranked_hits_still_carry_placeholder_wire_fields(tmp_path):
    """`score`/`tier`/`longest_run` are fixed constants in unranked mode
    (0 / 0 / len(q)) rather than absent — existing callers/tests key off
    these dict fields unconditionally."""
    cfg = _index(tmp_path, "/r", ["/r/environment.yml"])
    [hit] = search_ranked(cfg, "/r", "environment.yml", ranked=False)["hits"]
    assert hit["rel"] == "environment.yml"
    assert hit["score"] == 0
    assert hit["tier"] == 0
    assert hit["longest_run"] == len("environment.yml")
    assert hit["depth"] == 1


def test_unranked_like_metacharacters_match_only_the_literal_filename(tmp_path):
    """Mirrors `test_like_metacharacters_in_the_query_match_only_the_literal_filename`
    for the unranked branch — same escaping, same guarantee."""
    cfg = _index(tmp_path, "/r", [
        "/r/100%done.txt", "/r/100xdone.txt", "/r/100done.txt",
        "/r/a_b.txt", "/r/aXb.txt",
    ])

    def rels(q):
        return {h["rel"] for h in search_ranked(cfg, "/r", q, ranked=False)["hits"]}

    assert rels("100%done") == {"100%done.txt"}
    assert rels("%done") == {"100%done.txt"}
    assert rels("a_b") == {"a_b.txt"}
    assert rels("_b") == {"a_b.txt"}


def test_unranked_a_quote_in_the_query_does_not_break_the_sql(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/it's.txt", "/r/back-slash.txt"])
    assert {h["rel"] for h in search_ranked(cfg, "/r", "it's", ranked=False)["hits"]} == \
        {"it's.txt"}


@pytest.mark.skipif(
    os.name == "nt",
    reason="a backslash cannot appear in a Windows filename (it's a path "
           "separator there), so there is no literal `back\\slash.txt` for "
           "the query to match — this half of the coverage is POSIX-only",
)
def test_unranked_a_backslash_filename_is_matched_by_the_same_literal_query(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/it's.txt", "/r/back\\slash.txt"])
    assert {h["rel"] for h in
            search_ranked(cfg, "/r", "back\\slash", ranked=False)["hits"]} == \
        {"back\\slash.txt"}


def test_unranked_a_backslash_in_the_query_does_not_break_the_sql(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/back-slash.txt", "/r/backXslash.txt"])
    assert {h["rel"] for h in
            search_ranked(cfg, "/r", "back\\slash", ranked=False)["hits"]} == set()

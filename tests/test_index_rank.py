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
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index.config import IndexConfig
from fused_render.index.query import (
    MAX_GLOB_RANK_LIMIT,
    MAX_RANK_LIMIT,
    is_hidden_rel,
    query_wants_hidden,
    search_ranked,
)
from fused_render.index.runner import canonical_root
from fused_render.index.store import Sink, compact

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "rank-parity.json")
    .read_text(encoding="utf-8"))


def _group_case_only_ties(rels):
    """`rels` with any run of CONSECUTIVE entries that are equal under
    `.lower()` collapsed into a sorted tuple.

    The deleted index/rank.py's own docstring flagged this as a KNOWN,
    deliberate divergence: the JS ranker's final tie-break is
    `Intl.Collator(sensitivity: "base")`, and both the old Python ranker and
    this SQL rewrite use plain `rel ASC` (byte/ASCII comparison) instead —
    every OTHER tie-break (tier, score, depth, `lower(rel)`) still has to
    agree exactly, but a pair that is equal under all of them AND differs
    only in case (e.g. "file.txt" vs "FILE.TXT") is not guaranteed to land in
    the SAME order a locale-aware collator would pick. This is now purely a
    CROSS-LANGUAGE divergence, not a within-SQL nondeterminism one: `rel ASC`
    (query.py's `_rank_sql`) makes the SQL side's own order for such a pair
    deterministic and repeatable run to run — the physical-row-order
    dependency this helper originally existed to paper over is gone — but
    that deterministic order can still legitimately differ from what
    `Intl.Collator` would produce for the same pair, which is what this
    helper still exists to tolerate. Grouping same-tie runs into an
    order-independent tuple keeps the comparison strict about everything
    else."""
    out = []
    i = 0
    while i < len(rels):
        j = i
        while j + 1 < len(rels) and rels[j + 1].lower() == rels[i].lower():
            j += 1
        out.append(tuple(sorted(rels[i:j + 1])))
        i = j + 1
    return out


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


def _index_from_fixture(tmp_path):
    """The fixture's own 64 entries, loaded into a real index under `/r`."""
    files = [f"/r/{e['rel']}" for e in FIXTURE["entries"] if not e["is_dir"]]
    dirs = [f"/r/{e['rel']}" for e in FIXTURE["entries"] if e["is_dir"]]
    return _index(tmp_path, "/r", files, dirs=dirs)


@pytest.mark.parametrize("query", FIXTURE["queries"])
def test_sql_ranking_matches_the_js_ranker_on_substring_hits(tmp_path, query):
    """The fixture's expected order, RESTRICTED to substring matches — the
    part of `fuzzy.ts`'s answer index-backed search can still reach. `str.find`
    (Python) and `String.prototype.includes` (JS, via fuzzy.ts's own substring
    branch) and SQL's `LIKE '%q%'`/`strpos` all agree on "is `query` a
    substring", so filtering the JS-authoritative order down to substring
    rows and comparing it to the SQL order is a same-language-independent
    check that dropping the fuzzy pass didn't also reorder the tier-1/2/3
    rows that remain."""
    cfg = _index_from_fixture(tmp_path)
    ql = query.lower()
    show_hidden = query_wants_hidden(query)
    expected = [rel for rel in FIXTURE["expected"][query]
                if ql in rel.lower() and (show_hidden or not is_hidden_rel(rel))]
    out = search_ranked(cfg, "/r", query, limit=200)
    # A real on-disk index (unlike the flat fixture list) structurally must
    # hold a dirs-table row for every ANCESTOR directory a stored file
    # implies (index/store.py's `Sink.add`, one call per directory that
    # holds files) — e.g. `fused_render/index/query.py` forces a row for
    # `fused_render/index` even though the fixture never lists that
    # directory as its own entry. Those implied-ancestor rows are real
    # candidates the SQL side legitimately sees and the flat JS fixture
    # never modeled, so they are filtered back out here rather than
    # asserted on — this test is about ORDER, not about re-deriving which
    # ancestor directories a real filesystem has.
    fixture_rels = {e["rel"] for e in FIXTURE["entries"]}
    got = [h["rel"] for h in out["hits"] if h["rel"] in fixture_rels]
    assert _group_case_only_ties(got) == _group_case_only_ties(expected)


def test_an_empty_query_ranks_nothing(tmp_path):
    cfg = _index_from_fixture(tmp_path)
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


def test_the_depth_penalty_breaks_a_same_tier_same_score_tie_by_shallowness(
    tmp_path,
):
    """Two ancestor-only (tier 3) hits with the IDENTICAL matched window (the
    query matches the first path segment of both, at position 0, so raw
    `score` before the depth term is identical) — DEPTH_PENALTY is what stops
    the deeper one from winning purely by having accumulated more path."""
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
    # The un-penalised (pre-`depth`) score IS identical — same matched
    # window, same segment-start count — so the ordering above is entirely
    # DEPTH_PENALTY's doing, not a difference in what matched.
    n = len("xxxxxxxx")
    bare = n + 3 * (n - 1) + 5  # one segment start (position 0)
    by_rel = {h["rel"]: h for h in out["hits"]}
    assert by_rel[shallow]["score"] == bare  # depth 2, no penalty (<= SHALLOW_FREE)
    assert by_rel[deep]["score"] == bare - 4 * (6 - 3)  # DEPTH_PENALTY * (depth - 3)


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


def test_tier_1_2_3_boundaries_including_a_match_straddling_the_basename(
    tmp_path,
):
    """tier 1: query is a substring of the basename. tier 3: the match ends
    strictly before the basename starts (ancestor-only). tier 2: everything
    else — including a match that STRADDLES the boundary. A straddling match
    is only possible when the matched substring literally contains the "/"
    separator itself (the match is a run of CONSECUTIVE characters of `rel`),
    so the query has to spell across it: "oo/ba" against "foo/bar.txt" starts
    inside the directory segment "foo" and ends inside the basename "bar.txt"."""
    cfg = _index(tmp_path, "/r", ["/r/name-has-alpha.txt",  # tier 1: "alpha" in name-has-alpha.txt
                                  "/r/alpha/unrelated.txt",  # tier 3: match ends before "unrelated.txt"
                                  "/r/foo/bar.txt"])         # tier 2: "oo/ba" straddles the "/"
    by_rel = {h["rel"]: h for h in search_ranked(cfg, "/r", "alpha")["hits"]}
    assert by_rel["name-has-alpha.txt"]["tier"] == 1
    assert by_rel["alpha/unrelated.txt"]["tier"] == 3
    straddle = search_ranked(cfg, "/r", "oo/ba")["hits"]
    assert [h["rel"] for h in straddle] == ["foo/bar.txt"]
    assert straddle[0]["tier"] == 2


def test_the_camelcase_hump_counts_as_a_segment_start(tmp_path):
    """`_is_segment_start`'s camelCase test runs against the ORIGINAL-case
    path, not the lowercased one — an upper-case letter preceded by a
    lower-case one scores like a word boundary. Pinned by comparing a query
    that lands ONLY on humps against a same-length query that lands on none:
    the hump-aligned one must score higher."""
    cfg = _index(tmp_path, "/r", ["/r/MyRenderTarget.ts"])
    humps = search_ranked(cfg, "/r", "MRT")
    assert humps["hits"] == []  # "MRT" is not a substring at all — sanity
    # Compare two REAL substrings of the same file: one starting on a hump
    # ("Render", right after the lowercase "y"), one starting mid-word
    # ("ender", one character later, off any boundary).
    on_hump = search_ranked(cfg, "/r", "render")["hits"][0]
    off_hump = search_ranked(cfg, "/r", "ender")["hits"][0]
    # Equalize for the different query length by comparing the SEGMENT-START
    # contribution alone: score minus the "n + 3*(n-1)" run term and any name
    # bonus (neither query is the whole basename or a prefix of it).
    def bare(hit, n):
        return hit["score"] - (n + 3 * (n - 1))
    assert bare(on_hump, 6) > bare(off_hump, 5)


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


def test_glob_ranked_hits_carry_a_real_score(tmp_path):
    """Ranked glob hits carry a genuine, non-constant `score` (not the fixed
    `0` every glob hit used to carry) — pinned by checking two hits with
    different wildcard-swallow amounts do NOT share a score, and every hit
    still carries the same key set (`rel`, `is_dir`, `size`, `mtime`,
    `score`, `longest_run`, `tier`, `depth`) the other two modes' hits do.
    `tier` is no longer a fixed `0` placeholder either — both basenames here
    contain both literal runs (`icon`, `copy`), so both are tier 1, same rule
    `_rank_sql` uses (query is a substring of the basename)."""
    cfg = _index(tmp_path, "/r", ["/r/icon copy.png",
                                  "/r/icon-a-very-long-thing-copy.png"])
    hits = search_ranked(cfg, "/r", "**/**icon**copy**", glob=True)["hits"]
    assert len(hits) == 2
    assert hits[0]["score"] != hits[1]["score"]
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
    fixture harness like the JS-parity test above: gather the SET of rels
    the ranked branch returns for a query, then check the unranked branch
    returns the identical set, just reordered."""
    cfg = _index_from_fixture(tmp_path)
    for query in FIXTURE["queries"]:
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

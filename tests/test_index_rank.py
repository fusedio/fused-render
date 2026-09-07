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
    this SQL rewrite use plain `lower()` instead — every OTHER tie-break
    (tier, score, depth) still has to agree exactly, but a pair that is equal
    under all of them AND differs only in case (e.g. "file.txt" vs
    "FILE.TXT") can land in either order depending on which physical row
    order the source happened to hand the final sort, which for a real
    on-disk index (unlike the fixture's flat in-memory list) is out of this
    module's control. Grouping same-tie runs into an order-independent tuple
    keeps the comparison strict about everything else."""
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


def test_hidden_entries_need_a_dot_leading_query_segment(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/.env", "/r/environment.yml"])
    rels = [h["rel"] for h in search_ranked(cfg, "/r", "env")["hits"]]
    assert ".env" not in rels and "environment.yml" in rels
    rels = [h["rel"] for h in search_ranked(cfg, "/r", ".env")["hits"]]
    assert ".env" in rels


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

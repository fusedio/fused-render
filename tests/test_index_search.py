"""The explorer's in-folder corpus, served from the index.

`search_under` answers the same shape /api/fs/walk streams (rel/is_dir/size/
mtime), so the client's fuzzy scoring is untouched by where the corpus came
from — the only difference is that this one is instant and cross-session.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from fused_render.index.config import IndexConfig, load_config
from fused_render.index.query import search_ranked, search_under
from fused_render.index.store import Sink, compact, partition_files
from fused_render.server import create_app


def _index(tmp_path, root, files, dirs=()):
    cfg = IndexConfig(dir=str(tmp_path / "ix"))
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    by_dir = {d: [] for d in dirs}
    by_dir.setdefault(root, [])
    for i, p in enumerate(files):
        d, name = p.rsplit("/", 1)
        ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
        by_dir.setdefault(d, []).append((p, d, name, ext, 10 + i, 100.0 + i))
    for d, rows in by_dir.items():
        sink.add(d, "s", ("sig", rows, sum(r[4] for r in rows), 1_000_000_000, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


@pytest.mark.parametrize("awkward", ["Dave's stuff", "brack[et]s"])
def test_reading_the_index_survives_a_store_path_with_metachars(tmp_path, awkward):
    """The reader interpolates dirs.parquet into SQL too, so it has the same
    exposure the compaction had: an apostrophe closes the literal and a `[`
    turns the path into a glob that matches nothing."""
    home = tmp_path / awkward
    home.mkdir()
    cfg = _index(home, "/r", ["/r/alpha.txt"])
    out = search_under(cfg, "/r")
    assert out["covered"] is True
    assert [e["rel"] for e in out["entries"]] == ["alpha.txt"]


def test_search_under_covers_the_filesystem_root(tmp_path):
    """Searching "/" answered covered:false every time: rstrip("/") reduced the
    root to the empty string, which the guard then read as "no root given".
    Every line after that already special-cased "/" — only the normalisation
    did not, and `stats` next door had the `or "/"` this was missing."""
    cfg = _index(tmp_path, "/", ["/alpha.txt", "/sub/beta.md"], dirs=["/sub"])
    out = search_under(cfg, "/")
    assert out["covered"] is True
    assert "alpha.txt" in [e["rel"] for e in out["entries"]]
    assert "sub/beta.md" in [e["rel"] for e in out["entries"]]


def test_search_under_ignores_a_trailing_slash(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    assert (search_under(cfg, "/r/")["entries"]
            == search_under(cfg, "/r")["entries"])


def test_search_under_returns_walk_shaped_entries(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/alpha.txt", "/r/sub/beta.md"],
                 dirs=["/r/sub"])
    out = search_under(cfg, "/r")
    files = {e["rel"]: e for e in out["entries"] if not e["is_dir"]}
    assert sorted(files) == ["alpha.txt", "sub/beta.md"]
    assert files["alpha.txt"]["size"] == 10
    assert files["alpha.txt"]["mtime"] == 100.0
    dirs = [e for e in out["entries"] if e["is_dir"]]
    assert [d["rel"] for d in dirs] == ["sub"]
    assert dirs[0]["size"] is None


def test_search_under_reports_coverage_for_an_indexed_root(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    assert search_under(cfg, "/r")["covered"] is True


def test_search_under_reports_no_coverage_for_an_unindexed_root(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    out = search_under(cfg, "/elsewhere")
    assert out["covered"] is False
    assert out["entries"] == []


def test_searching_inside_a_package_falls_back_to_the_walk(tmp_path):
    """The scan records a .app as ONE opaque row and never lists it, so its dirs
    row says "this is a leaf", not "we know what is inside". Answering `covered`
    for it would report an empty corpus as complete; the live walk, which does
    list a leaf it was pointed at, has the real answer."""
    cfg = _index(tmp_path, "/r", ["/r/a.txt"], dirs=["/r/Cool.app"])
    out = search_under(cfg, "/r/Cool.app")
    assert out["covered"] is False
    assert out["entries"] == []
    # the package itself is still an entry of its PARENT's corpus
    assert "Cool.app" in [e["rel"] for e in search_under(cfg, "/r")["entries"]]


def test_searching_BELOW_a_package_root_also_falls_back_to_the_walk(tmp_path):
    """Testing only the root's final component was not enough. An index written
    before the leaf rule holds real dirs rows for paths INSIDE a package, and
    those rows do describe a scanned directory — so the coverage query says yes
    and the index answers from whatever partial set of package rows happens to
    be on disk, while the folder one level up is answered by the live walk. Two
    sources meant to be interchangeable then disagree, which is the whole reason
    LEAF_DIR_SUFFIXES is shared between them.

    The rows here are exactly what such an index looks like: the package's inner
    directories present and populated."""
    cfg = _index(tmp_path, "/r",
                 ["/r/a.txt", "/r/Cool.app/Contents/Info.plist"],
                 dirs=["/r/Cool.app", "/r/Cool.app/Contents"])
    for root in ("/r/Cool.app", "/r/Cool.app/Contents"):
        out = search_under(cfg, root)
        assert out["covered"] is False, f"{root} answered from the index"
        assert out["entries"] == []


def test_search_under_reports_no_coverage_on_an_empty_index(tmp_path):
    out = search_under(IndexConfig(dir=str(tmp_path / "ix")), "/r")
    assert out["covered"] is False
    assert out["fresh"] is False
    assert out["entries"] == []


def test_a_stale_index_is_covered_but_not_fresh(tmp_path, monkeypatch):
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    monkeypatch.setattr("fused_render.index.query.FRESH_MAX_AGE_S", -1)
    out = search_under(cfg, "/r")
    assert out["covered"] is True
    assert out["fresh"] is False


def test_search_under_only_reads_partitions_the_prefix_can_hit(tmp_path):
    """Prefix pruning is the reason an in-folder search reads a slice of the
    index rather than all of it."""
    cfg = _index(tmp_path, "/r", ["/r/a.txt"])
    cfg.part_rows = 1  # force one partition per row on the next compaction
    out = search_under(cfg, "/r")
    assert out["scanned_partitions"] <= out["of_partitions"]
    assert out["scanned_partitions"] >= 1
    # a sibling tree shares no prefix, so nothing is scanned for it
    assert search_under(cfg, "/zzz")["scanned_partitions"] == 0


def test_search_under_never_leaks_a_sibling_directorys_files(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/deep/in.txt"], dirs=["/r/deep"])
    cfg2 = _index(tmp_path, "/rr", ["/rr/out.txt"])
    rels = [e["rel"] for e in search_under(cfg2, "/r")["entries"]]
    assert "out.txt" not in rels
    assert "deep/in.txt" in rels


def test_search_under_optional_query_filters_server_side(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/alpha.txt", "/r/beta.md"])
    out = search_under(cfg, "/r", q="beta")
    assert [e["rel"] for e in out["entries"]] == ["beta.md"]


def test_search_under_caps_the_corpus_and_flags_truncation(tmp_path):
    cfg = _index(tmp_path, "/r", [f"/r/f{i}.txt" for i in range(10)])
    out = search_under(cfg, "/r", limit=3)
    assert len(out["entries"]) == 3
    assert out["truncated"] is True


def test_search_under_expands_a_tilde_root(tmp_path, monkeypatch):
    home = str(tmp_path / "userhome")
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: home if p in ("~", "~/") else p)
    cfg = _index(tmp_path, home, [home + "/a.txt"])
    assert search_under(cfg, "~")["covered"] is True


# -- the route ----------------------------------------------------------------

@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


def test_search_route_answers_the_corpus(home, tmp_path):
    root = str(tmp_path / "proj")
    cfg = load_config()
    src = _index(tmp_path, root, [root + "/a.txt"])
    # the fixture built the index at tmp_path/ix; point the store at it
    os.rename(src.dir, cfg.dir)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    body = client.get("/api/index/search", params={"root": root}).json()
    assert body["ok"] is True
    assert body["covered"] is True
    assert [e["rel"] for e in body["entries"]] == ["a.txt"]


def test_search_route_on_a_missing_index_is_a_quiet_miss(home, tmp_path):
    """Not an error: 'no index yet', 'root not covered' and 'a scan is
    running' are the same condition to the explorer — fall back silently."""
    client = TestClient(create_app(start_dir=str(tmp_path)))
    body = client.get("/api/index/search", params={"root": str(tmp_path)}).json()
    assert body["ok"] is True
    assert body["covered"] is False
    assert body["entries"] == []


def test_search_route_requires_a_root(home, tmp_path):
    resp = TestClient(create_app(start_dir=str(tmp_path))).get("/api/index/search")
    assert resp.status_code == 400


def _ranked_client(tmp_path, root, files, dirs=()):
    cfg = load_config()
    src = _index(tmp_path, root, files, dirs=dirs)
    os.rename(src.dir, cfg.dir)
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_rank_route_answers_ranked_hits(home, tmp_path):
    root = str(tmp_path / "proj")
    client = _ranked_client(tmp_path, root,
                            [root + "/readme.md", root + "/docs/readme-old.md",
                             root + "/other.bin"], dirs=[root + "/docs"])
    body = client.get("/api/index/rank",
                      params={"root": root, "q": "readme.md"}).json()
    assert body["ok"] is True and body["covered"] is True
    assert body["hits"][0]["rel"] == "readme.md"
    assert body["total"] == len(body["hits"])


def test_rank_route_does_not_return_positions(home, tmp_path):
    """The client re-runs fuzzyMatch over the ~200 rows it got back, so
    fuzzy.ts stays the single source of truth for what highlights — and the
    response stays small."""
    root = str(tmp_path / "proj")
    client = _ranked_client(tmp_path, root, [root + "/alpha.txt"])
    body = client.get("/api/index/rank",
                      params={"root": root, "q": "alpha"}).json()
    assert "positions" not in body["hits"][0]


def test_rank_route_on_a_missing_index_is_a_quiet_miss(home, tmp_path):
    client = TestClient(create_app(start_dir=str(tmp_path)))
    body = client.get("/api/index/rank",
                      params={"root": str(tmp_path), "q": "x"}).json()
    assert body["ok"] is True and body["covered"] is False and body["hits"] == []


def test_rank_route_requires_a_root(home, tmp_path):
    resp = TestClient(create_app(start_dir=str(tmp_path))).get("/api/index/rank")
    assert resp.status_code == 400


def test_rank_route_filters_a_gitignored_dir_under_a_NON_repo_root(home, tmp_path):
    """The hole a narrowed payload opens, closed.

    Oracles used to be discovered from the entry list handed to the filter, and
    the ranked route hands it ~200 candidates from which stage A has already
    dropped every dot-leading rel — so `.gitignore` could essentially never be
    in the set, every entry came back undecided, and NOTHING was filtered. The
    corpus route did not have the bug only because it carries the whole tree,
    dotfiles included.

    Home is not a repo here, which is the case that matters: inside a repo one
    oracle at the toplevel decides everything and the payload's contents are
    irrelevant."""
    root = str(tmp_path / "userhome")
    proj = tmp_path / "userhome" / "proj"
    (proj / "dist").mkdir(parents=True)
    (proj / ".gitignore").write_text("dist/\n", encoding="utf-8")
    client = _ranked_client(
        tmp_path, root,
        [root + "/proj/.gitignore", root + "/proj/dist/zzbundle.js",
         root + "/proj/zzkeep.js"],
        dirs=[root + "/proj", root + "/proj/dist"])
    body = client.get("/api/index/rank", params={"root": root, "q": "zz"}).json()
    assert body["covered"] is True
    assert [h["rel"] for h in body["hits"]] == ["proj/zzkeep.js"]
    # And the two routes agree, which is the actual requirement: the same file
    # must not appear in one search and vanish from the other.
    corpus = client.get("/api/index/search", params={"root": root}).json()
    assert "proj/dist/zzbundle.js" not in [e["rel"] for e in corpus["entries"]]


def test_rank_route_honours_the_limit(home, tmp_path):
    root = str(tmp_path / "proj")
    client = _ranked_client(tmp_path, root,
                            [f"{root}/alpha{i}.txt" for i in range(10)])
    body = client.get("/api/index/rank",
                      params={"root": root, "q": "alpha", "limit": 3}).json()
    assert len(body["hits"]) == 3 and body["truncated"] is True


def test_search_under_ignores_a_lookalike_underscore_sibling(tmp_path):
    """`_` matches any char in LIKE: searching inside /x/my_dir must not list
    files that actually live in /x/my-dir (the prefix is escaped)."""
    cfg = _index(tmp_path, "/x/my_dir", ["/x/my_dir/real.txt"])
    _index(tmp_path, "/x/my-dir", ["/x/my-dir/fake.txt"])  # same store
    rels = [e["rel"] for e in search_under(cfg, "/x/my_dir")["entries"]]
    assert "real.txt" in rels
    assert "fake.txt" not in rels


def test_dirs_are_not_silently_dropped_when_files_fill_the_cap(tmp_path):
    """Directories used to get only the budget the FILES branch left over, so
    on any truncated corpus (`room == 0`) folder search was dead, not degraded.
    Both kinds of entry compete in ONE depth-ordered query now, so a shallow
    directory survives a cap that deep files would otherwise have filled."""
    cfg = _index(tmp_path, "/r",
                 [f"/r/deep/a/b/x{i}.txt" for i in range(5)],
                 dirs=["/r/sub", "/r/deep", "/r/deep/a", "/r/deep/a/b"])
    out = search_under(cfg, "/r", limit=2)
    assert "sub" in [e["rel"] for e in out["entries"]]
    assert out["truncated"] is True


def test_a_matching_folder_survives_a_cap_full_of_matching_files(tmp_path):
    """The reported symptom: a query naming a folder returned 100 files from
    inside it and not the folder itself."""
    cfg = _index(tmp_path, "/r",
                 [f"/r/target-thing/f{i}.txt" for i in range(5)],
                 dirs=["/r/target-thing"])
    out = search_under(cfg, "/r", q="target-thing", limit=2)
    assert "target-thing" in [e["rel"] for e in out["entries"]]


def test_a_capped_corpus_prefers_shallow_entries(tmp_path):
    """The walk streams breadth-first, so its cap keeps shallow entries; plain
    ORDER BY path would fill the whole cap with the first deep subtree."""
    cfg = _index(tmp_path, "/r",
                 ["/r/deep/a/b/x1.txt", "/r/deep/a/b/x2.txt", "/r/top.txt"],
                 dirs=["/r/deep", "/r/deep/a", "/r/deep/a/b"])
    out = search_under(cfg, "/r", limit=2)
    assert sorted(e["rel"] for e in out["entries"]) == ["deep", "top.txt"]


def test_the_corpus_is_ordered_by_the_stored_depth_column(tmp_path):
    """Depth is a materialised int32, not a slash-counting expression: every
    query opens a fresh in-memory duckdb over parquet, so there is no catalog
    to hold a generated column (see store.schemas)."""
    cfg = _index(tmp_path, "/r", ["/r/a/b/deep.txt", "/r/top.txt"],
                 dirs=["/r/a", "/r/a/b"])
    t = pq.read_table(partition_files(cfg)[0])
    assert dict(zip(t.column("path").to_pylist(),
                    t.column("depth").to_pylist())) == {
        "/r/a/b/deep.txt": 4, "/r/top.txt": 2}
    d = pq.read_table(cfg.dirs_parquet)
    assert dict(zip(d.column("dir").to_pylist(),
                    d.column("depth").to_pylist())) == {
        "/r": 1, "/r/a": 2, "/r/a/b": 3}


def _drop_depth(cfg):
    """Rewrite the index as a pre-`depth` one would have been written."""
    for fp in partition_files(cfg) + [cfg.dirs_parquet]:
        t = pq.read_table(fp)
        pq.write_table(t.drop([c for c in ["depth"] if c in t.column_names]), fp)


def test_an_index_written_without_a_depth_column_still_reads(tmp_path):
    """Additive schema evolution: an index on disk from before `depth` existed
    falls back to the slash-count expression rather than hard-failing (the same
    defensive read load_dir_cache and the compaction already do)."""
    cfg = _index(tmp_path, "/r", ["/r/a/b/deep.txt", "/r/top.txt"],
                 dirs=["/r/a", "/r/a/b"])
    _drop_depth(cfg)
    out = search_under(cfg, "/r", limit=2)
    assert out["covered"] is True
    assert sorted(e["rel"] for e in out["entries"]) == ["a", "top.txt"]


# -- search_ranked: filtering AND ranking, server-side -------------------------
#
# The home page used to fetch the whole corpus (20 MB, 164k rows, silently
# capped at MAX_CORPUS so ~71% of the user's files could not be found at all)
# and rank it in the browser. `search_ranked` does both here and returns ~200
# rows. The ranker itself is pinned by tests/test_index_rank.py; these are
# about the two-stage query around it.

def test_search_ranked_returns_the_best_match_first(tmp_path):
    cfg = _index(tmp_path, "/r",
                 ["/r/readme.md", "/r/docs/readme-draft.md",
                  "/r/readme/other.txt", "/r/unrelated.bin"],
                 dirs=["/r/docs", "/r/readme"])
    out = search_ranked(cfg, "/r", "readme.md")
    assert out["covered"] is True
    assert out["hits"][0]["rel"] == "readme.md"


def test_search_ranked_finds_a_subsequence_the_like_filter_would_miss(tmp_path):
    """Stage A's coarse filter must admit fuzzy hits, not just substrings —
    that is the whole reason it is a subsequence regex and not an ILIKE."""
    cfg = _index(tmp_path, "/r", ["/r/alpha-beta.txt"])
    out = search_ranked(cfg, "/r", "albe")
    assert [h["rel"] for h in out["hits"]] == ["alpha-beta.txt"]


def test_search_ranked_hits_are_walk_shaped_and_carry_the_ranking(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/alpha.txt"])
    [hit] = search_ranked(cfg, "/r", "alpha")["hits"]
    assert hit["rel"] == "alpha.txt" and hit["is_dir"] is False
    assert hit["size"] == 10 and hit["mtime"] == 100.0
    assert hit["score"] > 0 and hit["tier"] == 1 and hit["depth"] == 1


def test_search_ranked_ranks_directories_against_files_in_one_query(tmp_path):
    """The same property search_under has, for the same reason: two queries
    gave folders only the budget the files left over, and folder search died
    on any tree big enough to fill it."""
    cfg = _index(tmp_path, "/r", [f"/r/target/f{i}.txt" for i in range(5)],
                 dirs=["/r/target"])
    out = search_ranked(cfg, "/r", "target", limit=2)
    assert out["hits"][0]["rel"] == "target"
    assert out["hits"][0]["is_dir"] is True


def test_search_ranked_answers_an_empty_query_with_no_hits(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/alpha.txt"])
    out = search_ranked(cfg, "/r", "")
    assert out["covered"] is True and out["hits"] == [] and out["total"] == 0


def test_search_ranked_is_a_quiet_miss_on_an_uncovered_root(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/alpha.txt"])
    out = search_ranked(cfg, "/elsewhere", "alpha")
    assert out["covered"] is False and out["hits"] == []


def test_search_ranked_filters_gitignored_entries_BEFORE_cutting_to_limit(tmp_path):
    """Filtering after the cut is what makes today's corpus report `truncated`
    while holding fewer rows than it says: the cap was spent on entries that
    were then dropped. So `limit` counts entries the user can actually see."""
    cfg = _index(tmp_path, "/r", [f"/r/alpha{i}.txt" for i in range(10)])
    dropped = {"alpha0.txt", "alpha1.txt", "alpha2.txt"}
    out = search_ranked(cfg, "/r", "alpha", limit=5,
                        gitignore_filter=lambda root, es, oracles:
                            [e for e in es if e["rel"] not in dropped])
    assert len(out["hits"]) == 5
    assert not dropped & {h["rel"] for h in out["hits"]}
    assert out["truncated"] is True


def test_search_ranked_flags_nothing_when_every_hit_fits(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/alpha.txt", "/r/beta.txt"])
    out = search_ranked(cfg, "/r", "alpha", limit=5)
    assert out["truncated"] is False and out["total"] == 1


def test_the_substring_pass_alone_answers_when_it_fills_the_cut(tmp_path):
    """The cheap pass is the whole query when it is enough.

    ILIKE over 571k rows costs ~51 ms where the subsequence regex costs ~143 ms,
    and the regex can only ever APPEND rows below every substring hit (see
    search_ranked's docstring on longest_run), so a filled cut means the second
    pass is pure spend."""
    cfg = _index(tmp_path, "/r", ["/r/alpha.txt", "/r/a-l-p-h-a.txt"])
    out = search_ranked(cfg, "/r", "alpha", limit=1)
    assert [h["rel"] for h in out["hits"]] == ["alpha.txt"]
    assert out["escalated"] is False


def test_it_escalates_to_the_subsequence_pass_when_substring_hits_run_short(tmp_path):
    cfg = _index(tmp_path, "/r", ["/r/alpha.txt", "/r/a-l-p-h-a.txt"])
    out = search_ranked(cfg, "/r", "alpha", limit=5)
    assert [h["rel"] for h in out["hits"]] == ["alpha.txt", "a-l-p-h-a.txt"]
    assert out["escalated"] is True


def test_the_escalation_ladder_is_lossless(tmp_path):
    """Skipping the regex pass must not change ONE row of the answer.

    The substring pass answering alone and the full two-pass answer have to
    agree on the rows they both cover — otherwise the optimisation is a
    behaviour change wearing a performance costume."""
    files = ["/r/alpha.txt", "/r/docs/alpha-notes.md", "/r/a-l-p-h-a.txt",
             "/r/away/lower/place/hat/area.txt"]
    cfg = _index(tmp_path, "/r", files, dirs=["/r/docs", "/r/away",
                                              "/r/away/lower",
                                              "/r/away/lower/place",
                                              "/r/away/lower/place/hat"])
    full = search_ranked(cfg, "/r", "alpha", limit=50)
    assert full["escalated"] is True
    for n in (1, 2):
        short = search_ranked(cfg, "/r", "alpha", limit=n)
        assert short["escalated"] is False
        assert [h["rel"] for h in short["hits"]] == \
            [h["rel"] for h in full["hits"]][:n]


def test_a_query_the_substring_pass_cannot_answer_still_answers(tmp_path):
    """Zero substring hits is the escalation case, not an empty result."""
    cfg = _index(tmp_path, "/r", ["/r/alpha-beta.txt"])
    out = search_ranked(cfg, "/r", "albe", limit=1)
    assert [h["rel"] for h in out["hits"]] == ["alpha-beta.txt"]
    assert out["escalated"] is True


def test_the_stage_a_cap_keeps_the_name_matches_over_the_fuzzy_ones(tmp_path):
    """Stage A can in principle drop a row stage B would rank top. Its coarse
    tier ordering is what makes that unlikely, and this is the property: with a
    cap far below the candidate count, the rows that survive are the ones the
    real ranker also puts first."""
    files = [f"/r/noise/a{i}-l-p-h-a.txt" for i in range(50)]
    files.append("/r/alpha.txt")
    cfg = _index(tmp_path, "/r", files, dirs=["/r/noise"])
    out = search_ranked(cfg, "/r", "alpha", cap=3)
    assert out["hits"][0]["rel"] == "alpha.txt"

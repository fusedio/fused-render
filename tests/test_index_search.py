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
from fused_render.index.query import search_under
from fused_render.index.store import Sink, compact
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

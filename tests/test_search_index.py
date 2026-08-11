"""/api/search/files, answered from the SQL index — the only engine.

The AI search's filter spec used to execute against Spotlight (or a bounded home
walk). The index holds the same facts in parquet — name, ext, size, mtime — so
the spec translates into ONE SQL query with no filesystem touch at all, and both
of the old engines are gone. These tests pin the translation (dates inclusive
both ends, kind routing, ext/size only on files), the truncation flag, and the
contract when the index cannot answer: an honest empty result for a miss, and a
plain error — never a silently narrower answer — when there is no index to read.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from fused_render.index.config import IndexConfig
from fused_render.index.store import Sink, compact
from fused_render.server import create_app
from fused_render.server.routers import search as search_mod
from fused_render.server.routers.search import (
    IndexUnavailable,
    _day_bound_epoch,
    _search_index,
)


def spec(**over):
    base = {
        "name_terms": [],
        "extensions": [],
        "kind": "any",
        "modified_within_days": None,
        "modified_after": None,
        "modified_before": None,
        "min_size_bytes": None,
        "max_size_bytes": None,
    }
    base.update(over)
    return base


def _index(tmp_path, root, files, dirs=(), dir_mtime_ns=1_000_000_000, name="ix"):
    """An index over `files` — (path, size, mtime) triples — plus `dirs`."""
    cfg = IndexConfig(dir=str(tmp_path / name))
    shards = str(tmp_path / name / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    by_dir = {d: [] for d in dirs}
    by_dir.setdefault(root, [])
    for path, size, mtime in files:
        d, fname = path.rsplit("/", 1)
        ext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
        by_dir.setdefault(d, []).append((path, d, fname, ext, size, mtime))
    for d, rows in by_dir.items():
        sink.add(d, "s", ("sig", rows, sum(r[4] for r in rows), dir_mtime_ns, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


def _paths(out):
    return sorted(e["path"] for e in out["entries"])


# -- what the index engine returns ---------------------------------------------

def test_index_engine_matches_name_terms_case_insensitively(tmp_path):
    cfg = _index(tmp_path, "/r", [("/r/Weather Report.csv", 10, 100.0),
                                  ("/r/notes.txt", 20, 200.0)])
    out = _search_index(spec(name_terms=["weather"]), cfg)
    assert _paths(out) == ["/r/Weather Report.csv"]
    assert out["engine"] == "index"
    assert out["truncated"] is False


def test_index_engine_ors_name_terms(tmp_path):
    cfg = _index(tmp_path, "/r", [("/r/a-cat.txt", 10, 100.0),
                                  ("/r/a-dog.txt", 10, 100.0),
                                  ("/r/a-fish.txt", 10, 100.0)])
    out = _search_index(spec(name_terms=["cat", "dog"]), cfg)
    assert _paths(out) == ["/r/a-cat.txt", "/r/a-dog.txt"]


def test_index_engine_returns_entries_in_the_response_shape(tmp_path):
    cfg = _index(tmp_path, "/r", [("/r/clip.mov", 4096, 1234.5)], dirs=["/r/sub"])
    out = _search_index(spec(extensions=["mov"]), cfg)
    assert out["entries"] == [
        {"path": "/r/clip.mov", "is_dir": False, "size": 4096, "mtime": 1234.5},
    ]
    dirs = _search_index(spec(name_terms=["sub"], kind="dir"), cfg)
    assert dirs["entries"] == [
        {"path": "/r/sub", "is_dir": True, "size": None, "mtime": 1.0},
    ]


def test_index_engine_filters_by_extension(tmp_path):
    cfg = _index(tmp_path, "/r", [("/r/a.MOV", 10, 100.0), ("/r/b.mp4", 10, 100.0),
                                  ("/r/c.txt", 10, 100.0)])
    out = _search_index(spec(extensions=["mov", "mp4"]), cfg)
    assert _paths(out) == ["/r/a.MOV", "/r/b.mp4"]


def test_index_engine_kind_routes_which_views_are_queried(tmp_path):
    cfg = _index(tmp_path, "/r", [("/r/report/report.csv", 10, 100.0)],
                 dirs=["/r/report"])
    both = _search_index(spec(name_terms=["report"]), cfg)
    assert _paths(both) == ["/r/report", "/r/report/report.csv"]
    assert _paths(_search_index(spec(name_terms=["report"], kind="file"), cfg)) == [
        "/r/report/report.csv"]
    assert _paths(_search_index(spec(name_terms=["report"], kind="dir"), cfg)) == [
        "/r/report"]


def test_index_engine_matches_name_terms_against_the_name_not_the_path(tmp_path):
    """Terms match the NAME column: a term that only appears in an ancestor
    directory is a hit on that directory, not on the files inside it (the
    client scores the whole relative path afterwards)."""
    cfg = _index(tmp_path, "/r", [("/r/report/data.csv", 10, 100.0)],
                 dirs=["/r/report"])
    assert _paths(_search_index(spec(name_terms=["report"]), cfg)) == ["/r/report"]


def test_index_engine_skips_the_dirs_view_when_nothing_narrows_a_directory(tmp_path):
    """An extensions-only spec ("find my mp4s") narrows files and nothing else;
    an unpredicated dirs branch would return every folder in the index and
    spend the result cap on rows that answer nothing."""
    cfg = _index(tmp_path, "/r", [("/r/clip.mp4", 10, 100.0)],
                 dirs=["/r/movies"])
    assert _paths(_search_index(spec(extensions=["mp4"]), cfg)) == ["/r/clip.mp4"]
    # dir-only, with nothing a dirs row can answer: refused, not answered with
    # every folder there is — there is no wider engine to hand it to.
    with pytest.raises(ValueError, match="folder"):
        _search_index(spec(extensions=["mp4"], kind="dir"), cfg)


def test_index_engine_lets_dirs_past_extension_and_size_filters(tmp_path):
    """Extension and size are FILE facts, so a dir hit passes them (the index
    has no size for a directory at all)."""
    cfg = _index(tmp_path, "/r", [], dirs=["/r/reports"])
    out = _search_index(
        spec(name_terms=["report"], extensions=["csv"], min_size_bytes=10_000), cfg)
    assert _paths(out) == ["/r/reports"]


def test_index_engine_size_bounds_apply_to_files(tmp_path):
    cfg = _index(tmp_path, "/r", [("/r/tiny.bin", 10, 100.0),
                                  ("/r/mid.bin", 5_000, 100.0),
                                  ("/r/huge.bin", 10_000_000, 100.0)])
    out = _search_index(spec(min_size_bytes=1_000, max_size_bytes=1_000_000), cfg)
    assert _paths(out) == ["/r/mid.bin"]


def test_index_engine_date_ranges_are_inclusive_local_days(tmp_path):
    """`modified_after: 06-01` includes everything from that local midnight and
    `modified_before: 06-30` includes all of the 30th."""
    first = _day_bound_epoch("2026-06-01", False)
    last_end = _day_bound_epoch("2026-06-30", True)
    cfg = _index(tmp_path, "/r", [
        ("/r/before.txt", 10, first - 1),
        ("/r/first-instant.txt", 10, first),
        ("/r/last-instant.txt", 10, last_end - 1),
        ("/r/after.txt", 10, last_end),
    ])
    out = _search_index(
        spec(modified_after="2026-06-01", modified_before="2026-06-30"), cfg)
    assert _paths(out) == ["/r/first-instant.txt", "/r/last-instant.txt"]


def test_index_engine_honors_modified_within_days(tmp_path):
    import time

    now = time.time()
    cfg = _index(tmp_path, "/r", [("/r/fresh.txt", 10, now - 3600),
                                  ("/r/stale.txt", 10, now - 10 * 86400)])
    out = _search_index(spec(modified_within_days=2), cfg)
    assert _paths(out) == ["/r/fresh.txt"]


def test_index_engine_screens_hidden_and_junk_paths(tmp_path):
    """Parity with the other two engines: dot segments and machine-managed
    directories never surface, whatever the index happens to hold."""
    cfg = _index(
        tmp_path, "/r",
        [("/r/.config/keep.txt", 10, 100.0),
         ("/r/node_modules/pkg/keep.txt", 10, 100.0),
         ("/r/keep.txt", 10, 100.0)],
        dirs=["/r/.config", "/r/node_modules", "/r/node_modules/pkg"])
    out = _search_index(spec(name_terms=["keep"]), cfg)
    assert _paths(out) == ["/r/keep.txt"]


def test_index_engine_marks_a_capped_result_set_truncated(tmp_path, monkeypatch):
    cfg = _index(tmp_path, "/r", [(f"/r/hit-{i}.txt", 10, 100.0 + i)
                                  for i in range(5)])
    monkeypatch.setattr(search_mod, "SEARCH_MAX_RESULTS", 3)
    out = _search_index(spec(name_terms=["hit"]), cfg)
    assert len(out["entries"]) == 3
    assert out["truncated"] is True
    monkeypatch.setattr(search_mod, "SEARCH_MAX_RESULTS", 5)
    assert _search_index(spec(name_terms=["hit"]), cfg)["truncated"] is False


def test_index_engine_reads_through_the_manifest_not_a_glob(tmp_path):
    """A compaction leaves the PREVIOUS generation's partitions on disk for
    readers still holding the old manifest (index-store.md §4). Globbing the
    files dir would count both generations and return every row twice."""
    cfg = _index(tmp_path, "/r", [("/r/a.txt", 10, 100.0)])
    shards = str(tmp_path / "ix" / "run2" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t2", pa, pq, cfg.shard_rows)
    sink.add("/r", "s", ("sig2", [("/r/a.txt", "/r", "a.txt", "txt", 10, 100.0)],
                         10, 2_000_000_000, 0))
    sink.close()
    compact(cfg, "/r", shards, pa, pq)
    assert len(os.listdir(cfg.files_dir)) > 1  # both generations on disk
    out = _search_index(spec(name_terms=["a"]), cfg)
    assert [e["path"] for e in out["entries"]] == ["/r/a.txt"]


def test_index_engine_drops_gitignored_hits(tmp_path):
    import subprocess as sp

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("out/\n")
    (repo / "out").mkdir()
    (repo / "out" / "junk.mov").write_bytes(b"x")
    (repo / "keep.mov").write_bytes(b"x")
    root = str(repo).replace(os.sep, "/")
    cfg = _index(tmp_path, root,
                 [(f"{root}/out/junk.mov", 1, 100.0), (f"{root}/keep.mov", 1, 100.0)],
                 dirs=[f"{root}/out"])
    out = _search_index(spec(extensions=["mov"]), cfg)
    assert _paths(out) == [f"{root}/keep.mov"]


# -- when the index cannot answer ---------------------------------------------

def test_a_never_built_index_is_an_error_not_an_empty_disk(tmp_path):
    """With no engine behind it, "no index" cannot be reported as "no matches":
    the box has to say the index is not ready, not imply the file is not there.
    The client shows the message it gets, so the failure is visible."""
    cfg = IndexConfig(dir=str(tmp_path / "empty"))
    with pytest.raises(IndexUnavailable):
        _search_index(spec(name_terms=["x"]), cfg)


def test_a_zero_hit_query_is_an_honest_empty_result(tmp_path):
    """A miss is a miss. There is no wider engine to consult, so an empty
    result set is the answer — reported as one, with `engine` still set."""
    cfg = _index(tmp_path, "/r", [("/r/a.txt", 10, 100.0)])
    out = _search_index(spec(name_terms=["nothing-like-this"]), cfg)
    assert out == {"entries": [], "truncated": False, "engine": "index"}


def test_index_engine_refuses_a_spec_with_no_narrowing_constraint(tmp_path):
    cfg = _index(tmp_path, "/r", [("/r/a.txt", 10, 100.0)])
    with pytest.raises(ValueError):
        _search_index(spec(), cfg)
    with pytest.raises(ValueError):
        _search_index(spec(kind="dir"), cfg)


def test_a_query_failure_is_reported_not_swallowed(tmp_path):
    """A corrupt or half-deleted store used to degrade to Spotlight; with
    nothing to degrade to, it has to surface."""
    cfg = _index(tmp_path, "/r", [("/r/a.txt", 10, 100.0)])
    for name in os.listdir(cfg.files_dir):
        with open(os.path.join(cfg.files_dir, name), "wb") as f:
            f.write(b"not parquet")
    with pytest.raises(RuntimeError):
        _search_index(spec(name_terms=["a"]), cfg)


# -- the endpoint --------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_the_endpoint_has_no_engine_but_the_index(client, tmp_path, monkeypatch):
    """Spotlight and the home walk are gone, not merely deprioritized."""
    for gone in ("_search_mdfind", "_search_walk_home", "_mdfind_query",
                 "_match_walk_entry", "_stat_entry"):
        assert not hasattr(search_mod, gone), gone
    cfg = _index(tmp_path, "/r", [("/r/quarterly.csv", 10, 100.0)])
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    res = client.post("/api/search/files", json={"name_terms": ["quarterly"]})
    assert res.status_code == 200
    body = res.json()
    assert body["engine"] == "index"
    assert [e["path"] for e in body["entries"]] == ["/r/quarterly.csv"]


def test_the_endpoint_reports_a_missing_index_as_a_503(client, tmp_path, monkeypatch):
    cfg = IndexConfig(dir=str(tmp_path / "empty"))
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    res = client.post("/api/search/files", json={"name_terms": ["quarterly"]})
    assert res.status_code == 503
    # A message a search box can show verbatim, not a traceback.
    assert "index" in res.json()["error"]


def test_the_endpoint_returns_an_empty_ok_for_a_miss(client, tmp_path, monkeypatch):
    cfg = _index(tmp_path, "/r", [("/r/a.txt", 10, 100.0)])
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    res = client.post("/api/search/files", json={"name_terms": ["zzzz"]})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "entries": [], "truncated": False,
                          "engine": "index"}


def test_the_endpoint_rejects_a_creation_date_filter(client, tmp_path, monkeypatch):
    cfg = _index(tmp_path, "/r", [("/r/a.txt", 10, 100.0)])
    monkeypatch.setattr(search_mod, "load_config", lambda: cfg)
    res = client.post("/api/search/files",
                      json={"name_terms": ["a"], "created_after": "2026-06-01"})
    assert res.status_code == 400
    assert "created" in res.json()["error"]

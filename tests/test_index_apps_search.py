"""`search_apps_ranked` — a registered kind's rows, ranked the same way
`search_ranked` ranks "files", without a directory tree to hang them off of.

See fused_render/index/specs/index-plugins.md's "apps-kind search" section
(and DECISIONS-index-plugins.md's "Open questions carried forward" this
resolves): `_rank_sql`/`_glob_sql` were already written against an abstract
`inner` subquery shape (`rel, size, mtime, is_dir, depth, nm, lrel`), not
hardwired to files/dirs — this module's tests are what confirms that
hypothesis rather than merely asserting it.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fused_render.index.config import IndexConfig
from fused_render.index.kinds import Column, IndexKind, register
from fused_render.index.query import search_apps_ranked
from fused_render.index.store import Sink, compact

KIND_NAME = "_test_apps_search"


def _register_kind(name=KIND_NAME, recency=True):
    kind = IndexKind(
        name=name,
        columns=(Column("name", "string"), Column("path", "string"),
                  Column("updated_at", "float64")),
        extract=lambda path, st: None,
        text_column="name",
        identity_column="path",
        recency_column="updated_at" if recency else None,
    )
    register(kind, replace=True)
    return kind


def _index(tmp_path, kind_name=KIND_NAME, root="/r", rows=()):
    cfg = IndexConfig(dir=str(tmp_path / "ix"), kind=kind_name)
    shards = str(tmp_path / "run" / "shards")
    os.makedirs(shards, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows, kind=kind_name)
    payload = [{"name": n, "path": p, "updated_at": u} for n, p, u in rows]
    sink.add(root, "s", ("sig", payload, 0, 1, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


# -- the read path works, against a real registered kind -----------------------

def test_a_substring_match_against_the_text_column_ranks_first(tmp_path):
    """A hit whose NAME contains the query outranks one where the query only
    appears elsewhere in the identity column (the path) — the same tier
    split `_rank_sql` already gives files vs. their ancestor directories."""
    _register_kind()
    cfg = _index(tmp_path, rows=[
        ("editor", "/apps/editor/index.html", 100.0),
        ("thing", "/apps/editor-folder/thing/index.html", 200.0),
    ])
    out = search_apps_ranked(cfg, "editor")
    assert [h["rel"] for h in out["hits"]] == [
        "/apps/editor/index.html", "/apps/editor-folder/thing/index.html"]


def test_an_exact_name_match_scores_above_a_prefix_match(tmp_path):
    _register_kind()
    cfg = _index(tmp_path, rows=[
        ("noteapp", "/apps/noteapp/index.html", 1.0),
        ("note", "/apps/note/index.html", 2.0),
    ])
    out = search_apps_ranked(cfg, "note")
    assert out["hits"][0]["rel"] == "/apps/note/index.html"


def test_the_recency_column_surfaces_as_mtime(tmp_path):
    _register_kind()
    cfg = _index(tmp_path, rows=[("solo", "/apps/solo/index.html", 42.0)])
    out = search_apps_ranked(cfg, "solo")
    assert out["hits"][0]["mtime"] == 42.0


def test_a_kind_without_a_recency_column_reports_no_mtime(tmp_path):
    _register_kind(name="_test_apps_search_no_recency", recency=False)
    cfg = _index(tmp_path, kind_name="_test_apps_search_no_recency",
                 rows=[("solo", "/apps/solo/index.html", 0.0)])
    out = search_apps_ranked(cfg, "solo")
    assert out["hits"][0]["mtime"] is None


def test_every_hit_is_not_a_directory(tmp_path):
    """A registered kind's rows have no directory concept of their own —
    every hit answers `is_dir: False`, never the "files vs. dirs" branch
    `search_ranked` runs for the built-in files/dirs index."""
    _register_kind()
    cfg = _index(tmp_path, rows=[("solo", "/apps/solo/index.html", 0.0)])
    out = search_apps_ranked(cfg, "solo")
    assert out["hits"][0]["is_dir"] is False


def test_an_empty_query_answers_with_no_hits(tmp_path):
    _register_kind()
    cfg = _index(tmp_path, rows=[("solo", "/apps/solo/index.html", 0.0)])
    assert search_apps_ranked(cfg, "")["hits"] == []
    assert search_apps_ranked(cfg, "   ")["hits"] == []


def test_an_unbuilt_index_answers_with_no_hits_rather_than_an_error(tmp_path):
    _register_kind(name="_test_apps_search_unbuilt")
    cfg = IndexConfig(dir=str(tmp_path / "ix-unbuilt"),
                      kind="_test_apps_search_unbuilt")
    out = search_apps_ranked(cfg, "anything")
    assert out == {"hits": [], "truncated": False, "total": 0, "covered": True}


def test_unranked_orders_by_identity_column_alone(tmp_path):
    """`ranked=False` (D720's unranked preference) drops the whole scoring
    apparatus the same way it does for `search_ranked` — ordered by the
    identity column, since there is no meaningful "depth" for a flat kind."""
    _register_kind()
    cfg = _index(tmp_path, rows=[
        ("b-app", "/apps/b-app/index.html", 0.0),
        ("a-app", "/apps/a-app/index.html", 0.0),
    ])
    out = search_apps_ranked(cfg, "app", ranked=False)
    assert [h["rel"] for h in out["hits"]] == [
        "/apps/a-app/index.html", "/apps/b-app/index.html"]


def test_glob_mode_matches_the_identity_column_by_pattern(tmp_path):
    """A bare `*` never crosses a `/` (`_glob_to_regex`'s documented
    single-segment semantics, shared with `search_ranked`'s own glob mode)
    — `**` does, which is what a pattern needs here since the identity
    column is a full, multi-segment absolute path, not a root-relative
    single name."""
    _register_kind()
    cfg = _index(tmp_path, rows=[
        ("one", "/apps/one/index.html", 0.0),
        ("two", "/apps/two/index.html", 0.0),
    ])
    out = search_apps_ranked(cfg, "**one**", glob=True)
    assert [h["rel"] for h in out["hits"]] == ["/apps/one/index.html"]


def test_a_kind_with_no_identity_column_raises(tmp_path):
    kind = IndexKind(
        name="_test_apps_search_no_identity",
        columns=(Column("name", "string"),),
        extract=lambda path, st: None,
        text_column="name",
    )
    register(kind, replace=True)
    cfg = IndexConfig(dir=str(tmp_path / "ix-no-id"),
                      kind="_test_apps_search_no_identity")
    with pytest.raises(ValueError):
        search_apps_ranked(cfg, "anything")


def test_the_row_cap_is_enforced_and_flagged(tmp_path):
    _register_kind()
    cfg = _index(tmp_path, rows=[
        (f"app{i}", f"/apps/app{i}/index.html", 0.0) for i in range(5)])
    out = search_apps_ranked(cfg, "app", limit=2)
    assert len(out["hits"]) == 2
    assert out["truncated"] is True

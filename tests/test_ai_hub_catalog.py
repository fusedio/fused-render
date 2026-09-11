"""Tests for the on-device Hub catalog store (`fused_render.ai.hub_catalog`,
D1236+). No network involved at this layer at all — this module never makes
a request, it only reads/writes parquet + a manifest under
`storage.home_dir()/hub_catalog`, so unlike `test_ai_hub_metadata.py` there is
no seam to monkeypatch, only an isolated home dir per test.
"""
import os

import pytest

from fused_render.ai import hub_catalog
from fused_render.ai.hub_catalog_config import load_config


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _row(repo_id, capability="text-generation", fmt="mlx", downloads=10,
         likes=1, last_modified="2026-01-01T00:00:00.000Z"):
    return {
        "capability": capability,
        "format": fmt,
        "raw": {
            "id": repo_id,
            "downloads": downloads,
            "likes": likes,
            "lastModified": last_modified,
            "createdAt": "2025-01-01T00:00:00.000Z",
            "library_name": "mlx",
            "gated": False,
            "private": False,
            "tags": [fmt, capability],
            "pipeline_tag": capability,
        },
    }


def test_no_pool_reads_as_absent_everywhere():
    cfg = load_config()
    assert hub_catalog.pool_entry(cfg, "text-generation") is None
    assert hub_catalog.pool_exists(cfg, "text-generation") is False
    assert hub_catalog.is_blocked(cfg, "text-generation") is False
    assert hub_catalog.query_pool(cfg, "text-generation") == []


def test_write_pool_then_read_it_back():
    cfg = load_config()
    rows = [_row("org/model-a", downloads=100), _row("org/model-b", downloads=5)]
    entry = hub_catalog.write_pool(cfg, "text-generation", rows)

    assert entry["generation"] == 1
    assert entry["rows"] == 2
    assert hub_catalog.pool_exists(cfg, "text-generation") is True

    got = hub_catalog.query_pool(cfg, "text-generation")
    assert {r["id"] for r in got} == {"org/model-a", "org/model-b"}


def test_manifest_written_last_generation_bumps_and_old_file_is_reclaimed():
    cfg = load_config()
    entry1 = hub_catalog.write_pool(cfg, "text-generation", [_row("org/a")])
    path1 = os.path.join(cfg.pools_dir, entry1["file"])
    assert os.path.exists(path1)

    entry2 = hub_catalog.write_pool(cfg, "text-generation", [_row("org/b")])
    assert entry2["generation"] == 2
    assert entry2["file"] != entry1["file"]
    # previous generation's file is reclaimed once the new one is live
    assert not os.path.exists(path1)
    assert os.path.exists(os.path.join(cfg.pools_dir, entry2["file"]))

    got = hub_catalog.query_pool(cfg, "text-generation")
    assert [r["id"] for r in got] == ["org/b"]


def test_pools_are_independent_per_capability():
    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [_row("org/text")])
    hub_catalog.write_pool(cfg, "image-generation", [_row("org/image", capability="image-generation")])

    assert hub_catalog.pool_exists(cfg, "text-generation")
    assert hub_catalog.pool_exists(cfg, "image-generation")
    assert [r["id"] for r in hub_catalog.query_pool(cfg, "text-generation")] == ["org/text"]
    assert [r["id"] for r in hub_catalog.query_pool(cfg, "image-generation")] == ["org/image"]


def test_query_pool_where_clause_filters_over_indexed_columns():
    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [
        _row("org/big", downloads=1000), _row("org/small", downloads=1),
    ])
    got = hub_catalog.query_pool(cfg, "text-generation", where="downloads > 100")
    assert [r["id"] for r in got] == ["org/big"]


def test_blocked_until_persists_and_expires():
    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [_row("org/a")])
    hub_catalog.set_blocked_until(cfg, "text-generation", until=1e15)  # far future
    assert hub_catalog.is_blocked(cfg, "text-generation") is True
    # pool itself is untouched by a block
    assert hub_catalog.pool_exists(cfg, "text-generation") is True
    assert [r["id"] for r in hub_catalog.query_pool(cfg, "text-generation")] == ["org/a"]

    hub_catalog.set_blocked_until(cfg, "text-generation", until=1.0)  # long past
    assert hub_catalog.is_blocked(cfg, "text-generation") is False


def test_blocked_until_can_be_set_before_any_pool_exists():
    cfg = load_config()
    hub_catalog.set_blocked_until(cfg, "text-generation", until=1e15)
    assert hub_catalog.is_blocked(cfg, "text-generation") is True
    assert hub_catalog.pool_exists(cfg, "text-generation") is False


def test_delete_catalog_clears_everything():
    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [_row("org/a")])
    hub_catalog.delete_catalog(cfg)
    assert hub_catalog.pool_exists(cfg, "text-generation") is False
    assert hub_catalog.read_manifest(cfg)["capabilities"] == {}


def test_mixed_str_and_bool_gated_values_do_not_raise():
    """Finding: a real capability's pool mixes repos with `gated: False`
    (bool), `gated: True` (bool), and `gated: "auto"`/`"manual"` (str) in the
    SAME column. `pa.table`'s type inference cannot pick one Arrow type for
    a Python list holding both bools and strs and used to raise, so any
    build of a non-trivial capability crashed before ever writing a pool."""
    cfg = load_config()
    rows = [
        _row("org/ungated", downloads=1)
        | {"raw": {**_row("org/ungated")["raw"], "gated": False}},
        _row("org/auto", downloads=2)
        | {"raw": {**_row("org/auto")["raw"], "gated": "auto"}},
        _row("org/manual", downloads=3)
        | {"raw": {**_row("org/manual")["raw"], "gated": "manual"}},
        _row("org/booltrue", downloads=4)
        | {"raw": {**_row("org/booltrue")["raw"], "gated": True}},
    ]
    entry = hub_catalog.write_pool(cfg, "text-generation", rows)
    assert entry["rows"] == 4
    ids = {r["id"] for r in hub_catalog.query_pool(cfg, "text-generation")}
    assert ids == {"org/ungated", "org/auto", "org/manual", "org/booltrue"}


def test_query_pool_on_blocked_before_any_build_returns_empty_not_keyerror():
    """Finding: `set_blocked_until` can write a manifest entry with no
    `"file"` key (a block set before the capability's first build ever
    ran). `query_pool` only checked `entry is None`, then did
    `entry["file"]` unconditionally — raising `KeyError` for exactly this
    state instead of returning `[]`, the same "no pool yet" answer
    `pool_exists` already gives it."""
    cfg = load_config()
    hub_catalog.set_blocked_until(cfg, "text-generation", until=1e15)
    assert hub_catalog.pool_entry(cfg, "text-generation") is not None
    assert "file" not in hub_catalog.pool_entry(cfg, "text-generation")
    assert hub_catalog.query_pool(cfg, "text-generation") == []


def test_malformed_row_fields_degrade_rather_than_raise():
    cfg = load_config()
    bad_row = {"capability": "text-generation", "format": "mlx", "raw": {
        "id": "org/weird", "downloads": "not-a-number", "likes": None,
        "lastModified": 12345, "library_name": None, "gated": "auto",
    }}
    entry = hub_catalog.write_pool(cfg, "text-generation", [bad_row])
    assert entry["rows"] == 1
    got = hub_catalog.query_pool(cfg, "text-generation")
    assert got[0]["id"] == "org/weird"

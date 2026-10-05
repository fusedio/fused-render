"""Streaming pool writes: a build holds one Hub page at a time, not the slice.

The server used to keep ~105k raw Hub dicts alive to hand `write_pool` one
list, and the fragmented pymalloc arenas that left behind were never returned
to the OS. `PoolWriter` + the page-callback fetch bound a build to one page;
these tests pin that the streamed file is row-for-row the old one.

No network: `httpx.get` is faked, same discipline as
`test_ai_hub_catalog_builder.py` (a real child process is in
`test_ai_hub_catalog_child.py`, against a local HTTP server).
"""
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pyarrow.parquet as pq
import pytest

from fused_render.ai import hub_catalog
from fused_render.ai import hub_catalog_builder as builder
from fused_render.ai.hub_catalog_config import HubCatalogConfig, load_config

TAGS = ("tag-a", "tag-b")
FORMATS = ("gguf", "mlx")
PAGES = 3
PAGE_ROWS = 4


class _Runner:
    hub_filter_tags = FORMATS
    hub_pool_tags = FORMATS


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(builder, "_hub_endpoint", lambda: "https://hub.test")
    monkeypatch.setattr(builder, "_token", lambda: None)
    monkeypatch.setattr(builder, "available_runners", lambda cap: (_Runner(),))
    monkeypatch.setattr(builder.ai_tasks, "tags_for_capability", lambda cap: TAGS)


def _row(repo_id, tag, fmt):
    return {"id": repo_id, "downloads": len(repo_id), "likes": 2,
            "lastModified": f"2026-01-{(len(repo_id) % 28) + 1:02d}T00:00:00.000Z",
            "createdAt": "2025-01-01T00:00:00.000Z", "library_name": fmt,
            "gated": "auto" if repo_id.endswith("1") else False,
            "pipeline_tag": tag, "tags": [tag, fmt],
            "config": {"model_type": "llama"},
            "siblings": [{"rfilename": "model_index.json"}]}


def _dataset():
    """{(tag, fmt): [page, ...]} — ids collide across pairs (every pair
    repeats `shared/<page>-<i>`) so first-writer-wins dedupe is exercised."""
    data = {}
    for tag in TAGS:
        for fmt in FORMATS:
            pages = []
            for p in range(PAGES):
                page = []
                for i in range(PAGE_ROWS):
                    rid = (f"shared/{p}-{i}" if i == 0
                           else f"{tag}-{fmt}/{p}-{i}")
                    page.append(_row(rid, tag, fmt))
                pages.append(page)
            data[(tag, fmt)] = pages
    return data


def _fake_get(data, calls=None):
    def fake_get(url, headers=None, **kw):
        q = parse_qs(urlsplit(url).query)
        tag, fmt = q["filter"]
        page = int(q.get("page", ["0"])[0])
        if calls is not None:
            calls.append((tag, fmt, page))
        rows = data[(tag, fmt)][page]
        hdrs = {}
        if page + 1 < len(data[(tag, fmt)]):
            hdrs["Link"] = f'<{url.split("&page=")[0]}&page={page + 1}>; rel="next"'
        return httpx.Response(200, content=json.dumps(rows).encode(), headers=hdrs,
                              request=httpx.Request("GET", url))
    return fake_get


def _legacy_rows(data):
    """What the OLD `_build_capability_pool_inner` handed `write_pool`."""
    merged = {}
    for tag in TAGS:
        for fmt in FORMATS:
            for page in data[(tag, fmt)]:
                for raw in page:
                    merged.setdefault(raw["id"], {
                        "capability": "text-generation", "format": fmt, "raw": raw})
    return list(merged.values())


def _read(cfg, capability):
    entry = hub_catalog.pool_entry(cfg, capability)
    return pq.read_table(f"{cfg.pools_dir}/{entry['file']}")


def test_streamed_build_matches_write_pool_for_multipage_multiformat_dupes(
        monkeypatch, tmp_path):
    data = _dataset()
    monkeypatch.setattr(httpx, "get", _fake_get(data))
    cfg = load_config()

    appended = []
    real_append = hub_catalog.PoolWriter.append

    def spy(self, rows):
        appended.append(len(rows))
        return real_append(self, rows)

    monkeypatch.setattr(hub_catalog.PoolWriter, "append", spy)
    result = builder.build_capability_pool(cfg, "text-generation")
    streamed_batches = list(appended)

    legacy_cfg = HubCatalogConfig(dir=str(tmp_path / "legacy"))
    expected = _legacy_rows(data)
    hub_catalog.write_pool(legacy_cfg, "text-generation", expected)

    assert result == {"rows": len(expected), "rateLimited": False}
    got = _read(cfg, "text-generation")
    want = _read(legacy_cfg, "text-generation")
    assert got.schema == want.schema
    assert got.to_pylist() == want.to_pylist()
    # Bounded by one page, never the slice.
    assert max(streamed_batches) <= PAGE_ROWS
    assert len(streamed_batches) > 1

    entry = hub_catalog.pool_entry(cfg, "text-generation")
    assert entry["rows"] == len(expected)
    assert entry["pages"] == len(TAGS) * len(FORMATS) * PAGES
    assert entry["formats"] == list(FORMATS)
    assert entry["schemaVersion"] == hub_catalog.ROW_SCHEMA_VERSION
    assert entry["generation"] == 1
    # query_pool callers see the identical raw dicts.
    assert hub_catalog.query_pool(cfg, "text-generation") == [r["raw"] for r in expected]


def test_streamed_build_never_materialises_all_rows(monkeypatch):
    """`_fetch_all_pages` with a page callback must not accumulate rows."""
    data = _dataset()
    monkeypatch.setattr(httpx, "get", _fake_get(data))
    seen = []
    rows, reset, err = builder._fetch_all_pages(
        "tag-a", "gguf", on_rows=lambda page: seen.append(len(page)))
    assert (rows, reset, err) == ([], None, None)
    assert seen == [PAGE_ROWS] * PAGES


def test_failed_stream_leaves_no_pool_file_and_no_manifest_entry(monkeypatch):
    data = _dataset()
    good = _fake_get(data)
    state = {"n": 0}

    def flaky(url, headers=None, **kw):
        state["n"] += 1
        if state["n"] == 5:
            return httpx.Response(500, request=httpx.Request("GET", url))
        return good(url, headers)

    monkeypatch.setattr(httpx, "get", flaky)
    cfg = load_config()
    result = builder.build_capability_pool(cfg, "text-generation")
    assert result == {"rows": 0, "error": True}
    assert hub_catalog.pool_entry(cfg, "text-generation") is None
    import os
    assert not os.path.isdir(cfg.pools_dir) or os.listdir(cfg.pools_dir) == []


def test_rate_limited_stream_writes_no_pool_and_sets_block(monkeypatch):
    data = _dataset()
    good = _fake_get(data)
    state = {"n": 0}

    def limited(url, headers=None, **kw):
        state["n"] += 1
        if state["n"] == 4:
            return httpx.Response(429, headers={"RateLimit": "limit=1, remaining=0, reset=60"},
                                  request=httpx.Request("GET", url))
        return good(url, headers)

    monkeypatch.setattr(httpx, "get", limited)
    cfg = load_config()
    result = builder.build_capability_pool(cfg, "text-generation")
    assert result["rateLimited"] is True
    assert not hub_catalog.pool_exists(cfg, "text-generation")
    assert hub_catalog.is_blocked(cfg, "text-generation")
    import os
    assert not any(n.endswith(".tmp") for n in os.listdir(cfg.pools_dir))


def test_pool_writer_abort_and_exception_clean_up_temp(tmp_path):
    import os
    cfg = HubCatalogConfig(dir=str(tmp_path / "c"))
    with pytest.raises(RuntimeError):
        with hub_catalog.PoolWriter(cfg, "cap") as w:
            w.append([{"capability": "cap", "format": "x", "raw": {"id": "a/b"}}])
            raise RuntimeError("boom")
    assert os.listdir(cfg.pools_dir) == []
    assert hub_catalog.pool_entry(cfg, "cap") is None


def test_delta_streams_existing_pool_and_matches_old_merge(monkeypatch, tmp_path):
    data = _dataset()
    monkeypatch.setattr(httpx, "get", _fake_get(data))
    cfg = load_config()
    builder.build_capability_pool(cfg, "text-generation")
    before = {r["id"]: r for r in hub_catalog.query_pool(cfg, "text-generation")}

    changed = dict(before["shared/0-0"], downloads=999999, lastModified="2099-01-01T00:00:00.000Z")
    brand_new = _row("fresh/new", "tag-a", "gguf")
    brand_new["lastModified"] = "2099-02-01T00:00:00.000Z"

    def delta_get(url, headers=None, **kw):
        return httpx.Response(200, content=json.dumps([brand_new, changed]).encode(),
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", delta_get)
    seen_existing = []
    real_iter = hub_catalog.iter_pool_rows

    def spy_iter(c, cap):
        for batch in real_iter(c, cap):
            seen_existing.append(len(batch))
            yield batch

    monkeypatch.setattr(hub_catalog, "iter_pool_rows", spy_iter)
    result = builder._refresh_delta_inprocess(cfg, "text-generation")

    expected = dict(before)
    expected["shared/0-0"] = changed
    expected["fresh/new"] = brand_new
    after = {r["id"]: r for r in hub_catalog.query_pool(cfg, "text-generation")}
    assert after == expected
    assert result["rows"] == len(expected)
    assert max(seen_existing) <= 1000
    entry = hub_catalog.pool_entry(cfg, "text-generation")
    assert entry["generation"] == 2 and entry["rows"] == len(expected)
    assert all(v == "" for v in _read(cfg, "text-generation").column("format").to_pylist())

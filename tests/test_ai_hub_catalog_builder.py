"""Tests for the bulk Hub catalog builder (`fused_render.ai.hub_catalog_builder`).

Same no-egress discipline as `tests/test_hub_models.py`: `httpx.get` is
replaced per test with a canned `httpx.Response`, never a real socket. The
builder's own seams (`_hub_endpoint`/`_token`) are monkeypatched too, so no
import-time state from `hub_models.py`/`registry.py` (an active token, a
different active runner on CI) can change what these tests exercise.
"""
import json
import time

import httpx
import pytest

from fused_render.ai import hub_catalog
from fused_render.ai import hub_catalog_builder as builder
from fused_render.ai.hub_catalog_config import load_config
from fused_render.ai import registry
from fused_render.ai import tasks as ai_tasks


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(builder, "_hub_endpoint", lambda: "https://hub.test")
    monkeypatch.setattr(builder, "_token", lambda: None)
    builder._building.clear()


def _hit(repo_id, **extra):
    row = {"id": repo_id, "downloads": 10, "likes": 1,
           "lastModified": "2026-01-01T00:00:00.000Z", "library_name": "mlx",
           "pipeline_tag": "text-generation", "tags": ["mlx"]}
    row.update(extra)
    return row


def _resp(rows, status=200, headers=None):
    return httpx.Response(status, content=json.dumps(rows).encode(),
                           headers=headers or {},
                           request=httpx.Request("GET", "https://hub.test/api/models"))


def test_single_page_build_writes_a_pool(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp([_hit("org/a"), _hit("org/b")]))
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)

    result = builder.build_capability_pool(load_config(), "text-generation")

    assert result == {"rows": 2, "rateLimited": False}
    cfg = load_config()
    assert hub_catalog.pool_exists(cfg, "text-generation")
    ids = {r["id"] for r in hub_catalog.query_pool(cfg, "text-generation")}
    assert ids == {"org/a", "org/b"}


def test_pagination_follows_link_next_header(monkeypatch):
    calls = []

    def fake_get(url, *a, **k):
        calls.append(url)
        if len(calls) == 1:
            return _resp([_hit("org/page1")],
                         headers={"Link": '<https://hub.test/api/models?cursor=2>; rel="next"'})
        return _resp([_hit("org/page2")])

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)

    result = builder.build_capability_pool(load_config(), "automatic-speech-recognition")

    assert result["rows"] == 2
    assert len(calls) == 2
    assert calls[1] == "https://hub.test/api/models?cursor=2"
    cfg = load_config()
    ids = {r["id"] for r in hub_catalog.query_pool(cfg, "automatic-speech-recognition")}
    assert ids == {"org/page1", "org/page2"}


def test_429_partway_through_never_writes_a_partial_pool(monkeypatch):
    """C1 (bugbot): a 429 that lands AFTER some rows were already merged from
    earlier pages must not be allowed to commit those rows as a served pool
    — `write_pool` must be skipped entirely whenever a rate limit was hit
    anywhere in this build, exactly like the already-empty case below. A
    partial pool being servable is worse than no pool: `pool_exists` would
    read True forever after, and `ensure_build_started` refuses to ever
    start a fresh build for a capability whose pool already "exists" — so a
    429 on page 3 of a real build used to permanently truncate that
    capability's catalog to whatever page 1-2 happened to contain."""
    calls = []

    def fake_get(url, *a, **k):
        calls.append(url)
        if len(calls) == 1:
            return _resp([_hit("org/first")],
                         headers={"Link": '<https://hub.test/api/models?cursor=2>; rel="next"'})
        return _resp([], status=429, headers={"RateLimit": "limit=100, remaining=0, reset=120"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)

    before = time.time()
    result = builder.build_capability_pool(load_config(), "automatic-speech-recognition")

    assert result["rateLimited"] is True
    assert result["rows"] == 1  # reports what was seen, but does NOT persist it
    cfg = load_config()
    assert not hub_catalog.pool_exists(cfg, "automatic-speech-recognition")
    assert hub_catalog.is_blocked(cfg, "automatic-speech-recognition")
    entry = hub_catalog.pool_entry(cfg, "automatic-speech-recognition")
    # roughly "now + 120s", not the fallback default
    assert before + 110 < entry["blockedUntil"] < before + 130
    assert "file" not in entry

    # Once the block clears, a fresh build is not permanently refused (the
    # old bug: `ensure_build_started` sees `pool_exists() == True` forever).
    monkeypatch.setattr(hub_catalog, "is_blocked", lambda *a, **k: False)
    assert builder.ensure_build_started("automatic-speech-recognition", cfg=cfg) is True


def test_429_with_no_rows_yet_leaves_no_pool_but_sets_the_block(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp(
        [], status=429, headers={"RateLimit": "limit=100, remaining=0, reset=30"}))
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)

    result = builder.build_capability_pool(load_config(), "automatic-speech-recognition")

    assert result == {"rows": 0, "rateLimited": True}
    cfg = load_config()
    assert not hub_catalog.pool_exists(cfg, "automatic-speech-recognition")
    assert hub_catalog.is_blocked(cfg, "automatic-speech-recognition")


def test_unparseable_ratelimit_header_falls_back_to_default_backoff(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp(
        [], status=429, headers={"RateLimit": "not-a-real-header"}))
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)

    before = time.time()
    builder.build_capability_pool(load_config(), "automatic-speech-recognition")

    cfg = load_config()
    entry = hub_catalog.pool_entry(cfg, "automatic-speech-recognition")
    assert before + builder._DEFAULT_BACKOFF_S - 5 < entry["blockedUntil"]


def test_format_filter_pages_each_runner_format_separately(monkeypatch):
    seen_filters = []

    class FakeRunner:
        hub_filter_tags = ("mlx", "gguf")

    def fake_get(url, *a, **k):
        seen_filters.append(url)
        if "mlx" in url:
            return _resp([_hit("org/mlx-model")])
        return _resp([_hit("org/gguf-model")])

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(builder, "for_capability", lambda cap: FakeRunner())

    result = builder.build_capability_pool(load_config(), "text-generation")

    assert result["rows"] == 2
    cfg = load_config()
    ids = {r["id"] for r in hub_catalog.query_pool(cfg, "text-generation")}
    assert ids == {"org/mlx-model", "org/gguf-model"}


def test_fetch_error_aborts_build_without_writing_a_truncated_pool(monkeypatch):
    """Finding: a network error / 5xx / non-JSON body on one (tag, format)
    pair used to be silently discarded (`_error` from `_fetch_all_pages` was
    never checked), so the loop moved on to the next pair and `write_pool`
    committed whatever HAD been merged so far. `pool_exists` then reads that
    truncated pool as built forever — the daily delta only ever widens an
    existing pool by `lastModified`, it never backfills a whole tag/format
    slice a build silently skipped. A genuine fetch error must abort the
    build instead: no pool written, so the next trigger retries from
    scratch."""
    calls = []

    def fake_get(url, *a, **k):
        calls.append(url)
        if len(calls) == 1:
            return _resp([_hit("org/first-tag-ok")])
        # Second (tag, format) pair: a 5xx the builder must not swallow.
        return _resp([], status=500)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)
    # TEXT_GENERATION resolves to more than one tag so a second pair exists
    # to fail on.
    monkeypatch.setattr(builder.ai_tasks, "tags_for_capability",
                         lambda cap: ("text-generation", "text2text-generation"))

    result = builder.build_capability_pool(load_config(), "text-generation")

    assert result == {"rows": 0, "error": True}
    cfg = load_config()
    assert not hub_catalog.pool_exists(cfg, "text-generation")


def test_fetch_error_leaves_an_existing_pool_untouched_on_delta_refresh(monkeypatch):
    """Same finding, delta path (`refresh_capability_pool_delta`): a fetch
    error on one (tag, format) pair must not overwrite the pool that was
    already there with a merge missing that pair's rows."""
    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [
        {"capability": "text-generation", "format": "",
         "raw": _hit("org/existing", lastModified="2026-01-01T00:00:00.000Z")},
    ])
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)
    monkeypatch.setattr(builder.ai_tasks, "tags_for_capability",
                         lambda cap: ("text-generation",))

    def fake_get(url, *a, **k):
        return _resp([], status=500)

    monkeypatch.setattr(httpx, "get", fake_get)

    result = builder.refresh_capability_pool_delta(cfg, "text-generation")

    assert result == {"skipped": "error"}
    ids = {r["id"] for r in hub_catalog.query_pool(cfg, "text-generation")}
    assert ids == {"org/existing"}


def test_ensure_build_started_skips_when_pool_already_built(monkeypatch):
    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [
        {"capability": "text-generation", "format": "mlx", "raw": _hit("org/existing")},
    ])
    started = builder.ensure_build_started("text-generation", cfg=cfg)
    assert started is False


def test_ensure_build_started_skips_while_blocked(monkeypatch):
    cfg = load_config()
    hub_catalog.set_blocked_until(cfg, "text-generation", time.time() + 1000)
    started = builder.ensure_build_started("text-generation", cfg=cfg)
    assert started is False


def test_ensure_build_started_builds_in_background(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp([_hit("org/bg")]))
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)
    cfg = load_config()

    started = builder.ensure_build_started("text-generation", cfg=cfg)
    assert started is True

    thread = builder._building["text-generation"]
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert hub_catalog.pool_exists(cfg, "text-generation")


# -- refresh_capability_pool_delta (spec item 4) ------------------------------


def test_delta_refresh_skips_a_capability_with_no_pool_built():
    cfg = load_config()
    result = builder.refresh_capability_pool_delta(cfg, "text-generation")
    assert result == {"skipped": "no-pool"}


def test_delta_refresh_skips_a_blocked_pool(monkeypatch):
    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [
        {"capability": "text-generation", "format": "mlx", "raw": _hit("org/existing")},
    ])
    hub_catalog.set_blocked_until(cfg, "text-generation", time.time() + 1000)

    def _boom(*a, **k):
        raise AssertionError("must not reach the Hub while blocked")

    monkeypatch.setattr(httpx, "get", _boom)
    result = builder.refresh_capability_pool_delta(cfg, "text-generation")
    assert result == {"skipped": "blocked"}


def test_delta_refresh_widens_the_pool_with_newer_rows_only(monkeypatch):
    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [
        {"capability": "text-generation", "format": "",
         "raw": _hit("org/old", lastModified="2026-01-01T00:00:00.000Z")},
    ])
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)

    new_row = _hit("org/new", lastModified="2026-01-05T00:00:00.000Z")
    old_row_again = _hit("org/old", lastModified="2026-01-01T00:00:00.000Z")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp([new_row, old_row_again]))

    result = builder.refresh_capability_pool_delta(cfg, "text-generation")
    assert result["rows"] == 2
    ids = {r["id"] for r in hub_catalog.query_pool(cfg, "text-generation")}
    assert ids == {"org/old", "org/new"}


def test_delta_refresh_invalidates_hub_metadata_for_changed_repos_only(monkeypatch):
    """item 4/5 hook: a repo the delta actually saw (its `lastModified` is
    newer than the pool's watermark) must be invalidated in `hub_metadata` so
    its harvested `config.json` reading gets re-checked, while an untouched
    repo already in the pool is left alone."""
    from fused_render.ai import hub_metadata

    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [
        {"capability": "text-generation", "format": "",
         "raw": _hit("org/untouched", lastModified="2026-01-01T00:00:00.000Z")},
    ])
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)

    # Seed hub_metadata entries for both repos so invalidate() has something
    # to act on (a no-entry repo is a documented no-op, not useful here).
    hub_metadata._upsert("org/untouched", {"meta": {"modelType": "a"}, "fetchedAt": time.time()})
    hub_metadata._upsert("org/changed", {"meta": {"modelType": "b"}, "fetchedAt": time.time()})

    new_row = _hit("org/changed", lastModified="2026-01-05T00:00:00.000Z")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp([new_row]))

    builder.refresh_capability_pool_delta(cfg, "text-generation")

    untouched_fetched_at = hub_metadata._load()["repos"]["org/untouched"]["fetchedAt"]
    changed_fetched_at = hub_metadata._load()["repos"]["org/changed"]["fetchedAt"]
    assert untouched_fetched_at > 0.0  # never invalidated
    assert changed_fetched_at == 0.0  # invalidate() resets to 0.0


# -- instrumentation (spec item 1) -------------------------------------------


def test_build_records_started_at_pages_and_build_seconds_on_manifest(monkeypatch, caplog):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp([_hit("org/a"), _hit("org/b")]))
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)

    before = time.time()
    with caplog.at_level("INFO", logger="fused_render.ai.hub_catalog_builder"):
        builder.build_capability_pool(load_config(), "text-generation")
    after = time.time()

    cfg = load_config()
    entry = hub_catalog.pool_entry(cfg, "text-generation")
    assert before <= entry["startedAt"] <= after
    # text-generation resolves to multiple pipeline tags (D780 multi-tag
    # capability); each tag is its own (tag, format) fetch, one page apiece.
    assert entry["pages"] == len(ai_tasks.tags_for_capability("text-generation"))
    assert entry["buildSeconds"] >= 0
    assert any("hub-catalog build complete" in r.message for r in caplog.records)
    assert any("page=0" in r.message for r in caplog.records)


def test_multi_page_build_counts_pages_across_tag_format_pairs(monkeypatch):
    calls = []

    def fake_get(url, *a, **k):
        calls.append(url)
        if len(calls) == 1:
            return _resp([_hit("org/page1")],
                         headers={"Link": '<https://hub.test/api/models?cursor=2>; rel="next"'})
        return _resp([_hit("org/page2")])

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)

    builder.build_capability_pool(load_config(), "automatic-speech-recognition")

    cfg = load_config()
    entry = hub_catalog.pool_entry(cfg, "automatic-speech-recognition")
    assert entry["pages"] == 2


def test_delta_refresh_also_records_instrumentation(monkeypatch):
    cfg = load_config()
    hub_catalog.write_pool(cfg, "text-generation", [
        {"capability": "text-generation", "format": "",
         "raw": _hit("org/old", lastModified="2026-01-01T00:00:00.000Z")},
    ])
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp(
        [_hit("org/new", lastModified="2026-01-05T00:00:00.000Z")]))

    monkeypatch.setattr(builder.ai_tasks, "tags_for_capability",
                         lambda cap: ("text-generation",))
    builder.refresh_capability_pool_delta(cfg, "text-generation")

    entry = hub_catalog.pool_entry(cfg, "text-generation")
    assert entry["pages"] == 1
    assert "buildSeconds" in entry
    assert "startedAt" in entry


# -- build_status (spec item 2) ----------------------------------------------


def test_build_status_none_when_nothing_has_ever_happened():
    cfg = load_config()
    status = builder.build_status("text-generation", cfg=cfg)
    assert status == {"state": "none", "pagesDone": None, "startedAt": None,
                       "blockedUntil": None}


def test_build_status_blocked_reports_deadline():
    cfg = load_config()
    until = time.time() + 500
    hub_catalog.set_blocked_until(cfg, "text-generation", until)
    status = builder.build_status("text-generation", cfg=cfg)
    assert status["state"] == "blocked"
    assert status["blockedUntil"] == until


def test_build_status_building_reports_live_pages_done(monkeypatch):
    import threading

    cfg = load_config()
    gate = threading.Event()
    released = threading.Event()

    def fake_page(url, headers):
        gate.set()
        released.wait(timeout=5)
        return [], httpx.Response(200, content=b"[]",
                                   request=httpx.Request("GET", url)), None

    monkeypatch.setattr(builder, "_page", fake_page)
    monkeypatch.setattr(builder, "for_capability", lambda cap: None)
    monkeypatch.setattr(builder.ai_tasks, "tags_for_capability",
                         lambda cap: ("text-generation",))

    thread = threading.Thread(
        target=lambda: builder.build_capability_pool(cfg, "text-generation"))
    thread.start()
    try:
        assert gate.wait(timeout=5)
        status = builder.build_status("text-generation", cfg=cfg)
        assert status["state"] == "building"
        assert status["startedAt"] is not None
    finally:
        released.set()
        thread.join(timeout=5)

    status_after = builder.build_status("text-generation", cfg=cfg)
    assert status_after["state"] == "none"

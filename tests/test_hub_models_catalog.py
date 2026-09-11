"""`/api/ai-models/hub/search`'s catalog path (SPEC docs/HUB_CATALOG_SPEC.md
item 3): once a capability's on-device pool is BUILT, a search runs entirely
over `hub_catalog.query_pool` — no Hub request, no `_MAX_FETCH`/`_OVERFETCH`
window, ranking and facets over the WHOLE pool. This is the regression test
for the "missing-publisher facet" bug the spec names: the old live path only
ever saw a 200-row overfetched window, so a publisher with all its models
past that window never appeared in the facet menu at all.

Complements `tests/test_hub_models.py` (the live path, untouched) and
`tests/test_ai_hub_catalog.py`/`tests/test_ai_hub_catalog_builder.py` (the
store/builder units). No network monkeypatching of `httpx.get` is set up at
all here for the pool test — the assertion IS that nothing calls it.
"""
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from fused_render.ai import hub_catalog
from fused_render.ai import registry
from fused_render.server import create_app
from fused_render.server.routers import hub_models as hub


def _search(client, body=None):
    return client.post("/api/ai-models/hub/search", json=body or {},
                       headers={"X-Fused": "1"})


@pytest.fixture(autouse=True)
def _clear_cache():
    hub._cache.clear()
    yield
    hub._cache.clear()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _no_format_filter(monkeypatch):
    """Same pin as `test_hub_models.py`'s fixture of the same name — a plain
    runner that declares no format tag and no secondary GGUF-capable runner,
    so every fixture row here survives `_model_row`'s format-drop branch
    regardless of which machine runs the suite."""
    runner = types.SimpleNamespace(hub_filter_tags=(), code="stand-in")
    monkeypatch.setattr(hub, "for_capability", lambda capability: runner)
    monkeypatch.setattr(hub, "available_runners", lambda capability: ())


@pytest.fixture()
def hub_cache(tmp_path, monkeypatch):
    cache = tmp_path / "hub"
    cache.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    return cache


@pytest.fixture()
def client():
    return TestClient(create_app(start_dir="/"))


def _pool_row(repo_id, downloads, likes=1, last_modified="2026-01-01T00:00:00.000Z"):
    return {
        "capability": registry.TEXT_GENERATION,
        "format": "",
        "raw": {
            "id": repo_id,
            "pipeline_tag": "text-generation",
            "downloads": downloads,
            "likes": likes,
            "lastModified": last_modified,
            "createdAt": "2025-01-01T00:00:00.000Z",
            "library_name": "transformers",
            "gated": False,
            "private": False,
            "tags": ["text-generation"],
        },
    }


def _build_big_pool(n_publishers=25, per_publisher=9):
    """A pool with `n_publishers * per_publisher` rows — comfortably past the
    old 200-row overfetch window this replaces — spread across many
    publishers so every one of them has to survive into the facet list for
    the missing-publisher-facet regression to be caught. Downloads are
    assigned so the LAST publisher created has the most-downloaded row,
    proving ranking runs over the whole pool rather than a truncated head."""
    cfg = hub_catalog.load_config()
    rows = []
    total = n_publishers * per_publisher
    counter = 0
    for p in range(n_publishers):
        for i in range(per_publisher):
            counter += 1
            # Reverse the download ranking relative to insertion order: the
            # very LAST row built gets the highest download count, so a
            # correct whole-pool "best" sort must reach past the first
            # (n_publishers - 1) publishers to rank it first.
            downloads = counter
            rows.append(_pool_row(f"pub{p}/model-{i}", downloads=downloads))
    assert len(rows) == total
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION, rows)
    return cfg, rows


def test_catalog_path_makes_zero_hub_requests(client, hub_cache, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(
        AssertionError("catalog path must not call the Hub")))
    _build_big_pool(n_publishers=25, per_publisher=9)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "downloads", "limit": 24})
    assert resp.status_code == 200
    assert calls == []
    body = resp.json()
    assert len(body["models"]) == 24
    # Whole-pool ranking: the single highest-downloads row (built last, see
    # `_build_big_pool`) must win a plain downloads sort.
    assert body["models"][0]["id"] == "pub24/model-8"


def test_catalog_path_facets_and_best_sort_cover_the_whole_pool(client, hub_cache, monkeypatch):
    """The regression test for the documented missing-publisher facet bug:
    every one of 25 publishers must appear in the facet list, and the
    highest-ranked model under `sort=best` must be the actual best row in
    the pool, not merely the best among an early truncated window."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("catalog path must not call the Hub")))
    _build_big_pool(n_publishers=25, per_publisher=9)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "best", "limit": 24})
    assert resp.status_code == 200
    body = resp.json()

    # `_pin_publisher_facets` always pins this machine's go-to publishers to
    # the front of the list regardless of what the pool holds (unrelated to
    # this bug), so this checks the pool's own publishers are ALL present
    # rather than asserting an exact set.
    publisher_ids = {p["id"] for p in body["facets"]["publishers"]}
    assert {f"pub{p}" for p in range(25)} <= publisher_ids

    # matchScore blends downloads among other axes (D780) — the last-built
    # row has both the highest downloads AND the most recent-looking id, so
    # under a whole-pool "best" sort it must lead the page. Under the OLD
    # 200-row-window live path this row would simply never have been fetched
    # at all in a pool this size.
    assert body["models"][0]["id"] == "pub24/model-8"


def test_catalog_ilike_escape_neutralizes_percent_and_underscore_wildcards():
    """Finding: `_catalog_ilike_escape` only doubled a literal quote, leaving
    `%`/`_` as live LIKE/ILIKE wildcards in caller-controlled search text —
    so a query for the literal substring `llama_3` would also match
    `llama-3`/`llama33` (any char in place of `_`), and a bare `%` would
    match the entire pool. Escaped text run through `ESCAPE '\\'` must match
    ONLY the literal substring."""
    escaped = hub._catalog_ilike_escape("llama_3")
    assert escaped == "llama\\_3"

    escaped_percent = hub._catalog_ilike_escape("100%")
    assert escaped_percent == "100\\%"

    # A literal backslash in the input must itself be doubled first, or it
    # would start escaping the character that follows it.
    escaped_backslash = hub._catalog_ilike_escape("a\\b")
    assert escaped_backslash == "a\\\\b"


def test_catalog_search_underscore_does_not_match_as_a_wildcard(client, hub_cache, monkeypatch):
    """End-to-end: a pool containing both `org/llama_3` (the literal query)
    and `org/llamaX3` (what `_` would ALSO match as an unescaped LIKE
    wildcard) must return only the literal match for a `llama_3` query."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("catalog path must not call the Hub")))
    cfg = hub_catalog.load_config()
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION, [
        _pool_row("org/llama_3", downloads=10),
        _pool_row("org/llamaX3", downloads=20),
    ])

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "q": "llama_3", "limit": 24})
    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()["models"]}
    assert ids == {"org/llama_3"}


def test_live_path_still_used_and_fires_hub_request_when_no_pool_exists(client, hub_cache, monkeypatch):
    """No pool built for this capability (fresh, empty catalog dir from
    `_isolated_home`) — the search must take the live Hub path exactly as
    before, i.e. `httpx.get` fires with today's params."""
    import json as _json

    def fake(url, **kwargs):
        fake.calls.append((url, kwargs))
        return httpx.Response(200, content=_json.dumps([
            {"id": "org/live-model", "pipeline_tag": "text-generation", "downloads": 5},
        ]).encode(), request=httpx.Request("GET", url))
    fake.calls = []
    monkeypatch.setattr(httpx, "get", fake)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "downloads", "limit": 24})
    assert resp.status_code == 200
    # TEXT_GENERATION resolves to more than one Hub tag (tasks.py), so the
    # live multi-tag path makes one request per tag, merged — unchanged by
    # this feature; the pool-exists branch is simply never taken here.
    assert len(fake.calls) == len(hub.ai_tasks.tags_for_capability(registry.TEXT_GENERATION))
    assert any("text-generation" in url for url, _kwargs in fake.calls)
    body = resp.json()
    assert [m["id"] for m in body["models"]] == ["org/live-model"]

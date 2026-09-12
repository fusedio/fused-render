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


def _pool_row(repo_id, downloads, likes=1, last_modified="2026-01-01T00:00:00.000Z", safetensors=None):
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
            "safetensors": safetensors,
        },
    }


def _pin_hardware(monkeypatch, *, ram_gb=32.0):
    """Same pin `test_hub_models.py`'s helper of the same name applies to the
    live path — a row's fit verdict must be a property of the FIXTURE, not of
    whatever box happens to run the suite (a CI runner's smaller RAM would
    silently flip "tight" to "no" and drop the row before the assertion)."""
    from fused_render.ai import hw_detect
    monkeypatch.setattr(hub.fit, "machine_ram_gb", lambda: ram_gb)
    monkeypatch.setattr(hub.fit, "_wired_limit_mb", lambda: None)
    monkeypatch.setattr(hub.hw_detect, "cached_hardware", lambda: hw_detect.HardwareInfo(
        gpus=[], total_vram_gb=0.0, bandwidth_gb_s=None, detected_at=0.0))


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


def test_best_sort_ranks_fit_tier_before_composite_score(client, hub_cache, monkeypatch):
    """D1268 (round-6 item 3): the legend says "memory fit first" but the
    D780 composite alone weights fit at only 0.35 — enough that a tight-fit
    row can outscore an easy-fit row on the OTHER four axes and rank between
    two easy rows. Pin `matchScore` directly (90 for the tight row, 80 for
    the easy one) so the fit VERDICT, not the score, is what decides the
    order: under the old score-only sort the tight row (90) would lead; the
    tier-first fix must put the easy row (80) first regardless."""
    _pin_hardware(monkeypatch, ram_gb=32.0)
    # 32GB * COMFORT(0.70) ~= 22.4GB budget. 5GB is comfortably under it
    # (easy); 20GB is past COMFORT but still within budget (tight).
    easy_bytes = int(5e9 / 2) * 2  # 5GB at BF16 (2 bytes/param)
    tight_bytes = int(20e9 / 2) * 2  # 20GB at BF16
    hub_catalog.write_pool(hub_catalog.load_config(), registry.TEXT_GENERATION, [
        _pool_row("org/easy-fit", downloads=10,
                  safetensors={"parameters": {"BF16": easy_bytes // 2}, "total": easy_bytes // 2}),
        _pool_row("org/tight-fit", downloads=10_000,
                  safetensors={"parameters": {"BF16": tight_bytes // 2}, "total": tight_bytes // 2}),
    ])

    def _fake_raw_score(row, ram_gb):
        return 90.0 if row.get("id") == "org/tight-fit" else 80.0

    monkeypatch.setattr(hub, "_composite_raw_score", _fake_raw_score)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "best", "limit": 24})
    assert resp.status_code == 200
    body = resp.json()

    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["org/easy-fit"]["fit"]["verdict"] == "easy"
    assert by_id["org/tight-fit"]["fit"]["verdict"] == "tight"
    assert by_id["org/easy-fit"]["matchScore"] == 80.0
    assert by_id["org/tight-fit"]["matchScore"] == 90.0

    ids = [m["id"] for m in body["models"]]
    assert ids.index("org/easy-fit") < ids.index("org/tight-fit")


def test_best_sort_pull_in_keeps_unclamped_raw_order_when_scores_clamp_equal(
        client, hub_cache, monkeypatch):
    """Code review finding: the family pull-in re-sort keyed on the CLAMPED
    `matchScore` while the initial sort keys on UNCLAMPED `raw_scores` — two
    rows that both clamp to 100 but differ before clamping could swap order
    once `_pull_in_family_members` runs its own re-sort. `org/variant`
    declares `org/base` as its base model via the Hub's own
    `base_model:finetune:<id>` tag, so with `limit=1` the initial (correct)
    sort keeps only `org/variant` and the pull-in reinserts `org/base`
    BEFORE it (case (c) in `_pull_in_family_members`'s own docstring) — the
    pre-resort order is therefore [org/base, org/variant], the WRONG way
    round. A matchScore-keyed re-sort sees two equal 100.0 values and (being
    stable) leaves that wrong order untouched; a raw_scores-keyed re-sort
    correctly restores org/variant (raw 250) ahead of org/base (raw 100)."""
    hub_catalog.write_pool(hub_catalog.load_config(), registry.TEXT_GENERATION, [
        {
            "capability": registry.TEXT_GENERATION,
            "format": "",
            "raw": {
                "id": "org/base",
                "pipeline_tag": "text-generation",
                "downloads": 1,
                "likes": 1,
                "lastModified": "2026-01-01T00:00:00.000Z",
                "createdAt": "2025-01-01T00:00:00.000Z",
                "library_name": "transformers",
                "gated": False,
                "private": False,
                "tags": ["text-generation"],
                "safetensors": None,
            },
        },
        {
            "capability": registry.TEXT_GENERATION,
            "format": "",
            "raw": {
                "id": "org/variant",
                "pipeline_tag": "text-generation",
                "downloads": 1,
                "likes": 1,
                "lastModified": "2026-01-01T00:00:00.000Z",
                "createdAt": "2025-01-01T00:00:00.000Z",
                "library_name": "transformers",
                "gated": False,
                "private": False,
                "tags": ["text-generation", "base_model:finetune:org/base"],
                "safetensors": None,
            },
        },
    ])

    def _fake_raw_score(row, ram_gb):
        # Both clamp to matchScore 100.0 (min(100.0, max(0.0, raw))) despite
        # differing wildly before clamping.
        return 250.0 if row.get("id") == "org/variant" else 100.0

    monkeypatch.setattr(hub, "_composite_raw_score", _fake_raw_score)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "best", "limit": 1})
    assert resp.status_code == 200
    body = resp.json()

    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["org/variant"]["matchScore"] == 100.0
    assert by_id["org/base"]["matchScore"] == 100.0

    ids = [m["id"] for m in body["models"]]
    assert ids.index("org/variant") < ids.index("org/base")


def test_catalog_path_publisher_facet_does_not_collapse_on_filter(client, hub_cache, monkeypatch):
    """C2: the publisher facet must be computed over the slice WITHOUT the
    publisher filter (matching D853's live-path behaviour) or picking one
    publisher would collapse the facet menu down to just that publisher,
    with no way back to "any" without reloading."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("catalog path must not call the Hub")))
    _build_big_pool(n_publishers=5, per_publisher=3)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "downloads",
                            "limit": 24, "publisher": "pub0"})
    assert resp.status_code == 200
    body = resp.json()
    # Every row returned is scoped to the chosen publisher...
    assert all(m["id"].startswith("pub0/") for m in body["models"])
    # ...but the facet list must still show every publisher in the pool.
    publisher_ids = {p["id"] for p in body["facets"]["publishers"]}
    assert {f"pub{p}" for p in range(5)} <= publisher_ids


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
    assert body["poolState"] == "none"
    assert "poolPagesDone" not in body


def test_catalog_path_reports_pool_state_ready(client, monkeypatch):
    cfg = hub_catalog.load_config()
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION, [
        {"capability": registry.TEXT_GENERATION, "format": "",
         "raw": {"id": "org/pooled", "pipeline_tag": "text-generation", "downloads": 5}},
    ])

    def _boom(*a, **k):
        raise AssertionError("catalog path must not call the Hub")
    monkeypatch.setattr(httpx, "get", _boom)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "downloads", "limit": 24})
    assert resp.status_code == 200
    assert resp.json()["poolState"] == "ready"


def test_live_path_reports_pool_state_building(client, monkeypatch):
    from fused_render.ai import hub_catalog_builder as real_builder
    from fused_render.server.routers import hub_models as hub_models_mod

    monkeypatch.setattr(hub_models_mod, "hub_catalog_builder", types.SimpleNamespace(
        ensure_build_started=lambda *a, **k: True,
        build_status=lambda *a, **k: {"state": "building", "pagesDone": 4,
                                       "startedAt": 1.0, "blockedUntil": None},
    ))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        200, content=b"[]", request=httpx.Request("GET", "https://hub.test")))

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "downloads", "limit": 24})
    assert resp.status_code == 200
    body = resp.json()
    assert body["poolState"] == "building"
    assert body["poolPagesDone"] == 4


def test_live_path_reports_pool_state_blocked(client, monkeypatch):
    from fused_render.server.routers import hub_models as hub_models_mod

    monkeypatch.setattr(hub_models_mod, "hub_catalog_builder", types.SimpleNamespace(
        ensure_build_started=lambda *a, **k: False,
        build_status=lambda *a, **k: {"state": "blocked", "pagesDone": None,
                                       "startedAt": None, "blockedUntil": 123.0},
    ))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        200, content=b"[]", request=httpx.Request("GET", "https://hub.test")))

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "downloads", "limit": 24})
    assert resp.status_code == 200
    body = resp.json()
    assert body["poolState"] == "blocked"
    assert "poolPagesDone" not in body


def test_catalog_path_starts_a_stale_rebuild_but_still_serves_the_existing_pool(
        client, hub_cache, monkeypatch):
    """C3 follow-up (D1260): `ensure_build_started` is only a no-op when the
    pool is fresh (`_formats_are_stale` False) — but the catalog gate used to
    return `_catalog_search` before ever calling it, so a pool built with a
    narrower format set than the machine can now serve (a second runner
    installed since) never triggered the wider rebuild. This pins that the
    catalog path now calls `ensure_build_started` (which itself decides
    whether a rebuild is actually warranted) BEFORE serving, and that the
    stale pool still answers this request rather than blocking on the
    rebuild."""
    cfg = hub_catalog.load_config()
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION,
                            [_pool_row("only/gguf-model", downloads=5)],
                            formats=("gguf",))

    runner = types.SimpleNamespace(hub_filter_tags=("gguf",), code="llamacpp-text")
    runner2 = types.SimpleNamespace(hub_filter_tags=("mlx",), code="mlx-text")
    monkeypatch.setattr(hub, "available_runners", lambda capability: (runner, runner2))

    started = []
    monkeypatch.setattr(hub.hub_catalog_builder, "ensure_build_started",
                         lambda capability, **k: started.append(capability) or True)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "downloads", "limit": 24})
    assert resp.status_code == 200
    assert started == [registry.TEXT_GENERATION]
    body = resp.json()
    assert [m["id"] for m in body["models"]] == ["only/gguf-model"]


# -- item 2 (scope-corrected): the flag survives the catalog path too -------


def test_catalog_path_flags_an_unloadable_row_instead_of_dropping_it(client, hub_cache, monkeypatch):
    cfg = hub_catalog.load_config()
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION, [
        _pool_row("org/llama-repo", downloads=5),
        _pool_row("org/neo-repo", downloads=10),
    ])
    # `_pool_row` does not carry `config` — patch it in directly, the same
    # `config.model_type` shape `_EXPAND` already returns live.
    rows = hub_catalog.query_pool(cfg, registry.TEXT_GENERATION)
    assert len(rows) == 2

    runner = types.SimpleNamespace(hub_filter_tags=(), code="mlx-text")
    monkeypatch.setattr(hub, "for_capability", lambda capability: runner)
    monkeypatch.setattr(hub.hub_loadable, "loadable_kind",
                         lambda code: ("model_types", frozenset({"llama"})))

    def _model_type_for(model_id):
        return {"org/llama-repo": "llama", "org/neo-repo": "neo_chat"}[model_id]

    real_model_row = hub._model_row

    def patched_model_row(raw, *a, **k):
        raw = dict(raw)
        raw["config"] = {"model_type": _model_type_for(raw["id"])}
        return real_model_row(raw, *a, **k)

    monkeypatch.setattr(hub, "_model_row", patched_model_row)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "downloads", "limit": 24})
    assert resp.status_code == 200
    by_id = {m["id"]: m for m in resp.json()["models"]}
    assert set(by_id) == {"org/llama-repo", "org/neo-repo"}
    assert by_id["org/llama-repo"]["loadable"] is True
    assert by_id["org/neo-repo"]["loadable"] is False
    assert by_id["org/neo-repo"]["loadableReason"] == "neo_chat not supported by mlx-vlm"
    # Order is untouched by the flag — same downloads-desc order as if
    # neither row had been judged at all.
    assert [m["id"] for m in resp.json()["models"]] == ["org/neo-repo", "org/llama-repo"]


def test_catalog_path_flags_a_cached_on_disk_row_too_no_exemption(
        client, hub_cache, monkeypatch, tmp_path):
    """The scope correction's explicit callout: a repo already on this
    disk still shows the chip if the active runner cannot load it — there
    is no on-disk exemption the way the ORIGINAL (drop-based) brief had
    one, because nothing here drops rows to need exempting from in the
    first place."""
    cache = tmp_path / "hf-cache"
    cache.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    dirname = "models--org--neo-repo"
    blob = cache / dirname / "blobs" / "b1"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x" * 64)
    snapshot = cache / dirname / "snapshots" / "c1"
    snapshot.mkdir(parents=True)
    try:
        (snapshot / "model.safetensors").symlink_to(blob)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    (cache / dirname / "refs").mkdir()
    (cache / dirname / "refs" / "main").write_text("c1")

    cfg = hub_catalog.load_config()
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION,
                            [_pool_row("org/neo-repo", downloads=5)])

    runner = types.SimpleNamespace(hub_filter_tags=(), code="mlx-text")
    monkeypatch.setattr(hub, "for_capability", lambda capability: runner)
    monkeypatch.setattr(hub.hub_loadable, "loadable_kind",
                         lambda code: ("model_types", frozenset({"llama"})))

    real_model_row = hub._model_row

    def patched_model_row(raw, *a, **k):
        raw = dict(raw)
        raw["config"] = {"model_type": "neo_chat"}
        return real_model_row(raw, *a, **k)

    monkeypatch.setattr(hub, "_model_row", patched_model_row)

    resp = _search(client, {"capability": registry.TEXT_GENERATION, "sort": "downloads", "limit": 24})
    row = resp.json()["models"][0]
    assert row["local"]["state"] == "downloaded"
    assert row["loadable"] is False
    assert row["loadableReason"] == "neo_chat not supported by mlx-vlm"

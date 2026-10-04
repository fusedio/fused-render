"""Tests for the daily hub-catalog delta-refresh wiring (SPEC
docs/HUB_CATALOG_SPEC.md item 4), modeled directly on
`tests/test_ai_supervisor_hub_metadata_refresh.py`.

`supervisor.start_hub_catalog_refresh` mirrors `start_hardware_refresh`/
`start_hub_metadata_refresh`: a background daemon thread that sweeps every
BUILT capability pool's manifest entry once a day, calling
`hub_catalog_builder.refresh_capability_pool_delta` for whichever ones are
due (never built, blocked, or refreshed within the last day are skipped).
"""
import pytest

from fused_render.ai import hub_catalog, hub_catalog_builder, registry, supervisor

# Captured at COLLECTION time, before any test's autouse
# `_no_ai_hub_catalog_refresh_thread` fixture (tests/conftest.py) monkeypatches
# `supervisor.start_hub_catalog_refresh` to a no-op for the rest of the
# suite — see that fixture's own docstring, and its two siblings, for why.
_real_start_hub_catalog_refresh = supervisor.start_hub_catalog_refresh


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _pool_row(repo_id, last_modified="2026-01-01T00:00:00.000Z"):
    return {
        "capability": registry.TEXT_GENERATION,
        "format": "",
        "raw": {"id": repo_id, "lastModified": last_modified},
    }


def test_a_tick_refreshes_a_built_pool_that_is_due(monkeypatch):
    cfg = hub_catalog.load_config()
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION, [_pool_row("org/model-a")])
    # Force the manifest entry's "updated" stamp far enough into the past to
    # be due, without waiting a real day.
    manifest = hub_catalog.read_manifest(cfg)
    manifest["capabilities"][registry.TEXT_GENERATION]["updated"] = 0.0
    hub_catalog._write_manifest(cfg, manifest)

    calls = []
    monkeypatch.setattr(hub_catalog_builder, "refresh_capability_pool_delta",
                        lambda cfg, capability: calls.append(capability) or {"rows": 1})

    supervisor._hub_catalog_refresh_tick()
    assert calls == [registry.TEXT_GENERATION]


def test_a_tick_skips_a_pool_refreshed_recently(monkeypatch):
    cfg = hub_catalog.load_config()
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION, [_pool_row("org/model-a")])
    # write_pool just stamped "updated" to now — nowhere near a day old.

    calls = []
    monkeypatch.setattr(hub_catalog_builder, "refresh_capability_pool_delta",
                        lambda cfg, capability: calls.append(capability))

    supervisor._hub_catalog_refresh_tick()
    assert calls == []


def test_a_tick_skips_a_capability_with_no_pool_built(monkeypatch):
    calls = []
    monkeypatch.setattr(hub_catalog_builder, "refresh_capability_pool_delta",
                        lambda cfg, capability: calls.append(capability))

    supervisor._hub_catalog_refresh_tick()
    assert calls == []


def test_a_tick_skips_a_blocked_pool_even_if_due(monkeypatch):
    cfg = hub_catalog.load_config()
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION, [_pool_row("org/model-a")])
    manifest = hub_catalog.read_manifest(cfg)
    manifest["capabilities"][registry.TEXT_GENERATION]["updated"] = 0.0
    hub_catalog._write_manifest(cfg, manifest)
    hub_catalog.set_blocked_until(cfg, registry.TEXT_GENERATION, 1e18)  # far future

    calls = []
    monkeypatch.setattr(hub_catalog_builder, "refresh_capability_pool_delta",
                        lambda cfg, capability: calls.append(capability))

    supervisor._hub_catalog_refresh_tick()
    assert calls == []


def test_a_tick_survives_one_capabilitys_refresh_raising(monkeypatch):
    """Two built, due, unblocked pools — one raises, the sweep must still
    reach the other, matching `_hub_metadata_refresh_tick`'s per-repo
    try/except pattern."""
    cfg = hub_catalog.load_config()
    hub_catalog.write_pool(cfg, registry.TEXT_GENERATION, [_pool_row("org/model-a")])
    hub_catalog.write_pool(cfg, registry.IMAGE_GENERATION, [_pool_row("org/model-b")])
    manifest = hub_catalog.read_manifest(cfg)
    manifest["capabilities"][registry.TEXT_GENERATION]["updated"] = 0.0
    manifest["capabilities"][registry.IMAGE_GENERATION]["updated"] = 0.0
    hub_catalog._write_manifest(cfg, manifest)

    calls = []

    def flaky(cfg, capability):
        calls.append(capability)
        if capability == registry.TEXT_GENERATION:
            raise RuntimeError("boom")

    monkeypatch.setattr(hub_catalog_builder, "refresh_capability_pool_delta", flaky)

    supervisor._hub_catalog_refresh_tick()  # must not raise
    assert set(calls) == {registry.TEXT_GENERATION, registry.IMAGE_GENERATION}


def test_start_hub_catalog_refresh_is_idempotent(monkeypatch):
    started = []

    class _FakeThread:
        def __init__(self, target, name, daemon):
            self.target = target
            self.name = name
            self._alive = True
            started.append(self)

        def start(self):
            pass

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(supervisor.threading, "Thread", _FakeThread)
    monkeypatch.setattr(supervisor, "_hub_catalog_refresh_thread", None)

    _real_start_hub_catalog_refresh()
    _real_start_hub_catalog_refresh()

    assert len(started) == 1
    assert started[0].name == "ai-hub-catalog-refresh"

    monkeypatch.setattr(supervisor, "_hub_catalog_refresh_thread", None)


def test_start_hub_catalog_refresh_starts_a_new_thread_once_the_old_one_died(monkeypatch):
    started = []

    class _FakeThread:
        def __init__(self, target, name, daemon):
            started.append(self)
            self._alive = True

        def start(self):
            pass

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(supervisor.threading, "Thread", _FakeThread)
    monkeypatch.setattr(supervisor, "_hub_catalog_refresh_thread", None)

    _real_start_hub_catalog_refresh()
    started[0]._alive = False
    _real_start_hub_catalog_refresh()

    assert len(started) == 2

    monkeypatch.setattr(supervisor, "_hub_catalog_refresh_thread", None)

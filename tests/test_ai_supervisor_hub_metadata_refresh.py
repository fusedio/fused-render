"""Tests for the background Hub-metadata-warming wiring (code review finding
1, on top of SPEC AI-17).

`ai_runtime._accepts_image`/`_capability_tags` used to call `hub_metadata.
get(model_id)` directly from `describe_catalog` — a synchronous `urllib` GET
with an 8-second timeout, on a route the AI Models picker polls. On a
Linux/Windows box the `llamacpp-text` list is several curated GGUF repos,
and a GGUF repo routinely has no `config.json` at all, so one catalog
request could reissue several timeout-bound fetches; offline or behind a
captive portal that stalls the page for tens of seconds. `supervisor.
start_hub_metadata_refresh` is the fix, mirroring `start_hardware_refresh`'s
shape exactly (`tests/test_ai_supervisor_hardware_refresh.py`) — a
background daemon thread, wired from `server/app.py`'s startup event, that
is now the ONLY caller of `hub_metadata.get` outside this module's own
tests; the request path reads `hub_metadata.cached()`, a plain disk read.
"""
import pytest

from fused_render.ai import catalog, hub_metadata, supervisor

# Captured at COLLECTION time, before any test's autouse
# `_no_ai_hub_metadata_refresh_thread` fixture (tests/conftest.py)
# monkeypatches `supervisor.start_hub_metadata_refresh` to a no-op for the
# rest of the suite — see that fixture's own docstring, and its two
# siblings, for why.
_real_start_hub_metadata_refresh = supervisor.start_hub_metadata_refresh


def test_a_tick_sweeps_every_curated_id_through_hub_metadata_get(monkeypatch):
    calls = []
    monkeypatch.setattr(hub_metadata, "get", lambda repo_id, **kw: calls.append(repo_id))
    supervisor._hub_metadata_refresh_tick()
    assert set(calls) == catalog.all_suggested_ids()
    assert calls  # the curated list is never empty


def test_a_tick_survives_one_repos_get_call_raising(monkeypatch):
    """`hub_metadata.get` already degrades network failures to `None`
    internally — this is the belt-and-suspenders case where it raises
    anyway (a bug in a future edit, a monkeypatched test double): one bad
    repo must not stop the sweep for the rest of the curated list."""
    ids = list(catalog.all_suggested_ids())
    calls = []

    def flaky(repo_id, **kw):
        calls.append(repo_id)
        if repo_id == ids[0]:
            raise RuntimeError("boom")

    monkeypatch.setattr(hub_metadata, "get", flaky)
    supervisor._hub_metadata_refresh_tick()  # must not raise
    assert set(calls) == set(ids)


def test_start_hub_metadata_refresh_is_idempotent(monkeypatch):
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
    monkeypatch.setattr(supervisor, "_hub_metadata_refresh_thread", None)

    _real_start_hub_metadata_refresh()
    _real_start_hub_metadata_refresh()

    assert len(started) == 1
    assert started[0].name == "ai-hub-metadata-refresh"

    monkeypatch.setattr(supervisor, "_hub_metadata_refresh_thread", None)


def test_start_hub_metadata_refresh_starts_a_new_thread_once_the_old_one_died(monkeypatch):
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
    monkeypatch.setattr(supervisor, "_hub_metadata_refresh_thread", None)

    _real_start_hub_metadata_refresh()
    started[0]._alive = False
    _real_start_hub_metadata_refresh()

    assert len(started) == 2

    monkeypatch.setattr(supervisor, "_hub_metadata_refresh_thread", None)

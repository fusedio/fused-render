"""SPEC-quiet-notifications.md bug 2: `Job.source` arriving by DEFAULT.

Round 1 (image/video) and Round 2 (text generation, both `_local_relay` and
`_apple_relay` in `fused_render/server/ai.py`) both shipped with a producer
that minted a job row and forgot to pass `source=` at all — silently, with no
test catching the omission, because opting in was the mechanism. The real
fix (`jobs.py`'s `_ambient_source` ContextVar, stamped by `server/common.py`'s
`no_cache_and_log` from `X-Fused-Source`, consulted as `jobs.upsert`'s
fallback when a producer's own `source=` resolves empty) makes `source`
arrive without any producer opting in — a producer has to go out of its way
to lose it now, not remember to gain it.

This file covers the two remaining producers image/video/transcribe/capture
tests didn't already (see DECISIONS-quiet-notifications.md): text generation,
both call sites, driven through the REAL `/api/ai` endpoint end-to-end
(TestClient), through the real ASGI middleware — not a direct call to
`_local_relay`/`_apple_relay`, which would bypass the very layer under test —
plus a pair of guard tests against a NEW job-minting producer reintroducing
this bug a third time.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fused_render
from fused_render import jobs
from fused_render.ai import supervisor
from fused_render.server import ai as _server_ai
from fused_render.server import create_app

from tests.test_ai_apple import fake_helper, needs_exec  # noqa: F401 (fixture + marker reuse)


@pytest.fixture()
def client():
    return TestClient(create_app(start_dir="/"))


@pytest.fixture(autouse=True)
def _clean_jobs():
    jobs.reset()
    yield
    jobs.reset()


# -- text generation, site 1: `_local_relay` (a resident local model) ---------


def test_a_local_text_rows_source_comes_from_the_ambient_X_Fused_Source(
        client, monkeypatch):
    """`_local_relay` mints its row via `supervisor.text_row_fields` +
    `supervisor._report`, neither of which is ever given a `source=` — the
    literal Round 2 bug. Sent with no `X-Fused-Page` (the Playground's own
    shape, `page` stays ""), `X-Fused-Source` must still land in the row."""
    monkeypatch.setattr(_server_ai, "_is_local_model", lambda m: True)
    monkeypatch.setattr(
        supervisor, "generate_text",
        lambda model, request: iter([{"type": "done", "ok": True, "tokens": 1}]))
    reply = client.post(
        "/api/ai", json={"prompt": "hi", "model": "org/chat"},
        headers={"X-Fused": "1", "X-Fused-Source": "/ai-models/playground"})
    assert reply.status_code == 200, reply.json()
    row = next(j for j in jobs.list_jobs()
               if j["id"].startswith(supervisor.TEXT_JOB_PREFIX))
    assert row["page"] == ""
    assert row["source"] == "/ai-models/playground"


# -- text generation, site 2: `_apple_relay` (Apple's on-device model) --------


@needs_exec
def test_an_apple_text_rows_source_comes_from_the_ambient_X_Fused_Source(
        client, fake_helper):
    """`_apple_relay` is `_local_relay`'s shape on purpose (its own docstring
    says so) and shares the same gap: no `source=` of its own. Same ambient
    fix, same assertion, over the real Apple-tier route (the stand-in helper
    from test_ai_apple.py, not a live model)."""
    reply = client.post(
        "/api/ai", json={"prompt": "hi", "provider": "apple"},
        headers={"X-Fused": "1", "X-Fused-Source": "/ai-models/playground"})
    assert reply.status_code == 200, reply.json()
    row = next(j for j in jobs.list_jobs()
               if j["id"].startswith(supervisor.TEXT_JOB_PREFIX))
    assert row["page"] == ""
    assert row["source"] == "/ai-models/playground"


# -- guard tests: a new producer cannot reintroduce this silently ------------


def test_every_job_minting_prefix_is_accounted_for():
    """Guard against a NEW kind of job-minting producer showing up with no
    source coverage at all — the literal way this bug reached production
    twice. If this fails because a new `*_JOB_PREFIX` constant was added
    somewhere in `fused_render/`, it means a new producer exists: add a
    producer-level test proving ITS row's `source` comes from
    `X-Fused-Source` (ambient default, or an explicit override) the way
    every prefix below already has one (`test_ai_runtime.py`'s image/video/
    transcribe tests, `test_capture.py`, this file) — then add the new
    prefix to `expected` below. Don't add to `expected` without adding that
    test first."""
    root = Path(fused_render.__file__).parent
    found = set()
    for path in root.rglob("*.py"):
        if "static" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"^(\w*JOB_PREFIX)\s*=", text, re.M):
            found.add(m.group(1))
    expected = {
        "SCHEDULE_JOB_PREFIX",   # jobs.py / schedule.py's own _JOB_PREFIX below
        "_JOB_PREFIX",           # schedule.py (background work, no request — see jobs.py)
        "INDEX_JOB_PREFIX",      # server/routers/index.py
        "IMAGE_JOB_PREFIX",      # ai/supervisor.py — /api/ai/image
        "TRANSCRIBE_JOB_PREFIX",  # ai/supervisor.py — /api/ai/transcribe
        "VIDEO_JOB_PREFIX",      # ai/supervisor.py — /api/ai/video
        "TEXT_JOB_PREFIX",       # ai/supervisor.py — /api/ai (local + apple)
        "BENCHMARK_JOB_PREFIX",  # ai/supervisor.py
        "JOB_PREFIX",            # capture/__init__.py — /api/capture/start
    }
    missing = found - expected
    stale = expected - found
    assert not missing, f"new job-minting prefix(es) with no source coverage yet: {missing}"
    assert not stale, f"expected prefix(es) no longer found (update this test): {stale}"


def test_job_source_is_only_ever_written_by_jobs_upsert():
    """The other half of the same guard: a producer could still bypass the
    ambient fallback entirely by mutating `Job.source` directly instead of
    going through `jobs.upsert` (the one place `_ambient_source` is
    consulted). If this fails, a new write site was added outside
    `jobs.py` — route it through `jobs.upsert` instead so both the ambient
    default and the explicit-wins-over-ambient rule apply to it too."""
    root = Path(fused_render.__file__).parent
    outside = []
    for path in root.rglob("*.py"):
        if "static" in path.parts or path.name == "jobs.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"\.sou" + r"rce\s*=[^=]", text):
            line = text[:m.start()].count("\n") + 1
            outside.append(f"{path}:{line}")
    assert not outside, f"Job.source written outside jobs.py: {outside}"

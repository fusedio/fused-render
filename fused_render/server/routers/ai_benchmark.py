"""The AI Models page's Benchmark tab, server side (SPEC AI-14).

Three routes and nothing else:

* `POST /api/ai/benchmark` — run one benchmark and answer with its record. The
  request is **held open for the whole run**, which is minutes, exactly as
  `POST /api/ai/image` and `POST /api/ai/transcribe` already are for work of the
  same length. Inventing a poll-a-benchmark-job protocol for a third long call
  would be new machinery with no new capability. **No job id comes back and no
  download-manager row is created** — see `benchmark.run` for why a benchmark
  cannot own a row for a model that already has a load row; the tab shows its own
  spinner for the duration.
* `GET /api/ai/benchmark` — every stored run, plus THIS machine. The machine
  block travels with the history rather than only on each run because the page
  has to caption a comparison ("these numbers are from this laptop") before it
  has drawn a single run.
* `POST /api/ai/benchmark/delete` — drop named runs, answering with the fresh
  history. Same shape as `POST /api/ai-models/delete`: a validated body, and a
  reply the page can swap in wholesale rather than patching rows it hopes are
  still true.

Everything a benchmark DOES is `fused_render.ai.benchmark` and
`fused_render.ai.bench_store`. What stays here is what is genuinely HTTP: the
`X-Fused` guard on the two mutating POSTs (D3), the body shapes, and — the part
that is not boilerplate — **three refusals that have to happen before the
supervisor is touched at all**:

* an **unknown capability**, which has no fixed workload and therefore nothing
  comparable to measure;
* a **model this machine does not hold**, because `supervisor.load()` would
  otherwise turn a button press into a silent multi-GB download and the page
  would sit on a spinner for an hour;
* a **second concurrent run on the same capability**. The supervisor holds one
  resident model per capability, so the second run's load EVICTS the first's
  model mid-measurement — the same hazard `generate_transcript`'s ordering
  comment documents — and the first run's number is then a measurement of two
  models fighting. Refused with a sentence rather than allowed to silently
  corrupt a figure somebody waited minutes for.

The guard is per CAPABILITY, not global: two capabilities have two independent
resident slots, and serializing across them would refuse a legitimate
back-to-back pair for no reason.
"""
from __future__ import annotations

import contextlib
import threading

from fastapi import APIRouter, Body, Header

from fused_render.ai import bench_store, benchmark, catalog
from fused_render.ai.hub_cache import cached_models, is_downloaded
from fused_render.ai.runners import formats
from fused_render.server.common import _error, _require_fused

router = APIRouter()

#: Capabilities with a benchmark in flight right now, and the lock that makes
#: claiming one atomic. A `set` rather than a lock per capability because the
#: answer the route needs is "is this one busy" and not "wait for it" — a
#: benchmark queued behind another benchmark would hold an HTTP request open for
#: twice the minutes with nothing to show, so the second one is refused instead.
_running: set[str] = set()
_running_lock = threading.Lock()


class _Busy(RuntimeError):
    """`_claim` refusing: this capability already has a run in flight.

    Its own class rather than a bare `RuntimeError`, because the `try` around
    `_claim` now also wraps `benchmark.run`, and a `RuntimeError` from inside a
    measurement must not be answered as "a benchmark is already running".
    """


@contextlib.contextmanager
def _claim(capability: str):
    """Hold `capability` for the duration, or raise `_Busy` if taken.

    A context manager rather than a claim/release pair so the release cannot be
    skipped by an early return or a raising runner: a leaked claim would leave
    that capability's Run button permanently dead until a restart, which is a
    far worse failure than the collision it guards against.
    """
    with _running_lock:
        if capability in _running:
            raise _Busy(capability)
        _running.add(capability)
    try:
        yield
    finally:
        with _running_lock:
            _running.discard(capability)


def _benchmarkable_models(capability: str) -> set[str]:
    """Every model id this machine may benchmark for `capability`.

    **Every id here is one whose bytes are actually on this disk.** The first cut
    unioned the disk with `catalog.for_capability(capability)` and stopped there,
    which defeated the guard entirely: that function is the CURATION (see
    `catalog.py` — "Curated, not fetched"), it has no filesystem awareness at
    all, so every recommended repo id passed and a Run press became a silent
    multi-GB `supervisor.load()` inside a request held open for up to
    `_LOAD_TIMEOUT_S`. The 404 this module's docstring promises never fired for
    exactly the ids most likely to be pressed.

    So the catalog is consulted for the SHAPE of an id, never as evidence that it
    is here. It is needed for one reason only: `llamacpp-text`'s curated ids are
    bare `.gguf` FILENAMES rather than repo ids (AI-5m), so they can never appear
    as a cached `repo_id` and a disk-only check would refuse every curated GGUF.
    `hub_cache.is_downloaded` is what resolves those through the recipe's
    `(repo, file)` pair — asked rather than re-derived, because a third copy of
    that rule is how this bug happened in the first place.

    A capability's curated list is still the filter on WHICH ids the catalog half
    may contribute, so a curated speech model cannot be benchmarked as a text
    one — and the disk half is filtered the same way, on `CachedModel.capability`
    itself, for the identical reason: a repo cached for `automatic-speech-
    recognition` passing this guard for `text-generation` would load Whisper
    into the text runner, fail, and store a permanent history row for a
    combination that was never valid. And a partly downloaded repo is absent
    from `cached_models()` already (D424), so nothing here can resume a
    stopped fetch.
    """
    cached = cached_models()
    admitted = {model.repo_id for model in cached if model.capability == capability}
    for entry in catalog.for_capability(capability):
        entry_id = entry.get("id")
        # Only the filename-shaped ids have anything to add: a curated REPO id is
        # already in `admitted` if it is here, and if it is not, it is not
        # benchmarkable. Guarding on the recipe keeps that explicit rather than
        # resting on `is_downloaded` happening to agree.
        if entry_id in formats.GGUF_RECIPES and is_downloaded(entry_id, cached):
            admitted.add(entry_id)
    return admitted


def _history() -> dict:
    """The payload both reading routes answer with."""
    return {"runs": bench_store.read(), "machine": benchmark.machine()}


@router.get("/api/ai/benchmark")
def api_ai_benchmark_history():
    """Every recorded run, oldest first, plus this machine.

    Sync `def`: it is one small JSON read, so FastAPI runs it in the threadpool
    rather than on the event loop. Unguarded, like every other read in the app.
    """
    return _history()


@router.post("/api/ai/benchmark")
def api_ai_benchmark_run(body: dict = Body(...),
                         x_fused: str | None = Header(default=None)):
    """Run one benchmark. Blocks for minutes; answers with the run record.

    Sync `def` is load-bearing here rather than incidental: FastAPI runs it on
    the threadpool, which is the only reason a multi-minute model load does not
    stall every other request the page is making. `benchmark.run` must never be
    awaited on the loop.

    A run that FAILED is still a 200 with `ok:false` — the run happened and has
    a result, and "this model OOMs on this laptop" is exactly what somebody
    benchmarks to find out. The status code describes the request; `ok`
    describes the model.

    A run STOPPED from outside is a 200 with **no `run` at all** and
    `cancelled: true`. Distinct on the wire rather than reported as a failed run,
    because nothing was measured: the first cut answered with the `ok:false,
    error:"cancelled"` record and the page appended it, drawing a phantom
    "Failed — cancelled" row that became the model's latest until a reload. Also
    not a 4xx — the request was well formed and reached a model, so a status the
    client's `catch` would turn into a request error would be wrong too. The
    client reads the ABSENCE of `run`; it never pattern-matches on an error
    string. It does tell the user, because nobody pressed a ✕ here (there is no
    row to press one on): the likely cause is `fused.ai.cancel()` from another
    page reaching the same shared worker, and a run that just vanished with no
    explanation is worse than one that failed.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        return _error("'model' must be a Hugging Face repo id", status=400)
    model = model.strip()

    capability = body.get("capability")
    # Against `benchmark.WORKLOADS`, not `registry.capabilities()`: a capability
    # the registry knows but no workload covers cannot be measured comparably,
    # and the two sets are pinned equal by tests/test_ai_benchmark_store.py — so
    # asking the table that actually has to answer keeps the refusal honest if
    # they ever drift.
    if not isinstance(capability, str) or capability not in benchmark.WORKLOADS:
        return _error(f"unknown capability {capability!r}", status=400)

    if model not in _benchmarkable_models(capability):
        # 404, not 400: the request is well formed and names a real thing that
        # is simply not here. Download it first — a benchmark that started the
        # download would hold this request open for an hour.
        return _error(
            f"{model} is not on this machine — download it before benchmarking it",
            status=404)

    try:
        with _claim(capability):
            record = benchmark.run(model, capability)
    except _Busy:
        return _error(
            f"a {capability} benchmark is already running — one at a time, or the "
            f"second load would evict the first model mid-measurement",
            status=409)
    except benchmark.Cancelled:
        # Stopped from outside. Nothing was measured, nothing was stored, and
        # nothing comes back for the page to draw — see `benchmark.Cancelled`.
        return {"cancelled": True}
    return {"run": record}


@router.post("/api/ai/benchmark/delete")
def api_ai_benchmark_delete(body: dict = Body(...),
                            x_fused: str | None = Header(default=None)):
    """Delete named runs, then answer with the fresh history.

    Body: `{"ids": ["<run id>", …]}`. Guarded by `X-Fused` (D3) like every
    mutating POST — this one destroys measurements that cost minutes of compute
    and cannot be recomputed for an app version that has moved on.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return _error("'ids' must be a non-empty list")
    removed = bench_store.delete([i for i in ids if isinstance(i, str)])
    return {**_history(), "removed": removed}

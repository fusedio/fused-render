"""The three benchmark endpoints (server/routers/ai_benchmark.py, SPEC AI-14).

`POST /api/ai/benchmark` holds the request open for the whole run, which is what
makes the rejections here worth pinning individually: by the time a bad request
reaches the supervisor it has already evicted somebody's resident model, and the
measurement it was interrupting is gone.

**The real `benchmark.run()` executes in these tests** — only the four supervisor
entry points beneath it are faked. Monkeypatching `run` itself would have left
the router's whole contract with it (that it returns a record, that the record
lands in the store) asserted against a stand-in, which is exactly the class of
green-over-nothing this suite has been bitten by before.

No model is downloaded and no worker is spawned; runner resolution is forced
rather than inherited, because MLX resolves on a Mac and on nothing in CI.
"""

import json

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.ai import bench_store, benchmark
from fused_render.ai import registry as ai_registry
from fused_render.ai.hub_cache import CachedModel
from fused_render.server import create_app


FUSED = {"X-Fused": "1"}  # D3 guard header required on the POSTs


class FakeRunner:
    code = "fake-runner"


def _stored(run_id: str) -> dict:
    """A record the store will hand back. `metrics`/`workload` are not optional
    padding: `bench_store.read` drops a record the page could not render, so a
    fixture standing for a stored run has to be one."""
    return {"id": run_id, "capability": ai_registry.TEXT_GENERATION,
            "workload": {"name": "w", "revision": 1, "params": {}}, "metrics": {}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    app = create_app(start_dir=str(tmp_path))
    return TestClient(app)


@pytest.fixture
def runnable(monkeypatch):
    """A machine that can serve every capability, holds `bench/model` resident,
    and generates instantly. Forced on purpose — see the module docstring."""
    monkeypatch.setattr(benchmark.registry, "for_capability", lambda cap: FakeRunner)
    monkeypatch.setattr(benchmark.supervisor, "ready_worker",
                        lambda cap, model=None: object())
    monkeypatch.setattr(benchmark.supervisor, "describe", lambda: {"loaded": [
        {"model": "bench/model", "capability": ai_registry.TEXT_GENERATION,
         "residentBytes": 4_000_000_000, "device": "mps"},
    ]})

    def generate_text(model, body):
        yield {"type": "chunk", "text": "hi"}
        yield {"type": "done", "ok": True, "tokens": 8, "input_tokens": 20}

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)


@pytest.fixture
def on_disk(monkeypatch):
    """`bench/model` is the one model this machine holds.

    Stubbed at the router's own lookup rather than by building a hub cache: what
    is under test is the rejection, and a real cache tree would make this a test
    of `hub_cache` instead."""
    from fused_render.server.routers import ai_benchmark as mod
    monkeypatch.setattr(mod, "_benchmarkable_models",
                        lambda capability: {"bench/model"})


# -- GET ------------------------------------------------------------------------


def test_history_on_an_empty_store_answers_with_no_runs_and_this_machine(client):
    """An empty history is a real answer, not a 404: the page needs the machine
    block to caption its (empty) chart, and a 404 would render as a failure."""
    body = client.get("/api/ai/benchmark").json()
    assert body["runs"] == []
    assert body["machine"] == benchmark.machine()


def test_history_returns_the_stored_runs_oldest_first(client, monkeypatch):
    for name in ("a", "b"):
        bench_store.append(_stored(name))
    assert [r["id"] for r in client.get("/api/ai/benchmark").json()["runs"]] == ["a", "b"]


def test_history_names_exactly_the_workload_covered_capabilities(client):
    """`workloadCapabilities` is what lets the Benchmark tab gate its own Run
    button on the server's own answer (`benchmark.NO_WORKLOAD_YET`) rather
    than hardcoding the gap — `text-to-video` is registered but has no fixed
    workload, so it must be ABSENT here even though the registry knows it."""
    body = client.get("/api/ai/benchmark").json()
    assert set(body["workloadCapabilities"]) == set(benchmark.WORKLOADS)
    assert ai_registry.VIDEO_GENERATION not in body["workloadCapabilities"]


def test_history_carries_the_actual_workload_table_derived_not_restated(client):
    """`workloads` (D483) is what lets the Benchmark tab say WHAT a run
    measures — a fixed prompt and token budget, a generated tone, a fixed
    image size — as a server fact rather than a frontend copy of
    `benchmark.WORKLOADS` that could silently drift from it. Built from the
    IDENTICAL `Workload.as_dict()` a run record's own `workload` block uses,
    so this asserts the response is that table, verbatim, not a hand-written
    stand-in for it."""
    body = client.get("/api/ai/benchmark").json()
    assert set(body["workloads"]) == set(benchmark.WORKLOADS)
    for capability, workload in benchmark.WORKLOADS.items():
        # Round-tripped through the SAME json encode/decode the real response
        # went through — `as_dict()`'s own `params` can hold a tuple (the
        # embeddings workload's `texts`), and JSON has no tuple type, so a
        # bare equality check here would fail on a list vs. tuple difference
        # that is an encoding artefact, not a real mismatch.
        expected = json.loads(json.dumps(workload.as_dict()))
        assert body["workloads"][capability] == expected
    # Video generation has no workload at all (`NO_WORKLOAD_YET`) — absent
    # from both fields, not a null placeholder in this one.
    assert ai_registry.VIDEO_GENERATION not in body["workloads"]


# -- POST -----------------------------------------------------------------------


def _post(client, **body):
    return client.post("/api/ai/benchmark", json=body, headers=FUSED)


def test_a_run_is_measured_appended_and_returned(client, runnable, on_disk):
    response = _post(client, model="bench/model",
                     capability=ai_registry.TEXT_GENERATION)
    assert response.status_code == 200
    record = response.json()["run"]
    assert record["ok"] is True
    assert record["model"] == "bench/model"
    assert record["peakResidentBytes"] == 4_000_000_000
    # The same record the history now holds — the response is not a second,
    # separately-built view of it.
    assert client.get("/api/ai/benchmark").json()["runs"] == [record]


def test_a_failed_run_is_a_200_carrying_a_not_ok_record(client, runnable, on_disk,
                                                         monkeypatch):
    """A model that OOMs is not a broken REQUEST. The run happened, it has a
    result, and the page shows it beside the successful ones — so the HTTP status
    describes the request and `ok` describes the model."""
    def generate_text(model, body):
        raise benchmark.supervisor.SupervisorError("out of memory")
        yield  # pragma: no cover - the real one is a generator too

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    response = _post(client, model="bench/model",
                     capability=ai_registry.TEXT_GENERATION)
    assert response.status_code == 200
    assert response.json()["run"]["ok"] is False
    assert response.json()["run"]["error"] == "out of memory"
    assert len(client.get("/api/ai/benchmark").json()["runs"]) == 1


def test_the_post_carries_the_d3_guard(client, runnable, on_disk):
    """It starts processes and holds a request open for minutes, so it is a
    mutating POST like every other one in the AI routers."""
    response = client.post("/api/ai/benchmark",
                           json={"model": "bench/model",
                                 "capability": ai_registry.TEXT_GENERATION})
    assert response.status_code == 403
    assert bench_store.read() == []


def test_an_unknown_capability_is_rejected_before_anything_loads(client, runnable,
                                                                on_disk):
    response = _post(client, model="bench/model", capability="telepathy")
    assert response.status_code == 400
    assert "telepathy" in response.json()["error"]
    assert bench_store.read() == []


def test_a_missing_model_is_rejected(client, runnable, on_disk):
    response = _post(client, capability=ai_registry.TEXT_GENERATION)
    assert response.status_code == 400
    assert "model" in response.json()["error"]


def test_a_model_this_machine_does_not_have_is_rejected(client, runnable, on_disk):
    """404 rather than a run: a benchmark must not become the thing that starts
    a 16GB download, and "nothing happened" with a spinner is the worst possible
    answer to a button press."""
    response = _post(client, model="somebody/else",
                     capability=ai_registry.TEXT_GENERATION)
    assert response.status_code == 404
    assert "somebody/else" in response.json()["error"]
    assert bench_store.read() == []


def test_a_second_run_on_the_same_capability_is_refused(client, runnable, on_disk,
                                                        monkeypatch):
    """Two concurrent runs on one capability would have the second EVICT the
    first's model mid-measurement — the same hazard the transcription lock
    exists for. Refused with a sentence rather than allowed to corrupt a number
    somebody is waiting minutes for.

    Simulated by claiming the capability directly instead of racing two threads:
    the guard is the subject, and a thread race would test the test.
    """
    from fused_render.server.routers import ai_benchmark as mod
    with mod._claim(ai_registry.TEXT_GENERATION):
        response = _post(client, model="bench/model",
                         capability=ai_registry.TEXT_GENERATION)
    assert response.status_code == 409
    assert "already" in response.json()["error"]
    assert bench_store.read() == []


def test_the_claim_is_released_even_when_the_run_fails(client, runnable, on_disk,
                                                       monkeypatch):
    """The guard must not be able to wedge a capability: a run that raised on
    the way out would otherwise leave the button dead until a restart."""
    def generate_text(model, body):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    from fused_render.server.routers import ai_benchmark as mod
    assert _post(client, model="bench/model",
                 capability=ai_registry.TEXT_GENERATION).status_code == 200
    assert ai_registry.TEXT_GENERATION not in mod._running
    # …and the next press is a run rather than a 409 on a claim nobody holds.
    assert _post(client, model="bench/model",
                 capability=ai_registry.TEXT_GENERATION).status_code == 200
    assert len(bench_store.read()) == 2


def test_a_different_capability_may_run_alongside(client, runnable, on_disk,
                                                  monkeypatch):
    """The guard is per capability, not global: the supervisor holds one
    resident model PER capability, so an embedding benchmark cannot evict a text
    one and there is no reason to serialize them."""
    from fused_render.server.routers import ai_benchmark as mod
    monkeypatch.setattr(mod, "_benchmarkable_models",
                        lambda capability: {"bench/model"})
    monkeypatch.setattr(benchmark.supervisor, "generate_embed",
                        lambda model, body: {"vectors": [], "dim": 4})
    with mod._claim(ai_registry.EMBEDDINGS):
        response = _post(client, model="bench/model",
                         capability=ai_registry.TEXT_GENERATION)
    assert response.status_code == 200


# -- delete ---------------------------------------------------------------------


def test_delete_removes_the_named_runs_and_answers_with_the_rest(client):
    for name in ("a", "b", "c"):
        bench_store.append(_stored(name))
    response = client.post("/api/ai/benchmark/delete",
                           json={"ids": ["a", "c"]}, headers=FUSED)
    assert response.status_code == 200
    body = response.json()
    assert body["removed"] == 2
    # The fresh history comes back with it, so the page swaps in state it just
    # re-read rather than patching rows it hopes are still true.
    assert [r["id"] for r in body["runs"]] == ["b"]
    assert body["machine"] == benchmark.machine()


def test_delete_requires_a_non_empty_list_of_ids(client):
    for body in ({}, {"ids": []}, {"ids": "a"}):
        response = client.post("/api/ai/benchmark/delete", json=body, headers=FUSED)
        assert response.status_code == 400, body
        assert "ids" in response.json()["error"]


def test_delete_carries_the_d3_guard(client):
    bench_store.append(_stored("a"))
    response = client.post("/api/ai/benchmark/delete", json={"ids": ["a"]})
    assert response.status_code == 403
    assert [r["id"] for r in bench_store.read()] == ["a"]


def test_deleting_an_id_that_is_already_gone_is_not_an_error(client):
    response = client.post("/api/ai/benchmark/delete",
                           json={"ids": ["nosuch"]}, headers=FUSED)
    assert response.status_code == 200
    assert response.json()["removed"] == 0


# -- the on-this-machine guard --------------------------------------------------
#
# **These tests do NOT stub `_benchmarkable_models`.** The fixture above does,
# and that stub is exactly why the guard shipped broken: it admitted every
# CURATED id, because `catalog.for_capability()` answers from the curation and
# has no filesystem awareness at all — so pressing Run on a recommended model
# started a multi-GB download inside a request held open for up to an hour,
# which is the precise thing the router's own docstring promises a 404 for.
# Only the disk SCAN is faked below; the guard itself is real.


def _cached(repo_id, capability, files=()):
    return CachedModel(repo_id, capability, 1, ("fake-runner",), frozenset(files))


@pytest.fixture
def disk(monkeypatch):
    """A machine holding one text repo, plus a curated shortlist for that
    capability of which only one entry is actually downloaded."""
    from fused_render.server.routers import ai_benchmark as mod

    monkeypatch.setattr(mod, "cached_models", lambda: [
        _cached("on/disk", ai_registry.TEXT_GENERATION),
        # The repo behind a curated GGUF FILENAME id, carrying one of its files.
        _cached("vendor/Some-GGUF", ai_registry.TEXT_GENERATION,
                files={"here.gguf"}),
    ])
    monkeypatch.setattr(mod.catalog, "for_capability", lambda cap: [
        {"id": "curated/not-here"},
        {"id": "here.gguf"},
        {"id": "missing.gguf"},
    ])
    monkeypatch.setattr(mod.formats, "GGUF_RECIPES", {
        "here.gguf": {"repo": "vendor/Some-GGUF", "file": "here.gguf"},
        "missing.gguf": {"repo": "vendor/Some-GGUF", "file": "missing.gguf"},
    })
    return mod


def test_a_curated_model_that_is_not_downloaded_is_not_benchmarkable(disk):
    """The bug: `catalog.for_capability` is the CURATION, and a curated id with
    no bytes on this disk must not reach `supervisor.load()`."""
    admitted = disk._benchmarkable_models(ai_registry.TEXT_GENERATION)
    assert "curated/not-here" not in admitted


def test_a_curated_gguf_filename_is_benchmarkable_only_when_its_file_is_present(
        disk):
    """The reason the catalog half is consulted at all: `llamacpp-text`'s curated
    ids are FILENAMES, not repo ids (AI-5m), so they can never appear as a cached
    `repo_id` — they resolve through the recipe's (repo, file) pair against the
    snapshot's own filenames."""
    admitted = disk._benchmarkable_models(ai_registry.TEXT_GENERATION)
    assert "here.gguf" in admitted          # the repo is here AND holds the file
    assert "missing.gguf" not in admitted   # same repo, that file never landed


def test_a_repo_on_this_disk_is_benchmarkable(disk):
    assert "on/disk" in disk._benchmarkable_models(ai_registry.TEXT_GENERATION)


def test_a_repo_cached_for_a_different_capability_is_not_benchmarkable(monkeypatch):
    """The bug: `admitted = {model.repo_id for model in cached}` (no
    capability filter) admitted a downloaded Whisper model as benchmarkable
    for text-generation — the docstring's own claim ("a curated speech model
    cannot be benchmarked as a text one") only ever held for the CATALOG half
    of this function, never the disk-scan half. `CachedModel.capability` is
    what tells the two apart."""
    from fused_render.server.routers import ai_benchmark as mod

    monkeypatch.setattr(mod, "cached_models", lambda: [
        _cached("openai/whisper-large-v3", ai_registry.SPEECH_TO_TEXT),
        _cached("on/disk", ai_registry.TEXT_GENERATION),
    ])
    monkeypatch.setattr(mod.catalog, "for_capability", lambda cap: [])
    admitted = mod._benchmarkable_models(ai_registry.TEXT_GENERATION)
    assert "openai/whisper-large-v3" not in admitted
    assert "on/disk" in admitted
    # And the reverse: the speech model IS benchmarkable for its own capability.
    assert "openai/whisper-large-v3" in mod._benchmarkable_models(
        ai_registry.SPEECH_TO_TEXT)


def test_a_partly_downloaded_repo_is_not_benchmarkable(monkeypatch):
    """`cached_models()` already drops a fetch that never finished (D424), which
    is what keeps a Run press from RESUMING a multi-GB pull inside the held-open
    request. Pinned here because this guard now rests on that filtering."""
    from fused_render.server.routers import ai_benchmark as mod

    # What the D424 skip inside `cached_models` leaves behind for a repo whose
    # blobs/ still holds a `.fusedpart`: nothing.
    monkeypatch.setattr(mod, "cached_models", lambda: [])
    monkeypatch.setattr(mod.catalog, "for_capability",
                        lambda cap: [{"id": "half/done"}])
    assert mod._benchmarkable_models(ai_registry.TEXT_GENERATION) == set()


def test_posting_a_curated_but_undownloaded_model_is_a_404_not_a_download(
        client, runnable, disk, monkeypatch):
    """End to end with the guard REAL: the 404 the docstring promises, and no
    load started on the way to it."""
    loads = []
    monkeypatch.setattr(benchmark.supervisor, "load",
                        lambda *a, **k: loads.append(a))
    response = _post(client, model="curated/not-here",
                     capability=ai_registry.TEXT_GENERATION)
    assert response.status_code == 404
    assert "curated/not-here" in response.json()["error"]
    assert bench_store.read() == []
    assert loads == []


def test_the_response_carries_no_job_id_but_a_titled_row_is_created(
        client, on_disk, monkeypatch):
    """The wire contract, guarded end to end.

    The HTTP response never carries a `jobId` — a page finds the measurement
    row the same way it finds any other server row, through
    `useCacheScan.ts`'s `job.title -> job` map, never through a value handed
    back by this endpoint. Three earlier attempts at the row itself collided
    on TITLE (a decorated one no consumer could find, then a bare one that
    SHADOWED the row `supervisor.load` opens for the same model — putting the
    download manager's only ✕ on the load and letting a cold run spin to its
    hour-long timeout); `_bench_job_title` is the fix, so THIS test asserts a
    row exists, titled distinctly, rather than asserting none does. Reporting
    is left REAL here, so the row the run actually opened shows up.
    """
    monkeypatch.setattr(benchmark.registry, "for_capability", lambda cap: FakeRunner)
    monkeypatch.setattr(benchmark.supervisor, "ready_worker",
                        lambda cap, model=None: object())
    monkeypatch.setattr(benchmark.supervisor, "describe", lambda: {"loaded": []})

    def generate_text(model, body):
        yield {"type": "chunk", "text": "hi"}
        yield {"type": "done", "ok": True, "tokens": 8}

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    jobs.reset()
    try:
        body = _post(client, model="bench/model",
                     capability=ai_registry.TEXT_GENERATION).json()
        assert "jobId" not in body
        assert body["run"]["ok"] is True
        rows = jobs.list_jobs()
        assert len(rows) == 1, "exactly one row for a benchmark, never zero or two"
        assert rows[0]["title"] == "Benchmark · bench/model"
        assert rows[0]["title"] != "bench/model"
        assert rows[0]["state"] == "done"
    finally:
        jobs.reset()


# -- a cancel on the wire -------------------------------------------------------


def test_a_cancelled_run_answers_with_no_run_at_all(client, runnable, on_disk,
                                                    monkeypatch):
    """Distinct on the wire, not an `ok:false` record. The page reads the ABSENCE
    of `run`; if the cancel came back as a failed run it would be appended and
    draw a phantom "Failed — cancelled" row that became the model's latest."""
    def generate_text(model, body):
        raise benchmark.Cancelled()
        yield  # pragma: no cover - the real one is a generator too

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    response = _post(client, model="bench/model",
                     capability=ai_registry.TEXT_GENERATION)
    # 200, not a 4xx: the request was fine and the user did this on purpose, so a
    # status the client's `catch` turns into an error banner would be wrong.
    assert response.status_code == 200
    body = response.json()
    assert body["cancelled"] is True
    assert "run" not in body
    assert "jobId" not in body
    assert bench_store.read() == []


def test_a_cancel_releases_the_capability_for_the_next_press(client, runnable,
                                                             on_disk,
                                                             monkeypatch):
    """The claim must not leak out through the new exception path — a leaked one
    leaves that capability's Run button dead until a restart."""
    from fused_render.server.routers import ai_benchmark as mod

    def cancelling(model, body):
        raise benchmark.Cancelled()
        yield  # pragma: no cover

    monkeypatch.setattr(benchmark.supervisor, "generate_text", cancelling)
    _post(client, model="bench/model", capability=ai_registry.TEXT_GENERATION)
    assert ai_registry.TEXT_GENERATION not in mod._running


def test_a_runtime_error_from_a_measurement_is_not_reported_as_busy(
        client, runnable, on_disk, monkeypatch):
    """`_claim` used to raise a bare `RuntimeError` and the `except` around it now
    also wraps `benchmark.run`, so a `RuntimeError` out of a measurement would
    have been answered "a benchmark is already running" — a 409 blaming a
    concurrent run that does not exist."""
    def exploding(model, body):
        raise RuntimeError("something inside the runner")
        yield  # pragma: no cover

    monkeypatch.setattr(benchmark.supervisor, "generate_text", exploding)
    response = _post(client, model="bench/model",
                     capability=ai_registry.TEXT_GENERATION)
    assert response.status_code == 200
    assert response.json()["run"]["ok"] is False
    assert "something inside the runner" in response.json()["run"]["error"]

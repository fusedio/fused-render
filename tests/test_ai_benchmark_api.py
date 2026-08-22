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

import pytest
from fastapi.testclient import TestClient

from fused_render.ai import bench_store, benchmark
from fused_render.ai import registry as ai_registry
from fused_render.server import create_app


FUSED = {"X-Fused": "1"}  # D3 guard header required on the POSTs


class FakeRunner:
    code = "fake-runner"


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
    monkeypatch.setattr(benchmark.supervisor, "_report", lambda job, **f: None)

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
        bench_store.append({"id": name, "capability": ai_registry.TEXT_GENERATION})
    assert [r["id"] for r in client.get("/api/ai/benchmark").json()["runs"]] == ["a", "b"]


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
        bench_store.append({"id": name, "capability": ai_registry.TEXT_GENERATION})
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
    bench_store.append({"id": "a"})
    response = client.post("/api/ai/benchmark/delete", json={"ids": ["a"]})
    assert response.status_code == 403
    assert [r["id"] for r in bench_store.read()] == ["a"]


def test_deleting_an_id_that_is_already_gone_is_not_an_error(client):
    response = client.post("/api/ai/benchmark/delete",
                           json={"ids": ["nosuch"]}, headers=FUSED)
    assert response.status_code == 200
    assert response.json()["removed"] == 0

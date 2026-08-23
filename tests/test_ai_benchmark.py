"""Tests for `benchmark.run()` — the measurement itself (SPEC AI-14).

**No model is loaded and no worker is spawned.** Every supervisor entry point
this module drives is monkeypatched, in the style tests/test_ai_runtime.py
established for the supervisor: the thing under test is the ORCHESTRATION —
what is timed, what is excluded, what stays null — and none of that needs mlx,
torch or a network.

The clock is scripted rather than real. `benchmark._now` is replaced with a
`Clock` the fakes advance themselves, so "the warm-up pass is excluded" is
asserted against an exact number instead of against a tolerance, and the suite
does not get slower to test a throughput figure.

Runner resolution is forced too, never inherited from the machine: MLX resolves
on this laptop and on nothing in CI, so a test that let the registry answer
would assert one thing here and another there.
"""
import json
import os
import wave

import pytest

from fused_render import jobs
from fused_render.ai import bench_store, benchmark
from fused_render.ai import registry as ai_registry


class Clock:
    """A monotonic clock the fakes drive. Never advances on its own — every
    second in these tests is one a fake deliberately spent."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeRunner:
    """Only the two fields `run()` reads off a resolved runner."""

    code = "fake-runner"
    capability = ""


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """A clock, a forced runner resolution, a tmp store, and a resident model.

    Returns the `Clock` — every test scripts its own timeline onto it.
    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    clock = Clock()
    monkeypatch.setattr(benchmark, "_now", clock)
    monkeypatch.setattr(benchmark.registry, "for_capability", lambda cap: FakeRunner)
    # Warm by default: the cold path is one test's subject, not every test's
    # preamble.
    monkeypatch.setattr(benchmark.supervisor, "ready_worker",
                        lambda cap, model=None: object())
    monkeypatch.setattr(benchmark.supervisor, "describe", lambda: {"loaded": []})
    # A no-op that just says "there was nothing to stop" by default — most
    # tests here are warm and must never reach it at all; the ones that force a
    # cold load override this with a recording fake (see D446's section below).
    monkeypatch.setattr(benchmark.supervisor, "unload", lambda **kwargs: False)
    return clock


# -- text generation ------------------------------------------------------------


def _text_run(clock, monkeypatch, *, warmup_seconds=100.0, done=None):
    """One text benchmark whose warm-up burns `warmup_seconds` and whose timed
    pass follows a fixed timeline: first token at +0.5s, done at +4.0s."""
    calls = {"n": 0}
    done = done if done is not None else {
        "type": "done", "ok": True, "tokens": 41, "input_tokens": 20,
        "seconds": 4.5,
    }

    def generate_text(model, body):
        calls["n"] += 1
        if calls["n"] == 1:
            # The discarded warm-up: deliberately absurdly slow, so any timing
            # that leaks into the result is unmissable rather than marginal.
            clock.advance(warmup_seconds)
            yield {"type": "chunk", "text": "x"}
            yield {"type": "done", "ok": True, "tokens": 1}
            return
        clock.advance(0.5)
        yield {"type": "chunk", "text": "Hello"}
        clock.advance(4.0)
        yield {"type": "chunk", "text": " world"}
        yield done

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    return record, calls


def test_text_reports_throughput_ttft_and_prompt_rate(bench, monkeypatch):
    """The primary metric is the DECODE rate, so the time spent producing the
    first token is charged to `ttftMs` and excluded from it — a model with a
    slow prefill and a fast decode has to read as exactly that."""
    record, _ = _text_run(bench, monkeypatch)
    metrics = record["metrics"]
    assert record["ok"] is True
    assert record["error"] is None
    assert metrics["ttftMs"] == pytest.approx(500.0)
    # 41 tokens, the first of which is the TTFT one, over the 4.0s that followed.
    assert metrics["tokensPerSecond"] == pytest.approx(10.0)
    assert metrics["outputTokens"] == 41
    # 20 prompt tokens read in the 0.5s before the first output token.
    assert metrics["promptTokensPerSecond"] == pytest.approx(40.0)


def test_the_warm_up_pass_is_excluded_from_the_timing(bench, monkeypatch):
    """A first generation pays for graph compilation, a lazily-materialised
    tokenizer and a cold cache. Timing it would make every benchmark a
    measurement of the first token in the process's life."""
    record, calls = _text_run(bench, monkeypatch, warmup_seconds=100.0)
    assert calls["n"] == 2  # warm-up, then the timed pass
    assert record["metrics"]["tokensPerSecond"] == pytest.approx(10.0)
    # Restated as an inequality too: if the 100s leaked in, the rate collapses.
    assert record["metrics"]["tokensPerSecond"] > 1.0


def test_a_metric_the_runner_will_not_report_stays_null(bench, monkeypatch):
    """The rule the whole feature rests on: never estimated, never zero. A done
    frame with no token count could have its tokens counted from the text, and
    that number would be a DIFFERENT measurement wearing the same label."""
    record, _ = _text_run(bench, monkeypatch,
                          done={"type": "done", "ok": True})
    metrics = record["metrics"]
    assert metrics["outputTokens"] is None
    assert metrics["tokensPerSecond"] is None
    assert metrics["promptTokensPerSecond"] is None
    # TTFT was genuinely measured by us, so it survives the missing counts.
    assert metrics["ttftMs"] == pytest.approx(500.0)
    assert record["ok"] is True


def test_a_done_frame_that_says_not_ok_fails_the_run(bench, monkeypatch):
    record, _ = _text_run(bench, monkeypatch,
                          done={"type": "done", "ok": False, "error": "out of memory"})
    assert record["ok"] is False
    assert "out of memory" in record["error"]


# -- a race settling between the load wait and the measurement call -------------
#
# `_load_to_ready` confirms a model ready before `run()` calls a measurement
# function, but `generate_text`/`generate_embed` re-check readiness themselves an
# instant later. If something else — another benchmark in a Run-all queue, a page
# elsewhere in the app — evicted the worker in that gap, those two answer the
# same way they answer a cold caller: kick off a load and raise `ModelNotReady`
# immediately, never blocking. A benchmark that recorded that as a permanent
# failure ("Qwen/Qwen3-4B-Instruct-2507 is loading now") never even attempted the
# model; `_await_settled_load` exists to wait the race out instead.


def test_a_model_not_ready_race_is_waited_out_and_retried(bench, monkeypatch):
    calls = {"n": 0}

    def generate_text(model, body):
        calls["n"] += 1
        if calls["n"] == 1:
            # The discarded warm-up: uneventful.
            bench.advance(1.0)
            yield {"type": "chunk", "text": "x"}
            yield {"type": "done", "ok": True, "tokens": 1}
            return
        if calls["n"] == 2:
            # The timed pass's first attempt races into an eviction — nothing
            # yielded yet, so nothing measured is lost by retrying.
            raise benchmark.supervisor.ModelNotReady(
                f"{model} is loading now", "sys:job")
        bench.advance(0.5)
        yield {"type": "chunk", "text": "Hello"}
        bench.advance(4.0)
        yield {"type": "chunk", "text": " world"}
        yield {"type": "done", "ok": True, "tokens": 41, "input_tokens": 20}

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is True
    assert record["error"] is None
    # warm-up, the raced attempt, and the retry that succeeded.
    assert calls["n"] == 3
    assert record["metrics"]["tokensPerSecond"] == pytest.approx(10.0)


def test_a_model_not_ready_race_that_never_settles_is_a_real_failure(
        bench, monkeypatch):
    """The wait is bounded by `_LOAD_TIMEOUT_S`, same as a genuine cold load —
    a capability something keeps re-evicting must eventually fail the run
    rather than hold the request open forever."""
    monkeypatch.setattr(benchmark, "_LOAD_POLL_S", 0.0)
    monkeypatch.setattr(benchmark, "_LOAD_TIMEOUT_S", 0.0)
    # The wait polls `ready_worker`, which must never say "ready" for this to
    # time out rather than settle.
    monkeypatch.setattr(benchmark.supervisor, "ready_worker",
                        lambda cap, model=None: None)

    def generate_text(model, body):
        raise benchmark.supervisor.ModelNotReady(
            f"{model} is loading now", "sys:job")

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    assert "did not finish loading in time" in record["error"]


def test_an_embedding_race_is_waited_out_and_retried_the_same_way(
        bench, monkeypatch):
    calls = {"n": 0}

    def generate_embed(model, body):
        calls["n"] += 1
        if calls["n"] == 1:
            # The discarded warm-up: uneventful.
            bench.advance(0.1)
            return {"vectors": [[0.0] * 768] * len(body["texts"]), "dim": 768}
        if calls["n"] == 2:
            # The timed pass's first attempt races into an eviction.
            raise benchmark.supervisor.ModelNotReady(
                f"{model} is loading now", "sys:job")
        bench.advance(2.0)
        return {"vectors": [[0.0] * 768] * len(body["texts"]), "dim": 768}

    monkeypatch.setattr(benchmark.supervisor, "generate_embed", generate_embed)
    record = benchmark.run("some/embed-model", ai_registry.EMBEDDINGS)
    assert record["ok"] is True
    # warm-up, the raced attempt, and the retry that succeeded.
    assert calls["n"] == 3
    assert record["metrics"]["dim"] == 768


# -- embeddings -----------------------------------------------------------------


def test_embeddings_report_texts_per_second_and_dim(bench, monkeypatch):
    calls = {"n": 0}

    def generate_embed(model, body):
        calls["n"] += 1
        assert body["texts"] == list(
            benchmark.WORKLOADS[ai_registry.EMBEDDINGS].params["texts"])
        bench.advance(50.0 if calls["n"] == 1 else 2.0)
        return {"vectors": [[0.0] * 768] * len(body["texts"]), "dim": 768,
                "model": model}

    monkeypatch.setattr(benchmark.supervisor, "generate_embed", generate_embed)
    record = benchmark.run("some/embed-model", ai_registry.EMBEDDINGS)
    metrics = record["metrics"]
    assert calls["n"] == 2
    assert metrics["batch"] == 8
    assert metrics["textsPerSecond"] == pytest.approx(4.0)  # 8 texts / 2.0s
    assert metrics["dim"] == 768


def test_an_embedding_reply_with_no_dim_leaves_dim_null(bench, monkeypatch):
    monkeypatch.setattr(benchmark.supervisor, "generate_embed",
                        lambda model, body: (bench.advance(1.0), {"vectors": []})[1])
    record = benchmark.run("some/embed-model", ai_registry.EMBEDDINGS)
    assert record["metrics"]["dim"] is None


# -- text to image --------------------------------------------------------------


def test_image_reports_seconds_per_step_at_the_catalog_default_steps(bench,
                                                                    monkeypatch):
    """Seconds per step is the primary metric precisely BECAUSE the step count
    is not fixed — a step-distilled model runs at 4 where another needs 28, and
    the per-step figure is the only comparable one. The count rides along on the
    run so a reader can reconstruct the wall clock."""
    seen = []

    def generate_image(model, request, job):
        seen.append(request)
        bench.advance(40.0 if len(seen) == 1 else 12.0)
        return {"path": request["out"], "steps": request["steps"]}

    monkeypatch.setattr(benchmark.supervisor, "generate_image", generate_image)
    monkeypatch.setattr(benchmark, "_image_steps", lambda model: 4)
    record = benchmark.run("some/image-model", ai_registry.IMAGE_GENERATION)
    metrics = record["metrics"]
    assert len(seen) == 2  # warm-up, then timed
    assert metrics["steps"] == 4
    assert metrics["totalSeconds"] == pytest.approx(12.0)
    assert metrics["secondsPerStep"] == pytest.approx(3.0)
    assert (metrics["width"], metrics["height"]) == (512, 512)
    # The workload's own prompt and canvas, not the caller's — that is what
    # "fixed" means. And the PNG goes to a temp path the run cleans up, never
    # into the user's images folder.
    workload = benchmark.WORKLOADS[ai_registry.IMAGE_GENERATION]
    assert seen[1]["prompt"] == workload.params["prompt"]
    assert seen[1]["seed"] == workload.params["seed"]
    import os
    assert not os.path.exists(seen[1]["out"])


def test_image_steps_fall_back_to_the_server_default_without_a_catalog_hint(
        bench, monkeypatch):
    """`_image_steps` is asked of the catalog, which is where the per-model hint
    lives; a model with no entry keeps the server's generic default rather than
    picking a number here."""
    monkeypatch.setattr(benchmark.catalog, "for_capability", lambda cap: [
        {"id": "known/model", "defaults": {"steps": 4}},
        {"id": "hintless/model"},
    ])
    assert benchmark._image_steps("known/model") == 4
    assert benchmark._image_steps("hintless/model") == benchmark.DEFAULT_IMAGE_STEPS
    assert benchmark._image_steps("never/heard-of-it") == benchmark.DEFAULT_IMAGE_STEPS


# -- speech to text -------------------------------------------------------------


def test_speech_reports_a_realtime_factor_over_a_synthesized_tone(bench,
                                                                 monkeypatch):
    """The audio is generated here with the stdlib rather than committed as a
    fixture, and its duration comes from the WORKLOAD rather than from whatever
    the model claims it heard — a fixed workload whose length the measured thing
    gets to report is not fixed."""
    seen = []

    def generate_transcript(model, request, job):
        seen.append(request)
        # The file really exists and really is 30 seconds of 16 kHz mono, which
        # is the half of this that a mocked-out writer would not have caught.
        with wave.open(request["path"], "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getframerate() == 16000
            assert wav.getnframes() == 16000 * 30
        bench.advance(20.0 if len(seen) == 1 else 3.0)
        return {"text": "beep", "duration": 30.0}

    monkeypatch.setattr(benchmark.supervisor, "generate_transcript",
                        generate_transcript)
    record = benchmark.run("some/whisper", ai_registry.SPEECH_TO_TEXT)
    metrics = record["metrics"]
    assert metrics["audioSeconds"] == pytest.approx(30.0)
    assert metrics["totalSeconds"] == pytest.approx(3.0)
    assert metrics["realtimeFactor"] == pytest.approx(10.0)
    import os
    assert not os.path.exists(seen[1]["path"])  # the temp dir is cleaned up


# -- load timing ----------------------------------------------------------------


def test_a_cold_model_records_the_seconds_it_took_to_load(bench, monkeypatch):
    """A cold load is a real cost somebody waits through and a real difference
    between two quantizations of one model, so it is measured — separately,
    because folding it into throughput would make the second run of the same
    model look faster than the first for no reason of the model's."""
    states = {"ready": False}

    def ready_worker(capability, model=None):
        return object() if states["ready"] else None

    def start_resident(model, capability):
        bench.advance(8.5)
        states["ready"] = True
        return {"jobId": "sys:ai-model:x", "model": model, "state": "loading"}, FakePending()

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    monkeypatch.setattr(benchmark.supervisor, "_start_resident", start_resident)
    monkeypatch.setattr(benchmark, "_LOAD_POLL_S", 0.0)
    record, _ = _text_run(bench, monkeypatch)
    assert record["loadSeconds"] == pytest.approx(8.5)


def test_an_already_resident_model_records_a_null_load(bench, monkeypatch):
    """`null`, not `0.0`: nothing was loaded, and a zero would read as an
    impossibly fast load rather than as a load that did not happen."""
    record, _ = _text_run(bench, monkeypatch)
    assert record["loadSeconds"] is None


def test_a_load_that_never_becomes_ready_fails_the_run(bench, monkeypatch):
    monkeypatch.setattr(benchmark.supervisor, "ready_worker",
                        lambda cap, model=None: None)
    pending = FakePending()
    monkeypatch.setattr(benchmark.supervisor, "_start_resident",
                        lambda model, capability: ({"jobId": "j"}, pending))
    # The record still OWNS the capability, or the eviction branch answers first
    # and this stops being a test about the timeout at all — which is exactly
    # what had quietly happened.
    monkeypatch.setattr(benchmark.supervisor, "_workers",
                        {ai_registry.TEXT_GENERATION: pending})
    monkeypatch.setattr(benchmark, "_LOAD_POLL_S", 0.0)
    monkeypatch.setattr(benchmark, "_LOAD_TIMEOUT_S", 0.0)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    # The SENTENCE, not `"load" in ...` — which was also satisfied by
    # "was unloaded before it could be used" and is what made the drift
    # invisible when the eviction branch took this test over.
    assert record["error"] == "some/text-model did not finish loading in time"


# -- unload after a cold benchmark (D446) ----------------------------------------
#
# A benchmark that had to cold-load the model tears it back down when the run
# ends, success or failure alike, because a measurement is not a claim on the
# model's residency; a WARM run never touches `unload` at all, because the
# model belongs to whoever already had it running.


def _cold_ready(bench, monkeypatch):
    """Force `_load_to_ready` down the COLD path: `ready_worker` says no until
    `_start_resident` flips a flag, the same shape
    `test_a_cold_model_records_the_seconds_it_took_to_load` uses."""
    states = {"ready": False}

    def ready_worker(capability, model=None):
        return object() if states["ready"] else None

    def start_resident(model, capability):
        bench.advance(1.0)
        states["ready"] = True
        return {"jobId": "j"}, FakePending()

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    monkeypatch.setattr(benchmark.supervisor, "_start_resident", start_resident)
    monkeypatch.setattr(benchmark, "_LOAD_POLL_S", 0.0)


def _spy_unload(monkeypatch):
    """Replace the default no-op `unload` fake with one that records every
    call and still answers True, the way a real unload of a resident model
    would."""
    calls = []
    monkeypatch.setattr(benchmark.supervisor, "unload",
                        lambda **kwargs: (calls.append(kwargs), True)[1])
    return calls


def test_a_warm_run_never_calls_unload(bench, monkeypatch):
    calls = _spy_unload(monkeypatch)
    record, _ = _text_run(bench, monkeypatch)
    assert record["loadSeconds"] is None
    assert calls == []


def test_a_cold_run_unloads_the_model_it_loaded(bench, monkeypatch):
    _cold_ready(bench, monkeypatch)
    calls = _spy_unload(monkeypatch)
    record, _ = _text_run(bench, monkeypatch)
    assert record["ok"] is True
    assert record["loadSeconds"] == pytest.approx(1.0)
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "some/text-model"
    assert call["capability"] == ai_registry.TEXT_GENERATION
    # Says a BENCHMARK did it, not the default "Unloaded" — the job row's
    # detail is the only place a reader would see why this model just vanished
    # from the Local tab's Loaded badge.
    assert "benchmark" in call["reason"].lower()


def test_a_cold_run_is_unloaded_even_when_the_workload_fails(bench, monkeypatch):
    """The crash case is precisely the one that matters most: a model big
    enough to fail its measurement is a model whose gigabytes are worst left
    parked."""
    _cold_ready(bench, monkeypatch)
    calls = _spy_unload(monkeypatch)
    record, _ = _text_run(bench, monkeypatch,
                          done={"type": "done", "ok": False, "error": "out of memory"})
    assert record["ok"] is False
    assert record["error"] == "out of memory"
    assert len(calls) == 1
    assert calls[0]["model"] == "some/text-model"


def test_a_cancelled_load_is_never_unloaded(loading, monkeypatch):
    """Nothing came up, so there is nothing of ours to tear down — see
    `test_a_cancelled_load_records_nothing` for the same scenario's other
    half."""
    clock, pending = loading
    calls = _spy_unload(monkeypatch)

    def ready_worker(capability, model=None):
        clock.advance(1.0)
        pending.state = "error"
        pending.error = "cancelled"
        benchmark.supervisor._workers.clear()
        return None

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    with pytest.raises(benchmark.Cancelled):
        benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert calls == []


def test_a_failing_unload_does_not_mask_the_measurement_error(bench, monkeypatch):
    """If `finally`'s own exception were left to propagate it would REPLACE the
    real one — Python's ordinary behaviour for an exception raised while
    another is already in flight — so a teardown bug would turn a legible
    "out of memory" into an opaque `RuntimeError` from deep inside the
    unload path. The measurement's own verdict must survive that."""
    _cold_ready(bench, monkeypatch)

    def failing_unload(**kwargs):
        raise RuntimeError("teardown exploded")

    monkeypatch.setattr(benchmark.supervisor, "unload", failing_unload)
    record, _ = _text_run(bench, monkeypatch,
                          done={"type": "done", "ok": False, "error": "out of memory"})
    assert record["ok"] is False
    assert record["error"] == "out of memory"


# -- memory, device and the record itself ---------------------------------------


def test_memory_and_device_come_off_describe_after_the_run(bench, monkeypatch):
    monkeypatch.setattr(benchmark.supervisor, "describe", lambda: {"loaded": [
        {"model": "other/model", "capability": ai_registry.EMBEDDINGS,
         "residentBytes": 1, "device": "cpu"},
        {"model": "some/text-model", "capability": ai_registry.TEXT_GENERATION,
         "residentBytes": 5_600_000_000, "device": "mps"},
    ]})
    record, _ = _text_run(bench, monkeypatch)
    assert record["peakResidentBytes"] == 5_600_000_000
    assert record["device"] == "mps"


def test_a_runner_that_reports_no_memory_leaves_it_null(bench, monkeypatch):
    monkeypatch.setattr(benchmark.supervisor, "describe", lambda: {"loaded": [
        {"model": "some/text-model", "capability": ai_registry.TEXT_GENERATION,
         "residentBytes": None, "device": None},
    ]})
    record, _ = _text_run(bench, monkeypatch)
    assert record["peakResidentBytes"] is None
    assert record["device"] is None


def test_the_record_carries_everything_needed_to_read_it_years_later(bench,
                                                                    monkeypatch):
    record, _ = _text_run(bench, monkeypatch)
    assert set(record) == {
        "id", "startedAt", "capability", "model", "runner", "device",
        "appVersion", "ok", "error", "loadSeconds", "peakResidentBytes",
        "machine", "workload", "metrics",
    }
    assert len(record["id"]) == 32  # uuid4 hex
    assert record["capability"] == ai_registry.TEXT_GENERATION
    assert record["model"] == "some/text-model"
    assert record["runner"] == "fake-runner"
    assert record["appVersion"]
    assert record["startedAt"] > 0
    assert record["machine"] == benchmark.machine()
    assert record["workload"] == (
        benchmark.WORKLOADS[ai_registry.TEXT_GENERATION].as_dict())
    # JSON-serializable, because the next thing that happens to it is a
    # write_json — a tuple in the params would round-trip as a list and a
    # numpy scalar would not round-trip at all.
    json.dumps(record)


def test_a_successful_run_is_appended_to_the_store(bench, monkeypatch):
    record, _ = _text_run(bench, monkeypatch)
    assert [r["id"] for r in bench_store.read()] == [record["id"]]


def test_a_raising_runner_yields_not_ok_and_is_still_appended(bench, monkeypatch):
    """A failure IS a result — "this model OOMs on this laptop" is exactly the
    thing somebody benchmarks to find out, and losing it would leave the page
    with a button that appears to do nothing."""
    def generate_text(model, body):
        raise benchmark.supervisor.SupervisorError("the model process is gone")
        yield  # pragma: no cover - makes this a generator, as the real one is

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    assert record["error"] == "the model process is gone"
    assert record["metrics"] == {}
    assert [r["id"] for r in bench_store.read()] == [record["id"]]


def test_an_unknown_capability_raises_rather_than_recording_nothing(bench):
    """The router turns this into a 4xx. It is not an `ok:false` run: there is
    no workload, so nothing was measured and there is nothing to record."""
    with pytest.raises(ValueError):
        benchmark.run("some/model", "telepathy")


def test_a_capability_with_no_runner_here_fails_without_loading(bench, monkeypatch):
    """Forced, not inherited: on a Linux CI box `text-to-image` may genuinely
    have no runner and on this Mac it does, so the resolution is stubbed to
    `None` and the assertion is about what `run()` does with that."""
    monkeypatch.setattr(benchmark.registry, "for_capability", lambda cap: None)
    monkeypatch.setattr(
        benchmark.registry, "unavailable_reason",
        lambda cap: "needs Apple Silicon (this is linux/x86_64)")
    # Two changes from the version that guarded nothing. It stubbed
    # `supervisor.load`, which `_load_to_ready` no longer calls, so the list it
    # asserted empty could never have been appended to. And the model was WARM,
    # so no bring-up would have been attempted whatever the ordering — which
    # makes the assertion vacuous a second way. `_start_resident` is the real
    # seam, and the model is cold, so this now fails if the runner check ever
    # moves below the load.
    monkeypatch.setattr(benchmark.supervisor, "ready_worker",
                        lambda cap, model=None: None)
    brought_up = []

    def start_resident(model, capability):
        # Records AND returns the real shape, so a regression fails on the
        # assertion below rather than on a TypeError from the double — a stub
        # that cannot survive being called tests the stub, not the code.
        brought_up.append(model)
        return {"jobId": "j"}, FakePending()

    monkeypatch.setattr(benchmark.supervisor, "_start_resident", start_resident)
    monkeypatch.setattr(benchmark, "_LOAD_TIMEOUT_S", 0.0)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    # This one FIRST, so a regression reports the real problem rather than the
    # downstream error message it produces.
    assert brought_up == [], (
        "a capability with no runner started a multi-GB bring-up before failing"
    )
    assert record["ok"] is False
    assert "Apple Silicon" in record["error"]
    assert record["runner"] is None


# -- no job row of our own -----------------------------------------------------
#
# **A benchmark deliberately OPENS no download-manager row.** Three rounds of
# trying produced three new defects, all of them the same root cause: server job
# rows are a TITLE-KEYED global namespace (`useCacheScan.ts` maps
# `job.title -> job`) with several consumers, and `supervisor.load` already owns
# the row titled exactly `model`. A prefixed title cannot be found by any
# consumer; the bare title SHADOWS the load row, which put the only visible ✕ on
# the load instead of the benchmark and left a cold run spinning to its hour-long
# timeout to record a phantom "did not finish loading in time".
#
# So the tab shows a plain in-tab spinner. These tests are the guard on that, and
# it is why they assert an ABSENCE. The one row a benchmark can INHERIT rather
# than open — the transcribe queue's — has its own section further down.


@pytest.fixture
def norows(tmp_path, monkeypatch):
    """`bench`, but with progress reporting left REAL and the job store empty, so
    a row created anywhere on the path is visible to the assertion."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    clock = Clock()
    monkeypatch.setattr(benchmark, "_now", clock)
    monkeypatch.setattr(benchmark.registry, "for_capability", lambda cap: FakeRunner)
    monkeypatch.setattr(benchmark.supervisor, "ready_worker",
                        lambda cap, model=None: object())
    monkeypatch.setattr(benchmark.supervisor, "describe", lambda: {"loaded": []})
    jobs.reset()
    yield clock
    jobs.reset()


def test_a_run_creates_no_job_row(norows, monkeypatch):
    record, _ = _text_run(norows, monkeypatch)
    assert record["ok"] is True
    assert jobs.list_jobs() == [], (
        "a benchmark opened a download-manager row; it must not — the title "
        "namespace is shared with supervisor.load and collides on the model id"
    )


def test_a_cold_run_creates_no_job_row_either(norows, monkeypatch):
    """The cold path is where the collision bit: `supervisor.load` opens the row
    titled `model`, and anything of ours sharing that namespace shadowed it."""
    states = {"ready": False}
    monkeypatch.setattr(benchmark.supervisor, "ready_worker",
                        lambda cap, model=None: object() if states["ready"] else None)

    def start_resident(model, capability):
        norows.advance(8.5)
        states["ready"] = True
        return {"jobId": "sys:ai-model:x", "model": model, "state": "loading"}, FakePending()

    monkeypatch.setattr(benchmark.supervisor, "_start_resident", start_resident)
    monkeypatch.setattr(benchmark, "_LOAD_POLL_S", 0.0)
    record, _ = _text_run(norows, monkeypatch)
    assert record["loadSeconds"] == pytest.approx(8.5)
    assert jobs.list_jobs() == []


def test_a_failed_run_creates_no_job_row(norows, monkeypatch):
    def generate_text(model, body):
        raise benchmark.supervisor.SupervisorError("out of memory")
        yield  # pragma: no cover

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    assert jobs.list_jobs() == []


def test_the_module_offers_no_job_row_machinery():
    """Named absences, so re-adding any of them is a deliberate act with a test
    to argue with rather than an innocent-looking convenience."""
    for gone in ("row_fields", "job_id", "BENCH_JOB_PREFIX", "_report",
                 "_WORKER_HONOURS_CANCEL"):
        assert not hasattr(benchmark, gone), gone


def test_run_takes_no_job_argument():
    """The signature is the enforcement: a caller cannot hand this a row to
    report on, so no caller can quietly reintroduce one."""
    import inspect
    assert list(inspect.signature(benchmark.run).parameters) == ["model", "capability"]


def test_a_generation_the_worker_needs_a_job_id_for_gets_a_private_one(norows,
                                                                      monkeypatch):
    """`generate_image`/`generate_transcript` take a job id structurally, and
    `worker_base.report`'s `job or JOB_ID` falls back to the model's own LOAD row
    when handed a falsy one — which would paint benchmark progress onto the
    load's bar. So the id is real, private, and names no row."""
    seen = []

    def generate_image(model, request, job):
        seen.append(job)
        norows.advance(1.0)
        return {"path": request["out"], "steps": request["steps"]}

    monkeypatch.setattr(benchmark.supervisor, "generate_image", generate_image)
    monkeypatch.setattr(benchmark, "_image_steps", lambda model: 4)
    benchmark.run("some/image-model", ai_registry.IMAGE_GENERATION)
    assert all(job for job in seen), "a falsy job id reaches the model's load row"
    # Never the model id, and never anything a page could write.
    assert all(job != "some/image-model" for job in seen)
    assert all(job.startswith("sys:") for job in seen)
    assert jobs.list_jobs() == []


# -- cancellation is not a measurement ------------------------------------------
#
# Kept after the row's removal, because it never depended on the row:
# `fused.ai.cancel()` from ANY page reaches the same worker through
# `supervisor.cancel_generation`, so a benchmark can be cut short by something
# that has nothing to do with this feature. What must not happen is recording the
# truncated work as a result.


def test_a_worker_that_reports_a_cancelled_generation_is_not_a_measurement(
        bench, monkeypatch):
    """The done frame carries `cancelled: true` with `ok: true` BESIDE it — the
    worker did what it was told — so reading only `ok` recorded a truncated token
    count as a measurement."""
    def generate_text(model, body):
        bench.advance(1.0)
        yield {"type": "chunk", "text": "x"}
        yield {"type": "done", "ok": True, "cancelled": True, "tokens": 3}

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    with pytest.raises(benchmark.Cancelled):
        benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert bench_store.read() == []


def test_a_stream_that_ends_with_no_done_frame_is_a_failure_not_a_success(
        bench, monkeypatch):
    """A worker killed or OOM-reaped mid-generation can close its side of the
    connection cleanly (no exception here — that path is `run()`'s own
    `except BaseException`, covered elsewhere) without ever sending the one
    frame that says how the generation went. The bug: `done` started as `{}`
    and `not done.get("ok", True)` defaults a MISSING frame to success, so a
    dead worker recorded `ok: true` with every metric null — the fastest
    possible measurement of nothing. A benchmark that measured nothing is a
    failed run, not a successful one with no numbers."""
    def generate_text(model, body):
        bench.advance(1.0)
        yield {"type": "chunk", "text": "x"}
        # The stream just... ends. No "done" frame, no exception.

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    assert record["error"]
    assert record["metrics"] == {}
    stored = bench_store.read()
    assert stored[-1]["id"] == record["id"]
    assert stored[-1]["ok"] is False


def test_a_cancelled_image_render_is_a_cancel_not_a_failed_model(bench, monkeypatch):
    """`generate_image`/`generate_transcript` say it with
    `SupervisorError("cancelled")` — the literal `start_image` switches on — so it
    has to arrive here as a cancel rather than as `ok:false`, which would record
    "this model failed on this laptop" for a run somebody else stopped."""
    def generate_image(model, request, job):
        raise benchmark.supervisor.SupervisorError("cancelled")

    monkeypatch.setattr(benchmark.supervisor, "generate_image", generate_image)
    monkeypatch.setattr(benchmark, "_image_steps", lambda model: 4)
    with pytest.raises(benchmark.Cancelled):
        benchmark.run("some/image-model", ai_registry.IMAGE_GENERATION)
    assert bench_store.read() == []


def test_an_interpreter_level_exit_propagates_and_is_not_recorded(bench,
                                                                 monkeypatch):
    """Ctrl-C on the dev server mid-benchmark must not write "this model failed on
    this laptop" — and must not be swallowed either."""
    def generate_text(model, body):
        raise KeyboardInterrupt
        yield  # pragma: no cover

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    with pytest.raises(KeyboardInterrupt):
        benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert bench_store.read() == []


def test_a_system_exit_propagates_too(bench, monkeypatch):
    def generate_text(model, body):
        raise SystemExit(1)
        yield  # pragma: no cover

    monkeypatch.setattr(benchmark.supervisor, "generate_text", generate_text)
    with pytest.raises(SystemExit):
        benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert bench_store.read() == []


# -- the load wait watches the LOAD, not just the clock -------------------------
#
# Removing the job row removed the cancel poll with it, and left `_load_to_ready`
# polling `ready_worker` alone. That is not enough, and the gap is not only about
# a ✕: when `_bring_up` fails OR is cancelled it sets `state="error"` on the
# pending record and DELETES it from `_workers`, so `ready_worker` returns None
# forever. The wait then ran to `_LOAD_TIMEOUT_S` — an hour — holding the HTTP
# request open, holding `_claim(capability)` so every other benchmark of that
# capability 409'd for the hour, and finally recording
# `ok:false, "<model> did not finish loading in time"`: a phantom "this model
# failed here" in permanent history, which is exactly what this design exists to
# prevent. So the wait polls the pending record the way `supervisor._wait_ready`
# does.


class FakePending:
    """The two fields `_load_to_ready` reads off a pending `Worker`, plus the
    identity `_workers` is checked against."""

    def __init__(self):
        self.state = "loading"
        self.error = ""
        self.detail = ""


@pytest.fixture
def loading(bench, monkeypatch):
    """A cold model whose bring-up never becomes ready, with a pending record the
    test can fail, cancel or evict. Returns `(clock, pending)`."""
    pending = FakePending()
    monkeypatch.setattr(benchmark.supervisor, "ready_worker",
                        lambda cap, model=None: None)
    monkeypatch.setattr(benchmark.supervisor, "_start_resident",
                        lambda model, capability: ({"jobId": "j"}, pending))
    # The table says this record still owns the capability unless a test says
    # otherwise — the eviction check reads it.
    monkeypatch.setattr(benchmark.supervisor, "_workers",
                        {ai_registry.TEXT_GENERATION: pending})
    monkeypatch.setattr(benchmark, "_LOAD_POLL_S", 0.0)
    # Generous, so a test that reaches the timeout has genuinely failed to notice
    # the state change rather than merely raced it.
    monkeypatch.setattr(benchmark, "_LOAD_TIMEOUT_S", 600.0)
    return bench, pending


def test_a_load_that_fails_is_recorded_as_the_real_failure(loading, monkeypatch):
    """Not "did not finish loading in time" an hour later — the loader's own
    sentence, immediately. This half was masked before the row's removal and is
    the more important one: a load fails far more often than it is cancelled."""
    clock, pending = loading

    def ready_worker(capability, model=None):
        clock.advance(1.0)
        # What `_bring_up`'s failure path leaves behind: state and error set, the
        # record dropped from the table.
        pending.state = "error"
        pending.error = "the model process is gone"
        benchmark.supervisor._workers.clear()
        return None

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    assert record["error"] == "the model process is gone"
    assert "did not finish loading in time" not in (record["error"] or "")
    # Recorded, because a load that genuinely fails IS a fact about this model on
    # this machine — unlike a cancel.
    assert [r["id"] for r in bench_store.read()] == [record["id"]]


def test_a_cancelled_load_records_nothing(loading, monkeypatch):
    """`_bring_up` reports a cancel as `state="error"` with the literal
    "cancelled" (`_failure_text`), so it arrives down the same channel as a real
    failure and has to be told apart from one — otherwise pressing the ✕ on the
    load row writes "this model failed on this laptop"."""
    clock, pending = loading

    def ready_worker(capability, model=None):
        clock.advance(1.0)
        pending.state = "error"
        pending.error = "cancelled"
        benchmark.supervisor._workers.clear()
        return None

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    with pytest.raises(benchmark.Cancelled):
        benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert bench_store.read() == []


def test_a_model_evicted_mid_load_says_so_rather_than_timing_out(loading,
                                                                monkeypatch):
    """Another model claimed the capability, or an unload landed. The record never
    errored, so there is no better answer than what happened to it — and it must
    not be an hour of silence either."""
    clock, pending = loading

    def ready_worker(capability, model=None):
        clock.advance(1.0)
        benchmark.supervisor._workers[ai_registry.TEXT_GENERATION] = FakePending()
        return None

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    assert "unloaded" in record["error"]


def test_the_timeout_still_ends_a_wait_that_reports_nothing(loading, monkeypatch):
    """The backstop survives: a bring-up that neither succeeds nor says it failed
    is still bounded."""
    clock, _pending = loading

    def ready_worker(capability, model=None):
        clock.advance(100.0)
        return None

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    assert "did not finish loading in time" in record["error"]


def test_a_warm_model_never_asks_the_supervisor_to_load(bench, monkeypatch):
    """The warm path must not have grown a bring-up: `_start_resident` EVICTS
    whatever holds the capability, so calling it for a model already resident
    would tear down the very worker about to be measured."""
    starts = []
    monkeypatch.setattr(benchmark.supervisor, "_start_resident",
                        lambda model, capability: starts.append(model))
    record, _ = _text_run(bench, monkeypatch)
    assert record["loadSeconds"] is None
    assert starts == []


# -- a row this benchmark did not open, and did not leave open ------------------
#
# The one place the "no job row" invariant is not ours to keep: a speech
# benchmark queued behind a real transcription goes through
# `supervisor._await_turn`, which reports `_transcribe_row(...)` — and that
# payload CARRIES A TITLE, so `jobs.upsert` accepts it and a row really is
# created under the private job id. Once it exists the worker's otherwise-refused
# titleless ticks start landing on it, and with every terminal report removed
# nothing ever closed it.
#
# These tests stand in for that path rather than driving the real transcribe lock
# (which needs a worker): the fake creates the row exactly as `_await_turn`
# would. The previous round's no-row tests all stubbed `generate_transcript`, so
# none of them could see any of this.


def test_a_row_inherited_from_the_transcribe_queue_is_closed(norows, monkeypatch):
    seen = {}

    def generate_transcript(model, request, job):
        # Exactly what `_await_turn` does when the transcribe lock is held.
        jobs.upsert({"id": job, "title": os.path.basename(request["path"]),
                     "state": "running", "kind": "task", "cancellable": True,
                     "unit": "s", "detail": "Queued behind another transcription…"},
                    server=True)
        seen["job"] = job
        seen["title"] = os.path.basename(request["path"])
        norows.advance(3.0)
        return {"text": "beep"}

    monkeypatch.setattr(benchmark.supervisor, "generate_transcript",
                        generate_transcript)
    benchmark.run("org/whisper", ai_registry.SPEECH_TO_TEXT)
    row = next((r for r in jobs.list_jobs() if r["id"] == seen["job"]), None)
    assert row is not None, "the simulated queue row vanished; test is not testing"
    assert row["state"] in ("done", "error", "cancelled"), (
        "a row inherited from the transcribe queue was left running forever"
    )


def test_the_temp_audio_is_named_so_an_inherited_row_reads_as_a_benchmark(
        bench, monkeypatch):
    """The title that row gets is the AUDIO FILE's basename, which is the only
    part of it this module controls. "benchmark.wav" could be a file the user
    dropped in; this cannot."""
    seen = []

    def generate_transcript(model, request, job):
        seen.append(os.path.basename(request["path"]))
        bench.advance(3.0)
        return {"text": "beep"}

    monkeypatch.setattr(benchmark.supervisor, "generate_transcript",
                        generate_transcript)
    benchmark.run("org/whisper", ai_registry.SPEECH_TO_TEXT)
    assert seen[-1].startswith("fused-benchmark-")
    assert seen[-1].endswith(".wav")


def test_a_text_run_creates_no_row_and_reaches_no_closer(norows, monkeypatch):
    """The ordinary path stays row-free. It does not reach `_close_any_row` at
    all — only the image and transcript paths do — which is why the property that
    the terminal report cannot CREATE a row is pinned on a transcript run
    instead, further down. This test used to claim that property and never call
    the function."""
    record, _ = _text_run(norows, monkeypatch)
    assert record["ok"] is True
    assert jobs.list_jobs() == []


# -- the inherited row's terminal state tells the truth -------------------------
#
# `_close_any_row` sent `state="done"` unconditionally from a bare `finally`, so
# on the exact path it exists for — a speech benchmark queued behind a real
# transcription — pressing the ✕ flipped that row to a green "done" with the
# queued detail still on it, and `jobs.upsert` cleared `cancel_requested` on the
# way (it treats `done` as terminal). A dead worker did the same: a row reporting
# success for a run recorded `ok:false`. Mirrors `supervisor.start_transcribe`'s
# own `run()`, which reports `cancelled`/`error`/`done` by outcome.


def _queue_row(job: str, path: str) -> None:
    """What `supervisor._await_turn` writes when the transcribe lock is held."""
    jobs.upsert({"id": job, "title": os.path.basename(path), "state": "running",
                 "kind": "task", "cancellable": True, "unit": "s",
                 "detail": "Queued behind another transcription…"}, server=True)


def test_a_cancelled_queued_transcription_closes_its_row_as_cancelled(
        norows, monkeypatch):
    seen = {}

    def generate_transcript(model, request, job):
        _queue_row(job, request["path"])
        seen["job"] = job
        raise benchmark.supervisor.SupervisorError("cancelled")

    monkeypatch.setattr(benchmark.supervisor, "generate_transcript",
                        generate_transcript)
    with pytest.raises(benchmark.Cancelled):
        benchmark.run("org/whisper", ai_registry.SPEECH_TO_TEXT)
    row = next(r for r in jobs.list_jobs() if r["id"] == seen["job"])
    assert row["state"] == "cancelled", (
        "the ✕ the user pressed was answered with a green 'done'"
    )


def test_a_failed_queued_transcription_closes_its_row_as_an_error(norows,
                                                                 monkeypatch):
    seen = {}

    def generate_transcript(model, request, job):
        _queue_row(job, request["path"])
        seen["job"] = job
        raise benchmark.supervisor.SupervisorError(
            "the transcription process did not answer")

    monkeypatch.setattr(benchmark.supervisor, "generate_transcript",
                        generate_transcript)
    record = benchmark.run("org/whisper", ai_registry.SPEECH_TO_TEXT)
    assert record["ok"] is False
    row = next(r for r in jobs.list_jobs() if r["id"] == seen["job"])
    assert row["state"] == "error"
    # The reason travels onto the row, so the manager does not just show a colour.
    assert "did not answer" in row["message"]


def test_a_successful_queued_transcription_still_closes_its_row_as_done(
        norows, monkeypatch):
    seen = {}

    def generate_transcript(model, request, job):
        _queue_row(job, request["path"])
        seen["job"] = job
        norows.advance(3.0)
        return {"text": "beep"}

    monkeypatch.setattr(benchmark.supervisor, "generate_transcript",
                        generate_transcript)
    benchmark.run("org/whisper", ai_registry.SPEECH_TO_TEXT)
    row = next(r for r in jobs.list_jobs() if r["id"] == seen["job"])
    assert row["state"] == "done"


def test_a_cancelled_image_render_closes_its_row_as_cancelled(norows, monkeypatch):
    """The image path inherits no row today, but it takes the same code — so the
    two long calls must not differ in a way somebody has to remember."""
    seen = {}

    def generate_image(model, request, job):
        _queue_row(job, request["out"])
        seen["job"] = job
        raise benchmark.supervisor.SupervisorError("cancelled")

    monkeypatch.setattr(benchmark.supervisor, "generate_image", generate_image)
    monkeypatch.setattr(benchmark, "_image_steps", lambda model: 4)
    with pytest.raises(benchmark.Cancelled):
        benchmark.run("some/image-model", ai_registry.IMAGE_GENERATION)
    assert next(r for r in jobs.list_jobs()
                if r["id"] == seen["job"])["state"] == "cancelled"


def test_closing_a_row_that_does_not_exist_creates_nothing_on_the_real_path(
        norows, monkeypatch):
    """Drives a TRANSCRIPT run, which is the only path that calls
    `_close_any_row` — the earlier version of this test drove a text run and so
    asserted an absence on a path that never called the function it documents.
    Titlelessness is the property: `jobs.upsert` refuses a first report with no
    title, so the report closes a row that exists and cannot bring one about."""
    def generate_transcript(model, request, job):
        norows.advance(3.0)
        return {"text": "beep"}

    monkeypatch.setattr(benchmark.supervisor, "generate_transcript",
                        generate_transcript)
    record = benchmark.run("org/whisper", ai_registry.SPEECH_TO_TEXT)
    assert record["ok"] is True
    assert jobs.list_jobs() == []


# -- an error state that has not been written yet -------------------------------


def test_a_half_written_error_is_waited_out_rather_than_generalised(loading,
                                                                   monkeypatch):
    """`_bring_up` sets `state` from its health poll OUTSIDE `_lock` and only
    then raises, so there is a window where a waiter sees `state == "error"` with
    `error` still empty. Reporting the generic "the model failed to load" there
    throws away the runner's own sentence — the exact information loss the
    record-polling change was made to fix. So an empty error is treated as
    not-yet-written and polled again."""
    clock, pending = loading
    # Read 1 is `_load_to_ready`'s own warm check, BEFORE the bring-up starts —
    # counted explicitly, because an off-by-one here silently moves the window
    # outside the poll loop and the test passes without ever entering it.
    reads = {"n": 0}

    def ready_worker(capability, model=None):
        reads["n"] += 1
        clock.advance(1.0)
        if reads["n"] == 2:
            # The window: `state` written by the health poll outside `_lock`,
            # `error` not yet written by the except path.
            pending.state = "error"
            pending.error = ""
        elif reads["n"] == 3:
            # `_bring_up`'s except path lands, writing both under the lock.
            pending.error = "mlx_lm could not read config.json"
            benchmark.supervisor._workers.clear()
        return None

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    assert record["error"] == "mlx_lm could not read config.json"
    # It really did pass through the half-written window rather than skipping it.
    assert reads["n"] >= 3


def test_an_error_that_is_never_written_still_ends_the_wait(loading, monkeypatch):
    """The grace is BOUNDED. A record stuck at `error` with nothing written is
    still answered — generically, which is all there is to say — rather than
    polled until the hour-long timeout."""
    clock, pending = loading
    reads = {"n": 0}

    def ready_worker(capability, model=None):
        reads["n"] += 1
        clock.advance(1.0)
        pending.state = "error"
        pending.error = ""
        return None

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION)
    assert record["ok"] is False
    assert record["error"] == "the model failed to load"
    # Ended on the GRACE, not on the 600s timeout the fixture sets: the poll
    # interval is 0, so a wait that ran to the deadline would show hundreds of
    # reads rather than a handful.
    assert reads["n"] < 10

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
import wave

import pytest

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
    # Progress reporting is best-effort plumbing, not this module's subject, and
    # a real `jobs.upsert` would leave rows behind for the next test to find.
    monkeypatch.setattr(benchmark.supervisor, "_report", lambda job, **f: None)
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
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION, "sys:job")
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
    record = benchmark.run("some/embed-model", ai_registry.EMBEDDINGS, "sys:job")
    metrics = record["metrics"]
    assert calls["n"] == 2
    assert metrics["batch"] == 8
    assert metrics["textsPerSecond"] == pytest.approx(4.0)  # 8 texts / 2.0s
    assert metrics["dim"] == 768


def test_an_embedding_reply_with_no_dim_leaves_dim_null(bench, monkeypatch):
    monkeypatch.setattr(benchmark.supervisor, "generate_embed",
                        lambda model, body: (bench.advance(1.0), {"vectors": []})[1])
    record = benchmark.run("some/embed-model", ai_registry.EMBEDDINGS, "sys:job")
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
    record = benchmark.run("some/image-model", ai_registry.IMAGE_GENERATION, "sys:job")
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
    record = benchmark.run("some/whisper", ai_registry.SPEECH_TO_TEXT, "sys:job")
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

    def load(model, capability, *, weights_only=False):
        bench.advance(8.5)
        states["ready"] = True
        return {"jobId": "sys:ai-model:x", "model": model, "state": "loading"}

    monkeypatch.setattr(benchmark.supervisor, "ready_worker", ready_worker)
    monkeypatch.setattr(benchmark.supervisor, "load", load)
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
    monkeypatch.setattr(benchmark.supervisor, "load",
                        lambda model, capability, **kw: {"jobId": "j"})
    monkeypatch.setattr(benchmark, "_LOAD_POLL_S", 0.0)
    monkeypatch.setattr(benchmark, "_LOAD_TIMEOUT_S", 0.0)
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION, "sys:job")
    assert record["ok"] is False
    assert "load" in record["error"].lower()


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
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION, "sys:job")
    assert record["ok"] is False
    assert record["error"] == "the model process is gone"
    assert record["metrics"] == {}
    assert [r["id"] for r in bench_store.read()] == [record["id"]]


def test_an_unknown_capability_raises_rather_than_recording_nothing(bench):
    """The router turns this into a 4xx. It is not an `ok:false` run: there is
    no workload, so nothing was measured and there is nothing to record."""
    with pytest.raises(ValueError):
        benchmark.run("some/model", "telepathy", "sys:job")


def test_a_capability_with_no_runner_here_fails_without_loading(bench, monkeypatch):
    """Forced, not inherited: on a Linux CI box `text-to-image` may genuinely
    have no runner and on this Mac it does, so the resolution is stubbed to
    `None` and the assertion is about what `run()` does with that."""
    monkeypatch.setattr(benchmark.registry, "for_capability", lambda cap: None)
    monkeypatch.setattr(
        benchmark.registry, "unavailable_reason",
        lambda cap: "needs Apple Silicon (this is linux/x86_64)")
    loads = []
    monkeypatch.setattr(benchmark.supervisor, "load",
                        lambda *a, **k: loads.append(a))
    record = benchmark.run("some/text-model", ai_registry.TEXT_GENERATION, "sys:job")
    assert record["ok"] is False
    assert "Apple Silicon" in record["error"]
    assert record["runner"] is None
    assert loads == []

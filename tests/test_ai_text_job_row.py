"""`_local_relay`'s Activity row for LOCAL text generation (this branch's own
addition): image, video and transcription already open a download-manager
row for the work they do; a completion from a resident text model never did,
so a 5-second local generation showed nothing anywhere in the shell.

Driven the way `test_ai_metrics.py` already drives the local tier —
`supervisor.generate_text` replaced with a plain iterator/generator of the
NDJSON-shaped dicts the worker would have sent — with `supervisor._report`
spied on (while still calling through to the real one, `test_ai_wait_ready_
row.py`'s own harness) so a tick's kwargs can be inspected directly.
"""
import asyncio
import gc
import threading
import time

import pytest

from fused_render import jobs
from fused_render.ai import supervisor
from fused_render.server import ai as _server_ai


@pytest.fixture(autouse=True)
def _clean_jobs():
    jobs.reset()
    yield
    jobs.reset()


def _spy(monkeypatch):
    """Capture every `_report` call (job, fields) in order, while still
    driving the real registry underneath — the same technique test_ai_wait_
    ready_row.py uses to inspect ticks without reconstructing them from
    `jobs.list_jobs()` timing."""
    reports = []
    real_report = supervisor._report

    def spy_report(job, **fields):
        reports.append((job, dict(fields)))
        real_report(job, **fields)

    monkeypatch.setattr(supervisor, "_report", spy_report)
    return reports


def _text_rows(reports):
    return [fields for job, fields in reports if job.startswith(supervisor.TEXT_JOB_PREFIX)]


def _local(monkeypatch, events):
    monkeypatch.setattr(supervisor, "generate_text", lambda model, request: iter(events))


# --------------------------------------------------------------- the row appears


def test_a_warm_model_completion_opens_a_row_and_reaches_done(monkeypatch):
    reports = _spy(monkeypatch)
    _local(monkeypatch, [
        {"type": "chunk", "text": "hi"},
        {"type": "chunk", "text": " there"},
        {"type": "done", "ok": True, "tokens": 2, "seconds": 0.1},
    ])

    resp = _server_ai._local_relay(
        "org/chat", "tell me a joke", "", False, {"prompt": "tell me a joke"})
    assert resp.status_code == 200

    ticks = _text_rows(reports)
    assert ticks, "a warm-model completion must open its own Activity row"
    assert ticks[0]["state"] == "running"
    assert ticks[-1]["state"] == "done"
    assert ticks[-1]["done"] == 2

    # The row is drawn from the registry too, not only from the kwargs a spy saw.
    rows = [j for j in jobs.list_jobs() if j["id"].startswith(supervisor.TEXT_JOB_PREFIX)]
    assert len(rows) == 1
    assert rows[0]["state"] == "done"


def test_the_title_is_the_prompts_first_line_not_the_model(monkeypatch):
    reports = _spy(monkeypatch)
    _local(monkeypatch, [{"type": "done", "ok": True, "tokens": 0}])

    prompt = "Summarize this document\nwith a lot more context below"
    _server_ai._local_relay("org/chat", prompt, "", False, {"prompt": prompt})

    ticks = _text_rows(reports)
    assert ticks[0]["title"] == "Summarize this document"
    assert ticks[0]["model"] == "org/chat"


# --------------------------------------------------------------- cold model: no row


def test_a_cold_model_opens_no_text_row_only_the_load_row(monkeypatch):
    """AI-5's fail-fast contract, unchanged: a model that is not resident
    answers 409 with the LOAD's job id, and the caller is meant to watch
    THAT row — a second row for the same wait is exactly the doubling
    `_wait_ready`'s merge exists to remove elsewhere."""
    reports = _spy(monkeypatch)

    def cold(model, request):
        raise supervisor.ModelNotReady("org/chat is loading now", "sys:ai-model:orgchat")
        yield  # pragma: no cover - generator function, never reached

    monkeypatch.setattr(supervisor, "generate_text", cold)

    resp = _server_ai._local_relay("org/chat", "hi", "", False, {"prompt": "hi"})
    assert resp.status_code == 409

    assert _text_rows(reports) == []
    assert [j for j in jobs.list_jobs() if j["id"].startswith(supervisor.TEXT_JOB_PREFIX)] == []


def test_a_worker_that_never_answers_also_opens_no_text_row(monkeypatch):
    """`generate_text` can also fail before yielding anything at all (the
    worker process not answering) — still no generation ever began, so still
    no row, the same rule as the cold-load case."""
    reports = _spy(monkeypatch)

    def broken(model, request):
        raise supervisor.SupervisorError("the model process did not answer: boom")
        yield  # pragma: no cover

    monkeypatch.setattr(supervisor, "generate_text", broken)

    resp = _server_ai._local_relay("org/chat", "hi", "", False, {"prompt": "hi"})
    assert resp.status_code == 502
    assert _text_rows(reports) == []


# --------------------------------------------------------------- identity on every tick


def test_every_tick_including_the_terminal_one_restates_the_rows_identity(monkeypatch):
    """A row can be REBUILT from scratch on any tick (`jobs._sweep` evicts
    the least recently updated running row once MAX_JOBS bites) — so a tick
    that omitted `title`/`cancellable`/`unit` would recreate a row missing
    them rather than update the one already showing. Pinned here the same
    way `test_ai_wait_ready_row.py` pins `transcribe_row_fields`'s rule."""
    reports = _spy(monkeypatch)
    _local(monkeypatch, [{"type": "chunk", "text": "hi"},
                         {"type": "done", "ok": True, "tokens": 1}])

    _server_ai._local_relay("org/chat", "hi", "", False, {"prompt": "hi"})

    ticks = _text_rows(reports)
    assert len(ticks) >= 2
    for fields in ticks:
        assert fields["title"] == "hi"
        assert fields["model"] == "org/chat"
        assert fields["kind"] == "task"
        assert fields["cancellable"] is True
        assert fields["unit"] == "tokens"


def test_a_failed_generation_reports_error_with_full_identity(monkeypatch):
    reports = _spy(monkeypatch)
    _local(monkeypatch, [{"type": "done", "ok": False, "error": "the model exploded"}])

    resp = _server_ai._local_relay("org/chat", "hi", "", False, {"prompt": "hi"})
    assert resp.status_code == 502

    ticks = _text_rows(reports)
    assert ticks[-1]["state"] == "error"
    assert ticks[-1]["message"] == "the model exploded"
    assert ticks[-1]["title"] == "hi"


# --------------------------------------------------------------- cancellation


def test_a_pressed_x_stops_the_generation_and_ends_the_row(monkeypatch):
    reports = _spy(monkeypatch)
    forwarded = {"n": 0}
    monkeypatch.setattr(supervisor, "cancel_generation", lambda: forwarded.__setitem__(
        "n", forwarded["n"] + 1) or True)

    def events(model, request):
        yield {"type": "chunk", "text": "a"}
        # By the time this chunk is consumed, the ✕ has been pressed —
        # `_local_relay`'s per-chunk poll must forward it before pulling the
        # worker's own cancelled `done` frame.
        job = next(j for j, _ in reports if j.startswith(supervisor.TEXT_JOB_PREFIX))
        jobs.request_cancel(job)
        yield {"type": "chunk", "text": "b"}
        yield {"type": "done", "ok": True, "cancelled": True, "tokens": 2}

    monkeypatch.setattr(supervisor, "generate_text", events)

    resp = _server_ai._local_relay("org/chat", "hi", "", False, {"prompt": "hi"})
    assert resp.status_code == 200

    assert forwarded["n"] >= 1, "the ✕ must reach supervisor.cancel_generation"
    ticks = _text_rows(reports)
    assert ticks[-1]["state"] == "cancelled"

    rows = [j for j in jobs.list_jobs() if j["id"].startswith(supervisor.TEXT_JOB_PREFIX)]
    assert rows[0]["state"] == "cancelled"


def test_an_unpressed_row_never_calls_cancel_generation(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(supervisor, "cancel_generation",
                        lambda: calls.__setitem__("n", calls["n"] + 1) or True)
    _local(monkeypatch, [{"type": "chunk", "text": "hi"},
                         {"type": "done", "ok": True, "tokens": 1}])

    _server_ai._local_relay("org/chat", "hi", "", False, {"prompt": "hi"})
    assert calls["n"] == 0


# --------------------------------------------------------------- client abort


def test_an_abandoned_stream_still_finalises_the_row(monkeypatch):
    """The page closes the connection mid-generation: Starlette abandons the
    NDJSON generator. `fused_ai.py`'s own reader names the mechanism this
    pins — `for c in stream(p): break` throws `GeneratorExit` into the sync
    generator via `.close()` once nothing else references it — and the row
    must not be left `running` forever when that happens."""
    reports = _spy(monkeypatch)

    def events(model, request):
        yield {"type": "chunk", "text": "a"}
        yield {"type": "chunk", "text": "b"}
        yield {"type": "done", "ok": True, "tokens": 2}

    monkeypatch.setattr(supervisor, "generate_text", events)

    resp = _server_ai._local_relay(
        "org/chat", "hi", "", True, {"prompt": "hi", "stream": True})

    async def go():
        # StreamingResponse wraps a sync generator through
        # `iterate_in_threadpool`, so this is the same shape
        # `test_ai_metrics.py`'s streamed-completion tests already drain —
        # `break` after the first frame is the abandonment.
        async for _ in resp.body_iterator:
            break

    asyncio.run(go())
    gc.collect()  # deterministic on CPython refcounting, but no reason to rely on timing

    ticks = _text_rows(reports)
    assert ticks, "at least the opening tick must have landed"
    assert ticks[-1]["state"] in ("error", "done", "cancelled")
    assert ticks[-1]["state"] != "running"


# --------------------------------------------------------------- prefill phase


def test_the_row_names_a_prefill_phase_before_any_chunk(monkeypatch):
    reports = _spy(monkeypatch)
    _local(monkeypatch, [
        {"type": "prefill", "input_tokens": 512},
        {"type": "chunk", "text": "hi"},
        {"type": "done", "ok": True, "tokens": 1},
    ])

    _server_ai._local_relay("org/chat", "hi", "", False, {"prompt": "hi"})

    ticks = _text_rows(reports)
    assert "512" in ticks[0]["detail"]
    assert "prompt" in ticks[0]["detail"].lower()
    # And the second tick (the first chunk) flips off the prefill wording.
    running = [t for t in ticks if t["state"] == "running"]
    assert any("generating" in t["detail"].lower() for t in running)


def test_the_prefill_detail_reads_correctly_with_no_token_count(monkeypatch):
    """`input_tokens` is `None` on the image path by design
    (`mlx_text/worker.py`'s own comment on `_prompt_tokens`) — the sentence
    must not print the literal word "None"."""
    reports = _spy(monkeypatch)
    _local(monkeypatch, [
        {"type": "prefill", "input_tokens": None},
        {"type": "done", "ok": True, "tokens": 0},
    ])

    _server_ai._local_relay("org/chat", "hi", "", False, {"prompt": "hi"})

    ticks = _text_rows(reports)
    assert "None" not in ticks[0]["detail"]


def test_a_long_prefill_is_kept_out_of_stalled_by_the_watchdog(monkeypatch):
    """`jobs.STALE_AFTER_S` is 30s; the watchdog restates the row well under
    that while `stream_generate` is silently doing the prefill forward pass.
    The interval is shrunk for the test rather than sleeping through 30s of
    real time."""
    monkeypatch.setattr(_server_ai, "_TEXT_WATCHDOG_TICK_S", 0.02)
    reports = _spy(monkeypatch)

    release = threading.Event()
    started = threading.Event()

    def events(model, request):
        yield {"type": "prefill", "input_tokens": 10}
        started.set()
        release.wait(2.0)
        yield {"type": "chunk", "text": "hi"}
        yield {"type": "done", "ok": True, "tokens": 1}

    monkeypatch.setattr(supervisor, "generate_text", events)

    result = {}

    def run():
        result["resp"] = _server_ai._local_relay(
            "org/chat", "hi", "", False, {"prompt": "hi"})

    t = threading.Thread(target=run)
    t.start()
    assert started.wait(2.0)
    # Long enough, at a 20ms tick, for several watchdog ticks to have landed
    # while the "worker" is still silently prefilling.
    time.sleep(0.2)
    ticks_during_prefill = len(_text_rows(reports))
    release.set()
    t.join(2.0)

    assert ticks_during_prefill >= 3, (
        "the watchdog must keep restating the row while prefill is silent")
    for fields in _text_rows(reports)[:ticks_during_prefill]:
        assert fields["state"] == "running"
    assert result["resp"].status_code == 200


def test_the_watchdog_never_ticks_after_the_terminal_report(monkeypatch):
    monkeypatch.setattr(_server_ai, "_TEXT_WATCHDOG_TICK_S", 0.02)
    reports = _spy(monkeypatch)

    def events(model, request):
        yield {"type": "prefill", "input_tokens": 5}
        yield {"type": "chunk", "text": "hi"}
        yield {"type": "done", "ok": True, "tokens": 1}

    monkeypatch.setattr(supervisor, "generate_text", events)

    _server_ai._local_relay("org/chat", "hi", "", False, {"prompt": "hi"})
    # The generation itself is instantaneous, but give any wayward watchdog
    # thread several tick intervals' worth of real time to prove it is gone.
    time.sleep(0.2)

    ticks = _text_rows(reports)
    terminal_index = next(i for i, t in enumerate(ticks) if t["state"] != "running")
    assert ticks[terminal_index + 1:] == [], (
        "no tick may land after the row reached a terminal state")


def test_the_cross_is_polled_during_prefill_not_only_between_chunks(monkeypatch):
    """A ✕ pressed while the prompt is still being READ has to stop the
    generation then, not a minute later when the first token arrives.

    The row is `cancellable=True` from its opening tick, so the cross is on
    screen for the whole prefill — and prefill is the one phase with no
    chunk boundaries to check on, which is exactly why the watchdog polls
    the cancel flag as well as restating the row. The failure this guards
    is a cross that draws but does nothing: pressing stop on a long-context
    prompt used to queue the cancel behind the model's first token.
    """
    monkeypatch.setattr(_server_ai, "_TEXT_WATCHDOG_TICK_S", 0.02)
    reports = _spy(monkeypatch)

    cancelled = threading.Event()
    monkeypatch.setattr(supervisor, "cancel_generation",
                        lambda *a, **k: cancelled.set() or True)

    prefilling = threading.Event()
    release = threading.Event()

    def events(model, request):
        yield {"type": "prefill", "input_tokens": 4096}
        prefilling.set()
        # The worker is silently doing its forward pass over the prompt: no
        # chunk has been produced, so nothing in the event loop below can
        # notice a cancel on its own.
        release.wait(2.0)
        yield {"type": "done", "ok": True, "cancelled": True, "tokens": 0}

    monkeypatch.setattr(supervisor, "generate_text", events)

    def run():
        _server_ai._local_relay("org/chat", "long prompt", "", False, {"prompt": "x"})

    t = threading.Thread(target=run)
    t.start()
    assert prefilling.wait(2.0)

    # The ✕: exactly what the manager's cross writes onto the row.
    job = [j for j, _ in reports if j.startswith(supervisor.TEXT_JOB_PREFIX)][0]
    jobs.request_cancel(job)

    assert cancelled.wait(2.0), \
        "the watchdog must forward a cancel pressed during prefill"
    release.set()
    t.join(2.0)

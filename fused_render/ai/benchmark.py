"""The fixed per-capability workloads a benchmark run executes (SPEC AI-14).

A benchmark answers "how fast is this model, on this machine, in this app
version" — and the only thing that makes two such answers comparable is that
the WORK was identical. So the work is not a parameter: there is exactly one
workload per capability, declared here, frozen, and named on every run record.

**Passive telemetry cannot do this job, which is why this exists beside
`server/ai_metrics.py` rather than inside it.** That module records the real
generations that happen to pass through — different prompts, different lengths,
different settings — so its tokens/second is a fair summary of a session and a
meaningless comparison between two models. The cost of the trade taken here is
that a number only exists where somebody pressed a button.

**`revision` is a comparability seam, and bumping it is a breaking change on
purpose.** If a prompt, a token budget or an image size ever changes, runs
recorded before the change are not comparable with runs after it. Every run
stores the revision it was measured under, which lets the page refuse to draw a
delta across the seam instead of quietly reporting a fake regression. Change a
`params` value and you MUST bump the `revision` beside it.

**Image generation deliberately does not fix the step count.** A shared step
count is either unfair to a step-distilled model or an out-of-memory on the
others — `catalog.py`'s per-model `defaults: {"steps": 4}` exists precisely
because FLUX.2 klein runs at 4. So the workload fixes the prompt and the
canvas, each model contributes its own catalog default step count, and
comparability is recovered by making SECONDS PER STEP the primary metric with
the step count recorded on the run.

**Speech to text synthesizes its own audio.** Realtime factor is a decode
throughput measure and does not need intelligible speech, and generating a tone
with the stdlib `wave` module avoids committing a binary asset to the repo for
one benchmark. The risk, stated rather than hidden: a model with
speech-dependent early-exit behaviour could look faster on a tone than on real
audio.

`machine()` records why a number is not portable. It is stdlib only — no
`psutil`, which fused-render's venv does not carry and which would make a
second-order field a new dependency — so a figure this platform will not report
comes back `None`. That is the module's standing rule and it applies to every
metric downstream too: **a value that was not measured is `null`, never zero and
never derived.** A zero reads as a real measurement of nothing; a `None` reads
as "this runner does not say", which is the truth.
"""
from __future__ import annotations

import logging
import math
import os
import platform
import secrets
import struct
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import fused_render
from fused_render import jobs
from fused_render.ai import bench_store, catalog, registry, supervisor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Workload:
    """One capability's fixed unit of work: a name, a revision, frozen params.

    `params` is a `MappingProxyType` rather than a plain dict because a
    workload a caller can mutate at runtime is not a fixed workload — and the
    accident it prevents (a measurement function stashing a per-run value into
    the shared table) would silently invalidate every comparison made after it.
    """

    name: str
    revision: int
    params: Mapping[str, object]

    def as_dict(self) -> dict:
        """The `workload` block as it is stored on a run record."""
        return {
            "name": self.name,
            "revision": self.revision,
            "params": dict(self.params),
        }


#: The fixed prompt every text benchmark decodes from. Short enough that the
#: prefill is not the measurement, concrete enough that no model can answer it
#: in two tokens and stop early — an early stop would end the timed window
#: before the decode rate stabilised and report a suspiciously fast model.
_TEXT_PROMPT = (
    "Write a short paragraph explaining what a file explorer does, "
    "in plain language, for somebody who has never used one."
)

#: The fixed texts an embeddings benchmark encodes. Eight of them, of differing
#: lengths: a single string measures per-call overhead rather than throughput,
#: and a batch of identical strings lets a runner that caches look faster than
#: it is.
_EMBED_TEXTS = (
    "a photo of a golden retriever on a beach",
    "quarterly revenue grew by eleven percent",
    "how do I mount a remote bucket as a local folder",
    "the mitochondrion is the powerhouse of the cell",
    "rain",
    "a parquet file with two hundred million rows of taxi trips",
    "she closed the laptop and walked out into the snow",
    "SELECT count(*) FROM read_parquet('s3://bucket/*.parquet')",
)

#: One entry per capability constant in `registry`. A capability with no entry
#: would render a Run button that measures nothing defined, so
#: `test_ai_benchmark_store.py` pins this table against `registry.capabilities()`
#: in BOTH directions — a fifth capability cannot be added without a workload,
#: and a workload cannot name a capability that does not exist.
WORKLOADS: Mapping[str, Workload] = MappingProxyType({
    registry.TEXT_GENERATION: Workload(
        name="text-128-tokens",
        revision=1,
        params=MappingProxyType({
            "prompt": _TEXT_PROMPT,
            # Enough decode to average out the first few tokens' warm-up
            # without turning one benchmark into a minute on a CPU-only box.
            "maxTokens": 128,
            # Greedy, so two runs of the same model do the same amount of work
            # and a slow sampler cannot masquerade as a slow model.
            "temperature": 0.0,
        }),
    ),
    registry.IMAGE_GENERATION: Workload(
        name="image-512-catalog-steps",
        revision=1,
        params=MappingProxyType({
            "prompt": "a lighthouse on a rocky coast at dawn, photograph",
            # Small on purpose: the metric is seconds per step, and 512² keeps
            # a benchmark to a minute or two on hardware where 1024² is ten.
            "width": 512,
            "height": 512,
            # `steps` is absent BY DESIGN — see the module docstring. Each
            # model contributes its catalog default and the run records it.
            "seed": 0,
            # Fixed here rather than left to the router's default, because it
            # changes how much work a step is on a classifier-free-guidance
            # pipeline: two models compared at different guidance are not
            # compared at all. The value is `/api/ai/image`'s own default, so a
            # benchmark measures the pipeline a page actually gets.
            "guidance": 4.0,
        }),
    ),
    registry.SPEECH_TO_TEXT: Workload(
        name="speech-30s-tone",
        revision=1,
        params=MappingProxyType({
            # 30s is Whisper's own window: one pass, no chunking policy in the
            # measurement, and a realtime factor that reads as "this many times
            # faster than listening to it".
            "audioSeconds": 30.0,
            "sampleRate": 16000,
            "toneHz": 440.0,
        }),
    ),
    registry.EMBEDDINGS: Workload(
        name="embed-8-texts",
        revision=1,
        params=MappingProxyType({
            "texts": _EMBED_TEXTS,
            "batch": len(_EMBED_TEXTS),
        }),
    ),
})


def _total_memory_bytes() -> int | None:
    """Physical RAM, or `None` where the stdlib will not say.

    `os.sysconf` covers macOS and Linux; Windows has no stdlib equivalent that
    does not go through `ctypes`, so it reports `None` rather than a guess —
    the null-over-estimate rule applies to the machine block too.
    """
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None


def machine() -> dict:
    """This host, as recorded on every run: why a number is not portable.

    Stored per run rather than once per file because the file outlives the
    machine — a home directory gets restored onto a new laptop, and a run whose
    machine is implicit becomes a number with no meaning at that moment.
    """
    return {
        "platform": platform.system() or platform.platform(),
        "arch": platform.machine(),
        "cpuCount": os.cpu_count(),
        "totalMemoryBytes": _total_memory_bytes(),
    }


# -- the run ---------------------------------------------------------------------

#: Steps for an image model the catalog has no per-model hint for — the same 28
#: `POST /api/ai/image` defaults to, so a benchmark measures the pipeline a page
#: actually gets rather than a number invented here.
DEFAULT_IMAGE_STEPS = 28

#: How many further polls to allow a pending record that says `error` with no
#: `error` message yet. `_bring_up` writes `state` from its health poll OUTSIDE
#: `supervisor._lock` and only then raises into the handler that writes the
#: message, so there is a real window where a waiter can read the state and not
#: the reason. Reporting the generic sentence there would throw away the runner's
#: own — the very information loss polling the record was meant to recover — and
#: waiting is enough, because the raise is the next thing that happens on that
#: thread. Bounded, so a record genuinely stuck at `error` with nothing written is
#: still answered in a few poll intervals rather than at the hour-long timeout.
_ERROR_GRACE_POLLS = 4

#: How long to wait for a cold model, and how often to ask. The wait matches the
#: supervisor's own (`LOAD_WAIT_TIMEOUT_S`, an hour) because it is the same wait
#: — a first-ever bring-up is a `uv sync` followed by a multi-GB pull — and a
#: benchmark that gave up sooner would report a load failure for a download that
#: was going to finish. Module-level so a test can shrink both.
_LOAD_TIMEOUT_S = supervisor.LOAD_WAIT_TIMEOUT_S
_LOAD_POLL_S = 0.5

#: Indirection so tests can script a timeline instead of sleeping through one.
#: `time.monotonic`, not `time.time`: a clock the user can drag backwards would
#: report a negative throughput. `startedAt` on the record is wall clock, which
#: is a different question (when was this taken) with a different right answer.
_now = time.monotonic


def _image_steps(model: str) -> int:
    """The step count this image model runs at: its catalog hint, else the
    server default.

    Asked of the catalog rather than decided here, because that is where the
    per-model hint already lives (`catalog.py`'s `defaults: {"steps": 4}` for
    step-distilled FLUX.2 klein) and the Playground reads the same one. A second
    copy of the rule is how a benchmark comes to run a model at a step count
    nothing else uses.
    """
    for entry in catalog.for_capability(registry.IMAGE_GENERATION):
        if entry.get("id") == model:
            steps = (entry.get("defaults") or {}).get("steps")
            if isinstance(steps, int) and steps > 0:
                return steps
            return DEFAULT_IMAGE_STEPS
    return DEFAULT_IMAGE_STEPS


def _write_tone_wav(path: str, seconds: float, sample_rate: int, hz: float) -> None:
    """Write `seconds` of a mono sine tone at `hz` as 16-bit PCM.

    Stdlib only, so no binary fixture is committed for one benchmark, and the
    file is regenerated per run rather than cached — writing 30s of 16 kHz mono
    is under a megabyte and a millisecond, which is far cheaper than owning a
    cache-invalidation rule.

    Amplitude is deliberately well below full scale: a clipped, square-ish wave
    is a different signal from a tone, and some front ends normalise loudly.
    """
    frames = int(seconds * sample_rate)
    step = 2.0 * math.pi * hz / sample_rate
    samples = struct.pack(
        f"<{frames}h",
        *(int(12000 * math.sin(step * i)) for i in range(frames)),
    )
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(samples)


def _measure_text(model: str, workload: Workload, *, timed: bool) -> dict:
    """Decode the fixed prompt, splitting prefill from decode.

    Two numbers, because one would hide the other: `ttftMs` is everything up to
    the first output token (the prompt read, the graph built, the cache
    allocated) and `tokensPerSecond` is the steady-state decode that follows. A
    single tokens-over-total-seconds figure makes a fast decoder with a slow
    prefill indistinguishable from the reverse, and those two models feel
    completely different to use.

    The prompt goes on the worker's RAW `prompt` field rather than as a
    `messages` turn: a chat template is per-model, so templating would send each
    model a different number of prefill tokens and quietly break the one thing
    the fixed workload exists to guarantee.
    """
    params = workload.params
    body = {
        "prompt": params["prompt"],
        "max_tokens": params["maxTokens"],
        "temperature": params["temperature"],
    }
    start = _now()
    first_token_at = None
    done: dict = {}
    for event in supervisor.generate_text(model, body):
        kind = event.get("type")
        if kind == "chunk" and first_token_at is None:
            first_token_at = _now()
        elif kind == "done":
            done = event
    end = _now()
    if done.get("cancelled"):
        # `ok` is TRUE on this frame — the worker did what it was told — so
        # reading only `ok` recorded a truncated generation's token count as a
        # measurement. And this arrives without anyone pressing THIS row's ✕:
        # `fused.ai.cancel()` from any page reaches the same worker through
        # `supervisor.cancel_generation`.
        raise Cancelled()
    if not done.get("ok", True):
        raise supervisor.SupervisorError(
            str(done.get("error") or "the generation failed"))
    if not timed:
        return {}

    # `tokens`/`input_tokens` are the WORKER's counts (AI-3). A worker that does
    # not report them leaves the derived rates null: counting the tokens off the
    # returned text here would be a different tokenizer's answer wearing this
    # model's label.
    output_tokens = done.get("tokens")
    prompt_tokens = done.get("input_tokens")
    ttft_s = None if first_token_at is None else first_token_at - start
    decode_s = None if first_token_at is None else end - first_token_at
    return {
        "tokensPerSecond": (
            # `- 1`: the first token was produced during the TTFT window above
            # and is charged there, so charging it here too would inflate the
            # decode rate of a short generation.
            (output_tokens - 1) / decode_s
            if isinstance(output_tokens, int) and output_tokens > 1
            and decode_s and decode_s > 0 else None
        ),
        "ttftMs": None if ttft_s is None else ttft_s * 1000.0,
        "promptTokensPerSecond": (
            prompt_tokens / ttft_s
            if isinstance(prompt_tokens, int) and ttft_s and ttft_s > 0 else None
        ),
        "outputTokens": output_tokens if isinstance(output_tokens, int) else None,
    }


def _measure_embed(model: str, workload: Workload, *, timed: bool) -> dict:
    """Encode the fixed batch of texts in one call.

    The whole batch in one request rather than eight requests, because batching
    is what an embedding backend is FOR — a per-call figure would measure this
    app's HTTP hop as much as the model.
    """
    texts = list(workload.params["texts"])
    start = _now()
    result = supervisor.generate_embed(model, {"texts": texts})
    total = _now() - start
    if not timed:
        return {}
    dim = (result or {}).get("dim")
    return {
        "textsPerSecond": len(texts) / total if total > 0 else None,
        "dim": dim if isinstance(dim, int) else None,
        "batch": len(texts),
    }


def _measure_image(model: str, workload: Workload, *, timed: bool) -> dict:
    """Render one image at the fixed canvas and this model's own step count.

    The PNG goes to a temp file that is deleted on the way out: a benchmark is a
    measurement, not a picture somebody asked for, and putting it in the user's
    images folder would leave the Playground's gallery full of lighthouses.
    """
    params = workload.params
    steps = _image_steps(model)
    with tempfile.TemporaryDirectory(prefix="fused-bench-") as tmp:
        request = {
            "prompt": params["prompt"],
            "width": params["width"],
            "height": params["height"],
            "steps": steps,
            "guidance": params["guidance"],
            "seed": params["seed"],
            "out": os.path.join(tmp, "benchmark.png"),
        }
        job = _unwatched_job()
        start = _now()
        try:
            supervisor.generate_image(model, request, job)
        except BaseException as e:
            # The image path has no queue and so inherits no row today; closing
            # anyway costs one swallowed `JobError` and means the two long calls
            # do not differ in a way somebody has to remember.
            _close_any_row(job, e)
            raise
        else:
            _close_any_row(job)
        total = _now() - start
    if not timed:
        return {}
    return {
        "secondsPerStep": total / steps if steps > 0 else None,
        "totalSeconds": total,
        "steps": steps,
        "width": params["width"],
        "height": params["height"],
    }


def _measure_transcript(model: str, workload: Workload, *,
                        timed: bool) -> dict:
    """Decode the synthesized tone and report how many times faster than
    realtime it was.

    `audioSeconds` comes from the WORKLOAD, not from the transcript's own
    `duration`: a fixed workload whose length is reported by the thing being
    measured is not fixed, and a runner that rounds or trims would move the
    denominator under the metric.

    The VAD is off, and so are diarization and word timings. All three are
    optional passes that change what the decode IS, and a model benchmarked with
    speech detection on a tone would be measured on how much of it it decided to
    skip.
    """
    params = workload.params
    seconds = float(params["audioSeconds"])
    with tempfile.TemporaryDirectory(prefix="fused-bench-") as tmp:
        # Named so that a row INHERITED from the transcribe queue reads as ours:
        # `supervisor._transcribe_title` titles that row with this basename, and
        # "benchmark.wav" is a name a user could plausibly have dropped in.
        # `_close_any_row` closes it; this makes it legible while it is open.
        audio = os.path.join(tmp, "fused-benchmark-tone.wav")
        _write_tone_wav(audio, seconds, int(params["sampleRate"]),
                        float(params["toneHz"]))
        request = {
            "path": audio,
            "model": model,
            "language": None,
            "task": "transcribe",
            "vad": False,
            "diarize": False,
            "words": False,
            "out": os.path.join(tmp, "fused-benchmark-tone.json"),
            "outText": os.path.join(tmp, "fused-benchmark-tone.txt"),
            # No `row`: that key is how a caller gives the worker a progress row
            # to restate its identity onto, and a benchmark has none by design
            # (see `run`). Without it the worker's ticks carry no title,
            # `jobs.upsert` refuses them and `worker_base.report` swallows the
            # refusal — so the decode runs and no row is created, which is
            # exactly what is wanted here.
        }
        job = _unwatched_job()
        start = _now()
        try:
            supervisor.generate_transcript(model, request, job)
        except BaseException as e:
            # Both arms close the row, because the row this closes is inherited on
            # the QUEUED path and a run that raises out of the queue (a ✕, a dead
            # worker) is exactly when a row left running is most misleading. What
            # the arms do NOT share is the state — see `_close_any_row`.
            _close_any_row(job, e)
            raise
        else:
            _close_any_row(job)
        total = _now() - start
    if not timed:
        return {}
    return {
        "realtimeFactor": seconds / total if total > 0 else None,
        "audioSeconds": seconds,
        "totalSeconds": total,
    }


#: One measurement function per capability, keyed by the same constants
#: `WORKLOADS` is. A table rather than an if-chain so that adding a capability
#: is adding two rows in one file, and so the store test's
#: every-capability-has-a-workload guard has an obvious sibling.
_MEASURE = {
    registry.TEXT_GENERATION: _measure_text,
    registry.IMAGE_GENERATION: _measure_image,
    registry.SPEECH_TO_TEXT: _measure_transcript,
    registry.EMBEDDINGS: _measure_embed,
}


class Cancelled(Exception):
    """This generation was stopped from outside, so there is nothing to record.

    Not a ✕ on a progress row — a benchmark has none (see `run`). It is
    `fused.ai.cancel()`, or any other caller of
    `supervisor.cancel_generation`, reaching the SAME resident worker: one
    model per capability is shared by the whole app, so a page cancelling its
    own generation stops a benchmark that happens to be running on it.

    A distinct exception rather than an `ok:false` record, because a cancelled
    run **measured nothing and is therefore not history**. It was returned as
    `{"ok": false, "error": "cancelled"}` once; the endpoint answered 200 with
    it and the page appended it, so a stopped run drew a phantom
    "Failed — cancelled" entry that became the model's LATEST — the delta and
    the summary then compared against it — until a reload made it vanish. The
    storage side already refused to keep such a record; raising is the same rule
    made impossible to break on the way out, since a caller cannot accidentally
    return an exception.
    """


def _unwatched_job() -> str:
    """A job id for a supervisor call that structurally requires one, naming NO
    download-manager row.

    `generate_image` and `generate_transcript` take a job id positionally and
    pass it to their worker; a benchmark has no row for them to report to, and
    that is deliberate (see `run`). So this is a placeholder, and it has exactly
    two requirements:

    * **Non-empty.** `worker_base.report` does `job = job or JOB_ID`, and
      `JOB_ID` is the worker's own process-level id — the row `supervisor.load`
      opened for this MODEL. Handing the call a falsy job would therefore paint
      benchmark ticks onto the load's byte progress bar, which is the collision
      this whole removal exists to end, arrived at from the other direction.
    * **`sys:`-prefixed.** Nothing creates a row under it — the workers' ticks
      carry no `title`, so `jobs.upsert` refuses them and `report` swallows the
      refusal — but if anything ever did, the row would be server-owned and
      unwritable by a page (BG-4a) rather than a page-owned row nobody can
      account for.

    Fresh per call, so two concurrent benchmarks cannot alias.
    """
    # `jobs.SERVER_ID_PREFIX`, never the literal: the reserved prefix is what
    # makes a row unwritable by a page, and it is minted in one place for the
    # same reason every other `sys:` id in the app is.
    return jobs.SERVER_ID_PREFIX + "ai-benchmark-unwatched-" + secrets.token_hex(6)


def _close_any_row(job: str, failure: BaseException | None = None) -> None:
    """Put a TERMINAL state on `job` if a row for it exists — and create nothing.

    **Titleless on purpose, and that is the whole mechanism.** `jobs.upsert`
    refuses a first report that carries no `title` and `supervisor._report`
    swallows the refusal, so this closes a row that is already there and cannot
    bring one into being. The ordinary path therefore stays row-free while the one
    path that can INHERIT a row gets it tidied.

    That path is a speech benchmark queued behind a real transcription:
    `supervisor._await_turn` reports `_transcribe_row(...)`, which carries a
    title, so a row really is created under the private job id — and once it
    exists the worker's otherwise-refused titleless ticks start landing on it.
    With no terminal report it then sat `running` until `jobs._sweep` evicted it.
    Fixed here rather than in `supervisor.py`, which is byte-identical to main and
    worth keeping that way.

    **The state follows the OUTCOME, which is the half the first cut got wrong.**
    It reported `done` from a bare `finally`, so on exactly the path this exists
    for — that queued row, which is `cancellable` — pressing the ✕ raised
    `SupervisorError("cancelled")` out of `_await_turn` and was answered with a
    green "done" carrying the queued detail, `jobs.upsert` clearing
    `cancel_requested` on the way because it treats `done` as terminal. A dead
    worker did the same: a row claiming success for a run recorded `ok:false`.
    So the shape here is `supervisor.start_transcribe`'s own `run()` — cancelled,
    error with the reason, or done.

    `str(e)` rather than `supervisor._failure_text(e)`: that helper LOGS a
    traceback, `run()` already calls it for the record, and one failure should not
    print two.
    """
    if failure is None:
        supervisor._report(job, state="done")
        return
    message = str(failure) or failure.__class__.__name__
    if isinstance(failure, Cancelled) or message == "cancelled":
        supervisor._report(job, state="cancelled")
    else:
        supervisor._report(job, state="error", message=message)


def _load_to_ready(model: str, capability: str) -> float | None:
    """Make `model` resident, returning the seconds it took — or `None` when it
    already was.

    `None` rather than `0.0`, which is the null-over-estimate rule applied to
    its most tempting case: a warm run genuinely did not load anything, and a
    zero would sit on the chart as an impossibly fast load.

    Its own poll loop rather than `supervisor._wait_ready`, for two reasons: that
    function reports the wait onto a progress row this feature deliberately does
    not have, and it treats an eviction as a hard error, where here the wait is a
    phase of a benchmark that has to end up as an `ok:false` RECORD rather than
    an exception on somebody else's bar.

    **It watches the pending RECORD, not only `ready_worker`, and that is not an
    optimisation.** When `_bring_up` fails — or is cancelled through the ✕ on the
    load's own row — it sets `state="error"` and an `error` message on the pending
    worker and DELETES it from the table, so `ready_worker` answers None forever
    and nothing else ever says why. Polling readiness alone therefore ran the
    whole `_LOAD_TIMEOUT_S` (an hour), holding the HTTP request open, holding the
    router's per-capability claim so every other benchmark of that capability was
    refused for the hour, and finally recording
    `ok:false, "did not finish loading in time"` — a phantom "this model failed
    here", which is the single thing this feature's history must never contain.
    A failed load is now the loader's OWN sentence, immediately, and a cancelled
    one is not recorded at all — with one honest caveat, `_ERROR_GRACE_POLLS`:
    the state and the message are not written together, so a sample can land
    between them, and the wait rides that out rather than reporting a generic
    failure it would have had the real reason for one poll later.

    `_start_resident` rather than `load()`: it is what `load()` calls for a
    non-`weights_only` request and it hands back the pending record this loop
    needs. So there is still exactly one thing that brings a model up — and it
    opens its own row, titled with the model id, which is the row a user watches
    through a cold benchmark's download, and the reason a benchmark must not open
    one of its own (see `run`).
    """
    if supervisor.ready_worker(capability, model) is not None:
        return None
    start = _now()
    _started, pending = supervisor._start_resident(model, capability)
    deadline = start + _LOAD_TIMEOUT_S
    error_polls = 0
    while True:
        if supervisor.ready_worker(capability, model) is not None:
            return _now() - start
        # One critical section for all three reads, which buys exactly one thing:
        # `_bring_up`'s EXCEPTION path writes the error and drops the record from
        # the table without releasing the lock between, so sampling those two
        # apart could catch the table already emptied and the message not yet
        # written and report a phantom eviction for a load that failed with a real
        # reason. Same ordering, and the same reason, as `supervisor._wait_ready`.
        #
        # It does NOT make `state` and `error` mutually consistent, and it is
        # worth being precise about that rather than implying otherwise: the
        # health-poll path assigns `worker.state` OUTSIDE the lock and only then
        # raises, so `state == "error"` with an empty `error` is a reachable
        # sample. `_ERROR_GRACE_POLLS` is what that costs us. The cancel
        # discrimination below is unaffected — a cancel only ever arrives through
        # the exception path, which does write both under the lock.
        with supervisor._lock:
            state, error = pending.state, pending.error
            evicted = supervisor._workers.get(capability) is not pending
        if state == "error":
            # `"cancelled"` is `supervisor._failure_text`'s literal for a ✕, so a
            # cancel arrives down the same channel as a genuine failure and has to
            # be told apart from one: a real failure is a fact about this model on
            # this machine and belongs in the history, a cancel is not and does
            # not.
            if error == "cancelled":
                raise Cancelled()
            if error:
                raise supervisor.SupervisorError(error)
            # The window above. Give the raising thread a few polls to say WHY
            # before falling back to a sentence that says nothing.
            error_polls += 1
            if error_polls > _ERROR_GRACE_POLLS:
                raise supervisor.SupervisorError("the model failed to load")
        if evicted:
            # Genuinely taken away rather than broken: another model claimed the
            # capability, or an unload landed. The record we hold never errored,
            # so there is no better answer than what happened to it — and an hour
            # of silence is a much worse one.
            raise supervisor.SupervisorError(
                f"{model} was unloaded before it could be used")
        if _now() >= deadline:
            # The backstop, for a bring-up that neither succeeds nor says it
            # failed. Reached now only by a genuinely stuck load rather than by
            # every failed one.
            raise supervisor.SupervisorError(
                f"{model} did not finish loading in time")
        # No progress tick: there is no row of ours to report onto. The user is
        # not blind through this phase — `_start_resident` opened the model's own
        # download row and the worker reports its bytes there — and that row's ✕
        # reaches this loop through `pending.state` above.
        time.sleep(_LOAD_POLL_S)


def _memory_and_device(model: str, capability: str) -> tuple[int | None, str | None]:
    """Resident bytes and the device the weights landed on, off `describe()`.

    Sampled AFTER the timed pass rather than continuously: `resident_bytes()`
    already reconciles RSS against a runner's own allocator figure and is
    GPU-pool aware on Apple Silicon (runners/worker_base.py), which is a much
    better number than anything sampled from here — and a polling thread
    reaching into a worker mid-generation would be a request waiting on a GPU
    call for no reason. The cost, stated rather than hidden: a transient spike
    during generation is missed, so this is a resident figure and a
    second-order number, not a true peak of the whole run.
    """
    for row in supervisor.describe().get("loaded") or []:
        if row.get("model") == model and row.get("capability") == capability:
            resident = row.get("residentBytes")
            return (resident if isinstance(resident, int) else None,
                    row.get("device") or None)
    return None, None


def run(model: str, capability: str) -> dict:
    """Benchmark `model` at `capability` and record the result. Blocking.

    Minutes of work — call it on a thread, never on the loop, exactly as
    `generate_image` and `generate_transcript` are called. The request is held
    open for the whole thing rather than turned into a poll-a-benchmark-job
    protocol: that is what those two already do for work of the same length, and
    inventing a second protocol for a third long call would be new machinery
    with no new capability.

    **No download-manager row is created, deliberately, and this is the third
    design after two failures.** Server job rows are a TITLE-KEYED global
    namespace — a page's only route to one is `useCacheScan.ts`'s map of
    `job.title -> job` — and `supervisor.load` already owns the row titled
    exactly `model`. Both spellings of a benchmark row are therefore broken: a
    decorated title ("Benchmark: <model>") is a row no consumer can find, and the
    bare model id SHADOWS the load row, which put the download manager's only
    visible ✕ on the load rather than on the benchmark and let a cold run spin to
    its hour-long timeout and record a phantom "did not finish loading in time".
    A benchmark cannot own a row for a model that already has one, so it owns
    none: the tab shows its own in-tab spinner for the duration. What the user
    still sees through the expensive phase is the LOAD's own row, reported by the
    supervisor with real byte counts, which is the row that was always right for
    that wait.

    **A failure is a RESULT, and it is stored.** "This model OOMs on this
    laptop" is precisely the sort of thing somebody benchmarks to find out, so a
    raising runner comes back as `ok:false` with the message and is appended
    like any other run.

    Three things RAISE instead of returning a record, because in none of them was
    anything measured: an unknown capability (`ValueError` — there is no
    workload, and the router turns it into a 4xx), a generation stopped from
    outside (`Cancelled` — see that class), and an interpreter-level exit, which
    is re-raised untouched.

    Order of operations, and every step of it is load-bearing:

    1. Resolve the runner FIRST. A capability this machine cannot serve fails
       with the registry's own sentence before anything is loaded or timed.
    2. Load to ready, timed (`None` when already resident).
    3. One discarded warm-up pass. A first generation pays for graph
       compilation, a lazily-built tokenizer and a cold cache; timing it would
       make every benchmark a measurement of the first token in the process's
       life. Uniform across capabilities rather than tuned per capability, which
       does cost a second image render — accepted, because a warm-up rule that
       varies by capability is a rule nobody can hold in their head while
       reading two numbers side by side.
    4. The timed pass.
    5. Memory and device off `describe()`.
    6. **Unload, if step 2 loaded it** (D435) — success, failure or exception
       alike. A benchmark is a measurement, not a claim on the model's
       residency, so a cold run must not leave gigabytes parked after the
       button stops spinning; a WARM run (step 2 returned `None`) unloads
       nothing, because the model belongs to whoever already had it running.
    """
    workload = WORKLOADS.get(capability)
    measure = _MEASURE.get(capability)
    if workload is None or measure is None:
        raise ValueError(f"there is no benchmark workload for {capability!r}")

    record = {
        "id": uuid.uuid4().hex,
        # Wall clock, unlike everything else here: this field answers "when was
        # this taken", which a monotonic clock cannot.
        "startedAt": time.time(),
        "capability": capability,
        "model": model,
        "runner": None,
        "device": None,
        # The app version is on every run because the app is part of what is
        # being measured — a runner upgrade that halves throughput is exactly
        # the regression this history exists to make visible.
        "appVersion": fused_render.__version__,
        "ok": False,
        "error": None,
        "loadSeconds": None,
        "peakResidentBytes": None,
        "machine": machine(),
        "workload": workload.as_dict(),
        # Empty until the timed pass returns. A failed run has no metrics rather
        # than a dict of nulls: "not measured" and "measured as nothing" must
        # not render the same.
        "metrics": {},
    }

    try:
        runner = registry.for_capability(capability)
        if runner is None:
            raise supervisor.SupervisorError(
                registry.unavailable_reason(capability)
                or f"nothing here can run {capability}")
        record["runner"] = runner.code

        loaded_seconds = _load_to_ready(model, capability)
        record["loadSeconds"] = loaded_seconds

        try:
            # The discarded warm-up, then the timed pass. Same function twice — see
            # step 3 of the docstring for why the first one's timings are thrown away
            # and why the rule is uniform across capabilities.
            measure(model, workload, timed=False)
            record["metrics"] = measure(model, workload, timed=True)

            record["peakResidentBytes"], record["device"] = _memory_and_device(
                model, capability)
            record["ok"] = True
        finally:
            # D435: a benchmark that had to COLD-LOAD the model tears it back
            # down when it is done — success, a failed workload, a timeout, or
            # an exception mid-measurement alike, which is exactly the `finally`
            # shape. `loaded_seconds` is `None` precisely when `_load_to_ready`
            # found the model already resident (its own docstring's
            # null-over-estimate rule, reused here as the unload signal rather
            # than adding a second piece of state to track the same fact): a
            # model somebody else was already using — a Playground chat, another
            # tab — is never ours to unload, and the capability may not even be
            # ours any more by the time we get here. Placed AFTER the memory and
            # device read above, which needs the worker still resident to answer
            # at all — unloading first would record both as null. `unload`'s own
            # exception is swallowed rather than left to propagate: a teardown
            # that fails must not steal the traceback from a measurement that
            # failed for a real reason (and on the success path there is no
            # exception in flight for it to compete with).
            if loaded_seconds is not None:
                try:
                    supervisor.unload(
                        model=model, capability=capability,
                        reason="Unloaded — a benchmark run loaded it and is done")
                except Exception:
                    logger.exception(
                        "benchmark: failed to unload %s after measuring it", model)
    except (KeyboardInterrupt, SystemExit):
        # NOT a result, and not ours to swallow. A Ctrl-C on the dev server or
        # an interpreter shutdown arriving on this threadpool thread is not a
        # fact about the model, and recording it as `ok:false` would write a
        # fake "this model failed on this laptop" row into the one history this
        # feature exists to keep trustworthy. Re-raised because a
        # `BaseException` handler that eats these is a process that will not go
        # down when asked.
        raise
    except Cancelled:
        # Nothing was measured, so there is nothing to record and nothing to
        # return. See `Cancelled` — the caller gets an exception precisely so a
        # stopped run cannot reach the page as a failed one.
        raise
    except supervisor.SupervisorError as e:
        # `"cancelled"` is the literal the load wait, the image worker and the
        # transcription worker all say it with (`supervisor._failure_text`, and
        # the same string `start_image`/`start_transcribe` switch on). Promoted
        # to `Cancelled` HERE, in one place, so the rest of this module and the
        # router never pattern-match on an error message.
        if str(e) == "cancelled":
            raise Cancelled() from e
        record["ok"] = False
        record["error"] = str(e)
    except BaseException as e:  # noqa: BLE001 - every failure is a stored result
        # A dead worker, a `MemoryError` out of a runner, a bug in a measurement
        # function: all of them are the same kind of fact about this model on
        # this machine, and all belong in the history rather than only in the
        # log. `_failure_text` names the class and logs the traceback, which is
        # the only copy — this thread is the top of its own stack.
        record["ok"] = False
        record["error"] = supervisor._failure_text(e)

    bench_store.append(record)
    return record

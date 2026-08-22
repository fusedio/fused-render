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

import math
import os
import platform
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

#: The download-manager row a benchmark reports to. Minted through
#: `jobs.SERVER_ID_PREFIX` like the supervisor's three prefixes are: the `sys:`
#: prefix is what makes a row unwritable by a page (BG-4a), so an id carrying it
#: is never assembled by hand.
BENCH_JOB_PREFIX = jobs.SERVER_ID_PREFIX + "ai-benchmark:"

#: Steps for an image model the catalog has no per-model hint for — the same 28
#: `POST /api/ai/image` defaults to, so a benchmark measures the pipeline a page
#: actually gets rather than a number invented here.
DEFAULT_IMAGE_STEPS = 28

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


def job_id(uid: str) -> str:
    """The download-manager row id for one benchmark run. See
    `supervisor.image_job_id` — same sanitisation, same reason."""
    return BENCH_JOB_PREFIX + "".join(c for c in uid if c.isalnum() or c in "._-")


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


def _measure_text(model: str, workload: Workload, job: str, *, timed: bool) -> dict:
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


def _measure_embed(model: str, workload: Workload, job: str, *, timed: bool) -> dict:
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


def _measure_image(model: str, workload: Workload, job: str, *, timed: bool) -> dict:
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
        start = _now()
        supervisor.generate_image(model, request, job)
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


def _measure_transcript(model: str, workload: Workload, job: str, *,
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
        audio = os.path.join(tmp, "benchmark.wav")
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
            "out": os.path.join(tmp, "benchmark.json"),
            "outText": os.path.join(tmp, "benchmark.txt"),
            # The row's identity, travelling into the worker exactly as
            # `supervisor.start_transcribe` sends it — and for the identical
            # reason, which is the one thing this path cannot do without.
            # `generate_transcript` passes it to `_wait_ready(row=...)` and the
            # worker restates it on every tick of a decode that runs in a
            # DIFFERENT PROCESS; with it absent both are `None`, `upsert`
            # refuses a first report with no title, and the row goes silent
            # mid-decode. Sent rather than re-spelled there so the two cannot
            # disagree about what this row is.
            "row": row_fields(model, registry.SPEECH_TO_TEXT),
        }
        start = _now()
        supervisor.generate_transcript(model, request, job)
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
    """The ✕ was pressed (or something else stopped the generation).

    A distinct exception rather than an `ok:false` record, because a cancelled
    run **measured nothing and is therefore not history**. The first cut returned
    `{"ok": false, "error": "cancelled"}`; the endpoint answered 200 with it and
    the page appended it, so pressing ✕ drew a phantom "Failed — cancelled" row
    that became the model's LATEST — the delta and the summary then compared
    against it — until a reload made it vanish. The storage side already refused
    to keep such a record; this is the same rule made impossible to break on the
    way out, since a caller cannot accidentally return an exception.
    """


#: Capabilities whose WORKER is handed the job id and aborts a generation
#: itself. For these the ✕ is live for the whole run.
#:
#: The other two are not an oversight in this module: `supervisor.generate_text`
#: and `generate_embed` take no `job` at all, so there is nothing in the
#: measurement that could read the flag — and an embedding is one forward pass
#: through a small tower, which is over before a cancel could mean anything.
#: Rather than advertise a ✕ that does nothing (the exact failure
#: `transcribe_row_fields` documents for a row that loses `cancellable`, in
#: reverse), the row DROPS `cancellable` when the measurement starts for those
#: two — see `run`. The load phase stays cancellable for every capability,
#: because `_load_to_ready`'s poll is real and the load is the phase that can
#: last an hour.
_WORKER_HONOURS_CANCEL = (registry.IMAGE_GENERATION, registry.SPEECH_TO_TEXT)


def row_fields(model: str, capability: str, *,
               cancellable: bool = True) -> dict:
    """The benchmark row's IDENTITY, as opposed to its progress — restated on
    every single report.

    The same payload, and the same reason, as `supervisor.transcribe_row_fields`:
    **any report may turn out to be a FIRST report.** `jobs._sweep` drops the
    least recently updated running row once `MAX_JOBS` (64) bites, so a tick that
    omits `title` does not update a row that is missing — `upsert` RAISES, and
    `_report` swallows the raise, so the row simply never comes back. The rest of
    the payload matters for the same reason one step down: `kind` defaults to
    "task" and `cancellable`/`unit` default to False and "", so an omission
    silently changes what the manager draws.

    **The title is the BARE MODEL ID, and that is a cross-language contract
    rather than a style choice.** The page's only route to a server-owned job row
    is `useCacheScan.ts`'s `new Map(jobs.filter(server).map(j => [j.title, j]))`
    — keyed by TITLE, deliberately, because re-deriving the sanitised job id in
    TypeScript would be a second copy of a Python rule. So a decorated title
    (`"Benchmark: <model>"`, which this shipped as) produces a row that EXISTS,
    reports correctly, and can never be found: `ModelProgress` renders with
    `job=undefined` for the whole multi-minute run, with no error anywhere. There
    is no fallback path either — the endpoint returns the `jobId` only once the
    run has finished. `test_the_row_title_is_exactly_what_the_client_looks_a_row_up_by`
    pins all three halves of that against the TypeScript sources.

    Speech to text returns `supervisor.transcribe_row_fields` VERBATIM rather
    than a lookalike: those workers report `done`/`total` in seconds, so the
    payload carries `unit="s"`, and a hand-written `unit=""` beside it drew a
    bare `12/30`. That payload exists to be the single spelling of a
    transcription row — using it is the point.

    `state` is deliberately NOT here — the terminal report needs `done`/`error`/
    `cancelled` and would have to override it, so every caller says its own.
    """
    if capability == registry.SPEECH_TO_TEXT:
        return {**supervisor.transcribe_row_fields(model),
                "cancellable": cancellable}
    return {"title": model[:80], "kind": "task",
            "cancellable": cancellable, "unit": ""}


def _report(job: str, model: str, capability: str, **fields) -> None:
    """One tick on the benchmark's row, carrying the row's identity, best-effort.

    Delegated to `supervisor._report` rather than reimplemented so a benchmark's
    ticks pass through the same swallow-everything guard every other AI progress
    report already does — reporting must never be the thing that breaks a run.

    **`model` is not optional, and that is the whole point of this wrapper.** The
    first cut reported `detail=` alone: `jobs.upsert` raises
    `JobError("the first report for a job must include a 'title'")` for an id it
    has never seen, `supervisor._report` swallows exactly that, and so every tick
    of every benchmark went on the floor — including the image worker's own
    per-step ticks, which are titleless too and were relying on this row already
    existing. The `jobId` the endpoint returned named nothing, which defeated
    `ModelProgress` and the documented `fused.watchJob(jobId)` contract at the
    same time. Nothing about that failure was visible: no exception, no log line,
    and a test double for `_report` cannot see it either.
    """
    cancellable = fields.pop("cancellable", True)
    supervisor._report(job, **row_fields(model, capability,
                                         cancellable=cancellable), **fields)


def _load_to_ready(model: str, capability: str, job: str) -> float | None:
    """Make `model` resident, returning the seconds it took — or `None` when it
    already was.

    `None` rather than `0.0`, which is the null-over-estimate rule applied to
    its most tempting case: a warm run genuinely did not load anything, and a
    zero would sit on the chart as an impossibly fast load.

    Its own poll loop rather than `supervisor._wait_ready`, for one reason: that
    function reports the wait onto the CALLER's row in a transcription's
    vocabulary and treats an eviction as a hard error. Here the wait is a phase
    of a benchmark that has to end up as an `ok:false` RECORD, not an exception
    on somebody else's progress bar. The load itself is still
    `supervisor.load()`, so there is exactly one thing that brings a model up.
    """
    if supervisor.ready_worker(capability, model) is not None:
        return None
    start = _now()
    supervisor.load(model, capability)
    deadline = start + _LOAD_TIMEOUT_S
    while True:
        if supervisor.ready_worker(capability, model) is not None:
            return _now() - start
        if _now() >= deadline:
            raise supervisor.SupervisorError(
                f"{model} did not finish loading in time")
        # The ✕, read the same way `_wait_ready` reads it — and read HERE
        # because this is the phase that can last an hour, so a cancel nobody
        # polls for is an operable-looking button that does nothing for the
        # whole download. The literal "cancelled" is the codebase's own signal
        # (see `supervisor._failure_text`), which `run` switches on.
        #
        # A tri-state `None` — no row to ask — is deliberately NOT treated as a
        # cancel: an evicted row is not a user pressing anything, and aborting a
        # legitimate multi-GB pull on capacity pressure would be a worse failure
        # than the one this guards. The tick below rebuilds the row.
        if supervisor._cancel_state(job) is True:
            raise Cancelled()
        _report(job, model, capability, state="running",
                detail=f"Loading {model}…")
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


def run(model: str, capability: str, job: str) -> dict:
    """Benchmark `model` at `capability` and record the result. Blocking.

    Minutes of work — call it on a thread, never on the loop, exactly as
    `generate_image` and `generate_transcript` are called. The request is held
    open for the whole thing rather than turned into a poll-a-benchmark-job
    protocol: that is what those two already do for work of the same length, and
    inventing a second protocol for a third long call would be new machinery
    with no new capability. Progress still flows to `job`, so a page is not
    blind while it waits.

    **A failure is a RESULT, and it is stored.** "This model OOMs on this
    laptop" is precisely the sort of thing somebody benchmarks to find out, so a
    raising runner comes back as `ok:false` with the message and is appended
    like any other run.

    Three things RAISE instead of returning a record, because in none of them was
    anything measured: an unknown capability (`ValueError` — there is no
    workload, and the router turns it into a 4xx), a cancel (`Cancelled` — see
    that class for the phantom history row this prevents), and an
    interpreter-level exit, which is re-raised untouched.

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

    # The row is OPENED before anything else happens, with a title, because
    # `jobs.upsert` refuses a first report that has none and `_report` swallows
    # the refusal — so a benchmark that only ever ticked `detail=` reported
    # nothing at all, and the `jobId` the endpoint handed back named a row that
    # did not exist. Same ordering, and the same reason, as
    # `supervisor.start_image` / `start_transcribe`.
    _report(job, model, capability, state="running",
            detail=f"Preparing {workload.name}…", done=None, total=None)
    # Whether the ✕ survives into the MEASUREMENT phase — see
    # `_WORKER_HONOURS_CANCEL`. Computed once so the phase reports and the
    # terminal report cannot disagree about what this row offers.
    measuring_cancellable = capability in _WORKER_HONOURS_CANCEL

    try:
        runner = registry.for_capability(capability)
        if runner is None:
            raise supervisor.SupervisorError(
                registry.unavailable_reason(capability)
                or f"nothing here can run {capability}")
        record["runner"] = runner.code

        record["loadSeconds"] = _load_to_ready(model, capability, job)

        _report(job, model, capability, state="running", detail="Warming up…",
                cancellable=measuring_cancellable)
        measure(model, workload, job, timed=False)

        _report(job, model, capability, state="running",
                detail=f"Running {workload.name}…",
                cancellable=measuring_cancellable)
        record["metrics"] = measure(model, workload, job, timed=True)

        record["peakResidentBytes"], record["device"] = _memory_and_device(
            model, capability)
        record["ok"] = True
    except (KeyboardInterrupt, SystemExit):
        # NOT a result, and not ours to swallow. A Ctrl-C on the dev server or
        # an interpreter shutdown arriving on this threadpool thread is not a
        # fact about the model, and recording it as `ok:false` would write a
        # fake "this model failed on this laptop" row into the one history this
        # feature exists to keep trustworthy. Re-raised because a
        # `BaseException` handler that eats these is a process that will not go
        # down when asked.
        _report(job, model, capability, state="cancelled")
        raise
    except Cancelled:
        # Nothing was measured, so there is nothing to record and nothing to
        # return. See `Cancelled` — the caller gets an exception precisely so a
        # cancelled run cannot reach the page as a failed one.
        _report(job, model, capability, state="cancelled")
        raise
    except supervisor.SupervisorError as e:
        # `"cancelled"` is the literal the load wait, the image worker and the
        # transcription worker all say it with (`supervisor._failure_text`, and
        # the same string `start_image`/`start_transcribe` switch on). Promoted
        # to `Cancelled` HERE, in one place, so the rest of this module and the
        # router never pattern-match on an error message.
        if str(e) == "cancelled":
            _report(job, model, capability, state="cancelled")
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

    if record["ok"]:
        _report(job, model, capability, state="done",
                detail=f"{workload.name} finished",
                cancellable=measuring_cancellable)
    else:
        _report(job, model, capability, state="error",
                message=record["error"] or "failed",
                cancellable=measuring_cancellable)
    bench_store.append(record)
    return record

"""The fixed per-capability workloads a benchmark run executes (SPEC AI-16).

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

import os
import platform
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from fused_render.ai import registry


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

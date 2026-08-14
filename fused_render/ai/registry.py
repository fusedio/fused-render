"""What local inference this machine can do, and which folder does it (SPEC §40).

A **runner** is a folder holding a `pyproject.toml` and a `worker.py`. That is the
whole of it — the same shape `templates/zarr_aoi/` uses for its tile daemon, and
for the same reason: the model runs in its OWN interpreter, built from its own
declaration, in its own process.

Three things follow from that, and they are the reasons this is a folder rather
than an import:

* **fused-render's venv never grows torch or mlx.** They are multi-GB and
  platform-specific; a file explorer that could not start without them would be a
  worse file explorer. The runner's `pyproject.toml` is the only place they are
  named, and `envinstall` (PY-18) builds it on first use — the same detached
  `uv sync` with the same progress record and the same verbatim errors that every
  other declaring folder gets. No new install machinery exists for AI.
* **A wedged model cannot take the app down.** OOM, a CUDA fault, a Rust panic
  inside a loader — all of it happens in a process the supervisor can kill.
* **Adding a backend is adding a folder.** This was written when MLX was the only
  text runner and said "transformers for Windows tomorrow" — which turned out to
  be exactly one new row here and one new folder (D293), with nothing else in the
  app changed. The claim is kept because it was tested.

**Availability is checked, never assumed.** MLX runs on Apple Silicon and nowhere
else, so `available()` answers with a REASON rather than a bool — "needs Apple
Silicon (this is linux/x86_64)" is something a page can show, while a silently
missing capability is something a user files a bug about.

Resolution is by CAPABILITY, not by model: a caller asks for `text-generation`
and gets whichever runner serves it here. A model id never picks the runner,
because the same repo can be servable by two backends and the choice belongs to
the machine, not to the string.

**Two runners can share one capability, and the ORDER between them is the whole
mechanism.** Text generation is served by MLX on Apple Silicon and by torch
everywhere else: both rows are registered, both are asked whether they can run,
and the first that says yes wins. Nothing else in the app knows there is more
than one — but the CATALOG does, because what to suggest depends on which
backend will load it (`catalog.py`), and an MLX checkpoint on a Windows machine
is a download that cannot be used.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from typing import Callable

# One capability per runner for now. The constants are the vocabulary the whole
# feature speaks — the API's `capability` parameter, the catalog's grouping, and
# the supervisor's one-resident-model-per-capability rule all key off these.
TEXT_GENERATION = "text-generation"
IMAGE_GENERATION = "text-to-image"
#: The Hub's own tag for it, like `IMAGE_GENERATION` is — so the constant, the
#: `pipeline_tag` on a Whisper repo and the capability a card asks to load are
#: one string rather than three that have to be kept in step.
SPEECH_TO_TEXT = "automatic-speech-recognition"

RUNNERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runners")


@dataclass(frozen=True)
class Availability:
    """Whether a runner can run here, and — when it cannot — why not in words.

    The reason is user-facing. "needs Apple Silicon (this is linux/x86_64)" tells
    someone what to do with the information; a False does not.
    """

    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class Runner:
    """One backend: a capability, a folder, and a rule about where it runs."""

    code: str
    capability: str
    #: Folder holding `pyproject.toml` (its declaration) and `worker.py` (the
    #: process the supervisor starts). Both are read from here and nowhere else.
    folder: str
    #: What this backend is, in one line, for the page that has to explain a
    #: capability the machine cannot serve.
    label: str
    #: What using this backend is LIKE, for the page to say before anything is
    #: loaded. A standing fact about the runner, never a claim about this
    #: machine — the device a model actually got is the worker's to report
    #: (`worker_base.STATE["device"]`) and is not knowable until one has run.
    #:
    #: It exists because the honest answer for `transformers-text` is "this may
    #: be a great deal slower than you expect, and here is why", and a user who
    #: reads that before starting an 8GB download has been told something
    #: useful. Empty for a runner with nothing surprising to say.
    note: str = ""
    _available: Callable[[], Availability] = field(repr=False, default=lambda: Availability(True))

    def available(self) -> Availability:
        """Can this runner run here — platform AND presence.

        The presence half is not paranoia about a broken install: a runner is
        registered before its folder is written (the image runner was listed
        with its worker still unbuilt), and a registry that advertises a
        capability whose folder is missing hands the user a Download button that
        fails at spawn while the API reports the capability ready. Advertising
        is a claim; this is the check that makes it true.
        """
        if not os.path.isfile(self.worker):
            return Availability(False, f"the {self.label} runner is not built yet")
        return self._available()

    @property
    def worker(self) -> str:
        return os.path.join(self.folder, "worker.py")

    @property
    def pyproject(self) -> str:
        return os.path.join(self.folder, "pyproject.toml")


def _apple_silicon() -> Availability:
    """MLX is Metal-only: Apple Silicon, and nothing else — not Intel Macs.

    Checked at call time rather than import time so a test can monkeypatch
    `platform` and get the answer it is asserting about.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin" and machine == "arm64":
        return Availability(True)
    return Availability(
        False,
        f"needs Apple Silicon — MLX runs on Metal only (this is {system.lower()}/{machine})",
    )


def _always() -> Availability:
    """torch + diffusers, torch + transformers, and CTranslate2 all build wheels
    for macOS (both architectures), Linux and Windows. Whether the machine is
    FAST enough is a different question, and not one to refuse on — a model
    answering slowly on a CPU is a model answering, and the device is reported
    (`worker_base.STATE["device"]`) so the page can say which case it is."""
    return Availability(True)


# The table. Ordered, and first-match-wins per capability — which is what lets
# TWO runners serve text generation: MLX takes Apple Silicon, and `transformers`
# below it picks up every machine MLX turns down. The ordering is the whole
# mechanism, so the rows are not sorted alphabetically and must not be.
_RUNNERS: tuple[Runner, ...] = (
    Runner(
        code="mlx-text",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "mlx_text"),
        label="MLX (Apple Silicon)",
        _available=_apple_silicon,
    ),
    Runner(
        code="transformers-text",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "transformers_text"),
        label="Transformers (PyTorch)",
        # ONE LINE, and that is a hard constraint rather than a summary: this
        # sits above the cards where it is read on the way past, and anything
        # that wraps is something nobody finishes. Everything that used to
        # follow a dash here — that the Windows build is CPU-only, that a CPU
        # answers at a few words a second — lives in the loaded card's tooltip
        # instead, where somebody has stopped to ask.
        note="Uses an NVIDIA GPU when PyTorch can see one, and the CPU otherwise.",
        # `_always`, and it is deliberately BELOW the MLX row rather than
        # instead of it. torch runs everywhere, so this row alone would serve
        # Apple Silicon too — and would be the wrong answer there: MLX is faster
        # on Metal and its 4-bit catalog is sized for a 16GB laptop, where this
        # runner's unquantized checkpoints are not. First-match-wins gives each
        # machine the better backend without either runner knowing about the
        # other.
        _available=_always,
    ),
    Runner(
        code="diffusers-image",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "diffusers_image"),
        label="Diffusers (PyTorch)",
        _available=_always,
    ),
    Runner(
        code="faster-whisper",
        capability=SPEECH_TO_TEXT,
        folder=os.path.join(RUNNERS_DIR, "faster_whisper"),
        label="faster-whisper (CTranslate2)",
        # `_always`, and that is the reason this runner is CTranslate2 and not
        # MLX: text generation is already Apple-Silicon-only, and a second
        # capability that only exists on a Mac would make "local AI" a thing
        # Windows and Linux users read about rather than use. An `mlx_whisper`
        # runner can be added later ABOVE this row — first-match-wins ordering
        # is what would let it take the Macs and leave everything else here.
        _available=_always,
    ),
)


#: Friendly task label (the vocabulary `ai_models` produces) -> the capability
#: that can actually RUN it.
#:
#: **A vision-language checkpoint is a text model when you only give it text**,
#: and that is not a technicality — `mlx-community/gemma-3-12b-it-4bit` is
#: labelled "image + text to text" because gemma-3 carries a vision tower, and
#: it is also one of the models this app's own catalog RECOMMENDS for chat.
#: Leaving the label out of this table took the Load button off a model the app
#: was suggesting on the next tab over. mlx-lm loads such a checkpoint through
#: its text config; the image half simply goes unused until an `mlx-vlm` runner
#: exists to use it.
_TASK_CAPABILITIES = {
    "text generation": TEXT_GENERATION,
    "image + text to text": TEXT_GENERATION,
    "text to image": IMAGE_GENERATION,
    "image generation": IMAGE_GENERATION,
    "speech recognition": SPEECH_TO_TEXT,
}

#: The other half of the same decision: labels nothing here serves, listed
#: rather than merely absent.
#:
#: Absence is how the gemma bug happened — a label that nobody had thought about
#: and a label that had been ruled out looked identical, so the vocabulary grew
#: and the table silently did not. `test_every_task_label_is_classified` requires
#: every label the listing can produce to appear in one of these two, which turns
#: "we forgot" into a failing test instead of a missing button.
NO_RUNNER_YET = frozenset({
    # Nothing here generates embeddings, classifies, or segments — these are
    # real jobs with no local runner in this cut.
    "embeddings", "sentence embeddings", "fill mask", "text classification",
    "token classification", "question answering", "summarization", "translation",
    "image classification", "zero-shot image classification",
    "zero-shot text classification", "image segmentation", "object detection",
    "depth estimation", "image to image", "image to text", "audio classification",
    "video generation",
    # Speech OUT, as opposed to speech in. Deliberately not folded into the
    # transcription capability as a direction flag: one capability holds one
    # resident model, so a shared "audio" capability would have a synthesis
    # model evict a Whisper model and back again on every alternation.
    "text to speech", "audio generation",
    # An encoder-decoder (T5-shaped). Not the causal-LM path mlx-lm serves, so
    # it is not text generation however much the name suggests it.
    "text-to-text generation",
    # A model that takes and returns several modalities at once. Which one a
    # caller wants is not a thing this table can decide.
    "any input to any output",
})


def capability_for_task(task: str | None) -> str | None:
    """Which capability, if any, could load a model doing `task`.

    Here rather than in the page, because the page would then hold a second copy
    of the mapping between the task vocabulary and the capability vocabulary —
    and a page that guesses "text-generation" for everything will happily try to
    load a diffusion model as a chat model.

    None for a label in `NO_RUNNER_YET`, and None for a label in NEITHER table —
    the answers are the same but the second one is a bug, which is what the
    classification test exists to catch.
    """
    if not task:
        return None
    return _TASK_CAPABILITIES.get(task)


def all_runners() -> tuple[Runner, ...]:
    return _RUNNERS


def by_code(code: str) -> Runner | None:
    return next((r for r in _RUNNERS if r.code == code), None)


def for_capability(capability: str) -> Runner | None:
    """The runner that serves `capability` HERE, or None.

    Availability is part of the resolution, not a check the caller does after:
    picking a runner that cannot run and failing later would report "the model
    failed to load" for a machine that was never going to be able to load it.
    """
    for runner in _RUNNERS:
        if runner.capability == capability and runner.available().ok:
            return runner
    return None


def unavailable_reason(capability: str) -> str | None:
    """Why nothing here serves `capability`, in words — or None when something does.

    The same sentence `supervisor._runner_or_raise` raises, available to a
    caller that has not got as far as starting anything. It exists because a
    request can now fail EARLIER than the supervisor for a reason that has
    nothing to do with the real one: since the catalog became per-runner (D293),
    an unavailable runner also has no curated default, so `POST /api/ai/image`
    answered "no image model is configured" — true, useless, and hiding the
    actionable "the Diffusers runner is not built yet" one layer down.

    Both messages are worth keeping, and this is what tells them apart: no
    runner is a fact about the MACHINE, and no suggestion is a fact about the
    CATALOG.
    """
    if for_capability(capability) is not None:
        return None
    known = next((r for r in _RUNNERS if r.capability == capability), None)
    if known is None:
        return f"no runner provides {capability!r}"
    return known.available().reason or f"no runner provides {capability!r}"


def capabilities() -> tuple[str, ...]:
    """Every capability the registry knows, servable here or not — the page
    lists them all so an unavailable one can say why."""
    seen: list[str] = []
    for runner in _RUNNERS:
        if runner.capability not in seen:
            seen.append(runner.capability)
    return tuple(seen)


def describe() -> list[dict]:
    """The registry as the API reports it: what exists, what runs here, and why
    not when it does not."""
    rows = []
    for runner in _RUNNERS:
        status = runner.available()
        rows.append(
            {
                "code": runner.code,
                "capability": runner.capability,
                "label": runner.label,
                "note": runner.note or None,
                "available": status.ok,
                "reason": status.reason or None,
            }
        )
    return rows

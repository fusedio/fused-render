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
mechanism.** Text generation prefers MLX on Apple Silicon and uses torch on
Windows and Linux, with torch also remaining a fallback on Apple Silicon when
MLX is unavailable; speech to text does the same thing with MLX Whisper over
CTranslate2. Both rows are registered, both are asked whether they can run, and
the first that says yes wins. Nothing else in the app knows there is more than
one — but the CATALOG does, because what to suggest depends on which backend
will load it (`catalog.py`), and an MLX checkpoint on a Windows machine is a
download that cannot be used.

**A user can override that order, and the override is a REQUEST rather than an
instruction** (D301). `resolve()` reads a per-capability preference — "auto", or
a runner code — from `shell/prefs.py`, and a named runner wins only if it can
actually run here. An honoured preference is the whole story; an override naming
a runner this machine cannot run is IGNORED and the ordering above decides, with
the reason carried out in the `Resolution` so a page can say what happened. That
asymmetry is the point: prefs.json travels — it is a plain file in a home
directory people sync, copy between machines and restore from a backup — so a
preference set on a Mac must not arrive on a Windows box and take speech to text
away entirely. A preference that quietly does nothing is recoverable; a
capability that has silently vanished is a bug report.
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
    """torch + diffusers and CTranslate2 build on every platform we ship.

    Whether the machine is FAST enough is a different question, and not one to
    refuse on — a model answering slowly on a CPU is a model answering, and the
    device is reported (`worker_base.STATE["device"]`) so the page can say which
    case it is.
    """
    return Availability(True)


def _transformers_platform() -> Availability:
    """The transformers text runner's supported production platforms.

    MLX is preferred on Apple Silicon by registry order, but torch's MPS path is
    a working fallback when MLX is absent or unavailable. Intel macOS is not a
    distribution target, and availability drives the catalog and Load button,
    so it must not be advertised merely because torch happens to publish a
    wheel there.
    """
    system = platform.system()
    machine = platform.machine()
    if (
        system in ("Windows", "Linux")
        or (system == "Darwin" and machine == "arm64")
    ):
        return Availability(True)
    return Availability(
        False,
        f"requires Windows, Linux, or Apple Silicon macOS (this is {system.lower()}/{machine})",
    )


# The table. Ordered, and first-match-wins per capability — which is what lets
# TWO runners serve one: MLX takes Apple Silicon when available, and the row
# below it serves Windows and Linux plus the Apple Silicon fallback. Both
# multi-runner capabilities (text generation, speech to text) are arranged that
# way. The ordering is the whole mechanism, so the rows are not sorted
# alphabetically and must not be — it is also the DEFAULT that a user's engine
# preference overrides, so a re-order silently re-decides every machine set to
# "auto", which is all of them until somebody chooses otherwise.
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
        # Deliberately BELOW the MLX row rather than instead of it. Apple Silicon
        # therefore gets MLX when it is present and this runner's working MPS
        # path when it is not; Windows and Linux come here directly. Intel macOS
        # is not a distribution target.
        _available=_transformers_platform,
    ),
    Runner(
        code="diffusers-image",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "diffusers_image"),
        label="Diffusers (PyTorch)",
        _available=_always,
    ),
    # Speech to text, and the capability that finally USED the two-runner
    # ordering this table was built for. MLX takes the Macs; CTranslate2 below
    # it keeps every other platform — and keeps the Macs too whenever the MLX
    # folder is not built yet, which is the state `Runner.available` describes.
    Runner(
        code="mlx-whisper",
        capability=SPEECH_TO_TEXT,
        folder=os.path.join(RUNNERS_DIR, "mlx_whisper"),
        label="MLX Whisper (Apple Silicon)",
        note="Transcribes on the GPU. Several times quicker than the CPU path "
             "on the same Mac.",
        _available=_apple_silicon,
    ),
    Runner(
        code="faster-whisper",
        capability=SPEECH_TO_TEXT,
        folder=os.path.join(RUNNERS_DIR, "faster_whisper"),
        label="faster-whisper (CTranslate2)",
        # `_always`, and that is why speech to text SHIPPED on CTranslate2
        # rather than on MLX: text generation was already Apple-Silicon-only,
        # and a second capability that existed on a Mac and nowhere else would
        # have made "local AI" a thing Windows and Linux users read about
        # rather than used. The MLX row above is the sequel that argument
        # always allowed for — it takes the Macs and leaves everything else
        # here, and no user loses a capability to it.
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


#: What a capability's engine preference says when nobody has chosen: use the
#: table's order. The literal is shared with `shell/prefs.py` and the
#: Preferences page rather than spelled three times, because it is a value that
#: travels through JSON and a typo in any copy reads as an unknown runner.
AUTO = "auto"


@dataclass(frozen=True)
class Resolution:
    """Which runner serves a capability here, and whether anyone was overruled.

    `for_capability` answers only the first half, which is all almost every
    caller wants. This exists for the ones that have to EXPLAIN the answer: the
    Preferences page, which must not show a preference as being in force when it
    is not, and the AI Models page, whose suggestion list changes when the
    engine does.
    """

    #: What will actually load. None when nothing can serve the capability here.
    runner: Runner | None
    #: The preference as stored — `AUTO`, or a runner code.
    requested: str = AUTO
    #: Why the request was not honoured, in words, for a page to show. Empty
    #: when it was — including when nothing was requested, since "auto" is
    #: honoured by definition.
    ignored_reason: str = ""

    @property
    def honoured(self) -> bool:
        return not self.ignored_reason


def preferred_code(capability: str) -> str:
    """The user's engine choice for `capability` — `AUTO` when there is none.

    Read on every resolution rather than cached, for the same reason
    `prefs.selected_engine()` is: a preference is a file, changing it must not
    need a restart (CT-5), and this is not on a hot path — a resolution happens
    once per load, per download and per page render, not per token.

    Imported lazily and defended, because the registry is imported by the
    supervisor and by the worker-facing code paths, and it must not become a
    thing that cannot answer because a preferences file is unreadable. A machine
    with no prefs.json is the normal case, not an error.
    """
    try:
        from fused_render.shell import prefs

        return prefs.engine_for_capability(capability)
    except Exception:  # noqa: BLE001 - a preference must never break resolution
        return AUTO


def _first_available(capability: str) -> Runner | None:
    """Registry order, filtered by availability — the rule before D301, and
    still the rule whenever a preference is absent or unusable."""
    for runner in _RUNNERS:
        if runner.capability == capability and runner.available().ok:
            return runner
    return None


def resolve(capability: str) -> Resolution:
    """Which runner serves `capability` here, and what the user asked for.

    Availability is part of the resolution and not a check the caller does
    after: picking a runner that cannot run and failing later would report "the
    model failed to load" for a machine that was never going to be able to load
    it. That applies to the PREFERENCE too, which is the whole design of this
    function — see the module docstring. A preference naming a runner that
    cannot run here is dropped, the ordering decides instead, and the reason
    comes back so that a page can say so rather than showing a control whose
    value has no effect.

    Three ways a preference is dropped, and they are told apart because the
    remedies differ:

    * the runner does not serve this capability (a stale prefs.json, or one
      hand-edited),
    * the runner is not registered at all (a preference written by a NEWER
      build, then opened by an older one — the reason this is not an assert),
    * the runner cannot run here, which is the case that actually happens: a
      preference set on a Mac, carried to a Windows machine in a synced home
      directory.
    """
    requested = preferred_code(capability)
    if requested and requested != AUTO:
        runner = by_code(requested)
        if runner is None:
            return Resolution(_first_available(capability), requested,
                              f"{requested} is not a runner this build knows")
        if runner.capability != capability:
            return Resolution(_first_available(capability), requested,
                              f"{runner.label} does not do {capability}")
        status = runner.available()
        if not status.ok:
            return Resolution(_first_available(capability), requested,
                              status.reason or f"{runner.label} cannot run here")
        return Resolution(runner, requested)
    return Resolution(_first_available(capability), AUTO)


def for_capability(capability: str) -> Runner | None:
    """The runner that serves `capability` HERE, or None.

    The whole app's resolution, and deliberately the SAME call for the
    supervisor, the catalog and the API — a second copy of this rule is how a
    page comes to offer a model the loader then refuses (D293), and a
    preference honoured in one place and not another would be the same bug with
    a new cause.
    """
    return resolve(capability).runner


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

    **Every runner's reason, not the first one's**, and with two runners per
    capability that stopped being a detail. The first cut took
    `next(r for r in _RUNNERS if r.capability == capability)`, which for text
    generation is always `mlx-text` — so a Linux machine whose transformers
    worker was missing (a state `Runner.available` documents, since a runner is
    registered before its folder is written) would be told text generation
    "needs Apple Silicon", naming the one backend that was never going to serve
    it and hiding the one that would have. Reported by review on the PR that
    added the second runner.

    Joined rather than picked, because there is no rule for choosing between
    them that is not a guess about which the reader meant — and a capability
    with one runner, which is all of them but this one, reads exactly as before.
    Duplicates are dropped: two runners of the same label failing the same way
    is one sentence, said once.
    """
    if for_capability(capability) is not None:
        return None
    reasons: list[str] = []
    for runner in _RUNNERS:
        if runner.capability != capability:
            continue
        reason = runner.available().reason
        if reason and reason not in reasons:
            reasons.append(reason)
    return "; ".join(reasons) or f"no runner provides {capability!r}"


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
    not when it does not.

    **`available` and `active` are different questions, and they only became
    different with D301.** Availability is a fact about the hardware: can this
    backend run at all. Active is a fact about this capability right now: is
    this the backend a load would use. They were the same answer while
    resolution was purely first-available — the first available runner was the
    one that ran — so `fused.ai.models.list()` reported availability and every
    reader took it to mean "this is what serves me". With a user preference in
    the middle that reading is wrong: on an Apple Silicon machine BOTH whisper
    runners are available and exactly one is active. A page that cannot tell
    them apart cannot say which engine transcribed for it.
    """
    engines = {capability: resolve(capability) for capability in capabilities()}
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
                # Which of the available runners this capability is actually
                # using. False for every runner of a capability nothing can
                # serve, which is the honest answer — there is no active engine.
                "active": engines[runner.capability].runner is runner,
            }
        )
    return rows


def describe_engines() -> list[dict]:
    """One row per capability: what was asked for, what is serving, what was
    ignored.

    Separate from `describe()` because it answers a different question and has a
    different cardinality — a preference belongs to a CAPABILITY, while
    availability belongs to a RUNNER. Folding the two would give every runner
    row a copy of its capability's preference, and two rows of one capability
    disagreeing about it would then be representable.
    """
    rows = []
    for capability in capabilities():
        resolution = resolve(capability)
        rows.append(
            {
                "capability": capability,
                # As STORED: what a PUT round-trips, and what applies again if
                # the machine that cannot honour it stops being the one in use.
                # Never rewritten to match reality — a preference silently
                # corrected on read is one the user cannot see or undo.
                "selected": resolution.requested,
                "effective": resolution.runner.code if resolution.runner else None,
                "effectiveLabel": resolution.runner.label if resolution.runner else None,
                # Null when the selection is in force (including "auto", which
                # is honoured by definition). A sentence when it is not, and the
                # UI is expected to show it — a control whose value does nothing,
                # with nothing saying why, is the failure this field exists for.
                "ignoredReason": resolution.ignored_reason or None,
                "choices": [
                    {
                        "code": runner.code,
                        "label": runner.label,
                        "note": runner.note or None,
                        "available": runner.available().ok,
                        # The registry's own words ("needs Apple Silicon — MLX
                        # runs on Metal only (this is windows/amd64)"), so the
                        # disabled radio explains itself with the same sentence
                        # the rest of the app uses. The page must not write its
                        # own copy of this, because the page cannot know it.
                        "reason": runner.available().reason or None,
                    }
                    for runner in _RUNNERS
                    if runner.capability == capability
                ],
            }
        )
    return rows

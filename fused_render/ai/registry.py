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
* **Adding a backend is adding a folder — and REMOVING one is removing a
  folder.** This was written when MLX was the only text runner and predicted a
  cross-platform runner "tomorrow"; that turned out to be exactly one new row
  here and one new folder (D293, transformers), and the llama.cpp rows below cost
  the same (D411). D416 then took the transformers family back OUT, and the
  reverse direction held too: three folders, three rows, and the ripple was in
  the CATALOG and the PROSE rather than in any mechanism. The claim is kept
  because it has now been tested in both directions.

**Availability is checked, never assumed.** MLX runs on Apple Silicon and nowhere
else, so `available()` answers with a REASON rather than a bool — "needs Apple
Silicon (this is linux/x86_64)" is something a page can show, while a silently
missing capability is something a user files a bug about.

Resolution is by CAPABILITY, not by model: a caller asks for `text-generation`
and gets whichever runner serves it here. A model id never picks the runner,
because the same repo can be servable by two backends and the choice belongs to
the machine, not to the string.

**Several runners can share one capability, and the ORDER between them is the
whole mechanism.** Text generation prefers MLX on Apple Silicon and uses torch
on Windows and Linux, with torch also remaining a fallback on Apple Silicon when
MLX is unavailable; speech to text does the same thing with MLX Whisper over
CTranslate2 (D319 briefly added a third row, Parakeet-TDT, and D406 withdrew it
— maintenance cost not justified by use — leaving the two rows below). Every
row is registered, every one is asked whether it can run, and the first that
says yes wins. Nothing else in the app knows there is more than
one — but the CATALOG does, because what to suggest depends on which backend
will load it (`catalog.py`), and an MLX checkpoint on a Windows machine is a
download that cannot be used.

**A user can override that order, and the override is a REQUEST rather than an
instruction** (D302). `resolve()` reads a per-capability preference — "auto", or
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

import glob
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
#: Vectors, not words: one model that turns a piece of text — or, where the
#: checkpoint has a vision tower, an image — into a point in one space, so a page
#: can compare them.
#:
#: **TWO MODEL SHAPES under one capability, which no other row here has.** A
#: DUAL ENCODER (SigLIP, CLIP) projects text and pictures into the same space and
#: answers `paths`; a PROSE ENCODER (BERT-family, 512 to 8192 tokens) answers
#: text alone and is what makes RAG, document search and clustering possible —
#: impossible on a dual encoder at any chunk size, since a SigLIP text tower
#: truncates at 64 tokens. `formats.DUAL_EMBED_MODEL_TYPES` and
#: `formats.TEXT_EMBED_MODEL_TYPES` are the two halves, and both engines here
#: serve both.
#:
#: **So `paths` and `kind` are PER-MODEL rather than per-capability**, and that
#: is the design consequence: the route refuses `paths` for a model with no
#: vision tower and `kind` for a model with no retrieval convention, each with a
#: 400 naming the model — the shape `_accepts_image` already uses for text
#: generation. A capability-level answer would have to be the union (offer
#: everything, fail inside a worker) or the intersection (offer neither, and the
#: image half of this capability disappears).
#:
#: **This capability was split until this branch, and the split is what got
#: reversed.** A separate `embed-text` capability meant two resident slots on a
#: machine that has budget for one, two catalogs, two bridge calls and two sets
#: of docs, to serve one question — "turn this into a vector" — whose answer
#: differs only in whether the checkpoint has a second tower.
#:
#: **It is also the one capability reached by MANY Hub tags**
#: (`ai/tasks.py`): `zero-shot-image-classification` for a dual encoder,
#: `feature-extraction` and `sentence-similarity` for a prose one. The plain
#: English name rather than any of them, unlike `SPEECH_TO_TEXT` above, and for
#: two reasons that both still hold: `zero-shot-image-classification` describes
#: one thing you can DO with these vectors rather than what the runner returns,
#: and no single tag covers both shapes. A future `reranking` capability sits
#: beside this one without renaming it.
EMBEDDINGS = "embeddings"
#: The Hub's own tag, like `IMAGE_GENERATION` and `SPEECH_TO_TEXT` are — a
#: diffusers text-to-video pipeline's `_class_name` folds onto "video
#: generation" (`hub_cache.py`'s `_diffusers_task`), and that label maps here.
#: The FIRST capability with no "everywhere" row: its only engine is MLX, so
#: unlike text/image/speech/embeddings there is no cross-platform fallback —
#: off Apple Silicon this capability has zero runners able to serve it, and
#: `catalog()` reports `default: null` for it there.
VIDEO_GENERATION = "text-to-video"
#: The Hub's own tag, spelled exactly as the Hub spells it — same convention
#: as every constant above. The second capability with no "everywhere" row:
#: its one engine (`hunyuan3d-mlx`) is MLX, so like `VIDEO_GENERATION` this
#: is Apple-Silicon-only with zero cross-platform fallback, and `catalog()`
#: reports `default: null` for it elsewhere. Shape generation only — see
#: the `hunyuan3d-mlx` `Runner` row below for what is deliberately not
#: ported (the PBR texture stage) and why.
IMAGE_TO_3D = "image-to-3d"

# --------------------------------------------------------------- SPEC AI-28
#: Orthogonal TAGS, not capabilities — `tool-use` and `vision` describe a
#: TEXT_GENERATION checkpoint's own abilities, and neither is a runner
#: dispatch key the way the five constants above are. **Do not add a row
#: here for a use case; the five capabilities stay a five-way dispatch
#: table, and item 18's own text is explicit that reshaping them into a
#: six-way (or more) enum is out of scope** — a model that both generates
#: text AND calls tools is still one `text-generation` entry, tagged.
TOOL_USE_TAG = "tool-use"
VISION_TAG = "vision"

#: A KNOWN-FAMILY allowlist for tool-use support, not a regex reverse-
#: engineered over an arbitrary repo id — the comparative study this build
#: derives from (llmfit) keys tool-use off exactly these families, and this
#: table restates its list rather than inventing a broader pattern that
#: would confidently tag a checkpoint nobody has actually verified calls
#: tools reliably. Each row is a tuple of substrings that must ALL appear in
#: the (normalized) evidence for a match — most rows are a single family
#: name, but `llama-3`/`mistral`/`gemma-3`/`gemma-4` additionally require an
#: instruction-tuning qualifier, because the base (non-instruct) checkpoint
#: of each is not trained on a tool-calling format the way its `-instruct`/
#: `-it` sibling is.
TOOL_USE_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("qwen3",),
    ("qwen2.5",),
    ("command-r",),
    ("hermes",),
    ("llama-3", "instruct"),
    ("mistral", "instruct"),
    ("gemma-3", "-it"),
    ("gemma-4", "-it"),
)


def _tag_haystack(*values: str | None) -> str:
    """Every non-empty string in `values`, lowercased and with underscores
    normalized to hyphens — the Hub spells the same family both ways
    (`llama_3` and `llama-3` both appear in the wild) and `TOOL_USE_
    FAMILIES` is written in the hyphenated form throughout, so normalizing
    ONE separator here is cheaper and less error-prone than doubling every
    row in the table to cover both spellings."""
    parts = [v for v in values if isinstance(v, str) and v]
    return "|".join(parts).lower().replace("_", "-")


def supports_tool_use(repo_id: str, *, model_type: str | None = None,
                      architecture: str | None = None) -> bool:
    """Does `repo_id` (optionally backed by `hub_metadata`'s harvested
    `model_type`/`architecture`) belong to a family `TOOL_USE_FAMILIES`
    lists?

    Dependency-light BY DESIGN: `registry.py` reads no filesystem or network
    beyond its own runner-folder discovery, so this takes whatever family
    evidence a caller already has rather than fetching `hub_metadata.get`
    itself — the caller (`ai_runtime.describe_catalog`) already holds that
    result for the KV-cache/vision questions and can pass it straight
    through.
    """
    haystack = _tag_haystack(repo_id, model_type, architecture)
    return any(all(token in haystack for token in family)
              for family in TOOL_USE_FAMILIES)


def capability_tags(repo_id: str, *, model_type: str | None = None,
                    architecture: str | None = None,
                    has_vision: bool = False) -> tuple[str, ...]:
    """The orthogonal tags `repo_id` carries — `TOOL_USE_TAG` per
    `supports_tool_use`, `VISION_TAG` when `has_vision` (the caller's own
    answer, from `hub_cache.has_vision_tower`/`hub_metadata`'s
    `hasVisionTower` — this module does not compute that itself, for the
    same dependency-light reason `supports_tool_use` takes its evidence as
    arguments). Empty when neither applies — never a placeholder value, so
    a caller can test `"tool-use" in capability_tags(...)` directly."""
    tags = []
    if supports_tool_use(repo_id, model_type=model_type, architecture=architecture):
        tags.append(TOOL_USE_TAG)
    if has_vision:
        tags.append(VISION_TAG)
    return tuple(tags)


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
    #: capability the machine cannot serve. The FULL name, qualifier and all
    #: ("MLX LM (Apple Silicon)"), and it has exactly one home: the Preferences
    #: engine picker, where the reader is choosing between backends and the
    #: qualifier is the difference between the options.
    label: str
    #: The same backend without the qualifier ("MLX LM"), for everywhere else.
    #:
    #: A FIELD, not the label with the brackets stripped off. A regex would make
    #: the short name a side effect of how someone punctuated the long one, and
    #: the first runner whose name legitimately contains brackets would lose
    #: half of it with nothing failing. It is also the vocabulary this app
    #: already speaks informally — `skills/fused-render-ai/SKILL.md`'s runner
    #: table has been writing these names for as long as it has existed.
    #:
    #: The qualifier is noise anywhere the reader is not CHOOSING: a card
    #: saying "MLX FLUX (Apple Silicon)" tells someone on a Mac nothing they did
    #: not know, and costs roughly double the width of a tag that has to fit
    #: beside the task and the size.
    #:
    #: **Both names are PRODUCT NAMES, and they are Title Case with acronyms
    #: left uppercase — "MLX Whisper", "Diffusers", "Faster Whisper".** The AI
    #: Models page prints them all side by side in one column, so a name that
    #: keeps its upstream punctuation reads as a different KIND of thing than
    #: its neighbours rather than as a faithful citation; `faster-whisper` sat
    #: next to `Diffusers` and `MLX FLUX` and looked like a package, not a
    #: choice. The exact upstream spelling is not lost by this: the card's
    #: library tag one column over is the literal identifier (`ctranslate2`,
    #: `diffusers`, `mlx`, `gguf`) and stays lowercase, which is the split that
    #: earns the rename. Anywhere a runner is IDENTIFIED rather than named —
    #: `code`, the folder, the pyproject, the catalog keys — the upstream
    #: spelling is load-bearing and must not be touched.
    short_label: str = ""
    #: The engine FAMILY — the same name with any hardware qualifier gone
    #: ("Diffusers" for all three Diffusers rows, "llama.cpp" for both
    #: llama.cpp ones). For the one surface whose statement is about the FILE
    #: rather than about this machine: the Local card's engine tag.
    #:
    #: **A tag that is a format claim must not carry a hardware qualifier.**
    #: The tag says "Diffusers", meaning "these weights are safetensors a
    #: Diffusers pipeline opens" — and all three Diffusers rows read the
    #: identical file, so "(ROCm)" there answers nothing a reader could have
    #: asked about a download sitting on disk, and leaks which machine happens
    #: to be looking at it into a sentence about the model. The card keeps the
    #: hardware-qualified `short_label` on the tag's hover and its aria-label,
    #: so which build would actually load it is one hover away rather than
    #: gone. Everywhere the statement IS about this machine — the loaded card,
    #: the job row, the "Using …" line — still reads `short`.
    #:
    #: A FIELD, not `short_label` with a trailing parenthetical stripped, for
    #: exactly the reason `short_label` is not `label` stripped: a regex makes
    #: the name a side effect of how somebody punctuated another one, and the
    #: first family whose own name contains brackets loses half of it with
    #: nothing failing. `test_every_runner_names_its_family_with_no_hardware_in_it`
    #: requires it on every registered row and forbids the qualifiers by name.
    family_label: str = ""
    #: What using this backend is LIKE, for the page to say before anything is
    #: loaded. A standing fact about the runner, never a claim about this
    #: machine — the device a model actually got is the worker's to report
    #: (`worker_base.STATE["device"]`) and is not knowable until one has run.
    #:
    #: It exists because for several rows the honest answer is "this may be a
    #: great deal slower, or larger, than you expect, and here is why" — the
    #: accelerated Diffusers rows' download, `mflux-image`'s memory ceiling.
    #: Empty for a runner with nothing surprising to say.
    #:
    #: **ONE OR TWO SHORT SENTENCES, fixed shape: what it does and where it
    #: runs, then the one cost or caveat worth a reader's attention.** No
    #: em-dash asides, no parenthetical hedges, no cross-references to another
    #: row, no packaging or provenance trivia — the reasoning behind a fact
    #: belongs in this comment, not in the sentence the reader sees. Every
    #: row's own `note` comment refers back to this one rather than restating
    #: it.
    #:
    #: **It renders under that engine's row on the AI Models page's Engines
    #: tab** (D315), beneath the engine picker, and only for the runner
    #: actually serving the capability. It spent a while over the Discover
    #: tab's capability sections instead, which was wrong twice: only some
    #: runners have a note, so those sections were blotchy and the sentences
    #: read as noise; and the `mflux-image` one is a CAUTION about a choice —
    #: the thing that tells a 16GB Mac to go back to Diffusers — which belongs
    #: beside the control that makes that switch, not over a grid of
    #: downloads.
    note: str = ""
    #: Extra Hub `filter=` tags a search must carry when THIS is the runner
    #: actually serving the capability — empty for every runner whose format
    #: needs no such narrowing (D412).
    #:
    #: **Why this is a runner field and not a hard-coded pair in
    #: `hub_models.py`.** That module's whole design is "adding a runner needs
    #: no edit here" (`supported_tags`'s own docstring, pinned by
    #: `tests/test_hub_models.py`); a capability with two formats behind it
    #: (`llamacpp-text`'s GGUF against `mlx-text`'s safetensors) is the first
    #: time that has mattered, since every earlier
    #: multi-runner capability shares one format across its variants. Reading
    #: the filter off the ACTIVE runner rather than hard-coding "text
    #: generation means gguf" keeps that property: a future format-specific
    #: runner declares its own tag here, and the module stays the same.
    #:
    #: **Deliberately keyed to the SERVING runner, which is host-dependent —
    #: the one exception to this module's "search does not depend on the
    #: host" rule** (see `hub_models.py`'s own docstring), and that is a
    #: considered exception rather than a quiet one: a search result this
    #: engine's own picker cannot resolve is not actionable HERE regardless
    #: of what a different machine's active engine could do with it, the
    #: same argument `_UNRUNNABLE_LIBRARIES` already makes about FORMAT
    #: (as opposed to hardware availability, which the rest of that
    #: docstring's rule is actually about). Two people running the identical
    #: query see different rows only when they made different, visible engine
    #: choices in Preferences — never for a reason neither could see.
    hub_filter_tags: tuple[str, ...] = ()
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
            return Availability(False, f"the {self.short} runner is not built yet")
        return self._available()

    @property
    def short(self) -> str:
        """`short_label`, falling back to the full one.

        The fallback is for a Runner built somewhere that has no opinion about
        display — a test's stand-in, mostly. A REGISTERED runner must set it,
        which `test_every_runner_has_both_names_and_they_differ_only_by_the_qualifier`
        requires: degrading to the long name is a cosmetic wart, degrading to ""
        would be a blank tag.
        """
        return self.short_label or self.label

    @property
    def family(self) -> str:
        """`family_label`, falling back to the short name.

        Same fallback and same reason as `short`: a Runner built in a test has
        no opinion about display, and a blank tag is worse than a tag with a
        qualifier on it. A REGISTERED runner must set the field, which
        `test_every_runner_names_its_family_with_no_hardware_in_it` requires.
        """
        return self.family_label or self.short

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
        f"needs Apple Silicon (this is {system.lower()}/{machine})",
    )


def _always() -> Availability:
    """torch + diffusers and CTranslate2 build on every platform we ship.

    Whether the machine is FAST enough is a different question, and not one to
    refuse on — a model answering slowly on a CPU is a model answering, and the
    device is reported (`worker_base.STATE["device"]`) so the page can say which
    case it is.
    """
    return Availability(True)


def _onnx_platform() -> Availability:
    """`onnx-embed`'s supported platforms — a HARD exclusion, meaning "there is
    no wheel to install", the same kind of claim `_llamacpp_platform` makes.

    Narrower by ARCHITECTURE than the withdrawn torch gate this replaces
    (Windows or Linux on ANY machine, plus Apple Silicon), and deliberately so:
    PyPI's `onnxruntime` 1.29 publishes `macosx_14_0_arm64`,
    `manylinux_2_28_{x86_64,aarch64}`, `win_amd64` and `win_arm64` — read off
    the release's own wheel list rather than assumed — and nothing else. There
    is no macOS x86_64 build and no Linux riscv64 one, so both are excluded
    because `uv sync` has NOTHING to install, an immediate total failure the
    moment a machine reaches this row.

    Checked by architecture per OS for `_llamacpp_platform`'s reason exactly:
    `machine()` spells one architecture differently per OS (`"AMD64"` on
    Windows, `"x86_64"` on Linux, `"arm64"` on Darwin), so each branch checks
    its own OS's spelling rather than one shared tuple that would silently stop
    matching the moment a branch used the wrong OS's name for it.

    Note this row is WIDER than `_llamacpp_platform` on Windows — onnxruntime
    publishes `win_arm64` and the llama.cpp index does not — so a Windows ARM64
    machine with no local text engine still gets local embeddings.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Linux" and machine in ("x86_64", "aarch64"):
        return Availability(True)
    if system == "Windows" and machine in ("AMD64", "ARM64"):
        return Availability(True)
    if system == "Darwin" and machine == "arm64":
        return Availability(True)
    if system == "Darwin":
        return Availability(
            False,
            f"needs Apple Silicon (this is {system.lower()}/{machine})",
        )
    if system == "Linux":
        return Availability(
            False,
            f"needs x86_64 or aarch64 (no {machine} wheel for Linux)",
        )
    if system == "Windows":
        return Availability(
            False,
            f"needs an x86_64 or ARM64 machine (no {machine} wheel for Windows)",
        )
    return Availability(
        False,
        f"needs Windows, Linux, or Apple Silicon macOS (this is {system.lower()}/{machine})",
    )


def _llamacpp_platform() -> Availability:
    """`llamacpp-text`'s supported platforms — a HARD exclusion, meaning "there
    is no wheel to install", not a business decision about where to distribute.

    The maintainer's CPU wheel index (`llamacpp_text/pyproject.toml`, D411)
    publishes `py3-none` wheels for a specific, checked tag set — re-verified
    directly against the index listing for the pinned `0.3.29` rather than
    trusted from an earlier pass: `macosx_11_0_arm64`,
    `manylinux2014_{x86_64,aarch64}.manylinux_2_17_*`,
    `musllinux_1_2_{x86_64,aarch64}`, `win_amd64`, and `linux_riscv64`. **There
    is no macOS x86_64 tag, and no `win_arm64` tag, at all.** So both are
    excluded because `uv sync` has NOTHING to install — an immediate, total
    failure the moment a machine reaches this row, not a slow or a degraded one.

    **This function is now the widest local text engine on the two platforms
    that are not Apple Silicon, which raises the cost of it being wrong** (D416).
    Until the transformers rows were removed, a machine this refused still had
    `transformers-text` — `_transformers_platform` said yes to Windows and Linux
    on ANY architecture — so an over-strict tag set here cost a user speed, not
    the capability. It no longer does: a Windows ARM64 box, or a Linux machine
    outside x86_64/aarch64/riscv64, now has no local text generation at all and
    falls back to `claude-cli`. That is the honest answer rather than a
    regression — the transformers rows advertised those machines a Load button
    whose `uv sync` would have had to find a torch wheel for the same
    architecture — but it is the reason `test_every_shipped_platform_keeps_a_local_text_engine`
    exists and enumerates the platforms this app actually ships to.

    **Checked by ARCHITECTURE, not merely by OS** — `system in ("Windows",
    "Linux")` alone (the shape this function used before) advertises a Load
    button on a Windows ARM64 box (Surface Pro X and similar) or any Linux
    machine outside the three architectures actually published, exactly the
    defect this function's own macOS branch was written to avoid. `machine()`
    spells the same architecture differently per OS — `"AMD64"` on Windows,
    `"x86_64"` on Linux, `"arm64"` on Darwin — so each OS branch checks its own
    OS's spelling rather than one shared tuple of names that would silently
    stop matching the moment a branch used the wrong OS's spelling for it.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Linux" and machine in ("x86_64", "aarch64", "riscv64"):
        return Availability(True)
    if system == "Windows" and machine == "AMD64":
        return Availability(True)
    if system == "Darwin" and machine == "arm64":
        return Availability(True)
    if system == "Darwin":
        return Availability(
            False,
            "needs Apple Silicon (no macOS x86_64 build)",
        )
    if system == "Windows":
        return Availability(
            False,
            "needs an x86_64 machine (win_amd64 only, no win_arm64)",
        )
    if system == "Linux":
        return Availability(
            False,
            f"needs x86_64, aarch64, or riscv64 (no {machine} build for Linux)",
        )
    return Availability(
        False,
        f"needs Windows, Linux, or Apple Silicon macOS (this is {system.lower()}/{machine})",
    )


# -- the accelerator probes ------------------------------------------------------
#
# CUDA and ROCm are OPT-IN rows (the CPU torch runners sit above them and remain
# the default), and both probes are HARD GATES: an accelerated row is selectable
# only where it can actually run. That is not fastidiousness — picking one on a
# machine with no matching device buys a multi-gigabyte wheel that then fails
# several frames inside a runtime library, which is exactly the "advertising is
# a claim" failure `Runner.available` was written for.
#
# **Everything below reads the KERNEL's answer, and reads it at CALL time.**
# Every path is a module-level constant so a test can build a fake sysfs on a
# tmp_path and repoint them — the filesystem analogue of the `registry.platform`
# monkeypatching every other availability test does.
#
# **Stdlib only, no torch, no subprocess.** This module is imported on a page
# render path (`describe`, `describe_engines`, and every `resolve`), so it may
# not import a 2GB framework to ask a question sysfs answers in microseconds,
# and it may not shell out: SPEC.md's ffmpeg rule bars relying on a system
# binary the app does not ship, `nvidia-smi` is not shipped, and a cold one
# costs 50-500ms against ~25µs for the sysfs walk.

#: Where the ROCm probe looks. `/sys/class/kfd` is the amdkfd driver's own
#: topology — the same thing ROCm's runtime enumerates — and `/dev/kfd` plus a
#: render node are the two devices a HIP process opens.
KFD_NODES_DIR = "/sys/class/kfd/kfd/topology/nodes"
KFD_DEVICE = "/dev/kfd"
DRI_DIR = "/dev/dri"
#: The DRM class, which answers two questions: is there an AMD GPU at all when
#: the KFD cannot say (`_amd_gpu_present`), and WHICH render node belongs to it
#: (`_amd_render_nodes`) — both `card*` and `renderD*` appear here, and each
#: carries the PCI `device/vendor` of the card behind it.
DRM_CLASS_DIR = "/sys/class/drm"
#: The PCI vendor id every AMD/ATI GPU reports.
AMD_PCI_VENDOR = "0x1002"

#: The gfx targets the ROCm wheels these runners install were actually built
#: for (torch 2.13 + rocm7.1).
#:
#: **TIED TO THE INDEX URL THE ROCm MANIFESTS PIN, so the two must move
#: together.** A wheel from a different ROCm index has a different set, and the
#: cost of getting this wrong is asymmetric: an unlisted card offered anyway is
#: a ~6GB download that dies inside HIP with "no kernel image is available for
#: execution", several frames below anything this app wrote. So an AMD GPU that
#: is not named here is refused with a reason, not optimistically allowed.
ROCM_TARGETS = frozenset({
    "gfx900", "gfx906", "gfx908", "gfx90a", "gfx942", "gfx950",
    "gfx1030", "gfx1100", "gfx1101", "gfx1102", "gfx1103",
    "gfx1150", "gfx1151", "gfx1200", "gfx1201",
})

#: Where the CUDA probe looks on Linux: the control node and at least one
#: per-GPU node are REQUIRED, and unified memory is checked only for PERMISSION
#: when it happens to exist.
#:
#: **`/dev/nvidia-uvm` is created LAZILY and its absence proves nothing** (D382).
#: `nvidia-modprobe` loads `nvidia_uvm` and makes the node the first time any
#: process creates a CUDA context; the display path needs only `nvidia` and
#: `nvidia_drm`. So a freshly booted desktop that has not run a CUDA program yet
#: has `/dev/nvidiactl` and `/dev/nvidia0` and NO `/dev/nvidia-uvm` while
#: `torch.cuda` works perfectly — the machine this gate was meant to serve, and
#: the one an existence check refused. When the node IS there, `os.access` on it
#: still earns its place: a container given the nodes without the access is
#: exactly the state it reports.
NVIDIA_CONTROL_DEVICE = "/dev/nvidiactl"
NVIDIA_UVM_DEVICE = "/dev/nvidia-uvm"
#: Where `/dev/nvidia0`, `/dev/nvidia1`… live. A constant so the glob below is
#: repointable with the rest.
NVIDIA_DEVICE_DIR = "/dev"
#: WSL2, which has NONE of the nodes above. GPU-PV exposes the card through
#: `/dev/dxg` and ships the CUDA driver library out of `/usr/lib/wsl/lib`, so a
#: WSL2 user whose `torch.cuda` works has no `/dev/nvidiactl` and no
#: `/dev/nvidia0` to show for it (D382). Both are `os.path.exists` and nothing
#: more: dlopening `libcuda.so.1` to be sure would initialise a driver on a page
#: render, which AI-6 bars for the same reason it bars `nvidia-smi`.
WSL_DXG_DEVICE = "/dev/dxg"
WSL_CUDA_LIBRARY = "/usr/lib/wsl/lib/libcuda.so.1"
#: Windows has no device nodes to ask, so the driver's own user-mode CUDA
#: library is the cheapest evidence available. **A HINT, NOT PROOF** — it is
#: installed by the display driver whether or not the GPU is CUDA-capable, and
#: proving it would mean loading it and calling `cuInit`, which is a DLL load
#: and a driver initialisation on a page render. Documented as the weaker gate
#: it is: on Windows a user can still pick a CUDA engine on a machine whose
#: driver is installed but whose GPU is not usable, and finds out at load time
#: with torch's own message. On Linux, where the nodes exist, the gate is real.
NVCUDA_DLL = r"C:\Windows\System32\nvcuda.dll"


def decode_gfx_target(raw: int) -> str | None:
    """An amdkfd `gfx_target_version` -> the target name ROCm wheels are named for.

    `major * 10000 + minor * 100 + step`, with **MINOR AND STEP RENDERED AS
    SINGLE HEX DIGITS**: 90010 is `gfx90a` and not `gfx9010`, 90402 is `gfx942`,
    120000 is `gfx1200`. A decimal render matches nothing in `ROCM_TARGETS`,
    which would refuse every AMD GPU on the argument that it is unsupported —
    the failure mode a wrong decoder has, and why the round-trip test over
    `ROCM_TARGETS` exists.

    None for 0, which is what a CPU node reports (see `_kfd_gfx_targets`).
    """
    if raw <= 0:
        return None
    major, rest = divmod(raw, 10000)
    minor, step = divmod(rest, 100)
    return f"gfx{major}{minor:x}{step:x}"


def _kfd_gfx_targets() -> list[str] | None:
    """Every GPU the amdkfd driver reports, decoded — None when unreadable.

    **EVERY node, not node 0.** Node 0 is the CPU on a machine with a perfectly
    working GPU (`cpu_cores_count 6, simd_count 0, gfx_target_version 0` on the
    box this was written on), so a probe that read only the first node decodes
    a zero target and concludes the machine has no supported GPU. A zero target
    is skipped rather than counted, which is the same fact stated once.

    An empty list means the driver is there and reports no GPU nodes — a
    container without device passthrough. None means the topology itself could
    not be read, which is a different sentence and gets one.
    """
    try:
        entries = sorted(os.listdir(KFD_NODES_DIR))
    except OSError:
        return None
    targets: list[str] = []
    read_any = False
    for entry in entries:
        path = os.path.join(KFD_NODES_DIR, entry, "properties")
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        read_any = True
        for line in text.splitlines():
            key, _, value = line.partition(" ")
            if key != "gfx_target_version":
                continue
            try:
                raw = int(value.strip())
            except ValueError:
                continue
            target = decode_gfx_target(raw)
            if target:
                targets.append(target)
    if entries and not read_any:
        return None
    return targets


def _amd_gpu_present() -> bool:
    """Is there an AMD GPU at all — asked of the DRM class, not of the KFD.

    The fallback for the branch where the KFD cannot answer, because a missing
    `/dev/kfd` has two very different causes: the amdkfd half of amdgpu is not
    loaded (an action — `modprobe amdgpu`, or reboot after a driver update), or
    there is no AMD GPU in the machine (a fact). One reason string for both
    would be wrong for whichever reader it was not written for.

    ~41µs, and only on the failure branch — the ordinary answer never runs it.
    """
    for path in glob.glob(os.path.join(DRM_CLASS_DIR, "card*", "device", "vendor")):
        try:
            with open(path, encoding="utf-8") as handle:
                if handle.read().strip().lower() == AMD_PCI_VENDOR:
                    return True
        except OSError:
            continue
    return False


def _amd_render_nodes() -> list[str]:
    """The `/dev/dri/renderD*` nodes belonging to an AMD card — HIP's second device.

    **Pinned to the AMD card, not to any render node that happens to open**
    (D382). The first version accepted any readable `renderD*`, which is wrong on
    every hybrid machine: an Intel iGPU's `renderD128` is world-openable on most
    distributions, so a box with an open Intel node and a restricted AMD one
    passed the gate on a device HIP will never touch, and the ~6GB install then
    failed when HIP opened the node it actually needed. `_amd_gpu_present` already
    reads `device/vendor` out of the DRM class; the render nodes are in the same
    class and carry the same file, so the vendor answers WHICH node too.

    Returns the device paths under `DRI_DIR` (sorted), not the sysfs entries —
    the caller asks `os.access` of the thing HIP opens.
    """
    nodes = []
    pattern = os.path.join(DRM_CLASS_DIR, "renderD*", "device", "vendor")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as handle:
                vendor = handle.read().strip().lower()
        except OSError:
            continue
        if vendor != AMD_PCI_VENDOR:
            continue
        node = os.path.join(DRI_DIR, os.path.basename(os.path.dirname(
            os.path.dirname(path))))
        if os.path.exists(node):
            nodes.append(node)
    return nodes


def _rocm() -> Availability:
    """The ROCm torch runners: Linux, an AMD GPU, and a gfx the wheel supports.

    **Never cached, and that is deliberate.** Every failure below is one a user
    FIXES WHILE THE APP IS RUNNING — `modprobe amdgpu`, plugging in an eGPU,
    restarting a container with `--device /dev/kfd`, being added to the render
    group and logging back in. A cached "no /dev/kfd" that survived the fix is
    precisely the bug the reason string exists to prevent: the sentence tells
    someone what to do and then the app refuses to notice they did it. The cost
    is ~22µs for the whole probe on the machine it was written on (~40µs more
    for the DRM fallback, which only runs on the failure branch), against a
    resolution that happens per load, per download and per page render — not per
    token. `_cuda` measures ~41µs the same way. An `lru_cache` would also make
    test ORDER significant against the monkeypatch style every other
    availability test here uses, which is a second reason of its own.
    `preferred_code` declines caching on the same grounds.

    **Permission is asked of the kernel, never modelled.** `os.access` with
    `R_OK | W_OK`, because a group-membership or mode-arithmetic check gets real
    machines wrong in BOTH directions: this box's `/dev/kfd` is `crw-rw-rw-`
    while the user is in neither render nor video (a group check would refuse a
    working machine), and its `card1` is `crw-rw----+` — a POSIX ACL, invisible
    to mode arithmetic, which would refuse a machine the ACL permits.
    """
    system = platform.system()
    machine = platform.machine()
    if system != "Linux":
        return Availability(
            False,
            f"needs Linux (this is {system.lower()}/{machine})",
        )
    if not os.path.exists(KFD_DEVICE):
        if _amd_gpu_present():
            return Availability(
                False,
                f"needs the amdgpu kernel driver ({KFD_DEVICE} is missing; "
                "load it with `modprobe amdgpu`, or reboot after a driver "
                "update)",
            )
        return Availability(
            False,
            f"needs an AMD GPU (this is {system.lower()}/{machine})",
        )
    if not os.access(KFD_DEVICE, os.R_OK | os.W_OK):
        return Availability(
            False,
            f"needs permission to use the GPU ({KFD_DEVICE} is not readable "
            "and writable by you; add your user to the group that owns it, "
            "usually `render`, then log out and back in)",
        )
    # HIP opens `/dev/kfd` AND the card's own render node, and the two failures
    # here are DIFFERENT SENTENCES because they have different fixes. A node that
    # is not THERE cannot be fixed by joining a group — that is a container
    # started without `--device /dev/dri`, or a `/dev/dri` the amdgpu driver has
    # not populated — while a node that is there and closed is precisely the
    # group case. One sentence for both sent half its readers after a `usermod`
    # that could not have helped.
    render_nodes = _amd_render_nodes()
    if not render_nodes:
        return Availability(
            False,
            f"needs the AMD GPU's render node (no {DRI_DIR}/renderD* device "
            "belongs to an AMD card here; a container started without "
            "`--device /dev/dri` looks like this)",
        )
    if not any(os.access(path, os.R_OK | os.W_OK) for path in render_nodes):
        return Availability(
            False,
            f"needs permission to use the GPU ({render_nodes[0]} is the AMD "
            "card's render node and is not readable and writable by you; add "
            "your user to the `render` group, then log out and back in)",
        )
    targets = _kfd_gfx_targets()
    if targets is None:
        return Availability(
            False,
            f"needs the amdgpu driver's topology ({KFD_NODES_DIR} could not "
            "be read, so the GPU cannot be identified)",
        )
    if not targets:
        return Availability(
            False,
            "needs an AMD GPU the kernel can see (the amdgpu driver reports "
            "CPU nodes only; a container started without `--device /dev/kfd "
            "--device /dev/dri` looks like this)",
        )
    if not any(target in ROCM_TARGETS for target in targets):
        found = ", ".join(sorted(set(targets)))
        return Availability(
            False,
            f"needs a supported AMD GPU ({found} is not supported by the "
            "ROCm build this runner installs)",
        )
    return Availability(True)


def _cuda() -> Availability:
    """The CUDA torch runners: an NVIDIA GPU whose driver is loaded and usable.

    A HARD GATE, like `_rocm` and for the same reason — an accelerated row that
    is selectable on a machine with no NVIDIA GPU is a multi-gigabyte download
    that fails at load. Not cached, for `_rocm`'s reasons exactly (an eGPU, a
    container restart, a driver reloaded — all fixed while the app runs).

    **Three shapes of NVIDIA machine, and only one of them has device nodes.**
    Ordinary Linux has `/dev/nvidiactl` + `/dev/nvidia[0-9]*`; WSL2 has neither
    and works anyway (`/dev/dxg`, and the driver's `libcuda.so.1` under
    `/usr/lib/wsl/lib`); Windows has no nodes at all and is gated on the
    driver's own DLL. Absence of the Linux nodes is therefore not evidence
    against WSL2, which is why that branch is asked FIRST rather than as a
    fallback after a refusal has already been written.

    **No `nvidia-smi`.** SPEC.md's rule about system binaries the app does not
    ship, and a cold `nvidia-smi` is 50-500ms on a per-page-render path against
    ~25µs of `os.access`.

    **No driver-version floor.** The floor belongs to the CUDA the runner's
    wheel pins, this module cannot read that from a file it does not have, and
    guessing high disables machines that work. The wheel's own error is the
    better reporter of a driver that is genuinely too old.
    """
    system = platform.system()
    if system == "Linux":
        # WSL2 FIRST, because it has none of the nodes below and torch.cuda
        # works there anyway (D382). GPU-PV projects the Windows driver into the
        # guest as `/dev/dxg` plus a `libcuda.so.1` under `/usr/lib/wsl/lib`, so
        # a WSL2 user was told "there is no /dev/nvidiactl on this machine",
        # which was true and beside the point, and could not select the engine
        # at all. Two `os.path.exists` and no dlopen — see the constants.
        if os.path.exists(WSL_DXG_DEVICE) and os.path.exists(WSL_CUDA_LIBRARY):
            return Availability(True)
        gpus = glob.glob(os.path.join(NVIDIA_DEVICE_DIR, "nvidia[0-9]*"))
        if not os.path.exists(NVIDIA_CONTROL_DEVICE) or not gpus:
            return Availability(
                False,
                f"needs an NVIDIA GPU (no {NVIDIA_CONTROL_DEVICE} or "
                "/dev/nvidia0 on this machine)",
            )
        unusable = [path for path in [NVIDIA_CONTROL_DEVICE, *sorted(gpus)]
                    if not os.access(path, os.R_OK | os.W_OK)]
        if unusable:
            return Availability(
                False,
                f"needs permission to use the GPU ({unusable[0]} is not "
                "readable and writable by you; this is usually a container "
                "missing `--gpus all`, or a device-permission rule)",
            )
        # …and unified memory, checked for PERMISSION and never for EXISTENCE
        # (D382). `nvidia_uvm` is a separate module that `nvidia-modprobe` loads
        # the first time a process creates a CUDA context, so a freshly booted
        # desktop that has not run a CUDA program yet has the GPU nodes, no
        # `/dev/nvidia-uvm`, and a working `torch.cuda`. Refusing on its absence
        # greyed out both CUDA rows there and blamed "a driver update without a
        # reboot" — the opposite of what had happened. A node that IS present and
        # closed is still worth a sentence: that is a container handed the
        # devices without the access, which no amount of waiting fixes.
        if os.path.exists(NVIDIA_UVM_DEVICE) and not os.access(
                NVIDIA_UVM_DEVICE, os.R_OK | os.W_OK):
            return Availability(
                False,
                f"needs permission to use the GPU ({NVIDIA_UVM_DEVICE} is not "
                "readable and writable by you; this is usually a container "
                "missing `--gpus all`, or a device-permission rule)",
            )
        return Availability(True)
    if system == "Windows":
        if not os.path.isfile(NVCUDA_DLL):
            return Availability(
                False,
                f"needs an NVIDIA GPU with its driver installed (the CUDA "
                f"library is not at {NVCUDA_DLL})",
            )
        return Availability(True)
    return Availability(
        False,
        "needs an NVIDIA GPU (published for Windows and Linux only)",
    )


#: Where the Vulkan LOADER lives on Linux — a handful of fixed paths rather
#: than one, because unlike CUDA/ROCm's kernel device nodes this is a
#: userspace `.so` a distro installs wherever ITS libdir convention says:
#: Debian/Ubuntu use the multiarch triplet, Fedora/RHEL/openSUSE use `lib64`,
#: Arch/Manjaro use a flat `/usr/lib`. `ctypes.util.find_library` would answer
#: this properly but shells out to `ldconfig` (or dlopens outright), either of
#: which `_cuda`'s docstring already rules out for this module — a handful of
#: `os.path.exists` checks stays inside the same "stdlib only, no subprocess,
#: no dlopen" rule its neighbours already follow.
VULKAN_LOADER_PATHS = (
    "/usr/lib/x86_64-linux-gnu/libvulkan.so.1",  # Debian / Ubuntu multiarch
    "/usr/lib64/libvulkan.so.1",                  # Fedora / RHEL / openSUSE
    "/usr/lib/libvulkan.so.1",                    # Arch / Manjaro
)
#: Where a Vulkan ICD — the GPU driver's own entry point — registers itself on
#: Linux: the loader's own manifest search path (LunarG's Vulkan-Loader
#: `LoaderDriverInterface.md`, "Driver Discovery on Linux", fetched and read
#: directly rather than assumed), narrowed to the two directories a distro
#: package or a container actually writes into. `/etc/vulkan/icd.d` is
#: searched AHEAD of `/usr/share/vulkan/icd.d` in the real loader and is where
#: a container image commonly bind-mounts a driver in, so both are checked
#: rather than only the share directory a bare-metal install uses.
VULKAN_ICD_DIRS = ("/etc/vulkan/icd.d", "/usr/share/vulkan/icd.d")
#: The Windows analogue of `NVCUDA_DLL`: the loader DLL a GPU driver installer
#: places in `System32`, the same "hint, not proof" the CUDA gate already
#: documents (installed by the display driver, not proof a device answers it).
VULKAN_DLL = r"C:\Windows\System32\vulkan-1.dll"


def _vulkan() -> Availability:
    """`llamacpp-text-vulkan`'s supported platforms and usable devices — one
    gate, unlike `_cuda`/`_rocm` beside it, because this row's own wheel
    tag set (installability, `_llamacpp_platform`'s question) and its usable
    hardware (`_cuda`/`_rocm`'s question) are BOTH narrower than anything else
    in this table, and answering them separately would need two functions
    that always run together.

    **The published wheel exists on exactly two platforms.** The vulkan index
    (`llamacpp_text_vulkan/pyproject.toml`) publishes `manylinux2014_x86_64`
    and `win_amd64` for `0.3.29` and NOTHING else — no macOS build at all
    (Apple Silicon already gets GPU acceleration through the CPU index's
    Metal-linked wheel, which is the whole reason this variant exists only
    for NVIDIA/AMD), no Linux aarch64, no Windows ARM64. Checked by
    architecture per OS exactly as `_llamacpp_platform` now is, and for the
    same reason: `system in ("Windows", "Linux")` alone would advertise a
    Load button on a Windows ARM64 box or a Linux aarch64 one, neither of
    which this specific index ships a wheel for.

    **A wheel existing is not the same question as it being USABLE, and this
    gate answers both because a Vulkan wheel supplies neither the loader nor
    the driver ICD — those come from the GPU driver, not from `pip`.** Two
    facts, established directly against the downloaded `0.3.29` wheels rather
    than assumed, decide how strict each half of the check must be:

    1. **The loader is a HARD link dependency, not a `dlopen`.** The wheel's
       own `libggml-vulkan.so` declares `DT_NEEDED libvulkan.so.1` (read
       straight out of its ELF `.dynamic` section), and `libggml.so` and
       `libllama.so` both declare `DT_NEEDED libggml-vulkan.so.0` in turn — so
       the whole chain fails to load, and `import llama_cpp` raises, the
       moment the loader is missing, regardless of whether a GPU is even
       asked about. The Windows DLL carries the identical dependency
       (`ggml-vulkan.dll` imports `vulkan-1.dll`, read from its PE import
       table), so the same hard-failure fact holds on both platforms this
       row ships for. This is why a MISSING LOADER is refused here rather
       than left to the worker's own error the way `_cuda`'s driver-version
       floor is: `_cuda`/`_rocm`'s missing-device case still lets `import
       torch` succeed and fail later inside a CUDA/HIP call, while a missing
       Vulkan loader here fails at the very first `import`, which is a worse
       and more confusing failure to hand back than a `Runner.available`
       reason string that names the actual cause.
    2. **A missing ICD (no GPU driver registered) is NOT a load failure —
       ggml's backend loader falls back to ITS OWN bundled CPU backend**
       (`libggml-cpu.so`/`ggml-cpu.dll`, present in both wheels alongside the
       74MB Vulkan one) when Vulkan enumerates zero devices, so `import
       llama_cpp` succeeds and inference still runs, just on the CPU. That
       is not a reason to pass the gate anyway: a machine in that state gets
       every byte of an 8x larger download (182MB vs. the CPU index's
       22.5MB Linux wheel) for the SAME CPU-only outcome the smaller
       `llamacpp-text` row already offers, which is exactly the "advertising
       a claim that buys nothing" case `_cuda`/`_rocm`'s device checks
       already exist to refuse.

    Both facts were established by parsing the actual `0.3.29` wheels
    (`zipfile` for the contents, a small ELF/PE parser for the dependency
    tables) on 2026-08-21 — not by reading ggml's source or assuming dynamic
    backend loading behaves like a plugin system, which it does NOT here: the
    Vulkan backend is linked in, not `dlopen`ed at runtime.

    **Not cached, for `_rocm`'s reasons exactly** — a loader package or a
    driver installed while the app is running is a fix that must be seen
    without a restart.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Linux" and machine == "x86_64":
        if not any(os.path.exists(path) for path in VULKAN_LOADER_PATHS):
            return Availability(
                False,
                "needs the Vulkan loader (no libvulkan.so.1 found on this "
                "distribution's usual library paths; install "
                "`vulkan-loader`/`libvulkan1`)",
            )
        if not any(
            os.path.isdir(d) and glob.glob(os.path.join(d, "*.json"))
            for d in VULKAN_ICD_DIRS
        ):
            return Availability(
                False,
                "needs a Vulkan GPU driver (the loader is installed but no "
                "driver ICD is registered under /etc/vulkan/icd.d or "
                "/usr/share/vulkan/icd.d; install your GPU vendor's Vulkan "
                "driver, e.g. `mesa-vulkan-drivers`)",
            )
        return Availability(True)
    if system == "Windows" and machine == "AMD64":
        if not os.path.isfile(VULKAN_DLL):
            return Availability(
                False,
                f"needs a GPU with its Vulkan driver installed (the loader "
                f"library is not at {VULKAN_DLL})",
            )
        return Availability(True)
    return Availability(
        False,
        "needs a Windows or Linux x86_64 machine (the llama.cpp Vulkan wheel "
        "publishes manylinux2014_x86_64 and win_amd64 only)",
    )


def _directml() -> Availability:
    """`onnx-embed-directml`'s platform — Windows on x86_64, and that is all.

    **The simplest probe in this section, and that is the right answer rather
    than an omission.** `_vulkan` beside it is long because a Vulkan wheel
    supplies neither the loader nor the driver ICD, so `import llama_cpp` HARD
    FAILS on a machine missing either — there is a real, catastrophic failure to
    gate against. DirectML has no equivalent: `onnxruntime-directml` links
    against `DirectML.dll` and `d3d12.dll`, both of which ship with Windows
    itself from Windows 10 1903 onwards, and DirectML runs on ANY Direct3D 12
    adapter — a discrete NVIDIA or AMD card, Intel Arc, or the integrated GPU
    every desktop Windows machine has. There is no "no driver installed" state
    to detect and nothing to `dlopen`-check.

    **So "plus a present GPU" is not modelled as a second probe.** Enumerating
    D3D12 adapters needs a `ctypes` call into `dxgi.dll` — a system binary
    question SPEC.md's rule keeps out of a per-page-render path — and every
    answer it could give on a machine that reaches this row is "yes". A probe
    that always answers yes is a probe whose failure mode is entirely its own
    bugs. The row is also OPT-IN from the Engines tab and sits below the CPU row,
    so `auto` never reaches it: nobody lands here without choosing to.

    `win_amd64` only, from the distribution's own wheel list (checked against
    `onnxruntime-directml` 1.24.4, which publishes `cp311`-`cp314` for
    `win_amd64` and no other tag) — so a Windows ARM64 machine is refused here
    even though plain `onnxruntime` serves it.

    Not cached, for `_rocm`'s reasons: the platform cannot change under a
    running app, but every other probe in this section declines caching and an
    `lru_cache` on one of them would make test ORDER significant against the
    monkeypatch style these tests use.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Windows" and machine == "AMD64":
        return Availability(True)
    if system == "Windows":
        return Availability(
            False,
            "needs an x86_64 machine (onnxruntime-directml publishes "
            "win_amd64 only, no win_arm64)",
        )
    return Availability(
        False,
        f"needs Windows (this is {system.lower()}/{machine})",
    )


# The table. Ordered, and first-match-wins per capability — which is what lets
# TWO runners serve one: MLX takes Apple Silicon when available, and the row
# below it serves Windows and Linux plus the Apple Silicon fallback. All four
# multi-runner capabilities (text generation, image generation, speech to text,
# embeddings) are arranged that way. The ordering is the whole mechanism, so the rows are
# not sorted alphabetically and must not be — it is also the DEFAULT that a
# user's engine preference overrides, so a re-order silently re-decides every
# machine set to "auto", which is all of them until somebody chooses otherwise.
#
# **A capability's rows are split PER HARDWARE, and the unaccelerated build is
# the default.** One `diffusers-image` row that installed whichever wheel index
# a manifest happened to pin made the accelerator an invisible property of the
# build: a machine got CUDA or it got the CPU and nothing on the page said
# which, so the name was honest on exactly one class of hardware. There are now
# three Diffusers rows — CPU, CUDA, ROCm — and two llama.cpp ones; the
# unaccelerated one sits FIRST in each family and is what every "auto" machine
# resolves to, and the accelerated ones are opt-in from the Engines tab and
# gated on the device actually being there (`_cuda`, `_rocm`, `_vulkan`).
# Unaccelerated-by-default is the conservative half of that decision: the
# accelerated wheels are much larger downloads with a hardware requirement, and
# a default that silently required one would fail on the machines least able to
# explain why. `code` on each family's original row is UNCHANGED, so a stored
# preference naming `diffusers-image` keeps meaning what it meant.
#
# **A code that is REMOVED is a different matter, and `resolve()` is where it is
# handled rather than here** (D416): the three `transformers-text*` codes were
# registered rows until D416 and a synced prefs.json can still name one. Nothing
# in this table pretends they exist — a stale preference is reported as unknown
# and the ordering decides instead, which is the same path a preference written
# by a NEWER build already took. See `resolve()`'s third bullet, and
# `test_a_removed_engine_code_in_prefs_degrades_to_the_ordering`.
#
# **A hardware variant carries its accelerator in BOTH names**, so `label` and
# `short_label` are equal on all three Diffusers rows and both llama.cpp ones
# ("Diffusers (CUDA)", "llama.cpp (CPU)"). The short name is what the Local card
# and `servingLine` print, and three engines whose short names are all
# "Diffusers" would render as one engine on every surface but the picker. The
# qualifier names the BUILD rather than the reader's machine, which is why the
# CPU rows keep it on a Mac that runs them on the GPU — and why naming a row
# after its FORMAT instead does not work: "llama.cpp (GGUF)" beside "llama.cpp
# (Vulkan)" qualified the wrong axis, since both rows read GGUF through the same
# `runners/llama_text.py`. The MLX rows keep a PLATFORM qualifier on the long
# name only — a bracketed qualifier in the SHORT name is therefore the marker
# of a hardware variant, and the Apple-only rows stay visually distinct from
# them.
_RUNNERS: tuple[Runner, ...] = (
    Runner(
        code="mlx-text",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "mlx_text"),
        label="MLX LM (Apple Silicon)",
        short_label="MLX LM",
        family_label="MLX LM",
        # ONE OR TWO SENTENCES (see `llamacpp-text`'s comment above). The
        # download fact is `mlx_text/pyproject.toml`'s own: every model in
        # this app's MLX text catalog is a unified vision-language checkpoint,
        # and `download()` pulls the whole repo with no allow/ignore
        # patterns — mlx-vlm reads only the language tower for a text-only
        # chat, but the vision one comes down anyway.
        note="Generates text on the GPU. Downloads the full checkpoint, "
             "including vision weights it doesn't use.",
        _available=_apple_silicon,
    ),
    # GGUF via llama.cpp (SPEC AI-11, AI-2a, D411) — and since D416 the ONLY
    # local text engine on Windows and Linux, so this row is what a bare "auto"
    # resolves to there.
    #
    # **That is a change of role this comment used to argue against, and the
    # argument is worth keeping rather than deleting.** The row shipped BELOW
    # three `transformers-text*` rows precisely so `auto` could never reach it:
    # `llamacpp_text/pyproject.toml` records that the maintainer's wheel index
    # is a coin-flip per release on macOS arm64 (4 of 16 sampled releases fail
    # `testzip()`), and a capability whose INSTALL can silently fail is a poor
    # thing to hand a machine that did not ask for it. D416 removed the rows
    # that made "never a fallthrough" possible, on a measurement this engine won
    # outright — 4.2x the throughput of transformers on this GPU tier, 2.4x on
    # CPU, at a third of the download and a third of the memory — so the choice
    # was between a default that is much better and occasionally uninstallable,
    # and keeping an engine that lost on every axis in order to preserve a
    # safety property. The default moved. What makes that affordable rather than
    # reckless is that the failure is LOUD and at install time: `uv sync`
    # reports a corrupt wheel verbatim through `envinstall` (PY-18), which is a
    # first-run error with a message, not a wrong answer later. The pinned
    # `0.3.29` Linux and Windows wheels were verified intact; macOS arm64 keeps
    # `mlx-text` ahead of this row anyway, which is where the audit's failures
    # were concentrated.
    #
    # `_llamacpp_platform`, not `_always`: the pin this runner declares is CPU
    # wheels, but NOT on every platform Diffusers CPU and Faster Whisper reach —
    # the maintainer's index publishes no macOS x86_64 tag at all, so Intel
    # macOS is a hard exclusion (see that function).
    Runner(
        code="llamacpp-text",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "llamacpp_text"),
        # "(CPU)" rather than the old "(GGUF)": the FORMAT is not what
        # distinguishes this row from its neighbour — `llamacpp-text-vulkan`
        # reads GGUF through the same shared `runners/llama_text.py` — and the
        # wheel index is, so the qualifier names the thing a reader is choosing
        # between. Both names carry it; see the table's naming note above.
        #
        # **"(CPU)" names the BUILD, not a prediction about the device.** It is
        # the maintainer's `whl/cpu` index — the wheel with no CUDA, ROCm or
        # Vulkan backend compiled in — and on Apple Silicon that same index's
        # wheel links `libggml-metal.dylib`, so this row runs on the GPU there.
        # What device a model actually got is the worker's to report; the `note`
        # says the Mac case out loud so the two never disagree. (The removed
        # `transformers-text` row set this precedent with its own `whl/cpu`
        # pin resolving to an MPS-capable macOS wheel, D382.)
        label="llama.cpp (CPU)",
        short_label="llama.cpp (CPU)",
        family_label="llama.cpp",
        # ONE OR TWO SENTENCES, fixed shape: what it does and where it runs,
        # then the one cost or caveat worth a reader's attention — no packaging
        # trivia, no cross-references, no reasoning about why the sentence
        # reads the way it does (that argument lives in this comment instead).
        #
        # **It names the Apple Silicon GPU, because this row USES it** — the
        # same correction D382 made. `llama_text.load()` reports device "gpu"
        # when the Metal backend takes the layers, so a note that mentioned
        # only the download would have had the Engines tab implying a CPU engine
        # while the loaded card beside it said `gpu`.
        note="Runs GGUF models on the CPU, or Apple Silicon's GPU. Small "
             "download.",
        _available=_llamacpp_platform,
        # A text-generation search result this engine cannot resolve at all
        # (a plain safetensors repo) is not actionable here — see
        # `hub_filter_tags`'s own docstring for why this is a runner field.
        hub_filter_tags=("gguf",),
    ),
    # The Vulkan variant of the row above — GPU acceleration on NVIDIA and AMD
    # under Windows and Linux, where `llamacpp-text`'s CPU-index pin is
    # CPU-only (Apple Silicon already gets Metal acceleration through that
    # same CPU-index wheel, which is why this variant does not also cover
    # macOS). Immediately BELOW `llamacpp-text`, so reaching this row is always
    # a CHOICE made on the Engines tab and never something `auto` falls into —
    # the same relationship the accelerated Diffusers rows have to
    # `diffusers-image`, and for the same reason (a much larger download with a
    # hardware requirement). D416 moved the DEFAULT onto `llamacpp-text`; it did
    # not move it onto this row, and must not: `_offload_schedule`'s over-commit
    # backoff is known not to engage on AMD (radv evicts other clients instead
    # of erroring, which took a desktop session down during testing — PR #706),
    # so an over-large model on this row can cost a user their session rather
    # than a slow load.
    Runner(
        code="llamacpp-text-vulkan",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "llamacpp_text_vulkan"),
        # Both names equal, for the reason the torch hardware variants' are
        # (see the table's naming note): "(Vulkan)" is this row's IDENTITY
        # rather than a platform aside, and the short name is what the Local
        # card and the job row print, so two rows both reading "llama.cpp"
        # would render as one engine everywhere but the picker.
        label="llama.cpp (Vulkan)",
        short_label="llama.cpp (Vulkan)",
        family_label="llama.cpp",
        note="Runs GGUF models on NVIDIA and AMD GPUs. Much faster than the "
             "CPU build; much larger download.",
        _available=_vulkan,
        hub_filter_tags=("gguf",),
    ),
    # Image generation is arranged like the other two: MLX takes the Macs
    # (D310). One 4.6GB repo against the ~10.1GB two-repo split the torch
    # recipe needs, ~8x quicker to load, ~15-20% quicker per image, measured
    # same model, prompt and seed.
    #
    # **The memory ceiling is a KNOWN, ACCEPTED risk rather than an unknown.**
    # MLX's allocator reported a ~23.6GB `get_cache_memory` high-water doing
    # those renders — larger than torch's ~19.1GB Metal allocation for the same
    # picture — on a 34GB machine already several GB into swap, and nothing has
    # been run on the 16GB Macs this app's own catalog says full-precision FLUX
    # already OOMs. The evidence is one machine's benchmark; the decision was to
    # take the speed and let a user who hits the ceiling move back to Diffusers
    # from the Engines tab, which is the case the engine preference (D302)
    # exists to serve in both directions. The `note` says so **under that very
    # picker** (D315) — it is the sentence a 16GB Mac needs at the moment it is
    # deciding whether to switch away, and it was over a grid of downloads on
    # another tab until then.
    Runner(
        code="mflux-image",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "mflux_image"),
        label="MLX FLUX (Apple Silicon)",
        short_label="MLX FLUX",
        family_label="MLX FLUX",
        # ONE OR TWO SENTENCES, fixed shape (see `llamacpp-text`'s comment
        # above). The memory caveat is the load-bearing fact: the reader it
        # exists for is someone on a small Mac deciding whether to switch AWAY,
        # and since D315 the line is rendered directly under the control that
        # does the switching.
        note="Loads fast from a small download. Uses more memory than "
             "Diffusers; untested below 32 GB.",
        _available=_apple_silicon,
    ),
    Runner(
        code="diffusers-image",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "diffusers_image"),
        # "(CPU)" for the reason `llamacpp-text` above states about its own:
        # the qualifier names the BUILD — the wheel with no accelerator
        # libraries in it — never a prediction about the device.
        label="Diffusers (CPU)",
        short_label="Diffusers (CPU)",
        family_label="Diffusers",
        # SAID OF THE CPU rather than of the row, because `torch_image._place()`
        # moves the pipeline to `mps` on a Mac (D382), and a flat "minutes per
        # image" contradicted the `mps` the loaded card reports on the very
        # machine this row exists to catch when MLX FLUX is unavailable.
        note="Renders on Apple Silicon's GPU, or on the CPU elsewhere. "
             "Minutes per image on CPU.",
        _available=_always,
    ),
    # The accelerated image variants, below the CPU row for the reason the text
    # variants are below theirs.
    Runner(
        code="diffusers-image-cuda",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "diffusers_image_cuda"),
        label="Diffusers (CUDA)",
        short_label="Diffusers (CUDA)",
        family_label="Diffusers",
        note="Renders in seconds per image on an NVIDIA GPU. Much larger "
             "download.",
        _available=_cuda,
    ),
    # THE SHARED RING, and why the ROCm image row warns about the desktop.
    #
    # On a single-GPU machine the render and the compositor submit to the SAME
    # ring, so a sustained submission starves the screen until the driver gives
    # up on it. Measured, not feared — an RX 9060 XT (gfx1200), kernel 7.1.4-zen:
    #
    #     amdgpu: ring gfx_0.0.0 timeout, signaled seq=6239674, emitted 6239675
    #     amdgpu:  Process Hyprland pid 832655 thread Hyprland:cs0
    #     amdgpu: Starting gfx_0.0.0 ring reset / Ring gfx_0.0.0 reset succeeded
    #     amdgpu: [drm] device wedged, but no recovery needed
    #
    # Note WHICH process the kernel named: the compositor, not the renderer. The
    # GPU recovered without a reboot and the session did not — the desktop went
    # down and the ring came back.
    #
    # **Honest about what produced it:** a continuous matmul loop, not a render.
    # A large 100-step render submits the same class of work, and a ~90s FLUX.2
    # klein render on that card finished without a stall — which is why the note
    # says a long render CAN stall the desktop rather than will.
    #
    # Nothing mitigates it here, deliberately. The fixes that exist are outside
    # this app — CU masking, queue priority, rendering on a card that is not
    # driving the display — none verified, and an unverified mitigation is a
    # promise made on the driver's behalf. Naming the cost and letting the user
    # choose is the bargain the download size already gets (D383). It is
    # documented HERE and not in the manifest on purpose: `state_digest` hashes
    # `pyproject.toml` whole, so a comment there would mark every already-built
    # ROCm env stale and charge existing users a resync for a paragraph.
    Runner(
        code="diffusers-image-rocm",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "diffusers_image_rocm"),
        label="Diffusers (ROCm)",
        short_label="Diffusers (ROCm)",
        family_label="Diffusers",
        # The desktop clause is not padding — see THE SHARED RING above for the
        # kernel log that proved it.
        note="Renders in seconds per image on an AMD GPU under Linux. Larger "
             "download; a long render can stall the desktop.",
        _available=_rocm,
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
        short_label="MLX Whisper",
        family_label="MLX Whisper",
        note="Transcribes on the GPU. Several times faster than the CPU "
             "path.",
        _available=_apple_silicon,
    ),
    # D319 briefly added a third row here, Parakeet-TDT (`parakeet-mlx`),
    # below MLX Whisper so the default would not move. D406 withdrew it —
    # maintenance cost not justified by use — leaving speech to text with the
    # same two-runner shape every other capability in this table has.
    Runner(
        code="faster-whisper",
        capability=SPEECH_TO_TEXT,
        folder=os.path.join(RUNNERS_DIR, "faster_whisper"),
        # "Faster Whisper", not the upstream "faster-whisper": the tag column is
        # product names, and `code` above still carries the exact spelling.
        label="Faster Whisper (CTranslate2)",
        short_label="Faster Whisper",
        family_label="Faster Whisper",
        # `_always`, and that is why speech to text SHIPPED on CTranslate2
        # rather than on MLX: text generation was already Apple-Silicon-only,
        # and a second capability that existed on a Mac and nowhere else would
        # have made "local AI" a thing Windows and Linux users read about
        # rather than used. The MLX row above is the sequel that argument
        # always allowed for — it takes the Macs and leaves everything else
        # here, and no user loses a capability to it.
        #
        # ONE OR TWO SENTENCES (see `llamacpp-text`'s comment above). Both
        # facts are `_placement()`'s own (faster_whisper/worker.py): CUDA when
        # `ctranslate2.get_cuda_device_count()` finds one, CPU otherwise —
        # CTranslate2 has no Metal backend, so this is what an Apple Silicon
        # machine gets whenever the MLX row above it is unavailable — and
        # that CPU path is still, in the module's own words, "several times
        # faster than real time".
        note="Transcribes on an NVIDIA GPU when available, otherwise the "
             "CPU. Several times faster than real time.",
        _available=_always,
    ),
    # Embeddings, the fourth capability, arranged like the other three: MLX takes
    # the Macs and the ONNX row below it keeps every other platform — plus the
    # Macs whenever the MLX folder is not built yet or its package cannot run.
    #
    # **The two runners share an embedding SPACE, which no other pair here
    # does.** They read DIFFERENT FILES out of the same models — mlx-embeddings
    # opens `mlx-community` safetensors through its own port of the
    # architecture, `onnx-embed` opens an `onnx-community` graph export — and
    # the cosine similarities the two produce for the same texts and images
    # still agree to about three decimals (measured on
    # `siglip2-base-patch16-384`). So a page that embedded a folder of images on
    # one engine and searches it from the other gets sensible answers, which is a
    # promise the whisper and text pairs explicitly cannot make about their
    # weights. It is not a promise this app relies on anywhere — vectors are the
    # caller's to store, and a switch is still a switch — but it is why the
    # engine choice here is genuinely free.
    #
    # **The two engines' CATALOG lists are separate, and that follows from the
    # files rather than from the vectors** (`catalog.py`'s keying rule): a Mac
    # cannot open an ONNX export and this engine cannot open MLX safetensors, so
    # the shared space does not make the downloads interchangeable.
    Runner(
        code="mlx-embed",
        capability=EMBEDDINGS,
        folder=os.path.join(RUNNERS_DIR, "mlx_embed"),
        label="MLX Embeddings (Apple Silicon)",
        short_label="MLX Embeddings",
        # The format claim with the hardware taken out: these weights are the
        # safetensors mlx-embeddings' own SigLIP port opens.
        family_label="MLX Embeddings",
        # ONE OR TWO SENTENCES (see `llamacpp-text`'s comment above). It
        # describes the DEFAULT on a Mac, so what it leads with is what the
        # reader gets.
        note="Embeds on the GPU. Same vector space as the ONNX engine.",
        _available=_apple_silicon,
    ),
    # **ONNX Runtime — the cross-platform embedding engine, and the
    # Apple-Silicon fallback behind `mlx-embed`.** There were three
    # `transformers-embed*` rows here until this branch, running the identical
    # checkpoints through torch; they went because a dual encoder is one forward
    # pass over a short sequence or one image, so the compute was never the
    # argument — the WHEEL was. Those rows installed 0.2 GB on the CPU index and
    # up to 5.9 GB on an accelerated one to run a model whose own weights are
    # 1.5 GB, where `onnxruntime` is tens of megabytes reading the same
    # checkpoint re-exported. `tests/test_ai_onnx_embed_real_weights.py` is what
    # licensed the swap: it asserts ≥0.999 cosine between the two engines'
    # vectors on real weights, both towers.
    #
    # Same four-row shape the torch family had: an unaccelerated build first —
    # what every `auto` machine off Apple Silicon resolves to — then the
    # accelerated ones, opt-in from the Engines tab and gated on the device
    # actually being there. DirectML leads those three because it is the only
    # one Windows can take, and unlike the CUDA/ROCm pair it is vendor-neutral,
    # so ONE row covers every Windows GPU rather than a folder per vendor.
    Runner(
        code="onnx-embed",
        capability=EMBEDDINGS,
        folder=os.path.join(RUNNERS_DIR, "onnx_embed"),
        # "(CPU)" on every row of a family with siblings, the discipline
        # `llamacpp-text` sets: the qualifier names the BUILD — PyPI's plain
        # `onnxruntime`, which has no GPU provider compiled in — never a
        # prediction about the device, and a family missing it on one row prints
        # two engines under one name.
        label="ONNX Embeddings (CPU)",
        short_label="ONNX Embeddings (CPU)",
        # The format claim with the hardware taken out: these weights are the
        # `onnx/` graphs an `InferenceSession` opens, whichever provider does it.
        family_label="ONNX Embeddings",
        # ONE OR TWO SENTENCES (see `llamacpp-text`'s comment above). It
        # describes the DEFAULT off a Mac, so what it leads with is what most
        # readers get: a workload that is already fast, on an engine that is a
        # small download.
        note="Embeds on any machine's CPU. Tens of megabytes rather than "
             "gigabytes.",
        _available=_onnx_platform,
    ),
    Runner(
        code="onnx-embed-directml",
        capability=EMBEDDINGS,
        folder=os.path.join(RUNNERS_DIR, "onnx_embed_directml"),
        label="ONNX Embeddings (DirectML)",
        short_label="ONNX Embeddings (DirectML)",
        family_label="ONNX Embeddings",
        # It names the vendor-neutrality because that is the fact that
        # distinguishes this row from the CUDA one on a Windows machine with an
        # NVIDIA card — both would work, and this one needs no `nvidia-*`
        # wheels. No desktop-stall warning: that hazard is a denoise holding the
        # GPU for minutes, and an embed call is one forward pass at
        # `embed_common.MAX_ITEMS` items, over in under a second.
        note="Embeds on any Windows GPU through Direct3D 12. No vendor "
             "runtime to install.",
        _available=_directml,
    ),
    Runner(
        code="onnx-embed-cuda",
        capability=EMBEDDINGS,
        folder=os.path.join(RUNNERS_DIR, "onnx_embed_cuda"),
        label="ONNX Embeddings (CUDA)",
        short_label="ONNX Embeddings (CUDA)",
        family_label="ONNX Embeddings",
        # Same `_cuda` probe the torch and diffusers CUDA rows use, unchanged:
        # "does this machine have a usable NVIDIA GPU" does not become a
        # different question because the wheel opening the model is onnxruntime.
        note="Embeds on an NVIDIA GPU. Larger download for a workload "
             "already fast on the CPU.",
        _available=_cuda,
    ),
    Runner(
        code="onnx-embed-rocm",
        capability=EMBEDDINGS,
        folder=os.path.join(RUNNERS_DIR, "onnx_embed_rocm"),
        label="ONNX Embeddings (ROCm)",
        short_label="ONNX Embeddings (ROCm)",
        family_label="ONNX Embeddings",
        note="Embeds on an AMD GPU under Linux. Larger download for a "
             "workload already fast on the CPU.",
        _available=_rocm,
    ),
    # Video generation, the fifth capability and the first with no
    # "everywhere" row: its one engine is MLX, so this capability is
    # Apple-Silicon-only and has no cross-platform fallback at all.
    #
    # `ltx-video` is LTX-2.3 run through `ltx-2-mlx`, a pure-MLX, MIT-licensed
    # port that keeps AUDIO (mlx-video's own LTX and Wan paths either want the
    # 57-108 GB bf16 trees or render silent video — see the plan's
    # "ltx-2-mlx, not mlx-video" decision) at an int4-distilled ~30 GB
    # download that a 16 GB Mac can hold. Gated on the SAME `_apple_silicon`
    # check `mlx-embed` above uses: this row has no bundled binary to resolve,
    # just a `uv sync`-able git package, so "can this machine run MLX at all"
    # is the whole gate.
    #
    # There was a second row here, `h3-video` (MiniMax H3 via antirez/h3.c, a
    # bundled Metal binary), removed in D468. It could not work on macOS
    # 14 on two independent counts — the binary built with an implicit
    # minos 15.0, and h3.c's attention path calls MPSGraph's
    # scaledDotProductAttention plus MTLCompileOptions.mathMode, both
    # macOS 15.0+ with no pre-15 fallback — and bundling it forced the whole
    # release build onto a macos-15 runner, which is what shipped a
    # macOS-15-only pyexpat.so and broke app launch on macOS 14 outright.
    # LTX-2.3 already served this capability as the default row.
    Runner(
        code="ltx-video",
        capability=VIDEO_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "ltx_video"),
        # The hardware qualifier lives on the long name only, per the naming
        # note above the table — `short_label`/`family_label` name the engine
        # ("LTX-2.3"), not the machine it happens to run on here.
        label="LTX-2.3 (Apple Silicon)",
        short_label="LTX-2.3",
        family_label="LTX-2.3",
        note="Generates text-to-video with audio, distilled to 8 steps. "
             "Needs 16 GB+ of RAM.",
        _available=_apple_silicon,
    ),
    # Image-to-3D, the sixth capability and the second with no "everywhere"
    # row — same shape as `ltx-video` above: one MLX engine, gated on the
    # same `_apple_silicon` probe, no bundled binary to resolve.
    #
    # `hunyuan3d-mlx` is Tencent's Hunyuan3D-2.1 shape pipeline (image ->
    # untextured mesh) through `iamsdas/Hunyuan3D-2.1-mlx`, a fork of
    # `dgrauet/Hunyuan3D-2.1-mlx` that adds only packaging (see the runner's
    # own `pyproject.toml` for the pin and why it is a fork rather than
    # upstream). Shape only, deliberately: the fork also ports a Stage 2 PBR
    # texture pipeline (`hy3dpaint`) that needs a second ~7.6 GB of weights,
    # runs a six-view diffusion render plus a super-resolution pass (peaking
    # near 38 GB), and would be a follow-up capability tier, not a variant
    # of this one — this row's worker never imports that half.
    Runner(
        code="hunyuan3d-mlx",
        capability=IMAGE_TO_3D,
        folder=os.path.join(RUNNERS_DIR, "hunyuan3d_mlx"),
        label="Hunyuan3D-2.1 (Apple Silicon)",
        short_label="Hunyuan3D-2.1",
        family_label="Hunyuan3D-2.1",
        note="Generates an untextured 3D mesh from one image. Needs "
             "16 GB+ of RAM.",
        _available=_apple_silicon,
    ),
)


@dataclass(frozen=True)
class VideoTraits:
    """The shape of a video request, for the one runner that will actually
    serve it — the three facts `server/routers/ai_runtime.py`'s route used
    to hardcode before this table existed: the frame-count grid, the default
    canvas, and the default step count.

    **The frame grid is `frames_base + frames_step * n`**, `n` starting at 0
    — `_snap_frames` (`ai_runtime.py`) rounds a request UP to the nearest
    point on it, never down, mirroring the engine's own alignment rule
    (LTX has no compiled binary to align against, but its VAE's temporal
    compression is 8, so `8n + 1` is the natural grid its own upstream CLI
    defaults to). Side clamps and
    the overall pixel budget stay SHARED across every video runner — those
    are values the app itself chose as a safety rail, not a fact about the
    engine's weights — so only the canvas DEFAULT is a trait here.

    Not a dict of bare ints: the fields are named exactly once (here) and
    read by name everywhere else, the same argument `Runner`'s own fields
    make over a positional tuple.
    """

    #: `n = 0`'s frame count — the shortest clip the grid can name.
    frames_base: int
    #: The grid's spacing — the next valid frame count is `frames_base +
    #: frames_step`, and so on.
    frames_step: int
    #: Which `n` a request that named no `frames` at all gets.
    default_frames_n: int
    default_width: int
    default_height: int
    default_steps: int


#: Runner code -> its `VideoTraits`. ABSENT for a runner with no video
#: capability — the same "absent rather than empty" shape `runners/engine_
#: options.py`'s own table uses (see that module's docstring for the
#: argument in full): the common case (a non-video runner) costs a dict
#: lookup rather than an entry that says nothing.
#:
#: `video_traits_for` is the only reader outside this file, and it falls
#: back to `ltx-video`'s row for a code THIS table has never heard of — a
#: runner registered by a test (`fake_video_runner`), or one written before
#: this table existed. The fallback is the SHIPPING video runner's own row
#: rather than an engine-neutral guess, so a caller this table cannot name
#: gets a request shape some real engine actually accepts rather than a
#: KeyError. It was H3's row until that runner was dropped (D468).
VIDEO_TRAITS: dict[str, VideoTraits] = {
    # 97 = 1 + 8*12 — upstream's own CLI default (`--frames 97`), and this
    # runner's own `worker.py` default. 704x480 and 8 steps are that same
    # CLI's `--width`/`--height`/(distilled default) numbers.
    "ltx-video": VideoTraits(
        frames_base=1, frames_step=8, default_frames_n=12,
        default_width=704, default_height=480, default_steps=8),
}


def video_traits_for(code: str | None) -> VideoTraits:
    """The request shape for `code`, falling back to `ltx-video`'s numbers.

    See `VIDEO_TRAITS`'s docstring for why the fallback is the shipping
    runner's row rather than some engine-neutral guess, for a runner this
    table does not name at all — `code=None` (nothing can serve this
    capability at all, checked separately before a route ever gets here)
    included.
    """
    return VIDEO_TRAITS.get(code or "", VIDEO_TRAITS["ltx-video"])


#: `n` ranges over this window on EVERY video runner's own grid — an
#: app-chosen safety rail, not a fact about either engine's weights (see
#: `VideoTraits`'s own docstring for that distinction), so it lives here as
#: a public constant rather than duplicated as a private one in every place
#: that needs the ACTUAL min/max frame count a runner's grid offers:
#: `server/routers/ai_runtime.py`'s `_snap_frames` and `catalog.py`'s own
#: video-traits payload for the Playground.
MIN_VIDEO_FRAMES_N = 1
MAX_VIDEO_FRAMES_N = 21


def video_frame_bounds(traits: VideoTraits) -> tuple[int, int]:
    """`(min, max)` actual frame counts `traits`' grid offers, at the app's
    own `[MIN_VIDEO_FRAMES_N, MAX_VIDEO_FRAMES_N]` window — the two absolute
    numbers a caller outside this module (a route clamping a request, a
    payload describing a slider's range) needs, so neither has to re-derive
    `frames_base + frames_step * n` itself.
    """
    return (traits.frames_base + traits.frames_step * MIN_VIDEO_FRAMES_N,
            traits.frames_base + traits.frames_step * MAX_VIDEO_FRAMES_N)


@dataclass(frozen=True)
class MeshTraits:
    """The shape of a mesh request, for the one runner that will actually
    serve it — `VideoTraits`' own argument applies verbatim: these are the
    facts `server/routers/ai_runtime.py`'s route would otherwise hardcode.

    There is no frame grid here — a mesh request has no analogous "valid
    point on a line" shape — so this table is flatter than `VideoTraits`:
    three defaults, each read from `hy3dshape.pipeline_mlx.ShapePipeline.
    __call__`'s own signature at the pinned commit (Task 1), not from
    memory. The app-level clamp WINDOW for each one is a module-level
    constant beside this table (`MIN_MESH_STEPS`/`MAX_MESH_STEPS` and its
    two siblings below), the same "shared across every runner of this
    capability, not per-trait-row" shape `MIN_VIDEO_FRAMES_N` already uses —
    there is only one mesh runner today, but a second one would still want
    the same safety rail, not its own.
    """

    default_steps: int
    default_guidance: float
    default_octree_resolution: int


#: Runner code -> its `MeshTraits`. Absent for a runner with no mesh
#: capability — `VIDEO_TRAITS`'s own "absent rather than empty" shape.
#: `mesh_traits_for` falls back to `hunyuan3d-mlx`'s row for the same reason
#: `video_traits_for` falls back to `ltx-video`'s: the one shipping engine's
#: numbers are a better default for an unrecognised code than an
#: engine-neutral guess.
MESH_TRAITS: dict[str, MeshTraits] = {
    "hunyuan3d-mlx": MeshTraits(
        default_steps=50, default_guidance=5.0, default_octree_resolution=256),
}


def mesh_traits_for(code: str | None) -> MeshTraits:
    """The request shape for `code`, falling back to `hunyuan3d-mlx`'s
    numbers. See `MESH_TRAITS`'s docstring for why."""
    return MESH_TRAITS.get(code or "", MESH_TRAITS["hunyuan3d-mlx"])


#: The app's own safety rails for a mesh request — not a fact about
#: `hy3dshape`'s weights, the same distinction `MIN_VIDEO_FRAMES_N` draws.
#:
#: Steps and octree resolution mirror the ONLY sliders upstream's own
#: `gradio_app.py` exposes for this pipeline at the pinned commit (`gr.
#: Slider(minimum=1, maximum=100, ...)` for inference steps, `gr.Slider(
#: minimum=16, maximum=512, ...)` for octree resolution) — upstream's own
#: accepted range, not one this app invented. Guidance has no such
#: upstream-recommended range (`gradio_app.py` never exposes it as a
#: slider at all, fixed at its PyTorch-reference default everywhere it
#: appears); `[0, 20]` here is an ordinary classifier-free-guidance window,
#: not a value anyone has tuned this checkpoint against.
#:
#: **`MAX_MESH_OCTREE_RESOLUTION` doubles as this capability's "face cap".**
#: `ShapePipeline` has no separate face-count parameter to clamp — marching
#: cubes at a given octree resolution produces however many triangles the
#: decoded surface actually has, and the lean, torch-free path this runner
#: takes (Task 1) deliberately skips `hy3dshape.postprocessors.FaceReducer`,
#: which needs `pymeshlab` AND `torch`. Bounding the resolution IS bounding
#: the face count for this engine — there is no second knob.
MIN_MESH_STEPS = 1
MAX_MESH_STEPS = 100
MIN_MESH_GUIDANCE = 0.0
MAX_MESH_GUIDANCE = 20.0
MIN_MESH_OCTREE_RESOLUTION = 16
MAX_MESH_OCTREE_RESOLUTION = 512


# The task vocabulary — which `pipeline_tag` means what, and which of them a
# runner here serves — lives in `ai/tasks.py`, keyed by the Hub's own tag. It
# was three tables in two modules (a tag-to-prose map, this capability map, and
# a ruled-out list), and the seam between them is what let an unclassified tag
# through: see that module's docstring. This module keeps the CAPABILITY
# constants above, which is the half `tasks.py` imports — VIDEO_GENERATION
# included, so `tasks.py`'s "text-to-video" row maps to it rather than sitting
# in NO_RUNNER now that `ltx-video` actually serves it.


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
    """Registry order, filtered by availability — the rule before D302, and
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
                              f"{runner.short} does not do {capability}")
        status = runner.available()
        if not status.ok:
            return Resolution(_first_available(capability), requested,
                              status.reason or f"{runner.short} cannot run here")
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
    generation is always `mlx-text` — so a Linux machine whose cross-platform
    text worker was missing (a state `Runner.available` documents, since a
    runner is registered before its folder is written) would be told text
    generation "needs Apple Silicon", naming the one backend that was never
    going to serve it and hiding the one that would have. Reported by review on
    the PR that added the second runner.

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
    different with D302.** Availability is a fact about the hardware: can this
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
                # BOTH names, and the field names say which is which. `label`
                # keeps meaning the full one everywhere on the wire — a
                # consumer that reads it after this change gets exactly what it
                # got before — and a surface that wants the qualifier-free name
                # asks for it. The alternative, quietly shortening `label`,
                # would be a change no reader of the payload could see.
                #
                # **No `familyLabel` here, deliberately.** This payload feeds
                # the Engines tab, where the reader is CHOOSING between builds
                # of one library — a family name is the one thing that cannot
                # tell "Diffusers (CUDA)" from "Diffusers (ROCm)", so the
                # surface that would render it has no use for it. It is the
                # Local card's tag that needs the qualifier gone, and that card
                # reads the engine object `ai_models.py` builds per repo.
                "label": runner.label,
                "shortLabel": runner.short,
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


def _choice(runner: Runner) -> dict:
    """One option under an engine picker, built from ONE probe (D382).

    A function rather than two lines inside the comprehension below because the
    bug this fixes was exactly that shape: `available` read `runner.available()`
    and `reason` read it AGAIN. That was free while every probe was a `platform`
    fact and stopped being free the moment a probe became a live device read
    (AI-6) — two calls can straddle a `modprobe`, a container restart or an eGPU
    being unplugged and disagree. Both disagreements reach the user and neither
    is a crash: the option serialises as `available: false` with `reason: null`,
    which the `<select>` renders as a disabled row with NOTHING saying why (the
    page has no copy of the reason and must not), or as `available: true` still
    carrying the refusal that has just stopped being true. Binding the status to
    a name once makes the second read impossible rather than merely unlikely.
    """
    status = runner.available()
    return {
        "code": runner.code,
        "label": runner.label,
        "note": runner.note or None,
        "available": status.ok,
        # The registry's own words ("needs Apple Silicon (this is
        # windows/amd64)"), so the disabled row explains itself with the same
        # sentence the rest of the app uses. The page must not write its own
        # copy of this, because the page cannot know it.
        "reason": status.reason or None,
    }


def _stranded_label(capability: str, requested: str) -> str | None:
    """The display name for a STORED selection that matches none of
    `capability`'s own choices — or None when there is no name to give.

    Exists so the Engines tab never has to derive a name from registry PROSE.
    It used to reach for `choices.find(c => c.code === selected)?.label`, which
    is exactly what fails here BY DEFINITION (a stranded code is the one that
    matches no choice) and left the frontend falling back to the raw stored
    code — "mlx-whisper" rather than "MLX Whisper" — even when the code names a
    real, registered runner. That raw fallback then broke `ignoredWarning`'s
    de-duplication for the wrong-capability shape specifically: `resolve()`
    writes `f"{runner.short} does not do {capability}"` (`"MLX Whisper does not
    do text-generation"`), and a frontend name of `"mlx-whisper"` does not
    occur inside a sentence that says `"MLX Whisper"` — so the page printed the
    name twice, in two different spellings, on the one line whose entire job is
    saying a preference did not take.

    `.short`, not `.label`: that is the exact string `resolve()` already wrote
    into the reason, and the two must be the SAME string for `ignoredWarning`'s
    `reason.includes(name)` check to find it — a full qualified label
    ("MLX Whisper (Apple Silicon)") would not occur inside a reason that only
    ever names the short one.

    Two cases return None, and the caller (`strandedSelection` on the frontend,
    mirrored here) cannot always tell them apart from the payload alone: `auto`
    is never stranded, and a WITHDRAWN code (`by_code` finds nothing — D416's
    `transformers-text*`) has no runner to ask a label of. Both leave the
    Engines tab to fall back to the bare stored code, which is the whole reason
    that fallback exists.
    """
    if requested == AUTO:
        return None
    if any(runner.code == requested for runner in _RUNNERS
           if runner.capability == capability):
        return None  # Not stranded: a real choice already carries this label.
    runner = by_code(requested)
    return runner.short if runner is not None else None


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
                # The summary line under the picker ("Using MLX LM.") reads the
                # short one: it sits directly beneath the options, which carry
                # the qualifier a line above, and repeating it there says
                # nothing the reader has not just read. The picker itself, and
                # the sentence that names a chosen option back to the user
                # (`ignoredWarning`), stay on `label`.
                "effectiveShortLabel": resolution.runner.short if resolution.runner else None,
                # Null when the selection is in force (including "auto", which
                # is honoured by definition). A sentence when it is not, and the
                # UI is expected to show it — a control whose value does nothing,
                # with nothing saying why, is the failure this field exists for.
                "ignoredReason": resolution.ignored_reason or None,
                # See `_stranded_label`. Null whenever `selected` is not
                # stranded at all (it names `auto` or a real choice) AND
                # whenever it is stranded but withdrawn (no runner to name).
                "strandedLabel": _stranded_label(capability, resolution.requested),
                "choices": [
                    _choice(runner)
                    for runner in _RUNNERS
                    if runner.capability == capability
                ],
            }
        )
    return rows

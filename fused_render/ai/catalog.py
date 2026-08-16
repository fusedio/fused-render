"""Which models to suggest, per runner (SPEC §40).

These lists used to live inside the apps — `local_chat/chat.py` carried three
MLX text models and `flux_text2image/worker.py` hard-coded one image model. That
put the curation in the wrong place twice over: a second chat app had to copy it,
and the AI Models page — the one surface whose whole job is "what should I have
on this disk" — could not see it at all.

**Curated, not fetched.** The Hub has half a million models and a `downloads`
sort is a popularity contest; what a user needs on a laptop is a handful that are
known to fit and known to work with the runner that will load them. That is an
editorial judgement, so it is a list a person maintains, with the reason written
down beside each entry.

**Keyed by RUNNER, not by capability, and that is what makes it correct on more
than one platform.** A suggestion is only meaningful for the backend that will
load it: `mlx-community/Qwen3.5-9B-MLX-4bit` is packed for Metal kernels and is
unloadable rubbish on a Windows box, while `Qwen/Qwen3-4B-Instruct-2507` is the
right answer there and the wrong one on a Mac that has MLX. One capability with
two runners (text generation since D293, speech to text since D302) therefore
has two lists, and the registry's resolution decides which one a machine sees —
the same mechanism, asked the same question, so the page cannot offer a model
the loader would then refuse.

**Which means the suggestion list now moves when a PREFERENCE moves**, not only
when the hardware differs: a user who switches speech to text from MLX to
CTranslate2 on the Preferences page is shown four different repos next time they
open the AI Models page, and the ones they may already have downloaded stop
being offered. That is correct — a suggestion is only meaningful for the backend
that will load it — but it must not be silent, which is why `describe()` reports
the resolved runner's LABEL beside every list and the page shows it.

Sizes are the on-disk download, and the notes are frank about the trade — "tight
on 16GB" is the sentence that stops someone starting an 8GB pull they will regret.
"""

from __future__ import annotations

from fused_render.ai import registry

#: runner code -> suggested models, best first. `size_gb` is the approximate
#: full-snapshot download in decimal GB: the sum of the Hub's per-file byte
#: metadata, rounded to one decimal. **None when that metadata is unavailable**,
#: which the card shows as "—" rather than inventing a figure from parameter
#: counts (the same no-guess rule the Hub result cards follow, D255/D295).
#: `note` is why you would or would not pick this one.
SUGGESTIONS: dict[str, list[dict]] = {
    # Refreshed 2026-08-16, and the refresh brought one fact this list has not
    # had to state before.
    #
    # **EVERY CURRENT CHECKPOINT HERE IS MULTIMODAL, AND HALF OF IT IS DEAD
    # WEIGHT.** Qwen3.5, Qwen3.6 and gemma-4 all ship as
    # `…ForConditionalGeneration` with a `vision_config` (gemma-4 adds an
    # `audio_config`), and mlx-lm loads the LANGUAGE TOWER and nothing else —
    # the vision and audio weights sit in the snapshot, downloaded and unused,
    # until an `mlx-vlm` runner exists to use them. It is not avoidable by
    # shopping around: there is no text-only conversion of Qwen3.5 on the Hub,
    # every 4-bit variant of it measured the same. So the sizes below are
    # honest about the download and quietly larger than a text-only model of
    # the same class — Qwen3.5 9B is 6.0GB against the Qwen3 8B it replaces at
    # 4.6GB, and 1.4GB of that difference is weights this app cannot currently
    # run. Under this file's own "sized for the machine that will actually run
    # them" rule that is a real cost, and it is why the ceiling here came DOWN
    # (the largest entry was 8.1GB, now 6.1GB) rather than up.
    #
    # **`mlx-community/gemma-4-12B-it-4bit` is deliberately absent**, and it is
    # the entry a future reader is most likely to try to add: it is newer than
    # everything here, 6.8GB, ungated, and — measured, not assumed — 1.3GB
    # SMALLER than the Gemma 3 12B this list used to carry. It does not load.
    # Its `model_type` is `gemma4_unified`, mlx-lm resolves a checkpoint by
    # importing `mlx_lm.models.<model_type>`, and 0.31.3 ships `gemma4.py` and
    # `gemma4_text.py` and no `gemma4_unified.py` — so the load ends in
    # "Model type gemma4_unified not supported." The `e4b`/`e2b` siblings below
    # are `gemma4` and do load. Recheck when mlx-lm is next bumped.
    #
    # Sizes are the whole-snapshot Hub byte sum on 2026-08-16 (D295), and every
    # entry is ungated. **One line each**, per the rule the transformers list
    # states below.
    "mlx-text": [
        {
            "id": "mlx-community/Qwen3.5-9B-MLX-4bit",
            "label": "Qwen3.5 9B (MLX 4-bit)",
            "size_gb": 6.0,
            "note": "The one to start with: strong on reasoning and code, and "
                    "comfortable on 16GB.",
        },
        {
            "id": "mlx-community/Qwen3.5-4B-MLX-4bit",
            "label": "Qwen3.5 4B (MLX 4-bit)",
            "size_gb": 3.1,
            "note": "Half the download and quicker to answer, for a machine "
                    "with other things open.",
        },
        {
            "id": "mlx-community/gemma-4-e4b-it-4bit",
            "label": "Gemma 4 E4B (4-bit)",
            "size_gb": 5.2,
            "note": "A second family at the same size, worth trying on a "
                    "prompt Qwen handles badly.",
        },
        {
            "id": "mlx-community/Qwen3.5-2B-MLX-4bit",
            "label": "Qwen3.5 2B (MLX 4-bit)",
            "size_gb": 1.8,
            "note": "The one to pick with no headroom — weaker, and it runs "
                    "anywhere MLX does.",
        },
        # Bonsai 27B (prism-ml). The family ships eight repos and most of them
        # cannot run here: the GGUF builds are llama.cpp's format, and the AWQ
        # builds are both AWQ and image-text-to-text. The MLX ones are what is
        # left, and only the 2-bit is listed — the 1-bit sibling is omitted
        # deliberately rather than forgotten (see the note below).
        #
        # KEPT through the 2026-08 refresh, and re-argued rather than assumed:
        # it still loads (`model_type` is `qwen3_5`, which mlx-lm ships), and it
        # is still the only way to have a 27B-class model inside this list's
        # size budget. What changed is the comparison its note used to make —
        # Gemma 3 12B has left the list, and against the 9B above it is no
        # longer the cheaper download, only the bigger model for the same money.
        {
            "id": "prism-ml/Ternary-Bonsai-27B-mlx-2bit",
            "label": "Ternary Bonsai 27B (MLX 2-bit)",
            # 6.1, not the 8.5 the Hub's file listing adds up to: this is what
            # the completed download MEASURES on disk, reported by the AI Models
            # page's own scan. Where the two disagree the measurement wins —
            # every other number on this page is bytes on a real filesystem, and
            # a suggestion that overstates by 2.4GB is the one figure someone
            # plans a download around.
            "size_gb": 6.1,
            "note": "27B for what the 9B above costs on disk. Ternary "
                    "quantization is new, so measure it against a 4-bit model "
                    "you already trust.",
        },
    ],
    # Text generation on Windows and Linux, plus the Apple Silicon fallback
    # when MLX is unavailable (D293). Three rules
    # picked these, and each one is a failure this app has already shipped once:
    #
    # * **Unquantized safetensors only.** Every other format on the Hub needs
    #   something this runner does not ship — GGUF is llama.cpp's, AWQ and GPTQ
    #   need their own packages, bitsandbytes needs an NVIDIA card — and a
    #   suggestion the loader then refuses is the trap AI-10 describes for
    #   CTranslate2 and the whisper runner had to write an error message about.
    # * **Ungated.** `google/gemma-3-*` and `meta-llama/*` need a licence
    #   accepted on the Hub first, so Download 401s partway through for a user
    #   who has done nothing wrong. The MLX list above gets away with gemma only
    #   because the `mlx-community` re-uploads are not gated.
    # * **Sized for the machine that will actually run them.** The accepted v1
    #   trade is that torch from PyPI is CPU-only on Windows (see this runner's
    #   `pyproject.toml`), so the list has to be usable with no GPU at all
    #   rather than assuming one — which is why the smallest entry is here on
    #   merit and not as an afterthought.
    #
    # Sizes sum every file in the Hub snapshot (2026-08-14) and round the byte
    # total to one decimal GB. That makes them download estimates rather than
    # filesystem measurements, but unlike parameter arithmetic they include the
    # tokenizer, configs, split weights and any other payload the whole-repo
    # downloader actually fetches (D295).
    #
    # **One line each.** A shortlist is read by SWEEPING it, and four cards of
    # three sentences is a wall nobody reaches the end of — so each note carries
    # the single thing that would change the choice and stops, with the rest
    # left to the model card the name links to. The older lists in this file
    # predate the rule and still run to three sentences.
    "transformers-text": [
        {
            "id": "Qwen/Qwen3-4B-Instruct-2507",
            "label": "Qwen3 4B Instruct",
            "size_gb": 8.1,
            "note": "The one to start with: the strongest all-rounder that "
                    "still fits a 16GB machine.",
        },
        {
            "id": "microsoft/Phi-4-mini-instruct",
            "label": "Phi-4 mini (3.8B)",
            "size_gb": 7.7,
            "note": "Punches above its size on reasoning and maths, and MIT "
                    "licensed.",
        },
        {
            "id": "Qwen/Qwen3-1.7B",
            "label": "Qwen3 1.7B",
            "size_gb": 4.1,
            "note": "The one to pick with no GPU.",
        },
        {
            "id": "Qwen/Qwen3-8B",
            "label": "Qwen3 8B",
            "size_gb": 16.4,
            "note": "Best quality here, and the only one that needs a GPU.",
        },
    ],
    "diffusers-image": [
        {
            "id": "black-forest-labs/FLUX.2-klein-4B",
            "label": "FLUX.2 klein 4B",
            "size_gb": 2.6,
            "note": "Quantized transformer (Q4_K_M) instead of the ~8GB bf16 "
                    "original — the full-precision one OOMs on 16GB machines.",
        },
    ],
    # The same model, converted for MLX — and unloadable by the runner above,
    # which is the now-familiar shape: a repo belongs to a backend, not to a
    # capability. `size_gb` is the whole snapshot, because unlike the diffusers
    # recipe there is nothing skipped and no second repo: every component is
    # already 4-bit. (2026-08-15 Hub metadata sums to 4.62e9 bytes. D303's
    # benchmark says 4.3GB for the same download — that is GiB; this field is
    # decimal GB, as D295 defines it, and the two numbers agree.)
    "mflux-image": [
        {
            "id": "mlx-community/FLUX.2-Klein-4B-4bit",
            "label": "FLUX.2 klein 4B (MLX 4-bit)",
            "size_gb": 4.6,
            "note": "One repo instead of the Diffusers split, and quicker per "
                    "image — but it reserves far more memory while running.",
        },
    ],
    # MLX conversions ONLY, and this is the third mutually unloadable Whisper
    # list in the app: a `mlx-community` repo carries `weights.npz` (or
    # `weights.safetensors`), which is neither CTranslate2's `model.bin` nor
    # transformers' `model.safetensors`. Suggesting across the line is exactly
    # the trap `faster_whisper/worker.py` had to write an error message about,
    # now with three ways to fall into it.
    #
    # These are the repos an Apple Silicon machine sees, and it sees them
    # BECAUSE it resolves to this runner — which since D302 is a user's choice
    # and not only a hardware fact. Switching the engine on the Preferences page
    # changes this list, which is correct and must not be silent; the page says
    # so (`describe`'s `runnerLabel`).
    #
    # Sizes use the same full-snapshot Hub metadata estimate as the lists above
    # (2026-08-15). **One line each**, per the rule the transformers list
    # states: a shortlist is read by sweeping it.
    "mlx-whisper": [
        {
            "id": "mlx-community/whisper-large-v3-turbo",
            "label": "Whisper large-v3 turbo (MLX)",
            "size_gb": 1.6,
            "note": "The one to start with: large-v3 accuracy at a fraction of "
                    "its decoding cost, and faster than real time by a wide "
                    "margin on Metal.",
        },
        {
            "id": "mlx-community/whisper-large-v3-mlx",
            "label": "Whisper large-v3 (MLX)",
            "size_gb": 3.1,
            "note": "The full model, for a recording turbo handles badly — "
                    "twice the disk and several times the decoding.",
        },
        {
            "id": "mlx-community/whisper-medium-mlx",
            "label": "Whisper medium (MLX)",
            "size_gb": 1.5,
            "note": "The middle option. Slower than turbo for no gain in "
                    "English; worth trying on a language turbo struggles with.",
        },
        {
            "id": "mlx-community/whisper-small-mlx",
            "label": "Whisper small (MLX)",
            "size_gb": 0.5,
            "note": "Light and quick, and noticeably weaker — it drops names "
                    "and punctuation the larger models get right.",
        },
    ],
    # CTranslate2 conversions ONLY. `openai/whisper-large-v3` is the repo
    # everyone reaches for and it does not load here — the runner reads
    # CTranslate2's `model.bin`, not transformers' safetensors — so suggesting
    # one would hand the user the exact failure `worker.py` had to write an
    # error message about.
    #
    # Sizes use the same full-snapshot Hub metadata estimate as the Transformers
    # list above (2026-08-14), including model.bin plus tokenizer and configs.
    "faster-whisper": [
        {
            "id": "deepdml/faster-whisper-large-v3-turbo-ct2",
            "label": "Whisper large-v3 turbo (CT2)",
            "size_gb": 1.6,
            "note": "large-v3 accuracy at roughly a quarter of its decoding "
                    "cost — the one to start with. Usable on CPU: a laptop "
                    "transcribes faster than real time.",
        },
        {
            "id": "Systran/faster-whisper-medium",
            "label": "Whisper medium (CT2)",
            "size_gb": 1.5,
            "note": "The middle option. Slower than turbo for no gain in "
                    "English; worth trying if a language turbo handles badly "
                    "is the problem.",
        },
        {
            "id": "Systran/faster-whisper-small",
            "label": "Whisper small (CT2)",
            "size_gb": 0.5,
            "note": "Fast and light enough for an old machine. Noticeably "
                    "weaker — it drops names and punctuation the larger models "
                    "get right.",
        },
    ],
}


def _runner_for(capability: str) -> registry.Runner | None:
    """The runner whose suggestions apply to `capability` HERE.

    The one that would actually LOAD, asked of the registry rather than decided
    again — a second copy of the resolution rule is how a page comes to offer a
    model the loader refuses. Falls back to the first runner REGISTERED for the
    capability when none can run here, because a capability this machine cannot
    serve is still worth listing with its reason (see `describe`), and an empty
    list under an explained heading says less than a populated one.
    """
    resolved = registry.for_capability(capability)
    if resolved is not None:
        return resolved
    return next((r for r in registry.all_runners() if r.capability == capability), None)


def for_runner(code: str) -> list[dict]:
    return list(SUGGESTIONS.get(code, ()))


def for_capability(capability: str) -> list[dict]:
    """What to suggest for `capability` on THIS machine."""
    runner = _runner_for(capability)
    return for_runner(runner.code) if runner else []


def default_for(capability: str) -> str | None:
    """The default for a capability: what "just load something" means here.

    Computed per call rather than baked into a module-level table, because the
    answer now depends on which runner resolves — and a table built at import
    time would freeze one machine's answer into the module and force every test
    to patch a private.
    """
    entries = for_capability(capability)
    return entries[0]["id"] if entries else None


def all_suggested_ids() -> set[str]:
    """Every suggested repo id, for the AI Models page's checkmarks.

    Deliberately EVERY runner's, not just the resolvable one's: the checkmark
    answers "is this on my disk", and a machine that has an MLX model cached
    from a previous life should be told so rather than have the row go quiet.
    """
    return {entry["id"] for entries in SUGGESTIONS.values() for entry in entries}


def describe() -> list[dict]:
    """The catalog grouped by capability, with each capability's availability.

    Availability rides along because a suggestion for something this machine
    cannot run is still worth showing — with the reason — rather than hiding,
    which would leave a user hunting for a feature that never was.

    The runner is resolved the way a LOAD resolves it, which is the fix D293
    needed: this used to take the first runner registered for the capability
    whatever its availability, so a Windows machine — where the MLX row is first
    and unavailable, and the transformers row below it is fine — would have been
    told text generation "needs Apple Silicon" while a runner sat ready to serve
    it, and shown four MLX repos it could not load.
    """
    rows = []
    for capability in registry.capabilities():
        runner = _runner_for(capability)
        status = runner.available() if runner else registry.Availability(False, "no runner")
        rows.append(
            {
                "capability": capability,
                "runner": runner.code if runner else None,
                # The backend in words ("Transformers (PyTorch)"), because with
                # two runners per capability the code alone stopped being
                # something a page could show a person.
                "runnerLabel": runner.label if runner else None,
                # …and the qualifier-free one, which is what the Discover
                # heading uses ("via MLX Whisper"): that caption says which
                # backend these suggestions belong to, not which backend to
                # pick, so the platform half is noise there.
                "runnerShortLabel": runner.short if runner else None,
                # …and what using it is LIKE, which is the sentence someone
                # wants BEFORE they start an 8GB download rather than after.
                "runnerNote": runner.note or None if runner else None,
                "available": status.ok,
                "reason": status.reason or None,
                "default": for_capability(capability)[0]["id"]
                if for_capability(capability) else None,
                "models": for_capability(capability),
            }
        )
    return rows

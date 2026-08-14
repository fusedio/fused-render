"""Which models to suggest, per capability (SPEC §40).

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

Sizes are the on-disk download, and the notes are frank about the trade — "tight
on 16GB" is the sentence that stops someone starting an 8GB pull they will regret.
"""

from __future__ import annotations

from fused_render.ai import registry

#: capability -> suggested models, best first. `size_gb` is the download —
#: **None when nobody has measured it**, which the card shows as "—" rather than
#: as a number someone would plan a multi-GB download around (the same no-guess
#: rule the Hub result cards follow, D255). `note` is why you would or would not
#: pick this one.
SUGGESTIONS: dict[str, list[dict]] = {
    registry.TEXT_GENERATION: [
        {
            "id": "mlx-community/Qwen3-8B-4bit",
            "label": "Qwen3 8B (4-bit)",
            "size_gb": 4.6,
            "note": "The safest strong option: fast, good at reasoning and code, "
                    "comfortable on 16GB.",
        },
        {
            "id": "mlx-community/gemma-3-12b-it-4bit",
            "label": "Gemma 3 12B (4-bit)",
            "size_gb": 8.1,
            "note": "Best quality that fits on a laptop. Tight on 16GB — close "
                    "other heavy apps first.",
        },
        {
            "id": "mlx-community/gemma-3-4b-it-4bit",
            "label": "Gemma 3 4B (4-bit)",
            "size_gb": 3.4,
            "note": "Very fast and light. Noticeably weaker output, but it runs "
                    "anywhere MLX does.",
        },
        # Bonsai 27B (prism-ml). The family ships eight repos and most of them
        # cannot run here: the GGUF builds are llama.cpp's format, and the AWQ
        # builds are both AWQ and image-text-to-text. The MLX ones are what is
        # left, and only the 2-bit is listed — the 1-bit sibling is omitted
        # deliberately rather than forgotten (see the note below).
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
            "note": "27B at roughly two bits per weight, so it costs less on "
                    "disk than Gemma 3 12B. Comfortable on 16GB. Ternary "
                    "quantization is new, so measure it against a 4-bit model "
                    "you already trust.",
        },
    ],
    registry.IMAGE_GENERATION: [
        {
            "id": "black-forest-labs/FLUX.2-klein-4B",
            "label": "FLUX.2 klein 4B",
            "size_gb": 2.6,
            "note": "Quantized transformer (Q4_K_M) instead of the ~8GB bf16 "
                    "original — the full-precision one OOMs on 16GB machines.",
        },
    ],
    # CTranslate2 conversions ONLY. `openai/whisper-large-v3` is the repo
    # everyone reaches for and it does not load here — the runner reads
    # CTranslate2's `model.bin`, not transformers' safetensors — so suggesting
    # one would hand the user the exact failure `worker.py` had to write an
    # error message about.
    #
    # Sizes are None because nobody has measured a completed download of these
    # on disk, and the file's own rule is that an unmeasured size shows as "—"
    # rather than as a figure someone plans a multi-GB pull around (D255).
    registry.SPEECH_TO_TEXT: [
        {
            "id": "deepdml/faster-whisper-large-v3-turbo-ct2",
            "label": "Whisper large-v3 turbo (CT2)",
            "size_gb": None,
            "note": "large-v3 accuracy at roughly a quarter of its decoding "
                    "cost — the one to start with. Usable on CPU: a laptop "
                    "transcribes faster than real time.",
        },
        {
            "id": "Systran/faster-whisper-medium",
            "label": "Whisper medium (CT2)",
            "size_gb": None,
            "note": "The middle option. Slower than turbo for no gain in "
                    "English; worth trying if a language turbo handles badly "
                    "is the problem.",
        },
        {
            "id": "Systran/faster-whisper-small",
            "label": "Whisper small (CT2)",
            "size_gb": None,
            "note": "Fast and light enough for an old machine. Noticeably "
                    "weaker — it drops names and punctuation the larger models "
                    "get right.",
        },
    ],
}

#: The default for each capability: what "just load something" means.
DEFAULTS = {
    capability: entries[0]["id"]
    for capability, entries in SUGGESTIONS.items()
    if entries
}


def for_capability(capability: str) -> list[dict]:
    return list(SUGGESTIONS.get(capability, ()))


def default_for(capability: str) -> str | None:
    return DEFAULTS.get(capability)


def all_suggested_ids() -> set[str]:
    """Every suggested repo id, for the AI Models page's checkmarks."""
    return {entry["id"] for entries in SUGGESTIONS.values() for entry in entries}


def describe() -> list[dict]:
    """The catalog grouped by capability, with each capability's availability.

    Availability rides along because a suggestion for something this machine
    cannot run is still worth showing — with the reason — rather than hiding,
    which would leave a Windows user wondering where text generation went.
    """
    rows = []
    for capability in registry.capabilities():
        runner = next(
            (r for r in registry.all_runners() if r.capability == capability), None)
        status = runner.available() if runner else registry.Availability(False, "no runner")
        rows.append(
            {
                "capability": capability,
                "runner": runner.code if runner else None,
                "available": status.ok,
                "reason": status.reason or None,
                "default": DEFAULTS.get(capability),
                "models": for_capability(capability),
            }
        )
    return rows

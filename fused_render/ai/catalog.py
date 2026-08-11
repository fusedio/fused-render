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

#: capability -> suggested models, best first. `size_gb` is the download; `note`
#: is why you would or would not pick this one.
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

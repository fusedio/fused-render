"""Architecture-aware engine detection for the Hub search pane (D1287+).

**The failure this exists to fix.** A user pressed Download on `OzzyGT/
MiniMax_H3_sdnq_4bit_pruned` (`pipeline_tag`: `text-to-video`, `library_name`:
`diffusers`) and got a raw Python traceback on the job row instead of a chip
that said the repo would not load here. The repo ships no flat
`model_index.json` and no `transformer-distilled*.safetensors` — it is a
Diffusers *modular* pipeline (`modular_model_index.json` plus per-component
`transformer/`, `transformer_ref/`, `vae/`, `scheduler/` folders), a layout
`hub_loadable.loadable_kind` had no name for, so it fell through to the
`("any", None)` default and was offered as loadable by every runner.

This module is the missing name: `resolve(raw)` reads a Hub search row's
`raw` dict — the SAME blob `_EXPAND` already fetches, nothing more — and
answers, purely: what architecture is this, and what engine would load it.

**Pure dict walk, no I/O, no `hub_models` import.** `resolve()` runs for
EVERY row in a page of results (not just ones that fail loadability — the
drawer shows an Architecture line for every row), so it must cost nothing
more than the dict/list reads any other per-row `raw`-derived fact already
costs (`_file_format`, `_has_diffusers_index`, ...). The one exception is
`shipped`, which asks the registry whether ANY runner code is wired to a
recognised engine at all — a fact about this codebase, not about the row,
and unrelated to the machine's hardware or installed venvs (`registry.
by_code` is a lookup in a static tuple, not a filesystem or subprocess
call). It is memoized process-wide (`_SHIPPED_CACHE`) exactly the way
`hub_loadable.mlx_vlm_model_types` memoizes its own registry-adjacent
lookup, so after the first row of a given engine every later row's `shipped`
read is a plain dict hit.

**The small, one-place engine table.** `_ENGINE_RUNNER_CODES` maps each
engine name this module recognises to the runner `code`s that would open it
— not duplicated anywhere else, and deliberately small: adding a real new
runner for a recognised engine only needs a code appended to its row here,
never a new branch in `resolve()` itself.
"""
from __future__ import annotations

from dataclasses import dataclass

from fused_render.ai.runners import formats


@dataclass(frozen=True)
class Architecture:
    """One row's resolved architecture fact — never fetched, always derived
    from a `raw` dict already on hand.

    `name`: the short architecture/pipeline-class string (`"MiniMaxH3Pipeline"`,
    `"Qwen3VLForConditionalGeneration"`, `"gemma3"`), or `None` when nothing in
    `raw` names one.

    `engine`: the engine that would load this architecture (`"Diffusers"`,
    `"llama.cpp"`, `"MLX"`, `"ltx-2-mlx"`, `"ONNX Runtime"`,
    `"Sentence Transformers"`), or `None` when no engine mapping recognises
    the repo's format signals at all.

    `shipped`: whether THIS APP has a runner code wired to `engine` — never
    hardcoded per engine; see `_ENGINE_RUNNER_CODES` and `_is_shipped`. Always
    `False` when `engine` is `None`.
    """

    name: str | None
    engine: str | None
    shipped: bool


#: engine name -> the runner `code`s that open it. The ONE place this
#: mapping lives — see the module docstring. An engine mapped to an empty
#: tuple is a recognised FORMAT/library fact with no runner behind it yet
#: (`"Sentence Transformers"`: the embedding runners gate on `model_type`,
#: never on this library name alone, so no runner code is keyed to it).
_ENGINE_RUNNER_CODES: dict[str, tuple[str, ...]] = {
    "Diffusers": ("diffusers-image", "diffusers-image-cuda", "diffusers-image-rocm"),
    "llama.cpp": ("llamacpp-text", "llamacpp-text-vulkan"),
    "MLX": ("mlx-text",),
    "ltx-2-mlx": ("ltx-video",),
    "ONNX Runtime": ("onnx-embed", "onnx-embed-directml", "onnx-embed-cuda",
                      "onnx-embed-rocm"),
    "Sentence Transformers": (),
}

#: engine name -> whether some runner code in `_ENGINE_RUNNER_CODES[engine]`
#: is registered, memoized process-wide (registry's `_RUNNERS` tuple is
#: static for the process's lifetime — this never goes stale). Cleared by
#: `reset_cache()`, the same test hook `hub_loadable.reset_cache` provides
#: for its own registry-adjacent cache.
_SHIPPED_CACHE: dict[str, bool] = {}


def reset_cache() -> None:
    """Test hook — a test that monkeypatches `registry.by_code` must not
    see a stale reading left by an earlier test."""
    _SHIPPED_CACHE.clear()


def _is_shipped(engine: str) -> bool:
    if engine in _SHIPPED_CACHE:
        return _SHIPPED_CACHE[engine]
    from fused_render.ai import registry

    codes = _ENGINE_RUNNER_CODES.get(engine, ())
    result = any(registry.by_code(code) is not None for code in codes)
    _SHIPPED_CACHE[engine] = result
    return result


def _sibling_names(raw: dict) -> frozenset[str]:
    siblings = raw.get("siblings")
    if not isinstance(siblings, list):
        return frozenset()
    names = [s.get("rfilename") for s in siblings if isinstance(s, dict)]
    return frozenset(n for n in names if isinstance(n, str))


def _resolve_name(raw: dict) -> str | None:
    """Signal order (brief item 1): a `diffusers:<PipelineClass>` tag, then
    `config.diffusers._class_name`, then `config.architectures[0]`, then
    `config.model_type`, then `library_name` alone."""
    tags = raw.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("diffusers:"):
                cls = tag.split(":", 1)[1]
                if cls:
                    return cls
    config = raw.get("config")
    if isinstance(config, dict):
        diffusers_cfg = config.get("diffusers")
        if isinstance(diffusers_cfg, dict):
            cls = diffusers_cfg.get("_class_name")
            if isinstance(cls, str) and cls:
                return cls
        archs = config.get("architectures")
        if isinstance(archs, list) and archs and isinstance(archs[0], str) and archs[0]:
            return archs[0]
        model_type = config.get("model_type")
        if isinstance(model_type, str) and model_type:
            return model_type
    library = raw.get("library_name")
    if isinstance(library, str) and library:
        return library
    return None


def _resolve_engine(raw: dict, names: frozenset[str]) -> str | None:
    """Which engine's format signals this row carries — checked in an order
    that puts the two unambiguous, single-file-format checks first (a
    directory of safetensors says nothing about modality on its own; a
    `.gguf` or the LTX split manifest does), then the broader Diffusers/MLX/
    ONNX/Sentence-Transformers library and manifest signals. Mirrors
    `formats.loaders`'s own DECISIVE-first shape, at a much smaller scale —
    this module only needs to name the engine, not classify the full
    snapshot."""
    if formats.has_ltx_split_layout(names):
        return "ltx-2-mlx"
    if any(name.lower().endswith(".gguf") for name in names):
        return "llama.cpp"
    library = raw.get("library_name")
    library = library if isinstance(library, str) else None
    if (formats.DIFFUSERS_INDEX in names
            or formats.DIFFUSERS_MODULAR_INDEX in names
            or library == "diffusers"):
        return "Diffusers"
    if any(name.lower().endswith(".onnx") for name in names) or library == "onnx":
        return "ONNX Runtime"
    tags = raw.get("tags")
    tags = tags if isinstance(tags, list) else []
    if library == "mlx" or any(isinstance(t, str) and t.lower() == "mlx" for t in tags):
        return "MLX"
    if library == "sentence-transformers":
        return "Sentence Transformers"
    return None


def resolve(raw: dict) -> Architecture:
    """`Architecture` for one Hub search row's `raw` dict — the same blob
    `_EXPAND` already fetched, no extra request, no filesystem lookup, no
    import from `hub_models`.

    Unknown always resolves to unloadable-safe defaults: an unrecognised
    architecture, an absent `config`, empty `siblings`, all read as `name=
    None, engine=None, shipped=False` — never anything that would make a
    row read as "supports nothing" on its own; `hub_loadable.admission`'s
    own fallback text carries that meaning, not this dataclass.
    """
    names = _sibling_names(raw)
    name = _resolve_name(raw)
    engine = _resolve_engine(raw, names)
    shipped = _is_shipped(engine) if engine is not None else False
    return Architecture(name=name, engine=engine, shipped=shipped)

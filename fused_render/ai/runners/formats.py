"""What each backend's WEIGHTS look like on disk, written once (SPEC §40).

A repo belongs to a BACKEND, not to a capability — three mutually unloadable
formats of Whisper are the standing proof, and `catalog.py` is keyed by runner
for the same reason. Every runner therefore checks the format by NAME before it
imports anything, so that a repo in the wrong shape produces a sentence naming
the format you have and the format this runner needs, rather than whatever
`FileNotFoundError` the library happens to raise.

Those checks used to be a constant apiece inside each `worker.py`. This module
is where they live now, because a SECOND reader appeared: the AI Models page
puts an engine tag on every cached repo, and the one thing that tag must never
do is promise a load the runner then refuses. Two copies of "what a CTranslate2
repo looks like" is exactly how that promise breaks.

**Stdlib only, and no import of `fused_render`.** It is imported by every
runner's own interpreter — which is a separate venv with the app's package
deliberately not on its path (see `supervisor._child_env`) — through the same
`sys.path` insert that reaches `worker_base`. The server reaches it as
`fused_render.ai.runners.formats`. Both readings must work, so nothing here may
import anything either side does not already have.

Runner CODES appear here as bare strings rather than as an import from
`registry`, for that same reason. `test_ai_formats.py` asserts every code this
module names is a registered runner, which is the drift this would otherwise
invite.
"""

from __future__ import annotations

import os

#: CTranslate2 writes one `model.bin` beside a plain-JSON config. Checked by
#: name because the loader's own failure is `Unable to open file 'model.bin'`,
#: which does not tell a user their download was the wrong FORMAT.
CT2_WEIGHTS = "model.bin"

#: An MLX conversion of Whisper. `.npz` is what `mlx-community` publishes today
#: and `.safetensors` is what `mlx_whisper.load_models` prefers when it is
#: there, so a repo with either is loadable.
MLX_WHISPER_WEIGHTS = ("weights.npz", "weights.safetensors")

#: What an mflux-readable snapshot always has: component subfolders of MLX
#: safetensors, rather than the single-file layout diffusers writes.
MFLUX_COMPONENTS = ("transformer", "text_encoder", "vae")

#: Repo id -> the mflux VARIANT class that loads it and the model config that
#: describes it. mflux has no `AutoPipeline`, and the variant and its config are
#: two arguments nothing can guess, so an unknown repo is refused with a
#: sentence rather than attempted.
#:
#: Here rather than in the worker because it is the OTHER half of "can mflux
#: load this": the components can be perfect and the repo still unbuildable,
#: and a card that showed the engine off the layout alone would offer a Load
#: for every MLX diffusion repo on the Hub.
MFLUX_VARIANTS = {
    "mlx-community/FLUX.2-Klein-4B-4bit": {
        "variant": "Flux2Klein",
        "module": "mflux.models.flux2.variants",
        "config": "flux2_klein_4b",
    },
}

#: A diffusers pipeline names itself here, and `from_pretrained` reads it.
DIFFUSERS_INDEX = "model_index.json"

#: Repo id -> the ONE file this app fetches out of it, and what it is a part of.
#:
#: **Repos the user never chose.** Two of them land in the Hub cache because a
#: runner needs a piece of them: the quantized transformer the FLUX.2 recipe
#: swaps in, and the speech detector the MLX whisper runner filters silence
#: with. Neither is a model — nothing here can load either one on its own — so
#: the AI Models page used to show them as peers of real models with the quiet
#: "no engine" tag and no explanation, and a user reclaiming 2.4GB by deleting
#: the mystery row broke the image model that needs it.
#:
#: Here, in `formats.py`, for the reason the module docstring gives about
#: `MFLUX_VARIANTS`: the ids are named inside RUNNER folders, which are separate
#: venvs the server process cannot import, and the page and the worker must not
#: be able to disagree about what is on the disk. The workers read `file` from
#: here rather than carrying their own copy, and `test_ai_formats.py` asserts
#: every recipe's component repo appears here — so a new recipe cannot
#: reintroduce a mystery row without failing a test.
#:
#: `of` is the repo id this is part of, or None when it belongs to an ENGINE
#: rather than to a model (the VAD serves every transcription, whatever model is
#: loaded). `part` is the noun the card wears; `owner` is what it is part of, in
#: the words the rest of the UI uses.
COMPONENT_REPOS = {
    "unsloth/FLUX.2-klein-4B-GGUF": {
        "file": "flux-2-klein-4b-Q4_K_M.gguf",
        "of": "black-forest-labs/FLUX.2-klein-4B",
        "owner": "FLUX.2 klein 4B",
        "part": "quantized transformer",
        "what": (
            "The 4-bit transformer FLUX.2 klein 4B loads instead of its own "
            "8GB one — fetched by the Diffusers image engine, not a model you "
            "can load on its own. Deleting it makes that model download it "
            "again on its next load."
        ),
    },
    "onnx-community/silero-vad": {
        "file": "onnx/model.onnx",
        "of": None,
        "owner": "MLX Whisper",
        "part": "speech detector",
        "what": (
            "The 2MB Silero detector the MLX Whisper engine uses to find the "
            "speech in a recording and skip the silence — fetched with any "
            "whisper download so an offline machine still has it. Deleting it "
            "costs a slower transcription, not a broken one."
        ),
    },
}


def component(repo_id: str) -> dict | None:
    """What `repo_id` is a component of, or None for an ordinary repo."""
    return COMPONENT_REPOS.get(repo_id)

#: What torch can open. `.bin` and `.pt` are pickles: readable, but with no
#: cheap header, which is why the page counts parameters only from safetensors.
TORCH_WEIGHTS = (".safetensors", ".bin", ".pt")

#: Quantizations `transformers_text/worker.py` refuses BY NAME, each with the
#: sentence it refuses them with: what transformers raises for an AWQ repo with
#: no autoawq installed is a bare ImportError several frames inside a loader,
#: and the user reading it cannot tell that their repo was the wrong kind
#: rather than their download broken.
UNLOADABLE_QUANT = {
    "awq": "an AWQ checkpoint, which needs a package this runner does not ship",
    "gptq": "a GPTQ checkpoint, which needs a package this runner does not ship",
    "bitsandbytes": (
        "a bitsandbytes checkpoint, which needs bitsandbytes and an NVIDIA GPU "
        "— this runner ships neither"
    ),
    "compressed-tensors": (
        "a compressed-tensors checkpoint, which needs a package this runner "
        "does not ship"
    ),
}

#: The runners whose format evidence also settles WHAT THE MODEL IS. A
#: `weights.npz` is a Whisper conversion and nothing else; a `model_index.json`
#: is a diffusion pipeline. The two text runners are the opposite case — a
#: directory of safetensors says nothing about the modality — so a match there
#: never implies a capability.
DECISIVE = ("faster-whisper", "mlx-whisper", "mflux-image", "diffusers-image")


def is_mlx_checkpoint(config: dict) -> bool:
    """MLX's own quantization: bit-packed for Metal kernels, and meaningless to
    torch. The `group_size` is what distinguishes it from every other
    `quantization` block — the same test `transformers_text` raises on."""
    block = config.get("quantization")
    return isinstance(block, dict) and "group_size" in block


def unloadable_quant(config: dict) -> str | None:
    """The quant method torch cannot read here, or None."""
    block = config.get("quantization_config")
    method = block.get("quant_method") if isinstance(block, dict) else None
    if isinstance(method, str) and method.lower() in UNLOADABLE_QUANT:
        return method.lower()
    return None


def has_ct2_weights(names) -> bool:
    return CT2_WEIGHTS in names


def has_mlx_whisper_weights(names) -> bool:
    return any(name in names for name in MLX_WHISPER_WEIGHTS)


#: What a CTranslate2 conversion of WHISPER carries beyond `model.bin`, which
#: is the loader's own (looser) test. The page needs the stricter one: it puts
#: a task label on the card, and "model.bin" alone is also the name a stray
#: pickle in any repo can have.
_CT2_WHISPER_KEYS = ("alignment_heads", "lang_ids", "suppress_ids")
_CT2_WHISPER_FILES = ("preprocessor_config.json", "vocabulary.json", "vocabulary.txt")


def is_ct2_whisper(names, config: dict) -> bool:
    """CT2 weights AND the Whisper-shaped evidence beside them."""
    if not has_ct2_weights(names):
        return False
    return (any(key in config for key in _CT2_WHISPER_KEYS)
            or any(name in names for name in _CT2_WHISPER_FILES))


def has_mflux_components(dirnames) -> bool:
    return all(name in dirnames for name in MFLUX_COMPONENTS)


def missing_mflux_components(snapshot_dir: str) -> list[str]:
    """The component folders an mflux load needs and this snapshot lacks."""
    return [name for name in MFLUX_COMPONENTS
            if not os.path.isdir(os.path.join(snapshot_dir, name))]


def loaders(*, repo_id: str, names, dirnames, config: dict, torch_weights: bool) -> tuple[str, ...]:
    """Which runners' `load()` would accept this snapshot, by code.

    Format only: whether such a runner RUNS here, and whether the capability is
    one it serves, are the registry's questions and are asked by the caller.

    `names`/`dirnames` are the snapshot's top-level entries, `config` its
    `config.json` (empty when absent), and `torch_weights` whether anything in
    the tree is a file torch can open.
    """
    found: list[str] = []
    if has_ct2_weights(names):
        found.append("faster-whisper")
    if has_mlx_whisper_weights(names):
        found.append("mlx-whisper")
    if repo_id in MFLUX_VARIANTS and has_mflux_components(dirnames):
        found.append("mflux-image")
    if DIFFUSERS_INDEX in names:
        found.append("diffusers-image")
    # The two text runners read the same directory of safetensors, and which of
    # them gets it is a platform-and-preference question rather than a format
    # one — with the one exception torch states itself: an MLX checkpoint is
    # packed for Metal and torch cannot read it at all.
    if torch_weights and not unloadable_quant(config):
        if is_mlx_checkpoint(config):
            found.append("mlx-text")
        else:
            found.extend(("mlx-text", "transformers-text"))
    return tuple(found)

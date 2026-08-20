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

#: An MLX conversion of Whisper, in the two spellings that are DISTINCT — no
#: other format on the Hub calls a file `weights.npz` or `weights.safetensors`,
#: so either name alone settles it. `mlx_whisper.load_models` itself looks for
#: `model.safetensors` first, then these two — but `model.safetensors` is the
#: filename every transformers repo carries, so that third spelling is only
#: evidence TOGETHER with a whisper-shaped config (see
#: `is_mlx_whisper_snapshot`). mlx-community publishes all three: `weights.npz`
#: on most conversions, `weights.safetensors` on the large-v3-turbo era, and
#: `model.safetensors` on the newer quantized re-uploads (whisper-tiny.en-8bit).
MLX_WHISPER_WEIGHTS = ("weights.npz", "weights.safetensors")

#: …and the third spelling, shared with every transformers repo on the Hub.
MLX_WHISPER_SHARED_WEIGHTS = "model.safetensors"

#: What an MLX whisper `config.json` is: the OpenAI `ModelDimensions` fields,
#: verbatim. A transformers Whisper config spells the same facts as
#: `num_mel_bins`/`d_model`/`encoder_layers` and a NeMo config has `target`, so
#: these keys appear together nowhere else. Two are required rather than one so
#: a stray `n_vocab` in some other config cannot claim a repo alone.
MLX_WHISPER_CONFIG_KEYS = ("n_mels", "n_audio_ctx", "n_vocab")

#: An MLX conversion of a NeMo Parakeet model — the single file `parakeet-mlx`
#: loads. Nothing distinguishing on its own: it is the name every transformers
#: repo on the Hub carries, which is exactly why the config below has to be
#: read too.
PARAKEET_WEIGHTS = "model.safetensors"

#: …and what DOES distinguish one. A Parakeet snapshot's `config.json` is
#: NeMo's training config, not a transformers one, and it names the class the
#: weights came out of in `target` — which is what `parakeet_mlx.from_config`
#: dispatches on. The prefix is narrowed to `…asr.models.` deliberately: NeMo
#: also ships TTS and LLM collections, and this runner loads neither, so a
#: check on the word "nemo" would offer a Load button for a speech SYNTHESIS
#: repo and fail inside a library that never had a chance.
NEMO_ASR_TARGET = "nemo.collections.asr.models."

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
#:
#: `vae` names the AUTOENCODER the conversion was made from, which is the key
#: `preview.PROJECTIONS` is indexed by. It is a class name out of diffusers and
#: mflux has no such class, so it can look like a stray fact in an MLX table —
#: it is not. The two image runners have to reach ONE row of ONE projection
#: table (a page must not be able to tell which engine rendered for it), the
#: torch one reads it off `type(pipe.vae).__name__`, and this side has no
#: equivalent to read: an mflux conversion carries the same latent space as the
#: repo it was converted from, and only this table knows which repo that was.
#: Verified rather than assumed — `bn.running_mean`/`running_var` are
#: bit-identical between the two repos (max|diff| = 0.0). A variant with no
#: `vae` simply gets no preview.
MFLUX_VARIANTS = {
    "mlx-community/FLUX.2-Klein-4B-4bit": {
        "variant": "Flux2Klein",
        "module": "mflux.models.flux2.variants",
        "config": "flux2_klein_4b",
        "vae": "AutoencoderKLFlux2",
    },
}

#: A diffusers pipeline names itself here, and `from_pretrained` reads it.
DIFFUSERS_INDEX = "model_index.json"

#: Repo id -> the ONE file this app fetches out of it, and what it is a part of.
#:
#: **Repos the user never chose.** They land in the Hub cache because a runner
#: needs a piece of them: the quantized transformer the FLUX.2 recipe swaps in,
#: the speech detector the MLX whisper runner filters silence with, and the two
#: sherpa-onnx models that put speaker labels on a transcript. None is a model —
#: nothing here can load any of them on its own — so
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
        # Not "MLX Whisper" since D319: the Parakeet engine reads the same
        # `runners/vad.py`, and a card naming one of the two engines that use
        # it would be wrong on whichever machine is running the other.
        "owner": "MLX transcription",
        "part": "speech detector",
        "what": (
            "The 2MB Silero detector the MLX transcription engines use to find "
            "the speech in a recording and skip the silence — fetched with any "
            "of their model downloads so an offline machine still has it. "
            "Deleting it costs a slower transcription, not a broken one."
        ),
    },
    "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0": {
        "file": "model.onnx",
        "of": None,
        "owner": "Whisper transcription",
        "part": "speaker segmenter",
        "what": (
            "The 6MB pyannote segmentation model that finds who is speaking "
            "when, for transcribe({diarize: true}) — used by BOTH speech-to-text "
            "engines, not a model you can load on its own. An ungated ONNX "
            "re-export (by csukuangfj, for sherpa-onnx) of pyannote/"
            "segmentation-3.0, MIT-licensed. Fetched on the first diarized "
            "transcription; deleting it makes the next one download it again."
        ),
    },
    "csukuangfj/speaker-embedding-models": {
        "file": "wespeaker_en_voxceleb_resnet34_LM.onnx",
        "of": None,
        "owner": "Whisper transcription",
        "part": "speaker embedding model",
        "what": (
            "The 27MB voice-fingerprint model that decides which of the "
            "segmenter's turns belong to the same person, for "
            "transcribe({diarize: true}) — used by BOTH speech-to-text engines, "
            "not a model you can load on its own. It is WeSpeaker's VoxCeleb "
            "ResNet34-LM speaker embedding, by the WeSpeaker team (CC BY 4.0), "
            "in the ONNX re-export sherpa-onnx reads. Fetched on the first "
            "diarized transcription; deleting it makes the next one download it "
            "again."
        ),
    },
}


def component(repo_id: str) -> dict | None:
    """What `repo_id` is a component of, or None for an ordinary repo."""
    return COMPONENT_REPOS.get(repo_id)

#: What torch can open. `.bin` and `.pt` are pickles: readable, but with no
#: cheap header, which is why the page counts parameters only from safetensors.
TORCH_WEIGHTS = (".safetensors", ".bin", ".pt")

#: Quantizations `runners/torch_text.py` refuses BY NAME, each with the
#: sentence it refuses them with: what transformers raises for an AWQ repo with
#: no autoawq installed is a bare ImportError several frames inside a loader,
#: and the user reading it cannot tell that their repo was the wrong kind
#: rather than their download broken.
UNLOADABLE_QUANT = {
    "awq": "an AWQ checkpoint, which needs a package this runner does not ship",
    "gptq": "a GPTQ checkpoint, which needs a package this runner does not ship",
    # **The reason here was rewritten on 2026-08-21 because the old one had
    # stopped being true, and the replacement is a MEASUREMENT rather than a
    # guess.** It used to say "needs bitsandbytes and an NVIDIA GPU — this
    # runner ships neither", and the GPU half is simply wrong now: bitsandbytes
    # 0.50.1 is MIT, publishes wheels and NO sdist for macos arm64,
    # manylinux x86_64 and aarch64, win_amd64 and win_arm64 (checked against
    # PyPI 2026-08-21, so AI-2a's wheels-only rule would NOT block it), and it
    # documents a dedicated CPU build. So "we cannot install it" is no longer
    # the obstacle — which left the only question that decides this: is 4-bit
    # NF4 fast enough on a CPU to be worth offering, given the default
    # resolution of all three folders installing this worker is a CPU torch?
    #
    # It was measured, not argued. `unsloth/Qwen3-4B-Instruct-2507-unsloth-bnb-4bit`
    # LOADS on `device_map="cpu"` and generates correct, coherent text — 219
    # `Linear4bit` modules, no error — and it halves resident memory (4.16GB
    # peak RSS against bf16's 8.55GB). It is also 3.6x SLOWER per token than
    # the bf16 checkpoint it would replace: 0.65 tok/s against 2.33 tok/s,
    # uncontended, same prompt and same 4B base model. Quantization here buys
    # memory and spends the one resource a CPU path has least of, and 0.65
    # tok/s is not a thing to put behind a Download button.
    #
    # **The caveat that keeps this open: that was an Apple Silicon M-series
    # (Mac17,3, 10 cores, 34GB), and this runner's bnb users would be Windows
    # and Linux x86.** bitsandbytes' optimized CPU path targets x86 AVX512/AMX,
    # none of which exists on arm64 — so an x86 box could plausibly land
    # somewhere very different, and this measurement CANNOT be generalised to
    # it. What it does establish is that the refusal is no longer about the
    # licence, the wheels or the card. Re-measure on x86 before reversing this;
    # the dependency is deliberately still absent from the three
    # `transformers_text*/pyproject.toml` files, so the refusal below is also
    # literally true — an unlisted package cannot be imported.
    "bitsandbytes": (
        "a bitsandbytes checkpoint, which needs bitsandbytes — a package this "
        "runner does not install, because 4-bit inference on the CPU it "
        "defaults to runs several times slower than the unquantized weights"
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
DECISIVE = ("faster-whisper", "mlx-whisper", "mflux-image", "diffusers-image",
            # Every hardware variant of the diffusers runner, because membership
            # here is a statement about the FORMAT — a `model_index.json` is a
            # diffusion pipeline whichever wheel opens it — and not about a
            # machine. Listing only the CPU row would make capability inference
            # depend on which build happens to be registered, so a build that
            # ever shipped the accelerated rows alone would silently stop
            # putting "text to image" on a cached FLUX card. The TEXT variants
            # stay out for the same reason `transformers-text` is out: a
            # directory of safetensors says nothing about the modality.
            "diffusers-image-cuda", "diffusers-image-rocm",
            # A NeMo ASR `target` is as decisive as a `weights.npz`: the config
            # names an ASR class, and nothing else in this app can read it.
            "parakeet-mlx")


def is_mlx_checkpoint(config: dict) -> bool:
    """MLX's own quantization: bit-packed for Metal kernels, and meaningless to
    torch. The `group_size` is what distinguishes it from every other
    `quantization` block — the same test `torch_text` raises on."""
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
    """The DISTINCT spellings only — see `is_mlx_whisper_snapshot` for the full
    test a runner and the page should ask."""
    return any(name in names for name in MLX_WHISPER_WEIGHTS)


def is_mlx_whisper_config(config: dict) -> bool:
    """The native whisper `ModelDimensions` config, which no transformers or
    NeMo checkpoint carries."""
    return sum(key in config for key in MLX_WHISPER_CONFIG_KEYS) >= 2


def is_mlx_whisper_snapshot(names, config: dict) -> bool:
    """Would `mlx_whisper.load_models` accept this snapshot?

    Either a distinctly-named weight file, or the transformers-shared
    `model.safetensors` with the whisper-shaped config beside it — the layout
    the newer quantized mlx-community re-uploads ship, which the filename test
    alone refused (the bug this function fixed).
    """
    if has_mlx_whisper_weights(names):
        return True
    return MLX_WHISPER_SHARED_WEIGHTS in names and is_mlx_whisper_config(config)


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


def is_parakeet_checkpoint(config: dict) -> bool:
    """A NeMo ASR export, which is the only thing `parakeet-mlx` can load.

    Read off `target` rather than off the filename, because the filename is
    `model.safetensors` — shared with every transformers checkpoint on the Hub
    — and off the config rather than the repo id, because a fine-tune under
    somebody's own account is the same format and deserves the same tag.
    """
    target = config.get("target")
    return isinstance(target, str) and target.startswith(NEMO_ASR_TARGET)


def has_mflux_components(dirnames) -> bool:
    return all(name in dirnames for name in MFLUX_COMPONENTS)


def missing_mflux_components(snapshot_dir: str) -> list[str]:
    """The component folders an mflux load needs and this snapshot lacks."""
    return [name for name in MFLUX_COMPONENTS
            if not os.path.isdir(os.path.join(snapshot_dir, name))]


#: **EVERY hardware variant of a torch runner, and this is the trap the split
#: had to walk around.** `ai_models.py` filters the engine row for a cached repo
#: on `r.code in meta.loaders`, so a code missing from here has no Load button
#: and no engine tag on a repo that engine loads perfectly — and AI-11e's
#: cached-model injection drops every such repo out of `models[]` as well. The
#: failure therefore lands exactly when an accelerated engine is the one
#: serving, and `test_ai_formats`'s original direction (every code named here is
#: registered) cannot see it, because the drift is the other way round.
#: `test_every_registered_runner_appears_in_loaders` closes that direction.
#:
#: A tuple per LIBRARY rather than four codes spelled into branches: the
#: variants read the same files by definition — the wheel differs, the format
#: does not — so a branch that could name three of them is a branch that can
#: forget one.
TRANSFORMERS_RUNNERS = ("transformers-text", "transformers-text-cuda",
                        "transformers-text-rocm")
DIFFUSERS_RUNNERS = ("diffusers-image", "diffusers-image-cuda",
                     "diffusers-image-rocm")


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
    if is_mlx_whisper_snapshot(names, config):
        found.append("mlx-whisper")
        # …and NOTHING else, for the Parakeet branch's reason: the newer
        # mlx-community re-uploads carry `model.safetensors` plus an MLX
        # `quantization` block, so the text branch below would offer to load a
        # speech model as a chat model. (The `weights.safetensors` era had the
        # same leak — `.safetensors` counts as torch weights — fixed by the
        # same return.)
        return tuple(found)
    if repo_id in MFLUX_VARIANTS and has_mflux_components(dirnames):
        found.append("mflux-image")
    if DIFFUSERS_INDEX in names:
        found.extend(DIFFUSERS_RUNNERS)
    if is_parakeet_checkpoint(config) and PARAKEET_WEIGHTS in names:
        found.append("parakeet-mlx")
        # …and NOTHING else, which is the point of returning here. A Parakeet
        # snapshot is a directory of safetensors, so the text branch below
        # would claim it too and the page would offer to load a speech model
        # as a chat model — the failure `DECISIVE` exists to prevent, arriving
        # by a new route.
        return tuple(found)
    # The two text runners read the same directory of safetensors, and which of
    # them gets it is a platform-and-preference question rather than a format
    # one — with the one exception torch states itself: an MLX checkpoint is
    # packed for Metal and torch cannot read it at all.
    if torch_weights and not unloadable_quant(config):
        if is_mlx_checkpoint(config):
            found.append("mlx-text")
        else:
            found.extend(("mlx-text", *TRANSFORMERS_RUNNERS))
    return tuple(found)

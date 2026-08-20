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
import struct

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

#: llama.cpp's single-file weights format (SPEC AI-11, `runners/llama_text.py`).
#: Unlike every other format in this module a `.gguf` needs no companion
#: config to identify — the vocabulary, the architecture and the model's own
#: chat template all live inside the one file's key-value metadata, which is
#: the reason GGUF is one file at all. So the presence check is the
#: extension, at the SNAPSHOT ROOT: `torch_text.py`'s own refusal already
#: treats "nothing but GGUF here" as the wrong-format case for transformers,
#: and this is that same evidence read the other way round, by the engine
#: that actually wants it.
#:
#: **Presence is not enough to call it TEXT, and that is a real bug this
#: module used to have.** GGUF is a container format, not a modality —
#: `city96/FLUX.1-dev-gguf` is an image model, `ggerganov/whisper.cpp`
#: publishes speech-recognition GGUFs, and a `has_gguf_weights` check alone
#: would tag either as `llamacpp-text` and put a Load button on the AI Models
#: page for a repo this runner cannot generate a token from — precisely the
#: failure `ai_models.py` used to describe in a comment about
#: `unsloth/FLUX.2-klein-4B-GGUF` before this runner existed to make the
#: mistake newly possible. `is_text_gguf` is the gate: it reads the file's OWN
#: `general.architecture` metadata (`gguf_architecture`) and checks it against
#: `GGUF_TEXT_ARCHITECTURES`, so a snapshot is only ever decisively
#: `llamacpp-text` when the GGUF itself says so — verified directly against a
#: real `city96/FLUX.1-dev-gguf` file (`general.architecture = "flux"`, not in
#: the table) and a real `unsloth/Qwen3.5-4B-GGUF` file (`"qwen35"`, in it),
#: 2026-08-21.
GGUF_EXTENSION = ".gguf"

#: llama.cpp's own architecture identifiers (`general.architecture` in a
#: GGUF's metadata) that denote a CAUSAL TEXT model — read directly off
#: `LLM_ARCH_NAMES` in llama.cpp's `src/llama-arch.cpp` at the commit this
#: runner vendors (SPEC AI-11, D402: llama-cpp-python 0.3.29 -> llama.cpp
#: `f05cf467`, 2026-06-13), MINUS the entries in that same table that are not
#: causal text generation: the BERT/T5 families (encoders and
#: encoder-decoders), `wavtokenizer-dec` (an audio codec), the embedding
#: variants (`gemma-embedding`, `llama-embed`, `pangu-embedded`), `clip`
#: (the table's own comment: "dummy, only used by llama-quantize"), and the
#: vision-language architectures whose text tower this runner has no code
#: path for (`qwen2vl`, `qwen3vl`, `qwen3vlmoe`, `cogvlm`, `hunyuan_vl`,
#: `paddleocr`, `deepseek2-ocr`).
#:
#: A DENYLIST would need updating every time llama.cpp adds an architecture
#: this app has never heard of; this allowlist instead fails toward "not
#: decisively text" for anything new, which just means a fresh architecture
#: does not get a Load button here until this table is refreshed — a missed
#: model, not a mislabelled one. Recheck against
#: `https://raw.githubusercontent.com/ggml-org/llama.cpp/<vendored commit>/src/llama-arch.cpp`
#: whenever the pin in `llamacpp_text/pyproject.toml` moves.
GGUF_TEXT_ARCHITECTURES = frozenset({
    "llama", "llama4", "deci", "falcon", "grok", "gpt2", "gptj", "gptneox",
    "mpt", "baichuan", "starcoder", "refact", "bloom", "stablelm", "qwen",
    "qwen2", "qwen2moe", "qwen3", "qwen3moe", "qwen3next", "qwen35",
    "qwen35moe", "phi2", "phi3", "phimoe", "plamo", "plamo2", "plamo3",
    "codeshell", "orion", "internlm2", "minicpm", "minicpm3", "gemma",
    "gemma2", "gemma3", "gemma3n", "gemma4", "gemma4-assistant",
    "starcoder2", "mamba", "mamba2", "jamba", "falcon-h1", "xverse",
    "command-r", "cohere2", "dbrx", "olmo", "olmo2", "olmoe", "openelm",
    "arctic", "deepseek", "deepseek2", "deepseek32", "chatglm", "glm4",
    "glm4moe", "glm-dsa", "bitnet", "jais", "jais2", "nemotron",
    "nemotron_h", "nemotron_h_moe", "exaone", "exaone4", "exaone-moe",
    "rwkv6", "rwkv6qwen2", "rwkv7", "arwkv7", "granite", "granitemoe",
    "granitehybrid", "chameleon", "plm", "bailingmoe", "bailingmoe2",
    "dots1", "arcee", "afmoe", "ernie4_5", "ernie4_5-moe", "hunyuan-moe",
    "hunyuan-dense", "smollm3", "gpt-oss", "lfm2", "lfm2moe", "dream",
    "smallthinker", "llada", "llada-moe", "seed_oss", "grovemoe", "apertus",
    "minimax-m2", "rnd1", "mistral3", "eagle3", "mistral4", "mimo2",
    "step35", "maincoder", "kimi-linear", "talkie", "mellum",
})

#: GGUF value-type codes this app can read directly (fixed-width scalars),
#: keyed to their byte width — from the GGUF spec's `gguf_type` enum.
_GGUF_SCALAR_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                      10: 8, 11: 8, 12: 8}
#: …and the two variable-width ones this app has to know how to SKIP: a
#: length-prefixed string (8) and a typed array (9), which can itself hold
#: strings — a GGUF's tokenizer vocabulary is exactly that, tens of thousands
#: of them, which is why `_gguf_skip_value` has to walk it rather than assume
#: a fixed width.
_GGUF_TYPE_STRING = 8
_GGUF_TYPE_ARRAY = 9

#: How much of a GGUF's own header this app reads before giving up on finding
#: `general.architecture`. Generous rather than tight: the key is written near
#: the very START of every GGUF llama.cpp's own converters produce — verified
#: directly (byte offset 70, key index 0) against a real
#: `unsloth/Qwen3.5-4B-GGUF` file, 2026-08-21 — so 2MB is headroom for an
#: unusual metadata ordering, not a budget this app expects to spend. A LOCAL
#: file read, never a network fetch (the caller has already downloaded or is
#: scanning an existing cache), so reading generously costs milliseconds, not
#: bytes billed to anyone's download.
_GGUF_HEADER_PEEK_BYTES = 2 * 1024 * 1024


def _gguf_read_string(buf: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<Q", buf, offset)
    offset += 8
    text = buf[offset:offset + length].decode("utf-8", errors="replace")
    return text, offset + length


def _gguf_skip_value(buf: bytes, offset: int, value_type: int) -> int:
    if value_type == _GGUF_TYPE_STRING:
        _text, offset = _gguf_read_string(buf, offset)
        return offset
    if value_type == _GGUF_TYPE_ARRAY:
        (item_type,) = struct.unpack_from("<I", buf, offset)
        offset += 4
        (length,) = struct.unpack_from("<Q", buf, offset)
        offset += 8
        for _ in range(length):
            offset = _gguf_skip_value(buf, offset, item_type)
        return offset
    return offset + _GGUF_SCALAR_SIZES[value_type]


def gguf_architecture(path: str) -> str | None:
    """`general.architecture` out of a GGUF file's own header, or None.

    A bounded LOCAL read (`_GGUF_HEADER_PEEK_BYTES`) and a hand-written
    parser rather than the `gguf` package: this module is stdlib-only and
    imported by every runner's own interpreter (see the module docstring),
    and `gguf` is a dependency only `diffusers_image/pyproject.toml`
    declares — pulling it in here would make every OTHER runner's venv able
    to import a package it never asked for, or would make this call silently
    unavailable in every venv that lacks it.

    Fails toward None — a truncated read (the peek window ended mid-value), a
    value type this app does not model, or a file that is not a GGUF at all
    are all "cannot tell", never a crash and never a guess. None is read by
    `is_text_gguf` as "not decisively text", which is the safe direction to
    fail in: a real text GGUF this cannot classify loses a Load button, an
    image or speech GGUF never gains one it would fail.
    """
    try:
        with open(path, "rb") as handle:
            buf = handle.read(_GGUF_HEADER_PEEK_BYTES)
    except OSError:
        return None
    try:
        if buf[:4] != b"GGUF":
            return None
        (kv_count,) = struct.unpack_from("<Q", buf, 16)
        offset = 24
        for _ in range(kv_count):
            key, offset = _gguf_read_string(buf, offset)
            (value_type,) = struct.unpack_from("<I", buf, offset)
            offset += 4
            if key == "general.architecture" and value_type == _GGUF_TYPE_STRING:
                value, _offset = _gguf_read_string(buf, offset)
                return value
            offset = _gguf_skip_value(buf, offset, value_type)
    except (struct.error, IndexError, UnicodeDecodeError):
        return None
    return None


def is_text_gguf(path: str) -> bool:
    """Would `llamacpp-text` actually load this GGUF — checked, not assumed
    from the extension alone. See `GGUF_TEXT_ARCHITECTURES`'s docstring."""
    return gguf_architecture(path) in GGUF_TEXT_ARCHITECTURES


#: Curated `(repo, file)` pairs `runners/llama_text.py` actually
#: downloads, keyed by an OPAQUE id — the GGUF's own filename, never parsed
#: for structure. See that module's docstring for why there is no
#: `repo:quant` id grammar: a GGUF repo commonly publishes two dozen
#: quantizations of one model, so a model here is really a `(repo, filename)`
#: pair rather than something a bare repo id can address.
#:
#: **Here, in `formats.py`, for the reason `COMPONENT_REPOS` states about
#: itself**: the runner is a separate venv the server process cannot import,
#: and TWO readers need this exact mapping and must not be able to disagree
#: about it — the AI Models page (deciding whether a curated entry is already
#: "downloaded", by REPO id, since that is how the local Hub cache is keyed;
#: and refusing to also show it as an undifferentiated second "cached" row)
#: and the worker itself (resolving a bare repo id BACK to the recipe that
#: fetched it, `llama_text._resolve_model_id`, for the id shape the page's
#: cache scan hands back). `test_ai_formats.py` asserts every id here also
#: appears in `catalog.SUGGESTIONS["llamacpp-text"]`, so the two cannot drift.
GGUF_RECIPES = {
    "Qwen3.5-4B-Q5_K_M.gguf": {
        "repo": "unsloth/Qwen3.5-4B-GGUF",
        "file": "Qwen3.5-4B-Q5_K_M.gguf",
    },
    "Qwen3.5-4B-Q8_0.gguf": {
        "repo": "unsloth/Qwen3.5-4B-GGUF",
        "file": "Qwen3.5-4B-Q8_0.gguf",
    },
    "Qwen3.5-9B-Q4_K_M.gguf": {
        "repo": "unsloth/Qwen3.5-9B-GGUF",
        "file": "Qwen3.5-9B-Q4_K_M.gguf",
    },
    "Qwen3.5-9B-Q8_0.gguf": {
        "repo": "unsloth/Qwen3.5-9B-GGUF",
        "file": "Qwen3.5-9B-Q8_0.gguf",
    },
    "Qwen3.8-27B-UD-Q3_K_XL.gguf": {
        "repo": "unsloth/Qwen3.8-27B-GGUF",
        "file": "Qwen3.8-27B-UD-Q3_K_XL.gguf",
    },
}

#: Quantizations `runners/torch_text.py` refuses BY NAME, each with the
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
            "parakeet-mlx",
            # A root-level `.gguf` is llama.cpp's format and nothing else in
            # this app reads one for TEXT (SPEC AI-11) — the diffusers image
            # runner's own GGUF use is a swapped-in COMPONENT of an otherwise
            # ordinary pipeline (`COMPONENT_REPOS`), not a snapshot whose root
            # is a bare `.gguf`, so the two cannot collide.
            "llamacpp-text")


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


def has_gguf_weights(names) -> bool:
    """A `.gguf` file at the snapshot ROOT — `names` is a top-level listing,
    never a recursive walk, so this cannot fire on a GGUF sitting inside some
    other pipeline's subfolder (`COMPONENT_REPOS`'s FLUX transformer is one).

    PRESENCE only — a container fact, not a modality one. `ai_models.py` uses
    this alone for its cosmetic "library: gguf" tag, which is honest about
    ANY GGUF repo whatever it contains. `loaders()` below asks the stricter
    question (`is_text_gguf`) before calling one decisively `llamacpp-text`.
    """
    return any(str(name).lower().endswith(GGUF_EXTENSION) for name in names)


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


def loaders(*, repo_id: str, names, dirnames, config: dict, torch_weights: bool,
           gguf_architecture: str | None = None) -> tuple[str, ...]:
    """Which runners' `load()` would accept this snapshot, by code.

    Format only: whether such a runner RUNS here, and whether the capability is
    one it serves, are the registry's questions and are asked by the caller.

    `names`/`dirnames` are the snapshot's top-level entries, `config` its
    `config.json` (empty when absent), `torch_weights` whether anything in the
    tree is a file torch can open, and `gguf_architecture` the caller's OWN
    reading of `gguf_architecture()` for whichever root `.gguf` file is
    present — passed in rather than read here because opening and parsing the
    file is I/O this pure evidence-classifier has never otherwise done, and
    the caller (`ai_models.py`) already has the snapshot path this needs.
    `None` when there is no GGUF, or when the caller could not read one.
    """
    found: list[str] = []
    if has_ct2_weights(names):
        found.append("faster-whisper")
    # Checked EARLY and returned on unconditionally, ahead of mflux/diffusers
    # below: a `.gguf` at the root is llama.cpp's format and nothing else's
    # (`DECISIVE`), and letting it fall through to those checks first is how
    # a snapshot that happens to ALSO carry a `model_index.json` would have
    # come back `(*DIFFUSERS_RUNNERS, "llamacpp-text")` — which, because this
    # runner is registered ahead of the diffusers rows, would have labelled a
    # diffusion pipeline as text generation (`ai_models._engine`'s
    # `decisive[0]`). Gated on the architecture the GGUF itself declares
    # (`GGUF_TEXT_ARCHITECTURES`), not the extension alone — see
    # `is_text_gguf`'s docstring for the image/speech GGUF repos that check
    # exists to keep out.
    if (has_gguf_weights(names)
            and gguf_architecture in GGUF_TEXT_ARCHITECTURES):
        found.append("llamacpp-text")
        return tuple(found)
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

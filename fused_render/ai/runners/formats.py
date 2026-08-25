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
import re
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

#: An MLX conversion of a NeMo Parakeet model — the single file the withdrawn
#: `parakeet-mlx` runner used to load (D406). Nothing distinguishing on its
#: own: it is the name every transformers repo on the Hub carries, which is
#: exactly why the config below has to be read too. Still checked so a
#: Parakeet snapshot is recognised as unloadable rather than mistaken for a
#: text checkpoint — see `is_parakeet_checkpoint` and the early return in
#: `loaders()`.
PARAKEET_WEIGHTS = "model.safetensors"

#: …and what DOES distinguish one. A Parakeet snapshot's `config.json` is
#: NeMo's training config, not a transformers one, and it names the class the
#: weights came out of in `target` — which is what `parakeet_mlx.from_config`
#: dispatched on before the runner was withdrawn (D406). The prefix is
#: narrowed to `…asr.models.` deliberately: NeMo also ships TTS and LLM
#: collections that no runner here loads either, so a check on the word
#: "nemo" would offer a Load button for a speech SYNTHESIS repo and fail
#: inside a library that never had a chance.
NEMO_ASR_TARGET = "nemo.collections.asr.models."

#: What an mflux-readable snapshot always has: component subfolders of MLX
#: safetensors, rather than the single-file layout diffusers writes.
MFLUX_COMPONENTS = ("transformer", "text_encoder", "vae")

#: What a MiniMax-H3 snapshot always has at its ROOT: the `FL2VA/`
#: checkpoint tree. **No runner reads this layout any more** — D468 dropped
#: `h3-video` — but the signal is still load-bearing, for the same reason
#: D406's withdrawn Parakeet runner left its own check standing: the real
#: repo (`MiniMaxAI/MiniMax-H3`) ALSO carries a root-level
#: `model_index.json` — h3.c's own bookkeeping file, not a diffusers
#: pipeline manifest — so this check has to run, and RETURN, before the
#: `DIFFUSERS_INDEX` check below, or a snapshot somebody already fetched
#: would be mislabelled as a diffusers-loadable repo and the page would
#: offer a Load button that opens on a layout diffusers cannot read.
H3_COMPONENT = "FL2VA"

#: `dgrauet/ltx-2.3-mlx-q4`'s own manifest file (mlx-forge's split-conversion
#: bookkeeping, not something any pipeline reads) — VERIFIED present at the
#: root of both curated LTX-2.3 repos (2026-08-23) beside the transformer
#: files `has_ltx_split_layout` also checks for. Paired with a
#: `transformer-*` safetensors name rather than trusted alone: a directory of
#: safetensors otherwise says nothing about the modality (this file's own
#: rule for why `mlx-text` is not in `DECISIVE`), and a manifest name alone
#: is a thinner claim than this codebase usually accepts for "this repo IS a
#: specific thing" — see `is_mlx_whisper_snapshot`'s own two-signal shape for
#: the same argument made about a different format.
LTX_SPLIT_MANIFEST = "split_model.json"

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

#: The EDIT counterpart of `MFLUX_VARIANTS` — one entry per model that also
#: supports base-image editing, keyed by the same repo id.
#:
#: **A second, independent table, not a nested key under the row above** —
#: `Flux2KleinEdit` does not subclass `Flux2Klein` — its own `__mro__` is
#: `['Flux2KleinEdit', 'Module', 'dict', 'object']`, verified against mflux
#: 0.19.0 on Apple Silicon — so the two are unrelated classes over the same
#: snapshot, and a single row cannot hold two unrelated `variant`/`module`
#: pairs without one of them reading as an override of the other, which it is
#: not. The module path is a full dotted submodule,
#: `mflux.models.flux2.variants.edit.flux2_klein_edit`, one level deeper than
#: the plain row's package — do not derive it from the plain row's by string
#: surgery, since nothing about the nesting is guaranteed to hold for a
#: future model.
#:
#: **`config` and `vae` are deliberately ABSENT here, not repeated.** They are
#: facts about the CHECKPOINT — the model config method and the autoencoder
#: the conversion carries — and editing changes only which class denoises it,
#: never which weights or which latent space it denoises. Copying them into a
#: second row would let the two rows disagree after a future edit to one of
#: them (a config method renamed, a vae swapped for a re-fit) touches only
#: the row someone remembered to change — a drift `mflux_edit_recipe` below
#: makes structurally impossible by reading them off `MFLUX_VARIANTS` every
#: time, rather than by convention. An `image_paths` request keeps
#: `MFLUX_VARIANTS`'s row's OWN key — absence here just means "no edit class
#: known for this repo", refused with a sentence exactly the way an absent
#: row in the table above already is.
MFLUX_EDIT_VARIANTS = {
    "mlx-community/FLUX.2-Klein-4B-4bit": {
        "variant": "Flux2KleinEdit",
        "module": "mflux.models.flux2.variants.edit.flux2_klein_edit",
    },
}


def mflux_edit_recipe(model_id: str) -> dict | None:
    """The full edit-mode recipe for `model_id` — `MFLUX_EDIT_VARIANTS`'s own
    `variant`/`module`, with `config`/`vae` DERIVED from `MFLUX_VARIANTS`'s
    row for the same id — or None when either table has no row for it.

    The one reader of both tables at once, so the two can never independently
    say something different about a checkpoint's config or latent space; see
    `MFLUX_EDIT_VARIANTS`'s own comment for why that would otherwise be a
    silent drift rather than a loud one.
    """
    edit = MFLUX_EDIT_VARIANTS.get(model_id)
    plain = MFLUX_VARIANTS.get(model_id)
    if edit is None or plain is None:
        return None
    # `.get("vae")`, not a hard index: `MFLUX_VARIANTS`'s own docstring
    # declares the key optional ("a variant with no `vae` simply gets no
    # preview"), and `_build_variant` reads it the same soft way
    # (`recipe.get("vae")`) — a plain row that ever ships without one must
    # not make the edit recipe raise where the plain row itself would not.
    # `config` stays a hard index: every row in this table names one, and
    # `_build_variant` reads it unconditionally.
    return {**edit, "config": plain["config"], "vae": plain.get("vae")}

#: A diffusers pipeline names itself here, and `from_pretrained` reads it.
DIFFUSERS_INDEX = "model_index.json"

#: The `model_type`s of the DUAL ENCODERS the embedding runners read — one
#: checkpoint holding a text tower and a vision tower that project into one
#: space, which is what `get_text_features` / `get_image_features` are.
#:
#: **This set is what makes `paths` legal**, and it is the only reason the
#: embedding gate has two halves rather than one: a request carrying image paths
#: needs a vision tower to feed them to, and the families here have one while the
#: families below do not (`ai_runtime._accepts_paths` answers the same question
#: off the cached `config.json`, since a fine-tune's `model_type` may be
#: anything).
#:
#: **`siglip` covers SigLIP AND SigLIP2**: `google/siglip2-base-patch16-384`
#: and `onnx-community/siglip2-base-patch16-384-ONNX` both
#: declare `"model_type": "siglip"` (checked 2026-08-25, and it is what makes
#: the MLX runner able to read it at all — mlx-embeddings 0.1.x ships a `siglip`
#: module and no `siglip2` one, and dispatches on this very field). There is no
#: separate spelling to add.
#:
#: Read off the config rather than the repo id, for `is_parakeet_checkpoint`'s
#: reason: a fine-tune under somebody's own account is the same format and
#: deserves the same tag.
DUAL_EMBED_MODEL_TYPES = frozenset({"siglip", "clip"})

#: …and the TEXT-ONLY encoders, which the `embeddings` capability also serves:
#: one tower, no vision half, hundreds or thousands of tokens of context instead
#: of a caption's 64. These are what make RAG, document search and clustering
#: possible at all — a SigLIP text tower truncates at 64 tokens, so no chunk size
#: makes it a paragraph encoder.
#:
#: **Four families, and the boundary is "does this `model_type` distinguish an
#: encoder from a generative model".** `bert`, `xlm-roberta`, `nomic_bert` and
#: `modernbert` are encoder-only architectures: nothing generative wears those
#: strings, so the field is real evidence. Deliberately ABSENT are `qwen3`,
#: `gemma3_text`, `lfm2` and the rest of the decoder-derived embedding ports —
#: `mlx-embeddings` genuinely ships modules for several of them, but their
#: `model_type` is the CHAT architecture's, so admitting them here would route
#: every Qwen3 chat checkpoint on the disk to the embedding runner. That is
#: `is_parakeet_checkpoint`'s lesson restated: evidence that does not
#: distinguish is not evidence.
TEXT_EMBED_MODEL_TYPES = frozenset({"bert", "xlm-roberta", "nomic_bert",
                                    "modernbert"})

#: The gate `loaders()` actually asks — "is this an embedding checkpoint at all"
#: — which is one question with one answer, so it is the union. The two halves
#: above are for the callers that need to know WHICH kind, and there are exactly
#: two: the `paths` refusal at the route, and this file's own MLX subset.
EMBED_MODEL_TYPES = DUAL_EMBED_MODEL_TYPES | TEXT_EMBED_MODEL_TYPES

#: …and the subset MLX reads. Same field, shorter list, and it is
#: `mlx-embeddings` 0.1.0's own module directory intersected with the gate above:
#: it ships `siglip.py`, `bert.py`, `modernbert.py` and `xlm_roberta.py` (the
#: loader sanitizes `-` to `_` when it imports by `model_type`, which is why
#: `xlm-roberta` is spelled with the hyphen the config uses).
#:
#: Two families are therefore ONNX-ONLY here. `clip`: mlx-embeddings has no CLIP
#: port, so a CLIP checkpoint in SAFETENSORS is loadable by nothing at all — the
#: ONNX runner reads a `clip` export happily, but it reads the export and not the
#: safetensors. `nomic_bert`: same story, and it is the reason the curated
#: default is an ONNX row rather than one both engines share.
#:
#: A SUBSET of `EMBED_MODEL_TYPES`, never a superset: a family the gate does not
#: recognise can never reach the MLX check in `loaders()`, so an entry here that
#: is not in the union above would be a line nothing can read.
#:
#: Split rather than shared because this is exactly what `loaders()` answers: a
#: Mac that resolved to MLX must not be offered a Load for a checkpoint its
#: engine has no module for.
MLX_EMBED_MODEL_TYPES = frozenset({"siglip", "bert", "xlm-roberta",
                                   "modernbert"})

#: Prompt scheme -> `(query_prefix, document_prefix)`. Ported from PR #780
#: (Aman Bagrecha) essentially verbatim, comments included.
#:
#: **Retrieval encoders are asymmetric and this is not a detail.** A question
#: and the passage that answers it are different kinds of text, and every
#: model named here was trained with that difference spelled out in its
#: input. Embedding both sides identically costs real recall on the models
#: that instruct one side — and costs it SILENTLY, which is the part that
#: matters: the vectors still come back, still unit length, still comparable,
#: just worse. Nothing downstream can detect it.
#:
#: Each value is `(query_prefix, document_prefix)`, applied by plain
#: concatenation — no template engine, because every scheme here is literally
#: a string glued to the front, checked against each model's own card.
#:
#: `"none"` is a REAL scheme and the fallback, not an absence: a model whose
#: convention this table does not know is embedded verbatim on both sides,
#: which is the symmetric behaviour every encoder supports and the only
#: honest answer for a repo nobody here has read the card for. Guessing a
#: prefix would be worse than not prefixing — a wrong instruction is not
#: ignored, it is text the model dutifully encodes as though it were content.
#: It is also how a DUAL ENCODER answers: SigLIP has no retrieval convention
#: at all, so `"none"` is what `ai_runtime` reads to refuse `kind` on one.
TEXT_EMBED_PROMPTS = {
    "none": ("", ""),
    # bge v1.5. The card instructs the QUERY only and says in as many words
    # that the passage side takes none ("no instruction needed for
    # passages"), so this is the one asymmetric scheme whose document branch
    # is genuinely the empty string rather than a second prefix.
    "bge": ("Represent this sentence for searching relevant passages: ", ""),
    # nomic-embed-text v1 and v1.5. Its card is explicit that the task
    # prefixes are REQUIRED rather than advisory — the model was trained
    # multi-task and the prefix is what selects the task, so an unprefixed
    # call is out of distribution on both sides, not merely un-tuned.
    "nomic": ("search_query: ", "search_document: "),
    # The e5 family (intfloat/e5-*-v2, multilingual-e5-*). Both sides
    # prefixed, and the card warns that swapping the two is worse than using
    # neither.
    "e5": ("query: ", "passage: "),
    # Qwen3-Embedding ships NAMED prompts in
    # `config_sentence_transformers.json` (`query` and `document`); the query
    # one is an instruction block and the document one is empty. The task
    # sentence is Qwen's own default out of that file, kept verbatim rather
    # than reworded — it is part of what the model was tuned against, not a
    # comment this app is free to improve.
    "qwen3": ("Instruct: Given a web search query, retrieve relevant passages "
              "that answer the query\nQuery:", ""),
    # EmbeddingGemma's card gives a prompt per task; these are its
    # `Retrieval-query` and `Retrieval-document` forms. The document form
    # ends at `text: ` because the card's template carries an optional
    # `title:` ahead of it, and `none` is what that field takes when there is
    # no title — which is always, here, since this API takes a flat list of
    # strings and has nowhere for a caller to put one.
    "gemma-embedding": ("task: search result | query: ", "title: none | text: "),
}

#: The two values `kind` may take. A closed set, checked at the edge
#: (`embed_common.request_kind`) rather than defaulted through, because a
#: typo'd `"queries"` silently falling back to the document prefix is the exact
#: silent-degradation failure this whole table exists to prevent.
TEXT_EMBED_KINDS = ("query", "document")

#: What a caller who says nothing gets. See `embed_common.DEFAULT_KIND` for the
#: whole argument.
TEXT_EMBED_DEFAULT_KIND = "document"

#: Repo id -> its prompt scheme, for every model this app CURATES. Checked
#: before the heuristic below, so the guess never runs for the models most
#: people will ever load — and `test_ai_catalog_embeddings.py` asserts every
#: curated embeddings id resolves to a scheme, which makes "has a known
#: convention" a curation rule rather than a hope.
#:
#: PR #780 carried the same idea keyed by GGUF FILENAME, because llama.cpp
#: addresses a model as a `(repo, file)` pair. Nothing here does: both engines
#: take a repo id, so that is the key.
TEXT_EMBED_SCHEMES = {
    "nomic-ai/nomic-embed-text-v1.5": "nomic",
    "intfloat/multilingual-e5-small": "e5",
    "BAAI/bge-base-en-v1.5": "bge",
}

#: Substrings of a REPO ID that identify a prompt scheme, most specific first —
#: the fallback for a repo `TEXT_EMBED_SCHEMES` above does not curate.
#:
#: **A heuristic, and named as one.** A model's prompt convention is a fact
#: about its training that no file in the snapshot records, so for an uncurated
#: repo the id is the only evidence there is. It is reasonable evidence — an
#: embedding repo is named after the model it holds, near universally — but it
#: is evidence and not proof, which is why the scheme actually resolved travels
#: back on the catalog entry (`ai_runtime`'s `promptScheme`) rather than being
#: applied out of sight. A caller who sees the wrong one can pass `kind`
#: deliberately or name a curated id instead.
#:
#: `qwen3-embedding` ahead of any bare `qwen3` and `nomic-embed` ahead of any
#: bare `nomic` for the obvious reason; the e5 hints are spelled with their
#: size suffix rather than as a bare `e5` because two letters that common
#: match ids with nothing to do with the family.
TEXT_EMBED_SCHEME_HINTS = (
    ("qwen3-embedding", "qwen3"),
    ("embeddinggemma", "gemma-embedding"),
    ("gemma-embedding", "gemma-embedding"),
    ("nomic-embed", "nomic"),
    ("multilingual-e5", "e5"),
    ("e5-small", "e5"),
    ("e5-base", "e5"),
    ("e5-large", "e5"),
    # bge-m3 is the family member that takes NO query instruction — its card
    # says so outright — so it must not inherit the `bge` scheme below by
    # substring. Ordered ahead of `bge-` for exactly that.
    ("bge-m3", "none"),
    ("bge-", "bge"),
)


def text_embed_prompt(scheme: str, kind: str) -> str:
    """The prefix to glue in front of one text, for `scheme` and `kind`.

    An unknown scheme falls back to `"none"` rather than raising: this is
    reached from a worker holding a resident model with a validated batch in
    hand, and a scheme name that has drifted out of the table is a reason to
    embed plainly, not to fail a call that would otherwise work.
    """
    query, document = TEXT_EMBED_PROMPTS.get(scheme) or TEXT_EMBED_PROMPTS["none"]
    return query if kind == "query" else document


def text_embed_scheme(model_id: str) -> str:
    """The prompt scheme for a model — the curated table, then the repo id.

    Curated first: `TEXT_EMBED_SCHEMES` states the scheme outright for every id
    this app recommends, so the heuristic never runs for the models most people
    will ever load. Everything else falls to `TEXT_EMBED_SCHEME_HINTS` over the
    repo id, and finally to `"none"`.

    Lowercased before matching: publishers capitalise inconsistently
    (`BAAI/bge-*` against `BAAI/BGE-*` in their own docs), and a scheme that
    turned on that would be precisely the silent wrong answer this table exists
    to avoid.

    `"none"` for a DUAL ENCODER, and that is the answer rather than a gap:
    SigLIP and CLIP have no query/passage convention, so there is no prefix to
    apply and `kind` is a parameter with nothing to do — which is exactly what
    `ai_runtime` refuses it on.
    """
    curated = TEXT_EMBED_SCHEMES.get(model_id)
    if curated:
        return curated
    haystack = model_id.lower()
    for hint, scheme in TEXT_EMBED_SCHEME_HINTS:
        if hint in haystack:
            return scheme
    return "none"

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
        # D319 briefly had a second `runners/vad.py` caller (Parakeet), which
        # is why the module lives at the runners root rather than inside
        # `mlx_whisper/`; D406 withdrew that engine, leaving MLX Whisper as
        # the module's sole caller, but the shared location stays (no reason
        # to move it back for a caller count that could grow again).
        "owner": "MLX Whisper",
        "part": "speech detector",
        "what": (
            "The 2MB Silero detector the MLX Whisper engine uses to find "
            "the speech in a recording and skip the silence — fetched with "
            "its model downloads so an offline machine still has it. "
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

#: What a safetensors-reading engine can open. `.bin` and `.pt` are pickles:
#: readable, but with no cheap header, which is why the page counts parameters
#: only from safetensors.
#:
#: The name is torch's because the extensions are torch's conventions and
#: `ai_models.py` reads it to decide whether a snapshot holds weights at all —
#: a question about the FILES, which did not change when D416 removed the torch
#: text runners. `diffusers-image*` and `mlx-text` still read every one of them.
TORCH_WEIGHTS = (".safetensors", ".bin", ".pt")

#: What an `onnxruntime.InferenceSession` can open. One extension, and it is
#: deliberately NOT paired with `.onnx_data` here: an export over the 2 GB
#: protobuf limit splits its tensors into a sidecar of that name, but a sidecar
#: with no `.onnx` graph beside it is not a loadable model, so the graph file is
#: the whole of the evidence. (`onnx-community/siglip2-so400m-patch14-384-ONNX`
#: is the split case: `onnx/text_model.onnx` is 0.6 MB of graph pointing at a
#: 2.8 GB `onnx/text_model.onnx_data`. Both are FETCHED — see
#: `runners/onnx_embed.py`'s `allow_patterns` — this constant just decides what
#: counts as "there are weights here".)
#:
#: Separate from `TORCH_WEIGHTS` rather than appended to it, because the two
#: answer different questions for different engines: `ai_models.py` counts
#: parameters off safetensors headers, and no `.onnx` has one.
ONNX_WEIGHTS = (".onnx",)

#: llama.cpp's single-file weights format (SPEC AI-11, `runners/llama_text.py`).
#: Unlike every other format in this module a `.gguf` needs no companion
#: config to identify — the vocabulary, the architecture and the model's own
#: chat template all live inside the one file's key-value metadata, which is
#: the reason GGUF is one file at all. So the presence check is the
#: extension, at the SNAPSHOT ROOT. (The removed `torch_text.py` read the same
#: evidence the other way round — "nothing but GGUF here" was its
#: wrong-format refusal, and it pointed the user at this engine. Since D416
#: only the engine that WANTS a GGUF reads this.)
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
#: runner vendors (SPEC AI-11, D411: llama-cpp-python 0.3.29 -> llama.cpp
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


def _gguf_uint_by_suffix(path: str, suffix: str) -> int | None:
    """The one unsigned/signed-int GGUF header value whose key ends in `suffix`.

    Matched by SUFFIX rather than by a full key, because every architecture
    namespaces its own metadata (`qwen35.block_count`, `lfm2moe.expert_count`)
    and exactly one such key exists in a well-formed GGUF — so no caller needs
    to read `general.architecture` first, and an architecture this module has
    never heard of still answers.

    Bounded local peek, and fails toward None on every malformed shape: a
    truncated read, a missing magic, or a value type this app does not model
    is "cannot tell", never a crash and never a guess. That last case matters
    more than it looks — the peek is a fixed `_GGUF_HEADER_PEEK_BYTES` window
    and a GGUF's tokenizer arrays are megabytes wide, so a key that sorts
    after them is simply not visible from here and reads as None. Both keys
    this is used for sit in the architecture block, ahead of the tokenizer.
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
            if key.endswith(suffix) and value_type in (4, 5):
                fmt = "<I" if value_type == 4 else "<i"
                (value,) = struct.unpack_from(fmt, buf, offset)
                return value
            offset = _gguf_skip_value(buf, offset, value_type)
    except (struct.error, IndexError, UnicodeDecodeError):
        return None
    return None


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


def gguf_block_count(path: str) -> int | None:
    """The model's own transformer layer count out of its GGUF header, or None.

    **Why `llama_text.py` needs this at all.** Neither `llama-cpp-python`
    0.3.29's ctypes surface nor any vendor SDK this app is willing to shell
    out to exposes available GPU memory — the bindings wrap `llama.h`, not
    the lower-level `ggml-backend.h` functions (`ggml_backend_dev_memory`)
    that would answer it, confirmed by reading the installed package's own
    `llama_cpp.py`. So there is no way to CALCULATE how many layers of a
    given model fit in whatever VRAM this machine has; the only honest
    alternative is to know the total layer count and TRY a shrinking sequence
    of offload counts, and this is where that number comes from.

    GGUF's own key convention is `<architecture>.block_count` — verified by
    downloading the real header bytes of `unsloth/Qwen3.5-4B-GGUF`,
    `unsloth/Qwen3.5-9B-GGUF` and `unsloth/Qwen3.8-27B-GGUF` on 2026-08-21
    (`qwen35.block_count` in all three, 32/32/65 respectively) rather than
    assumed from the spec alone. Matched by SUFFIX rather than requiring the
    caller to already know the architecture prefix: exactly one such key
    exists in a well-formed GGUF, so this needs no second read of
    `general.architecture` first, and works even for an architecture this
    module has never heard of.

    Same bounded local peek and the same fail-toward-None contract as
    `gguf_architecture` — a truncated read or a value type this app does not
    model is "cannot tell", never a crash and never a guess. Callers must
    treat None as "no sizing information", not as zero layers.
    """
    return _gguf_uint_by_suffix(path, ".block_count")


def gguf_expert_count(path: str) -> int | None:
    """How many experts a mixture-of-experts GGUF holds, or None if it is dense.

    Read for ONE decision, in `llama_text._offload_schedule`: a MoE model can
    be split a way a dense one cannot — its expert tensors are most of the
    weights but only a few of them are multiplied per token, so parking those
    on the CPU and keeping attention on the GPU costs far less speed than
    dropping whole layers does. Nothing else may branch on this; in
    particular it is NOT a quality or capability signal.

    None means "no expert weights to park", and a dense model genuinely has
    no such key — verified against the local cache on 2026-08-21, where
    `LFM2.5-8B-A1B` carries `lfm2moe.expert_count = 32` (with
    `expert_used_count = 4`) and `Qwen3.5-4B`, `gemma-4-E4B-it` and
    `LFM2.5-1.2B` carry no `expert`-prefixed key at all. Same suffix match,
    same bounded peek and the same fail-toward-None contract as
    `gguf_block_count` — see `_gguf_uint_by_suffix`.
    """
    return _gguf_uint_by_suffix(path, ".expert_count")


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
#:
#: **`unsloth` is not a rule, and this table stopped pretending it was.** The
#: shortlist's smallest entry comes out of `LiquidAI/…`, the publisher's own
#: repo, because that is where LFM2.5 is published — nothing here reads the
#: owner, and a curated `(repo, file)` pair is as loadable from one namespace
#: as another.
GGUF_RECIPES = {
    "LFM2.5-1.2B-Instruct-Q4_K_M.gguf": {
        "repo": "LiquidAI/LFM2.5-1.2B-Instruct-GGUF",
        "file": "LFM2.5-1.2B-Instruct-Q4_K_M.gguf",
    },
    "Qwen3.5-4B-Q4_K_M.gguf": {
        "repo": "unsloth/Qwen3.5-4B-GGUF",
        "file": "Qwen3.5-4B-Q4_K_M.gguf",
    },
    "gemma-4-E4B-it-Q4_K_M.gguf": {
        "repo": "unsloth/gemma-4-E4B-it-GGUF",
        "file": "gemma-4-E4B-it-Q4_K_M.gguf",
    },
    "LFM2.5-8B-A1B-Q4_K_M.gguf": {
        "repo": "LiquidAI/LFM2.5-8B-A1B-GGUF",
        "file": "LFM2.5-8B-A1B-Q4_K_M.gguf",
    },
    "Qwen3.8-27B-UD-Q3_K_XL.gguf": {
        "repo": "unsloth/Qwen3.8-27B-GGUF",
        "file": "Qwen3.8-27B-UD-Q3_K_XL.gguf",
    },
}


# ---------------------------------------------------------------------------
# Picking ONE GGUF file out of an arbitrary repo's own listing (D412).
#
# `GGUF_RECIPES` above is 5 keys over 3 repos — a hand-curated shortcut, not
# a limit llama.cpp itself imposes. Any Hub repo that carries a root-level
# GGUF is loadable by `llama_cpp.Llama`; the only reason an uncurated one was
# previously refused is that this app had no rule for choosing WHICH of a
# repo's 20-30 quantizations a bare repo id should mean. This section is that
# rule, used by both `llama_text._resolve_model_id` (worker side, an
# uncurated repo id) and `hub_models.py`'s search (page side, deciding
# whether a GGUF search result is actionable) — the same "one answer, two
# readers" shape `GGUF_RECIPES` itself states above, and for the identical
# reason: the two must not be able to disagree about which file a repo id
# means.
#
# **Deterministic, not hardware-aware, and that is a considered choice, not
# an oversight.** A model id has to determine the same bytes on a 6GB laptop
# and a 24GB desktop — `catalog.SUGGESTIONS["llamacpp-text"]`'s own
# `size_gb` field is a promise that breaks the moment resolution varies by
# machine, and `ai_runtime.py`'s downloaded/curated join keys entries by
# FILENAME for the same reason. A hardware BUDGET was considered and
# rejected on a harder ground than non-determinism: there is nothing to
# budget against. `llama_cpp.py`'s ctypes bindings expose no
# `ggml_backend_dev_memory` or any other free-VRAM query (confirmed by
# reading the installed bindings — see `llama_text.py`'s own "sized by
# trying, not calculating" note), so a "budget" would be total system RAM or
# a guess, not a measurement. What makes hardware-blindness AFFORDABLE rather
# than reckless is `llama_text._offload_schedule`: an over-large pick
# degrades to partial or full CPU offload instead of failing, so the cost of
# picking too big is a slower load, never a crash — the one cost backoff
# cannot undo is the DOWNLOAD itself, which is exactly why the suffix
# priority below starts at Q4_K_M rather than at the true smallest quant a
# repo might publish.
#
# **The exclusion rules were measured, not assumed.** Split shards and
# subdirectories were checked against five real repos (`unsloth/Qwen3.5-9B-GGUF`,
# `unsloth/Qwen3.8-27B-GGUF`, `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF`,
# `bartowski/Qwen_Qwen3-8B-GGUF`, `ornith-ai/Ornith-1.0-9B-GGUF`) on
# 2026-08-21; the auxiliary-filename list was widened past that single pass —
# an initial guess of `mmproj` / `mtp-` / `draft` (from ONE observed file,
# `MTP/mtp-Qwen3.8-27B-Q4_0.gguf`) was re-checked against ~200 real
# `text-generation` + `gguf`-tagged repos (2637 GGUF filenames) before being
# trusted: `mmproj` and `draft` held as plain substrings, `mtp-` was WRONG as
# an anchored pattern (`RVN-Q4_K_M-mtp.gguf` — the dash comes BEFORE "mtp",
# not after, in the common suffix form) and is now a bare substring too, and
# `projector` was found and added (`llama-3.2-11B-vision_f16_projector.gguf`
# — an mmproj-shaped file that never contains the string "mmproj" at all).
# `specul`/`speculative` was tried and REJECTED: it false-positive-matched a
# real fine-tune name, `DeepSeek-R1-Distill-Qwen-1.5B-GRPO-SpeculativeReasoner`,
# that is not a draft model at all. Multimodal projectors are NOT
# theoretical for a text-generation picker either — every mmproj-carrying
# repo found in that scan (`prism-ml/Bonsai-27B-gguf` and its ternary
# sibling) is tagged `pipeline_tag: text-generation`, the base LLM and its
# projector sharing one repo, so this exclusion is load-bearing on the exact
# path this picker serves, not a precaution against a shape that cannot
# occur here.

#: A GGUF this app must never offer as "the chat model", even though it is an
#: ordinary single file a naive scan would rank — auxiliary weights
#: llama.cpp's own ecosystem ships ALONGSIDE a standalone causal-LM
#: checkpoint rather than as one. Bare substrings, not anchored patterns —
#: see the section note above for why `mtp-` (anchored) missed real files an
#: unanchored `mtp` does not.
GGUF_AUXILIARY_RE = re.compile(r"mmproj|mtp|draft|projector", re.IGNORECASE)

#: A multi-part GGUF shard (`-00001-of-00005.gguf`). `download()`
#: (`worker_base.download_file`) fetches exactly ONE file, so a split
#: quantization can never be assembled by this runner's existing download
#: path — excluding it from candidacy costs nothing this runner could have
#: served anyway. Observed only under a `BF16/` subdirectory in the sample
#: above, so `pick_gguf_file`'s subdirectory exclusion already catches every
#: split file seen in practice; this regex is the belt to that suspenders; in
#: case a future repo ships a split quantization at its root.
GGUF_SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)

#: Standard llama.cpp quantization suffixes this picker will choose between,
#: MOST-preferred first. Starts at `Q4_K_M` rather than a smaller K-quant
#: (`Q3_K_M`, `Q2_K`) DELIBERATELY: those exist and are sometimes smaller,
#: but this is the one list where being wrong is expensive in a way
#: `llama_text._offload_schedule`'s backoff cannot fix — a pick that is too
#: LARGE for the GPU degrades to a slower load, but a pick that is too SMALL
#: (an aggressively quantized, noticeably degraded model) downloads exactly
#: as many bytes and then answers worse, which backoff has no lever for at
#: all. `Q4_K_M` is the community's own floor for "still a reliable
#: general-purpose quant" — the branch's own curated table never suggests
#: anything below it either (its cheapest entries are Q4_K_M/Q5_K_M).
#: `Q6_K`/`Q8_0` are ranked last among NAMED suffixes because they are closer
#: to unquantized than to the sweet spot most repos are downloaded for.
GGUF_SUFFIX_PRIORITY = (
    "Q4_K_M", "Q4_K_S", "IQ4_NL", "IQ4_XS", "Q4_1", "Q4_0",
    "Q5_K_M", "Q5_K_S", "Q5_1", "Q5_0",
    "Q6_K",
    "Q8_0",
)

#: The quantization token right before `.gguf` — optionally prefixed by
#: unsloth's `UD-` dynamic-quant marker — e.g. `Q4_K_M` out of
#: `...-Q4_K_M.gguf`, or `Q3_K_XL` (`ud` set) out of `...-UD-Q3_K_XL.gguf`.
#: Anchored to the literal end of the filename, so `.search()` can only
#: succeed at the one position adjacent to `.gguf`, never on an
#: accidental earlier substring. Matches nothing for an unsuffixed file
#: (`model.gguf`) or a full-precision one (`BF16`/`F16`/`F32` are not shaped
#: like `<letters><digit>` immediately followed by `.gguf`), which is
#: deliberate: neither should be picked BY SUFFIX, only as a last-resort
#: single-candidate fallback (`pick_gguf_file`).
_GGUF_QUANT_TOKEN_RE = re.compile(
    r"(?:^|[-_.])(?P<ud>UD-)?(?P<token>[A-Za-z]{1,2}\d(?:_[A-Za-z0-9]+)*)\.gguf$",
    re.IGNORECASE,
)
#: The bit-width family a quant token names — `Q4_K_M`/`IQ4_XS` are both
#: family `4`. Matched separately from the full suffix table because
#: unsloth's dynamic quants (`UD-Q3_K_XL`) do not share an exact suffix with
#: any plain quant — "XL" names a per-layer bit ALLOCATION, not a fixed
#: width — so family membership is the only reliable signal for THOSE files.
_GGUF_FAMILY_RE = re.compile(r"^I?Q(\d)", re.IGNORECASE)
#: Plain (non-dynamic) families this picker will rank at all — everything
#: below 4 bits is excluded UNLESS it is a `UD-` dynamic quant, whose
#: per-layer allocation is specifically engineered to stay usable at a lower
#: AVERAGE bit width (the branch's own curated 27B entry is `UD-Q3_K_XL`,
#: a family-3 dynamic quant, for exactly this reason). A plain, uniform
#: sub-4-bit quant has no such engineering behind it and is excluded.
_GGUF_NAMED_FAMILIES = frozenset({4, 5, 6, 8})


def _gguf_rank(filename: str) -> tuple[int, int] | None:
    """`filename`'s sort key for `pick_gguf_file` — smaller sorts first
    (more preferred) — or None to exclude it from ranked candidacy.

    Four tiers, described in ascending (best-to-worst) order:

    0. A plain (non-`UD-`) NAMED suffix, ranked by `GGUF_SUFFIX_PRIORITY`'s
       own order.
    1. A plain named suffix `GGUF_SUFFIX_PRIORITY` does not list by exact
       name but whose family (`Q4`/`Q5`/`Q6`/`Q8`) is still one of the four
       ranked ones — a suffix variant this table has not been taught yet,
       ranked below every NAMED suffix in the same family rather than
       excluded outright.
    2. A `UD-` dynamic quant of one of the four ranked families — eligible,
       per the module note above, but ranked below every plain quant of any
       ranked family, since a plain quant needs no engineering to stay
       usable at that width.
    3. A `UD-` dynamic quant of an UNRANKED (sub-4-bit) family — eligible
       ONLY because it is dynamic, and ranked last: this is the tier the
       branch's own curated `UD-Q3_K_XL` entry would land in.

    A plain sub-4-bit quant, an unquantized file (`BF16`/`F16`/`F32`), or a
    filename with no recognisable quant token at all returns None — not a
    tier, EXCLUDED. `pick_gguf_file`'s single-candidate fallback is the only
    way one of those is ever chosen, and only when nothing else competes.
    """
    match = _GGUF_QUANT_TOKEN_RE.search(filename)
    if not match:
        return None
    token = match.group("token").upper()
    is_ud = bool(match.group("ud"))
    family_match = _GGUF_FAMILY_RE.match(token)
    if not family_match:
        return None
    family = int(family_match.group(1))
    ranked_family = family in _GGUF_NAMED_FAMILIES
    if not ranked_family and not is_ud:
        return None
    try:
        suffix_rank = GGUF_SUFFIX_PRIORITY.index(token)
    except ValueError:
        suffix_rank = len(GGUF_SUFFIX_PRIORITY)
    if ranked_family:
        tier = 2 if is_ud else (0 if token in GGUF_SUFFIX_PRIORITY else 1)
    else:
        tier = 3
    return (tier, suffix_rank)


def pick_gguf_file(filenames) -> str | None:
    """The one GGUF file `filenames` (a repo's own listing, root-relative)
    means as a chat model — or None when nothing here qualifies.

    **This is the whole of Piece 1** (D412): given ANY Hub repo's file
    listing, decide which single `.gguf` a bare repo id resolves to, the same
    question `GGUF_RECIPES` answers by hand for 5 curated filenames. Three
    passes:

    1. Exclude by SHAPE — a subdirectory entry (`BF16/model.gguf`, sidesteps
       both the unquantized-BF16 case and every split shard observed, since
       both live under a subdirectory in every sample checked), a non-GGUF
       file, a multi-part shard (`GGUF_SPLIT_RE`), or an auxiliary weight
       (`GGUF_AUXILIARY_RE`) — regardless of what its own quant suffix says.
    2. RANK what is left by `_gguf_rank` and take the best-ranked file, when
       anything ranks at all.
    3. Only when NOTHING ranks (no recognisable quant token anywhere) and
       there is EXACTLY ONE eligible file, fall back to it — the same
       "no ambiguity, nothing to guess between" rule
       `llama_text._resolve_model_id` already uses for a curated repo with
       one recipe. With MORE than one unranked candidate, refuse: picking
       the smallest of them would risk exactly what this function exists to
       avoid, an `mmproj` or a draft model offered as the chat model.
    """
    candidates = []
    for name in filenames:
        if not isinstance(name, str) or "/" in name:
            continue
        if not name.lower().endswith(GGUF_EXTENSION):
            continue
        if GGUF_SPLIT_RE.search(name) or GGUF_AUXILIARY_RE.search(name):
            continue
        candidates.append(name)
    if not candidates:
        return None
    ranked = sorted(
        (rank_and_name for rank_and_name in
         ((_gguf_rank(name), name) for name in candidates)
         if rank_and_name[0] is not None),
        key=lambda pair: (pair[0], pair[1]),
    )
    if ranked:
        return ranked[0][1]
    if len(candidates) == 1:
        return candidates[0]
    return None


#: Quantization methods NO engine in this app can read, so a repo declaring one
#: is not offered a Load button (`loaders()` below) whatever else its snapshot
#: contains. AWQ, GPTQ and compressed-tensors each need a package no runner
#: here installs; bitsandbytes is the one with a story.
#:
#: **This was a dict of refusal SENTENCES until D416, and the sentences went
#: with the runner that printed them.** `runners/torch_text.py` raised
#: "`<repo>` is an AWQ checkpoint, which needs a package this runner does not
#: ship" so that a user reading a bare `ImportError` several frames inside a
#: transformers loader could tell a wrong-format repo from a broken download.
#: With that runner removed, the only consumer left is `unloadable_quant()`,
#: which reads the KEYS — so the values were inert prose and are gone rather
#: than kept warm for a runner that may never exist. `mlx-text` is now the only
#: engine this gate protects, and it needs no per-method sentence: mlx-lm reads
#: MLX-packed or plain safetensors and nothing else, so "not one of these four"
#: is the whole of the question here.
#:
#: **The bitsandbytes entry carries a measurement that has now been asked and
#: answered twice, and the second answer is why this comment survived the
#: deletion.** It first said "needs bitsandbytes and an NVIDIA GPU — this
#: runner ships neither", which stopped being true: bitsandbytes 0.50.1 is MIT,
#: publishes wheels and no sdist for macos arm64, manylinux x86_64 and aarch64,
#: win_amd64 and win_arm64 (checked 2026-08-21, so AI-2a's wheels-only rule
#: would not have blocked it), and documents a dedicated CPU build. So the
#: question became whether 4-bit NF4 is fast enough on a CPU to be worth
#: offering, and it was MEASURED rather than argued:
#: `unsloth/Qwen3-4B-Instruct-2507-unsloth-bnb-4bit` loads on
#: `device_map="cpu"` and generates correct, coherent text (219 `Linear4bit`
#: modules, no error) at half the resident memory of bf16 (4.16GB peak RSS
#: against 8.55GB) — and 3.6x SLOWER per token, 0.65 tok/s against 2.33,
#: uncontended, same prompt and same 4B base. Quantization there bought memory
#: and spent the one resource a CPU path has least of.
#:
#: **That measurement was on Apple Silicon and it explicitly asked for an x86
#: re-measurement before anyone reversed the refusal. D416 is that
#: re-measurement, and it removed the engine instead.** On Linux x86_64 (Ryzen
#: 5 9600X, Radeon RX 9060 XT), same prompt, same 128-token cap, back to back:
#: transformers managed 12.6 tok/s on the ROCm GPU and 2.7 on the CPU, while
#: llama.cpp reading a Q5_K_M GGUF of the same model managed 53.4 and 6.4 — 4.2x
#: and 2.4x, at a third of the download and a third of the peak RSS. So the open
#: question ("is quantized CPU inference on x86 worth installing bitsandbytes
#: for?") is settled in a way that makes the original framing obsolete: the
#: quantized path this app offers is GGUF through llama.cpp, which needs no
#: extra package and beats the unquantized transformers path it would have been
#: competing with. There is nothing left here to re-measure.
UNLOADABLE_QUANT = frozenset({"awq", "gptq", "bitsandbytes", "compressed-tensors"})

#: The runners whose format evidence also settles WHAT THE MODEL IS. A
#: `weights.npz` is a Whisper conversion and nothing else; a `model_index.json`
#: is a diffusion pipeline. The safetensors text runner is the opposite case — a
#: directory of safetensors says nothing about the modality — so a match there
#: never implies a capability. (It was "the two text runners" until D416 removed
#: the transformers family; one runner reads that format now, and the reason it
#: is not decisive is unchanged, because the reason was about the FORMAT.)
#:
#: `parakeet-mlx` is gone (D406) and was never added here: the branch that
#: recognises a NeMo ASR `target` claims no runner (see `loaders()`), so there
#: is no code for this tuple to list. A NeMo ASR snapshot is still decisive —
#: the config names an ASR class nothing else in this app can read — it is
#: just decisive about matching NOTHING, which the early return in `loaders()`
#: enforces directly rather than through this table.
DECISIVE = ("faster-whisper", "mlx-whisper", "mflux-image", "ltx-video",
            "diffusers-image",
            # Every hardware variant of the diffusers runner, because membership
            # here is a statement about the FORMAT — a `model_index.json` is a
            # diffusion pipeline whichever wheel opens it — and not about a
            # machine. Listing only the CPU row would make capability inference
            # depend on which build happens to be registered, so a build that
            # ever shipped the accelerated rows alone would silently stop
            # putting "text to image" on a cached FLUX card. `mlx-text` is
            # the counter-case and stays OUT, for the reason the removed
            # `transformers-text*` rows also did: a directory of safetensors
            # says nothing about the modality, whichever engine opens it.
            "diffusers-image-cuda", "diffusers-image-rocm",
            # A root-level `.gguf` is llama.cpp's format and nothing else in
            # this app reads one for TEXT (SPEC AI-11) — the diffusers image
            # runner's own GGUF use is a swapped-in COMPONENT of an otherwise
            # ordinary pipeline (`COMPONENT_REPOS`), not a snapshot whose root
            # is a bare `.gguf`, so the two cannot collide. Both llama.cpp
            # builds, for the same reason the diffusers hardware variants are
            # both listed above: format evidence, not a fact about a machine.
            # Spelled literally rather than via `LLAMACPP_RUNNERS` because that
            # tuple (like `DIFFUSERS_RUNNERS`) is defined further down this
            # module, alongside `loaders()` — the same order this file already
            # keeps for the diffusers pair just above.
            "llamacpp-text", "llamacpp-text-vulkan",
            # A `siglip`/`clip` model_type is decisive too, and it has to be: a
            # dual encoder is a directory of weights like any other, so without
            # this the text branch would claim it and a cached SigLIP card would
            # offer to load a vision-text encoder as a chat model. Every code
            # appears for `DIFFUSERS_RUNNERS`' reason — membership is a
            # statement about the FORMAT, and a config saying `siglip` says the
            # same thing whichever engine opens it. That covers BOTH weight
            # layouts: an `onnx-community/*-ONNX` export carries the same
            # `model_type: siglip` with an `onnx/` tree instead of
            # `model.safetensors`, and without its codes here a cached export
            # with no pipeline_tag would come back with NO capability at all —
            # `hub_cache._resolve` would find no candidate runner and the card
            # would show a repo nothing can load. All spelled literally rather
            # than via `ONNX_EMBED_RUNNERS` for the same forward-declaration
            # reason `LLAMACPP_RUNNERS` is spelled literally just above.
            "mlx-embed",
            "onnx-embed", "onnx-embed-directml", "onnx-embed-cuda",
            "onnx-embed-rocm")


def unloadable_quant(config: dict) -> str | None:
    """The quant method nothing here can read, or None — see `UNLOADABLE_QUANT`."""
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
    """A NeMo ASR export — the format the withdrawn `parakeet-mlx` runner used
    to load (D406). Kept so a cached NeMo ASR snapshot is still recognised as
    "a speech model nothing here can load" rather than falling through to the
    text runners below (see the early return in `loaders()`).

    Read off `target` rather than off the filename, because the filename is
    `model.safetensors` — shared with every transformers checkpoint on the Hub
    — and off the config rather than the repo id, because a fine-tune under
    somebody's own account is the same format and deserves the same tag.
    """
    target = config.get("target")
    return isinstance(target, str) and target.startswith(NEMO_ASR_TARGET)


def embed_model_type(config: dict) -> str | None:
    """The embedding family this config declares, or None.

    EITHER half of the gate — a dual encoder (`DUAL_EMBED_MODEL_TYPES`) or a
    text-only one (`TEXT_EMBED_MODEL_TYPES`). One function for both, because
    `loaders()` asks a single question ("is this an embedding checkpoint") and
    only then asks which half the answer is in; a function per half would be two
    places to forget a family in, and the halves are already two constants.

    Lowercased, because `model_type` is written by whoever exported the
    checkpoint and a `SigLIP` or a `ModernBERT` would otherwise read as an
    unknown family.
    """
    model_type = config.get("model_type")
    if not isinstance(model_type, str):
        return None
    model_type = model_type.strip().lower()
    return model_type if model_type in EMBED_MODEL_TYPES else None


def has_mflux_components(dirnames) -> bool:
    return all(name in dirnames for name in MFLUX_COMPONENTS)


def has_h3_components(dirnames) -> bool:
    return H3_COMPONENT in dirnames


def has_ltx_split_layout(names) -> bool:
    """Is this an mlx-forge split conversion of LTX-2.3 — `ltx_video`'s own
    curated layout? `names` is the snapshot's TOP-LEVEL FILES (`loaders`'s
    own parameter, unlike `has_h3_components`'s `dirnames` — this format has
    no component subfolders at all, everything sits at the root).

    Two signals, both required: the manifest `LTX_SPLIT_MANIFEST` names, and
    at least one `transformer-*.safetensors` beside it (the dev transformer,
    the distilled one, or both — `ltx_video/worker.py`'s own
    `_distilled_transformer_filename` is stricter about WHICH one it wants to
    fetch; this check only asks whether the repo is shaped like one of these
    conversions at all).
    """
    if LTX_SPLIT_MANIFEST not in names:
        return False
    return any(name.startswith("transformer-") and name.endswith(".safetensors")
               for name in names)


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
DIFFUSERS_RUNNERS = ("diffusers-image", "diffusers-image-cuda",
                     "diffusers-image-rocm")
#: Both llama.cpp builds — CPU/Metal and Vulkan — for the identical reason:
#: a root `.gguf` is the same FORMAT whichever wheel opens it, and naming only
#: `llamacpp-text` here is exactly the trap this comment already describes for
#: the other two families (see `test_every_registered_runner_appears_in_loaders`).
LLAMACPP_RUNNERS = ("llamacpp-text", "llamacpp-text-vulkan")
#: All four ONNX Runtime embedding builds — CPU, DirectML, CUDA and ROCm — for
#: the same reason as the two tuples above: an `onnx/` tree holding a
#: `text_model.onnx` is the same FORMAT whichever execution provider's
#: `InferenceSession` opens it, and a branch that named only the CPU row would be
#: exactly the trap `test_every_registered_runner_appears_in_loaders` exists to
#: catch — a registered runner with no engine tag, no Load button and no cached
#: repos offered, on precisely the machines that chose it.
#:
#: `mlx-embed` stays OUT: it has no hardware variant of its own to enumerate,
#: and it is appended separately in `loaders()` because it is gated on
#: `MLX_EMBED_MODEL_TYPES` in a way none of these four are — and, since the
#: three `transformers-embed*` rows went, because it reads a different weight
#: layout entirely (safetensors, not `.onnx`).
ONNX_EMBED_RUNNERS = ("onnx-embed", "onnx-embed-directml", "onnx-embed-cuda",
                      "onnx-embed-rocm")


def loaders(*, repo_id: str, names, dirnames, config: dict, torch_weights: bool,
           onnx_weights: bool = False,
           gguf_architecture: str | None = None) -> tuple[str, ...]:
    """Which runners' `load()` would accept this snapshot, by code.

    Format only: whether such a runner RUNS here, and whether the capability is
    one it serves, are the registry's questions and are asked by the caller.

    `names`/`dirnames` are the snapshot's top-level entries, `config` its
    `config.json` (empty when absent), `torch_weights` whether anything in the
    tree is a file torch can open, `onnx_weights` whether anything in it is a
    file `onnxruntime` can open (two INDEPENDENT facts, not a fork — a repo may
    publish both layouts, and each engine reads only its own), and
    `gguf_architecture` the caller's OWN
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
        found.extend(LLAMACPP_RUNNERS)
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
    if has_ltx_split_layout(names):
        found.append("ltx-video")
        # …and NOTHING else, for the `mlx-text` branch's reason further
        # down: this snapshot IS a directory of safetensors (nothing here
        # gives it component subfolders), so without this return the
        # fallthrough at the bottom of this function would ALSO claim it
        # and offer to load an LTX-2.3 checkpoint as a chat model.
        return tuple(found)
    if has_h3_components(dirnames):
        # Claims NO runner — D468 dropped `h3-video`, which was the only
        # thing that could read this layout — but the early return MUST
        # stay, exactly as D406's Parakeet withdrawal kept its own. The
        # real repo carries a root `model_index.json` of its own (h3.c's
        # bookkeeping, not a diffusers manifest), so falling through would
        # let the check below claim it and the page would offer a Diffusers
        # Load button that opens on a layout diffusers cannot read.
        return tuple(found)
    if repo_id in MFLUX_VARIANTS and has_mflux_components(dirnames):
        found.append("mflux-image")
    if DIFFUSERS_INDEX in names:
        found.extend(DIFFUSERS_RUNNERS)
    if is_parakeet_checkpoint(config) and PARAKEET_WEIGHTS in names:
        # D406 withdrew the `parakeet-mlx` runner (maintenance cost not
        # justified by use), so this branch now claims NO runner rather than
        # appending one — but it MUST keep the early return. A Parakeet/NeMo
        # ASR snapshot is a directory of safetensors identical in shape to a
        # transformers text checkpoint, so without this return the text
        # branch below would claim it and the page would offer to load a
        # speech model as a chat model. The correct answer for a cached NeMo
        # ASR repo is "a speech model nothing here can load" — matching no
        # runner, offered by nothing — not "matches nothing here so fall
        # through to whatever else recognises the file layout."
        return tuple(found)
    family = embed_model_type(config)
    if family and (torch_weights or onnx_weights):
        # TWO independent appends, not a fork. The same `onnx-community` account
        # sometimes re-uploads `model.safetensors` beside its export, and such a
        # repo really is readable by both engines — so each engine's rows are
        # gated on ITS OWN weight fact and neither excludes the other: MLX
        # reads safetensors and `onnxruntime` reads the `onnx/` graphs, and
        # those are two separate questions about one repo.
        if torch_weights and family in MLX_EMBED_MODEL_TYPES:
            found.append("mlx-embed")
        if onnx_weights:
            # All four execution providers, not just the CPU row —
            # `ONNX_EMBED_RUNNERS`' own comment gives the reason, and it is the
            # DIFFUSERS_RUNNERS/LLAMACPP_RUNNERS reason again: a variant
            # registered but absent here is invisible to the page. `mlx-embed`
            # gets no analogue on this side: MLX has no ONNX reader at all, so an
            # export is invisible to it whatever the family says.
            found.extend(ONNX_EMBED_RUNNERS)
        # …and NOTHING else, for the `.gguf` branch's reason: an embedding
        # snapshot is a directory of weights like any other, so the text branch
        # below would claim it and the page would offer to load an encoder as a
        # chat model. Load-bearing along two axes now, not one:
        #
        # * both LAYOUTS — an ONNX export ships `tokenizer.json` and a
        #   `config.json` and would fall through just as readily as a
        #   safetensors one;
        # * both FAMILIES — and the prose half is the sharper case. A dual
        #   encoder at least has a vision tower to make it obviously not a chat
        #   model; `BAAI/bge-base-en-v1.5` is a directory of safetensors with a
        #   `bert` config, byte-for-byte the shape `mlx-text` reads, and it can
        #   never generate a token.
        #
        # It returns even when `found` is EMPTY, which is the case a fallthrough
        # looks harmless in: a `nomic_bert` SAFETENSORS snapshot is loadable by
        # nothing here (mlx-embeddings has no module for it and this is not an
        # ONNX export), and "an embedding model nothing here can load" is the
        # correct answer — not "matches nothing here, so fall through to whatever
        # else recognises the file layout."
        return tuple(found)
    # A directory of safetensors, which since D416 exactly one engine here
    # reads: `mlx-text`. This branch used to fork — an MLX-packed checkpoint
    # went to `mlx-text` alone, anything else to `mlx-text` plus all three
    # `transformers-text*` rows — and with the transformers family gone both
    # arms answered the same thing, so the fork (and `is_mlx_checkpoint`, whose
    # only caller it was) went with it. Deliberately NOT replaced by a
    # `.gguf`-style DECISIVE claim: safetensors says nothing about the modality,
    # which is why this branch sits last, after every check that does.
    #
    # **`config` is REQUIRED, not merely consulted.** `mlx_lm.load` resolves a
    # checkpoint by reading `config.json` and importing
    # `mlx_lm.models.<model_type>`, so a weights directory without one is not a
    # repo this engine can open, whatever the extensions say. Claiming it anyway
    # is how `SymphonyGen/SymphonyGen` — four bare `.pt` checkpoints of a
    # symbolic-music policy, no config, `library_name: pytorch` — matched
    # `mlx-text`, which then let the caller's format fallback call it a chat
    # model. Same shape as the Parakeet early return above: the honest answer
    # for weights nothing can resolve is "no runner", not "the one engine whose
    # file extensions happen to match".
    if torch_weights and config and not unloadable_quant(config):
        found.append("mlx-text")
    return tuple(found)

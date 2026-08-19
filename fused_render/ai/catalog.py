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
two BACKENDS (text generation since D293, speech to text since D302) therefore
has two lists — hardware variants of one backend share theirs, since the wheel
changes and the readable format does not (`_SHARED_SUGGESTIONS`) — and the
registry's resolution decides which one a machine sees, the same mechanism
asked the same question, so the page cannot offer a model the loader would then
refuse.

**Which means the suggestion list CAN move when a PREFERENCE moves**, not only
when the hardware differs: a user who switches speech to text from MLX to
CTranslate2 on the Preferences page is shown four different repos next time they
open the AI Models page, and the ones they may already have downloaded stop
being offered. That is correct — a suggestion is only meaningful for the backend
that will load it — but it must not be silent, which is why `describe()` reports
the resolved runner's LABEL beside every list and the page shows it.

**"Can", because since the per-hardware split some engine switches move the
label and leave the list exactly where it is.** CPU -> CUDA -> ROCm is a change
of wheel, not of weights format, so those rows share one list by construction
(`_SHARED_SUGGESTIONS`) and the page correctly shows the same repos under a new
engine name. The rule underneath is unchanged and is the one to reason from: the
list belongs to the FORMAT a backend reads, and it moves when and only when that
format does.

Sizes are the on-disk download, and the notes are frank about the trade — which
since the per-hardware runner split means frank about the MODEL and never about
the machine: one list is shown by three engines (CPU, CUDA, ROCm), and a note
saying "tight on 16GB" would mean system RAM under one of them and VRAM under
the others. `size_gb` is the field that answers the budget question, in the one
unit this file defines; see the rule above the transformers list.

**`size_gb` IS EVERY BYTE THE DOWNLOAD FETCHES, ACROSS EVERY REPO IT TOUCHES.**
Not the weights file, not the interesting part, not one of the two repos — the
whole of what appears on the disk when the user presses Download, in decimal GB.
The diffusers FLUX entry said 2.6 for eighteen months of reading, because 2.6 is
what its GGUF transformer weighs, while the pull also brought the base repo's
text encoder and VAE: the true figure was 18.6 (D308). The mflux entry beside it
has always meant the whole snapshot. Two adjacent lists whose field means
different things is a silent ordering bug the moment either gains a second
entry, because the rule below sorts and DEFAULTS on this number.

**ONE ORDERING RULE: SMALLEST FIRST, AND THE DEFAULT FOLLOWS POSITION 0.** Every
list here is sorted by ascending `size_gb`, and `default_for()` returns
`entries[0]["id"]` — so what a bare `fused.ai.transcribe()` or `fused.ai.image()`
loads is simply the smallest model the resolved runner offers. Entries with no
`size_gb` (none today) sort LAST: an unknown download is the one thing that must
not be promoted into the "safe, small, starts quickly" slot.

**The cost of that rule is deliberate and was chosen by the user with the
trade-off in front of them (2026-08-16).** A no-model call now gets the LEAST
ACCURATE model rather than the recommended one — `Systran/faster-whisper-small`
instead of `deepdml/faster-whisper-large-v3-turbo-ct2`, `Qwen/Qwen3-1.7B` instead
of `Qwen/Qwen3-4B-Instruct-2507`. The alternative — a separate `default: True`
field, so the list could be ordered one way and the default picked another — was
offered and rejected: one rule that a reader can verify by eye beats two that can
silently disagree. **Do not "fix" this back** by reordering a list so the good
model leads, and do not reintroduce a default field; a smallest-first list whose
head is also the default is the intended design, not a sorting accident.
"""

from __future__ import annotations

from fused_render.ai import registry

#: runner code -> suggested models, SMALLEST FIRST (see the ordering rule in the
#: module docstring; position 0 is also the default). `size_gb` is the approximate
#: download in decimal GB — EVERY repo and every file the Download fetches, summed
#: from the Hub's per-file byte metadata and rounded to one decimal, never the
#: headline weights alone. **None when that metadata is unavailable**,
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
    # the same class, and under this file's own "sized for the machine that
    # will actually run them" rule that is a real cost paid on every entry.
    #
    # **OptiQ quants where they exist, and ONE ENTRY PER BASE MODEL.** The
    # `-OptiQ-4bit` repos are mixed precision: a sensitivity pass puts the
    # layers that need it at 8-bit and leaves the rest at 4-bit, written as a
    # per-layer map in the config's `quantization` dict. That is ordinary
    # mlx-lm mixed-precision format — they load on the runner as it ships, with
    # no new package and no pyproject change — and they are tagged
    # `text-generation`, while the `-MLX-4bit` siblings they replace are
    # `mlx-vlm` conversions tagged `image-text-to-text` whose own model cards
    # say a better conversion may exist elsewhere. Listing both would put one
    # base model on the page twice, distinguished by a suffix a reader cannot
    # interpret, which is a worse shortlist rather than a longer one.
    #
    # **The OptiQ premium is real and the model cards understate it.** They say
    # "within ~5% of a stock uniform 4-bit quant"; measured, the 9B is 8.22GB
    # against the uniform conversion's 5.98GB, which is 37% — 132 of its 248
    # layers sit at 8-bit, and a multi-token-prediction file (~0.19GB) and a
    # higher-precision vision tower ride along. The sizes here are the measured
    # ones, because a user picking "the better 9B" and finding two extra
    # gigabytes downloading is the surprise this column exists to prevent.
    # Their configs also carry keys mlx-lm ignores (`mtp_file`, `optiq_vision`,
    # `mlx_lm_extra_tensors`); nothing here reads them, and the `group_size`
    # and `bits` that `runners/formats.py` keys on are present at the top level
    # exactly as in a uniform quant — checked, not assumed.
    #
    # **Both `mlx-community/gemma-4-12B-it-4bit` and its OptiQ sibling are
    # deliberately absent**, and they are the entries a future reader is most
    # likely to try to add: newer than everything here, ungated, and the plain
    # one is — measured, not assumed — 1.3GB SMALLER than the Gemma 3 12B this
    # list used to carry. Neither loads. Their `model_type` is `gemma4_unified`,
    # mlx-lm resolves a checkpoint by importing `mlx_lm.models.<model_type>`,
    # and 0.31.3 ships `gemma4.py` and `gemma4_text.py` and no
    # `gemma4_unified.py` — so the load ends in "Model type gemma4_unified not
    # supported." The `e4b`/`e2b` siblings below are `gemma4` and do load.
    # Recheck when mlx-lm is next bumped.
    #
    # Ordered SMALLEST FIRST, like every list in this file, which means the 1.8GB
    # 2B leads and is what a no-model chat call loads — see the module docstring
    # for why that trade was taken deliberately. Sizes are the whole-snapshot Hub
    # byte sum on 2026-08-16 (D295), and every entry is ungated. **One line
    # each**, and each note names the entry it compares itself to rather than
    # saying "above"/"below", so a future size correction that moves a row does
    # not quietly falsify the prose.
    "mlx-text": [
        {
            "id": "mlx-community/Qwen3.5-2B-MLX-4bit",
            "label": "Qwen3.5 2B (MLX 4-bit)",
            "size_gb": 1.8,
            "note": "The smallest here and the one a bare call loads — weaker "
                    "than the rest, and it runs anywhere MLX does.",
        },
        {
            "id": "mlx-community/Qwen3.5-4B-OptiQ-4bit",
            "label": "Qwen3.5 4B (OptiQ 4-bit)",
            "size_gb": 4.0,
            "note": "The best all-round pick: strong on reasoning and code, and "
                    "comfortable on 16GB.",
        },
        {
            "id": "mlx-community/gemma-4-e4b-it-4bit",
            "label": "Gemma 4 E4B (4-bit)",
            "size_gb": 5.2,
            "note": "A second family at much the same size as the Qwen 4B, "
                    "worth trying on a prompt Qwen handles badly.",
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
        # size budget — the other 27B here is 20GB. Its note names the Qwen 9B
        # by name: under smallest-first the 9B now sits AFTER it, so the old
        # "the 9B above" wording was the reordering's first casualty.
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
            "note": "27B for less disk than the Qwen 9B costs. Ternary "
                    "quantization is new, so measure it against a 4-bit model "
                    "you already trust.",
        },
        {
            "id": "mlx-community/Qwen3.5-9B-OptiQ-4bit",
            "label": "Qwen3.5 9B (OptiQ 4-bit)",
            "size_gb": 8.2,
            "note": "Better answers than the Qwen 4B for twice the download — "
                    "tight on 16GB, so close other heavy apps first.",
        },
        # Qwen3.8 27B. The family ships two sizes and only this one is on the
        # table: the other is 2.4T-A95B, which no laptop runs. Sits between the
        # Qwen 9B and the Qwen3.6 27B on size, not at the bottom — smallest-first
        # is about size_gb alone, not how confident the entry is.
        {
            "id": "mlx-community/Qwen3.8-27B-4bit",
            "label": "Qwen3.8 27B (MLX 4-bit)",
            # Measured from the repo's blob sizes, not estimated.
            "size_gb": 16.1,
            "note": "Newest Qwen, bigger than the 9B above — wants 32GB+, so it "
                    "will not fit a 16GB machine. Its checkpoint is "
                    "Qwen3_5ForConditionalGeneration, the same class mlx-lm "
                    "already loads for the Qwen3.5 entries here, but that has "
                    "not been confirmed on this specific repo.",
        },
        # LAST, and the only entry here that is not a 16GB-machine model. It
        # lands at the bottom on size alone now, which happens to agree with
        # where its memory cost belongs: 20GB of weights resident is a 32GB Mac,
        # and the note says so instead of leaving someone to find out after the
        # download. Its 35B-A3B MoE sibling is omitted for the reason D310 gives
        # about mflux — 24.7GB resident on a 34GB machine already several GB
        # into swap is not something to suggest on one machine's evidence.
        {
            "id": "mlx-community/Qwen3.6-27B-OptiQ-4bit",
            "label": "Qwen3.6 27B (OptiQ 4-bit)",
            "size_gb": 20.0,
            "note": "The best answers here, and it needs 32GB — on a 16GB "
                    "machine this swaps rather than runs.",
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
    #
    # **THE NOTES ARE HARDWARE-NEUTRAL, and that is a rule rather than a style.**
    # This one list is what the CPU, CUDA and ROCm builds of Transformers all
    # show (see `_SHARED_SUGGESTIONS`), so a note that mentions a device is
    # wrong on two rows out of three: "the one to pick with no GPU" is a
    # tautology on the CPU engine, and "the only one that needs a GPU" reads as
    # "do not pick this" inside a list whose only purpose is to be picked from.
    # A memory claim is worse than wrong, it is ambiguous — "fits a 16GB
    # machine" means SYSTEM RAM on the CPU row and VRAM on the accelerated
    # ones, which are different numbers on the same physical machine, and on
    # ROCm they are not even the number on the box (pytorch#184880 has a 16GB
    # RX 9060 XT reporting ~7915MB usable, so an 8.1GB entry that "fits 16GB"
    # does not fit that row). **`size_gb` carries the budget question**, in the
    # one unit this file defines — every byte the download fetches — and a
    # reader comparing it against their own machine is doing arithmetic these
    # sentences cannot do for them.
    "transformers-text": [
        {
            "id": "Qwen/Qwen3-1.7B",
            "label": "Qwen3 1.7B",
            "size_gb": 4.1,
            "note": "The smallest here and the one a bare call loads — quickest "
                    "to fetch and quickest to answer.",
        },
        {
            "id": "microsoft/Phi-4-mini-instruct",
            "label": "Phi-4 mini (3.8B)",
            "size_gb": 7.7,
            "note": "Punches above its size on reasoning and maths, and MIT "
                    "licensed.",
        },
        {
            "id": "Qwen/Qwen3-4B-Instruct-2507",
            "label": "Qwen3 4B Instruct",
            "size_gb": 8.1,
            "note": "The best all-round pick: clearly stronger than the small "
                    "ones without being the largest download here.",
        },
        {
            "id": "Qwen/Qwen3-8B",
            "label": "Qwen3 8B",
            "size_gb": 16.4,
            "note": "Best quality here, and twice the download and the memory "
                    "of the pick above it.",
        },
    ],
    "diffusers-image": [
        {
            "id": "black-forest-labs/FLUX.2-klein-4B",
            "label": "FLUX.2 klein 4B",
            # EVERYTHING the Download fetches, both repos: 8.23 of base
            # components (text encoder 8.05, VAE 0.17, tokenizer + configs) plus
            # the 2.60 GGUF transformer. It said 2.6 — the GGUF alone — while
            # the actual pull was 18.6, and the field two lists down means the
            # whole download; see the module docstring's rule (D308).
            "size_gb": 10.8,
            # Hardware-neutral, per the rule the transformers list above states:
            # this one list serves the CPU, CUDA and ROCm Diffusers rows, and
            # the sentence used to say the full-precision pipeline "OOMs on
            # 16GB machines" — a claim about system RAM on one row and about
            # VRAM on the others, and on ROCm about neither (a 16GB card can
            # report half that usable). What survives the move is the fact that
            # decides the choice anyway: this is the smaller thing to fetch and
            # to hold.
            "note": "Quantized transformer (Q4_K_M) rather than the bf16 "
                    "original — several GB less to fetch and to hold in memory.",
        },
    ],
    # The same model, converted for MLX — and unloadable by the runner above,
    # which is the now-familiar shape: a repo belongs to a backend, not to a
    # capability. `size_gb` is the whole snapshot, because unlike the diffusers
    # recipe there is nothing skipped and no second repo: every component is
    # already 4-bit. (2026-08-15 Hub metadata sums to 4.62e9 bytes. D310's
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
    # list in the app: a `mlx-community` repo carries `weights.npz`,
    # `weights.safetensors`, or (on the newer quantized re-uploads) transformers'
    # own filename `model.safetensors` beside whisper's native config — none of
    # which is CTranslate2's `model.bin` or a transformers checkpoint. Suggesting
    # across the line is exactly the trap `faster_whisper/worker.py` had to
    # write an error message about, now with three ways to fall into it.
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
            "id": "mlx-community/whisper-tiny.en-8bit",
            "label": "Whisper tiny English (MLX 8-bit)",
            "size_gb": 0.05,
            "note": "The quickest download and decode here, English only — "
                    "fine for a rough draft of clear speech, below small on "
                    "everything else.",
        },
        {
            "id": "mlx-community/whisper-small-mlx",
            "label": "Whisper small (MLX)",
            "size_gb": 0.5,
            "note": "The smallest here, and what a bare transcribe call loads — "
                    "quick, but it drops names and punctuation turbo gets "
                    "right.",
        },
        {
            "id": "mlx-community/whisper-medium-mlx",
            "label": "Whisper medium (MLX)",
            "size_gb": 1.5,
            "note": "Bigger than small for no gain in English over turbo, which "
                    "costs about the same — worth it only for a language turbo "
                    "struggles with.",
        },
        {
            "id": "mlx-community/whisper-large-v3-turbo",
            "label": "Whisper large-v3 turbo (MLX)",
            "size_gb": 1.6,
            "note": "The best value here: large-v3 accuracy at a fraction of "
                    "its decoding cost, and faster than real time by a wide "
                    "margin on Metal.",
        },
        {
            "id": "mlx-community/whisper-large-v3-mlx",
            "label": "Whisper large-v3 (MLX)",
            "size_gb": 3.1,
            "note": "The full model, for a recording turbo handles badly — "
                    "twice turbo's disk and several times its decoding.",
        },
    ],
    # NeMo Parakeet exports ONLY, and the FOURTH mutually unloadable speech
    # list in the app (D319). These carry `model.safetensors` — the same
    # filename a transformers checkpoint has — beside a `config.json` naming a
    # NeMo ASR class, which is what tells the two apart (`formats.py`). A
    # Whisper repo suggested here would fail inside `from_config` with "Model
    # is not supported yet!", and a Parakeet repo suggested to a whisper runner
    # fails the other way; the split is per RUNNER for exactly this reason.
    #
    # These are the repos a Mac sees only after CHOOSING this engine on the
    # Engines tab — the registry keeps Whisper as the default (see the runner
    # row). Sizes are the same full-snapshot Hub metadata estimate as the lists
    # above (2026-08-17). **One line each**, per the rule the transformers list
    # states: a shortlist is read by sweeping it.
    "parakeet-mlx": [
        {
            "id": "mlx-community/parakeet-tdt_ctc-110m",
            "label": "Parakeet TDT-CTC 110M",
            "size_gb": 0.5,
            "note": "The smallest here, and what a bare transcribe call loads — "
                    "English only, and it drops the punctuation the 0.6B "
                    "models get right.",
        },
        {
            "id": "mlx-community/parakeet-tdt-0.6b-v2",
            "label": "Parakeet TDT 0.6B v2",
            "size_gb": 2.5,
            "note": "English only, and the most accurate of the three on it — "
                    "pick v3 instead unless every recording is in English.",
        },
        {
            "id": "mlx-community/parakeet-tdt-0.6b-v3",
            "label": "Parakeet TDT 0.6B v3",
            "size_gb": 2.5,
            "note": "The one to reach for: v2's accuracy plus 24 more European "
                    "languages, detected rather than declared.",
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
            "id": "Systran/faster-whisper-small",
            "label": "Whisper small (CT2)",
            "size_gb": 0.5,
            "note": "The smallest here, and what a bare transcribe call loads — "
                    "light enough for an old machine, but it drops names and "
                    "punctuation turbo gets right.",
        },
        {
            "id": "Systran/faster-whisper-medium",
            "label": "Whisper medium (CT2)",
            "size_gb": 1.5,
            "note": "Bigger than small for no gain in English over turbo, which "
                    "costs about the same — worth it only for a language turbo "
                    "handles badly.",
        },
        {
            "id": "deepdml/faster-whisper-large-v3-turbo-ct2",
            "label": "Whisper large-v3 turbo (CT2)",
            "size_gb": 1.6,
            "note": "The best value here: large-v3 accuracy at roughly a "
                    "quarter of its decoding cost. Usable on CPU — a laptop "
                    "transcribes faster than real time.",
        },
    ],
}

#: Hardware variant -> the runner whose list it SHARES. Resolved by `for_runner`
#: rather than copied into `SUGGESTIONS`, and the direction of that choice is the
#: point.
#:
#: **This file is keyed by runner because a repo belongs to a BACKEND** — the
#: docstring's argument is about weights formats, `mlx-community/…` against
#: `Qwen/…`, and two lists exist because neither backend can open the other's
#: files. A CUDA build of Transformers reads byte for byte what the CPU build
#: reads; the split between them is which wheel gets installed, and nothing
#: about which repos are loadable. So their lists must be identical BY
#: CONSTRUCTION. Four copied literals would be identical only until somebody
#: edited one of them, and the failure that produces — a curated model offered
#: on the CPU engine and missing on the CUDA one, or worse, sized for a
#: different budget — is silent on the page.
#:
#: An alias also keeps two invariants this file states elsewhere true: every id
#: still appears in exactly ONE list (`capability_of` reads that), and
#: `all_suggested_ids()` is not four copies deduplicated by luck.
#:
#: What must NOT be aliased is a runner that reads a different format. That is
#: the whole keying rule, and it is why this table names the specific pairs
#: instead of stripping a suffix off a code.
_SHARED_SUGGESTIONS = {
    "transformers-text-cuda": "transformers-text",
    "transformers-text-rocm": "transformers-text",
    "diffusers-image-cuda": "diffusers-image",
    "diffusers-image-rocm": "diffusers-image",
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
    """The curated list for a runner, following the hardware-variant alias.

    A copy of the list, as it always was — callers append to it (the router's
    cached-repo union does) and must not be editing the curation.
    """
    return list(SUGGESTIONS.get(_SHARED_SUGGESTIONS.get(code, code), ()))


def for_capability(capability: str) -> list[dict]:
    """What to suggest for `capability` on THIS machine."""
    runner = _runner_for(capability)
    return for_runner(runner.code) if runner else []


def default_for(capability: str) -> str | None:
    """The default for a capability: what "just load something" means here.

    Position 0 of the resolved runner's list, and every list is sorted smallest
    first — so this is the SMALLEST model, not the best one. That is the whole
    of the rule and it was chosen deliberately; the module docstring records why,
    and why there is no separate default field to override it.

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


def capability_of(repo_id: str) -> str | None:
    """The capability a SUGGESTED repo belongs to, or None for anything not here.

    The cheap pre-cache signal a load needs (D321): inferring a capability from
    the weight layout requires a snapshot on disk, and a cold load has none —
    but for every repo this app itself recommends, the curation already knows,
    and knowing costs a dict lookup rather than a Hub round trip.

    Keyed by RUNNER like the rest of the file, so the capability comes from the
    registry rather than being restated here — one repo id belongs to exactly
    one runner's list, and three mutually unloadable Whisper conversions are why
    that list is per-backend in the first place.

    **That one-list-per-id invariant survives the hardware variants only because
    they are ALIASED rather than copied** (`_SHARED_SUGGESTIONS`): a CUDA
    Transformers row shares the CPU row's entries instead of holding its own,
    so no id appears under two keys and this loop cannot see the same repo
    twice. Copying the lists would not have broken the ANSWER — the variants of
    one backend share a capability, so first-match-wins returns the same string
    — but it would have made the sentence above false, which is how a later
    reader ends up trusting an invariant nothing enforces.
    """
    for code, entries in SUGGESTIONS.items():
        if any(entry["id"] == repo_id for entry in entries):
            runner = registry.by_code(code)
            if runner is not None:
                return runner.capability
    return None


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
                # The backend in words ("Transformers (CUDA)"), because with
                # two runners per capability the code alone stopped being
                # something a page could show a person.
                "runnerLabel": runner.label if runner else None,
                # …and the qualifier-free one, which is what the Discover
                # heading uses ("via MLX Whisper"): that caption says which
                # backend these suggestions belong to, not which backend to
                # pick, so the platform half is noise there.
                "runnerShortLabel": runner.short if runner else None,
                # …and what using it is LIKE. The Discover tab no longer prints
                # this over its capability sections (D315): only some runners
                # have one, so the sections came out blotchy, and the one that
                # matters is a caution about an engine CHOICE, which now reads
                # under the picker on the Engines tab. The field stays on this
                # payload because it is the answer to "what is the runner
                # serving this capability like", which is a question about the
                # catalog and not about where a page chose to print it.
                "runnerNote": runner.note or None if runner else None,
                "available": status.ok,
                "reason": status.reason or None,
                "default": for_capability(capability)[0]["id"]
                if for_capability(capability) else None,
                "models": for_capability(capability),
            }
        )
    return rows

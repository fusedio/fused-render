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
unloadable rubbish on a Windows box, while `Qwen/Qwen3.5-4B` is the
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
unit this file defines; see the rules stated above `SUGGESTIONS` itself.

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
ACCURATE model rather than the recommended one — `Systran/faster-whisper-tiny.en`
instead of `deepdml/faster-whisper-large-v3-turbo-ct2`, and the smallest text
entry instead of the strongest one (`Qwen/Qwen3.5-4B` rather than
`Qwen/Qwen3.5-9B` after the 2026-08-21 refresh; the pair the user was actually
shown was `Qwen/Qwen3-1.7B` against `Qwen/Qwen3-4B-Instruct-2507`, both since
retired, which is why the rule is stated in terms of POSITION and not of
names). The alternative — a separate `default: True`
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
#: `nickname` is the short human name the Playground sidebar shows — the
#: model without its quantization/engine qualifier. A curated FIELD, never
#: derived by stripping the label's parenthetical at runtime, for the reason
#: `short_label` and `family_label` are fields too (AI-2c): a stripped name
#: is a value nobody owns and no test can see.
#:
#: **`recommended` IS A SECOND AXIS, AND IT IS THE ONLY ONE THE PLAYGROUND
#: READS.** Absent on most entries and `True` on a few, opt-in: every entry here
#: is a model this app stands behind on the AI Models page, and the marked subset
#: is the shorter list the Playground offers — recommended-or-downloaded is what
#: its sidebar draws (D425). The two surfaces want different lengths for the same
#: curation. The AI Models page is a place to SHOP: eight text entries from 0.7GB
#: to 20GB is the range someone comparing downloads needs, and its Local tab
#: exists to say what a disk already holds. The Playground is a place to TRY,
#: reached by someone who wants to type a sentence and see what comes back — and
#: a sidebar of eight rows, five of which are a multi-gigabyte download away
#: from answering, is a decision where a text box was wanted.
#:
#: **It is NOT "the best ones", and it is NOT the default.** `default_for()` is
#: still position 0, still the smallest, and a recommended entry has no bearing
#: on it — see the module docstring on why there is no `default: True` field and
#: why reintroducing one under this name would be the same mistake wearing a new
#: word. What the flag means is "a first thing to try": the quick one that
#: answers on any machine, the one worth real work, and a second family to try a
#: bad answer against. A 20GB model can be the best row in its list and still be
#: a poor first click.
#:
#: **Each list needs at least one, and a test pins that** — an unmarked list
#: leaves the Playground's group empty on a machine with nothing downloaded,
#: which is the one state this filter must not produce.
#:
#: **ONE LINE EACH.** A shortlist is read by SWEEPING it, and four cards of three
#: sentences is a wall nobody reaches the end of — so each note carries the
#: single thing that would change the choice and stops, with the rest left to
#: the model card the name links to. The oldest lists here predate the rule and
#: still run to three sentences.
#:
#: **THE NOTES ARE HARDWARE-NEUTRAL, and that is a rule rather than a style.**
#: A list is shown by every hardware variant of its runner (`_SHARED_SUGGESTIONS`),
#: so a note that mentions a device is wrong on all but one of them: "the one to
#: pick with no GPU" is a tautology on the CPU row, and "the only one that needs
#: a GPU" reads as "do not pick this" inside a list whose only purpose is to be
#: picked from. A memory claim is worse than wrong, it is ambiguous — "fits a
#: 16GB machine" means SYSTEM RAM on the CPU row and VRAM on the accelerated
#: ones, which are different numbers on the same physical machine, and on ROCm
#: they are not even the number on the box (pytorch#184880 has a 16GB RX 9060 XT
#: reporting ~7915MB usable). **`size_gb` carries the budget question**, in the
#: one unit this file defines — every byte the download fetches — and a reader
#: comparing it against their own machine is doing arithmetic these sentences
#: cannot do for them.
#:
#: Both rules were written above the `transformers-text` list and are stated here
#: because D416 removed it: a rule that lives inside one entry of a table is a
#: rule that disappears when that entry does, and four other lists cite it.
SUGGESTIONS: dict[str, list[dict]] = {
    # Refreshed 2026-08-16, and the refresh brought one fact this list has not
    # had to state before.
    #
    # **EVERY QWEN AND GEMMA CHECKPOINT HERE IS MULTIMODAL, AND HALF OF IT IS
    # DEAD WEIGHT** — the LFM2.5 entry at position 0 is the exception, and it
    # is the first entry this list has ever had that pays none of this cost:
    # `Lfm2ForCausalLM`, no `vision_config`, every byte fetched is a byte the
    # language model uses. Qwen3.5, Qwen3.6 and gemma-4 all ship as
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
    # Ordered SMALLEST FIRST, like every list in this file, which means the
    # 0.7GB LFM2.5 1.2B leads and is what a no-model chat call loads — see the
    # module docstring for why that trade was taken deliberately. Sizes are the
    # whole-snapshot Hub byte sum on 2026-08-16 (D295), the LFM2.5 row's
    # re-measured on 2026-08-21, and every entry is ungated. **One line each**,
    # and each note names the entry it compares itself to rather than saying
    # "above"/"below", so a future size correction that moves a row does not
    # quietly falsify the prose.
    "mlx-text": [
        # Position 0, replacing `mlx-community/Qwen3.5-2B-MLX-4bit` (1.7GB),
        # and it is a straight win rather than a trade: 2.6x less to fetch,
        # text-only where the Qwen 2B spent part of that on a vision tower
        # mlx-lm drops, and the same model the `llamacpp-text` list leads with
        # — so the two platforms now start a bare `fused.ai()` on the SAME
        # model rather than on two different compromises.
        #
        # **Loadable by the mechanism, not by a load** — this repo could not
        # be tried here, because MLX does not install off Apple Silicon and
        # the refresh that added it was done on Linux. What WAS checked is the
        # exact resolution step the `gemma4_unified` paragraph above turns on:
        # `model_type` is `lfm2`, mlx-lm resolves a checkpoint by importing
        # `mlx_lm.models.<model_type>`, and 0.31.3's wheel ships `lfm2.py`
        # (read out of the published wheel, not assumed). Its `quantization`
        # block is a plain uniform `{group_size: 64, bits: 4, mode: affine}`
        # with no `quant_method`, so `formats.unloadable_quant` passes it the
        # same way it passes every other row here. Someone on a Mac should
        # still load it once.
        {
            "id": "mlx-community/LFM2.5-1.2B-Instruct-4bit",
            "recommended": True,
            "label": "LFM2.5 1.2B Instruct (MLX 4-bit)",
            "nickname": "LFM2.5 1.2B Instruct",
            "size_gb": 0.7,
            "note": "The smallest here and the one a bare call loads — quick "
                    "to fetch and to answer, and weaker than every other row.",
        },
        {
            "id": "mlx-community/Qwen3.5-4B-OptiQ-4bit",
            "recommended": True,
            "label": "Qwen3.5 4B (OptiQ 4-bit)",
            "nickname": "Qwen3.5 4B",
            "size_gb": 4.0,
            "note": "The best all-round pick: strong on reasoning and code, and "
                    "comfortable on 16GB.",
        },
        # The only MIXTURE-OF-EXPERTS row in this file, and it is here because
        # the router changes what a size means: 8B of weights resident, ~1B of
        # them multiplied per token, so it answers at roughly the speed of the
        # 1.2B at the head of this list while knowing what an 8B knows. That
        # is a different axis from every other row, which is why it earns a
        # line 0.35GB from the Gemma below rather than duplicating it.
        #
        # **Nothing special is needed to load it, and that is worth stating
        # because it is easy to assume otherwise.** MoE experts are ordinary
        # tensors in the checkpoint; what is conditional is the COMPUTE, which
        # the router does inside the model. There is no "load only the active
        # experts" mode to miss — the whole 4.9GB is fetched and resident, and
        # the win is arithmetic per token, not bytes.
        #
        # `LiquidAI/`, not `mlx-community/`: the publisher's own conversion is
        # the only one (checked 2026-08-21), which is the same reason the
        # prism-ml Bonsai row below is not an `mlx-community` id either.
        # Mechanism-checked like the LFM2.5 row at position 0 and NOT loaded,
        # for the same reason — `model_type` is `lfm2_moe`, and 0.31.3's wheel
        # ships `lfm2_moe.py`, read out of it.
        {
            "id": "LiquidAI/LFM2.5-8B-A1B-MLX-4bit",
            "label": "LFM2.5 8B-A1B (MLX 4-bit)",
            "nickname": "LFM2.5 8B-A1B",
            "size_gb": 4.9,
            "note": "8B of knowledge answering at about a 1B's speed — a "
                    "mixture of experts, so only a fraction of it runs per "
                    "token.",
        },
        {
            "id": "mlx-community/gemma-4-e4b-it-4bit",
            "recommended": True,
            "label": "Gemma 4 E4B (4-bit)",
            "nickname": "Gemma 4 E4B",
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
            "nickname": "Ternary Bonsai 27B",
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
            "nickname": "Qwen3.5 9B",
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
            "nickname": "Qwen3.8 27B",
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
            "nickname": "Qwen3.6 27B",
            "size_gb": 20.0,
            "note": "The best answers here, and it needs 32GB — on a 16GB "
                    "machine this swaps rather than runs.",
        },
    ],
    # llama.cpp / GGUF (SPEC AI-11, D411) — opt-in, and the only runner here
    # whose ids are NOT Hub repo ids: a GGUF repo publishes two dozen
    # quantizations of one model (`unsloth/Qwen3.5-9B-GGUF` alone sums to
    # 147.81GB across every file it holds), so the id is the curated key
    # `runners/llama_text.py`'s `_GGUF_RECIPES` maps to one `(repo, file)`
    # pair — see that module's docstring for why this is not a `repo:quant`
    # grammar. **That is also why Hub search cannot populate this list**: the
    # Discover tab's search hands back a bare repo id, this runner has no rule
    # for picking one file out of thirty, and only the ids curated here ever
    # load — the same limitation `formats.COMPONENT_REPOS`'s repos already
    # have, for the same reason.
    #
    # **Every entry's `general.architecture` is checked against
    # `formats.GGUF_TEXT_ARCHITECTURES` BEFORE it is listed, and then the file
    # is actually LOADED.** The metadata check is cheap and catches the wrong
    # class of model; it does not prove that the June 2026 llama.cpp this
    # runner vendors can build an August 2026 checkpoint of a family whose
    # NAME it knows. That gap is exactly the trap the `mlx-text` list above
    # documents for `gemma4_unified`, so every entry ADDED here was loaded
    # through `llamacpp_text/.venv`'s own `llama_cpp.Llama` and asked for a
    # token before it went in (2026-08-21, llama-cpp-python 0.3.29): LFM2.5
    # 1.2B (`lfm2`), Qwen3.5 4B Q4_K_M (`qwen35`), Gemma 4 E4B (`gemma4`) and
    # LFM2.5 8B-A1B (`lfm2moe`) each answered. The 27B row carries over from the previous shortlist
    # UNCHANGED and was not re-loaded for this refresh — 13GB to re-verify a
    # file that was already shipping. Redo the loads when the pin in
    # `llamacpp_text/pyproject.toml` moves; an arch name in the table is a
    # necessary condition, never a sufficient one.
    #
    # **Three publishers over five rows, and that is a REQUIREMENT of the list
    # rather than a coincidence of what was newest.** This list was five Qwen
    # entries over three Qwen repos: two of them the same 4B at two
    # quantizations, two of them the same 9B. A shortlist whose every row
    # shares a tokenizer, a training mix and a failure mode gives a user
    # nothing to try when the first answer is bad — "measure it against a
    # model you already trust" is advice the MLX list can give because it has
    # Gemma beside Qwen, and this one could not. Now: Liquid, Qwen and Google.
    #
    # **Liquid appears twice, and the second one is not a duplicate.** The
    # 1.2B at position 0 and the 8B-A1B below it are a dense model and a
    # mixture of experts — the same publisher, and nothing else in common that
    # matters to a picker. Where a repeated Qwen 4B at two quantizations gave
    # a reader one choice wearing two labels, these two answer different
    # questions ("the smallest thing that works" and "8B answers at 1B
    # speed"), which is the test a row has to pass to be here.
    #
    # **And that requirement is load-bearing precisely BECAUSE of D416.** Since
    # the transformers family was withdrawn, this list is what a bare "auto"
    # reaches on Windows and Linux rather than an opt-in a user had to go
    # looking for — so these four entries are no longer an alternative
    # shortlist beside a safetensors one, they are the whole of what text
    # generation suggests on those platforms. A monoculture was a thin
    # shortlist when it was the second list; it is the only list now.
    #
    # **The Qwen entries are still current-generation, which is the thing that
    # made the old monoculture defensible.** llama.cpp converts and runs the
    # TEXT TOWER of the `Qwen3_5ForConditionalGeneration` family — the same
    # language model the removed `transformers-text` list served in full bf16
    # precision, at 19.3GB for the 9B (D416) — so a machine too small for that
    # download still runs the same model's answers at a fraction of the size,
    # not a smaller model wearing the same name.
    #
    # Sizes verified against the Hub's `?blobs=true` metadata on 2026-08-21,
    # summing ONLY the one GGUF file `download_file` fetches (never the whole
    # repo) plus nothing else — a GGUF carries its own vocabulary, config and
    # chat template inside the one file, so there is no second download to add
    # in whatever else sits beside it in the repo (the LFM2.5 repo ships a
    # `leap/` directory of runtime manifests and the gemma repo a `config.json`
    # and an `MTP/` folder; none of it is fetched, see
    # `runners/llama_text.py`). Every file checked is a single
    # root-level `.gguf`, not a `-00001-of-0000N` shard — `download_file` takes
    # one filename, so a sharded tier would have been silently unloadable and
    # was excluded before it could ship (the 27B repo's own BF16 tier IS
    # sharded, which is one reason its shortlist entry is the aggressively
    # quantized UD-Q3_K_XL rather than anything closer to full precision).
    # Every repo checked `gated: false`.
    #
    # **The "no text model under 4B" rule is deliberately broken at position 0,
    # and only there.** That rule was written against 2025-era 1-3B models and
    # it is the right default still — but position 0 is not a recommendation,
    # it is what a bare `fused.ai()` loads on a machine whose owner never
    # opened this page, and on the CPU wheel that is most non-Apple machines.
    # A 2.7GB download that answers at a few tokens a second is a worse first
    # experience than a 0.7GB one that answers immediately, and LFM2.5 is not a
    # small transformer — Liquid's hybrid short-convolution stack is built for
    # CPU decode, which is the case this engine actually defaults into. The
    # `mlx-text` list above leads with the SAME model for the same reason, so
    # the two platforms now start a no-model call on one model rather than on
    # two different compromises. Every OTHER row here obeys the rule.
    #
    # **One line each** and hardware-neutral, per `SUGGESTIONS`' own rules:
    # this list
    # is shared by BOTH llama.cpp builds (`_SHARED_SUGGESTIONS` aliases
    # `llamacpp-text-vulkan` to this same key), so a note naming one build's
    # device would be wrong on the other.
    "llamacpp-text": [
        # The two Q8_0 rows this list used to carry (the 4B at 4.5GB and the 9B
        # at 9.5GB) are gone, and not for length: Q8_0 is roughly twice
        # Q4_K_M's arithmetic per token for a quality gain that is small next
        # to moving up a size class, and this engine's default build has no
        # GPU to hide that behind. A row that is never the right pick fails
        # this file's own "every line is somebody's answer" test.
        {
            "id": "LFM2.5-1.2B-Instruct-Q4_K_M.gguf",
            "recommended": True,
            "label": "LFM2.5 1.2B Instruct (Q4_K_M)",
            "nickname": "LFM2.5 1.2B Instruct",
            "size_gb": 0.7,
            "note": "The smallest here and the one a bare call loads — a "
                    "hybrid architecture built for CPU decode, so it answers "
                    "immediately where a 4B thinks.",
        },
        {
            "id": "Qwen3.5-4B-Q4_K_M.gguf",
            "recommended": True,
            "label": "Qwen3.5 4B (Q4_K_M)",
            "nickname": "Qwen3.5 4B",
            "size_gb": 2.7,
            "note": "The first row here strong enough for real work: current-"
                    "gen Qwen, and a fifth of the unquantized 4B's download.",
        },
        {
            "id": "gemma-4-E4B-it-Q4_K_M.gguf",
            "recommended": True,
            "label": "Gemma 4 E4B (Q4_K_M)",
            "nickname": "Gemma 4 E4B",
            "size_gb": 5.0,
            "note": "A second family, worth trying on a prompt the Qwen 4B "
                    "above handles badly — it answers above its size class "
                    "for the download.",
        },
        # The mixture-of-experts row, and it matters MORE here than its MLX
        # twin does: this list's default build has no GPU, and a router that
        # multiplies ~1B of an 8B model per token is the one architecture that
        # turns a CPU into a reasonable place to run an 8B at all. Nothing in
        # the runner has to know — MoE experts are ordinary tensors, only the
        # COMPUTE is conditional, and llama.cpp does that inside the graph.
        #
        # **What we CANNOT do for it is placement**, and that is a binding
        # limit rather than an oversight: llama.cpp's `--n-cpu-moe` (keep the
        # expert tensors on the CPU, put attention on the GPU) is backed by
        # `llama_model_params.tensor_buft_overrides`, which llama-cpp-python
        # 0.3.29 exposes in the ctypes struct and NOT as a `Llama.__init__`
        # keyword — checked against the installed signature, which offers only
        # `n_gpu_layers`, `split_mode`, `main_gpu` and `tensor_split`. So on
        # the Vulkan build this row is offloaded by whole layers like any
        # dense model, leaving the trick unused. On the CPU build, which is
        # what most machines resolve to, there is nothing left on the table.
        {
            "id": "LFM2.5-8B-A1B-Q4_K_M.gguf",
            "label": "LFM2.5 8B-A1B (Q4_K_M)",
            "nickname": "LFM2.5 8B-A1B",
            "size_gb": 5.2,
            "note": "8B of knowledge answering at about a 1B's speed — a "
                    "mixture of experts, so only a fraction of it runs per "
                    "token.",
        },
        {
            "id": "Qwen3.8-27B-UD-Q3_K_XL.gguf",
            "label": "Qwen3.8 27B (UD-Q3_K_XL)",
            "nickname": "Qwen3.8 27B",
            "size_gb": 13.1,
            "note": "The newest and largest model here, quantized hard to fit "
                    "the download — expect a bigger quality hit than the "
                    "LFM2.5 8B-A1B above, and a laptop GPU to run it mostly "
                    "or entirely on the CPU rather than resident in VRAM.",
        },
    ],
    "diffusers-image": [
        {
            "id": "tonera/FLUX.2-klein-4B-int8-diffusers",
            "recommended": True,
            "label": "FLUX.2 klein 4B (int8)",
            "nickname": "FLUX.2 klein 4B",
            # The whole repo, per the module docstring's rule: 8.22e9 bytes of
            # `usedStorage` (2026-08-20 Hub metadata), and here that number IS
            # the download — one repo, no component repo, and no skipped
            # subfolder, because the quantization is already in the checkpoint.
            "size_gb": 8.2,
            # **No `_GGUF_RECIPES` row, and that is the point of this entry.**
            # The repo is a full diffusers pipeline whose `transformer/config.json`
            # carries `"quant_method": "torchao"`, so `from_pretrained` builds
            # the quantized component itself and the ordinary no-recipe branch
            # of `torch_image.load()` loads it unchanged. Its weights are a
            # `.bin` rather than a `.safetensors` — torchao's tensor subclasses
            # do not serialize to safetensors in this save — which needs no flag
            # either: `use_safetensors` defaults to None, which ModelMixin reads
            # as "prefer safetensors, allow pickle", and the fallback to
            # `diffusion_pytorch_model.bin` is per COMPONENT, so the text
            # encoder and VAE still come from their safetensors. Forcing
            # `use_safetensors=False` would break those two instead.
            #
            # It is the only entry, so it is what a bare `fused.ai.image()`
            # starts: the ordering rule
            # (`test_every_suggestion_list_is_ordered_smallest_first`) holds
            # trivially, and being the smaller thing to fetch is why this is the
            # entry the list keeps.
            #
            # Hardware-neutral, per `SUGGESTIONS`' own rule: this one list
            # serves the CPU, CUDA and ROCm Diffusers rows, so a
            # note about what fits in "16GB" would mean system RAM on one row
            # and VRAM on the others.
            "note": "One self-contained repo with an int8-quantized "
                    "transformer: several GB less to fetch and to hold than the "
                    "bf16 FLUX.2 pipeline it is built from.",
            # klein is a step-distilled model: D310's benchmark ran it at 4
            # steps (~34s an image on the reference Mac), and the server's
            # generic 28-step default turns a first image into minutes for no
            # quality a distilled model can spend. The Playground reads this
            # hint; a model without one keeps the server's default.
            "defaults": {"steps": 4},
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
            "recommended": True,
            "label": "FLUX.2 klein 4B (MLX 4-bit)",
            "nickname": "FLUX.2 klein 4B",
            "size_gb": 4.6,
            "note": "One repo instead of the Diffusers split, and quicker per "
                    "image — but it reserves far more memory while running.",
            # The same distilled model; the same D310 4-step benchmark.
            "defaults": {"steps": 4},
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
    # (2026-08-15). **One line each**, per `SUGGESTIONS`' own rule: a
    # shortlist is read by sweeping it.
    #
    # **No medium.** It weighs about what large-v3 turbo weighs and turbo is
    # better at every language, so a medium row would be an entry that is never
    # the right pick — a shortlist earns its length by every line being
    # somebody's answer.
    "mlx-whisper": [
        {
            "id": "mlx-community/whisper-tiny.en-8bit",
            "label": "Whisper tiny English (MLX 8-bit)",
            "nickname": "Whisper tiny English",
            "size_gb": 0.05,
            "note": "The quickest download and decode here, English only — "
                    "fine for a rough draft of clear speech, below small on "
                    "everything else.",
        },
        {
            "id": "mlx-community/whisper-small-mlx",
            "recommended": True,
            "label": "Whisper small (MLX)",
            "nickname": "Whisper small",
            "size_gb": 0.5,
            "note": "The smallest here, and what a bare transcribe call loads — "
                    "quick, but it drops names and punctuation turbo gets "
                    "right.",
        },
        {
            "id": "mlx-community/whisper-large-v3-turbo",
            "recommended": True,
            "label": "Whisper large-v3 turbo (MLX)",
            "nickname": "Whisper large-v3 turbo",
            "size_gb": 1.6,
            "note": "The best value here: large-v3 accuracy at a fraction of "
                    "its decoding cost, and faster than real time by a wide "
                    "margin on Metal.",
        },
        {
            "id": "mlx-community/whisper-large-v3-mlx",
            "label": "Whisper large-v3 (MLX)",
            "nickname": "Whisper large-v3",
            "size_gb": 3.1,
            "note": "The full model, for a recording turbo handles badly — "
                    "twice turbo's disk and several times its decoding.",
        },
    ],
    # A fourth speech list here, `parakeet-mlx` (NeMo Parakeet exports), lived
    # briefly under D319 and was removed by D406 along with the runner that
    # read it — maintenance cost not justified by use.
    #
    # CTranslate2 conversions ONLY. `openai/whisper-large-v3` is the repo
    # everyone reaches for and it does not load here — the runner reads
    # CTranslate2's `model.bin`, not transformers' safetensors — so suggesting
    # one would hand the user the exact failure `worker.py` had to write an
    # error message about.
    #
    # Sizes use the same full-snapshot Hub metadata estimate every list in this
    # file uses (2026-08-14; tiny.en re-checked 2026-08-21), including model.bin
    # plus tokenizer and configs. It was stated as "the same estimate as the
    # Transformers list above" until D416 removed that list — the METHOD is
    # what the sentence is about, and it is unchanged, so it now names the method
    # rather than a neighbour that has to keep existing for this to parse.
    #
    # **No medium.** large-v3 turbo weighs about the same 1.6GB and is better at
    # every language, so a medium row would be an entry that is never the right
    # pick — a shortlist earns its length by every line being somebody's answer.
    "faster-whisper": [
        {
            "id": "Systran/faster-whisper-tiny.en",
            "label": "Whisper tiny English (CT2)",
            "nickname": "Whisper tiny English",
            "size_gb": 0.08,
            "note": "The quickest download and decode here, English only — "
                    "fine for a rough draft of clear speech, below small on "
                    "everything else.",
        },
        {
            "id": "Systran/faster-whisper-small",
            "recommended": True,
            "label": "Whisper small (CT2)",
            "nickname": "Whisper small",
            "size_gb": 0.5,
            "note": "Light enough for an old machine, but it drops names and "
                    "punctuation turbo gets right.",
        },
        {
            "id": "deepdml/faster-whisper-large-v3-turbo-ct2",
            "recommended": True,
            "label": "Whisper large-v3 turbo (CT2)",
            "nickname": "Whisper large-v3 turbo",
            "size_gb": 1.6,
            "note": "The best value here: large-v3 accuracy at roughly a "
                    "quarter of its decoding cost. Usable on CPU — a laptop "
                    "transcribes faster than real time.",
        },
    ],
    # Embeddings, and the ONE capability here whose two backends read the same
    # bytes — which is why `mlx-embed` has no list of its own and is aliased onto
    # this one (`_SHARED_SUGGESTIONS`). `google/siglip2-*` publishes a single
    # format: a `model.safetensors` beside a `"model_type": "siglip"` config.
    # Transformers reads it, and mlx-embeddings' own SigLIP port reads the same
    # file — there is no `mlx-community` re-upload to prefer and nothing to
    # convert, so pointing both engines at the same repos is not a coincidence to
    # be maintained by hand, it is the format rule this file is keyed on.
    #
    # Keyed under the TORCH row rather than the MLX one because this is the
    # platform-agnostic backend: every machine can resolve here, and only Apple
    # Silicon can resolve to the other.
    #
    # **Why these two and nothing else.** Smallest first, per the module rule,
    # so the base model is what a bare `fused.ai.embed()` loads: 768 dimensions
    # is a comfortable vector to keep a few thousand of in a page, and 1.5GB is
    # the smallest download that gets a genuinely good multilingual encoder.
    # The so400m is the accuracy option at three times the disk and three times
    # the compute per item, and its 1152-dim vectors are a third more storage
    # for whoever is keeping them.
    #
    # **`openai/clip-vit-base-patch32` is deliberately absent**, and it is the
    # entry a future reader is most likely to try to add — it is the famous one,
    # it is 512-dim, and the model itself is about 600MB. The repo is not:
    # it ships TensorFlow, Flax and PyTorch-pickle copies of the same weights
    # beside the safetensors, so the whole-repo download this app's
    # `size_gb` rule measures (and `download_snapshot` actually performs) is
    # 3.6GB — more than twice the SigLIP2 base for a weaker, English-only
    # encoder. mlx-embeddings has no CLIP module either, so it would also be an
    # entry that vanishes when a Mac switches engines. The torch runner still
    # LOADS a CLIP repo a user fetches themselves (`formats.EMBED_MODEL_TYPES`);
    # this file is curation, and curating that download is a different question.
    #
    # Sizes are the Hub's per-file byte sums for the whole snapshot (2026-08-21),
    # rounded to one decimal like every other list here. Both repos are ungated
    # and Apache-2.0. **One line each**, per the rule the transformers text list
    # states.
    "transformers-embed": [
        {
            "id": "google/siglip2-base-patch16-384",
            "recommended": True,
            "label": "SigLIP2 base (384px)",
            "size_gb": 1.5,
            "note": "The smallest here and what a bare embed call loads — "
                    "768-dim vectors, multilingual, and quick enough to index a "
                    "folder of photos.",
        },
        {
            "id": "google/siglip2-so400m-patch14-384",
            "recommended": True,
            "label": "SigLIP2 so400m (384px)",
            "size_gb": 4.6,
            "note": "Noticeably better matches than the base model, for three "
                    "times the download and 1152-dim vectors to store.",
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
#: files. A CUDA build of Diffusers reads byte for byte what the CPU build
#: reads; the split between them is which wheel gets installed, and nothing
#: about which repos are loadable. So their lists must be identical BY
#: CONSTRUCTION. Three copied literals — one per Diffusers row — would be
#: identical only until somebody edited one of them, and the failure that
#: produces is silent on the page: a curated model offered on the CPU engine and
#: missing on the CUDA one, or worse, sized for a different budget. (Transformers
#: was this paragraph's example until D416 withdrew it; Diffusers is the
#: surviving three-row family and the argument is the same one.)
#:
#: An alias also keeps two invariants this file states elsewhere true: every id
#: still appears in exactly ONE list (`capability_of` reads that), and
#: `all_suggested_ids()` is not a pile of copies deduplicated by luck. Left
#: count-free deliberately: the number of literals aliasing avoids is the number
#: of rows in the table, which is a thing that changes.
#:
#: What must NOT be aliased is a runner that reads a different format. That is
#: the whole keying rule, and it is why this table names the specific pairs
#: instead of stripping a suffix off a code.
_SHARED_SUGGESTIONS = {
    "diffusers-image-cuda": "diffusers-image",
    "diffusers-image-rocm": "diffusers-image",
    "llamacpp-text-vulkan": "llamacpp-text",
    # Not a hardware variant of the same runner — a DIFFERENT runner that reads
    # the same repos (see the comment on the embeddings block above). Aliased
    # for the same reason as the pairs above: one list to keep in step rather
    # than two copies drifting apart.
    "mlx-embed": "transformers-embed",
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


def mirror_id(model_id: str) -> str:
    """The repo id to name to the model mirror for `model_id`, or `""` (AI-5m).

    A translation, and it is what keeps the mirror reachable for llama.cpp at
    all. Every other runner's suggested ids ARE repo ids and come back
    unchanged, but `llamacpp-text`'s are bare `.gguf` FILENAMES — one repo
    publishes many quantizations, so the page keys the curation by file — while
    the worker names the recipe's REPO to the mirror
    (`llama_text.download` -> `worker_base.download_file(recipe["repo"], …)`).
    Handed the filename, `mirror.allowed` refuses it against `_REPO_ID`
    (`org/name`) and the whole feature is off for every model in that list,
    silently: no manifest request is a download that looks perfectly normal.

    **The privacy rule is unchanged and this cannot widen it.** `""` for
    anything not in `all_suggested_ids()`, so a model the user found in Discover
    is never named to our distribution, and the lookup is in the CURATED recipe
    table — an uncurated GGUF filename has no row and gets nothing. What the
    worker learns is still the answer for ONE model.

    It lives here rather than in `mirror.py` because that file is imported by a
    runner's interpreter as a bare module with no `fused_render` package on
    `sys.path`: neither this file nor `formats` is reachable from there, and the
    decision has to be made in the server process anyway (see
    `supervisor._mirror_ok`).
    """
    if not model_id or model_id not in all_suggested_ids():
        return ""
    from fused_render.ai.runners import formats

    recipe = formats.GGUF_RECIPES.get(model_id)
    # A suggested id with no recipe row is already a repo id (every other
    # runner's list), so it is handed down as it is. Imported lazily to keep this
    # module free of a runner import at load time.
    return recipe["repo"] if recipe else model_id


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
    Diffusers row shares the CPU row's entries instead of holding its own,
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
    whatever its availability, so a Windows machine — where the MLX row is
    first and unavailable, and the cross-platform row below it is fine — would
    have been told text generation "needs Apple Silicon" while a runner sat
    ready to serve it, and shown four MLX repos it could not load.
    """
    rows = []
    for capability in registry.capabilities():
        runner = _runner_for(capability)
        status = runner.available() if runner else registry.Availability(False, "no runner")
        rows.append(
            {
                "capability": capability,
                "runner": runner.code if runner else None,
                # The backend in words ("Diffusers (CUDA)"), because with
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

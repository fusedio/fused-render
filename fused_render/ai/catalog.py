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
#: `nickname` is the short human name the Playground sidebar shows — the model
#: without its quantization/engine qualifier and WITHOUT ITS PARAMETER COUNT,
#: which the card prints beside it as a chip: "Qwen3.5", not "Qwen3.5 4B". The
#: name and the chip repeating each other was the whole reason to state the
#: rule here. It goes further where the full name is jargon rather than a name
#: — both Liquid rows read "Liquid 2.5" where the labels say "LFM2.5 1.2B
#: Instruct" and "LFM2.5 8B-A1B", because the acronym and the instruct-tuning
#: distinguish nothing on a tab where every text row is chat-tuned.
#:
#: So SIBLINGS OF ONE FAMILY SHARE A NICKNAME on purpose (two "Liquid 2.5", two
#: "Qwen3.5"), and the chip is what tells them apart. That only holds while
#: every colliding row HAS a `params` — a family whose rows would collide with
#: no distinguishing chip must keep the size in the name. What is not a
#: parameter count stays: Whisper's tiny/small/large-v3 and SigLIP2's `base`
#: are the publisher's own variant names, and the chip says 39M beside them.
#: A curated FIELD, never
#: derived by stripping the label's parenthetical at runtime, for the reason
#: `short_label` and `family_label` are fields too (AI-2c): a stripped name
#: is a value nobody owns and no test can see.
#: `params` and `quantization` are the two facts the Playground's stage header
#: prints beside the size — parameter count as the publisher states it ("4B",
#: "8B (~1B active)", "4B effective") and the quantization scheme by its own
#: name. Curated fields for the same AI-2c reason as `nickname`: parsing them
#: out of a repo id breaks on the first ternary, MoE or effective-size entry.
#: Both optional — an entry whose scheme has no honest short name (the plain
#: MLX/CT2 Whisper conversions, the embeddings encoders) omits `quantization`
#: rather than inventing one, and the header omits the line.
#:
#: **`recommended` IS A SECOND AXIS, AND IT IS THE ONLY ONE THE PLAYGROUND
#: READS.** Opt-in, absent almost everywhere: every entry here is a model this
#: app stands behind on the AI Models page, and the marked one is what the
#: Playground offers — recommended-or-downloaded is what its sidebar draws
#: (D425). The two surfaces want different lengths for the same curation. The AI
#: Models page is a place to SHOP: eight text entries from 0.7GB to 20GB is the
#: range someone comparing downloads needs, and its Local tab exists to say what
#: a disk already holds. The Playground is a place to TRY, reached by someone who
#: wants to type a sentence and see what comes back — and a sidebar of eight
#: rows, most of them a multi-gigabyte download away from answering, is a
#: decision where a text box was wanted.
#:
#: **EXACTLY ONE PER LIST — one per capability AND engine, since that is what a
#: list IS — and a test pins the count both ways.** Not "a handful", which is
#: what this started as (three of the eight text entries, two of the four MLX
#: whispers) and which was the owner's call to cut: a shortlist of three is still
#: a comparison, and the reader who came to type a sentence has no way to make
#: it. One row is an ANSWER. It also makes the flag's meaning checkable by eye —
#: "the model to try on this engine" has one correct value per list, where "a
#: good first click" had as many as somebody felt like marking. The floor and the
#: ceiling matter for different reasons: no marked entry leaves the Playground's
#: group empty on a machine with nothing downloaded, and two puts the choice back.
#:
#: **It is NOT "the best one", and it is NOT the default.** `default_for()` is
#: still position 0, still the smallest, and a recommended entry has no bearing
#: on it — see the module docstring on why there is no `default: True` field and
#: why reintroducing one under this name would be the same mistake wearing a new
#: word. What the flag means is "the one to TRY on this engine": strong enough
#: that a first answer is a fair picture of what local inference does here, small
#: enough that the download is not the experience.
#:
#: **Where that lands is per LIST and is nobody's formula.** On the text lists it
#: is neither end — the smallest entry is quick and weaker than every row above
#: it, and a 20GB model can be the best row in its list and still be a poor first
#: click. On `mlx-whisper` it IS the head: a 0.05GB tiny.en downloads in seconds
#: and transcribes clear speech well enough to show what the feature does, which
#: is exactly the "the download is not the experience" half of the rule winning
#: outright. So the marked entry sometimes coincides with `default_for()` and
#: sometimes does not, and that is not a bug either way — the two axes are
#: independent, not opposed, and nothing may derive one from the other. Note that
#: the two whisper lists disagree today (MLX marks tiny.en, CTranslate2 marks
#: turbo); an engine's list is its own editorial judgement, so that is allowed
#: rather than a drift to reconcile.
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
            "params": "1.2B",
            "quantization": "MLX 4-bit",
            "label": "LFM2.5 1.2B Instruct (MLX 4-bit)",
            "nickname": "Liquid 2.5",
            "size_gb": 0.7,
            "note": "The smallest here and the one a bare call loads — quick "
                    "to fetch and to answer, and weaker than every other row.",
        },
        {
            "id": "mlx-community/Qwen3.5-4B-OptiQ-4bit",
            "params": "4B",
            "quantization": "OptiQ 4-bit",
            "recommended": True,
            "label": "Qwen3.5 4B (OptiQ 4-bit)",
            "nickname": "Qwen 3.5",
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
            "params": "8B (~1B active)",
            "quantization": "MLX 4-bit",
            "label": "LFM2.5 8B-A1B (MLX 4-bit)",
            "nickname": "Liquid 2.5",
            "size_gb": 4.9,
            "note": "8B of knowledge answering at about a 1B's speed — a "
                    "mixture of experts, so only a fraction of it runs per "
                    "token.",
        },
        {
            "id": "mlx-community/gemma-4-e4b-it-4bit",
            "params": "4B effective",
            "quantization": "MLX 4-bit",
            "label": "Gemma 4 E4B (4-bit)",
            "nickname": "Gemma 4",
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
            "params": "27B",
            "quantization": "Ternary 2-bit",
            "label": "Ternary Bonsai 27B (MLX 2-bit)",
            "nickname": "Ternary Bonsai",
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
            "params": "9B",
            "quantization": "OptiQ 4-bit",
            "label": "Qwen3.5 9B (OptiQ 4-bit)",
            "nickname": "Qwen 3.5",
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
            "params": "27B",
            "quantization": "MLX 4-bit",
            "label": "Qwen3.8 27B (MLX 4-bit)",
            "nickname": "Qwen 3.8",
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
            "params": "27B",
            "quantization": "OptiQ 4-bit",
            "label": "Qwen3.6 27B (OptiQ 4-bit)",
            "nickname": "Qwen 3.6",
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
            "params": "1.2B",
            "quantization": "GGUF Q4_K_M",
            "label": "LFM2.5 1.2B Instruct (Q4_K_M)",
            "nickname": "Liquid 2.5",
            "size_gb": 0.7,
            "note": "The smallest here and the one a bare call loads — a "
                    "hybrid architecture built for CPU decode, so it answers "
                    "immediately where a 4B thinks.",
        },
        {
            "id": "Qwen3.5-4B-Q4_K_M.gguf",
            "params": "4B",
            "quantization": "GGUF Q4_K_M",
            "recommended": True,
            "label": "Qwen3.5 4B (Q4_K_M)",
            "nickname": "Qwen 3.5",
            "size_gb": 2.7,
            "note": "The first row here strong enough for real work: current-"
                    "gen Qwen, and a fifth of the unquantized 4B's download.",
        },
        {
            "id": "gemma-4-E4B-it-Q4_K_M.gguf",
            "params": "4B effective",
            "quantization": "GGUF Q4_K_M",
            "label": "Gemma 4 E4B (Q4_K_M)",
            "nickname": "Gemma 4",
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
            "params": "8B (~1B active)",
            "quantization": "GGUF Q4_K_M",
            "label": "LFM2.5 8B-A1B (Q4_K_M)",
            "nickname": "Liquid 2.5",
            "size_gb": 5.2,
            "note": "8B of knowledge answering at about a 1B's speed — a "
                    "mixture of experts, so only a fraction of it runs per "
                    "token.",
        },
        {
            "id": "Qwen3.8-27B-UD-Q3_K_XL.gguf",
            "params": "27B",
            "quantization": "GGUF UD-Q3_K_XL",
            "label": "Qwen3.8 27B (UD-Q3_K_XL)",
            "nickname": "Qwen 3.8",
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
            "params": "4B",
            "quantization": "int8 (torchao)",
            "recommended": True,
            "label": "FLUX.2 klein 4B (int8)",
            "nickname": "FLUX.2 klein",
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
            "params": "4B",
            "quantization": "MLX 4-bit",
            "recommended": True,
            "label": "FLUX.2 klein 4B (MLX 4-bit)",
            "nickname": "FLUX.2 klein",
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
            "params": "39M",
            "quantization": "MLX 8-bit",
            "label": "Whisper tiny English (MLX 8-bit)",
            "nickname": "Whisper Tiny en",
            "recommended": True,
            "size_gb": 0.05,
            "note": "The quickest download and decode here, English only — "
                    "fine for a rough draft of clear speech, below small on "
                    "everything else.",
        },
        {
            "id": "mlx-community/whisper-small-mlx",
            "params": "244M",
            "label": "Whisper small (MLX)",
            "nickname": "Whisper Small",
            "size_gb": 0.5,
            "note": "The smallest here, and what a bare transcribe call loads — "
                    "quick, but it drops names and punctuation turbo gets "
                    "right.",
        },
        {
            "id": "mlx-community/whisper-large-v3-turbo",
            "params": "809M",
            "label": "Whisper large-v3 turbo (MLX)",
            "nickname": "Whisper Large-v3 turbo",
            "size_gb": 1.6,
            "note": "The best value here: large-v3 accuracy at a fraction of "
                    "its decoding cost, and faster than real time by a wide "
                    "margin on Metal.",
        },
        {
            "id": "mlx-community/whisper-large-v3-mlx",
            "params": "1.5B",
            "label": "Whisper large-v3 (MLX)",
            "nickname": "Whisper Large-v3",
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
            "params": "39M",
            "label": "Whisper tiny English (CT2)",
            "nickname": "Whisper Tiny en",
            "size_gb": 0.08,
            "note": "The quickest download and decode here, English only — "
                    "fine for a rough draft of clear speech, below small on "
                    "everything else.",
        },
        {
            "id": "Systran/faster-whisper-small",
            "params": "244M",
            "label": "Whisper small (CT2)",
            "nickname": "Whisper Small",
            "size_gb": 0.5,
            "note": "Light enough for an old machine, but it drops names and "
                    "punctuation turbo gets right.",
        },
        {
            "id": "deepdml/faster-whisper-large-v3-turbo-ct2",
            "params": "809M",
            "recommended": True,
            "label": "Whisper large-v3 turbo (CT2)",
            "nickname": "Whisper Large-v3 turbo",
            "size_gb": 1.6,
            "note": "The best value here: large-v3 accuracy at roughly a "
                    "quarter of its decoding cost. Usable on CPU — a laptop "
                    "transcribes faster than real time.",
        },
    ],
    # Embeddings on MLX. TWO SHAPES, and they come from two different kinds of
    # repo for one reason: which account publishes a SINGLE-FORMAT build of the
    # thing this engine reads.
    #
    # * The `google/siglip2-*` upstream safetensors are used where no
    #   `mlx-community` build is BETTER, not as a rule. mlx-embeddings' own
    #   SigLIP port reads `model.safetensors` beside a `"model_type": "siglip"`
    #   config directly, so an upstream repo needs no conversion to work — which
    #   makes the conversion worth taking only when it buys something. For
    #   so400m it buys half the download at the same capability (bf16 against
    #   upstream fp32) and it is taken; for the base row the only conversion on
    #   offer is 224px and 8-bit, a weaker model, and it is not.
    # * The PROSE row is an `mlx-community` conversion, because that is where a
    #   single-format MLX build of a prose encoder exists at all. The upstream
    #   `nomic-ai/*` repos ship an ONNX export and (for some) an OpenVINO copy
    #   beside their safetensors, so this list's whole-snapshot convention would
    #   charge four or five times the weights for one; the conversion ships MLX
    #   safetensors and tokenizer files and nothing else, so the convention costs
    #   nothing there. That is also the re-upload convention every other MLX row
    #   in this file already follows.
    #
    # `size_gb` is the WHOLE snapshot for all three, this file's ordinary
    # convention — each of these repos publishes one format, so the whole-repo
    # pull IS the fetched set. (The ONNX block below documents why it has to
    # deviate.) Hub per-file byte sums, 2026-08-25. All three ungated;
    # Apache-2.0.
    #
    # **The prose row leads, so a Mac's default is a paragraph encoder too** —
    # 0.30 GB against the ONNX default's 0.55, with four times the context.
    #
    # **Cross-engine vector comparability is NOT a goal of this table, and this
    # paragraph used to say the opposite.** It claimed the two engines' rows were
    # matched so a page's vectors would survive an engine switch, and it told the
    # next reader not to "optimize" the bigger row. That was a real constraint
    # once and it has been withdrawn: nobody is asked to move an index between a
    # Mac and a Linux box, and holding both lists to the intersection of what the
    # two engines can read cost real download size for a promise no one wanted.
    # Each engine now curates the best build IT can read.
    #
    # What DOES matter, and is a live hazard, is provenance WITHIN one engine.
    # Two models on the same list can share a dimension — `onnx-embed`'s nomic
    # default and its SigLIP2 base row are both 768 — so vectors indexed under
    # one and queried under the other have the same shape, no error, and
    # different meanings. There is nothing this table can do about that: the fix
    # is that `/api/ai/embed` returns the `model` it used and a caller stores it
    # beside the vectors. `skills/fused-render-ai/SKILL.md` states that rule, and
    # it is the reason the field exists.
    #
    # **Every curated row has a KNOWN PROMPT SCHEME**, the same curation rule the
    # ONNX block states. The prose row needed an explicit
    # `formats.TEXT_EMBED_SCHEMES` entry to get one: its id spells the account
    # `nomicai-`, so the substring heuristic misses it and would have resolved
    # `"none"` — see that table's own comment.
    #
    # **What is deliberately NOT here, and why**, because each of these is a
    # trip a later reader would otherwise take:
    #
    # * `mlx-community/bge-small-en-v1.5-bf16` (0.07 GB) and
    #   `mlx-community/multilingual-e5-small-mlx` (0.25 GB). Both LOAD — `bert`
    #   is in the gate — and both would take position 0 on size, which under the
    #   smallest-first rule makes them the default. One is English-only and both
    #   cap at 512 tokens, so either would make a Mac's default the weakest model
    #   in this list for the job the capability exists for. The identical
    #   structural argument keeps bge off the ONNX list.
    # * `mlx-community/embeddinggemma-300m-bf16` (0.65 GB) and
    #   `mlx-community/Qwen3-Embedding-0.6B-8bit` (0.65 GB). These do NOT load:
    #   `gemma3_text` and `qwen3` are not in `formats.MLX_EMBED_MODEL_TYPES`, and
    #   that set is deliberately narrower than mlx-embeddings' module list — a
    #   decoder-derived embedder wears its CHAT architecture's `model_type`, so
    #   admitting it would route every Qwen3 chat checkpoint on the disk to this
    #   runner. Both schemes are already in `TEXT_EMBED_PROMPTS`, which makes
    #   them look one line from ready; the gate is what blocks them, not the
    #   prompts.
    # * `mlx-community/nomicai-modernbert-embed-base-4bit` (0.09 GB), the same
    #   model a third the size. Quantization costs retrieval quality and costs it
    #   into the VECTOR, where there is no sampling step afterwards to absorb it
    #   — the same class of silent loss the prompt table guards against. bf16 is
    #   the honest default; the 4-bit build is a fine thing to fetch by hand.
    # * `mlx-community/siglip2-base-patch16-224-8bit` (the base row's only
    #   `mlx-community` counterpart). NOT a cheaper build of
    #   `google/siglip2-base-patch16-384`: it is 224px and 8-BIT, a weaker model
    #   on both axes rather than the same one converted. The so400m row above
    #   took the conversion precisely because that one IS the same capability at
    #   half the bytes (bf16 against fp32); this one is not, so the base row
    #   stays upstream. The two decisions are consistent, not contradictory —
    #   the question each time is "same model, cheaper build?" and here the
    #   answer is no.
    "mlx-embed": [
        {
            "id": "mlx-community/nomicai-modernbert-embed-base-bf16",
            "params": "149M",
            "recommended": True,
            "label": "ModernBERT Embed base",
            "nickname": "ModernBERT Embed",
            # 298.04 MB weights + 3.58 MB tokenizer + configs = 0.30 GB, and
            # that IS the whole repo: the conversion ships one format.
            "size_gb": 0.3,
            "note": "The default: 8192 tokens of context and 768-dim vectors, "
                    "so whole documents embed at once — nomic's ModernBERT, "
                    "converted for MLX.",
        },
        {
            "id": "google/siglip2-base-patch16-384",
            "params": "375M",
            "label": "SigLIP2 base (384px)",
            "nickname": "SigLIP2 base",
            "size_gb": 1.5,
            "note": "The one that reads IMAGES — text and photos in one space, "
                    "768-dim, multilingual, but only 64 tokens of text.",
        },
        {
            "id": "mlx-community/siglip2-so400m-patch16-384",
            # 2272.20 MB safetensors + 34.36 MB tokenizer + configs = 2.31 GB,
            # the whole repo (10 files, safetensors only). Hub per-file byte
            # sums, 2026-08-25.
            #
            # Half the upstream row it replaces, and the saving is PRECISION not
            # capability: 2272 MB over ~1.14B parameters is two bytes each, where
            # `google/siglip2-so400m-patch14-384`'s 4.6 GB is four. `config.json`
            # reports no `quantization`, so this is a bf16 conversion and not a
            # quantized build — the distinction that keeps the 4-bit ModernBERT
            # off this list.
            "params": "1.1B",
            "label": "SigLIP2 so400m (384px)",
            "nickname": "SigLIP2 so400m",
            "size_gb": 2.31,
            "note": "Noticeably better matches than the base model, for 1152-dim "
                    "vectors to store instead of 768.",
        },
    ],
    # Embeddings — `onnx-community`'s ONNX Runtime exports of the two
    # `google/siglip2-*` checkpoints above. The safetensors were served here by
    # three withdrawn `transformers-embed*` rows until this branch: a dual
    # encoder is one forward pass over a short sequence or one image, so the
    # compute was never the argument — the WHEEL was, at 0.2 GB on the CPU index
    # and up to 5.9 GB on an accelerated one to run a model whose own weights
    # are 1.5 GB, where `onnxruntime` is tens of megabytes.
    #
    # **Two embeddings blocks, and `mlx-embed` is no longer ALIASED onto this
    # one.** It was, and correctly, while `google/siglip2-*` published a single
    # format both engines read — one list served both by construction, and
    # `_SHARED_SUGGESTIONS` carried the only cross-RUNNER alias in this file.
    # An ONNX export ends that coincidence: mlx-embeddings cannot open a `.onnx`
    # graph and `onnxruntime` cannot open MLX safetensors, so an alias would
    # offer every Mac a download its engine has no reader for. The two engines
    # still produce vectors in the SAME SPACE (`registry.py`'s comment on the
    # `mlx-embed` row), which is why a page can switch between them — the space
    # is shared, the files are not, and this file is keyed on the files.
    #
    # **`size_gb` here is the FETCHED file set, not the whole snapshot, and that
    # is a deliberate exception to this file's own convention.** Everywhere else
    # the figure is the Hub's per-file byte sum for the entire repo, because
    # that is what `download_snapshot` actually pulls — and the CLIP rejection
    # note below rests on exactly that reading. Every repo in this list breaks
    # the assumption behind it: they publish EIGHT quantizations of each tower
    # side by side, so the whole snapshot is 11.42 GB for the base export and
    # 29.5 GB for the so400m, none of which this app fetches.
    # `runners/onnx_embed.py`'s `download()` pins `allow_patterns` to the fp32
    # graphs plus the tokenizer, and the figures below are that set, in DECIMAL
    # GB (this file's own convention — see the LTX rows, which check out exactly
    # against `/1e9`), not GiB:
    #   nomic:   547,310,275 graph + 716,106 tokenizer/pooling config
    #            = 548,026,381 B = 0.55 GB
    #            (whole repo 2.2 GB — eight quantizations plus a safetensors copy)
    #   base:    372,975,112 vision + 1,129,469,657 text + 34,411,767 tokenizer
    #            + configs = 1,536,856,536 B = 1.54 GB
    #   so400m:  1,713,485,119 vision + 2,831,730,610 text (599,026 B of graph
    #            plus its 2,831,131,584 B external-data sidecar) + 34,412,213
    #            tokenizer/configs = 4,579,627,942 B = 4.58 GB
    # Measured against the Hub's own per-file byte sums, 2026-08-25 — and
    # `size_gb` below is each total rounded to one decimal (0.5/1.5/4.6), the
    # same rounding every other row in this file gets. An earlier version of
    # this comment carried a different set of component byte counts for base
    # and so400m, and neither the "1.54 GB"/"4.58 GB" it stated nor the
    # `size_gb` it shipped was that set's DECIMAL total — the decimal figure
    # those bytes actually produce is 1.61/4.80 GB, over the 0.05 tolerance
    # `test_ai_onnx_embed_real_weights.py` allows, which would have failed the
    # gate on a fully correct download and blamed `allow_patterns` for it. It
    # then called the 1.5/4.6 GB agreement with the torch repos' own figures "a
    # coincidence" — with the numbers corrected, it no longer needs that
    # excuse, but the underlying caution still holds: the ONNX export of a
    # tower is a different file from its safetensors, and a future re-export
    # can move either figure independently. `tests/test_ai_onnx_embed_real_weights.py`
    # asserts the on-disk total against these numbers on a machine that really
    # downloaded one, which is the only place the pin can be checked.
    #
    # **THE PROSE ROW LEADS THIS LIST, and that is the user-visible change on
    # this branch.** A bare `fused.ai.embed({texts})` used to load a 64-token
    # caption encoder; it now loads a 2048-token paragraph encoder. That is the
    # point of widening the capability — a SigLIP text tower truncates at 64
    # tokens, so no chunk size turns it into a document encoder, and RAG,
    # clustering and document search were all impossible at any setting. Anyone
    # who indexed a corpus with the old default has to re-index: the two models'
    # vectors are not comparable, and nothing can detect that they are not.
    #
    # Smallest first, per the module rule, which puts the ONE prose row at
    # position 0 — so it is what a bare `fused.ai.embed()` loads on any machine
    # that resolves to this engine. The two SigLIP2 exports sit below it: 768
    # dimensions is a comfortable vector to keep a few thousand of in a page, and
    # the so400m is the accuracy option at three times the disk and three times
    # the compute per item, with 1152-dim vectors that are a third more storage
    # for whoever is keeping them.
    #
    # **A CLIP export is deliberately absent**, and it is the entry a future
    # reader is most likely to try to add — CLIP is the famous one and it is
    # 512-dim. `onnx-community/clip-vit-base-patch32-ONNX` would even avoid the
    # reason the torch curation refused `openai/clip-vit-base-patch32` (that
    # repo ships TensorFlow, Flax and PyTorch-pickle copies beside the
    # safetensors, so the whole-repo pull is 3.6 GB for a weaker, English-only
    # encoder). What survives that change is the other half of the argument:
    # mlx-embeddings has no CLIP module, so a curated CLIP is an entry that
    # vanishes the moment a Mac switches engines. This runner still LOADS a
    # `clip` export a user fetches themselves (`formats.EMBED_MODEL_TYPES`);
    # this file is curation, and curating that download is a different question.
    #
    # Both repos are ungated and Apache-2.0, like their upstreams. **One line
    # each**, per the rule the transformers text list states.
    #
    # **Every row here has a KNOWN PROMPT SCHEME, and that is a curation rule
    # rather than a coincidence** (`formats.TEXT_EMBED_SCHEMES`, asserted by
    # `test_ai_catalog_embeddings.py`). A retrieval encoder instructs a question
    # and a passage differently, and a model whose convention this app does not
    # know embeds both verbatim — which still returns unit-length vectors of the
    # right dimension, just worse ones, with nothing downstream able to tell.
    # Recommending a model we cannot prompt correctly would be recommending a
    # silent accuracy loss.
    #
    # **`sentence-transformers/all-MiniLM-L6-v2` is deliberately absent**, and
    # it is the entry a future reader is most likely to try to add: it is the
    # famous small one, its ONNX export is 90 MB, and it would take position 0
    # on size alone. It is not retrieval-trained — it was distilled on a
    # symmetric sentence-similarity objective, has no query/passage convention
    # to prompt with, and is measurably behind every row below on retrieval. Put
    # differently: the smallest-first rule would make it the DEFAULT, so adding
    # it would mean a bare embed call loading the one model here that is bad at
    # the job the capability exists for.
    #
    # **`BAAI/bge-base-en-v1.5` is absent for the same structural reason, and
    # this one IS a deviation from the plan worth naming.** It is a good
    # retrieval encoder with a known scheme, and its fetched fp32 set is 0.44 GB
    # — SMALLER than the prose row below. Under the smallest-first rule that makes
    # it position 0 and therefore the default, and it is English-only at 512
    # tokens: a worse default than a 2048-token one for a capability whose whole
    # point is paragraphs, and a narrower one than the multilingual SigLIP2 the
    # default is moving away from. Curating it would mean either shipping that
    # default or adding the separate `default:` field this file's module
    # docstring explicitly rejects. It loads fine if a user fetches it
    # themselves, and its scheme is in `TEXT_EMBED_SCHEMES` for exactly that
    # case.
    #
    # **`intfloat/multilingual-e5-small` was curated here and was REMOVED — a
    # scope decision, not a defect.** It is a good encoder: 100 languages in one
    # space at 384-dim, 512 tokens, a known `e5` scheme, and a 0.49 GB fetch. It
    # loads fine when fetched by hand, and its scheme stays in
    # `TEXT_EMBED_SCHEMES` so that a user who does gets prompted correctly. What
    # went was the RECOMMENDATION: one curated prose encoder is the shortlist
    # this capability wants, and a second one that is smaller-but-shorter-context
    # is a comparison put in front of a reader who came to embed a paragraph.
    #
    # **It leaves one fact behind worth keeping, though it is no longer a
    # constraint.** e5-small is `model_type: bert` (a `BertModel` with an XLM-R
    # sentencepiece tokenizer — not `xlm-roberta`, which is the easy thing to
    # assume from its tokenizer files), and `bert` is in
    # `formats.MLX_EMBED_MODEL_TYPES` while nomic's `nomic_bert` is not. So while
    # e5 was curated here it was also the only curated prose model MLX could
    # open, and removing it briefly left the curated prose set ONNX-only.
    #
    # That is no longer the state: the `mlx-embed` block above curates
    # `mlx-community/nomicai-modernbert-embed-base-bf16`, a `modernbert`
    # conversion the gate admits, so each engine has its own curated prose row
    # from its own kind of repo. Nothing here needs to be re-added to close a
    # gap — and a row added back to THIS list would be an ONNX row, which is not
    # what a Mac reads.
    "onnx-embed": [
        {
            "id": "nomic-ai/nomic-embed-text-v1.5",
            "params": "137M",
            "recommended": True,
            "label": "Nomic Embed Text v1.5",
            "nickname": "Nomic Embed",
            # onnx/model.onnx 547.31 MB + tokenizer.json + vocab.txt + the
            # pooling and tokenizer configs = 0.55 GB. The whole repo is 2.2 GB
            # (eight quantizations plus a safetensors copy this engine cannot
            # open), which is the deviation the block header documents.
            "size_gb": 0.5,
            "note": "The default: 2048 tokens of context and 768-dim vectors, "
                    "so a whole paragraph embeds at once — what makes document "
                    "search and RAG possible at all.",
        },
        {
            "id": "onnx-community/siglip2-base-patch16-384-ONNX",
            "params": "375M",
            "label": "SigLIP2 base (384px)",
            "nickname": "SigLIP2 base",
            "size_gb": 1.5,
            "note": "The one that reads IMAGES — text and photos in one space, "
                    "768-dim, multilingual, but only 64 tokens of text.",
        },
        {
            "id": "onnx-community/siglip2-so400m-patch14-384-ONNX",
            "params": "1.1B",
            "label": "SigLIP2 so400m (384px)",
            "nickname": "SigLIP2 so400m",
            "size_gb": 4.6,
            "note": "Noticeably better matches than the base model, for three "
                    "times the download and 1152-dim vectors to store.",
        },
    ],
    # LTX-2.3 on MLX, through `ltx-2-mlx` — the accessible video engine (see
    # the plan's "ltx-2-mlx, not mlx-video" decision) that a bare
    # `fused.ai.video()` now reaches first (`registry.py`'s ordering).
    #
    # **`size_gb` is every byte BOTH downloads fetch, per this file's own
    # rule** — the weights repo's curated file set (`ltx_video/worker.py`'s
    # `download`, which picks exactly ONE transformer file out of the two
    # this repo ships side by side — see that module's docstring for why a
    # bare glob would have silently fetched both) PLUS the whole Gemma-3
    # text encoder repo neither tier can render without. Measured against
    # the Hub's own per-file byte sums, 2026-08-23:
    #   int4: 20,479,309,067 B weights + 8,068,021,302 B gemma = 28.5 GB
    #   int8: 29,754,496,331 B weights + 8,068,021,302 B gemma = 37.8 GB
    # (the plan's own estimate — ~29.9/~39.2 GB — was written before the
    # worker's final pattern set excluded the second, unused transformer
    # copy; these are the re-derived figures the plan itself calls for).
    #
    # **`resident_gb` (SPEC AI-22, D526) is the declared rung `fit.py`'s own
    # module docstring names THIS row as the motivating case for**:
    # `DistilledPipeline(low_memory=True)` frees the transformer and the
    # Gemma-3 text encoder between stages, so the true resident PEAK is one
    # stage — the larger of the two components above — never their sum.
    # `size_gb` is correct as the download figure (every byte both repos
    # fetch, per this file's own rule); `resident_gb` is `max(weights bytes,
    # gemma bytes) / 1e9`, rounded to the same one decimal `size_gb` uses,
    # computed from the exact figures stated above rather than a fresh
    # guess — real evidence this rung requires, not an estimate invented
    # for it. The weights component is the larger one at BOTH tiers (gemma
    # is a fixed 8,068,021,302 B either way), so `resident_gb` here is
    # simply the weights half of the sum above: int4 20.5, int8 29.8.
    "ltx-video": [
        {
            "id": "dgrauet/ltx-2.3-mlx-q4",
            "recommended": True,
            "label": "LTX-2.3 int4 distilled",
            "nickname": "LTX-2.3",
            # Both video tiers shipped with no `params` and no `quantization`,
            # so the playground's model card had two of its four facts blank
            # beside every text and image entry. Filled from the PUBLISHERS,
            # per this file's AI-2c rule that these are strings somebody owns:
            # the upstream weights are `ltx-2.3-22b-distilled` (Lightricks/LTX-2.3,
            # a 22B DiT), and the conversion's own card states "Int4
            # quantization (group_size 64, transformer block Linear weights
            # only)" — mlx-forge, not mlx-community, but the same scheme the
            # other MLX rows in this file name as "MLX 4-bit", and the column is
            # read down. The group size and the linear-weights-only scope stay
            # here rather than in the field: they are true and they are not what
            # a reader comparing two rows is asking.
            "params": "22B",
            "quantization": "MLX 4-bit",
            "size_gb": 28.5,
            # See this list's own header comment for where 20.5 comes from —
            # the larger of the two components DistilledPipeline ever holds
            # resident at once (20,479,309,067 B weights, the larger of the
            # pair), not the 28.5 GB sum both downloads fetch.
            "resident_gb": 20.5,
            "note": "Text-to-video with audio, 8 denoising steps, on a "
                    "16 GB+ Mac. Diverges from the bf16 sample at this "
                    "tier — upstream's own ladder calls it a different "
                    "valid composition, not a degraded one — and ships "
                    "under the LTX-2 Community License: it carries a "
                    "revenue threshold and a non-compete "
                    "(Attachment A, item 20).",
            # `DistilledPipeline` runs a fixed 8-step stage-1 schedule
            # (`ltx_video/worker.py`'s own default, and `registry.VIDEO_
            # TRAITS["ltx-video"].default_steps`) — a property of the
            # PIPELINE both tiers share, not of the quantization. Named
            # explicitly here too, the same "one repo, one curator's hint"
            # shape the image entries above use for their own step-distilled
            # models, rather than relying only on the engine-level fallback
            # (`registry.video_traits_for`) agreeing with it by construction.
            "defaults": {"steps": 8},
        },
        {
            "id": "dgrauet/ltx-2.3-mlx-q8",
            "label": "LTX-2.3 int8 distilled",
            "nickname": "LTX-2.3",
            # Same 22B upstream, the other tier of the same conversion ("Int8
            # quantization (group_size 64, transformer block Linear weights
            # only)") — see the int4 entry for where both strings come from.
            "params": "22B",
            "quantization": "MLX 8-bit",
            "size_gb": 37.8,
            # See the int4 entry above (and this list's header comment) —
            # 29.8 is the larger single-stage component (29,754,496,331 B
            # weights), not the 37.8 GB sum both downloads fetch.
            "resident_gb": 29.8,
            "note": "The same LTX-2 Community License, for the tier that "
                    "reproduces the bf16 sample — a 32 GB+ machine and "
                    "roughly 9 GB more download than the int4 default.",
            # Same pipeline, same schedule — see the int4 entry's own comment.
            "defaults": {"steps": 8},
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
    # Hardware variants of `onnx-embed` — same repos, same `onnx/` graphs, a
    # different execution provider — for the identical reason. The four ONNX
    # builds differ in which `onnxruntime*` distribution is installed and in
    # nothing about which files are loadable, so their lists must be identical
    # BY CONSTRUCTION rather than by nobody having edited one of them yet.
    "onnx-embed-directml": "onnx-embed",
    "onnx-embed-cuda": "onnx-embed",
    "onnx-embed-rocm": "onnx-embed",
    # **`mlx-embed` used to be aliased here and deliberately is not any more.**
    # It was the one entry in this table that aliased a DIFFERENT RUNNER rather
    # than a hardware variant, and it was correct while `google/siglip2-*`
    # published one format that both the torch runner and mlx-embeddings read.
    # `onnx-embed` reads a graph export instead, which MLX has no reader for, so
    # the coincidence that justified the alias is gone — see the embeddings
    # block above, and `test_ai_catalog_embeddings.py`, which pins the absence.
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
    """The curated list for a runner, following the hardware-variant alias,
    with the user's `~/.fused-render/models.json` overlay merged in
    (`catalog_overlay.apply`, SPEC AI-25) — an overlay row whose `id`
    matches a built-in one overrides it, a new `id` appends.

    **The overlay is looked up under the same RESOLVED key `builtin` is**
    (code review finding 3) — `_SHARED_SUGGESTIONS.get(code, code)`, not the
    raw `code` a caller passed in. `for_runner("llamacpp-text-vulkan")`
    reads the built-in `llamacpp-text` list; an overlay keyed by `code`
    alone would look for `models.json["llamacpp-text-vulkan"]` and find
    nothing there even when the user wrote the entry under
    `"llamacpp-text"` (the only name that appears anywhere in the built-in
    curation a user could plausibly have copied), silently doing nothing on
    every Vulkan/CUDA/ROCm/DirectML machine — exactly the scenario
    `catalog_overlay.py`'s own module docstring gives as the motivating
    example. Every hardware variant of a runner shares ONE overlay
    namespace, matching how they already share one built-in list.

    A copy of the list, as it always was — callers append to it (the router's
    cached-repo union does) and must not be editing the curation.
    """
    from fused_render.ai import catalog_overlay

    canonical = _SHARED_SUGGESTIONS.get(code, code)
    builtin = SUGGESTIONS.get(canonical, ())
    return catalog_overlay.apply(canonical, list(builtin))


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
    """Every suggested repo id, across every runner.

    **Not the page's checkmarks — that is what this docstring used to say and it
    was stale enough to be misleading.** The AI Models page ticks a card from
    `/api/ai/catalog`'s payload (`_catalog_with_downloads` -> `for_capability`),
    which holds only the RESOLVING runner's list, so the tick is already
    engine-aware and a Mac-only row does not wear it on Linux. The one live
    caller is `mirror_id`'s privacy gate.

    Deliberately EVERY runner's all the same, and for that caller's reason: the
    gate asks "is this a model we ourselves recommend", so that a repo the user
    found in Discover is never named to our own distribution. Narrowing it to the
    resolvable runner would silently turn the mirror off for every model on the
    other engine's list.

    `runners_offering` is the narrow companion for callers that need to know
    WHICH engine an id belongs to — see its docstring for the difference.
    """
    return {entry["id"] for entries in SUGGESTIONS.values() for entry in entries}


def runners_offering(model_id: str) -> tuple[str, ...]:
    """Every runner code whose curated list names `model_id`, in registry order.

    **The NARROW companion to `all_suggested_ids()`, and the two answer opposite
    questions on purpose.** That one is deliberately every runner's ids, because
    it backs the "is this on my disk" checkmark and a Mac model cached from a
    previous life should still tick. This one says WHICH engine a curated id
    belongs to, which is what a caller needs before offering to fetch or load it
    — and the difference between them is exactly the gap
    `google/siglip2-so400m-patch14-384` fell into.

    Empty for an uncurated repo, which is not the same answer as "no engine
    reads it": nobody here has an opinion about a repo the user found
    themselves, and `formats.loaders()` is what judges those. Callers must treat
    empty as "no information" rather than as a refusal.

    Resolves aliases (`_SHARED_SUGGESTIONS`) the same way `for_runner` does, so a
    hardware variant reports as offering everything its family's list holds.
    """
    codes = []
    for runner in registry.all_runners():
        entries = SUGGESTIONS.get(_SHARED_SUGGESTIONS.get(runner.code, runner.code))
        if entries and any(entry["id"] == model_id for entry in entries):
            codes.append(runner.code)
    return tuple(codes)


#: The same MODEL, curated for a different engine — id to id.
#:
#: **This table exists because the torch removal orphaned exactly these two ids
#: on every machine that is not a Mac**, and for no more general reason than
#: that. Both were curated for `transformers-embed`, which every platform could
#: run; with that engine gone their only curated home is `mlx-embed`, so a Linux
#: or Windows user who had already downloaded one is holding a snapshot no
#: engine available to them can read. The ONNX exports below are the same
#: checkpoints in the format the engine they DO have opens, which is what makes
#: the refusal a "fetch this instead" rather than a shrug.
#:
#: **Written out rather than derived, deliberately.** The mapping is
#: `google/X` -> `onnx-community/X-ONNX` for both rows, and that is a
#: coincidence of how one account names its conversions — not a rule. Inferring
#: it from string munging would invent ids for every other `google/*` repo on
#: the Hub and confidently recommend downloads that do not exist. Two lines of
#: data cannot do that.
#:
#: Not a migration to run, and nothing is rewritten on disk: the snapshot stays
#: exactly where it is, still perfectly loadable the moment the user opens the
#: same cache on a Mac. `counterpart_for` below is what checks that a
#: counterpart is REALLY curated for the engine being offered it, so this table
#: cannot outlive the rows it points at.
#: **The so400m pair was here and was REMOVED, because it stopped being a pair.**
#: The MLX so400m row is now `mlx-community/siglip2-so400m-patch16-384` and the
#: ONNX one is still patch14 — a genuinely different checkpoint, not the same
#: weights in another format. This table's entire claim is "the SAME model in the
#: format this machine's engine does read", and offering a patch14 export as the
#: counterpart of a patch16 conversion would break exactly the promise the
#: sentence makes. A stranded so400m snapshot now falls to `engine_gap`'s
#: no-counterpart branch, which names the engine that serves embeddings here and
#: recommends nothing — the honest answer.
#:
#: The base row stays: `google/siglip2-base-patch16-384` and
#: `onnx-community/siglip2-base-patch16-384-ONNX` are patch16 both, one export of
#: one checkpoint, which is the only relationship this table is allowed to
#: assert.
COUNTERPART_IDS = {
    "google/siglip2-base-patch16-384":
        "onnx-community/siglip2-base-patch16-384-ONNX",
}


def counterpart_for(model_id: str, runner_code: str) -> str | None:
    """`model_id`'s equivalent in `runner_code`'s own curated list, or None.

    Checked against the list rather than returned from `COUNTERPART_IDS`
    directly: a table entry pointing at a row somebody later removed would
    otherwise have this function recommending a download nothing curates. The
    table proposes; the curation decides.
    """
    counterpart = COUNTERPART_IDS.get(model_id)
    if not counterpart:
        return None
    return counterpart if counterpart in {
        entry["id"] for entry in for_runner(runner_code)} else None


def engine_gap(model_id: str) -> dict | None:
    """Why no engine available here can serve `model_id`, or None when one can.

    **The one place this question is answered, because two surfaces ask it and
    they must not disagree**: the Local tab's card (which decides whether to
    offer a resume, and what sentence to print) and the load/download/embed
    routes (which refuse a request made anyway — a stale URL, a seeded app
    param, a stored pref). A card that offered an action the route then refused
    would be the failure this function exists to remove.

    Answers from the CURATION, which is what makes it work on a snapshot whose
    format cannot be read yet. `formats.loaders()` needs weights on disk, so a
    PARTIALLY downloaded repo has no format evidence at all and cannot be judged
    that way — and download is the one operation a format gate structurally
    cannot guard, since fetching the files is the whole point of it. For a
    curated id the curation knows the answer before a byte arrives.

    None in three cases, and each is a deliberate "no information" rather than
    an approval:

    * an UNCURATED id — nobody here has an opinion about a repo the user found
      themselves, and the runner's own format check is the right judge;
    * a curated id whose engine IS the one serving its capability here;
    * a curated id offered by a runner that is available, even if not the one
      currently selected — that is the Engines tab's business, and
      `hub_cache._engine` already prints "switch it on the Engines tab" for it.

    The dict carries the engines that DO curate it, whether any of them could
    run here at all, the counterpart to fetch instead where there is one, and
    the finished sentence. `registry.unavailable_reason` supplies the
    "cannot run here" half so this reads in the app's existing vocabulary
    rather than a parallel phrasing invented here.
    """
    codes = runners_offering(model_id)
    if not codes:
        return None
    offering = [registry.by_code(code) for code in codes]
    offering = [runner for runner in offering if runner is not None]
    if not offering:
        return None
    capability = offering[0].capability
    serving = registry.for_capability(capability)
    if serving is not None and any(r.code == serving.code for r in offering):
        return None
    # Available-but-not-selected is not a gap: switching engines fixes it, and
    # that is a sentence the card already knows how to print.
    if any(runner.available().ok for runner in offering):
        return None

    names = " or ".join(dict.fromkeys(runner.short for runner in offering))
    # The registry's own words for why the engine that reads this cannot run,
    # taken from the first offering runner rather than composed here.
    why = offering[0].available().reason or registry.unavailable_reason(capability)
    counterpart = counterpart_for(model_id, serving.code) if serving else None

    # A COLON rather than a dash before `why`: the registry's own reasons
    # already contain an em-dash ("needs Apple Silicon — MLX runs on Metal
    # only"), and nesting one inside another reads as a broken sentence.
    reason = (
        f"{model_id} is only readable by {names}, which cannot run on this "
        f"machine: {why}." if why else
        f"{model_id} is only readable by {names}, which cannot run on this "
        f"machine.")
    if counterpart:
        reason += (
            f" The same model in the format this machine's engine does read is "
            f"{counterpart} — fetch that instead. Nothing is deleted: this "
            f"snapshot stays on disk and still loads on a machine that can run "
            f"{names}.")
    elif serving is not None:
        reason += (
            f" {serving.short} is what serves {capability} here, and it does "
            f"not read this model's files.")
    return {
        "engines": tuple(runner.code for runner in offering),
        "capability": capability,
        "serving": serving.code if serving is not None else None,
        "counterpart": counterpart,
        "reason": reason,
    }


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
                #
                # Gated on `status.ok`, unlike `runnerLabel`/`runnerShortLabel`
                # above: `_runner_for`'s fallback hands back the first
                # REGISTERED runner even when it cannot run here (so an
                # unservable capability still names what it would be and why
                # not), but the note describes what using that backend is
                # LIKE — and a backend this machine cannot use has nothing to
                # be like yet. `reason` already says why not; a note on top of
                # it would describe an experience nobody here is having.
                "runnerNote": (runner.note or None) if runner and status.ok else None,
                "available": status.ok,
                "reason": status.reason or None,
                # Gated on `status.ok`, which every capability before video
                # generation made irrelevant: each of them has an
                # "everywhere" row, so SOME runner was always available and
                # this was always non-null in practice. Video is the first
                # capability that can be genuinely unservable here — its
                # one engine is MLX, with no fallback — and a caller with no model
                # specified must be told there is nothing to load, not handed
                # an id `default_for()` would then fail to load anyway. The
                # suggestion LIST still shows the entry either way (`models`
                # below) — a repo worth downloading once you have a Mac is
                # still worth listing today, per `_runner_for`'s own fallback.
                "default": for_capability(capability)[0]["id"]
                if status.ok and for_capability(capability) else None,
                "models": for_capability(capability),
                # Absent for every capability but video generation — the
                # same "absent rather than empty" shape `registry.VIDEO_
                # TRAITS` itself uses. Video is the first (only) capability
                # whose REQUEST SHAPE varies by which runner resolved (the
                # frame grid, canvas and step defaults — `registry.
                # VideoTraits`), and the Playground's own frame/canvas/step
                # sliders need those numbers to draw a control that agrees
                # with what the server will actually do — Task 5 of the
                # LTX-2.3 plan de-hardcoded the SERVER's copy of the frame
                # grid and left the client statically wired to it, which is
                # the defect this field exists to close.
                "videoTraits": (
                    _video_traits_payload(runner.code)
                    if capability == registry.VIDEO_GENERATION and runner is not None
                    else None
                ),
            }
        )
    return rows


def _video_traits_payload(runner_code: str) -> dict:
    """`registry.VideoTraits`, in the shape `describe()`'s payload wants —
    absolute frame bounds rather than the `(base, step, n)` the server's own
    grid math uses internally, because a slider draws a min/max/step/value,
    not an `n` window it would have to re-derive the same arithmetic to get.
    """
    traits = registry.video_traits_for(runner_code)
    min_frames, max_frames = registry.video_frame_bounds(traits)
    default_frames = (traits.frames_base
                      + traits.frames_step * traits.default_frames_n)
    return {
        "framesBase": traits.frames_base,
        "framesStep": traits.frames_step,
        "minFrames": min_frames,
        "maxFrames": max_frames,
        "defaultFrames": default_frames,
        "defaultWidth": traits.default_width,
        "defaultHeight": traits.default_height,
        "defaultSteps": traits.default_steps,
    }

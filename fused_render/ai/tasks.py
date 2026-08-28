"""What a model DOES, in Hugging Face's own vocabulary, and whether we run it.

**One table, keyed by the Hub's `pipeline_tag`.** This module replaces three
tables that used to sit apart — a tag-to-prose map in `hub_cache.py`, a
prose-to-capability map in `registry.py`, and a ruled-out list beside it — and
the split between them is what let two bugs through:

* **The vocabulary was OPEN.** The tag-to-prose step ended in
  `tag.replace("-", " ")`, so any string the Hub invented became a task label
  nobody had classified. `SymphonyGen/SymphonyGen` (`pipeline_tag:
  reinforcement-learning`, four `.pt` files, no `config.json`) came out as a
  label in neither table, fell through the format fallback, and was offered in
  the Playground's TEXT section with a Load button aimed at mlx-lm.
* **"Ruled out" and "never heard of" were the SAME answer** — both `None` — so
  no caller could tell a task we have decided not to serve from one nobody has
  looked at. `classify()` returns three states instead, and that distinction is
  what stops a ruled-out task from being rescued by a format guess.

The tag list is **vendored from `@huggingface/tasks`** (huggingface.js,
`packages/tasks/src/pipelines.ts`, `PIPELINE_DATA`) — the same table that routes
the Hub's own inference widgets, and the only authoritative enumeration of the
vocabulary; no Python package ships it. Vendored rather than fetched because
classification runs OFFLINE: `hub_cache.cached_capability` reads a snapshot
directory and makes no network call, and a page that needed the Hub to say what
a downloaded model is would stop working on a plane. A tag we have not vendored
is not an error — it is `UNKNOWN`, which is a state with its own behaviour (see
below) rather than a fallback into somebody's capability.

`tests/test_ai_tasks.py` pins that every vendored tag is classified, and
`scripts/` has no generator: the list is small, changes a few times a year, and
a hand-maintained table is reviewable in a way a generated one is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from fused_render.ai.registry import (
    EMBEDDINGS,
    IMAGE_GENERATION,
    SPEECH_TO_TEXT,
    TEXT_GENERATION,
    VIDEO_GENERATION,
)

#: A task this machine can serve: some runner's capability covers it.
SUPPORTED = "supported"
#: A task we RECOGNISE and deliberately do not serve. Video generation, speech
#: synthesis, tabular regression: real jobs, no runner in this cut. The state
#: exists so a page can SAY that, which is the difference between "we do not
#: support this" and a model quietly vanishing from every list.
NO_RUNNER = "no-runner"
#: A tag not in this table, or no evidence of a task at all. Never mapped to a
#: capability, however suggestive the file layout: an unrecognised task is the
#: one case where a format guess is most likely to be wrong and most likely to
#: be believed.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Task:
    """One row of the Hub's pipeline vocabulary, and our answer about it."""

    #: The Hub's own `pipeline_tag` value. The key everything joins on — a
    #: search filter, a card's label, a cached repo's classification — because
    #: it is the one spelling both sides of the app can be sure of.
    tag: str
    #: The words a person reads. Ours, not the tag: "image + text to text"
    #: rather than the unreadable "image text to text" that hyphens-to-spaces
    #: produces, and "speech recognition" for a tag whose own spelling
    #: ("automatic-speech-recognition") is longer than a card has room for.
    label: str
    #: `@huggingface/tasks`' own grouping, carried so a menu can be ordered and
    #: an unsupported answer can name the family it belongs to.
    modality: str
    #: The capability that would load it, or None when nothing here does.
    capability: str | None = None
    #: One sentence explaining the task itself, for a glossary (HS-7). Written
    #: for someone who has not met the term, not for someone who has.
    help: str = ""
    #: Why we do not serve it, when we do not — a sentence a page can print.
    #: Empty for a supported task, and defaulted for an unsupported one whose
    #: reason is simply that nobody has written the runner (see `reason()`).
    note: str = ""


def _t(tag, label, modality, capability=None, help="", note="") -> Task:
    return Task(tag, label, modality, capability, help, note)


#: Every `pipeline_tag` the Hub serves today, in MENU order: text first, then
#: the multimodal rows that read as text jobs, then vision, audio, and the
#: families this app has no business in. Order is load-bearing — the Discover
#: tab's filter menu is this table filtered, and a menu ordered by an enum's
#: accident reads as random.
#:
#: **Seven rows carry a capability and the other fifty do not**, which is the
#: honest shape of a desktop app whose five capabilities are each reachable by
#: more than one Hub tag (a chat model answers both `text-generation` and,
#: with a vision tower, `image-text-to-text`/`visual-question-answering`).
#: The interesting ones are commented; the rest are ordinary "no runner for
#: this" rows.
_TASKS: tuple[Task, ...] = (
    # ---------------------------------------------------------------- text
    _t("text-generation", "text generation", "nlp", TEXT_GENERATION,
       "Continues or answers a prompt in text — chat models, code models, completion."),
    # **A vision-language checkpoint IS the causal LM the text runner loads**
    # when you only give it text — Qwen3.5 and gemma-4 ship as one checkpoint
    # with a vision tower attached, including every entry in this app's own MLX
    # catalog. Leaving this row unmapped once took the Load button off the
    # models the app was recommending on the next tab over.
    #
    # **This comment used to explain why the row is mapped despite the tower
    # being ignored — mlx-lm loaded the language half and never touched the
    # rest. That is no longer true, and it is the interesting kind of
    # out-of-date: the runner switched to mlx-vlm (`mlx_text/worker.py`),
    # which loads the SAME checkpoint `lazy=True` and reads the tower on
    # demand, when a request actually attaches an image (`_accepts_image`,
    # `ai_runtime.py`). So this row is no longer "map it anyway so the Load
    # button isn't hidden" — it is a real capability, image and all.
    _t("image-text-to-text", "image + text to text", "multimodal", TEXT_GENERATION,
       "Takes an image AND a prompt, answers in text — describing a picture, reading a chart, visual chat."),
    _t("text-to-image", "text to image", "multimodal", IMAGE_GENERATION,
       "Generates a picture from a written description."),
    _t("automatic-speech-recognition", "speech recognition", "audio", SPEECH_TO_TEXT,
       "Transcribes speech in audio into text."),
    # **This tag IS the embedding capability**, and it is the pairing a reader
    # is most likely to think is a mistake. It is what every SigLIP, SigLIP2 and
    # CLIP repo carries — both entries in this app's embedding catalog included
    # — and what it describes is a DUAL ENCODER: the "classification" is done by
    # embedding the labels, embedding the image, and comparing, which is exactly
    # the two calls the embedding runners expose.
    _t("zero-shot-image-classification", "zero-shot image classification", "cv", EMBEDDINGS,
       "Says what an image shows, against labels you supply at the time rather than a fixed set."),

    # ------------------------------------------------------ text, and SERVED
    # **The two rows the embedding capability finally does claim.** They were
    # `None` with a withheld reason for as long as the capability meant dual
    # encoders only: what wears these tags is a sentence-transformers
    # checkpoint — a text encoder plus a pooling configuration, with no vision
    # tower — and the runners of the day had nothing but
    # `get_text_features`/`get_image_features` to call on it, so mapping them
    # would have put a Load button on `sentence-transformers/all-MiniLM-L6-v2`
    # and offered a download that then refused. That note ended "when it ships,
    # these two move", and this is the change where it shipped: both embedding
    # runners now load a text-only encoder (`formats.TEXT_EMBED_MODEL_TYPES`,
    # `runners/onnx_embed.py`'s prose path) and pool it correctly.
    #
    # **So EMBEDDINGS is now the one capability reached by many tags, and that
    # is worth saying out loud**: `zero-shot-image-classification` for a dual
    # encoder, and these two for a prose one. Every other capability here is
    # one-tag-to-one-capability, or close to it; this one covers two genuinely
    # different model shapes under one resident slot, which is the whole point
    # of `_accepts_paths` gating the image half per model rather than per
    # capability.
    _t("feature-extraction", "embeddings", "multimodal", EMBEDDINGS,
       "Turns text into vectors, so things can be compared or searched by meaning."),
    _t("sentence-similarity", "sentence embeddings", "nlp", EMBEDDINGS,
       "Turns sentences into vectors, so similar sentences land near each other — search, clustering, RAG."),

    # ------------------------------------------------- text, nothing serves it
    _t("summarization", "summarization", "nlp", None,
       "Shortens a long text into its main points.",
       "A chat model does this from a prompt — no separate runner ships for it."),
    _t("translation", "translation", "nlp", None,
       "Translates text from one language into another.",
       "A chat model does this from a prompt — no separate runner ships for it."),
    _t("fill-mask", "fill mask", "nlp", None,
       "Fills in blanked-out words in a sentence. Mostly a building block for other models."),
    _t("text-classification", "text classification", "nlp", None,
       "Sorts a piece of text into categories — sentiment, topic, spam."),
    _t("token-classification", "token classification", "nlp", None,
       "Labels each word in a sentence — named entities, parts of speech."),
    _t("question-answering", "question answering", "nlp", None,
       "Finds the span of a supplied document that answers a question."),
    _t("zero-shot-classification", "zero-shot text classification", "nlp", None,
       "Sorts text into categories you supply at the time rather than a fixed set."),
    _t("multiple-choice", "multiple choice", "nlp", None,
       "Picks the best of several supplied answers."),
    _t("text-ranking", "text ranking", "nlp", None,
       "Reorders search results by how well each one answers a query."),
    _t("text-retrieval", "text retrieval", "nlp", None,
       "Finds the documents in a collection that answer a query."),
    _t("table-question-answering", "table question answering", "nlp", None,
       "Answers a question by reading a table."),

    # ------------------------------------------------------------------ vision
    _t("image-to-text", "image to text", "multimodal", None,
       "Describes an image in words — captioning, OCR.",
       "The vision-language models here load as chat models (image + text to text); "
       "a caption-only checkpoint has no runner."),
    _t("image-to-image", "image to image", "cv", None,
       "Turns one picture into another — upscaling, restyling, inpainting."),
    _t("image-text-to-image", "image + text to image", "multimodal", None,
       "Edits a picture from a written instruction."),
    _t("unconditional-image-generation", "unconditional image generation", "cv", None,
       "Generates a picture from nothing in particular — no prompt.",
       "The image runners here generate FROM a prompt; an unconditional pipeline has "
       "nothing for the prompt box to do."),
    _t("image-classification", "image classification", "cv", None,
       "Says what an image is a picture of, from a fixed set of labels."),
    _t("image-segmentation", "image segmentation", "cv", None,
       "Marks which pixels belong to which object."),
    _t("object-detection", "object detection", "cv", None,
       "Finds objects in an image and boxes them."),
    _t("zero-shot-object-detection", "zero-shot object detection", "cv", None,
       "Finds objects named by labels you supply at the time."),
    _t("depth-estimation", "depth estimation", "cv", None,
       "Estimates how far away each part of an image is."),
    _t("mask-generation", "mask generation", "cv", None,
       "Outlines every distinct thing in an image, unprompted."),
    _t("keypoint-detection", "keypoint detection", "cv", None,
       "Finds landmark points — joints on a body, corners on an object."),
    # **The neighbour that did NOT move when `feature-extraction` and
    # `sentence-similarity` did, and the difference is a tower rather than a
    # policy.** Those two wear a text encoder, which both embedding runners now
    # load. This one wears an IMAGE-ONLY encoder — DINOv2 and DINOv3 are the
    # models people download for it — and an image-only encoder has no text
    # tower at all, so there is nothing here that can open one: the dual path
    # wants both towers and the prose path wants a tokenizer. Not "a use case we
    # have not got to" but a third model shape, and admitting it would need a
    # third load path.
    _t("image-feature-extraction", "image embeddings", "cv", None,
       "Turns an image into a vector, so images can be compared or searched.",
       "The embedding runners here load a dual encoder (images AND text) or a text "
       "encoder; an image-only encoder has no text tower for either to read."),
    # The Hub's OTHER tag for the same checkpoints `image-text-to-text` above
    # already maps: a repo carrying this tag instead is still the identical
    # unified vision-language chat model, and mlx-vlm answers a question about
    # a picture exactly the way it describes one — there is no separate
    # VQA-only architecture here that needs its own row. Used to be `None` with
    # a note that a VQA-only checkpoint has no runner; that was true of the
    # architecture this tag is COMMONLY seen on, but the note was reachable
    # from a repo that actually carries this exact tag and IS one of the
    # catalog's own recommendations, which read as "no runner" for a model
    # this app runs today.
    _t("visual-question-answering", "visual question answering", "multimodal", TEXT_GENERATION,
       "Answers a question about a picture."),
    _t("document-question-answering", "document question answering", "multimodal", None,
       "Answers a question by reading a scanned document."),
    _t("visual-document-retrieval", "visual document retrieval", "multimodal", None,
       "Finds the page of a document that answers a query, from the page images."),
    _t("text-to-3d", "text to 3D", "cv", None,
       "Generates a 3D object from a written description."),
    _t("image-to-3d", "image to 3D", "cv", None,
       "Generates a 3D object from a picture."),

    # ------------------------------------------------------------------- video
    # `ltx-video` (LTX-2.3 on `ltx-2-mlx`) serves this one — PROMPT
    # only, no reference image, so its two image-conditioned siblings just
    # below stay unmapped rather than folding onto the same capability.
    _t("text-to-video", "video generation", "multimodal", VIDEO_GENERATION,
       "Generates video frames, usually from a description or a still image."),
    _t("image-to-video", "image to video", "cv", None,
       "Animates a still picture into a clip.",
       "Video generation needs far more memory and time than the image runners here are "
       "built for; no runner ships for it."),
    _t("image-text-to-video", "image + text to video", "multimodal", None,
       "Animates a still picture, directed by a written instruction.",
       "Video generation needs far more memory and time than the image runners here are "
       "built for; no runner ships for it."),
    _t("video-to-video", "video to video", "cv", None,
       "Restyles or upscales an existing clip."),
    _t("video-classification", "video classification", "cv", None,
       "Says what is happening in a clip, from a fixed set of labels."),
    _t("video-text-to-text", "video + text to text", "multimodal", None,
       "Answers questions about a clip.",
       "mlx-lm loads the language tower of an image-and-text checkpoint, but nothing here "
       "feeds it video frames."),

    # ------------------------------------------------------------------- audio
    # Speech OUT, as opposed to speech in. Deliberately NOT folded into the
    # transcription capability as a direction flag: one capability holds one
    # resident model (AI-4), so a shared "audio" capability would have a
    # synthesis model evict a Whisper model and back again on every alternation.
    _t("text-to-speech", "text to speech", "audio", None,
       "Reads text aloud as audio.",
       "Speech synthesis is a separate capability from transcription — one resident model "
       "per capability — and no runner ships for it yet."),
    _t("text-to-audio", "audio generation", "audio", None,
       "Generates sound — speech, music, effects.",
       "No audio-generation runner ships yet."),
    # An audio-language model: a recording and a prompt in, text out. NOT speech
    # recognition — it is asked questions about the audio rather than asked to
    # transcribe it — and mlx-lm resolves a checkpoint by importing
    # `mlx_lm.models.<model_type>`, which has no module for one.
    _t("audio-text-to-text", "audio + text to text", "multimodal", None,
       "Answers questions about a recording — an audio clip and a prompt in, text out.",
       "Neither the text runners nor the speech runners load an audio-language model."),
    _t("audio-classification", "audio classification", "audio", None,
       "Sorts a sound into categories — which language, which speaker, what noise."),
    _t("audio-to-audio", "audio to audio", "audio", None,
       "Cleans up or separates sound — denoising, splitting voices from music."),
    _t("voice-activity-detection", "voice activity detection", "audio", None,
       "Finds which parts of a recording contain speech.",
       "This ships INSIDE the transcription runners as a component (`vad.py`), not as a "
       "model you load on its own."),

    # ------------------------------------------- families this app is not in
    _t("tabular-classification", "tabular classification", "tabular", None,
       "Sorts rows of a table into categories."),
    _t("tabular-regression", "tabular regression", "tabular", None,
       "Predicts a number from the columns of a table."),
    _t("tabular-to-text", "tabular to text", "tabular", None,
       "Writes a sentence describing rows of a table."),
    _t("table-to-text", "table to text", "tabular", None,
       "Writes a sentence describing a table."),
    _t("time-series-forecasting", "time-series forecasting", "tabular", None,
       "Predicts how a measured series continues."),
    _t("reinforcement-learning", "reinforcement learning", "rl", None,
       "A policy trained by trial and error — game agents, control.",
       "A policy is run by the environment it was trained for, not by a text, image or "
       "speech runner."),
    _t("robotics", "robotics", "rl", None,
       "Drives a robot from what its sensors see.",
       "A policy is run by the robot it was trained for, not by a text, image or speech "
       "runner."),
    _t("graph-ml", "graph machine learning", "other", None,
       "Learns over nodes and edges — molecules, networks."),
    # A model that takes and returns several modalities at once. Which one a
    # caller wants is not a thing this table can decide, so it stays unserved
    # even where a runner could technically open the checkpoint.
    _t("any-to-any", "any input to any output", "multimodal", None,
       "Handles several kinds of input and output — text, images, audio — in one model."),
    _t("other", "other", "other", None,
       "The author did not say what this model does."),
)

TASKS: dict[str, Task] = {task.tag: task for task in _TASKS}

#: Menu order, as vendored. A caller wanting only what runs here filters on
#: `capability` — see `supported_tags`.
TAG_ORDER: tuple[str, ...] = tuple(task.tag for task in _TASKS)


@dataclass(frozen=True)
class Classification:
    """What a model does and whether we run it — the three-state answer.

    `support` is the field to branch on. `capability` is non-None **exactly
    when** `support` is `SUPPORTED`, so a caller that reads only the capability
    still cannot offer a Load button for something no runner serves; the extra
    states exist so a page can EXPLAIN a null rather than drawing nothing.
    """

    #: The Hub tag this came from, when there was one. Kept even for `UNKNOWN`:
    #: a tag we have not vendored is still the most precise thing we know.
    tag: str | None
    #: Display words. For an unrecognised tag, the tag itself in plain spacing —
    #: printable, never joined to anything.
    label: str | None
    capability: str | None
    support: str
    #: A sentence for a page to show when `support` is not `SUPPORTED`. Empty
    #: otherwise, and empty when there is no evidence at all — "we do not know
    #: what this is" is said by the absence of a label, not by a sentence.
    reason: str = ""

    @property
    def supported(self) -> bool:
        return self.support == SUPPORTED

    @property
    def ruled_out(self) -> bool:
        """A task we recognise and do not serve.

        The one predicate that separates this from `UNKNOWN`, and the reason the
        distinction exists: format evidence may rescue an unknown repo, and must
        never rescue a ruled-out one. That is what put a Load button under
        `SymphonyGen` (a music policy read as a chat model) and under a cached
        diffusers VIDEO pipeline (read as an image model, because the diffusers
        runners are decisive about the FORMAT and nothing checked the task).
        """
        return self.support == NO_RUNNER


#: Nothing known at all: no card tag, no architecture, no decisive format.
NOTHING = Classification(None, None, None, UNKNOWN, "")


def classify(tag: str | None) -> Classification:
    """Classify one `pipeline_tag` — the ONLY way into the vocabulary.

    Deliberately total: every input gets an answer, and the answer for a string
    this table has never seen is `UNKNOWN` rather than a prose label that later
    code will treat as meaningful. The old path ended in
    `tag.replace("-", " ")`, which made the vocabulary open and therefore
    unclassifiable — `tests/test_ai_tasks.py` pins that an invented tag lands
    here and not in a capability.
    """
    if not isinstance(tag, str) or not tag.strip():
        return NOTHING
    tag = tag.strip()
    task = TASKS.get(tag)
    if task is None:
        return Classification(
            tag, tag.replace("-", " ").replace("_", " "), None, UNKNOWN,
            "This app does not recognise that kind of model, so it cannot say what would "
            "run it.")
    if task.capability:
        return Classification(tag, task.label, task.capability, SUPPORTED, "")
    return Classification(tag, task.label, None, NO_RUNNER, reason(task))


def reason(task: Task) -> str:
    """Why an unsupported task is unsupported, in words a page can print.

    A written `note` where the answer has any subtlety — those are the ones a
    user would otherwise mail us about — and a plain sentence where it does not.
    Generated rather than written fifty-three times, because a generated
    sentence that is honest beats a hand-written one that goes stale.
    """
    if task.note:
        return task.note
    return f"Nothing on this machine runs {task.label} models."


def capability_for_tag(tag: str | None) -> str | None:
    """The capability that would load a model with this tag, or None.

    A convenience over `classify`, for callers that genuinely only need the
    capability (a search filter, a grouping key). Anything that SHOWS the
    answer to a person should call `classify` and read `support` — a null here
    is three different facts.
    """
    return classify(tag).capability


def supported_tags() -> tuple[str, ...]:
    """The tags this app can download AND run, in menu order.

    One list, derived — so a search filter and a downloaded card cannot disagree
    about whether a kind of model is runnable, which they would the moment two
    hand-maintained lists drifted. Adding a runner is a `capability` on one row
    here; nothing else needs an edit.
    """
    return tuple(task.tag for task in _TASKS if task.capability)


def label_for(tag: str | None) -> str | None:
    return classify(tag).label


def help_for(tag: str | None) -> str | None:
    """The glossary sentence for a tag, or None. Keyed by TAG (HS-7): the two
    faces of the AI Models page used to key it by prose label, which is how one
    concept read from a card and from a config produced two spellings and only
    one of them had an entry."""
    task = TASKS.get(tag) if isinstance(tag, str) else None
    return task.help if task and task.help else None

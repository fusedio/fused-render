"""Validating and shaping one `fused.ai.embed()` request, written once (SPEC §40).

`mlx_embed` and `onnx_embed` are two DIFFERENT engines that produce vectors in
the SAME SPACE (see `catalog.py`'s embeddings blocks, and `registry.EMBEDDINGS`'s
own comment) — the one pair here where that is true. A caller must therefore
get the same answer to "is this request well-formed" and "what does this
vector mean" whichever engine served it, which is exactly the kind of rule a
second copy drifts on. This module is where both halves of `generate()` that
have nothing to do with a text tower or a vision tower live, so neither
worker has to re-derive them.

(They read different FILES — MLX safetensors against an `onnx/` graph export —
which is why `catalog.py` keeps two lists. That is a fact about downloads and
changes nothing about the request shape, which is this module's whole subject.)

**Mostly stdlib, and no import of `fused_render`** — the same constraint
`formats.py` documents, for the same reason: this is imported by both runners'
own interpreters through the same `sys.path` insert that reaches
`worker_base`. `open_image` is the one exception, and it defers its PIL import
to the call itself (never at module load) — pillow is a dependency BOTH of
these two runners declare, unlike onnxruntime or mlx-embeddings, so importing it
lazily here costs nothing neither runner already pays, and costs it only when
a page actually asked to embed an image.
"""

from __future__ import annotations

import math
import os
import sys

# `formats` sits in THIS directory. Two loaders reach this file — both runners
# put `runners/` on `sys.path` and import it bare, while
# `server/routers/ai_runtime.py` imports it as
# `fused_render.ai.runners.embed_common` — and the package-relative reading must
# be tried FIRST so the server does not end up with a second copy of `formats`
# under a second name. `partial.py` carries the same two-line guard for the same
# two loaders; see its comment for the drift it prevents.
try:
    from . import formats
except ImportError:  # pragma: no cover - the runner reading, exercised in prod
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import formats

#: The two things a text can BE to a retrieval model, and what a caller who says
#: nothing gets. Both live in `formats` because BOTH runners and the SERVER read
#: them — the route reports the resolved scheme on every catalog entry — and a
#: second literal here is the copy that goes stale.
KINDS = formats.TEXT_EMBED_KINDS

#: **"document", and it is the one defaulting decision in this file that could
#: quietly cost someone their recall, so here is the whole argument** (ported
#: from PR #780, whose `text_embed_common.py` argues it at length).
#:
#: There is no neutral option. Every scheme in `formats.TEXT_EMBED_PROMPTS` has
#: two sides and a call must pick one, so the question is only which wrong guess
#: is cheaper when the caller has not thought about it yet.
#:
#: * Default "query", and someone who indexes a corpus with a bare call stamps
#:   the query instruction onto ten thousand passages. Every later search then
#:   compares a properly-prefixed query against a corpus that claims to be ten
#:   thousand queries — the mismatched state, which is measurably worse than
#:   using no prefix at all (the e5 card says so outright about its own pair).
#: * Default "document", and the same person gets a corpus that is internally
#:   consistent. If they never discover `kind`, every text in the system —
#:   corpus and query alike — carries the same prefix, which is the SYMMETRIC
#:   behaviour every one of these models supports and the behaviour every
#:   encoder had before asymmetric prompting existed. It is not optimal; it
#:   degrades gracefully rather than to the mismatched state.
#:
#: The tie-breaker is `bge`: its document side is the EMPTY string, because its
#: card instructs the query only. So on that family this default means a bare
#: call embeds text verbatim — precisely what someone who has not read about
#: prompt schemes expects to happen — while the opposite default would silently
#: prepend a sentence about searching to text nobody was searching.
#:
#: Documented on the bridge (`fused.ai.embed`) as well, because a default whose
#: reasoning lives only in the runner is a default page authors cannot act on.
DEFAULT_KIND = formats.TEXT_EMBED_DEFAULT_KIND

#: Above this a batch is refused rather than run. The same number
#: `ai_runtime.api_ai_embed` checks before a job ever starts — restated here,
#: not merely trusted from there, because `generate()` is also reachable
#: through the worker's own `/generate` route directly (a test drives it that
#: way, and so would any caller that is not the app's own router).
MAX_ITEMS = 64


def request_kind(body: dict) -> tuple[str, list, str]:
    """`("texts"|"paths", items, "query"|"document")`, or a `ValueError` naming
    what is wrong.

    The one shape both engines accept: EXACTLY one of `texts`/`paths`, a
    non-empty list of non-empty strings, at most `MAX_ITEMS` long, and an
    optional retrieval `kind`. Checked once so a batch of 65 does not read as an
    ONNX limit on one engine and an MLX one on the other — the same argument
    `MAX_ITEMS` above makes.

    **THREE values, and the third is why this is a wider tuple rather than a
    second function beside it.** `kind` picks which half of a retrieval model's
    prompt pair goes in front of these texts, and a caller who never asked for
    it gets `DEFAULT_KIND` — so a worker that forgot to call an
    `embed_common.retrieval_kind(body)` would silently embed every query with
    the document prefix and return vectors that are unit length, correctly
    shaped and worse. Widening the tuple makes that omission a `ValueError` at
    unpack time instead of a recall regression nobody attributes to this call.
    Two engines and one route read this; the compiler is the reviewer.

    **An unrecognised `kind` is refused rather than defaulted**, for the same
    reason stated the other way round: it is the one field here whose wrong
    value produces no error anywhere downstream.

    **`kind` beside `paths` is refused too.** A prompt scheme instructs TEXT —
    `"search_query: "` glued to the front of a sentence — so there is nothing
    for it to prefix on an image. Accepting it would mean silently ignoring the
    only field the caller set.
    """
    texts = body.get("texts")
    paths = body.get("paths")
    has_texts = isinstance(texts, list) and bool(texts)
    has_paths = isinstance(paths, list) and bool(paths)
    if has_texts and has_paths:
        raise ValueError("pass exactly one of 'texts' or 'paths', not both")
    if not has_texts and not has_paths:
        raise ValueError("pass 'texts' or 'paths' — a non-empty list of strings")
    source, items = ("texts", texts) if has_texts else ("paths", paths)
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items at a time, got {len(items)}")
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item:
            raise ValueError(f"'{source}[{index}]' must be a non-empty string")

    kind = body.get("kind")
    # `None` is JSON's "I did not say", and the bridge forwards an `undefined`
    # option as an absent key — but a page building its body with
    # `kind: state || null` sends the null, and that must mean the default
    # rather than a 400.
    if kind is None:
        kind = DEFAULT_KIND
    elif source == "paths":
        raise ValueError(
            "'kind' applies to 'texts' only — it picks which half of a "
            "retrieval model's prompt pair goes in front of them, and there is "
            "nothing to prefix on an image. Drop it, or pass 'texts'.")
    if kind not in KINDS:
        raise ValueError(
            f"'kind' must be one of {', '.join(repr(k) for k in KINDS)} "
            f"(got {kind!r}) — a retrieval model instructs a question and a "
            f"passage differently, so this picks which prompt goes in front "
            f"of these texts. Leave it out for {DEFAULT_KIND!r}.")
    return source, items, kind


def prompted(texts: list, kind: str, scheme: str) -> list:
    """`texts` with this model's prefix for `kind` glued to the front of each.

    A plain concatenation and nothing cleverer, because every scheme in
    `formats.TEXT_EMBED_PROMPTS` is literally a string the model's own card puts
    in front of the text. The `"none"` scheme's prefix is `""`, so this returns
    the input unchanged for a model with no convention — as a NEW list, because
    the caller may hold the original for its own reporting.
    """
    prefix = formats.text_embed_prompt(scheme, kind)
    if not prefix:
        return list(texts)
    return [prefix + text for text in texts]


#: What a `paths` entry can BE, by its extension — and the extension is the
#: whole test, deliberately. A page author passes a path they read off the
#: explorer, so the extension is the one fact about the file they can see and
#: act on; sniffing magic bytes would let a `.mp3` that is really a JPEG embed
#: as a picture, which is a confident wrong vector rather than a sentence
#: naming the file. Anything not listed is read as an IMAGE and handed to
#: PIL, whose own "is not a readable image" sentence then names it — the
#: behaviour every dual encoder had before audio and video existed here, kept
#: so a `.jpeg`/`.PNG`/`.heic` keeps working without this table knowing it.
#:
#: Two decoders, two lists. Audio is read by `mlx_audio.audio_io.read` (wav,
#: mp3, flac and ogg-vorbis natively through miniaudio; m4a/aac/opus by
#: shelling out to an `ffmpeg` this app does not ship — see `media_error`).
#: Video is read by OpenCV's `VideoCapture`, which opens what the platform's
#: codecs do. `.webm` is listed as VIDEO because that is what the container
#: nearly always holds, and OpenCV reads it; an audio-only webm is the rare
#: case and is refused as "not a readable video" rather than silently
#: decoded as sound.
AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".flac", ".ogg", ".oga",
                            ".m4a", ".aac", ".opus"})
VIDEO_SUFFIXES = frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
                            ".mpg", ".mpeg"})

#: The three kinds `media_kind` answers, in the order the unified runner
#: embeds them.
IMAGE, AUDIO, VIDEO = "image", "audio", "video"

#: Which of those every embedding family reads. Read by the route (to publish
#: `acceptsAudio`/`acceptsVideo` and refuse a sound on a picture model by
#: name) and by both workers (to refuse the same request a second time for a
#: caller reaching `/generate` directly). A family absent here reads nothing
#: by path — the prose encoders, whose `paths` refusal predates this table.
#: Keyed by `formats.embed_model_type`'s answer, so the route and the worker
#: classify one checkpoint identically.
EMBED_MEDIA = {
    "siglip": (IMAGE,),
    "clip": (IMAGE,),
    "embedding_gemma2": (IMAGE, AUDIO, VIDEO),
}

#: Those suffixes that need `ffmpeg` on PATH to decode — named so the refusal
#: can say what to install, or what to convert to, rather than relaying
#: mlx-audio's own multi-line install banner.
_FFMPEG_AUDIO_SUFFIXES = frozenset({".m4a", ".aac", ".opus"})


def media_kind(path: str) -> str:
    """`"image"`, `"audio"` or `"video"` for `path`, off its extension alone —
    see `AUDIO_SUFFIXES` for why the extension is the whole test."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in AUDIO_SUFFIXES:
        return AUDIO
    if suffix in VIDEO_SUFFIXES:
        return VIDEO
    return IMAGE


def check_media_path(path: str) -> None:
    """Raise `ValueError` naming `path` when it is not a readable file.

    The audio and video decoders take a PATH rather than an open handle, so
    the two ordinary failures `open_image` turns into sentences — gone, or a
    directory — would otherwise surface as whatever `miniaudio` or OpenCV says
    about them, which for OpenCV is nothing at all (`VideoCapture` just fails
    to open). Checked here, once, so both decoders get the same two sentences
    `open_image` gives a picture.
    """
    if os.path.isdir(path):
        raise ValueError(f"not a file: {path}")
    if not os.path.isfile(path):
        raise ValueError(f"no such file: {path}")


#: Named in every media refusal, because the fix is to pick a different model
#: and this is the one curated row that reads all three kinds.
UNIFIED_HINT = ("EmbeddingGemma 2 (mlx-community/embeddinggemma-2-bf16, on a "
                "Mac) embeds text, images, audio and video in one space")


def unaccepted_media(model_id: str, accepted: tuple, paths: list) -> str | None:
    """The sentence refusing the first entry of `paths` whose kind `accepted`
    lacks — or None when every path is something this model reads.

    One function for the ROUTE and BOTH workers, so a sound file handed to a
    SigLIP row is refused identically at `/api/ai/embed` and at the worker's
    own `/generate`: the model is named because the fix is to pick a different
    one, the FILE is named because a batch of 64 has to say which entry was
    the problem, and `UNIFIED_HINT` says where audio and video do work.
    """
    for path in paths:
        kind = media_kind(path)
        if kind not in accepted:
            reads = " and ".join(accepted) if accepted else "nothing"
            article = "a" if kind == VIDEO else "an"
            return (f"{model_id} reads text and {reads} only — {path} is "
                    f"{article} {kind} file, which it has no encoder for. "
                    f"{UNIFIED_HINT}.")
    return None


def media_error(path: str, kind: str, error: BaseException) -> ValueError:
    """The `ValueError` to raise for a decoder failure on `path` — the file,
    never a traceback, exactly `open_image`'s rule.

    One case gets its own sentence: mlx-audio decodes m4a/aac/opus by shelling
    out to `ffmpeg`, and raises a `RuntimeError` carrying a multi-line install
    banner when it is not on PATH. This app does not ship ffmpeg (the
    transcribe runner's manifest states why at length), so the useful sentence
    names the formats that decode WITHOUT it rather than relaying the banner.
    """
    suffix = os.path.splitext(path)[1].lower()
    text = str(error)
    if kind == AUDIO and suffix in _FFMPEG_AUDIO_SUFFIXES and "ffmpeg" in text:
        return ValueError(
            f"{path} needs ffmpeg on PATH to decode ({suffix} is not a format "
            f"the runner reads on its own). Install ffmpeg, or convert it to "
            f"wav, mp3, flac or ogg, which read without it.")
    first = text.strip().splitlines()[0] if text.strip() else error.__class__.__name__
    return ValueError(f"{path} is not a readable {kind} ({first})")


#: Whether `register_heif_opener()` has run in this process. Once, not per call:
#: it mutates PIL's global format registry, and `open_image` is on the hot path
#: of a 64-image batch.
_heif_registered = False


def _register_heif():
    """Teach PIL to open HEIC/HEIF and AVIF, if pillow-heif is installed.

    **Not optional in practice.** HEIC is the default camera format on every
    iPhone since iOS 11, so a photo library is mostly HEIC — and plain Pillow
    cannot open a single one of those files. Without this, `paths` embedding
    works on screenshots and fails on the actual photographs, which is the
    half a page's user cares about; measured on a real library, the first
    `.HEIC` came back "is not a readable image".

    Soft-failing on ImportError rather than raising: an engine whose venv
    predates this dependency must still embed the formats it always could,
    and the caller then gets `open_image`'s ordinary "not a readable image"
    sentence for a HEIC rather than an import traceback.
    """
    global _heif_registered
    if _heif_registered:
        return
    _heif_registered = True
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return
    register_heif_opener()


def open_image(path: str):
    """The image at `path`, as RGB — or a `ValueError` naming the file.

    **The file, never a traceback.** A page passes a path it read from the
    explorer, and the ways that can go wrong are ordinary and nameable: gone,
    a directory, not actually a picture. `UnidentifiedImageError` is PIL's own
    word for the last of those, and it is what a caller sees for a `.txt`
    handed in by mistake — the failure `formats.py`'s whole approach exists to
    turn into a sentence rather than a stack frame, one layer up.

    `.convert("RGB")` unconditionally: a dual encoder's vision tower takes
    three channels, and a paletted PNG or an RGBA screenshot would otherwise
    fail inside the processor with an error about tensor shape rather than
    about the picture.
    """
    from PIL import Image, ImageFile, UnidentifiedImageError

    # **A truncated file is decoded as far as it goes, not refused.** Photo
    # libraries are full of half-written JPEGs — an interrupted download, a
    # messaging app's cache, a copy that died mid-flight — and PIL's default is
    # to raise `OSError: image file is truncated (N bytes not processed)` on
    # them. In a batch API that is the wrong trade by a wide margin: one bad
    # file out of 64 would abort the whole call, so indexing a real folder
    # stops dead on a file the user does not know exists and cannot act on.
    # The bytes that ARE there make a perfectly good embedding.
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    _register_heif()

    try:
        with Image.open(path) as image:
            return image.convert("RGB")
    except FileNotFoundError:
        raise ValueError(f"no such file: {path}") from None
    except IsADirectoryError:
        raise ValueError(f"not a file: {path}") from None
    except UnidentifiedImageError:
        raise ValueError(f"{path} is not a readable image") from None
    except OSError as e:
        raise ValueError(f"could not read {path}: {e}") from None


def unit_normalize(vectors: list) -> list:
    """Every row scaled to length 1, in plain Python floats.

    **Framework-agnostic on purpose.** Both runners hand this the SAME shape —
    a plain list of lists, already off the GPU/Metal device and out of numpy —
    so the one piece of arithmetic that makes two engines' vectors comparable
    (the whole point of the shared space `registry.py` documents) is read in one
    place rather than risking a numpy call and an mx call quietly disagreeing.

    A zero vector is left as zero rather than raising or dividing by it — not
    a real embedding a resident model produces, but a mocked one in a test
    might supply exactly that, and a divide-by-zero belongs to the test's
    fixture, not to this function's contract.
    """
    normalized = []
    for row in vectors:
        norm = math.sqrt(sum(v * v for v in row))
        normalized.append([v / norm for v in row] if norm > 0 else [float(v) for v in row])
    return normalized

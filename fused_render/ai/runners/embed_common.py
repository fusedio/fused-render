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

#: Above this a batch is refused rather than run. The same number
#: `ai_runtime.api_ai_embed` checks before a job ever starts — restated here,
#: not merely trusted from there, because `generate()` is also reachable
#: through the worker's own `/generate` route directly (a test drives it that
#: way, and so would any caller that is not the app's own router).
MAX_ITEMS = 64


def request_kind(body: dict) -> tuple[str, list]:
    """`("texts"|"paths", items)`, or raises `ValueError` naming what is wrong.

    The one shape both engines accept: EXACTLY one of `texts`/`paths`, a
    non-empty list of non-empty strings, at most `MAX_ITEMS` long. Checked
    once so a batch of 65 does not read as an ONNX limit on one engine and an
    MLX one on the other — the same argument `MAX_ITEMS` above makes.
    """
    texts = body.get("texts")
    paths = body.get("paths")
    has_texts = isinstance(texts, list) and bool(texts)
    has_paths = isinstance(paths, list) and bool(paths)
    if has_texts and has_paths:
        raise ValueError("pass exactly one of 'texts' or 'paths', not both")
    if not has_texts and not has_paths:
        raise ValueError("pass 'texts' or 'paths' — a non-empty list of strings")
    kind, items = ("texts", texts) if has_texts else ("paths", paths)
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items at a time, got {len(items)}")
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item:
            raise ValueError(f"'{kind}[{index}]' must be a non-empty string")
    return kind, items


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

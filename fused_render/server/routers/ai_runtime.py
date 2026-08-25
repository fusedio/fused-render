"""GET /api/ai/runtime, /api/ai/catalog and the load/unload/download POSTs —
what this machine is running locally (SPEC §40).

The other half of `/api/ai`. That endpoint answers "complete this prompt"; these
answer "which model, held where, costing what" — the questions that only exist
once inference is local and a model is a resident process rather than a request
to somebody else's datacentre.

Four routes and one rule each:

* `GET /api/ai/runtime` — what is loaded, what each is costing in resident bytes,
  and which runners this machine can even use. In-memory plus one health probe
  per live worker, so the sidebar can poll it.
* `POST /api/ai/runtime/load` — make a model resident. Returns a JOB ID
  immediately; a cold load is a multi-GB download and nothing waits on it. A
  `capability` left out is INFERRED from what the repo is, never defaulted —
  see `_inferred_capability`, and D321 for the bug that made it so.
* `POST /api/ai/runtime/unload` — release the weights.
* `POST /api/ai/runtime/download` — fetch without loading, for the AI Models
  page, where the verb is "Download" and the user is not asking to run anything
  yet.

Plus the three routes that make a capability DO something rather than be
resident: `POST /api/ai/image`, `POST /api/ai/transcribe` and
`POST /api/ai/video`. All three answer with a job id and a path, because all
three run for minutes (video: potentially hours — see `supervisor.
VIDEO_TIMEOUT_S`) and all three produce a file.

The POSTs mutate — they start processes and write gigabytes — so every one of
them carries the D3 `X-Fused` guard. The reads do not, like every other read in
the app.
"""

from __future__ import annotations

import functools
import os
import secrets
import struct
import time

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render._view_url_codec import canonical_fs_path
from fused_render.ai import catalog, registry, supervisor
# The `speakers` rule and the per-engine option rules, imported rather than
# restated. They are the SAME modules the runners import out of their own venvs
# — which is why every heavy import inside them is deferred, and why reading a
# rule here costs nothing. `embed_common` joins them for the same reason: its
# request-shape check is what BOTH embedding runners' own `generate()` calls,
# and a body this route refuses must be refused for the identical reason a
# worker asked directly would give.
from fused_render.ai.runners import diarize, embed_common, engine_options, formats, partial, preview
from fused_render.server.common import _error, _require_fused
# The AI Models page's reading of the local cache, imported rather than
# re-derived: see `_inferred_capability` and `_catalog_with_downloads`. It imports
# nothing from here.
from fused_render.ai.hub_cache import (
    CachedModel, cached_capability, cached_models, embed_family, has_vision_tower,
    is_downloaded,
)

router = APIRouter()

# Bounds for an image request. Not distrust of the caller — the caller is a page
# on this machine — but arithmetic: a 4096² render at 100 steps is an hour and
# an OOM on a laptop, and a page that asked for it by typo should get a picture
# rather than a hung worker. Dimensions snap to a multiple of 16 because the
# pipelines require it and silently rounding is friendlier than a stack trace
# from inside torch.
_MIN_SIDE, _MAX_SIDE, _SIDE_STEP = 256, 2048, 16
_MAX_STEPS = 100
_MAX_SEED = 2**31 - 1

# The request envelope of a job-backed AI call is closed (D413): an option
# neither of these routes has is refused with a 400 rather than silently
# dropped. These are the CALLER-FACING sets — the same facts `runtime.js`
# restates as its own whitelist arrays, and `test_the_bridges_accepted_*`
# below is what stops the two from drifting apart.
_IMAGE_OPTIONS = frozenset({
    "prompt", "model", "width", "height", "steps", "guidance", "seed", "image"})
# Bounds for a video request. Narrower canvas than an image's — `w*h <=
# 768*1344` — originally chosen against the FL2VA checkpoint of the
# since-dropped `h3-video` runner (D468), the shape it was benchmarked at;
# a caller asking for more gets clamped down to it rather than an OOM
# minutes into a render. Kept unchanged on that runner's removal because it
# is a safety rail the APP chose, not a fact about any engine's weights
# (unlike the frame grid and the canvas/step DEFAULTS below, which are —
# see `registry.VideoTraits`). `frames` snaps to the SERVING engine's own
# valid grid (`_snap_frames`, given that engine's traits), because a value
# off its grid is not a smaller or larger request, it is one that engine
# renders differently than the reply would claim.
_MIN_VIDEO_SIDE, _MAX_VIDEO_SIDE, _VIDEO_SIDE_STEP = 256, 1344, 32
_MAX_VIDEO_PIXELS = 768 * 1344
#: `n` ranges 1..21 on EVERY engine's own grid — an app-chosen bound, not a
#: per-engine fact. `registry.MIN_VIDEO_FRAMES_N`/`MAX_VIDEO_FRAMES_N`, not a
#: private pair here, because `catalog.py`'s video-traits payload for the
#: Playground's frame slider needs the identical window — a slider computed
#: from one and a server clamped by the other would disagree with itself
#: exactly the way Task 5 left the client disagreeing with the engine's
#: grid. The window was originally VERIFIED against the built `h3` binary of
#: the since-dropped `h3-video` runner (D468), which refused `n=0` and
#: anything aligning past its released 5..362 range outright. LTX has no
#: compiled binary to refuse a value, so the same `[1, 21]` window is
#: carried over as the app's own bound on its grid (1 + 8*21 = 169 frames,
#: ~7s at 24fps), rather than inventing an unrelated ceiling with no
#: measurement behind it.
_MIN_FRAMES_N, _MAX_FRAMES_N = registry.MIN_VIDEO_FRAMES_N, registry.MAX_VIDEO_FRAMES_N
#: The floor of 2 came from the since-dropped `h3-video` runner's own hard
#: range ("denoising steps must be in [2, 1000]", D468) — for that binary 1
#: step was not merely slow, it was refused outright. LTX has no such floor
#: (`stage1_steps` is a plain slice of a fixed sigma schedule — see
#: `ltx_video/worker.py`), but a value this low is not a meaningfully faster
#: render, so the floor stays as the app's own rather than being relaxed to
#: 1 on that runner's removal. The ceiling (50) is ours to pick either way.
_MIN_VIDEO_STEPS, _MAX_VIDEO_STEPS = 2, 50
# No `guidance` here — the shipping video engine is CFG-distilled and takes
# no such parameter. A caller passing one hits `_reject_unknown` like any
# other unsupported option.
_VIDEO_OPTIONS = frozenset({
    "prompt", "model", "width", "height", "frames", "steps", "seed"})
_TRANSCRIBE_OPTIONS = frozenset({
    "path", "model", "language", "task", "initialPrompt", "vad", "diarize",
    "speakers", "words"})
# `base` is bridge-injected — `aiTranscribe` adds it from the page's own
# `?path=`, never from the caller's own options object — so the SERVER's
# accepted set is wider than the caller-facing one on purpose. Collapsing
# these two into one set would make a caller passing `base` itself stop
# being an error.
_TRANSCRIBE_SERVER_OPTIONS = _TRANSCRIBE_OPTIONS | {"base"}
# `aiImage` gained the identical asymmetry the moment `image` became an
# option: `runtime.js` injects `body.base` from the page's own `?path=`
# exactly as `aiTranscribe` does, so a caller passing `base` directly is
# passing an option that does not exist from where it is standing.
_IMAGE_SERVER_OPTIONS = _IMAGE_OPTIONS | {"base"}


def _reject_unknown(body: dict, allowed: frozenset[str], endpoint: str):
    """400 naming every key of `body` that is not in `allowed`, or None.

    Called before any other validation in `api_ai_image`/`api_ai_transcribe`
    so an envelope error beats a field error — a page that mistyped an
    option AND passed a bad `steps` learns about the option it does not have
    first, rather than about the unrelated field it also got wrong.

    Reports every unknown key at once, not just the first: a page passing
    both `image` and `strength` should learn about both in one round trip.
    Sorted, so the message is stable and testable.
    """
    unknown = sorted(k for k in body if k not in allowed)
    if not unknown:
        return None
    named = ", ".join(repr(k) for k in unknown)
    verb = "is not an option" if len(unknown) == 1 else "are not options"
    accepted = ", ".join(sorted(allowed))
    return _error(f"{named} {verb} of {endpoint}; accepted: {accepted}", status=400)


def _side(value, default: int) -> int:
    try:
        side = int(value)
    except (TypeError, ValueError):
        side = default
    side = max(_MIN_SIDE, min(_MAX_SIDE, side))
    return side - (side % _SIDE_STEP)


def _image_pixel_size(path: str) -> tuple[int, int] | None:
    """`(width, height)` read off `path`'s own PNG/JPEG/WebP header, or None.

    Decision 1: an edit's default size comes from the BASE IMAGE, and this
    process has no Pillow — the app's own `pyproject.toml` does not carry it,
    and `/api/ai/image` answers before the render, from the server rather
    than from a worker that may not even be resident yet. So this is a small
    stdlib reader rather than a new dependency: three formats, each read off
    the handful of bytes at the front of the file that name its own
    dimensions, never the pixels.

    Fails toward None on anything this cannot parse — a truncated read, a
    format not listed, a file that is not actually an image despite its
    extension — which the caller reads as "fall back to the ordinary 1024²
    default" rather than as an error: this is a convenience default, not a
    validation the caller is trusted to have gotten right elsewhere (the
    `/api/fs/*` existence/is-a-file checks already ran before this is called).
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                # IHDR is always the first chunk: 8-byte signature, then a
                # 4-byte length, a 4-byte "IHDR", then width/height as two
                # big-endian uint32s.
                width, height = struct.unpack(">II", head[16:24])
                return width, height
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                # All three sub-formats — not just the extended `VP8X`.
                # `cwebp`, Pillow and a browser's own "Save as WebP" all
                # emit plain `VP8 ` (lossy) or `VP8L` (lossless), and a
                # reader that only understood `VP8X` would fall back to
                # 1024x1024 for the ordinary case and stretch the render —
                # a silent surprise exactly of the kind this feature exists
                # to avoid, not an acceptable narrowing. Every sub-format's
                # own payload starts at the same offset (12-byte RIFF
                # header + 8-byte chunk header), so the three branches
                # differ only in how many more bytes of THEIR bitstream
                # header they read.
                kind = head[12:16]
                if kind == b"VP8X":
                    # The one form that names a CANVAS size directly, not a
                    # bitstream one: 1 byte of flags, 3 reserved, then
                    # width-1/height-1 as two 24-bit little-endian ints.
                    handle.seek(24)
                    dims = handle.read(6)
                    if len(dims) < 6:
                        return None
                    width = int.from_bytes(dims[0:3], "little") + 1
                    height = int.from_bytes(dims[3:6], "little") + 1
                    return width, height
                if kind == b"VP8L":
                    # Lossless: a 1-byte signature (0x2F) then a packed
                    # 32-bit little-endian header — 14 bits width-1, 14
                    # bits height-1, 1 bit alpha, 3 bits version.
                    handle.seek(20)
                    payload = handle.read(5)
                    if len(payload) < 5 or payload[0] != 0x2F:
                        return None
                    bits = int.from_bytes(payload[1:5], "little")
                    width = (bits & 0x3FFF) + 1
                    height = ((bits >> 14) & 0x3FFF) + 1
                    return width, height
                if kind == b"VP8 ":
                    # Lossy: a 3-byte frame tag, then — on a KEY frame only
                    # — a 3-byte start code (`0x9d 0x01 0x2a`) and width/
                    # height as two little-endian uint16s, each carrying a
                    # 2-bit scale factor in its own top bits (RFC 6386
                    # §9.1). A WebP's first frame is always a key frame, so
                    # this is the frame every such file opens with.
                    handle.seek(20)
                    payload = handle.read(10)
                    if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
                        return None
                    width = int.from_bytes(payload[6:8], "little") & 0x3FFF
                    height = int.from_bytes(payload[8:10], "little") & 0x3FFF
                    return width, height
                return None
            if head[:2] == b"\xff\xd8":
                # Walk JPEG markers until an SOFn (start of frame) segment,
                # which carries height then width as big-endian uint16s.
                # APPn/COM/etc. segments are skipped by their own length.
                handle.seek(2)
                while True:
                    marker = handle.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    kind = marker[1]
                    if kind in (0xD8, 0x01) or 0xD0 <= kind <= 0xD9:
                        continue  # no length field on these
                    length_bytes = handle.read(2)
                    if len(length_bytes) < 2:
                        return None
                    length = struct.unpack(">H", length_bytes)[0]
                    if 0xC0 <= kind <= 0xCF and kind not in (0xC4, 0xC8, 0xCC):
                        data = handle.read(5)
                        if len(data) < 5:
                            return None
                        height, width = struct.unpack(">HH", data[1:5])
                        return width, height
                    handle.seek(length - 2, 1)
    except (OSError, struct.error):
        return None
    return None


def _edit_default_size(image_path: str) -> tuple[int, int] | None:
    """An edit's default `(width, height)`, or None to fall back to 1024².

    The prototype's own arithmetic (confirmed as written by the gate run —
    see the flux2-edit handoff, Decision 1): fit the longest side to 1024
    WITHOUT upscaling, snap down to a multiple of 16, floor 256, aspect
    preserved. **The 256 floor overrides "aspect preserved" on an extreme
    ratio** — a 4000x200 base (20:1) floors its short side to 256 and comes
    back 1024x256 (4:1) — which is a real, accepted consequence of the
    arithmetic as written, not an oversight; see AI-9f and the SKILL for the
    same note.

    **Integer division throughout, not `scale = min(1.0, 1024.0 / longest)`
    followed by `int(side * scale)`.** That float form is a deliberate
    DEVIATION from the prototype rather than a port of it: the prototype
    carries the identical rounding accident, but it was never the stated
    contract. Floating-point makes `1024.0 / 1122 * 1122` land on
    `1023.9999999999999` rather than `1024.0` for roughly one width in nine,
    and `int()` truncates that short — 1122x600 came back `1008x544`
    instead of the intended `1024x544`, snapped a whole `_SIDE_STEP` short
    of the longest side the docstring promises to hit. `width * 1024 //
    longest` computes the same ratio in integers and cancels exactly when
    `longest` divides `width * 1024`, which is the case a scale-by-float
    silently gets wrong.
    """
    dims = _image_pixel_size(image_path)
    if dims is None:
        return None
    width, height = dims
    if width <= 0 or height <= 0:
        return None
    longest = max(width, height)
    if longest > 1024:
        # Downscale only — an already-small base is never blown up to fill
        # 1024 (a 500x400 base stays 500x400-shaped, just snapped).
        width = width * 1024 // longest
        height = height * 1024 // longest
    fitted_w = max(_MIN_SIDE, width // _SIDE_STEP * _SIDE_STEP)
    fitted_h = max(_MIN_SIDE, height // _SIDE_STEP * _SIDE_STEP)
    return fitted_w, fitted_h


def _images_dir() -> str:
    """Where rendered images land: `<home>/ai/images`.

    Under the app's home rather than beside the page that asked, because the
    page may be anywhere — including a read-only folder — and because a picture
    that took four minutes to make should outlive the tab that made it.
    """
    from fused_render.shell.storage import home_dir

    directory = os.path.join(home_dir(), "ai", "images")
    os.makedirs(directory, exist_ok=True)
    return directory


def _videos_dir() -> str:
    """Where rendered videos land: `<home>/ai/videos`. See `_images_dir`."""
    from fused_render.shell.storage import home_dir

    directory = os.path.join(home_dir(), "ai", "videos")
    os.makedirs(directory, exist_ok=True)
    return directory


def _video_side(value, default: int) -> int:
    """One dimension, clamped to the video range and snapped DOWN to a
    multiple of 32 — `_side`'s rule, with video's own bounds."""
    try:
        side = int(value)
    except (TypeError, ValueError):
        side = default
    side = max(_MIN_VIDEO_SIDE, min(_MAX_VIDEO_SIDE, side))
    return side - (side % _VIDEO_SIDE_STEP)


def _clamp_video_canvas(width: int, height: int) -> tuple[int, int]:
    """`(width, height)`, each already snapped by `_video_side`, brought under
    `w*h <= 768*1344` by shaving the LARGER side down by one step at a time.

    Alternating on the larger side (rather than always the same one) keeps an
    over-asked SQUARE canvas square rather than silently favouring one axis —
    an 1344x1344 ask should shrink toward a still-roughly-square frame, not
    collapse to `1344 x <minimum>`.
    """
    while width * height > _MAX_VIDEO_PIXELS:
        if width >= height and width > _MIN_VIDEO_SIDE:
            width -= _VIDEO_SIDE_STEP
        elif height > _MIN_VIDEO_SIDE:
            height -= _VIDEO_SIDE_STEP
        else:
            break
    return width, height


def _snap_frames(value, traits: "registry.VideoTraits") -> int:
    """The value on `traits`' own frame grid that the serving ENGINE would
    ACTUALLY RENDER for `value` — rounded UP to the next grid point, never
    to the nearest one.

    **Per-runner since Task 5 of the LTX-2.3 plan** — this used to be the
    since-dropped `h3-video` runner's grid, `5 + 17n`, hardcoded, because it
    was the only video runner there was. `traits` now carries whichever
    engine will actually serve the request (`registry.video_traits_for`,
    resolved by the caller), and the arithmetic is unchanged: `value =
    max(traits.frames_base, requested)`, then rounded up to the next
    `frames_base + frames_step * n`. Matching the direction matters, not only
    the grid: a server that rounded to NEAREST would report a smaller
    `frames` than the render it just started for any request whose
    distance-below its nearest grid point is shorter than its distance to the
    one above (on `5 + 17n`, verified against that runner's own binary, 100
    rendered as 107, not the "closer" 90). LTX has no compiled binary to
    align against, but `8n + 1` is the grid its own upstream CLI defaults to,
    and rounding the same direction keeps this function's one contract —
    "the frames on the reply are the frames the engine renders" — true.

    Bounded to `n` in `[_MIN_FRAMES_N, _MAX_FRAMES_N]` regardless of engine —
    an app-chosen safety rail (unlike the grid itself, this is not a fact
    about either engine's weights), so it stays a shared constant rather
    than a fourth `VideoTraits` field.
    """
    base, step = traits.frames_base, traits.frames_step
    try:
        frames = int(value)
    except (TypeError, ValueError):
        return base + step * traits.default_frames_n
    frames = max(base, frames)
    remainder = (frames - base) % step
    if remainder:
        frames += step - remainder
    n = (frames - base) // step
    n = max(_MIN_FRAMES_N, min(_MAX_FRAMES_N, n))
    return base + step * n


#: How long a preview frame has to sit untouched before a sweep takes it.
#:
#: An hour, which is far longer than it needs to be and deliberately so. A LIVE
#: preview is rewritten every denoising step, so its mtime is always seconds
#: old — the threshold is not really a timeout, it is the line between "nobody
#: is writing this" and "somebody is", and it is what lets the sweep run without
#: knowing which renders are in flight. Erring long costs an orphan an extra
#: hour on disk; erring short would delete the picture a user is watching.
_PREVIEW_TTL = 3600


def _sweep_previews(directory: str) -> None:
    """Remove preview frames that no render is writing any more.

    `preview.Sink.discard` runs on the way out of a render and takes the
    thumbnail with it — but only on a normal unwind, and a worker does not
    always get one. `supervisor._terminate` / `_kill_tree` end the process
    outright when a model is unloaded, the app shuts down, or a worker wedges,
    and what survives is a `<stem>.preview.png` (plus, if the kill landed
    between the save and the replace, a `.<pid>.tmp` beside it) in
    `<home>/ai/images` — a directory the user browses, holding a file with no
    job row to explain it and nothing that would ever remove it.

    Swept HERE, on the way into a render, rather than by a background timer: it
    is the moment this is free (the caller is about to wait minutes) and the
    only moment it is needed (the directory grows only when renders happen), and
    a timer would be a lifecycle to own for a few kilobytes.

    Matched by `preview.SUFFIX` appearing anywhere in the name, which covers the
    frame and its temp in one test and cannot match a render's own
    `<timestamp>-<uid>.png`. **The image itself is never touched at any age** —
    it is the artefact the whole feature exists to produce.

    Best-effort throughout, for the reason `discard` is: this runs at the front
    of a request that is about to work, and an untidy folder is worth more than
    a refused render. A directory that cannot be listed and a file that cannot
    be removed are both simply left.
    """
    cutoff = time.time() - _PREVIEW_TTL
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if preview.SUFFIX not in name:
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _transcripts_dir() -> str:
    """Where transcripts land: `<home>/ai/transcripts`.

    Same argument as `_images_dir`, and a stronger one: a 90-minute recording is
    minutes of decoding, and the tab that asked may be closed by the time it
    lands. The file is the result; the job row is only how it was watched.
    """
    from fused_render.shell.storage import home_dir

    directory = os.path.join(home_dir(), "ai", "transcripts")
    os.makedirs(directory, exist_ok=True)
    return directory


def _model_of(body: dict) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        return ""
    return model.strip()


def _inferred_capability(model: str) -> tuple[str | None, str | None]:
    """What to load `model` AS when the caller did not say, or why we cannot tell.

    **The omitted `capability` used to mean text generation, silently** (D321),
    which is a wrong-runner dispatch dressed up as a corrupt model: an MLX
    diffusion repo reached mlx-lm and raised `FileNotFoundError: config.json`,
    a repo that has never had one, while `/api/ai/image` rendered from the same
    snapshot perfectly — because that route is capability-bound by construction
    and this one was not. The same shape fired earlier through Preload with a
    whisper repo.

    Four questions, cheapest-honest first, and none of them touches the network:

    1. **The local snapshot**, read by `ai_models.cached_capability` — the very
       reading the AI Models page puts its engine tag and its Load button on.
       Asked of that module rather than re-derived here, so the card and the
       load cannot disagree about what a repo is.
    2. **The catalog**, for a repo not on disk yet. Every id this app itself
       recommends belongs to a runner, so the whisper-Preload case is answered
       before a byte is fetched.
    3. **Text generation**, for a repo that is neither — the old default, kept
       deliberately. A cold load of an unknown id cannot be classified without
       downloading it, and refusing one would break every page that preloads a
       chat model by id. The cost of a wrong guess here is bounded by the
       runner's own format check (`runners/formats.py`), which names the format
       it got and the format it needs instead of letting a library error escape.
    4. …except when the repo IS on disk and nothing here reads it. That is the
       one case where guessing has no excuse, and it answers with a sentence
       naming the repo, what it looks like, and what to pass.
    """
    reading = cached_capability(model)
    if reading.capability is not None:
        return reading.capability, None
    catalogued = catalog.capability_of(model)
    if catalogued is not None:
        return catalogued, None
    if not reading.cached:
        return registry.TEXT_GENERATION, None
    looks = (f"it looks like {reading.looks_like}" if reading.looks_like
             else "no engine that ships here reads its files")
    return None, (
        f"cannot tell what {model} is for, so 'capability' cannot be left out: "
        f"it is in this machine's model cache and {looks}. Pass one of "
        f"{', '.join(registry.capabilities())} — for example "
        f"fused.ai.models.load({model!r}, {{capability: "
        f"{registry.TEXT_GENERATION!r}}})."
    )


def _resolve_capability(body: dict, model: str) -> tuple[str | None, object]:
    """`(capability, None)`, or `(None, an error response)`.

    An explicitly passed capability is validated and used unchanged — this
    governs the OMITTED case only, which is what makes it additive.
    """
    requested = body.get("capability")
    if requested is not None:
        capability = requested if isinstance(requested, str) else ""
        if capability not in registry.capabilities():
            return None, _error(f"unknown capability {requested!r}", status=400)
        return capability, None
    capability, why = _inferred_capability(model)
    if capability is None:
        # 400, like every other "this request cannot be acted on as written":
        # the fix is an argument the caller can add.
        return None, _error(why, status=400)
    return capability, None


@router.get("/api/ai/runtime")
def api_ai_runtime():
    """Loaded models, their memory, and the runners available here.

    Sync `def`: it makes one localhost health request per live worker (usually
    zero or one), so it belongs in the threadpool rather than on the event loop.
    """
    return supervisor.describe()


def _cached_size_gb(size: int) -> float | None:
    """A measured footprint as `size_gb` means it: decimal GB, one decimal.

    The same unit and precision the curated entries use, because the field is read
    by the same line of the same page. `None` when there is nothing to measure —
    the no-guess rule the rest of this payload follows.
    """
    if size <= 0:
        return None
    return round(size / 1e9, 1)


def _cached_label(repo_id: str) -> str:
    """A cached repo's display name: the repo's own name, without the owner.

    Not a hand-written label — nobody wrote one, and inventing prose from a repo id
    would read as curation that isn't. The apps render `label || id`, so this only
    has to be the shorter true thing: "Qwen3-8B-MLX-4bit" rather than
    "mlx-community/Qwen3-8B-MLX-4bit" in a dropdown that is already narrow.
    """
    return repo_id.rsplit("/", 1)[-1] or repo_id


def _cached_order(model: CachedModel):
    """catalog.py's ordering rule, applied to the cached tail: SMALLEST FIRST, and
    a repo with NOTHING MEASURABLE sorts LAST rather than into the smallest slot.

    Over raw BYTES rather than the rounded `size_gb`: a 40MB repo and a 900MB one
    both round to 0.0 at one decimal, and a display precision must not be what
    decides an order.
    """
    return (model.size <= 0, model.size, model.repo_id)


def _unsupported_downloads() -> list[dict]:
    """Model repos on this disk that NO capability can load, with the reason.

    **The listing exists because dropping them was the wrong silence.** Every
    picker reads `capabilities[]`, and a repo with no capability is in none of
    those lists — so a user who downloaded a text-to-speech model, a depth
    estimator or a symbolic-music policy watched it vanish from the Playground
    with nothing said. "You have this, and here is why there is no button" is a
    sentence only this side can write (`ai/tasks.py` writes it per task), and a
    page that omits the row answers the reader's actual next question — where
    did my download go — with nothing at all.

    NOT in `capabilities[]` as a fake group, and that is deliberate: every app
    reading this payload maps `models[]` and offers what it finds, so a row in
    there is a row something will try to load. This is a separate key, which an
    older client ignores and a picker has to opt into showing.

    Sorted like the cached tail everywhere else — smallest first, unmeasurable
    last. Components, datasets, Spaces and half-finished fetches never reach
    here; `cached_models` has already dropped them, and none of them is a model
    somebody chose.
    """
    return [
        {
            "id": model.repo_id,
            "label": _cached_label(model.repo_id),
            "size_gb": _cached_size_gb(model.size),
            # What it IS, when anything said — the label a card prints beside
            # the reason. None for a repo nothing could identify, where the
            # reason is empty too and the row says only "on this disk,
            # unrunnable", which is the honest whole of what we know.
            "task": model.task,
            # "no-runner" or "unknown" — never "supported", by construction:
            # a supported task with a readable format has a capability and is
            # in `capabilities[]` instead.
            "support": model.support,
            "reason": model.reason,
        }
        for model in sorted(cached_models(), key=_cached_order)
        if model.capability is None
    ]


def _catalog_with_downloads() -> list[dict]:
    """`catalog.describe()`, plus the models this disk actually has.

    **The bug this closes (D323).** A user searches the Hub on the Discover tab,
    presses Download, and the bytes land in the cache — and the model then appears
    in NO page's picker, because every page reads `fused.ai.models.catalog()` and
    that was the curation and nothing else. Three shipped apps read this one payload
    the same way (find the capability, map `models[]` for `{id, label, size_gb,
    note}`, select `default`), so putting the downloaded repos INTO `models[]` fixes
    all three with no change to any of them.

    **The union lives here, not in `catalog.py`.** That module is curation — "Curated,
    not fetched" is its first heading — and it has no filesystem awareness at all;
    teaching it to scan the hub cache would put a disk walk under `default_for()`,
    which is called on the hot path of a bare `fused.ai.image()`. This router already
    imports the cache reading for `_inferred_capability`, so the join costs it one
    more import and costs `catalog.py` nothing.

    **Every list here is per RUNNER, and the cached half obeys that too.** A
    capability is NOT enough to put a repo in a list: `catalog.SUGGESTIONS` is keyed
    by runner precisely because one capability's backends read mutually unloadable
    formats (AI-11a), and a cached repo injected on its capability alone would break
    that invariant inside the very same array. `openai/whisper-large-v3` is a speech
    model that neither shipping speech runner reads; `mlx-community/Qwen3-8B-MLX-4bit`
    is a text model that llama.cpp cannot open, so on a Mac switched to
    llama.cpp it is an unusable download. So the test is the FORMAT's own answer —
    is the runner this row resolved among the ones that would accept this snapshot
    (`CachedModel.loaders`)? — and anything else is left out of `models[]` entirely.

    **Left out, not flagged.** `models[]` has no `available`/`reason` field and every
    consumer reads it as "things I may offer"; adding one would mean every existing
    picker keeps offering the unloadable repo until it learns a new key, which is the
    failure being fixed rather than a fix. The repo is not hidden — the AI Models
    page's Local tab is the surface for "what is on my disk", it lists the repo, and
    it already prints WHICH engine reads it and what stands in the way ("text
    generation is set to llama.cpp (CPU), which does not read this format — switch
    it on the Engines tab"). A picker cannot say that; a card can.

    **Cached entries are APPENDED.** `entry.default`, `catalog.default_for()` and
    `catalog.for_capability()` keep answering over the curated list alone — read
    catalog.py's docstring on why smallest-first with the default at position 0 is
    deliberate. A bare `fused.ai.transcribe()` therefore still loads a vetted model
    rather than whatever 20GB experiment is on the disk, and the tail is sorted by the
    same rule so the two halves read as one list. **The one case where a cached entry
    reaches index 0 is a runner with no `SUGGESTIONS` key at all**, where there is
    nothing curated to put in front of it; `default` is then None, which is the
    honest answer, and `source` is on every entry so that a consumer inventing a
    `models[0]` fallback can refuse an uncurated one. Read `default`, never `models[0]`.

    Two additive fields make the states tellable apart without a second request:
    `source` ("curated" | "cached") says which half an entry came from, and
    `downloaded` says whether it is on this disk — so a curated entry can be marked
    downloaded and is not duplicated as a cached one. `loaded` is read live from the
    supervisor rather than from the memoised scan, because residency changes on a
    second's notice and the disk inventory does not.

    `recommended` is a THIRD, and it is the curation's own second axis rather than
    anything this join computes: True on the subset of curated entries a person
    marked as a first thing to try, always False on a cached one — nobody wrote a
    recommendation for a repo the user found themselves, the same reason `note` is
    null there. Normalised to a bool on both halves so a consumer can filter on it
    without reading absence as an answer; the Playground draws
    recommended-or-on-disk and every other picker keeps reading the whole list
    (D425).

    **One runner's curated ids are FILENAMES, not repo ids, and this function is
    where that stops being invisible.** `formats.GGUF_RECIPES` keys
    `llamacpp-text`'s catalog entries by the GGUF's own filename — the module
    docstring there explains why a repo id alone cannot address one of a
    repo's several curated quantizations — so `entry["id"] in on_disk`
    (a set of REPO ids) can never be true for one of those entries: a
    downloaded `Qwen3.5-9B-Q4_K_M.gguf` showed "Download" forever, while the
    same bytes appeared a SECOND time as a plain "cached" row keyed by
    `unsloth/Qwen3.5-9B-GGUF`, whose Load button then failed (that repo id is
    not itself a `GGUF_RECIPES` key). `hub_cache.is_downloaded` resolves a
    filename-keyed entry through the recipe's `(repo, file)` pair and
    `CachedModel.files` (the snapshot's own filenames) instead of a set of repo
    ids alone — and it lives THERE rather than here because the Benchmark tab's
    "is this model on this machine" guard needs the identical answer, and the
    copy it wrote instead admitted every curated id;
    `curated_repo_ids` then removes the SAME repo from the "cached"
    tail below whenever any of ITS curated entries resolved as downloaded, so
    the two halves cannot show the one download twice under two different ids.

    **`repo` puts that same translation ON THE WIRE, because a client cannot
    redo it.** Every entry carries the repo id whose cache folder holds it —
    equal to `id` everywhere but a filename-keyed one, where it is the recipe's
    `repo`. The Local tab has the identical duplicate to avoid and no way to
    avoid it: its "do I already have a card for this" map is keyed by repo id
    (`/api/ai-models`, the page's own walk), so `LFM2.5-1.2B-Instruct-Q4_K_M.gguf`
    never matched `LiquidAI/LFM2.5-1.2B-Instruct-GGUF` and the row kept its
    Download button beside its own finished disk card — the same "Download
    forever" this docstring describes, one layer up and still open. It cannot
    read `downloaded` instead (`mergeSections` states why: two definitions of
    on-disk on one page are two moments they were true), so what it needs is the
    IDENTITY, not the verdict. A field rather than a client-side table for the
    reason `GGUF_RECIPES` is server-side at all: which repo publishes a curated
    quantization is the curation's fact, and a second copy in TypeScript is one
    that goes stale the next time a recipe's repo changes.
    """
    rows = catalog.describe()
    cached = cached_models()
    resident = supervisor.resident_models()
    by_capability: dict[str, list] = {}
    for model in cached:
        if model.capability is None:
            # No capability could be inferred, and inventing one is how a load came
            # to send a diffusion repo to mlx-lm (D321). The repo is still visible
            # on the AI Models page, which is the surface for "what is on my disk".
            continue
        by_capability.setdefault(model.capability, []).append(model)

    def _downloaded(entry_id: str) -> bool:
        # `hub_cache.is_downloaded`, not a local reading of the same two facts:
        # the Benchmark tab's server side needs the identical answer, and the
        # copy it wrote instead got the curated half wrong (see that function).
        # `cached` is passed so a row of twenty entries pays for the scan once.
        return is_downloaded(entry_id, cached)

    def _repo_of(entry_id: str) -> str:
        # The repo id that ADDRESSES this entry's bytes, which is the entry id
        # itself for every runner but the filename-keyed one. One lookup in the
        # curation's own table, so no consumer has to keep a second copy of it.
        recipe = formats.GGUF_RECIPES.get(entry_id)
        return recipe["repo"] if recipe else entry_id

    for row in rows:
        curated = [
            dict(entry, source="curated", downloaded=_downloaded(entry["id"]),
                 repo=_repo_of(entry["id"]),
                 loaded=entry["id"] in resident,
                 # Normalised to a bool HERE rather than left absent, because
                 # the curation writes it opt-in (`catalog.py`) and a consumer
                 # that filters on it must not have to tell "not recommended"
                 # from "an older server that had never heard of the field".
                 recommended=bool(entry.get("recommended")))
            for entry in row["models"]
        ]
        curated_ids = {entry["id"] for entry in curated}
        # Repo ids already spoken for by a DOWNLOADED filename-keyed curated
        # entry — see the docstring. Read off `repo` (post-`_downloaded`) rather
        # than re-checking `formats.GGUF_RECIPES` here, so this stays correct for
        # any future runner whose ids work the same way without this function
        # needing to know which one. `repo != id` IS "filename-keyed", by
        # `_repo_of`'s own definition, and is why that translation is a field
        # rather than a second lookup here.
        curated_repo_ids = {
            entry["repo"] for entry in curated
            if entry["downloaded"] and entry["repo"] != entry["id"]
        }
        extra = [
            {
                "id": model.repo_id,
                "label": _cached_label(model.repo_id),
                "size_gb": _cached_size_gb(model.size),
                # No note, and not an invented one: a note in this payload is a
                # person's frank sentence about a trade-off, and null says "no such
                # sentence exists" where prose generated from a repo id would
                # claim one does.
                "note": None,
                "source": "cached",
                "downloaded": True,
                # Its own repo id, so `repo` is on EVERY entry rather than on
                # the half that needed it: a consumer reading it only where it
                # differs from `id` is a consumer that has to know which half it
                # is holding, which is the distinction this field exists to
                # remove.
                "repo": model.repo_id,
                # Never recommended: `recommended` is a curator's mark and
                # nobody has made one about a repo the user found themselves.
                # It costs the Playground nothing — a cached entry is on the
                # disk by definition, and downloaded is the other half of what
                # that sidebar draws.
                "recommended": False,
                "loaded": model.repo_id in resident,
            }
            for model in sorted(by_capability.get(row["capability"], ()), key=_cached_order)
            if model.repo_id not in curated_ids
            and model.repo_id not in curated_repo_ids
            # The per-runner invariant, enforced: this row's list belongs to the
            # runner `describe()` resolved, and a repo whose format that runner does
            # not read has no business in it. See the docstring for both real repos
            # this drops and why they are dropped rather than flagged.
            and row["runner"] in model.loaders
        ]
        row["models"] = curated + extra
        for entry in row["models"]:
            entry["fit"] = _fit_verdict(entry.get("size_gb"))
            # Whether this one can be handed a base image to EDIT (AI-9f) —
            # computed per entry on BOTH halves, because a cached mflux repo
            # with no edit variant is as unable to edit as a diffusers one and
            # a picker filtering on absence would offer it anyway.
            entry["acceptsImage"] = _accepts_image(
                row["capability"], row["runner"], entry["id"])
            # The embeddings pair (SPEC §40): whether this entry may be handed
            # image PATHS, and which retrieval prompt scheme its texts get.
            # Computed per entry on BOTH halves for `acceptsImage`'s reason — a
            # cached prose encoder is as unable to read an image as a curated
            # one, and a picker filtering on absence would offer it anyway.
            entry["acceptsPaths"] = _accepts_paths(row["capability"],
                                                   entry["id"])
            entry["promptScheme"] = _prompt_scheme(row["capability"],
                                                   entry["id"])
    return rows


@functools.lru_cache(maxsize=1)
def _machine_ram_gb() -> float | None:
    """Total physical memory in decimal GB, or None where it cannot be read.

    Stdlib only — psutil lives in the runner venvs, not this one (AI-2's rule:
    the server's environment stays a file explorer's). `sysconf` covers macOS
    and Linux; Windows answers through GlobalMemoryStatusEx. Cached forever:
    the machine's RAM does not change under a running server, and this is read
    per catalog request.
    """
    try:
        if hasattr(os, "sysconf") and os.sysconf_names.get("SC_PHYS_PAGES"):
            return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except (ValueError, OSError):
        pass
    try:  # pragma: no cover - the Windows branch
        import ctypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / 1e9
    except Exception:  # noqa: BLE001 - absent windll off Windows, and none of it is fatal
        pass
    return None


def _fit_verdict(size_gb: float | None) -> str | None:
    """Will this model sit comfortably on THIS machine — "easy", "tight" or "no".

    The question a newcomer is actually asking of the size figure, answered
    with the size figure's own crude honesty: the download is roughly what the
    weights occupy resident (every curated entry is quantized), and a model
    whose weights take over half the machine's memory shares the rest with the
    OS, the browser and this server — a swap storm read as "the app hung"
    (AI-4's arithmetic). Under a quarter is comfortable; between the two is
    real but tight. A judgement, not a measurement — the page words it as one.

    None when either half is unknown: a verdict invented over a missing size
    is the same lie the "—" size cell exists to avoid.
    """
    ram = _machine_ram_gb()
    if size_gb is None or ram is None or ram <= 0:
        return None
    if size_gb <= ram * 0.25:
        return "easy"
    if size_gb <= ram * 0.5:
        return "tight"
    return "no"


def _accepts_image(capability: str, runner_code: str | None, model_id: str) -> bool:
    """Can `model_id` be handed an image on this machine — to EDIT (AI-9f) or,
    since the mlx_text runner switched to mlx-vlm, to be ASKED ABOUT (AI-11j)?

    **No longer image-capability-only.** SPEC AI-11j originally read this
    field as True only where the model could be an EDIT base, because mlx-lm
    loaded only a checkpoint's language tower and the vision half of every MLX
    text model was dead weight it never touched. `mlx_text/worker.py` now
    loads through mlx-vlm instead (`lazy=True`), which CAN read that tower —
    on demand, only when a request actually attaches an image — so a
    TEXT_GENERATION entry is a real candidate here too, provided the
    checkpoint it names actually has a tower to feed one to.

    Two branches, one principle kept from before: **computed, never curated,
    and False rather than True-by-vacancy.**

    - IMAGE_GENERATION — unchanged, and still a mirror of `api_ai_image`'s own
      two refusals in the same order, so a picker's attach button and the
      route that would 400 the resulting request cannot disagree: the ENGINE
      (`engine_options` is the one place that says which backends honour
      `image`) and then the MODEL (mflux additionally needs an edit variant
      class named for the repo, `formats.mflux_edit_recipe`, since a repo can
      render and not edit).
    - TEXT_GENERATION — True only when the resolved runner is `mlx-text` (the
      one runner here that reads a checkpoint through mlx-vlm at all — a
      llama.cpp GGUF text model has no vision tower to speak of and must come
      back False the same as before) AND `hub_cache.has_vision_tower` finds a
      `vision_config`/`image_token_id` in the cached checkpoint's own
      `config.json`. Read straight off disk, with no model load involved —
      an attach button whose request then 400s is exactly the failure this
      field exists to prevent, so "cannot tell" answers False rather than
      guessing True.
    - Every other capability: False. `engine_options` is an exception list
      for the image route alone, so treating "refuses nothing" as evidence
      would have every non-image, non-mlx-text model in the payload claiming
      it takes a photo.
    """
    if runner_code is None:
        return False
    if capability == registry.IMAGE_GENERATION:
        try:
            engine_options.unsupported_or_raise(runner_code, image="probe")
        except ValueError:
            return False
        if runner_code == "mflux-image":
            return formats.mflux_edit_recipe(model_id) is not None
        return True
    if capability == registry.TEXT_GENERATION and runner_code == "mlx-text":
        return has_vision_tower(model_id)
    return False


def _accepts_paths(capability: str, model_id: str) -> bool:
    """Can `model_id` be handed image PATHS to embed (SPEC §40)?

    `_accepts_image`'s sibling for the embeddings capability, and it keeps that
    function's two rules: **computed, never curated, and False rather than
    True-by-vacancy.** A dual encoder (SigLIP, CLIP) has a vision tower and a
    joint space, so a photo and a sentence are comparable; a prose encoder has
    one tower and handing it pixel values embeds nothing.

    Fails CLOSED — `hub_cache.embed_family` is three-valued and only `"dual"`
    answers True, so a model with no snapshot on disk yet reports False and the
    Playground draws no image mode for it. An affordance whose request then 400s
    is exactly the failure this field exists to prevent, and that is the same
    trade `_accepts_image` makes for the TEXT_GENERATION half.

    The ROUTE deliberately does NOT mirror this reading — see
    `hub_cache.embed_family`'s own docstring for the asymmetry and why it is the
    safe direction: the route refuses only on positive evidence of a text
    encoder, so a `paths` call on a cold dual encoder still answers
    `model_loading` and starts the download rather than being refused for a
    config file that is not there yet.

    False for every capability but embeddings, for `_accepts_image`'s reason:
    treating "no evidence against" as evidence would have every text and speech
    entry in the payload claiming it takes a photo.
    """
    if capability != registry.EMBEDDINGS:
        return False
    return embed_family(model_id) == "dual"


def _prompt_scheme(capability: str, model_id: str) -> str | None:
    """Which retrieval prompt scheme `model_id` wants, or None where the
    question does not apply (SPEC §40).

    `formats.text_embed_scheme`'s answer, published so the Playground can draw
    a query/document toggle only for a model the route will actually accept
    `kind` for — and so a reader can SEE which convention was applied, since a
    prefix is invisible in the vectors that come back.

    **`"none"` comes back as None on the wire**, not as the string. `"none"` is
    a real scheme internally (embed verbatim, both sides) but on the wire it
    means "this model has no convention, so `kind` is a parameter with nothing
    to do" — and a frontend testing `promptScheme` for truthiness must get the
    same answer as the route's own refusal, which is keyed on exactly this.

    None for every capability but embeddings: a chat model has prompts too, and
    they are nothing to do with this table.
    """
    if capability != registry.EMBEDDINGS:
        return None
    scheme = formats.text_embed_scheme(model_id)
    return scheme if scheme != "none" else None


@router.get("/api/ai/catalog")
def api_ai_catalog():
    """Suggested models per capability, plus what is on this disk.

    Sync `def`: `cached_models()` walks the hub cache (memoised, see there), so it
    belongs in the threadpool rather than on the event loop.
    """
    return {"capabilities": _catalog_with_downloads(),
            # Everything else on this disk, with the reason it is not above.
            "unsupported": _unsupported_downloads(),
            "ramGb": _machine_ram_gb()}


@router.post("/api/ai/runtime/load")
def api_ai_load(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    model = _model_of(body)
    if not model:
        return _error("'model' must be a Hugging Face repo id", status=400)
    capability, refusal = _resolve_capability(body, model)
    if refusal is not None:
        return refusal
    try:
        return supervisor.load(model, capability)
    except supervisor.SupervisorError as e:
        # 409, not 500: the request was well-formed and the answer is a fact
        # about this machine ("needs Apple Silicon"), not a server fault.
        return _error(str(e), status=409)


@router.post("/api/ai/runtime/unload")
def api_ai_unload(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    model = _model_of(body) or None
    capability = body.get("capability") if isinstance(body.get("capability"), str) else None
    if model is None and capability is None:
        return _error("name a 'model' or a 'capability' to unload", status=400)
    # Matching `cancel`, 45 lines below: an unrecognised capability is a 400,
    # not a no-op. Without this, a typo went straight to `supervisor.unload()`,
    # which filters workers by equality and answers `bool(targets)` — so
    # `{"stopped": false}` is exactly what a correct request against an idle
    # machine also answers, and the caller cannot tell the two apart. Only
    # checked when `capability` is not None, so the `model`-only form is
    # unaffected.
    if capability is not None and capability not in registry.capabilities():
        return _error(f"unknown capability {capability!r}", status=400)
    stopped = supervisor.unload(model=model, capability=capability)
    return {"stopped": stopped, **supervisor.describe()}


@router.post("/api/ai/runtime/download")
def api_ai_download(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Fetch a model's weights without loading them.

    Same machinery as a load — the runner's worker is the only thing that knows
    how to fetch for its own format — stopped one step earlier. That is why this
    is not `huggingface_hub.snapshot_download` called from here: a GGUF image
    model and an MLX text model do not download the same set of files, and the
    runner is where that knowledge already lives.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    model = _model_of(body)
    if not model:
        return _error("'model' must be a Hugging Face repo id", status=400)
    capability, refusal = _resolve_capability(body, model)
    if refusal is not None:
        return refusal
    try:
        return supervisor.load(model, capability, weights_only=True)
    except supervisor.SupervisorError as e:
        return _error(str(e), status=409)


@router.post("/api/ai/cancel")
def api_ai_cancel(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Stop the generation in flight on a resident model.

    Not the same as unloading: the weights stay, so the next message starts
    answering immediately. A chat box needs this — a model that has decided to
    write nine hundred tokens is otherwise something you can only wait out or
    unload — and the supervisor could already do it; only the route was missing.

    False when there was nothing to stop, which is not an error: a Stop pressed
    just as the last token arrived should be a no-op, not a failure.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    capability = body.get("capability")
    if capability is not None and capability not in registry.capabilities():
        return _error(f"unknown capability {capability!r}", status=400)
    return {"cancelled": supervisor.cancel_generation(
        capability or registry.TEXT_GENERATION)}


@router.post("/api/ai/image")
def api_ai_image(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Render one image. Returns everything about it except the pixels.

    **Job-backed, like a download, and for the same reason**: this runs for
    minutes. The reply comes back immediately with a `jobId` to watch — and with
    the PATH and the SEED already decided, which is what makes a second lookup
    unnecessary. The server picks both: it owns where user files go, and a seed
    the caller did not supply has to be recorded somewhere or the render is not
    reproducible. Nothing about the finished image needs a second endpoint, and
    the job record needs no result field.

    The file is written by the worker and read back through `/api/fs/raw`, the
    same door every other local file goes through — `fused.ai.image()` hands the
    page a ready-made URL for it.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    # Checked first, so an unknown option is reported even when another field
    # is also wrong — see `_reject_unknown`. The wider, SERVER set: `base` is
    # bridge-injected, same asymmetry as `/api/ai/transcribe`.
    rejection = _reject_unknown(body, _IMAGE_SERVER_OPTIONS, "/api/ai/image")
    if rejection is not None:
        return rejection

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("'prompt' must be a non-empty string", status=400)

    model = _model_of(body) or catalog.default_for(registry.IMAGE_GENERATION)
    if not model:
        # A machine with no image runner has no default either, and answering
        # about the CATALOG would bury the reason: "the Diffusers runner is not
        # built yet" is something a user can act on, "no image model is
        # configured" is not. The runner's reason wins where there is one.
        return _error(registry.unavailable_reason(registry.IMAGE_GENERATION)
                      or "no image model is configured", status=409)

    # `image` (SPEC AI-9f): edit a base image instead of rendering from the
    # prompt alone. mflux-only — every diffusers image code refuses it, since
    # that pipeline's SIGNATURE is known (`Flux2KleinPipeline.__call__` takes
    # `image` first, defaulting to None for a plain render) but whether it
    # RENDERS a correct edit is not, on any machine this app has run on
    # (D413's own failure mode, reproduced inside mflux itself during the
    # gate run: an image argument accepted and silently ignored).
    image = body.get("image")
    image_path = None
    if image is not None:
        # Decision 4: one image, a single string. An array or any other type
        # is a 400 rather than a guess at what the first (or last) element
        # was meant to mean — multi-reference conditioning is unverified.
        if not isinstance(image, str) or not image.strip():
            return _error(
                "'image' must be the path to one base image, as a single "
                "string — fused.ai.image({image}) edits exactly one image, "
                "so an array or any other type is rejected rather than "
                "guessed at", status=400)
        # Refused HERE, before a job row opens: `engine_options.py`'s own
        # rule is to refuse at the endpoint AND again in the worker, and the
        # endpoint is where the RESOLVED runner is already known — the one
        # that will actually serve this request regardless of which model id
        # was named, since mflux/diffusers is an Engines-tab choice, not a
        # per-model one.
        active_runner = registry.for_capability(registry.IMAGE_GENERATION)
        if active_runner is not None:
            try:
                engine_options.unsupported_or_raise(active_runner.code, image=image)
            except ValueError as e:
                return _error(str(e), status=400)
            # The ENGINE can edit (mflux), but this specific MODEL may not
            # have an edit variant class named for it — `formats.
            # MFLUX_VARIANTS` accepts a repo for plain generation with no
            # promise it also appears in `MFLUX_EDIT_VARIANTS`. Checked here,
            # before a job row opens, for the identical reason the engine
            # refusal two lines up is: without it, a repo this runner cannot
            # edit with would still pass `_require_fused`, open a job, and
            # potentially trigger a venv build and a multi-GB download
            # before the worker's own `_build_variant` finally raises — the
            # exact cost this whole block exists to avoid paying first.
            if (active_runner.code == "mflux-image"
                    and formats.mflux_edit_recipe(model) is None):
                return _error(
                    f"{model} has no edit variant this runner knows how to "
                    "build — it can render from a prompt with this model "
                    "but not edit an existing image with it. Try "
                    "mlx-community/FLUX.2-Klein-4B-4bit.", status=400)
        # Page-relative, the same rule `/api/ai/transcribe`'s `path` follows
        # (RH-1): a relative `image` resolves against the directory of
        # `base`, the calling page's own absolute path. An absolute `image`
        # ignores `base`, as it does there. No allowlist, for the identical
        # reason `api_ai_transcribe` gives: `/api/fs/raw` already serves any
        # absolute path on this machine, so the only checks are the ones a
        # typo deserves.
        image_path = os.path.expanduser(image.strip())
        base = body.get("base")
        if not os.path.isabs(image_path):
            if not isinstance(base, str) or not os.path.isabs(base):
                return _error(
                    "'image' must be absolute, or relative to a page named "
                    "by 'base'", status=400)
            image_path = os.path.join(os.path.dirname(base), image_path)
        image_path = os.path.abspath(image_path)
        if not os.path.exists(image_path):
            return _error(f"no such file: {image_path}", status=400)
        if not os.path.isfile(image_path):
            return _error(f"not a file: {image_path}", status=400)

    # Decision 1: an edit's default size comes from the BASE IMAGE, using the
    # prototype's own arithmetic (confirmed as written by the gate run). Any
    # explicit `width`/`height` still wins — this only changes the DEFAULT.
    default_width = default_height = 1024
    if image_path is not None:
        edit_size = _edit_default_size(image_path)
        if edit_size is not None:
            default_width, default_height = edit_size

    # An edit's defaults are the PROTOTYPE's own (4 steps, guidance 1.0), not
    # the 28/4.0 shared between the generate paths of both image engines
    # (`mflux_image/worker.py:generate`'s own comment) — applying the
    # generate defaults to an edit silently would be a real quality
    # regression (mflux's own denoising mechanism for editing wants far
    # fewer steps and far less guidance than a from-scratch render), and
    # changing them for this one mode is a documented choice rather than an
    # unnoticed one.
    default_steps = 4 if image_path is not None else 28
    default_guidance = 1.0 if image_path is not None else 4.0
    # `is None or == ""`, NOT `body.get(...) or default` — the falsy-`or`
    # form silently replaced an explicit `steps: 0` or `guidance: 0` with
    # the default, clamping never got a chance to run on the caller's own
    # 0 at all. This predates this PR (the base commit already read `body.
    # get("steps") or 28`) — it is fixed here because two DIFFERENT
    # defaults depending on mode is what makes the silent substitution
    # obvious rather than a one-in-a-million edge case: an edit whose
    # caller typed `steps: 0` meaning "clamp me to the floor" got a 4- or
    # 28-step render instead, depending on which mode the same bug fired
    # under. `None`/`""` are the two spellings of "I did not say" this
    # endpoint already reads that way for other fields (`diarize.speakers`,
    # D318) — a JSON `null` and an empty form field, not a value someone
    # meant.
    steps_in = body.get("steps")
    if steps_in is None or steps_in == "":
        steps_in = default_steps
    try:
        steps = max(1, min(_MAX_STEPS, int(steps_in)))
    except (TypeError, ValueError):
        return _error("'steps' must be a number", status=400)
    # (#732's own independent fix for this exact `guidance` case merged
    # while this branch was in flight — `is None` only, no `""` and no
    # per-mode default; superseded here by the fuller fix above, which
    # both bugs needed anyway.)
    guidance_in = body.get("guidance")
    if guidance_in is None or guidance_in == "":
        guidance_in = default_guidance
    try:
        guidance = max(0.0, min(20.0, float(guidance_in)))
    except (TypeError, ValueError):
        return _error("'guidance' must be a number", status=400)
    # A seed the caller did not choose is chosen HERE and reported back, so
    # "make that one again" is always possible — a seed invented inside the
    # worker and never surfaced would make every unseeded image unrepeatable.
    try:
        seed = int(body["seed"]) if body.get("seed") is not None else secrets.randbelow(_MAX_SEED)
    except (TypeError, ValueError):
        return _error("'seed' must be a whole number", status=400)
    seed = max(0, min(_MAX_SEED, seed))

    uid = secrets.token_hex(6)
    job = supervisor.image_job_id(uid)
    images = _images_dir()
    # Before the render, not after: a preview orphaned by a killed worker has no
    # unwind coming that would clean it up, so the next request is the only
    # thing that will ever look. See `_sweep_previews`.
    _sweep_previews(images)
    # Time-ordered and unique: the folder sorts chronologically in the explorer,
    # and two renders in the same second still land on different files.
    path = os.path.join(images, f"{time.strftime('%Y%m%d-%H%M%S')}-{uid}.png")

    request = {
        "prompt": prompt.strip(),
        "width": _side(body.get("width"), default_width),
        "height": _side(body.get("height"), default_height),
        "steps": steps,
        "guidance": guidance,
        "seed": seed,
        "out": path,
        # …and where the picture-in-progress goes while it denoises, so a page
        # has something to show through a render that takes minutes. Derived
        # through `preview.preview_path` rather than spelled here, for the
        # reason `outPartial` is: the worker that writes this file and the reply
        # that advertises it must name the same one, and a second spelling of
        # the suffix is how they come to disagree. A sibling of the image for
        # the same reason the transcript's three are siblings — the server owns
        # where user files go.
        #
        # Sent unconditionally. Whether a preview HAPPENS is the worker's answer
        # (it needs a fitted projection for the model's latent space), and a
        # route that tried to predict it would need this process to know what a
        # runner venv it cannot import has a matrix for.
        "outPreview": preview.preview_path(path),
    }
    if image_path is not None:
        # Absent entirely rather than `None` when there is no base image —
        # `mflux_image/worker.py`'s `generate()` reads its presence to decide
        # the MODE (edit vs. plain generate), and `body.get("image")` answers
        # that identically for "the key is missing" and "the key is None",
        # but a worker that ever grew a stricter check should not have to
        # tell those two apart because this route always sent one.
        request["image"] = image_path
    try:
        supervisor.start_image(model, request, job)
    except supervisor.SupervisorError as e:
        # 409 for the same reason a load does: the request was well-formed and
        # the answer is a fact about this machine, not a server fault.
        return _error(str(e), status=409)
    # The settled request, not the one that came in: `width` may have been
    # snapped, `steps` clamped, `seed` invented. A caller that echoes these back
    # gets the render it actually got, not the one it asked for. `out` is the
    # worker's field name for the same thing `path` is, so it is not repeated.
    reply = {
        "jobId": job,
        # Canonical, like every other path this API hands back (`previewPath`
        # below, and `/api/ai/transcribe`'s own `path`) — this goes back to a
        # page that will put it in a `/api/fs/raw` URL, and a Windows path that
        # reached it backslashed would not match what the shell stored for the
        # same file.
        "path": canonical_fs_path(path),
        # Canonical for the same reason. It is a promise about a PATH, not
        # about a file: a model with no fitted projection writes nothing there,
        # and `fused.ai.image` treats a missing preview as the ordinary case
        # rather than as an error.
        "previewPath": canonical_fs_path(request["outPreview"]),
        "model": model,
        "prompt": request["prompt"],
        "width": request["width"],
        "height": request["height"],
        "steps": steps,
        "guidance": guidance,
        "seed": seed,
    }
    if image_path is not None:
        # Echoed beside `path`, canonical for the identical reason: a caller
        # that passed a relative `image` can see which file it actually
        # resolved to.
        reply["image"] = canonical_fs_path(image_path)
    return reply


@router.post("/api/ai/video")
def api_ai_video(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Render one video (with audio). Returns everything about it except the
    bytes. `api_ai_image`'s twin — job-backed for the same reason, minus
    `guidance` (the engine is CFG-distilled) and `previewPath` (no live
    preview in this build), plus `frames`.

    The 409 case is the one this route has that the image route does not:
    video generation is the first capability with no "everywhere" row, so on
    anything but Apple Silicon this always answers with
    `registry.unavailable_reason` rather than ever reaching a default model.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    # Checked first, so an unknown option (`guidance`, say) is reported even
    # when another field is also wrong — see `_reject_unknown`.
    rejection = _reject_unknown(body, _VIDEO_OPTIONS, "/api/ai/video")
    if rejection is not None:
        return rejection

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("'prompt' must be a non-empty string", status=400)

    model = _model_of(body) or catalog.default_for(registry.VIDEO_GENERATION)
    if not model:
        # CORRECTED: this branch is dead in practice, exactly like the same
        # branch in `api_ai_image` above (`catalog.default_for` never gates
        # on availability -- only `catalog.describe`'s own `default` field
        # does that, a different function entirely -- and `SUGGESTIONS
        # ["ltx-video"]` is a hardcoded non-empty list, so `default_for`
        # always returns an id here whether or not this machine can run it).
        # Kept anyway, matching `api_ai_image`'s own choice: cheap
        # defensive code against a catalog that someday ships an empty
        # shortlist, not the mechanism this route actually relies on for
        # the 409. The REAL "needs Apple Silicon" answer, on a machine that
        # cannot serve this capability, comes from `start_video`'s own
        # `_runner_or_raise` below -- caught and turned into the same 409 a
        # few lines down.
        return _error(registry.unavailable_reason(registry.VIDEO_GENERATION)
                      or "no video model is configured", status=409)

    # The runner that will actually SERVE this request — resolution is by
    # CAPABILITY, not by `model` (`registry.py`'s own module docstring), so
    # this is the same call `start_video`'s `_runner_or_raise` makes a few
    # lines down, made here too because the request SHAPE (frame grid,
    # canvas/step defaults) is that runner's fact, not the route's own.
    # `None` when nothing can serve the capability at all — already answered
    # with a 409 above via `catalog.default_for`'s dead branch, or about to
    # be via `start_video`'s own error below; `video_traits_for` handles
    # `None` by falling back to the shipping runner's own numbers.
    serving_runner = registry.for_capability(registry.VIDEO_GENERATION)
    traits = registry.video_traits_for(serving_runner.code if serving_runner else None)

    # **Naming a model explicitly does NOT pick its runner.** Resolution is
    # by CAPABILITY plus stored preference (`registry.resolve`), never by
    # `model` — `start_video`'s own `_runner_or_raise` never reads it either.
    # So naming a repo that some OTHER video runner reads would build and
    # start the resolved worker against it anyway, raising deep inside
    # `load()` after a (cheap, listing-only) Hub round trip — a confusing
    # failure for someone who deliberately named the model they already have
    # on disk. Refused here instead, naming the place a different engine IS
    # reachable: the Engines tab, which is exactly the switch
    # `registry.resolve` already honours (see that module's own docstring).
    #
    # **CURRENTLY UNREACHABLE, and kept deliberately.** D468 dropped
    # `h3-video`, leaving one video runner, and `formats.loaders` no longer
    # names any video runner but `ltx-video` — so `runner_code !=
    # serving_runner.code` cannot hold today. The guard is generic over
    # runners rather than about those two specifically, and it is what a
    # second video engine's own arrival would otherwise have to remember to
    # add back; the same argument `formats.py`'s withdrawn-runner early
    # returns make for themselves. Silent for anything not already cached —
    # there is no
    # format evidence to refuse on without a network call this route has
    # never made, and an uncached id is the ordinary "let the runner's own
    # `load()` refusal explain it" path every other capability already
    # relies on.
    if serving_runner is not None:
        reading = cached_capability(model)
        if (reading.cached and reading.capability == registry.VIDEO_GENERATION
                and reading.runner_code is not None
                and reading.runner_code != serving_runner.code):
            other = registry.by_code(reading.runner_code)
            other_name = other.short if other is not None else reading.runner_code
            return _error(
                f"{model} is an {other_name} model, and video generation is "
                f"set to {serving_runner.short}, which does not read this "
                f"format — switch the video engine to {other_name} on the "
                f"Engines tab, or name a model {serving_runner.short} reads.",
                status=409)

    try:
        steps = max(_MIN_VIDEO_STEPS,
                    min(_MAX_VIDEO_STEPS, int(body.get("steps") or traits.default_steps)))
    except (TypeError, ValueError):
        return _error("'steps' must be a number", status=400)
    frames = _snap_frames(body.get("frames"), traits)
    # A seed the caller did not choose is chosen HERE and reported back, so
    # "make that one again" is always possible — same rule `/api/ai/image` uses.
    try:
        seed = int(body["seed"]) if body.get("seed") is not None else secrets.randbelow(_MAX_SEED)
    except (TypeError, ValueError):
        return _error("'seed' must be a whole number", status=400)
    seed = max(0, min(_MAX_SEED, seed))

    # The serving engine's own default canvas (`traits.default_width/height`
    # — VERIFIED per-engine: LTX's own CLI `--width`/`--height` for
    # `ltx-video`). A bare call renders at the
    # shape the ENGINE is tuned for, the same way the image route's
    # 1024x1024 default matches its own pipelines' square default rather
    # than an arbitrary size. The side snap and pixel clamp below stay
    # shared across every engine — see `_MIN_VIDEO_SIDE` and friends above.
    width = _video_side(body.get("width"), traits.default_width)
    height = _video_side(body.get("height"), traits.default_height)
    width, height = _clamp_video_canvas(width, height)

    uid = secrets.token_hex(6)
    job = supervisor.video_job_id(uid)
    videos = _videos_dir()
    # Time-ordered and unique, like the image route's filename.
    path = os.path.join(videos, f"{time.strftime('%Y%m%d-%H%M%S')}-{uid}.mp4")

    request = {
        "prompt": prompt.strip(),
        "width": width,
        "height": height,
        "frames": frames,
        "steps": steps,
        "seed": seed,
        "out": path,
    }
    try:
        supervisor.start_video(model, request, job)
    except supervisor.SupervisorError as e:
        # 409 for the same reason a load does: the request was well-formed and
        # the answer is a fact about this machine, not a server fault.
        return _error(str(e), status=409)
    # The settled request, not the one that came in: `width`/`height` may have
    # been snapped, `frames` rounded to the engine's grid, `steps` clamped, `seed`
    # invented. A caller that echoes these back gets the render it actually
    # got, not the one it asked for.
    return {
        "jobId": job,
        # Canonical, like every other path this API hands back.
        "path": canonical_fs_path(path),
        "model": model,
        "prompt": request["prompt"],
        "width": width,
        "height": height,
        "frames": frames,
        "steps": steps,
        "seed": seed,
    }


#: Whisper's two directions. One flag to the model, so leaving `translate` out
#: would only buy a second PR later — but named rather than silently defaulted:
#: "translation" instead of "translate" would otherwise transcribe in the
#: original language and read as the model ignoring the request.
_TRANSCRIBE_TASKS = ("transcribe", "translate")


@router.post("/api/ai/transcribe")
def api_ai_transcribe(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Transcribe one audio or video file. Returns where the words will land.

    **Job-backed like `/api/ai/image`, not streamed like chat**, and for the
    same reason squared: a 90-minute recording is minutes of decoding. The reply
    comes back immediately with a `jobId` to watch and with the OUTPUT PATHS
    already decided, so nothing needs a second lookup — and the transcript is a
    file, so a page that navigated away mid-run still finds it.

    **The input is a path, and there is no allowlist here on purpose.**
    `/api/fs/raw` already serves any absolute path on this machine, because this
    app IS a local file explorer; the protection is D3/D36's `X-Fused` guard plus
    same-origin, and the worker's own port needs the token the supervisor
    generated. So the only checks are the ones a typo deserves: normalize, and
    refuse something missing or not a regular file before a job row opens.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    # Checked first, same as `api_ai_image` — see `_reject_unknown`. `base` is
    # in the server's accepted set (the bridge injects it) but not the
    # caller-facing one the bridge itself validates against.
    rejection = _reject_unknown(body, _TRANSCRIBE_SERVER_OPTIONS, "/api/ai/transcribe")
    if rejection is not None:
        return rejection

    source = body.get("path")
    if not isinstance(source, str) or not source.strip():
        return _error("'path' must be the audio or video file to transcribe", status=400)
    source = os.path.expanduser(source.strip())
    # Page-relative, the same rule `/api/fs/raw` follows (RH-1): a relative
    # `path` resolves against the directory of `base`, the calling page's own
    # absolute path. `fused.readFile("clip.m4a")` already means "beside this
    # page", so this call meaning "beside wherever the server was launched
    # from" would be a trap — a 400 naming a path the author never wrote, or,
    # if a same-named file happens to sit under that cwd, silently transcribing
    # the wrong recording. An absolute `path` ignores `base`, as it does there.
    base = body.get("base")
    if not os.path.isabs(source):
        if not isinstance(base, str) or not os.path.isabs(base):
            return _error(
                "'path' must be absolute, or relative to a page named by 'base'",
                status=400)
        source = os.path.join(os.path.dirname(base), source)
    source = os.path.abspath(source)
    if not os.path.exists(source):
        return _error(f"no such file: {source}", status=400)
    if not os.path.isfile(source):
        return _error(f"not a file: {source}", status=400)

    task = body.get("task") or "transcribe"
    if task not in _TRANSCRIBE_TASKS:
        return _error(
            f"'task' must be {_TRANSCRIBE_TASKS[0]!r} (same language) or "
            f"{_TRANSCRIBE_TASKS[1]!r} (into English), not {task!r}", status=400)

    # Speaker labels, and the optional count that fixes how many there are.
    # Checked BEFORE the model is resolved and before a job row exists, with the
    # other arguments a typo deserves an answer about — `runtime.js` refuses the
    # same request first, but the bridge is not the only door: a page can POST
    # here, and so can anything else on this machine holding the `X-Fused`
    # header.
    #
    # An ABSENT count is not a refusal (D318): `speakers_or_raise` answers None
    # and the worker's clustering estimates it. Only a bad explicit value —
    # `0`, `-1`, `true`, `"2"` — is a 400, and it still is.
    #
    # The rule comes from `runners/diarize.py`, the module the workers import
    # out of their own venvs, so the sentence a caller reads here is the same
    # sentence the worker would have raised. `bool(...)` and not `is None`: this
    # one has no true default to invert (D320's trap), it is off unless asked
    # for, so a JSON null and an absent key mean the same thing.
    diarizing = bool(body.get("diarize"))
    speakers = None
    if diarizing:
        try:
            speakers = diarize.speakers_or_raise(body.get("speakers"))
        except ValueError as e:
            return _error(str(e), status=400)

    # …and what the ENGINE that will serve this cannot do at all. D319 added a
    # third engine, Parakeet, that had no translate task, no `language`
    # argument and no text conditioning; D406 withdrew it, so the two engines
    # sharing THIS capability today (MLX Whisper, Faster Whisper) both answer
    # everything below and neither carries a row in `engine_options.
    # UNSUPPORTED` — that table is no longer empty overall (D432 gave the
    # diffusers image engines their own `image` refusal), just still empty
    # for transcribe — but the check stays, for the next transcribe engine
    # that needs one.
    #
    # Asked HERE, beside the other arguments a typo deserves an answer about,
    # because the answer is already available: `for_capability` is the same
    # resolution `supervisor._runner_or_raise` does a few lines down, so
    # nothing is guessed and nothing is resolved twice differently. The worker
    # refuses again on arrival — it is not the only door — but by then the user
    # has paid for a job row, possibly a venv build and a multi-gigabyte
    # download to be told something that was knowable before any of it.
    #
    # No runner at all is NOT a 400 here: that is the 409 below, which names
    # the machine's reason rather than the request's.
    # Per-word timings inside each segment (D392). `bool(...)` and not `is None`
    # for `diarize`'s reason: it has no true default to invert, it is off unless
    # asked for, so a JSON null and an absent key mean the same thing.
    #
    # **NOT refused when the engine has none, unlike everything below** (D392):
    # an engine without word timings leaves the `words` key off its segments,
    # which a caller reads directly, so the option is answered best-effort
    # instead of turning a page that runs on two machines into a page that has to
    # ask which one it is on. It is forwarded either way, and the worker honours
    # it or does not.
    wants_words = bool(body.get("words"))

    engine = registry.for_capability(registry.SPEECH_TO_TEXT)
    if engine is not None:
        try:
            engine_options.unsupported_or_raise(
                engine.code, task=task, language=body.get("language"),
                initial_prompt=body.get("initialPrompt"))
        except ValueError as e:
            return _error(str(e), status=400)

    model = _model_of(body) or catalog.default_for(registry.SPEECH_TO_TEXT)
    if not model:
        # See `api_ai_image`: no runner and no curated default are different
        # facts, and only the first one tells the user what to do.
        return _error(registry.unavailable_reason(registry.SPEECH_TO_TEXT)
                      or "no transcription model is configured", status=409)

    uid = secrets.token_hex(6)
    job = supervisor.transcribe_job_id(uid)
    # Named after the RECORDING, not the job: a folder of transcripts is
    # something a user browses, and `meeting-2024.json` is findable where a hex
    # id is not. Time-ordered and unique all the same, so the folder sorts
    # chronologically and two runs over the same file do not overwrite.
    # `out_base`, not `base`: `base` above is the calling PAGE's path, and two
    # different meanings on one name in one function is one edit away from
    # resolving an input against a transcripts directory.
    stem = os.path.splitext(os.path.basename(source))[0][:60]
    out_base = os.path.join(_transcripts_dir(),
                            f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}-{uid}")

    request = {
        "path": source,
        "model": model,
        # Absent means auto-detect, which is Whisper's own default and the right
        # one — a caller who knew the language would rarely be asking.
        "language": body.get("language") or None,
        "task": task,
        "initialPrompt": body.get("initialPrompt") or None,
        # The VAD skips silence, which on a recording with long gaps is most of
        # the wall clock. Off is for a caller who found it clipping speech.
        #
        # `is None` rather than a `get` default: a JSON null means "not
        # specified", and `bool(body.get("vad", True))` reads it as False — so
        # a page spreading an options object with an unset key got the opposite
        # of the documented default. `task` and `language` use `or` above and
        # are null-safe already; this was the one that inverted.
        "vad": True if body.get("vad") is None else bool(body.get("vad")),
        # Speaker labels on every segment, plus a top-level list of them in the
        # written JSON. Off unless asked for, so an existing caller's transcript
        # is byte-identical — and `speakers` is only sent when it is meaningful,
        # rather than as a null the worker would have to re-validate as absent.
        "diarize": diarizing,
        # Per-word timings inside each segment. Off unless asked for, so an
        # existing caller's transcript is byte-identical — it costs an extra
        # forward pass per decoded window and changes the decode path, which is
        # why it is asked for rather than always on (D392).
        "words": wants_words,
        # `speakers is not None`, not `diarizing`: a diarized run whose count
        # was left out sends no key at all rather than a null the worker would
        # have to re-read as absence. Same rule as before D318 made the count
        # optional — the key is present exactly when it carries a number.
        **({"speakers": speakers} if speakers is not None else {}),
        "out": out_base + ".json",
        "outText": out_base + ".txt",
        # …and where the segments land AS they are decoded, so a page has a
        # transcript to render before the run finishes. Derived through
        # `partial.partial_path` rather than spelled here, because the worker
        # that writes this file and the reply that advertises it must name the
        # same one — and a second spelling of the suffix is how they come to
        # disagree. A sibling of the other two for the same reason they are
        # siblings: the server owns where user files go.
        "outPartial": partial.partial_path(out_base + ".json"),
    }
    try:
        supervisor.start_transcribe(model, request, job)
    except supervisor.SupervisorError as e:
        return _error(str(e), status=409)
    return {
        "jobId": job,
        # Canonical, because these go back to a page that will put them in a
        # /api/fs/raw URL — a Windows path that reached it backslashed would not
        # match what the shell stored for the same file.
        "path": canonical_fs_path(source),
        "output": canonical_fs_path(request["out"]),
        "outputText": canonical_fs_path(request["outText"]),
        # The progressive transcript, canonicalised like its two siblings —
        # `runtime.js` tails it through `/api/fs/raw`, so it is the same URL
        # with the same Windows hazard, and a third path that skipped this
        # would be the one that broke there.
        "outputPartial": canonical_fs_path(request["outPartial"]),
        "model": model,
        "task": task,
    }


def _embed_error(type_: str, message: str, status: int,
                 job_id: str | None = None) -> JSONResponse:
    """The `/api/ai/embed` wire shape: `{ok:false, error:{type, message}}`.

    **Not `_error`'s plain `{error: message}`** — the shape `/api/ai/image` and
    `/api/ai/transcribe` use, and reasonably so: their 409 is always
    "unavailable", nothing more to say. This route's 409 can instead mean the
    model is loading NOW, exactly like `/api/ai`'s own local-model path (see
    `_ai_error`/`ModelNotReady` there), and that means a job id the page should
    watch — a field `_error`'s shape has nowhere to carry. Matching `/api/ai`'s
    contract rather than inventing a third one is what lets `fused.ai.embed`
    read errors the same way `fused.ai` already does.
    """
    payload = {"ok": False, "error": {"type": type_, "message": message}}
    if job_id is not None:
        payload["error"]["jobId"] = job_id
    return JSONResponse(payload, status_code=status)


@router.post("/api/ai/embed")
def api_ai_embed(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Embed text or an image into the resident dual encoder's vector space.

    **Not job-backed, unlike `/api/ai/image` and `/api/ai/transcribe`.** Both
    of those run for minutes and produce a file; this is one forward pass over
    a batch of at most `embed_common.MAX_ITEMS` short items, over before a
    progress row would ever have drawn — so the reply IS the result, the way
    `/api/ai`'s non-streaming reply is.

    **A cold model is `model_loading`, not `unavailable`** — the same fork
    `/api/ai`'s local-model path takes (`supervisor.generate_text` /
    `ModelNotReady`) rather than the one `/api/ai/image` takes (load inside the
    render's own job): an embed call has no job of its own for a multi-GB
    fetch to hide inside, so the load starts and its id comes back on a 409 for
    the caller to watch, exactly as the first `fused.ai(...)` on a cold local
    model already does.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    # Same rule `generate()` enforces inside each worker's own venv
    # (`embed_common.request_kind`) — refused HERE too, before a model is even
    # resolved, so a malformed request costs nothing rather than a 409 that
    # implies the fix is to wait.
    try:
        # The retrieval `kind` is validated here and forwarded RESOLVED in the
        # body below, so the route's reading is the one that counts — the worker
        # validates the same field again through the same function, exactly as
        # it does the batch ceiling.
        source, items, kind = embed_common.request_kind(body)
    except ValueError as e:
        return _embed_error("bad_request", str(e), status=400)

    if source == "paths":
        # Page-relative, exactly the rule `/api/ai/transcribe`'s `path` follows
        # (RH-1): the worker is a separate process with its own cwd, so an
        # unresolved relative path would mean "beside wherever the server was
        # launched from" rather than "beside this page" — a trap whatever the
        # error message says. An absolute path passes through untouched, as it
        # does there.
        base = body.get("base")
        resolved = []
        for path in items:
            path = os.path.expanduser(path)
            if not os.path.isabs(path):
                if not isinstance(base, str) or not os.path.isabs(base):
                    return _embed_error(
                        "bad_request",
                        "'paths' must be absolute, or relative to a page "
                        "named by 'base'", status=400)
                path = os.path.join(os.path.dirname(base), path)
            resolved.append(os.path.abspath(path))
        items = resolved

    model = _model_of(body) or catalog.default_for(registry.EMBEDDINGS)
    if not model:
        # See `api_ai_image`'s identical comment: no runner and no curated
        # default are different facts, and only the runner's own reason tells
        # the user what to do about it.
        return _embed_error(
            "unavailable",
            registry.unavailable_reason(registry.EMBEDDINGS)
            or "no embedding model is configured",
            status=409)

    # **Two per-model refusals, in this order** (SPEC §40) — `paths` then
    # `kind`, mirroring `api_ai_image`'s ENGINE-then-MODEL ordering so the
    # picker's affordances and this route cannot come to disagree about which
    # request is legal. Both fire AFTER the model is resolved, because both are
    # facts about the model rather than about the request, and neither can be
    # asked before `default_for` has answered.
    #
    # Refused rather than IGNORED, which is the whole point: a `paths` request a
    # text encoder accepted would embed noise, and a `kind` a dual encoder
    # accepted would be a parameter with no effect — and neither failure is
    # detectable downstream, since both return unit-length vectors of the right
    # dimension.
    if source == "paths" and embed_family(model) == "text":
        # `== "text"`, POSITIVE evidence, not `not _accepts_paths(...)` — see
        # `hub_cache.embed_family`'s docstring. A cold dual encoder has no
        # config on disk to read, and it must still fall through to the
        # `model_loading` reply below and start its download rather than being
        # refused for a file that is not there yet.
        return _embed_error(
            "bad_request",
            f"{model} is a text encoder — it has no vision tower, so 'paths' "
            f"is not something it can read. Pass 'texts' instead, or name a "
            f"dual encoder (a SigLIP or CLIP model) to embed images.",
            status=400)
    if "kind" in body and body.get("kind") is not None:
        scheme = formats.text_embed_scheme(model)
        if scheme == "none":
            return _embed_error(
                "bad_request",
                f"{model} has no retrieval prompt convention, so 'kind' would "
                f"change nothing about the vectors it returns — leave it out. "
                f"It applies to a retrieval encoder that instructs a question "
                f"and a passage differently; this model embeds both the same "
                f"way.",
                status=400)

    forwarded = {source: items}
    # `kind` on a `texts` request only, and only as the RESOLVED value: the
    # worker refuses `kind` beside `paths` outright (a prompt scheme has nothing
    # to prefix on an image), so sending it there would turn a legal request
    # into a 500 from inside the worker.
    if source == "texts":
        forwarded["kind"] = kind
    try:
        result = supervisor.generate_embed(model, forwarded)
    except supervisor.ModelNotReady as e:
        # NOT a failure (see `_ai_failed`'s own comment on the same fork in
        # `server/ai.py`): the load already started, and its job id is what
        # lets the caller show that download rather than just a rejection.
        return _embed_error("model_loading", str(e), status=409, job_id=e.job_id)
    except supervisor.SupervisorError as e:
        return _embed_error("ai_error", str(e), status=502)

    return {
        "ok": True,
        "result": {
            "vectors": result.get("vectors") or [],
            "dim": result.get("dim") or 0,
            "model": model,
        },
    }

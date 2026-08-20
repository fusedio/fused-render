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

Plus the two routes that make a capability DO something rather than be resident:
`POST /api/ai/image` and `POST /api/ai/transcribe`. Both answer with a job id
and a path, because both run for minutes and both produce a file.

The POSTs mutate — they start processes and write gigabytes — so every one of
them carries the D3 `X-Fused` guard. The reads do not, like every other read in
the app.
"""

from __future__ import annotations

import os
import secrets
import time

from fastapi import APIRouter, Body, Header

from fused_render._view_url_codec import canonical_fs_path
from fused_render.ai import catalog, registry, supervisor
# The `speakers` rule and the per-engine option rules, imported rather than
# restated. They are the SAME modules the runners import out of their own venvs
# — which is why every heavy import inside them is deferred, and why reading a
# rule here costs nothing.
from fused_render.ai.runners import diarize, engine_options, partial, preview
from fused_render.server.common import _error, _require_fused
# The AI Models page's reading of the local cache, imported rather than
# re-derived: see `_inferred_capability` and `_catalog_with_downloads`. It imports
# nothing from here.
from fused_render.server.routers.ai_models import (
    CachedModel, cached_capability, cached_models,
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


def _side(value, default: int) -> int:
    try:
        side = int(value)
    except (TypeError, ValueError):
        side = default
    side = max(_MIN_SIDE, min(_MAX_SIDE, side))
    return side - (side % _SIDE_STEP)


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
    is a text model that Transformers cannot open, so on a Mac switched to
    Transformers it is an unusable download. So the test is the FORMAT's own answer —
    is the runner this row resolved among the ones that would accept this snapshot
    (`CachedModel.loaders`)? — and anything else is left out of `models[]` entirely.

    **Left out, not flagged.** `models[]` has no `available`/`reason` field and every
    consumer reads it as "things I may offer"; adding one would mean every existing
    picker keeps offering the unloadable repo until it learns a new key, which is the
    failure being fixed rather than a fix. The repo is not hidden — the AI Models
    page's Local tab is the surface for "what is on my disk", it lists the repo, and
    it already prints WHICH engine reads it and what stands in the way ("text
    generation is set to Transformers, which does not read this format — switch it on
    the Engines tab"). A picker cannot say that; a card can.

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
    """
    rows = catalog.describe()
    cached = cached_models()
    on_disk = {model.repo_id for model in cached}
    resident = supervisor.resident_models()
    by_capability: dict[str, list] = {}
    for model in cached:
        if model.capability is None:
            # No capability could be inferred, and inventing one is how a load came
            # to send a diffusion repo to mlx-lm (D321). The repo is still visible
            # on the AI Models page, which is the surface for "what is on my disk".
            continue
        by_capability.setdefault(model.capability, []).append(model)
    for row in rows:
        curated = [
            dict(entry, source="curated", downloaded=entry["id"] in on_disk,
                 loaded=entry["id"] in resident)
            for entry in row["models"]
        ]
        curated_ids = {entry["id"] for entry in curated}
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
                "loaded": model.repo_id in resident,
            }
            for model in sorted(by_capability.get(row["capability"], ()), key=_cached_order)
            if model.repo_id not in curated_ids
            # The per-runner invariant, enforced: this row's list belongs to the
            # runner `describe()` resolved, and a repo whose format that runner does
            # not read has no business in it. See the docstring for both real repos
            # this drops and why they are dropped rather than flagged.
            and row["runner"] in model.loaders
        ]
        row["models"] = curated + extra
    return rows


@router.get("/api/ai/catalog")
def api_ai_catalog():
    """Suggested models per capability, plus what is on this disk.

    Sync `def`: `cached_models()` walks the hub cache (memoised, see there), so it
    belongs in the threadpool rather than on the event loop.
    """
    return {"capabilities": _catalog_with_downloads()}


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

    try:
        steps = max(1, min(_MAX_STEPS, int(body.get("steps") or 28)))
    except (TypeError, ValueError):
        return _error("'steps' must be a number", status=400)
    try:
        guidance = max(0.0, min(20.0, float(body.get("guidance") or 4.0)))
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
        "width": _side(body.get("width"), 1024),
        "height": _side(body.get("height"), 1024),
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
    return {
        "jobId": job,
        "path": path,
        # Canonical, because this goes back to a page that will put it in a
        # `/api/fs/raw` URL — a Windows path that reached it backslashed would
        # not match what the shell stored for the same file. It is a promise
        # about a PATH, not about a file: a model with no fitted projection
        # writes nothing there, and `fused.ai.image` treats a missing preview
        # as the ordinary case rather than as an error.
        "previewPath": canonical_fs_path(request["outPreview"]),
        "model": model,
        "prompt": request["prompt"],
        "width": request["width"],
        "height": request["height"],
        "steps": steps,
        "guidance": guidance,
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

    # …and what the ENGINE that will serve this cannot do at all (D319). Three
    # engines share this capability now and one of them — Parakeet — has no
    # translate task, no `language` argument and no text conditioning.
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

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
  immediately; a cold load is a multi-GB download and nothing waits on it.
* `POST /api/ai/runtime/unload` — release the weights.
* `POST /api/ai/runtime/download` — fetch without loading, for the AI Models
  page, where the verb is "Download" and the user is not asking to run anything
  yet.

The POSTs mutate — they start processes and write gigabytes — so all three carry
the D3 `X-Fused` guard. The reads do not, like every other read in the app.
"""

from __future__ import annotations

import os
import secrets
import time

from fastapi import APIRouter, Body, Header

from fused_render.ai import catalog, registry, supervisor
from fused_render.server.common import _error, _require_fused

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


def _model_of(body: dict) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        return ""
    return model.strip()


def _capability_of(body: dict, default: str = registry.TEXT_GENERATION) -> str:
    capability = body.get("capability")
    if capability is None:
        return default
    return capability if isinstance(capability, str) else ""


@router.get("/api/ai/runtime")
def api_ai_runtime():
    """Loaded models, their memory, and the runners available here.

    Sync `def`: it makes one localhost health request per live worker (usually
    zero or one), so it belongs in the threadpool rather than on the event loop.
    """
    return supervisor.describe()


@router.get("/api/ai/catalog")
def api_ai_catalog():
    """Suggested models per capability, with what this machine can run.

    The AI Models page joins these against the cache to draw a checkmark, so the
    reply is deliberately just the curation — whether a model is on disk is the
    cache's question and is answered by the endpoint that scans it.
    """
    return {"capabilities": catalog.describe()}


@router.post("/api/ai/runtime/load")
def api_ai_load(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    model = _model_of(body)
    if not model:
        return _error("'model' must be a Hugging Face repo id", status=400)
    capability = _capability_of(body)
    if capability not in registry.capabilities():
        return _error(f"unknown capability {body.get('capability')!r}", status=400)
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
    capability = _capability_of(body)
    if capability not in registry.capabilities():
        return _error(f"unknown capability {body.get('capability')!r}", status=400)
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
        return _error("no image model is configured", status=409)

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
    # Time-ordered and unique: the folder sorts chronologically in the explorer,
    # and two renders in the same second still land on different files.
    path = os.path.join(_images_dir(), f"{time.strftime('%Y%m%d-%H%M%S')}-{uid}.png")

    request = {
        "prompt": prompt.strip(),
        "width": _side(body.get("width"), 1024),
        "height": _side(body.get("height"), 1024),
        "steps": steps,
        "guidance": guidance,
        "seed": seed,
        "out": path,
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
        "model": model,
        "prompt": request["prompt"],
        "width": request["width"],
        "height": request["height"],
        "steps": steps,
        "guidance": guidance,
        "seed": seed,
    }

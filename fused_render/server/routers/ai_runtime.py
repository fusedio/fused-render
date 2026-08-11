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

from fastapi import APIRouter, Body, Header

from fused_render.ai import catalog, registry, supervisor
from fused_render.server.common import _error, _require_fused

router = APIRouter()


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

"""GET /api/app-models/overview, POST /api/app-models/accept.

The first-open overview of the AI models an app declares in its
`pyproject.toml` (see `fused_render/app_models.py`). Both handlers are plain
defs doing parse/stat work only: no model loads, no network, and the overview
never records an "open".
"""
import math
import os

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from fused_render import app_models
from fused_render.ai import hub_cache, supervisor
from fused_render.server import common
from fused_render.server.routers import ai_runtime

router = APIRouter(prefix="/api/app-models")

#: Rough bandwidth behind the "about N min" estimate; not measured.
_MB_PER_S = 20


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _catalog_index() -> tuple[dict, dict]:
    """`{model id: entry}` and `{capability: [entries]}` from the catalog."""
    by_id: dict = {}
    by_cap: dict = {}
    for row in ai_runtime._catalog_with_downloads():
        entries = row.get("models") or []
        by_cap[row["capability"]] = entries
        for entry in entries:
            by_id.setdefault(entry["id"], entry)
    return by_id, by_cap


def _label(model: str, entry: dict | None) -> str:
    if entry and entry.get("label"):
        return entry["label"]
    return model.rsplit("/", 1)[-1]


def _provider(model: str) -> str:
    if common.provider_of_model(model) == "apple":
        return "apple"
    from fused_render.server.ai import _is_local_model
    return "local" if _is_local_model(model) else "cloud"


def _live_job(model: str) -> str | None:
    from fused_render import jobs
    job_id = supervisor.job_id_for(model)
    try:
        live = {j.get("id") for j in jobs.list_jobs() if j.get("state") == jobs.RUNNING}
    except Exception:  # noqa: BLE001 — a progress hint, never worth a 500
        return None
    return job_id if job_id in live else None


def _row(cap: str, decl_row: dict, state: dict, by_id: dict, by_cap: dict) -> dict:
    model = decl_row["model"]
    provider = _provider(model)
    entry = by_id.get(model)
    if provider == "local":
        downloaded = (bool(entry["downloaded"]) if entry is not None
                      else hub_cache.is_downloaded(model))
        size = entry.get("size_gb") if entry is not None else None
    else:
        downloaded, size = True, None
    swappable = bool(decl_row["swappable"])
    candidates = []
    if swappable:
        candidates = [
            {"id": e["id"], "label": _label(e["id"], e), "sizeGb": e.get("size_gb")}
            for e in by_cap.get(cap, ()) if e.get("downloaded") and e["id"] != model
        ]
    stored = (state.get("substitutions") or {}).get(cap)
    return {
        "capability": cap,
        "verb": decl_row["verb"],
        "label": _label(model, entry),
        "model": model,
        "use": decl_row["use"] or app_models.CAPABILITY_LABEL.get(cap, cap),
        "provider": provider,
        "sizeGb": size,
        "downloaded": downloaded,
        "swappable": swappable,
        "choice": stored if swappable and stored else model,
        "candidates": candidates,
        "jobId": None if downloaded else _live_job(model),
    }


def _entry_path(path: str) -> str | None:
    return path if path and os.path.isabs(path) else None


@router.get("/overview")
def overview(path: str = Query(...)):
    path = _entry_path(path)
    app_dir = app_models.find_app_dir(path) if path else None
    if app_dir is None:
        return {"declares": False}
    decl = app_models.load_declaration(app_dir)
    state = app_models.read_state(app_dir)
    by_id, by_cap = _catalog_index()
    rows = [_row(cap, r, state, by_id, by_cap) for cap, r in decl.items()]
    total = sum(r["sizeGb"] or 0 for r in rows)
    need = sum((r["sizeGb"] or 0) for r in rows if not r["downloaded"])
    return {
        "declares": True,
        "accepted": app_models.is_accepted(app_dir, decl),
        "appName": os.path.basename(app_dir),
        "appDir": app_dir,
        "hash": app_models.declaration_hash(decl),
        "rows": rows,
        "totalGb": round(total, 3),
        "needGb": round(need, 3),
        "etaMin": math.ceil(need * 1024 / _MB_PER_S / 60) if need > 0 else None,
    }


@router.post("/accept")
def accept(body: dict = Body(...)):
    path = _entry_path(body.get("path") or "")
    app_dir = app_models.find_app_dir(path) if path else None
    if app_dir is None:
        return _error("this app declares no models", 404)
    decl = app_models.load_declaration(app_dir)
    subs_in = body.get("substitutions") or {}
    if not isinstance(subs_in, dict):
        return _error("'substitutions' must be an object", 400)
    _, by_cap = _catalog_index()
    subs: dict = {}
    for cap, sub in subs_in.items():
        row = decl.get(cap)
        if row is None:
            return _error(f"{cap!r} is not a declared capability", 400)
        if not isinstance(sub, str) or not sub:
            return _error(f"substitute for {cap!r} must be a model id", 400)
        if sub == row["model"]:
            continue
        if not row["swappable"]:
            return _error(f"{cap!r} is not swappable", 400)
        ok = {e["id"] for e in by_cap.get(cap, ()) if e.get("downloaded")}
        if sub not in ok:
            return _error(f"{sub!r} is not a downloaded {cap} model", 400)
        subs[cap] = sub
    digest = app_models.declaration_hash(decl)
    app_models.write_state(app_dir, True, digest, subs)
    return {"ok": True, "hash": digest}

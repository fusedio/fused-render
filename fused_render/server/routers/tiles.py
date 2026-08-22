"""The public tile surface: `fused.tiles` / /api/tiles/* (docs/ENGINE_HOST_DESIGN.md).

A page opens a dataset as map layers on the shared :1777 server instead of
spawning its own tile daemon. This is the NARROW, DOCUMENTED face of the
internal engine host (routers/engines.py): a page names a target and gets back
tile URLs on this origin; it never learns an engine id, a daemon path, the
reinit replay, or the proxy. Everything under /api/engines stays internal, and
this module is its only public caller.

An ALLOWLIST, not a passthrough: exactly /open, one tile route per format, a
status read and a close — no arbitrary child path is reachable. `{layer}` is
joined into the child engine's request path, so it is validated as a bare token
before any forward (path-traversal guard, correctness not auth — D3 stands).
"""
import asyncio
import os
import re
from urllib.parse import quote

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import Response

from fused_render.executor import run_python
from fused_render.server import engine_host
from fused_render.server.common import _error, _require_fused
from fused_render.server.routers import engines
from fused_render.shell import prefs as shell_prefs

router = APIRouter()

#: A public tile-engine kind -> the internal engine id it rides. One entry
#: today; a second tile engine is a one-line add. `open` uses "geo" only, and
#: never takes an engine id / daemon / path from the caller.
ENGINES = {"geo": "map"}

#: {layer} is joined into the child engine's request path, so it must be a bare
#: token — no separators or dots that could climb out of the tile namespace.
_LAYER = re.compile(r"^[A-Za-z0-9_-]+$")

#: map_render's descriptor.kind -> the simple public `kind`.
_PUBLIC_KIND = {
    "raster_tiles": "raster",
    "vector_tiles_mvt": "vector",
    "vector_geojson": "geojson",
    "vector_points_binary": "geojson",
    "raster_image": "image",
}


def _map_render_path() -> str:
    from fused_render.server import templates as server_templates

    return os.path.join(server_templates.TEMPLATES_DIR, "map", "map_render.py")


async def _describe_layer(target: str, options: dict) -> dict:
    """Run the map template's render entry point the way /api/run does and hand
    back its descriptor. map_render ensures the engine, describes through the
    proxy, registers the reinit replay and rewrites its URLs to proxy paths —
    this only re-faces the result. Isolated as one function so a test can
    substitute a canned descriptor without wiring the whole render path."""
    params = dict(options or {})
    params["target"] = target
    resolved = _map_render_path()
    if shell_prefs.effective_engine() == "fused":
        from fused_render import engine as _engine

        work = _engine.run_python(resolved, params)
    else:
        work = asyncio.to_thread(run_python, resolved, params)
    result = await work
    if not result.get("ok"):
        error = result.get("error") or {}
        return {
            "status": "error",
            "message": error.get("message") or "the map engine failed",
            "traceback": error.get("traceback"),
        }
    return result.get("result") or {}


def _layer_from_descriptor(descriptor: dict) -> dict:
    """Turn a map descriptor into the public layer object, sourcing every URL
    from the layer id so no /api/engines path (or child origin/token) can leak."""
    data = descriptor.get("data") or {}
    layer_id = data.get("source_id") or descriptor.get("id")
    kind = _PUBLIC_KIND.get(descriptor.get("kind"), descriptor.get("kind"))
    layer = {
        "id": layer_id,
        "kind": kind,
        "bounds": descriptor.get("bounds"),
        "minzoom": descriptor.get("minzoom"),
        "maxzoom": descriptor.get("maxzoom"),
        "tileUrl": None,
        "vectorTileUrl": None,
        "dataUrl": None,
        "warnings": descriptor.get("warnings") or [],
        "closeToken": data.get("reinit_key"),
    }
    if layer_id and kind == "raster":
        layer["tileUrl"] = f"/api/tiles/{layer_id}/{{z}}/{{x}}/{{y}}.png"
    elif layer_id and kind == "vector":
        layer["vectorTileUrl"] = f"/api/tiles/{layer_id}/{{z}}/{{x}}/{{y}}.pbf"
    else:
        for key in ("geojson_path", "image_path", "points_path"):
            path = data.get(key)
            if path:
                layer["dataUrl"] = "/api/fs/raw?path=" + quote(str(path), safe="")
                break
    return layer


def _bad_layer(layer: str):
    if not _LAYER.match(layer):
        return _error("not found", status=404)
    return None


@router.post("/api/tiles/open")
async def api_tiles_open(payload: dict = Body(...),
                         x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    target = str(payload.get("target") or "").strip()
    if not target:
        return _error("open needs a target")
    options = payload.get("options")
    if options is not None and not isinstance(options, dict):
        return _error("options must be an object")
    descriptor = await _describe_layer(target, options or {})
    if descriptor.get("status") != "ok":
        return {
            "status": "error",
            "message": descriptor.get("message") or "could not open the target",
            "traceback": descriptor.get("traceback"),
        }
    return _layer_from_descriptor(descriptor)


@router.get("/api/tiles/{layer}/{z}/{x}/{y}.png")
async def api_tiles_raster(layer: str, z: int, x: int, y: int, request: Request):
    if (bad := _bad_layer(layer)) is not None:
        return bad
    return await engines._forward(
        ENGINES["geo"], request, f"/tiles/{layer}/{z}/{x}/{y}.png", b"")


@router.get("/api/tiles/{layer}/{z}/{x}/{y}.pbf")
async def api_tiles_vector(layer: str, z: int, x: int, y: int, request: Request):
    if (bad := _bad_layer(layer)) is not None:
        return bad
    return await engines._forward(
        ENGINES["geo"], request, f"/vtiles/{layer}/{z}/{x}/{y}.pbf", b"")


@router.get("/api/tiles/{layer}/status")
async def api_tiles_status(layer: str, request: Request):
    if (bad := _bad_layer(layer)) is not None:
        return bad
    return await engines._forward(ENGINES["geo"], request, f"/jobs/{layer}", b"")


@router.post("/api/tiles/{layer}/close")
async def api_tiles_close(layer: str, payload: dict = Body(default={}),
                          x_fused: str | None = Header(default=None)):
    if (error := _require_fused(x_fused)) is not None:
        return error
    if (bad := _bad_layer(layer)) is not None:
        return bad
    token = str((payload or {}).get("closeToken") or "")
    if token:
        engine_host.forget(ENGINES["geo"], token)
    return {"ok": True}

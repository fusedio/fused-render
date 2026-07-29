import os

from fastapi import APIRouter, Body, Header

from fused_render.server.common import _error, _require_fused

router = APIRouter()


@router.post("/api/export")
def api_export(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    from fused_render.export import ExportError, _asset_key, export_page

    page = body.get("page")
    out = body.get("out")
    if not page or not os.path.isabs(page):
        return _error("'page' must be an absolute path to the .html page")
    if not out or not os.path.isabs(out):
        return _error("'out' must be an absolute path to the output directory")

    # Optional file selection (same as the Deploy modal): extra files to bundle
    # beyond the literal-call scan, and files to drop from it. Absent -> auto-only.
    include = body.get("include") or []
    exclude = body.get("exclude") or []
    for name, value in (("include", include), ("exclude", exclude)):
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            return _error(f"'{name}' must be an array of relative file paths")

    cache_max_age = body.get("cache_max_age") or "0s"

    try:
        plan = export_page(
            page, out, include=include, exclude=exclude, cache_max_age=cache_max_age
        )
    except ExportError as e:
        return _error(str(e))

    # Mirror the v2 manifest shape (entrypoints carry the payload-relative `key`, assets
    # just `path`+`name`) so a caller sees the same fields the bundle's manifest.json has.
    return {
        "out": os.path.abspath(out),
        "entrypoints": [
            {"path": e.path, "name": e.name, "key": _asset_key(e.path)}
            for e in plan.entrypoints
        ],
        "assets": [{"path": a.path, "name": a.name} for a in plan.assets],
        "warnings": plan.warnings,
    }

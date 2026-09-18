"""Third-party index-manifest confirmation (SPEC-index-plugins.md decision
#8, "the app proposes, the user confirms — never silent"): the HTTP surface
over `fused_render.index.manifest`'s propose/confirm/refuse store.

`propose` takes `html` — the calling page's own entry file — and resolves
its folder server-side exactly as `routers/background_apps.py`'s endpoints
resolve a daemon's folder: never a raw folder path from the caller, so this
adds no new path-typed API to defend. `confirm`/`refuse` take `folder`
directly, which stays safe despite being a raw path because
`manifest.confirm_index`/`refuse_index` only ever act on a folder already
present in the pending/confirmed lists — a caller cannot use either to
touch a folder that was never legitimately proposed first.

Approving a proposal (`confirm`) is the one act that grants a plugin's
declared root; nothing here imports or runs a third party's module — that
stays the caller's job, gated on `manifest.confirmed_folders()`, exactly as
`manifest.py`'s own module docstring specifies.
"""
import asyncio
import os

from fastapi import APIRouter, Body, Header

from fused_render.index import manifest
from fused_render.server.common import _error, _require_fused

router = APIRouter()


def _folder_for(html) -> str | None:
    """The app folder `html` (the caller's own entry page) belongs to —
    realpath'd, mirroring `background_apps._folder_for` so a folder reached
    through a symlink alias still resolves to the one identity
    `manifest.py`'s realpath-normalized store keys everything by."""
    if not isinstance(html, str) or not html:
        return None
    return os.path.realpath(os.path.dirname(os.path.abspath(html)))


def _entry(folder: str) -> dict:
    """A listing row for `folder`: its declared `kind`, or `None` when the
    folder's manifest has since gone missing or turned invalid — a
    proposal outliving its folder's own deletion degrades to an unnamed
    entry rather than an error, mirroring `load_manifest`'s own
    never-raises posture."""
    m = manifest.load_manifest(folder)
    return {"folder": folder, "kind": m.kind if m is not None else None}


@router.get("/api/index/proposals")
async def api_index_proposals():
    # Read-only, same posture as every other GET here — no X-Fused guard.
    pending, confirmed = await asyncio.to_thread(
        lambda: (manifest.pending_folders(), manifest.confirmed_folders()))
    return {
        "pending": [_entry(f) for f in pending],
        "confirmed": [_entry(f) for f in confirmed],
    }


@router.post("/api/index/proposals/propose")
async def api_index_propose(body: dict = Body(...),
                            x_fused: str | None = Header(default=None)):
    """A running app proposes its own folder for indexing. `{"ok": False}`
    rather than an error when the folder has no valid manifest — a folder
    cannot propose what it hasn't declared, and that is the caller's own
    state to read, not a request error."""
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    folder = _folder_for(body.get("html"))
    if folder is None:
        return _error("request body must include 'html'")
    ok = await asyncio.to_thread(manifest.propose_index, folder)
    return {"ok": ok}


@router.post("/api/index/proposals/confirm")
async def api_index_confirm(body: dict = Body(...),
                            x_fused: str | None = Header(default=None)):
    """The user approves a pending proposal — the only act that grants a
    plugin's declared root (decision #8). This is also the "caller"
    `manifest.py`'s own module docstring always deferred importing the
    module to: a bare `confirm_index` only ever rewrote the JSON proposal
    store, so nothing ever imported the folder's module or registered its
    `IndexKind` — a confirmed kind never appeared in `kinds.registered()`
    (and so never in `GET /api/index/kinds`) and could never be scanned.
    `register_confirmed_kinds()` is best-effort and never raises: a folder
    whose module fails to import still ends up `ok: True` here (it IS
    confirmed — the JSON store says so), it just will not appear as a
    scannable kind until its manifest/module is fixed and re-confirmed."""
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    folder = body.get("folder")
    if not isinstance(folder, str) or not folder:
        return _error("request body must include 'folder'")
    ok = await asyncio.to_thread(manifest.confirm_index, folder)
    if ok:
        await asyncio.to_thread(manifest.register_confirmed_kinds)
    return {"ok": ok}


@router.post("/api/index/proposals/refuse")
async def api_index_refuse(body: dict = Body(...),
                           x_fused: str | None = Header(default=None)):
    """The user declines a pending proposal, or revokes one already
    confirmed. Always succeeds (`manifest.refuse_index` never raises,
    including for a folder in neither list)."""
    if (guard := _require_fused(x_fused)) is not None:
        return guard
    folder = body.get("folder")
    if not isinstance(folder, str) or not folder:
        return _error("request body must include 'folder'")
    await asyncio.to_thread(manifest.refuse_index, folder)
    return {"ok": True}

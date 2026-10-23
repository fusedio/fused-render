"""The floating recording pill — the server half. No AppKit.

While a `fused.capture.audio` recording runs, macOS shows a small always-on-top
pill (app icon, level meter, pause, stop). The panel itself is AppKit and lives
in `recording_pill_panel.py`; this module is what the capture code and the
uvicorn threads may call, and it is a no-op everywhere the panel is not
installed (other platforms, a source server, a test): `native_hooks` stays empty
and every call returns without doing anything. Same split as `launcher.py` /
`launcher_panel.py`.

``native_hooks`` keys, filled by `app.py` once the panel exists:

- ``show(info)`` — put the pill up for this recording (`describe()`'s dict)
- ``hide(cid)`` — that recording ended
- ``hide_all()`` — every recording ended (quit, `stop_all`)

A hook is called from whichever thread ended or started the recording, so the
installed one hops to the main thread itself.
"""

from __future__ import annotations

import logging
import os

from fused_render._platform import is_within
from fused_render import _flavor

logger = logging.getLogger(__name__)

native_hooks: dict = {}


def _nearest_app(page: str, root: str) -> tuple[str, dict | None]:
    """`(folder, icon)` for the closest folder from `page` up to `root` that has
    an icon; `(root, None)` when none does. A page outside `root` checks `root`."""
    from fused_render import app_listing

    aroot = os.path.abspath(root)
    d = os.path.dirname(page)
    if not is_within(os.path.abspath(d), aroot):
        return root, app_listing.app_icon(root)
    while True:
        found = app_listing.app_icon(d)
        if found:
            return d, found
        if not is_within(os.path.abspath(d), aroot, strict=True):
            return root, None
        parent = os.path.dirname(d)
        if parent == d:
            return root, None
        d = parent


def describe(session) -> dict:
    """What the pill needs to draw itself for `session`, JSON-safe.

    The icon is the nearest `icon.svg`/`icon.png` from the page's folder up to
    its project root (the same mapping the job rows use). The walk is needed
    because a project root can be a parent repo with a `pyproject.toml`, which
    sits above the app folder that actually carries the icon; the name and
    click-to-open folder follow the icon's folder. No icon keeps the root. No
    page, or one that is not an app, falls back to the Fused artwork on the
    page side (`flavor` picks which)."""
    app = ""
    icon = None
    try:
        from fused_render import jobs, launcher

        page = getattr(session, "page", "") or ""
        root = jobs.app_for_page(page)
        found = None
        if root:
            app, found = _nearest_app(page, root)
        if found:
            icon = launcher._icon_url(found.get("icon"), found.get("mtime"))
    except Exception:                            # noqa: BLE001 - decoration only
        logger.debug("recording pill: describing %s failed", session.id,
                     exc_info=True)
    return {"id": session.id,
            "name": os.path.basename(app.rstrip("/\\")) if app else "Fused",
            "app": app,
            "icon": icon,
            "flavor": "bot" if _flavor.is_bot() else "render",
            "canPause": bool(session.can_pause),
            "mode": session.mode}


def _call(key: str, *args) -> None:
    hook = native_hooks.get(key)
    if hook is None:
        return
    try:
        hook(*args)
    except Exception:                            # noqa: BLE001 - a pill, not the work
        logger.debug("recording pill: %s failed", key, exc_info=True)


def show(session) -> None:
    if native_hooks.get("show") is None:
        return
    _call("show", describe(session))


def hide(cid: str) -> None:
    _call("hide", cid)


def hide_all() -> None:
    _call("hide_all")

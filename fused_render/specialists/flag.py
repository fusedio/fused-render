"""The `specialists` feature flag.

On only when env `FUSED_RENDER_SPECIALISTS` is "1", or when it is unset/other and prefs.json stores
`specialists_enabled: true` (same shape as `breakout_enabled`). The env var wins both ways so tests and dev runs
can force either state without touching the user's prefs.
"""
from __future__ import annotations

import os

from fastapi import HTTPException

ENV = "FUSED_RENDER_SPECIALISTS"


def on() -> bool:
    """True when specialists are enabled. Never raises: a broken prefs file reads as off."""
    env = os.environ.get(ENV)
    if env == "1":
        return True
    if env == "0":
        return False
    try:
        from fused_render.shell import prefs

        return prefs.read_prefs().get("specialists_enabled") is True
    except Exception:  # noqa: BLE001
        return False


def require() -> None:
    """FastAPI dependency for every `/api/specialists*` route: with the flag off the routes do not exist, so they
    answer exactly what an unknown route answers (404), not a 403 that would advertise the feature."""
    if not on():
        raise HTTPException(status_code=404, detail="not found")

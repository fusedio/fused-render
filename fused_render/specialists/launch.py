"""Stub (being built by stream 1)."""
from __future__ import annotations


def root() -> str:
    from fused_render.specialists import paths

    return paths.root()


def spec(key: str, file: str) -> dict:
    return {"error": "Specialist launch is not built yet", "reason": "not_built"}

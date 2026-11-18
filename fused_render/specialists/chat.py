"""Stub (being built by stream 1)."""
from __future__ import annotations

from typing import Literal


def state(spec_id: str) -> dict:
    return {"current": None, "folded": []}


def record_start(spec_id: str, session_id: str) -> None:
    return None


def clear(spec_id: str) -> dict:
    return state(spec_id)


def kind_of(spec_id: str, session_id: str) -> Literal["main", "breakout", ""]:
    return ""

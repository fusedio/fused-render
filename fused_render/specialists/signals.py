"""Behaviour signals recorded against a task session (stub)."""

from __future__ import annotations

from typing import Literal


def record(session_id, kind: Literal["stop", "deny", "interrupt", "undo", "failure"], detail: dict | None = None) -> None:
    return None


def since(session_id, ts: float) -> list[dict]:
    return []

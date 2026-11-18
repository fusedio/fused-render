"""Receipts: which task pulled which specialist's guidance, when, at which memory version (stub)."""

from __future__ import annotations


def record(spec_id, *, session_id, run_id, cwd, trigger, match, lines) -> dict:
    raise NotImplementedError


def due_for_review(now: float) -> list[tuple[str, dict]]:
    return []


def set_review(spec_id, rid, **state) -> None:
    raise NotImplementedError


def for_session(session_id) -> list[dict]:
    return []


def guided_week(spec_id) -> int:
    return 0

"""Stub (being built by stream 1)."""
from __future__ import annotations


def policy(spec_id: str, kind: str, session_id: str, tool: str) -> str:
    return "ask"


def set_allowed(spec_id: str, add=(), remove=()) -> list:
    return []


def roster(spec, kind: str) -> list[dict]:
    return []

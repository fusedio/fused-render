"""Stub (being built by stream 1)."""
from __future__ import annotations


def commit(spec_id: str, message: str) -> str | None:
    return None


def log(spec_id: str, path: str | None = None, limit: int = 50) -> list[dict]:
    return []


def memory_version(spec_id: str) -> str:
    return ""


def after_turn(spec_id: str, kind: str, session_id: str) -> None:
    return None


def diff_files(spec_id: str, sha: str) -> list[str]:
    return []

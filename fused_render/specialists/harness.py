"""Stub (being built by stream 1)."""
from __future__ import annotations

from typing import Literal, Sequence

BUSY = "busy"
CLOSED = "closed"


def run_side(spec, kind: Literal["check", "reviewer", "nightly"], prompt: str, *, trigger: dict,
             read_dirs: Sequence[str] = (), write_globs: Sequence[str] = (), services: bool = False,
             timeout_s: float | None = None) -> dict:
    return {"status": "error", "error": "not built yet", "answer": ""}


def extract_block(text: str, name: str) -> str | None:
    if not text:
        return None
    start = text.find("<<<" + name)
    if start < 0:
        return None
    start += len("<<<" + name)
    end = text.find(name + ">>>", start)
    if end < 0:
        return None
    return text[start:end].strip("\n")


def rows(spec_id: str, limit: int = 100) -> list[dict]:
    return []


def row(spec_id: str, rid: str) -> dict | None:
    return None


def shutdown(budget_s: float = 2.0) -> None:
    return None


def _spawn(argv, *, cwd, env, prompt, timeout_s, on_line=None):
    raise NotImplementedError

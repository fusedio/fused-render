"""Stub (being built by stream 1)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Specialist:
    id: str
    name: str
    subject: str = ""
    face: str = ""
    model: str = ""
    effort: str = ""
    triggers: dict = field(default_factory=lambda: {"folders": [], "keywords": []})
    connections: list = field(default_factory=list)
    created: float = 0.0
    shipped: str = ""
    home: str = ""


def all() -> list[Specialist]:  # noqa: A001
    return []


def any_exist() -> bool:
    return False


def get(spec_id: str) -> Specialist | None:
    return None


def find(key: str) -> Specialist | None:
    return None


def create(name: str, subject: str, triggers: dict | None = None, shipped: str = "") -> Specialist:
    raise NotImplementedError


def update(spec_id: str, **fields) -> Specialist:
    raise NotImplementedError


def delete(spec_id: str) -> None:
    raise NotImplementedError

"""Behaviour signals recorded against a task session (PRODUCT §4; PLAN A.2 "Behaviour signals").

    <root>/.state/signals/<session_id>.jsonl    one {"kind","at","detail"} per line

Kinds: `stop` (the user cancelled a turn), `deny` (refused a card), `interrupt` (a hook saw `is_interrupt`),
`undo` (a snapshot revert), `failure` (a tool failed). All seen by the app without a model call; the reviewer reads
them (`since`) to decide whether a task that pulled guidance was quiet.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Literal

KINDS = ("stop", "deny", "interrupt", "undo", "failure")
_SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LOCK = threading.Lock()


def _path(session_id: str) -> str | None:
    if not isinstance(session_id, str) or not _SID.match(session_id):
        return None
    from fused_render.specialists import paths

    return os.path.join(paths.state_root(), "signals", session_id + ".jsonl")


def record(session_id, kind: Literal["stop", "deny", "interrupt", "undo", "failure"],
           detail: dict | None = None) -> None:
    """Append one signal. Never raises (a lost signal only makes a review less informed)."""
    path = _path(session_id)
    if path is None or kind not in KINDS:
        return
    row = {"kind": kind, "at": time.time(), "detail": detail if isinstance(detail, dict) else {}}
    try:
        line = json.dumps(row, ensure_ascii=False, default=str)[:8000]
        with _LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except (OSError, ValueError):
        pass


def since(session_id, ts: float) -> list[dict]:
    """The signals of `session_id` at or after `ts`, oldest first."""
    path = _path(session_id)
    if path is None:
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("at"), (int, float)) and row["at"] >= ts:
                    out.append(row)
    except OSError:
        return []
    return out

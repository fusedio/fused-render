"""A specialist's app state, and reading its conversations.

`<home>/.specialist/state.json`:
    main          the main chat's session id (the page mounts it; Clear folds it and the next send starts fresh)
    folded        earlier main chats, `[{session_id, at}]`, oldest first
    breakouts     `{session_id: transcript lines the update has read}`
    tasks         `{session_id: {target, lines_read}}`, the tasks that used the specialist
    seen          when the user last looked at the chat
    last_update   when the update last looked for work
    update_files  the inbox files the update in progress was given
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

from fused_render.specialists import home as hm
from fused_render.specialists import paths

_LOCK = threading.Lock()


def _path(home: str) -> str:
    return os.path.join(home, paths.APP_DIR, "state.json")


def load(home: str) -> dict:
    d = hm.read_json(_path(home), {})
    return {"main": None, "folded": [], "breakouts": {}, "tasks": {}, "seen": 0, "last_update": 0,
            "update_files": [], **d}


def change(home: str, fn) -> dict:
    """`fn(state)` edits the state in place; written back under one lock."""
    with _LOCK:
        d = load(home)
        fn(d)
        hm.write_json(_path(home), d)
    return d


def record_start(spec_id: str, kind: str, session_id: str) -> None:
    """A main chat or breakout of `spec_id` answered its start with `session_id`."""
    home = paths.home(spec_id)
    if not session_id or not os.path.isdir(home):
        return

    def fn(d):
        if kind == "breakout":
            d["breakouts"].setdefault(session_id, 0)
        elif d["main"] != session_id:
            if d["main"]:
                d["folded"].append({"session_id": d["main"], "at": time.time()})
            d["main"] = session_id
    change(home, fn)


def record_task(spec_id: str, session_id: str, target: str) -> None:
    home = paths.home(spec_id)
    if session_id and os.path.isdir(home):
        change(home, lambda d: d["tasks"].setdefault(session_id, {"target": target, "lines_read": 0}))


def view(home: str) -> dict:
    d = load(home)
    return {"session_id": d["main"], "folded": d["folded"]}


def clear(home: str) -> dict:
    def fn(d):
        if d["main"]:
            d["folded"].append({"session_id": d["main"], "at": time.time()})
        d["main"] = None
    change(home, fn)
    return view(home)


def mark_seen(home: str) -> None:
    change(home, lambda d: d.update(seen=time.time()))


# -------------------------------------------------------------- transcripts ---
def transcript(target: str, session_id: str) -> str:
    from fused_render.claude_agent import agent_module

    agent = agent_module()
    return agent._session_transcript_path(agent._workdir(target), session_id)


def _text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    parts = [b.get("text", "") for b in content or () if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def messages(path: str, start: int = 0) -> tuple[list[dict], int]:
    """The text messages of a transcript from line `start` on, `[{role, text, at}]` (tool calls and results left
    out), and the number of lines in the file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return [], start
    out = []
    for ln in lines[start:]:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if row.get("type") not in ("user", "assistant") or row.get("isMeta") or row.get("isSidechain"):
            continue
        text = _text((row.get("message") or {}).get("content"))
        if text:
            try:
                at = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
            except (KeyError, AttributeError, ValueError):
                at = 0.0
            out.append({"role": row["type"], "text": text, "at": at})
    return out, len(lines)


_LAST: dict[str, tuple[int, dict | None]] = {}


def last(home: str, session_id: str | None) -> dict | None:
    """The main chat's last message, `{role, text, at}`, re-read only when the transcript grew (the Bots list asks
    every few seconds)."""
    if not session_id:
        return None
    path = transcript(home, session_id)
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    hit = _LAST.get(path)
    if hit is None or hit[0] != size:
        msgs, _ = messages(path)
        hit = _LAST[path] = (size, msgs[-1] if msgs else None)
    return hit[1]

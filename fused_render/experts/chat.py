"""An expert's app state, and reading its conversations.

`<home>/.expert/state.json`:
    main          the main chat's session id (the page mounts it; Clear folds it and the next send starts fresh)
    folded        earlier main chats, `[{session_id, at}]`, oldest first
    sources       `{session_id: {path, line, file}}`: each conversation that reaches the inbox (a task that named the
                  expert, a breakout, a main chat): its transcript, the lines already learned from, and the
                  pointer file waiting in `inbox/` ("" when none)
    seen          when the user last opened the Learning tab
    last_update   when the last learning run ended
    update        the learning run in progress: `{session, files, lines}` (its session, the inbox files it was
                  given, each listed conversation's line count when it started), else None
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

from fused_render.claude_agent import agent_module
from fused_render.experts import home as hm
from fused_render.experts import paths

_LOCK = threading.Lock()


def _path(home: str) -> str:
    return os.path.join(home, paths.APP_DIR, "state.json")


def load(home: str) -> dict:
    d = hm.read_json(_path(home), {})
    return {"main": None, "folded": [], "sources": {}, "seen": 0, "last_update": 0, "update": None, **d}


def change(home: str, fn) -> dict:
    """`fn(state)` edits the state in place; written back under one lock."""
    with _LOCK:
        d = load(home)
        fn(d)
        hm.write_json(_path(home), d)
    return d


def record_start(spec_id: str, kind: str, session_id: str) -> None:
    """A main chat of `spec_id` answered its start with `session_id` (a breakout needs no record: any session in
    the home that is neither the main chat nor a learning run is one)."""
    home = paths.home(spec_id)
    if kind != "main" or not session_id or not os.path.isdir(home):
        return

    def fn(d):
        if d["main"] != session_id:
            if d["main"]:
                d["folded"].append({"session_id": d["main"], "at": time.time()})
            d["main"] = session_id
    change(home, fn)


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
        if row["type"] == "user":  # the blocks the app puts before what the user typed
            text = agent_module()._strip_machinery(text)
        if text:
            try:
                at = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
            except (KeyError, AttributeError, ValueError):
                at = 0.0
            out.append({"role": row["type"], "text": text, "at": at})
    return out, len(lines)


def words(text: str) -> str:
    """The first words of `text`, as a title."""
    w = " ".join(text.split())
    return w[:60] + "…" if len(w) > 60 else w


def task_title(path: str) -> str:
    """A task's title as its list shows it by default: the first words of its first message."""
    msgs, _ = messages(path)
    return words(next((m["text"] for m in msgs if m["role"] == "user"), "")) or "a task"


_LAST: dict[str, tuple[int, dict | None]] = {}


def last(home: str, session_id: str | None) -> dict | None:
    """The main chat's last reply, `{role, text, at}` (as a bot's row shows its bot's last message: the user's own
    lines carry the chat's wire tags), re-read only when the transcript grew (the Bots list asks every few
    seconds)."""
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
        hit = _LAST[path] = (size, next((m for m in reversed(msgs) if m["role"] == "assistant"), None))
    return hit[1]

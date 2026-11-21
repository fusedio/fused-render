"""What waits for a specialist's next learning run: one markdown file each in `<home>/inbox/`.

A file is a `# <title>` line and its text. A task's `specialist_note` writes the note's text; a conversation (a task
that named the specialist, a breakout, the main chat) gets a pointer to its transcript (`update.point`). The title
line says which (`parse`); the pane lists and deletes them.
"""
from __future__ import annotations

import os
import re
import time
import uuid

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.md$")


def _dir(spec) -> str:
    return os.path.join(spec.home, "inbox")


def add(spec, sid: str, title: str, text: str) -> str:
    """Write a note and return its file name (time first, so names sort oldest first)."""
    name = "%s-%s-%s.md" % (time.strftime("%Y%m%d-%H%M%S"), re.sub(r"[^A-Za-z0-9]", "", sid)[:8] or "app",
                            uuid.uuid4().hex[:6])
    os.makedirs(_dir(spec), exist_ok=True)
    with open(os.path.join(_dir(spec), name), "w", encoding="utf-8") as fh:
        fh.write("# %s\n\n%s\n" % (title, text.strip()))
    return name


_KINDS = (("Note from task ", "note"), ("Task ", "task"), ("Breakout from ", "breakout"), ("Chat ", "chat"))


def parse(text: str) -> dict:
    """`{kind, title}` from a file's first line: `# Task "…"` is a task, and so on; the title is what is quoted."""
    line = text.split("\n", 1)[0].removeprefix("# ").strip()
    kind, rest = next(((k, line[len(p):]) for p, k in _KINDS if line.startswith(p)), ("note", line))
    return {"kind": kind, "title": rest.strip('"')}


def list(spec) -> list[dict]:  # noqa: A001 — the contract's name
    """`[{name, text, at, kind, title}]`, oldest first."""
    try:
        names = sorted(n for n in os.listdir(_dir(spec)) if _NAME.match(n))
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(_dir(spec), name)
        try:  # an update may remove it meanwhile
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            out.append({"name": name, "text": text, "at": os.path.getmtime(path), **parse(text)})
        except FileNotFoundError:
            continue
    return out


def delete(spec, name: str) -> bool:
    """Remove one note; False when there is no such note."""
    if not _NAME.match(name or ""):
        return False
    try:
        os.remove(os.path.join(_dir(spec), name))
    except FileNotFoundError:
        return False
    return True

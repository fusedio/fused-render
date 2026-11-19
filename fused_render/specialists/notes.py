"""Notes waiting for a specialist's next update: one markdown file each in `<home>/inbox/`.

A task's `specialist_note` adds one, the update writes the unread parts of tasks and breakouts as more, and the
pane lists and deletes them. The file is the whole note: no kinds, no states.
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


def list(spec) -> list[dict]:  # noqa: A001 — the contract's name
    """`[{name, text, at}]`, oldest first."""
    try:
        names = sorted(n for n in os.listdir(_dir(spec)) if _NAME.match(n))
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(_dir(spec), name)
        try:  # an update may remove it meanwhile
            with open(path, encoding="utf-8", errors="replace") as fh:
                out.append({"name": name, "text": fh.read(), "at": os.path.getmtime(path)})
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

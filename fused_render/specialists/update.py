"""The update: a turn in the specialist's own main chat that folds what waits in `inbox/` into memory and skills.

Before it starts, the app writes the unread parts of the tasks and breakouts that used the specialist into `inbox/`
(`prepare`), so the turn needs only its own folder. It runs unattended (launch kind `update`: acceptEdits, no MCP
server, anything that would raise a card denied at once). When it ends cleanly `history.after_turn` removes the
inbox files it was given and commits "Update" with its answer; a failed run leaves them for the next one.
"""
from __future__ import annotations

import threading
import time

from fused_render import claude_spawn
from fused_render.specialists import chat, history, notes, store

PROMPT = ("<specialist-update>\n"
          "Time for your update. The files in inbox/ are notes sent to you and the parts of tasks and breakouts "
          "that used you since your last update. Fold what is worth keeping into memory.md and your skills; leave "
          "inbox/ as it is, the app clears it. Then answer in a few plain sentences: what you added, what you "
          "changed, and what you left out and why.\n"
          "</specialist-update>")
DAY_S = 24 * 3600
#: The tail of one task or breakout kept for the update; older text of a very long one is left out.
EXCERPT_MAX = 20_000


class Busy(RuntimeError):
    pass


def due(spec: store.Specialist, now: float) -> bool:
    return now - chat.load(spec.home)["last_update"] >= DAY_S and not store.live_runs(spec.home)


def _excerpt(msgs: list[dict]) -> str:
    who = {"user": "User", "assistant": "Claude"}
    return "\n\n".join(f"**{who[m['role']]}:** {m['text']}" for m in msgs)[-EXCERPT_MAX:]


def prepare(spec: store.Specialist) -> list[str]:
    """Write the unread parts of tasks and breakouts into `inbox/` and move their cursors; the inbox files now
    waiting."""
    home, now = spec.home, time.time()
    st = chat.load(home)
    read_to: dict[str, int] = {}
    for sid, start in st["breakouts"].items():
        msgs, read_to[sid] = chat.messages(chat.transcript(home, sid), start)
        if msgs:
            notes.add(spec, sid, "From a breakout", _excerpt(msgs))
    for sid, task in st["tasks"].items():
        msgs, read_to[sid] = chat.messages(chat.transcript(task["target"], sid), task["lines_read"])
        if msgs:
            notes.add(spec, sid, f"From a task in {task['target']}", _excerpt(msgs))

    def fn(d):
        for sid, n in read_to.items():
            if sid in d["breakouts"]:
                d["breakouts"][sid] = n
            elif sid in d["tasks"]:
                d["tasks"][sid]["lines_read"] = n
        d["last_update"] = now
    chat.change(home, fn)
    return [n["name"] for n in notes.list(spec)]


def start(spec: store.Specialist) -> bool:
    """Start the update; False when nothing is waiting. `Busy` while a session is working in the home."""
    home = spec.home
    if store.live_runs(home):
        raise Busy("The specialist is working; try again when it has finished.")
    files = prepare(spec)
    if not files:
        return False
    # So the update's own commit shows the notes leaving inbox/.
    history.commit(home, "Notes waiting", ["inbox"])
    chat.change(home, lambda d: d.update(update_files=files))
    store.end_idle_hosts(home)
    main = chat.load(home)["main"]
    res = claude_spawn.spawn_helper(home, PROMPT, "acceptEdits", session_id=main or "", isolate_mcp=True,
                                    specialist=f"{spec.id}:update")
    if res.get("error"):
        raise RuntimeError(res["error"])
    if not main:
        chat.record_start(spec.id, "main", res["session_id"])
    threading.Thread(target=_watch, args=(home, res["run_id"]), daemon=True, name="specialist-update").start()
    return True


def _watch(home: str, run_id: str) -> None:
    """Poll the run to its end (the poll commits the turn), then end its host so the user's next message starts
    the main chat under its own launch spec."""
    claude_spawn.record_session_when_ready(claude_spawn.load_agent(), run_id)
    store.end_idle_hosts(home)

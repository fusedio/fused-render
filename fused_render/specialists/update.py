"""The update: a turn in the specialist's own main chat that folds what waits in `inbox/` into memory and skills.

Everything it learns from is a file in `inbox/`, written when it happens: a task's note (`tools.note`), and, at the
end of each turn of a breakout or of a task that used the specialist, that turn's new part of the conversation
(`gather`, from `history.turn_ended`), one file per session. The update reads only its own folder. It runs unattended
(launch kind `update`: acceptEdits, no MCP server, anything that would raise a card denied at once). When it ends
cleanly `history.after_turn` removes the inbox files present when it started and commits "Update" with its answer; a
failed run leaves them for the next one.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from fused_render import claude_spawn
from fused_render.specialists import chat, history, notes, store

PROMPT = ("<specialist-update>\n"
          "Time for your update. Read each of these files in inbox/ with the Read tool: notes sent to you and the "
          "parts of tasks and breakouts that used you since your last update.\n{files}\n"
          "Fold what is worth keeping into memory.md and your skills; leave inbox/ as it is, the app clears it. "
          "Then answer in a few plain sentences: what you added, what you changed, and what you left out and why.\n"
          "</specialist-update>")
DAY_S = 24 * 3600
#: The tail of one turn's part kept for the inbox; older text of a very long one is left out.
EXCERPT_MAX = 20_000
#: Where a breakout's seed (platform/lib/breakout-context.ts) names the task it came from.
_FROM = re.compile(r'(?:titled|began with) ("(?:[^"\\]|\\.)*")')


class Busy(RuntimeError):
    pass


def due(spec: store.Specialist, now: float) -> bool:
    return (now - chat.load(spec.home)["last_update"] >= DAY_S and bool(notes.list(spec))
            and not store.live_runs(spec.home))


def _excerpt(msgs: list[dict]) -> str:
    who = {"user": "User", "assistant": "Claude"}
    return "\n\n".join(f"**{who[m['role']]}:** {m['text']}" for m in msgs)[-EXCERPT_MAX:]


def _breakout_title(path: str) -> str:
    msgs, _ = chat.messages(path)
    m = _FROM.search(msgs[0]["text"]) if msgs else None
    return f'Breakout from "{chat.words(json.loads(m.group(1)))}"' if m else "A breakout"


def gather(spec: store.Specialist, sid: str, kind: str) -> None:
    """A turn of `sid` ended (`kind`: "breakouts" or "tasks" in the state): append its new part to the session's
    inbox file, starting a new file when there is none (first turn, or the update removed it)."""
    rec = chat.load(spec.home)[kind][sid]
    path = chat.transcript(spec.home if kind == "breakouts" else rec["target"], sid)
    msgs, read_to = chat.messages(path, rec["lines_read"])
    name = rec["file"]
    if msgs:
        if not (name and os.path.exists(os.path.join(spec.home, "inbox", name))):
            title = _breakout_title(path) if kind == "breakouts" else f'Task "{chat.task_title(path)}"'
            name = notes.add(spec, sid, title, "")
        notes.append(spec, name, _excerpt(msgs))
    chat.change(spec.home, lambda d: d[kind][sid].update(lines_read=read_to, file=name))


def start(spec: store.Specialist) -> bool:
    """Start the update; False when nothing is waiting. `Busy` while a session is working in the home."""
    home = spec.home
    if store.live_runs(home):
        raise Busy("The specialist is working; try again when it has finished.")
    files = [n["name"] for n in notes.list(spec)]
    if not files:
        return False
    # So the update's own commit shows the notes leaving inbox/.
    history.commit(home, "Notes waiting", ["inbox"])
    chat.change(home, lambda d: d.update(last_update=time.time(), update_files=files))
    store.end_idle_hosts(home)
    main = chat.load(home)["main"]
    # On the model and effort the user picked for the main chat ("" for each: the CLI's default).
    model, effort = claude_spawn.load_agent()._session_settings(main or "")
    # Named, so the turn need not list the folder: its Bash asks, and nobody is there to answer.
    prompt = PROMPT.format(files="\n".join(f"- inbox/{f}" for f in files))
    res = claude_spawn.spawn_helper(home, prompt, "acceptEdits", session_id=main or "", model=model, effort=effort,
                                    isolate_mcp=True, specialist=f"{spec.id}:update")
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

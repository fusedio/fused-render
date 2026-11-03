"""Give a bot a task from the command line (or any local script).

    python -m fused_render.bots.botsend <bot name or id> "<task text>"
    python -m fused_render.bots.botsend --list

Writes the task into the bot's drop folder (`~/Fused/bots/<folder>/.fused/drop/`,
never the Inbox at `.fused/inbox/`); the server's scheduler picks it up within
~20 s and runs it exactly as if you had typed it in the chat. Nothing listens on the network: only processes on
this machine can do this.
"""
from __future__ import annotations

import os
import re
import sys
import time

from fused_render.bots import paths as bpaths
from fused_render.bots import store


# The name main() gives a dropped file: `<YYYYmmdd>-<HHMMSS>-<pid>.txt`. migrate_v3's one-time drop fixup moves only
# files of this shape out of the Inbox, so a user's own `notes.txt` there is never turned into a task.
DROP_NAME_RE = re.compile(r"^\d{8}-\d{6}-\d+\.txt$")


def drop_dir(bid: str) -> str:
    """Where a task for bot `bid` is dropped: the same `paths.bot_drop` the server's scheduler drains."""
    return bpaths.bot_drop(bid)


def bots():
    # The merged meta (store.read_meta), not bot.json alone: `status` is private state (.fused/state.json).
    out = []
    for bid in store.list_ids():
        try:
            m = store.read_meta(bid)
        except (OSError, ValueError):
            continue
        m.setdefault("id", bid)
        out.append(m)
    return out


def main(argv):
    if len(argv) == 1 and argv[0] == "--list":
        for b in bots():
            print(f"{b['id']}  {b.get('status', '?'):8}  {b.get('name', '')}")
        return 0
    if len(argv) < 2:
        print(__doc__.strip())
        return 2
    who, task = argv[0], " ".join(argv[1:]).strip()
    match = [b for b in bots() if b["id"] == who or b["id"].startswith(who) or b.get("name", "").lower() == who.lower()]
    if len(match) != 1:
        names = ", ".join(f"{b.get('name')} ({b['id']})" for b in bots()) or "none"
        print(f"{'no' if not match else 'ambiguous'} bot {who!r}; bots: {names}", file=sys.stderr)
        return 1
    if not task:
        print("empty task", file=sys.stderr)
        return 1
    drop = drop_dir(match[0]["id"])
    name = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    with open(os.path.join(drop, name + ".tmp"), "w", encoding="utf-8") as f:
        f.write(task + "\n")
    os.replace(os.path.join(drop, name + ".tmp"), os.path.join(drop, name + ".txt"))
    print(f"queued for {match[0].get('name')} ({match[0]['id']}); it starts within ~20 s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

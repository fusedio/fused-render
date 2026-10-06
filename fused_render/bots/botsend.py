"""Give a bot a task from the command line (or any local script).

    python -m fused_render.bots.botsend <bot name or id> "<task text>"
    python -m fused_render.bots.botsend --list

Writes the task into the bot's inbox folder (`<home>/bots/data/bots/<id>/inbox/`);
the server's scheduler picks it up within ~20 s and runs it exactly as if you
had typed it in the chat. Nothing listens on the network: only processes on
this machine can do this.
"""
from __future__ import annotations

import json
import os
import sys
import time

from fused_render.bots import paths as bpaths


def bots():
    out = []
    root = bpaths.data_dir()
    for bid in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        try:
            with open(os.path.join(root, bid, "bot.json"), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            pass
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
    inbox = os.path.join(bpaths.data_dir(), match[0]["id"], "inbox")
    os.makedirs(inbox, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    with open(os.path.join(inbox, name + ".tmp"), "w", encoding="utf-8") as f:
        f.write(task + "\n")
    os.replace(os.path.join(inbox, name + ".tmp"), os.path.join(inbox, name + ".txt"))
    print(f"queued for {match[0].get('name')} ({match[0]['id']}); it starts within ~20 s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

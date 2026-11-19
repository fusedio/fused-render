"""Showing work in a specialist's main chat (docs/specialists.md §15): a canvas, or one node of a canvas (a UDF,
a widget, an HTML node, a note), as a card the user opens live.

The main chat, and only it, gets one more session MCP server, `specialist_canvas`, with two tools:

    canvas_create   name, env?    a new canvas on the user's Fused account and its folder here; returns the folder
    show            path, note?   a card in the chat for a canvas or one node of one

Both are the experts' tools (`experts/show.py`: `make_canvas`, `view`), so the two kinds of bot make and classify
a canvas the same way. The specialist writes every node file itself, with its own Write / Edit; nothing here
reads or lays out what it wrote. The canvases it made are listed in `.specialist/state.json` (`canvases`), so a
second `canvas_create` under the same name returns the folder instead of refusing the taken name.

A `show` that worked ends its answer with one `CARD` line carrying the card's payload; the chat reads it from the
tool row (frontend `apps/claude/specialists/view-card.ts`) and draws the card after that row.
"""
from __future__ import annotations

import json
import os
import time

from fused_render.specialists import chat

SERVER = "specialist_canvas"
CREATE = "canvas_create"
SHOW = "show"
#: The line a successful `show` ends with: `CARD {"kind", "path", "title", "canvas", "env", "note"}`.
CARD = "[fused-card] "
SKILL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "showing-your-work.md")
MADE_MAX = 200
NOTE_MAX = 200

TOOLS = {
    CREATE: {
        "description": (
            "Make a new, empty Fused canvas of the user's and its editable folder on this machine, for work you want "
            "to show them (UDFs, widgets). Returns the folder. You then write the node files into that folder "
            "yourself with Write / Edit, and their rows in its canvas.toml. Only when the user asked you to build "
            "or show something; never for a throwaway run."),
        "inputSchema": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string", "description": "The canvas name: letters, digits and underscores only."},
            "env": {"type": "string", "description": "The Fused environment (`prod` or `unstable`). Leave it out "
                                                       "unless the user named one."}}},
    },
    SHOW: {
        "description": (
            "Show the user a canvas or one node of a canvas as a card in the chat; the card opens it live. `path` "
            "is the absolute path of a node's file (`.py` UDF, `.json` widget, `.html`, `.md`) at the root of a "
            "canvas folder under `user/`, or the canvas folder itself. Call it once the files are written and the "
            "canvas validates. Calling it again for the same path after an edit is not needed: the open view "
            "follows the files."),
        "inputSchema": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Absolute path of the node file or the canvas folder."},
            "note": {"type": "string", "description": "One short line shown on the card (what this is)."}}},
    },
}


def on() -> bool:
    """Whether a main chat gets the server: the Canvases feature is on and the `fused` CLI is there (the format
    check, `canvas validate`, and running a UDF both need it). Never raises: any failure is no."""
    try:
        from fused_render import fusedcli
        from fused_render.shell import prefs
        return bool(prefs.canvases_enabled()) and bool(os.environ.get(fusedcli.CLI_DIR_ENV))
    except Exception:  # noqa: BLE001
        return False


def envs() -> tuple[list, str]:
    """`(the environments a canvas may be made in, the one used when `env` is left out)`. The default is the one
    environment the user has canvas copies of, else `prod`."""
    from fused_render import canvases
    from fused_render.experts import store as xstore
    try:
        base = canvases.canvases_base()
        have = [e for e in xstore.RUN_ENVS if os.path.isdir(os.path.join(base, e))]
    except Exception:  # noqa: BLE001
        have = []
    return list(xstore.RUN_ENVS), (have[0] if len(have) == 1 else "" if have else "prod")


def note() -> str:
    """The sentences a main chat that has the tools is told (`launch.spec`'s `append`)."""
    return (f"You can show the user your work: read {SKILL} first. `mcp__{SERVER}__{CREATE}` makes a new canvas of "
            "theirs and returns its folder, you write its node files (`<name>.py` UDFs, `<name>.json` widgets) and "
            f"their canvas.toml rows there, and `mcp__{SERVER}__{SHOW}` puts a card in the chat that opens a node or "
            "the canvas live. Do this when the user asks to see, build or change something; they keep improving it "
            "by telling you, and you edit the same files.")


def made(home: str) -> list:
    rows = chat.load(home).get("canvases")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("env"), str)
            and isinstance(r.get("name"), str)] if isinstance(rows, list) else []


def _record(home: str, env: str, name: str, folder: str) -> None:
    def fn(d):
        rows = [r for r in made(home) if (r["env"], r["name"]) != (env, name)]
        rows.append({"env": env, "name": name, "dir": folder, "at": int(time.time())})
        d["canvases"] = rows[-MADE_MAX:]
    chat.change(home, fn)


def mcp_tools() -> list[dict]:
    return [{"name": name, **spec} for name, spec in TOOLS.items()]


def _result(text: str, ok: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": not ok}


def call(spec, name: str, args) -> dict:
    """Run one of the two tools for specialist `spec`: `{content, isError}`. Never raises."""
    args = args if isinstance(args, dict) else {}
    try:
        from fused_render.experts import show as xshow
        if not on():
            return _result("error: canvases are not available right now (the Canvases feature is off in "
                           "Preferences, or the fused CLI is missing).", False)
        if name == CREATE:
            allowed, default = envs()
            _, text, ok = xshow.make_canvas(args, allowed, made(spec.home),
                                            lambda env, cname, folder: _record(spec.home, env, cname, folder),
                                            default_env=default)
            return _result(text, bool(ok))
        if name == SHOW:
            _, text, view = xshow.view(args, spec.home)
            if view is None:
                return _result(text, False)
            said = " ".join(str(args.get("note") or "").split())[:NOTE_MAX]
            return _result(text + "\n" + CARD + json.dumps({**view, "note": said}), True)
    except Exception as e:  # noqa: BLE001
        return _result(f"error: {e}", False)
    return _result(f"error: no such tool ({name})", False)

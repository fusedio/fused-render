"""Showing work in an expert's main chat (docs/experts.md §14): a canvas, or one node of a canvas (a UDF,
a widget, an HTML node, a note), as a card the user opens live.

The main chat, and only it, gets one more session MCP server, `expert_canvas`, with two tools:

    canvas_create   name, env?    a new canvas on the user's Fused account and its folder here; returns the folder.
                                  Gated: the user approves each one.
    show            path, note?   a card in the chat for a canvas or one node of one

The expert writes every node file itself, with its own Write / Edit; nothing here reads or lays out what it
wrote. A `show` also makes sure the canvas it shows is being synced (`_publish`), which is what repairs a canvas
the expert made with the `fused` CLI instead of `canvas_create`. The canvases it made are listed in `.expert/state.json` (`canvases`), so a
second `canvas_create` under the same name returns the folder instead of refusing the taken name.

A `show` that worked ends its answer with one `CARD` line carrying the card's payload; the chat reads it from the
tool row (frontend `apps/claude/experts/view-card.ts`) and draws the card after that row.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from fused_render.experts import chat

logger = logging.getLogger(__name__)

SERVER = "expert_canvas"
CREATE = "canvas_create"
SHOW = "show"
#: The line a successful `show` ends with: `CARD {"kind", "path", "title", "canvas", "env", "note", "live"}`.
CARD = "[fused-card] "
SKILL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "showing-your-work.md")
#: `canvas_create` asks the user every time, in every permission mode (`launch.spec`'s `ask`; the permission
#: server never answers it from the chat's "Always approve", `permission_server.EXPERT_CANVAS_CREATE`).
ASK = [f"mcp__{SERVER}__{CREATE}"]
MADE_MAX = 200
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")
NOTE_MAX = 200

TOOLS = {
    CREATE: {
        "description": (
            "Make a new, empty Fused canvas of the user's and its editable folder on this machine, for work you want "
            "to show them (UDFs, widgets). Returns the folder. You then write the node files into that folder "
            "yourself with Write / Edit, and their rows in its canvas.toml. Only when the user asked you to build "
            "or show something; never for a throwaway run. The user is asked to approve each new canvas. This is "
            "the only way to make a canvas: never use the `fused` CLI's create, clone, pull or push for one, and "
            "never make the folder yourself."),
        "inputSchema": {"type": "object", "required": ["name", "env"], "properties": {
            "name": {"type": "string", "description": "The canvas name: letters, digits and underscores only."},
            # Required, so the approval card the user answers names the environment beside the canvas; the
            # default is written into this description per session (`mcp_tools`).
            "env": {"type": "string", "description": "The Fused environment the canvas is made in."}}},
    },
    SHOW: {
        "description": (
            "Show the user a canvas or one node of a canvas as a card in the chat. A node's card draws the node "
            "live in the chat as soon as it appears, which RUNS a UDF; pass `live: false` for a UDF that is slow, "
            "costly or computes a lot of data, and the card waits for the user to click before anything runs. `path` "
            "is the absolute path of a node's file (`.py` UDF, `.json` widget, `.html`, `.md`) at the root of a "
            "canvas folder under `user/`, or the canvas folder itself. Call it once the files are written and the "
            "canvas validates. Calling it again for the same path after an edit is not needed: the open view "
            "follows the files."),
        "inputSchema": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Absolute path of the node file or the canvas folder."},
            "note": {"type": "string", "description": "One short line shown on the card (what this is)."},
            "live": {"type": "boolean", "description": (
                "Whether a node's card loads (and so runs) by itself when it appears. Default true. Give false "
                "for a heavy UDF: a long run, a large area or date range, a big table, or anything you did not "
                "see finish quickly when you ran it.")}}},
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
    """`(the environments a canvas may be made in, the one used when `env` is left out)`: the default is the
    user's default Fused environment (`fusedcli.workbench_env`: the Preferences choice, or the process's pin),
    read at call time."""
    from fused_render import fusedcli
    default = fusedcli.workbench_env()
    return sorted({*fusedcli.WORKBENCH_ENV_CHOICES, default}), default


def skills() -> bool:
    """Make sure the `workbench` skills (the canvas.toml, UDF and JSON-UI widget format references a canvas task
    is handed) are on disk for a main chat about to start, fetching them once when they never were. True when
    there is a root to hand the session (`launch.spec`'s `workbench_skills`). Never raises."""
    try:
        from fused_render import skill_plugin
        return bool(skill_plugin.workbench_plugin_root() or skill_plugin.sync_workbench_plugin())
    except Exception:  # noqa: BLE001
        return False


def note(skills: bool = False) -> str:
    """The sentences a main chat that has the tools is told (`launch.spec`'s `append`); `skills` when the
    session is also handed the `workbench` skills."""
    return (f"You can show the user your work: read {SKILL} first. `mcp__{SERVER}__{CREATE}` makes a new canvas of "
            "theirs and returns its folder, you write its node files (`<name>.py` UDFs, `<name>.json` widgets) and "
            f"their canvas.toml rows there, and `mcp__{SERVER}__{SHOW}` puts a card in the chat that opens a node or "
            "the canvas live. Do this when the user asks to see, build or change something; they keep improving it "
            f"by telling you, and you edit the same files. `{CREATE}` is the ONLY way you make a canvas: never with "
            "the `fused` CLI's own `canvas create`, `clone`, `pull` or `push`, and never by making a folder under a "
            "canvas `user/` folder yourself. Only the tool asks the user, registers the canvas with the app and "
            "starts its sync; a canvas made any other way is not synced and can open with an error. If the tool is "
            "refused or fails, tell the user what it said and stop; do not work around it."
            + (" You have the `workbench` skills for the formats, and must invoke the one for a file before you "
               "write or change it, however small the change: `workbench:json-ui-schemas` for any `.json` widget, "
               "`workbench:fused-udfs` for any `.py` UDF, `workbench:canvas-toml` before the first canvas.toml "
               "edit, `workbench:html-template-nodes` for any `.html` node." if skills else ""))


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


def _refusal(response) -> str | None:
    """A canvases route's refusal as one sentence, None for its success dict."""
    if isinstance(response, dict):
        return None
    try:
        body = json.loads(response.body)
        return str(body.get("error") or "request failed")
    except Exception:  # noqa: BLE001
        return "request failed"


def make_canvas(args: dict, envs: list, made_rows: list, record, default_env: str = "") -> tuple:
    """`(label, result text, ok)`. Create on Fused, clone, start the watcher: the three calls the Canvases page
    makes, in this process. `envs` are the environments allowed, `made_rows` the canvases this expert made
    before, `record(env, name, folder)` notes a new one, `default_env` the environment when `env` is left out.
    The watcher is started before the expert writes anything on purpose: a watcher that first sees a folder
    of seconds-old files takes them as already published (`canvases._FRESH_WINDOW_S`)."""
    name = str(args.get("name") or "").strip()
    label = f"create canvas {name or '?'}"
    if not _NAME_RE.fullmatch(name):
        return label, "error: a canvas name is letters, digits and underscores only (at most 128).", False
    env = str(args.get("env") or "").strip() or (envs[0] if len(envs) == 1 else default_env)
    if env not in envs:
        return label, f"error: give `env`, one of: {', '.join(envs)}.", False
    from fused_render import canvases
    folder = canvases._canvas_dir(env, name)
    mine = any((r["env"], r["name"]) == (env, name) for r in made_rows)
    if os.path.isdir(folder) and not mine:
        return label, (f"error: the user already has a canvas named {name} in {env} ({folder}). Pick another name; "
                       "to change that canvas, ask the user first."), False
    if not (mine and os.path.isfile(os.path.join(folder, "canvas.toml"))):
        why = _refusal(canvases.api_canvases_create({"name": name, "env": env}, x_fused="1"))
        if why is not None and not mine:
            return label, f"error: the canvas was not created: {why}", False
        why = _refusal(canvases.api_canvases_clone({"name": name, "env": env}, x_fused="1"))
        if why is not None:
            return label, f"error: the canvas was created on Fused but its folder could not be made: {why}", False
    record(env, name, folder)
    # Best effort: another fused-render may own the clone's sync, and the card's view starts it again anyway.
    started = _refusal(canvases._sync_start_response(env, name)) is None
    return label, (
        f"The canvas {name} ({env}) is ready. Its folder: {folder}\n"
        "It may hold one starter node, `udf.py`: keep, change or remove it (file and canvas.toml row together).\n"
        "Write each node as a file at the folder's root with Write / Edit and add its `[[canvas.nodes]]` row to "
        "canvas.toml with a targeted edit (re-read canvas.toml first). These writes need no approval.\n"
        f"Then check it with `fused workbench --env {env} canvas validate {folder}` and call `{SHOW}` with the "
        "node file's path."
        + ("" if started else "\nIts sync has not started yet; it starts when the user opens the card.")), True


def view(args: dict, base: str) -> tuple:
    """`(label, result text, view)`: `view` is the card's payload, None when `path` is nothing to show. A
    relative `path` is taken from `base` (the home)."""
    raw = str(args.get("path") or "").strip()
    label = f"show {os.path.basename(raw.rstrip('/')) or '?'}"
    if not raw:
        return label, "error: give `path`.", None
    path = os.path.normpath(os.path.join(base, os.path.expanduser(raw)))
    from fused_render import canvases
    if os.path.basename(path) == "canvas.toml":
        path = os.path.dirname(path)
    if os.path.isdir(path):
        found = canvases.clone_dir_for(path)
        if (found is None or os.path.normpath(found[2]) != path
                or not os.path.isfile(os.path.join(path, "canvas.toml"))):
            return label, ("error: that folder is not a canvas of the user's. A canvas you can show is a folder "
                           "with a canvas.toml directly under an environment's `user/` folder."), None
        env, name, folder = found
        return label, _shown(name), {"kind": "canvas", "path": folder, "title": name, "canvas": name, "env": env}
    if not os.path.isfile(path):
        return label, f"error: no such file: {path}", None
    node = canvases.canvas_node_file(path)
    if node is None:
        return label, ("error: that file is not a node of a canvas of the user's, so there is nothing to open. A "
                       "node is a `.py`, `.json`, `.html` or `.md` file at the root of a canvas folder under "
                       "`user/` that has a `[[canvas.nodes]]` row (`udfName` = the file's name without its suffix) "
                       "in that folder's canvas.toml. Add the row, or make a canvas with "
                       f"`{CREATE}` and write the file there."), None
    return label, _shown(node["title"]), {"kind": node["kind"], "path": path, "title": node["title"],
                                          "canvas": node["name"], "env": node["env"]}


def _publish(env: str, name: str) -> None:
    """Make sure the app syncs a canvas the expert shows. One made with `canvas_create` already is. One it made
    itself with the `fused` CLI, against its instructions, has no watcher until a page opens it: start one, and
    push now, because a watcher that starts on a folder of seconds-old files takes them as already published
    (`canvases._FRESH_WINDOW_S`). The push runs off the tool call (it takes seconds). Best effort."""
    from fused_render import canvases
    if canvases._sync_manager(env, name, create=False) is not None:
        return
    if _refusal(canvases._sync_start_response(env, name)) is not None:
        return  # a reference copy, or another fused-render syncs it

    def push():
        try:
            canvases.api_canvases_sync_push({"name": name, "env": env}, x_fused="1")
        except Exception:  # noqa: BLE001
            logger.warning("experts: first push of canvas %s/%s failed", env, name, exc_info=True)

    threading.Thread(target=push, daemon=True, name=f"expert-canvas-push-{name}").start()


def _shown(title: str) -> str:
    return (f"The card for {title} is in the chat. Say in one line what it shows; do not describe how to open it.")


def mcp_tools() -> list[dict]:
    """The two tools as MCP definitions. `canvas_create`'s `env` says which environment is the user's default
    right now, so the expert passes it and the approval card shows it."""
    allowed, default = envs()
    out = json.loads(json.dumps([{"name": name, **spec} for name, spec in TOOLS.items()]))
    out[0]["inputSchema"]["properties"]["env"]["description"] = (
        f"The Fused environment the canvas is made in. Always give it: `{default}`, the user's default "
        f"environment, unless the user explicitly asked for another (one of: {', '.join(allowed)}). The user "
        "sees it on the approval card.")
    return out


def _result(text: str, ok: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": not ok}


def call(spec, name: str, args) -> dict:
    """Run one of the two tools for expert `spec`: `{content, isError}`. Never raises."""
    args = args if isinstance(args, dict) else {}
    try:
        if not on():
            return _result("error: canvases are not available right now (the Canvases feature is off in "
                           "Preferences, or the fused CLI is missing).", False)
        if name == CREATE:
            allowed, default = envs()
            _, text, ok = make_canvas(args, allowed, made(spec.home),
                                            lambda env, cname, folder: _record(spec.home, env, cname, folder),
                                            default_env=default)
            return _result(text, bool(ok))
        if name == SHOW:
            _, text, card = view(args, spec.home)
            if card is None:
                return _result(text, False)
            _publish(card["env"], card["canvas"])
            said = " ".join(str(args.get("note") or "").split())[:NOTE_MAX]
            return _result(text + "\n" + CARD + json.dumps({**card, "note": said, "live": args.get("live") is not False}), True)
    except Exception as e:  # noqa: BLE001
        return _result(f"error: {e}", False)
    return _result(f"error: no such tool ({name})", False)

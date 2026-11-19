"""Showing work in the chat (docs/experts.md §17): the two tools an expert's attended chat has besides Claude
Code's own, and the canvases it made with them.

- `canvas_create` makes a new canvas on Fused and its live clone on this machine, the way the Canvases page does
  (`canvases.api_canvases_create`, then the clone, then the sync watcher). The expert then writes the node files
  itself, with Edit / Write: files at the root of a canvas it made are written without an approval card
  (`made_file`, asked by `store.free_write`).
- `show` puts a card in the chat for a canvas, or for one node of a canvas (a UDF, a widget, an HTML node, a
  note). The card opens the same live view the explorer shows for that file. This module only says what the path
  is (`canvases.canvas_node_file`, the node view's own rule); it renders nothing.

As everywhere in experts: our code delivers, the harness thinks. Nothing here writes a node, lays out a canvas or
reads what the expert wrote.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from fused_render.experts import store

#: The bots' MCP server (agent_engine.MCP_SERVER; a test keeps the two equal). Spelled here so the fence can name
#: the tools without importing the engine, which imports the fence.
SERVER = "bot"
CREATE = "canvas_create"
SHOW = "show"
SKILL = "showing-your-work.md"

TOOLS = {
    CREATE: {
        "description": (
            "Make a new, empty Fused canvas of the user's and its editable folder on this machine, for work you want "
            "to show them (UDFs, widgets). Returns the folder. You then write the node files into that folder "
            "yourself with Write / Edit, and their rows in its canvas.toml. Only when the user asked you to build "
            "or show something; never for a throwaway run (use your scratch folder for those)."),
        "inputSchema": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string", "description": "The canvas name: letters, digits and underscores only."},
            "env": {"type": "string", "description": "The Fused environment (`prod` or `unstable`). Leave it out "
                                                       "when you were told about one environment only."}}},
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
            "note": {"type": "string", "description": "One short line shown above the card (what this is)."}}},
    },
}

_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")
_LOCK = threading.Lock()
#: Files in a canvas folder that are fused-render's own, never the expert's to write without a card.
_NOT_NODES = frozenset({"CLAUDE.md"})
MADE_MAX = 200


def on(bot) -> bool:
    """Whether this expert's attended chat gets the two tools: the Canvases feature is on and the expert may run
    UDFs somewhere (`store.run_envs`: the `fused` CLI is there and a folder of canvas copies is granted). Never
    raises: any failure is no."""
    try:
        from fused_render.shell import prefs
        return bool(prefs.canvases_enabled()) and bool(store.run_envs(bot))
    except Exception:  # noqa: BLE001
        return False


def specs() -> list:
    return [{"name": name, **spec} for name, spec in TOOLS.items()]


def rules(bot) -> list:
    """The fence's allow rules for a chat that has the tools (`sessions.fence(show=True)`): the two tools, the
    read-only `canvas validate` per environment, and a Read rule for the workbench skills (the canvas.toml, UDF
    and widget format references), when this machine has them."""
    from fused_render.experts import sessions
    out = [f"mcp__{SERVER}__{name}" for name in TOOLS]
    out += [f"Bash(fused workbench --env {env} canvas validate:*)" for env, _ in store.run_envs(bot)]
    refs = skills_root()
    if refs and not (sessions._GLOB_CHARS & set(refs + os.path.realpath(refs))):
        out += [f"Read({sessions._rule_path(p)}/**)" for p in sessions._spellings(refs)]
    return out


def skills_root() -> str:
    """The folder of `workbench` skills fused-render fetched for canvas sessions ("" when there is none yet):
    the authoritative canvas.toml, UDF and JSON-UI widget references, which the expert may read."""
    try:
        from fused_render import skill_plugin
        root = skill_plugin.workbench_plugin_root()
        return root if isinstance(root, str) and os.path.isdir(root) else ""
    except Exception:  # noqa: BLE001
        return ""


def note(bot) -> str:
    """The one line a chat that has the tools is told (`store.where(show=True)`; "" without them)."""
    if not on(bot):
        return ""
    skill = os.path.join(store.expert_dir(bot), "skills", SKILL)
    refs = skills_root()
    return ("You can show the user your work"
            + (f" (read {skill} first)" if os.path.isfile(skill) else "")
            + f": `mcp__{SERVER}__{CREATE}` makes a new canvas of theirs and returns its folder, you write its node "
              "files (`<name>.py` UDFs, `<name>.json` widgets) and their canvas.toml rows there without asking, and "
              f"`mcp__{SERVER}__{SHOW}` puts a card in the chat that opens a node or the canvas live. Do this when "
              "the user asks to see, build or change something; they keep improving it by telling you, and you edit "
              "the same files."
            + (f" The canvas.toml, UDF and widget format references are under {refs}." if refs else ""))


# ------------------------------------------------------------- made canvases ---
def _made_path(bot) -> str:
    return os.path.join(store.state_dir(bot), "canvases.json")


def made(bot) -> list:
    """`[{env, name, dir, at}]`: the canvases this expert made with `canvas_create`, oldest first."""
    try:
        with open(_made_path(bot), encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("dir"), str)
            and isinstance(r.get("env"), str) and isinstance(r.get("name"), str)] if isinstance(rows, list) else []


def _note_made(bot, env: str, name: str, folder: str) -> None:
    with _LOCK:
        rows = [r for r in made(bot) if (r["env"], r["name"]) != (env, name)]
        rows.append({"env": env, "name": name, "dir": folder, "at": int(time.time())})
        path = _made_path(bot)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows[-MADE_MAX:], f)
        os.replace(tmp, path)


def made_file(bot, real: str) -> bool:
    """`real` (a real path) is a file the expert writes without a card: directly in the folder of a canvas it
    made, while that folder is still where a clone of that name lives (`canvases._canvas_dir`: a recorded path
    cannot point anywhere else) and not a symlink elsewhere. Not a dot file (the marker, `.fusedignore`), not
    CLAUDE.md, nothing in a subfolder: node files and canvas.toml only."""
    base = os.path.basename(real)
    if not base or base.startswith(".") or base in _NOT_NODES:
        return False
    folder = os.path.dirname(real)
    try:
        from fused_render import canvases
        for row in made(bot):
            if not _NAME_RE.fullmatch(row["name"]) or row["env"] not in store.RUN_ENVS:
                continue
            home = canvases._canvas_dir(row["env"], row["name"])
            # The canvas folder itself must be a real folder under the clones root: one replaced by a link would
            # make "its files" whatever the link points at.
            where = os.path.join(os.path.realpath(os.path.dirname(home)), row["name"])
            if (where == folder and not os.path.islink(home) and os.path.isdir(home)
                    and os.path.realpath(row["dir"]) == folder):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


# --------------------------------------------------------------------- tools ---
def _refusal(response) -> str | None:
    """A canvases route's refusal as one sentence, None for its success dict."""
    if isinstance(response, dict):
        return None
    try:
        body = json.loads(response.body)
        return str(body.get("error") or "request failed")
    except Exception:  # noqa: BLE001
        return "request failed"


def canvas_create(bot, args: dict) -> tuple:
    """`(label, result text, ok)`. Create on Fused, clone, start the watcher: the three calls the Canvases page
    makes, in this process. The watcher is started before the expert writes anything on purpose: a watcher that
    first sees a folder of seconds-old files takes them as already published (`canvases._FRESH_WINDOW_S`)."""
    if not on(bot):
        return (f"create canvas {str(args.get('name') or '').strip() or '?'}",
                "error: canvases are not available to you right now (the Canvases feature is off, or you were "
                "granted no folder of canvas copies).", False)
    return make_canvas(args, [env for env, _ in store.run_envs(bot)], made(bot),
                       lambda env, name, folder: _note_made(bot, env, name, folder))


def make_canvas(args: dict, envs: list, made_rows: list, record, default_env: str = "") -> tuple:
    """`canvas_create` for any kind of bot (a specialist's is specialists/show.py): `envs` the environments it
    may use, `made_rows` the canvases it made before (`[{env, name, ...}]`), `record(env, name, folder)` notes
    a new one, `default_env` the environment when `env` is left out and `envs` holds more than one."""
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


def show(bot, args: dict) -> tuple:
    """`(label, result text, view)`: `view` is the card's payload, None when `path` is nothing to show.
    `{kind: canvas|udf|widget|html|note, path, title, canvas, env}`."""
    return view(args, store.expert_dir(bot))


def view(args: dict, base: str) -> tuple:
    """`show` for any kind of bot: a relative `path` is taken from `base`."""
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


def _shown(title: str) -> str:
    return (f"The card for {title} is in the chat; the user opens it to see it live. Say in one line what it shows; "
            "do not describe how to open it.")

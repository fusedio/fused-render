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
            "to show them (UDFs, widgets). The user is asked to approve it first (call it without asking them in "
            "words; if they deny it, do not retry). Returns the folder and the format references to read. You then "
            "write the node files into that folder yourself with Write / Edit, and their rows in its canvas.toml. "
            "Only when the user asked you to build or show something; never for a throwaway run (use your scratch "
            "folder for those)."),
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
    """The fence's allow rules for a chat that has the tools (`sessions.fence(show=True)`, once per spawn): the two
    tools, the read-only `canvas validate` and `json-ui schemas` per environment, and a Read
    rule for the folder of staged `workbench` skills (the canvas.toml, UDF and widget format references). The rule
    names the stable parent (`skill_plugin.workbench_staged_dir`), not one generation: the skills may arrive after
    the spawn (`ensure_skills` starts the fetch here, off-thread) and a refresh stages a new `g-<stamp>` beside the
    old one. Also kicks that fetch: this is evaluated exactly when such a chat is spawned."""
    from fused_render.experts import sessions
    ensure_skills()
    out = [f"mcp__{SERVER}__{name}" for name in TOOLS]
    for env, _ in store.run_envs(bot):
        # Not `json-ui validate`: given a path it uploads that file's text to Fused, so it is no rule's to allow
        # (Claude Code judges each call). `canvas validate` parses locally; `json-ui schemas` takes names only.
        out += [f"Bash(fused workbench --env {env} canvas validate:*)",
                f"Bash(fused workbench --env {env} json-ui schemas:*)"]
    folders = []
    try:
        from fused_render import skill_plugin
        folders.append(skill_plugin.workbench_staged_dir())
    except Exception:  # noqa: BLE001
        pass
    root = skills_root()
    if root and not any(_inside(root, f) for f in folders):
        folders.append(root)  # a raw clone published because staging failed, or a developer's override
    for refs in folders:
        if sessions._GLOB_CHARS & set(refs + os.path.realpath(refs)):
            continue  # the rule is a glob with no escape: no rule rather than a wider one
        out += [f"Read({sessions._rule_path(p)}/**)" for p in sessions._spellings(refs)]
    return out


def _inside(path: str, folder: str) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(folder) + os.sep)


#: Seconds `canvas_create` waits for a fetch of the workbench skills when none are on this machine yet (a first
#: shallow clone of a small public repo; `skill_plugin` bounds the git calls themselves at 60 s).
SKILLS_WAIT_S = 30.0
#: The format reference for each kind of file the expert writes, under the skills root (`skills_root`).
REFERENCES = (("widget (.json)", ("json-ui-schemas/SKILL.md", "json-ui-schemas/reference.md")),
              ("canvas.toml", ("canvas-toml/SKILL.md",)),
              ("UDF (.py)", ("fused-udfs/SKILL.md",)),
              ("HTML node (.html)", ("html-template-nodes/SKILL.md",)))


def ensure_skills(wait: float = 0.0) -> str:
    """Start the fetch of the `workbench` skills (`canvases._kick_workbench_skills`: off-thread, rate-limited in
    `skill_plugin`, a no-op while one runs) and return `skills_root()`. `wait` > 0 and nothing on this machine yet:
    wait up to that many seconds for the fetch in flight to finish. Never raises, never fails a caller."""
    try:
        from fused_render import canvases
        canvases._kick_workbench_skills()
        if wait > 0 and not skills_root():
            lock = canvases._SKILLS_FETCH_LOCK  # held by the fetch thread until it has synced and exported
            if lock.acquire(timeout=wait):
                lock.release()
    except Exception:  # noqa: BLE001
        pass
    return skills_root()


def skills_root() -> str:
    """The `workbench` skills as fused-render's canvas sessions get them ("" when this machine has none yet): the
    SPLICED generation `skill_plugin` staged and published (`export_workbench_plugin_env`, run by every sync), with
    the standalone-only pull / push guidance taken out. It is read from what that export published in this process
    rather than by calling the export, which copies and hashes the whole tree: this runs on every chat message
    (`store.where`). When nothing is published but a fetched clone is there (fetched by another process), it is
    staged and published now. The raw clone comes back only when `skill_plugin` itself fell back to it."""
    try:
        from fused_render import canvases, skill_plugin as sp
        live = os.environ.get(sp.WORKBENCH_PLUGIN_DIR_ENV) or ""
        if live and sp._is_loadable(live, sp.WORKBENCH_SKILLS):
            return live
        if sp.workbench_plugin_root() and not canvases._SKILLS_FETCH_LOCK.locked():  # a fetch in flight exports
            root = sp.export_workbench_plugin_env()
            return root if root and sp._is_loadable(root, sp.WORKBENCH_SKILLS) else ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def references(root: str) -> list:
    """`[(kind, [absolute paths])]`: the format references under `root` that are there, in `REFERENCES` order."""
    out = []
    for kind, rels in REFERENCES if root else ():
        paths = [os.path.join(root, "skills", *rel.split("/")) for rel in rels]
        if paths := [p for p in paths if os.path.isfile(p)]:
            out.append((kind, paths))
    return out


def _refs_text(root: str) -> str:
    """The references as one sentence for the expert (with `root` known), "" when none is there."""
    refs = references(root)
    if not refs:
        return ""
    parent = os.path.dirname(root)
    return ("Read the format reference for each kind of file before you write it, never guess a format: "
            + "; ".join(f"{kind}: {' and '.join(paths)}" for kind, paths in refs)
            + (f" (if one of these is gone, a newer copy is in the newest `g-*` folder of {parent})."
               if os.path.basename(root).startswith("g-") else "."))


def note(bot) -> str:
    """The one line a chat that has the tools is told (`store.where(show=True)`; "" without them). The references
    are named by their current, concrete paths: `store.where` is hashed per message and a resumed session is told
    again when it changes (`agent_engine._expert_message`), so a new generation of the skills is re-told."""
    if not on(bot):
        return ""
    skill = os.path.join(store.expert_dir(bot), "skills", SKILL)
    root = skills_root()
    refs = _refs_text(root)
    return ("You can show the user your work"
            + (f" (read {skill} first)" if os.path.isfile(skill) else "")
            + f": `mcp__{SERVER}__{CREATE}` asks the user, then makes a new canvas of theirs and returns its folder, "
              "you write its node files (`<name>.py` UDFs, `<name>.json` widgets) and their canvas.toml rows there "
              f"without asking, and `mcp__{SERVER}__{SHOW}` puts a card in the chat that opens a node or the canvas "
              "live. Do this when the user asks to see, build or change something; they keep improving it by telling "
              "you, and you edit the same files. "
            + (refs or "The format references for canvas.toml, UDFs and widgets are not on this machine yet (they "
                       f"are being fetched); `{CREATE}`'s result names them once they are. Until you have read them, "
                       "never guess a format."))


# ------------------------------------------------------------- made canvases ---
#: What the expert writes in a canvas it made without a card: node files, and the canvas file itself.
_NODE_SUFFIXES = frozenset({".py", ".json", ".html", ".md"})


def _made_path(bot) -> str:
    """The record of the canvases this expert made, in the app's own state folder and NOT in the expert's: the
    record decides what the expert writes without a card, so it must not be a file the expert's session works
    beside (its folder is the session's working directory, where a shell command could rewrite it)."""
    from fused_render.shell.storage import home_dir
    folder = os.path.join(home_dir(), "experts", str(getattr(bot, "id", "") or "unknown"))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "canvases.json")


def made(bot) -> list:
    """`[{env, name, dir, at, id, ino}]`: the canvases this expert made with `canvas_create`, oldest first, as
    recorded. A row is only believed while it still names the same canvas (`_folder_of`)."""
    try:
        with open(_made_path(bot), encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("dir"), str)
            and isinstance(r.get("env"), str) and isinstance(r.get("name"), str)] if isinstance(rows, list) else []


def _identity(folder: str) -> dict:
    """What tells this canvas folder from another one of the same name later: the Fused canvas id in its marker
    (`.fused-canvas.json`, written by the clone; a hard pull keeps it) and the folder's own inode."""
    out = {"id": "", "ino": []}
    try:
        from fused_render import canvas_marker
        marker = canvas_marker.read(folder)
        out["id"] = (marker.id or "") if marker is not None else ""
    except Exception:  # noqa: BLE001
        pass
    try:
        st = os.stat(folder)
        out["ino"] = [st.st_dev, st.st_ino]
    except OSError:
        pass
    return out


def _note_made(bot, env: str, name: str, folder: str) -> None:
    with _LOCK:
        rows = [r for r in made(bot) if (r["env"], r["name"]) != (env, name)]
        rows.append({"env": env, "name": name, "dir": folder, "at": int(time.time()), **_identity(folder)})
        path = _made_path(bot)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows[-MADE_MAX:], f)
        os.replace(tmp, path)


def _folder_of(row: dict) -> str:
    """The real folder of a recorded canvas while the record still holds, else "". It holds when the folder is a
    real folder (not a link) at the place a clone of that name lives (`canvases._canvas_dir`: a recorded path
    cannot point anywhere else) AND it is still the canvas that was made: the Fused canvas id in its marker is
    the recorded one, or, for a canvas recorded without an id, the folder is the very same folder (inode). A
    canvas the user deleted and made again under the same name is another canvas: its files ask."""
    try:
        from fused_render import canvases
        if not _NAME_RE.fullmatch(row["name"]) or row["env"] not in store.RUN_ENVS:
            return ""
        home = canvases._canvas_dir(row["env"], row["name"])
        where = os.path.join(os.path.realpath(os.path.dirname(home)), row["name"])
        if os.path.islink(home) or not os.path.isdir(home) or os.path.realpath(row["dir"]) != where:
            return ""
        now = _identity(home)
        was = row.get("id") if isinstance(row.get("id"), str) else ""
        if was:
            return where if now["id"] == was else ""
        return where if row.get("ino") and now["ino"] == row.get("ino") else ""
    except Exception:  # noqa: BLE001
        return ""


def made_dirs(bot) -> list:
    """The real folders of the canvases this expert made that are still those canvases."""
    return [d for d in (_folder_of(r) for r in made(bot)) if d]


def is_mine(bot, env: str, name: str) -> bool:
    """`name` in `env` is a canvas this expert made and its folder is still that canvas."""
    return any((r["env"], r["name"]) == (env, name) and _folder_of(r) for r in made(bot))


def made_file(bot, real: str) -> bool:
    """`real` (a real path) is a file the expert writes without a card: a node file (`.py`, `.json`, `.html`,
    `.md`) or `canvas.toml` directly in the folder of a canvas it made (`_folder_of`). Not a dot file (the marker,
    `.fusedignore`), not CLAUDE.md, nothing in a subfolder, and not a file with a second hard link (an edit of it
    would land on whatever else that link is)."""
    base = os.path.basename(real)
    if not base or base.startswith(".") or base in _NOT_NODES:
        return False
    if base != "canvas.toml" and os.path.splitext(base)[1] not in _NODE_SUFFIXES:
        return False
    try:
        if os.path.dirname(real) not in made_dirs(bot):
            return False
        return not os.path.exists(real) or (os.path.isfile(real) and os.stat(real).st_nlink == 1)
    except Exception:  # noqa: BLE001
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
    name = str(args.get("name") or "").strip()
    label = f"create canvas {name or '?'}"
    if not on(bot):
        return label, "error: canvases are not available to you right now (the Canvases feature is off, or you were " \
                      "granted no folder of canvas copies).", False
    if not _NAME_RE.fullmatch(name):
        return label, "error: a canvas name is letters, digits and underscores only (at most 128).", False
    envs = [env for env, _ in store.run_envs(bot)]
    env = str(args.get("env") or "").strip() or (envs[0] if len(envs) == 1 else "")
    if env not in envs:
        return label, f"error: give `env`, one of: {', '.join(envs)}.", False
    from fused_render import canvases
    folder = canvases._canvas_dir(env, name)
    mine = is_mine(bot, env, name)
    if os.path.isdir(folder) and not mine:
        return label, (f"error: the user already has a canvas named {name} in {env} ({folder}). Pick another name; "
                       "to change that canvas, ask the user first."), False
    again = mine and os.path.isfile(os.path.join(folder, "canvas.toml"))
    if not again:
        why = _refusal(canvases.api_canvases_create({"name": name, "env": env}, x_fused="1"))
        if why is not None:
            return label, f"error: the canvas was not created: {why}", False
        why = _refusal(canvases.api_canvases_clone({"name": name, "env": env}, x_fused="1"))
        if why is not None:
            return label, f"error: the canvas was created on Fused but its folder could not be made: {why}", False
    _note_made(bot, env, name, folder)
    # Best effort: another fused-render may own the clone's sync, and the card's view starts it again anyway.
    started = _refusal(canvases._sync_start_response(env, name)) is None
    # The format references the expert writes from: fetched now if this machine has none yet (bounded; a failed
    # fetch does not fail the tool, the text says so).
    refs = _refs_text(ensure_skills(wait=SKILLS_WAIT_S))
    return label, (
        (f"You already made the canvas {name} ({env}); nothing new was created. Its folder: {folder}\n" if again else
         f"The canvas {name} ({env}) is ready. Its folder: {folder}\n"
         "It may hold one starter node, `udf.py`: keep, change or remove it (file and canvas.toml row together).\n")
        +
        "Write each node as a file at the folder's root with Write / Edit and add its `[[canvas.nodes]]` row to "
        "canvas.toml with a targeted edit (re-read canvas.toml first). These writes need no approval.\n"
        f"Then check it with `fused workbench --env {env} canvas validate {folder}` and call `{SHOW}` with the "
        "node file's path."
        + ("" if started else "\nIts sync has not started yet; it starts when the user opens the card.")
        + "\n" + (refs or "The format references (canvas.toml, UDFs, widgets) could not be fetched to this machine. "
                           "Do not guess a format: copy the shape of a working node from the canvas copies you may "
                           "read, and if there is no widget to copy, show a UDF only.")), True


def show(bot, args: dict) -> tuple:
    """`(label, result text, view)`: `view` is the card's payload, None when `path` is nothing to show.
    `{kind: canvas|udf|widget|html|note, path, title, canvas, env}`."""
    raw = str(args.get("path") or "").strip()
    label = f"show {os.path.basename(raw.rstrip('/')) or '?'}"
    if not raw:
        return label, "error: give `path`.", None
    path = os.path.normpath(os.path.join(store.expert_dir(bot), os.path.expanduser(raw)))
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

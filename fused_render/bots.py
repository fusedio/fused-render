"""Bots store: the on-disk half of the Bots sub-app (/bots).

A bot is a directory under the bots root (`~/Fused-bot`, or `$FUSED_BOT_DIR`):

    <root>/<slug>/
      bot.json          persona + display fields (name, emoji, color, model, effort)
      memory/MEMORY.md  bot-owned memory, plain markdown, edited by the bot's
                        `remember`/`forget` MCP tools and the Memory sheet
      tasks.json        {"tasks": [...]}: every app task this bot started

THE DIRECTORY NAME IS THE SLUG, MINTED ONCE AND NEVER RENAMED. The bot's chat
cwd is this directory, and Claude Code files its session transcripts under
`~/.claude/projects/<munged cwd>/` — renaming the folder would orphan every
chat the bot ever had. So `name` (and everything else in bot.json) is freely
editable, while `slug` is fixed at creation and ignored on update.

Live task status is deliberately NOT stored: tasks.json records what was asked
(id, app, spec), and the state/turn/run/session come from the schedule store at
read time (`schedule.list_entries()`, joined by id). Storing a copy would go
stale the moment the task finished.

Wiring-free: this module must not import `fused_render.server` (the router
imports us). Model/effort validation stays in the router, next to the one
vocabulary routers/apps.py already owns for the composer's pickers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone

from fused_render import app_listing, schedule
from fused_render.shell.seed import fused_dir

BOT_FILE = "bot.json"
MEMORY_REL = os.path.join("memory", "MEMORY.md")
TASKS_FILE = "tasks.json"
MEMORY_SEED = "# Memory\n"

DEFAULT_EMOJI = "🤖"
DEFAULT_COLOR = "#7c5cff"

_SLUG_MAX = 40
# What a slug may look like on the way IN from a URL: the minting rule's own
# output shape. Anything else (dots, slashes, uppercase) can never name a bot,
# and rejecting it here is also the path-traversal guard for every route.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# The editable bot.json fields and their caps. Caps are generous; they exist so
# a runaway client cannot write a megabyte persona into every system prompt.
_EDITABLE = {"name": 200, "persona": 20000, "emoji": 32, "color": 64,
             "model": 32, "effort": 32}

# One lock for every read-modify-write in this process. The only writers are
# this server's own handlers (the MCP server reaches tasks.json/MEMORY.md
# through HTTP), so a process lock is enough; the atomic replace below covers
# a reader racing a writer.
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bots_root() -> str:
    """`$FUSED_BOT_DIR`, else `~/Fused-bot`, normalized. Path only — no I/O.
    The harness (templates/claude/agent.py) resolves the same way to decide
    whether a chat target is a bot, so the two must agree."""
    return os.path.abspath(os.path.expanduser(
        os.environ.get("FUSED_BOT_DIR") or "~/Fused-bot"))


def slugify(name: str) -> str:
    """kebab-case of `name`: lowercase `[a-z0-9]+` runs joined by `-`, capped
    at 40 chars (trailing `-` trimmed after the cut). "" when nothing survives
    (an all-emoji name) — `mint_slug` falls back for that."""
    parts = re.findall(r"[a-z0-9]+", str(name).lower())
    return "-".join(parts)[:_SLUG_MAX].strip("-")


def mint_slug(name: str) -> str:
    """A slug for a NEW bot: `slugify(name)` (or "bot"), suffixed `-2`, `-3`…
    until no directory of that name exists under the root."""
    base = slugify(name) or "bot"
    root = bots_root()
    slug, i = base, 1
    while os.path.exists(os.path.join(root, slug)):
        i += 1
        suffix = f"-{i}"
        slug = base[:_SLUG_MAX - len(suffix)].rstrip("-") + suffix
    return slug


def valid_slug(slug) -> bool:
    return isinstance(slug, str) and bool(_SLUG_RE.match(slug))


def bot_dir(slug: str) -> str | None:
    """The bot's directory, or None when `slug` is malformed or names no bot
    (a directory without bot.json is not a bot)."""
    if not valid_slug(slug):
        return None
    d = os.path.join(bots_root(), slug)
    return d if os.path.isfile(os.path.join(d, BOT_FILE)) else None


def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: str, data) -> None:
    _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _clean_fields(fields: dict) -> dict:
    """The editable subset of `fields`, strings only, stripped and capped.
    Persona keeps its inner whitespace (it is prose); only its ends are
    trimmed. A null is read as "" (clear the field)."""
    out = {}
    for key, cap in _EDITABLE.items():
        if key not in fields:
            continue
        value = fields[key]
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{key!r} must be a string")
        out[key] = value.strip()[:cap]
    return out


def _as_bot(slug: str, data: dict) -> dict:
    """bot.json as the API returns it: every field present (older or
    hand-edited files may miss some), the slug from the DIRECTORY (the
    invariant — a hand-edited slug in the file never wins), plus `path`."""
    d = os.path.join(bots_root(), slug)
    return {
        "name": str(data.get("name") or slug),
        "slug": slug,
        "persona": str(data.get("persona") or ""),
        "emoji": str(data.get("emoji") or DEFAULT_EMOJI),
        "color": str(data.get("color") or DEFAULT_COLOR),
        "model": str(data.get("model") or ""),
        "effort": str(data.get("effort") or ""),
        "created": str(data.get("created") or ""),
        "updated": str(data.get("updated") or ""),
        "path": d,
    }


def get_bot(slug: str) -> dict | None:
    d = bot_dir(slug)
    if d is None:
        return None
    data = _read_json(os.path.join(d, BOT_FILE), {})
    return _as_bot(slug, data if isinstance(data, dict) else {})


def list_bots() -> list[dict]:
    """Every bot under the root, oldest first (a rail of bots reads best in
    creation order: a new bot lands at the bottom, nothing reshuffles when one
    is edited). Missing root → []."""
    root = bots_root()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    bots = [b for b in (get_bot(n) for n in names if valid_slug(n)) if b]
    bots.sort(key=lambda b: (b["created"], b["slug"]))
    return bots


def create_bot(fields: dict) -> dict:
    """Mint the slug, create the directory with bot.json, an empty
    memory/MEMORY.md and an empty tasks.json. Raises ValueError on an empty
    name or a non-string field (model/effort membership is the router's)."""
    clean = _clean_fields(fields)
    if not clean.get("name"):
        raise ValueError("'name' must be a non-empty string")
    now = _now()
    with _LOCK:
        os.makedirs(bots_root(), exist_ok=True)
        slug = mint_slug(clean["name"])
        d = os.path.join(bots_root(), slug)
        # exist_ok=False: mint_slug checked and, under the lock, nothing in
        # this process can race it; another process racing fails loudly here
        # rather than two bots sharing one directory.
        os.makedirs(d)
        data = {"name": clean["name"], "slug": slug,
                "persona": clean.get("persona", ""),
                "emoji": clean.get("emoji") or DEFAULT_EMOJI,
                "color": clean.get("color") or DEFAULT_COLOR,
                "model": clean.get("model", ""),
                "effort": clean.get("effort", ""),
                "created": now, "updated": now}
        _write_atomic(os.path.join(d, MEMORY_REL), MEMORY_SEED)
        _write_json(os.path.join(d, TASKS_FILE), {"tasks": []})
        # bot.json LAST: it is what makes the directory a bot (bot_dir), so a
        # crash midway leaves a directory nobody lists rather than a bot with
        # no memory file.
        _write_json(os.path.join(d, BOT_FILE), data)
    return _as_bot(slug, data)


def update_bot(slug: str, fields: dict) -> dict | None:
    """Merge the editable fields into bot.json. `slug` in `fields` is ignored
    (the directory name is the slug, forever). None when no such bot; raises
    ValueError on an emptied name or a non-string field."""
    clean = _clean_fields(fields)
    if "name" in clean and not clean["name"]:
        raise ValueError("'name' must be a non-empty string")
    with _LOCK:
        d = bot_dir(slug)
        if d is None:
            return None
        path = os.path.join(d, BOT_FILE)
        data = _read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        data.update(clean)
        data["slug"] = slug
        data["updated"] = _now()
        _write_json(path, data)
    return _as_bot(slug, data)


def delete_bot(slug: str) -> bool:
    """Remove the bot's directory. Its chat transcripts under ~/.claude are
    left alone — they are Claude Code's, not ours."""
    with _LOCK:
        d = bot_dir(slug)
        if d is None:
            return False
        shutil.rmtree(d)
    return True


# ------------------------------------------------------------------ memory

def memory_path(slug: str) -> str | None:
    d = bot_dir(slug)
    return None if d is None else os.path.join(d, MEMORY_REL)


def read_memory(slug: str) -> str | None:
    """MEMORY.md text; the seed when the file went missing; None when no bot."""
    p = memory_path(slug)
    if p is None:
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return MEMORY_SEED


def write_memory(slug: str, content: str) -> bool:
    p = memory_path(slug)
    if p is None:
        return False
    with _LOCK:
        _write_atomic(p, content)
    return True


# ------------------------------------------------------------------ tasks

def _stored_tasks(d: str) -> list[dict]:
    data = _read_json(os.path.join(d, TASKS_FILE), {})
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []


def with_live(task: dict, entries: list[dict] | None = None) -> dict:
    """`task` with its schedule entry's live fields merged in: state, turn,
    error, and run_id / claude_session_id when the entry has them (the send
    may have resolved after the record was written). A task whose entry is
    gone (deleted from the Tasks page) keeps its stored ids and reads state
    "" — unknown, not failed."""
    if entries is None:
        entries = schedule.list_entries()
    tid = str(task.get("id") or "")
    live = (next((e for e in entries if str(e.get("id") or "") == tid), None)
            if tid else None)
    out = dict(task)
    if live is None:
        out.setdefault("state", "")
        out.setdefault("turn", "")
        out.setdefault("error", "")
        return out
    out["state"] = str(live.get("state") or "")
    out["turn"] = str(live.get("turn") or "")
    out["error"] = str(live.get("error") or "")
    run_id = str(live.get("run_id") or "")
    session = str(live.get("claude_session_id") or live.get("session_id") or "")
    if run_id:
        out["run_id"] = run_id
    if session:
        out["claude_session_id"] = session
    return out


def list_tasks(slug: str) -> list[dict] | None:
    """The bot's tasks in the order they were started (OLDEST first — the
    file's own order), each joined to its live schedule status. None when no
    bot."""
    d = bot_dir(slug)
    if d is None:
        return None
    entries = schedule.list_entries()
    return [with_live(t, entries) for t in _stored_tasks(d)]


def task_record(*, entry: dict, kind: str, app_name: str, app_path: str,
                entry_html: str, spec: str) -> dict:
    """The tasks.json row for a task just started from `entry` (the schedule
    entry `_create_app_task` returned). `spec` is the bot's spec as written,
    not the framed prompt the builder received."""
    return {
        "id": str(entry.get("id") or ""),
        "kind": kind,
        "app_name": app_name,
        "app_path": app_path,
        "entry_html": entry_html,
        "spec": spec,
        "run_id": str(entry.get("run_id") or ""),
        "claude_session_id": str(entry.get("claude_session_id")
                                 or entry.get("session_id") or ""),
        "created": _now(),
    }


def append_task(slug: str, record: dict) -> bool:
    with _LOCK:
        d = bot_dir(slug)
        if d is None:
            return False
        tasks = _stored_tasks(d)
        tasks.append(record)
        _write_json(os.path.join(d, TASKS_FILE), {"tasks": tasks})
    return True


# ------------------------------------------------------------------ apps

def apps_root() -> str:
    """Where new apps are created (`POST /api/apps/new`): <fused_dir>/local."""
    return os.path.join(fused_dir(), "local")


def list_apps() -> list[dict]:
    """The workspace's `local` apps, name order: every direct child folder
    whose `app_listing.app_entry` resolves. The fused-app meta marker is THE
    signal (D301), not "has an index.html" — the name-based guess D301
    retired. Hidden and unreadable folders are skipped."""
    root = apps_root()
    try:
        names = sorted(os.listdir(root), key=str.lower)
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith("."):
            continue
        p = os.path.join(root, n)
        if not os.path.isdir(p):
            continue
        try:
            entry = app_listing.app_entry(p)
        except OSError:
            continue
        if entry:
            out.append({"name": n, "path": os.path.abspath(p), "entry_html": entry})
    return out


def resolve_app(path: str) -> tuple[str, str] | None:
    """(app folder, entry html) for an edit target given as either the folder
    or one of its pages. None when it names no app."""
    p = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(p) and p.lower().endswith(".html"):
        return os.path.dirname(p), p
    if os.path.isdir(p):
        try:
            entry = app_listing.app_entry(p)
        except OSError:
            return None
        if entry:
            return p, entry
    return None

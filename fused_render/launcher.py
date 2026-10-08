"""The launcher's data: what it can open, how a query ranks it, its settings.

The launcher (the ``/launcher`` page in ``launcher_panel.py``) is a
Spotlight-like panel on a global shortcut (⌥Space by default): an empty
query lists the RECENTLY OPENED apps (``routers.apps.recent_apps`` — the
clock Home's strip reads, stamped whenever an app page renders, D301),
newest open first, with the sidebar's desk (``current_apps``, newest-added
first) filling any gap below them; typing searches every app this machine
knows — those, every app in the workspace (``app_listing.workspace_apps``,
which covers the ``showcase`` and ``local`` tags), the linked folders
(``registered_apps``) and the exported ``.fused`` files the index knows
(``exported_apps``). The same set the /apps hub shows. Under the app rows, a non-empty query also lists files
and folders from the file index (``file_results``): the home search box's
engine, a handful of rows, opened in the explorer.

Under Fused Bot (``_flavor.is_bot()``) the same panel lists BOTS instead:
the bots page's list, read through ``dock._bot_rows`` (the menu-bar Dock's
reader, so bot.json is read one way only), pinned bots first by name, then
the rest most recently updated first, every one in the empty-query list. No
workspace walk, no desk, no recents store, no files: a bot is not a folder,
and the Bot app has no explorer to open a file in. A pick selects the bot in
a Bots window (``app.py``'s ``_open_bot_native``, the Dock's door too).

Settings live in ``prefs.json`` (shell/prefs.py), two keys per flavor::

    Fused Render  {"launcher_hotkey": "alt+space",
                   "launcher_row_modifier": "alt"}
    Fused Bot     {"bot_launcher_hotkey": "alt+shift+space",
                   "bot_launcher_row_modifier": "alt+shift"}

Both apps share ONE prefs.json (one tree, owner's call) and run side by
side, so the bot launcher stores under its own keys (``hotkey_key()`` /
``row_modifier_key()``) and defaults to combinations Render does not hold:
Carbon's ``RegisterEventHotKey`` refuses a combo another process already
registered, so equal defaults would leave whichever app started second with
no shortcut. The WIRE names on ``PUT /api/prefs`` stay ``launcher_hotkey`` /
``launcher_row_modifier`` in both apps (the page never knows the flavor);
the server maps them to the flavor's key, so the bot keys are stored but
never surfaced under Render.

The hotkey opens the launcher (``hotkey.py`` spec syntax). The row
modifier is the modifier (or ``+``-joined modifiers) that,
with a digit 1–9, opens the Nth app of the empty-query list (nine global
shortcuts, resolved at press time — the Nth recently opened app, the desk
behind them); while the launcher is up, the Nth row shown. The SAME list
when the query is empty, by design (owner, 2026-10-03). ``<modifier>+0``
opens the shell home. One knob from the user's point of view. Missing or
corrupt → the flavor's defaults (``default_hotkey()`` /
``default_row_modifier()``).

``search`` is pure and ranks by match quality then by registry order: a
name that starts with the query, then a word inside the name that does,
then the query as a substring, then its letters in order (``"os"`` finds
``OpenSVG``). Page titles are searched too. Case-insensitive throughout.

``native_hooks`` is what the uvicorn thread may call into AppKit-land:
`app.py` fills it once the panel exists (``rebind``, ``hotkey_bound``,
``pinned_bound``, ``open_keys``); each hook hops to the main thread itself.
Empty on every other platform and under ``fused-render serve``, where the
settings still store but nothing binds.

Ported from Render App (fused-render-lite `launcher.py`); the registry and
the settings store are main's.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import urllib.parse

from fused_render import _flavor, hotkey
from fused_render._view_url_codec import (app_page_path, canonical_fs_path, embed_url_path,
                                          view_url_path)

logger = logging.getLogger(__name__)

MAX_RESULTS = 9  # one <modifier>-digit each
DEFAULT_ROW_MODIFIER = "alt"
#: The bot flavor's defaults: one modifier more than Render's, so both apps'
#: shortcuts bind when they run side by side (see the module docstring).
BOT_DEFAULT_HOTKEY = "alt+shift+space"
BOT_DEFAULT_ROW_MODIFIER = "alt+shift"

#: Filled by app.py on macOS once the panel exists. See the module docstring.
native_hooks: dict = {}


# ---- settings (prefs.json) --------------------------------------------------------

def _read_prefs() -> dict:
    from fused_render.shell.prefs import read_prefs

    return read_prefs()


def hotkey_key() -> str:
    """The prefs.json key THIS flavor's launcher hotkey is stored under."""
    return "bot_launcher_hotkey" if _flavor.is_bot() else "launcher_hotkey"


def row_modifier_key() -> str:
    """The prefs.json key THIS flavor's row modifier is stored under."""
    return "bot_launcher_row_modifier" if _flavor.is_bot() else "launcher_row_modifier"


def default_hotkey() -> str:
    return BOT_DEFAULT_HOTKEY if _flavor.is_bot() else hotkey.DEFAULT_SPEC


def default_row_modifier() -> str:
    return BOT_DEFAULT_ROW_MODIFIER if _flavor.is_bot() else DEFAULT_ROW_MODIFIER


def get_hotkey() -> str:
    """The stored shortcut spec, canonical; the default when absent or invalid."""
    raw = _read_prefs().get(hotkey_key())
    try:
        return hotkey.canonical(raw) if isinstance(raw, str) else default_hotkey()
    except hotkey.SpecError:
        return default_hotkey()


def canonical_hotkey(spec) -> str:
    """``spec`` canonicalised for storing; raises ``hotkey.SpecError``."""
    return hotkey.canonical(str(spec or ""))


def canonical_modifiers(spec) -> str:
    """``"cmd+alt"`` → ``"alt+cmd"``; SpecError when empty or not all modifiers."""
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    if not parts:
        raise hotkey.SpecError("pick at least one modifier")
    names = set()
    for m in parts:
        m = hotkey.MODIFIER_ALIASES.get(m, m)
        if m not in hotkey.MODIFIERS:
            raise hotkey.SpecError(f"unknown modifier {m!r}")
        names.add(m)
    return "+".join(m for m in hotkey.MODIFIER_ORDER if m in names)


def get_row_modifier() -> str:
    raw = _read_prefs().get(row_modifier_key())
    try:
        return canonical_modifiers(raw) if isinstance(raw, str) else default_row_modifier()
    except hotkey.SpecError:
        return default_row_modifier()


def pinned_specs(modifier: str) -> list[str]:
    """The nine specs ``<modifier>+1`` … ``+9``; empty when off."""
    return [f"{modifier}+{n}" for n in range(1, 10)] if modifier else []


def home_spec(modifier: str) -> str | None:
    """``<modifier>+0`` opens the shell home."""
    return f"{modifier}+0" if modifier else None


def modifier_display(spec: str) -> str:
    """``"alt+cmd"`` → ``"⌥⌘"``."""
    names = set(str(spec or "").split("+"))
    return "".join(hotkey.MODIFIER_SYMBOLS[m] for m in hotkey.MODIFIER_ORDER if m in names)


def settings() -> dict:
    """What the pages read: both shortcuts with display forms, whether the
    panel exists in THIS process (the packaged macOS app installs the
    hooks; a `fused-render serve` on a Mac has no panel, so the Preferences
    section stays hidden there), whether the system accepted the
    bindings (None = nothing has tried), and ``kind``, what the rows are
    (``"bots"`` under Fused Bot, else ``"apps"``), so the pages word
    themselves without knowing the flavor."""
    spec, row = get_hotkey(), get_row_modifier()
    bound = native_hooks.get("hotkey_bound")
    pinned = native_hooks.get("pinned_bound")
    return {
        "available": sys.platform == "darwin" and "rebind" in native_hooks,
        "kind": "bots" if _flavor.is_bot() else "apps",
        "hotkey": spec,
        "display": hotkey.display(spec),
        "row_modifier": row,
        "row_modifier_display": modifier_display(row),
        "bound": bound() if bound is not None else None,
        "pinned_bound": pinned() if pinned is not None else None,
    }


def notify_settings_changed(hotkey_spec: str | None) -> None:
    """After a settings write: rebind the launcher shortcut (``hotkey_spec``
    given) or just refresh the row shortcuts and the page (None). A no-op
    where no panel exists."""
    rebind = native_hooks.get("rebind")
    if rebind is not None:
        rebind(hotkey_spec)


# ---- registry ------------------------------------------------------------------------

HOME_ROW = {"home": True, "path": "", "url": "/", "name": _flavor.display_name(),
            "title": _flavor.display_name(), "kind": "home", "pinned": False,
            "recent": False, "running": False, "icon": None}

_CACHE_TTL_S = 2.0
_cache_lock = threading.Lock()
_cache: tuple[float, list[dict]] | None = None


def _icon_url(icon: str | None, mtime) -> str | None:
    if not icon:
        return None
    return ("/api/fs/raw?path=" + urllib.parse.quote(icon, safe="/")
            + "&v=" + urllib.parse.quote(str(mtime or "")))


def _folder_row(a: dict, *, pinned: bool, recent: bool = False) -> dict:
    path = canonical_fs_path(os.path.abspath(a["path"])).rstrip("/") or a["path"]
    return {
        "path": path,
        "url": app_page_path(path),
        "name": a.get("name") or os.path.basename(path),
        "title": a.get("title") or a.get("name") or os.path.basename(path),
        "kind": "app",
        "pinned": pinned,
        "recent": recent,
        "running": False,
        "icon": _icon_url(a.get("icon"), a.get("icon_mtime")),
    }


def _appfile_row(a: dict, *, recent: bool = False) -> dict:
    path = a["path"]
    return {
        "path": path,
        "url": embed_url_path(path),
        "name": a.get("name") or os.path.basename(path),
        "title": a.get("name") or os.path.basename(path),
        "kind": "appfile",
        "pinned": False,
        "recent": recent,
        "running": False,
        "icon": None,
    }


def recent_rows(limit: int = MAX_RESULTS) -> list[dict]:
    """The recently opened apps, newest open first — Home's definition
    (`routers.apps.recent_apps`), so the panel and the Home strip never
    disagree about what was opened last. Folders carry the page's title the
    way desk rows do; ``pinned`` says whether the row is also on the desk.
    Never raises: a store that cannot be read is zero rows, and the desk
    still fills the empty query."""
    from fused_render import app_listing, current_apps

    try:
        from fused_render.server.routers.apps import recent_apps

        apps = recent_apps(limit)
    except Exception:  # noqa: BLE001 — see docstring
        logger.debug("recent apps failed", exc_info=True)
        return []
    # realpath on both sides: the desk stores canonical abspaths, `app_dict`
    # hands back realpaths, and a symlinked workspace spells them apart.
    try:
        desk = {os.path.realpath(a["path"]) for a in current_apps.read_state()["apps"]}
    except Exception:  # noqa: BLE001
        desk = set()
    rows = []
    for a in apps:
        if a.get("kind") == "appfile":
            rows.append(_appfile_row(a, recent=True))
            continue
        entry = a.get("entry")
        title = app_listing.entry_title(entry) if entry else None
        pinned = os.path.realpath(a["path"]) in desk
        rows.append(_folder_row({**a, "title": title}, pinned=pinned, recent=True))
    return rows


def desk_rows() -> list[dict]:
    """The sidebar's desk (``current_apps``), NEWEST-ADDED FIRST — the order
    the sidebar seeds before any drag (a drag reorder lives in the browser's
    localStorage and is not visible here). In the launcher's empty-query
    list they trail the recently opened apps (`search`). Folders that are
    gone are skipped."""
    from fused_render import app_listing, current_apps

    rows = []
    for a in reversed(current_apps.list_apps()):
        if not a.get("exists"):
            continue
        entry = a.get("entry")
        title = app_listing.entry_title(entry) if entry else None
        rows.append(_folder_row({**a, "title": title}, pinned=True))
    return rows


def _bot_registry() -> list[dict]:
    """The bot flavor's rows: every listed bot (``dock._bot_rows``, the one
    bot.json reader the Dock uses too), pinned first by name (casefold, the
    Dock's order), then the rest most recently updated first, marked
    ``recent`` so the empty query lists them all (`search`'s one-list rule,
    so ``nth_pinned`` and the panel agree). ``path`` is the bot id: the key
    the open callback receives. Never raises: a bots store that cannot be
    read is zero rows."""
    from fused_render import dock

    try:
        bots = dock._bot_rows()  # noqa: SLF001 — the Dock's reader, deliberately shared
    except Exception:  # noqa: BLE001 — see docstring
        logger.debug("bot rows failed", exc_info=True)
        return []
    pinned = sorted((b for b in bots if b["pinned"]), key=lambda b: (b["name"].casefold(), b["id"]))
    rest = sorted((b for b in bots if not b["pinned"]), key=lambda b: -b["updated"])

    def row(b: dict, *, pinned: bool) -> dict:
        return {"kind": "bot", "id": b["id"], "path": b["id"], "url": dock.bot_view_path(b["id"]),
                "name": b["name"], "title": b["name"], "icon": None, "face": b["face"],
                "status": b["status"], "running": b["running"], "pinned": pinned,
                "recent": not pinned}

    return [row(b, pinned=True) for b in pinned] + [row(b, pinned=False) for b in rest]


def _registry_uncached() -> list[dict]:
    if _flavor.is_bot():
        return _bot_registry()
    from fused_render import app_listing, exported_apps, registered_apps
    from fused_render.shell.seed import fused_dir

    rows: list[dict] = []
    seen: set[str] = set()

    def key_of(path: str) -> str:
        return os.path.realpath(path)

    for r in recent_rows() + desk_rows():
        k = key_of(r["path"])
        if k in seen:
            continue
        seen.add(k)
        rows.append(r)
    listed: list[dict] = []
    try:
        listed.extend(app_listing.workspace_apps(fused_dir()))
    except Exception:  # noqa: BLE001 — a workspace that cannot be walked is zero rows
        logger.debug("workspace walk failed", exc_info=True)
    try:
        listed.extend(registered_apps.registered_apps())
    except Exception:  # noqa: BLE001
        logger.debug("registered apps failed", exc_info=True)
    for a in listed:
        if not a.get("entry"):
            continue
        k = key_of(a["path"])
        if k in seen:
            continue
        seen.add(k)
        rows.append(_folder_row(a, pinned=False))
    try:
        files = exported_apps.exported_apps()
    except Exception:  # noqa: BLE001
        logger.debug("exported apps failed", exc_info=True)
        files = []
    for a in files:
        k = key_of(a["path"])
        if k in seen:
            continue
        seen.add(k)
        rows.append(_appfile_row(a))
    return rows


def registry(running=frozenset()) -> list[dict]:
    """Every app the launcher can open: the recently opened ones first
    (newest open first), then the rest of the desk (newest-added first),
    then the workspace + linked apps, then exported ``.fused`` files. Cached
    for a couple of seconds: the workspace walk is not free and a query
    arrives per keystroke. ``running`` marks the rows whose path a window
    shows. Ties in a search keep this order, so a recent app outranks a
    never-opened one with the same match.

    Rows: ``{path, url, name, title, kind, pinned, recent, running, icon}``.
    Under Fused Bot the rows are bots (`_bot_registry`, plus ``id``,
    ``face``, ``status``); their ``running`` is the bot's own status, not a
    window, so ``running`` (window paths) is not applied to them.
    """
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now - _cache[0] < _CACHE_TTL_S:
            rows = _cache[1]
        else:
            rows = _registry_uncached()
            _cache = (now, rows)
    running_abs = {os.path.realpath(p) for p in running}
    return [r if r.get("kind") == "bot"
            else {**r, "running": os.path.realpath(r["path"]) in running_abs} for r in rows]


def invalidate() -> None:
    """Drop the registry cache (tests; a desk mutation could call it too)."""
    global _cache
    with _cache_lock:
        _cache = None


def nth_pinned(n: int) -> dict | None:
    """The ROW the global ``<modifier>+n`` opens (1-based): the ``n``-th row
    of the empty-query list — exactly what the panel shows before any typing,
    so the digit means the same app (or bot) whether or not the panel is up.
    A row rather than a path because the caller dispatches on ``kind`` (an
    app path vs a bot id). Through `registry`, so the per-keystroke cache
    serves a press too. None past the end."""
    rows = search("", registry())
    return rows[n - 1] if 1 <= n <= len(rows) else None


# ---- search ----------------------------------------------------------------------------

def _subsequence(q: str, s: str) -> bool:
    it = iter(s)
    return all(ch in it for ch in q)


def _score(q: str, row: dict) -> int | None:
    """Lower is better; None when the row does not match."""
    best = None
    for field, penalty in ((row.get("name") or "", 0), (row.get("title") or "", 1)):
        s = field.lower()
        if not s:
            continue
        if s.startswith(q):
            score = 0
        elif any(w.startswith(q) for w in s.replace("-", " ").replace("_", " ").split()):
            score = 10
        elif q in s:
            score = 20
        elif _subsequence(q, s):
            score = 30
        else:
            continue
        score += penalty
        if best is None or score < best:
            best = score
    return best


def search(query: str, rows: list[dict], limit: int = MAX_RESULTS) -> list[dict]:
    """An empty query → the recently opened rows, then the desk rows, in
    registry order (recents are already newest first, the desk newest-added
    first, and a row on both lists appears once, where its open put it).
    Otherwise every row that matches, best match first, ties in registry
    order."""
    q = str(query or "").strip().lower()
    if not q:
        return [r for r in rows if r.get("recent") or r.get("pinned")][:limit]
    scored = []
    for i, r in enumerate(rows):
        s = _score(q, r)
        if s is not None:
            scored.append((s, i, r))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [r for _s, _i, r in scored[:limit]]


def results(query: str, running=frozenset()) -> list[dict]:
    """``search`` over the live registry, the home row always last — never
    counted against the limit, never filtered by the query: the shell
    itself, on ``<modifier>+0``."""
    out = list(search(query, registry(running)))
    out.append(dict(HOME_ROW))
    return out


# ---- files -----------------------------------------------------------------------------

#: Files and folders shown under the app rows for a non-empty query. Fewer
#: than the apps: they are the second answer, and the panel grows per row.
FILE_RESULTS = 6

#: Where a launcher file search looks: the same root as the shell's home
#: search box. A query may still walk out of it (``~/x``, ``/x``) — that is
#: `resolve_query`'s business, and the launcher inherits it unchanged.
FILE_SEARCH_ROOT = "~"


def _file_row(path: str, is_dir: bool) -> dict:
    path = canonical_fs_path(os.path.abspath(path)).rstrip("/") or path
    return {
        "path": path,
        "url": view_url_path(path),
        "name": os.path.basename(path) or path,
        "title": os.path.basename(path) or path,
        "kind": "folder" if is_dir else "file",
        "pinned": False,
        "recent": False,
        "running": False,
        "icon": None,
    }


def file_results(query: str, limit: int = FILE_RESULTS, exclude=()) -> dict:
    """Files and folders under ``~`` matching ``query``, from the file index
    — the ranked engine behind the shell's home search (`_rank_body`, so
    ``~``/``/`` bases, whitespace wildcards and the mount guard all behave
    exactly as they do there). Returns ``{"files": [rows], "reason": str}``:
    ``reason`` is the index's own coverage word (``""`` when it answered,
    else ``scanning``/``uncovered``/``mount``/``package``), so the page can
    say WHY there are no files rather than "no matches".

    Never raises and never blocks on anything but the index query: an
    empty query, a missing index, a closed engine, an import failure on a
    build without duckdb all read as zero rows. App search must not fail
    because file search did. ``exclude`` holds paths already shown as app
    rows, so an app folder is not listed twice."""
    q = str(query or "").strip()
    if not q:
        return {"files": [], "reason": ""}
    try:
        from fused_render.index.config import load_config
        from fused_render.server.routers.index import _rank_body, _rank_reason

        cfg = load_config()
        root = os.path.expanduser(FILE_SEARCH_ROOT)
        # A few over the cap: the app-row overlap is dropped below.
        out = _rank_body(cfg, root, q, limit=limit + len(exclude) + 4)
        base = str(out.get("base") or root)
        if not out.get("hits"):
            # `_rank_body` only ever says `uncovered`/`package`; "a scan is
            # running" and "that is a mount" are `_rank_reason`'s words, the
            # same call the home search route makes — without it a home still
            # being scanned read as permanently unindexed.
            out["reason"] = _rank_reason(cfg, base, out)
    except Exception:  # noqa: BLE001 — see docstring
        logger.debug("launcher file search failed", exc_info=True)
        return {"files": [], "reason": ""}
    skip = {os.path.realpath(p) for p in exclude}
    rows: list[dict] = []
    for hit in out.get("hits") or []:
        rel = hit.get("rel") or ""
        path = os.path.join(base, rel) if rel else base
        if os.path.realpath(path) in skip:
            continue
        rows.append(_file_row(path, bool(hit.get("is_dir"))))
        if len(rows) >= limit:
            break
    return {"files": rows, "reason": str(out.get("reason") or "")}

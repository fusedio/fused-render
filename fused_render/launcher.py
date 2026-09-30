"""The launcher's data: what it can open, how a query ranks it, its settings.

The launcher (``static/launcher.html`` in ``launcher_panel.py``) is a
Spotlight-like panel on a global shortcut (⌥Space by default): an empty
query lists the apps on the sidebar's desk (``current_apps``, newest first),
and typing searches every app this machine knows — the desk, every app in
the workspace (``app_listing.workspace_apps``, which covers the ``showcase``
and ``local`` tags), the linked folders (``registered_apps``) and the
exported ``.fused`` files the index knows (``exported_apps``). The same set
the /apps hub shows. Under the app rows, a non-empty query also lists files
and folders from the file index (``file_results``): the home search box's
engine, a handful of rows, opened in the explorer.

Settings live in ``prefs.json`` (shell/prefs.py), two keys::

    {"launcher_hotkey": "alt+space", "launcher_row_modifier": "alt"}

``launcher_hotkey`` opens the launcher (``hotkey.py`` spec syntax).
``launcher_row_modifier`` is the modifier (or ``+``-joined modifiers) that,
with a digit 1–9, opens the Nth app: from anywhere, the Nth DESK app (nine
global shortcuts); while the launcher is up, the Nth row — the same list
when the query is empty. ``<modifier>+0`` opens the shell home. One knob from
the user's point of view. Missing or corrupt → the defaults.

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

from fused_render import hotkey
from fused_render._view_url_codec import (app_page_path, canonical_fs_path, embed_url_path,
                                          view_url_path)

logger = logging.getLogger(__name__)

MAX_RESULTS = 9  # one <modifier>-digit each
DEFAULT_ROW_MODIFIER = "alt"
HOTKEY_KEY = "launcher_hotkey"
ROW_MODIFIER_KEY = "launcher_row_modifier"

#: Filled by app.py on macOS once the panel exists. See the module docstring.
native_hooks: dict = {}


# ---- settings (prefs.json) --------------------------------------------------------

def _read_prefs() -> dict:
    from fused_render.shell.prefs import read_prefs

    return read_prefs()


def get_hotkey() -> str:
    """The stored shortcut spec, canonical; the default when absent or invalid."""
    raw = _read_prefs().get(HOTKEY_KEY)
    try:
        return hotkey.canonical(raw) if isinstance(raw, str) else hotkey.DEFAULT_SPEC
    except hotkey.SpecError:
        return hotkey.DEFAULT_SPEC


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
    raw = _read_prefs().get(ROW_MODIFIER_KEY)
    try:
        return canonical_modifiers(raw) if isinstance(raw, str) else DEFAULT_ROW_MODIFIER
    except hotkey.SpecError:
        return DEFAULT_ROW_MODIFIER


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
    section stays hidden there), and whether the system accepted the
    bindings (None = nothing has tried)."""
    spec, row = get_hotkey(), get_row_modifier()
    bound = native_hooks.get("hotkey_bound")
    pinned = native_hooks.get("pinned_bound")
    return {
        "available": sys.platform == "darwin" and "rebind" in native_hooks,
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

HOME_ROW = {"home": True, "path": "", "url": "/", "name": "Fused Render",
            "title": "Fused Render", "kind": "home", "pinned": False,
            "running": False, "icon": None}

_CACHE_TTL_S = 2.0
_cache_lock = threading.Lock()
_cache: tuple[float, list[dict]] | None = None


def _icon_url(icon: str | None, mtime) -> str | None:
    if not icon:
        return None
    return ("/api/fs/raw?path=" + urllib.parse.quote(icon, safe="/")
            + "&v=" + urllib.parse.quote(str(mtime or "")))


def _folder_row(a: dict, *, pinned: bool) -> dict:
    path = canonical_fs_path(os.path.abspath(a["path"])).rstrip("/") or a["path"]
    return {
        "path": path,
        "url": app_page_path(path),
        "name": a.get("name") or os.path.basename(path),
        "title": a.get("title") or a.get("name") or os.path.basename(path),
        "kind": "app",
        "pinned": pinned,
        "running": False,
        "icon": _icon_url(a.get("icon"), a.get("icon_mtime")),
    }


def desk_rows() -> list[dict]:
    """The sidebar's desk (``current_apps``), NEWEST-ADDED FIRST — the order
    the sidebar seeds before any drag (a drag reorder lives in the browser's
    localStorage and is not visible here). ``<modifier>+N`` opens the Nth of
    these. Folders that are gone are skipped."""
    from fused_render import app_listing, current_apps

    rows = []
    for a in reversed(current_apps.list_apps()):
        if not a.get("exists"):
            continue
        entry = a.get("entry")
        title = app_listing.entry_title(entry) if entry else None
        rows.append(_folder_row({**a, "title": title}, pinned=True))
    return rows


def _registry_uncached() -> list[dict]:
    from fused_render import app_listing, exported_apps, registered_apps
    from fused_render.shell.seed import fused_dir

    rows: list[dict] = []
    seen: set[str] = set()

    def key_of(path: str) -> str:
        return os.path.realpath(path)

    for r in desk_rows():
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
        path = a["path"]
        k = key_of(path)
        if k in seen:
            continue
        seen.add(k)
        rows.append({
            "path": path,
            "url": embed_url_path(path),
            "name": a.get("name") or os.path.basename(path),
            "title": a.get("name") or os.path.basename(path),
            "kind": "appfile",
            "pinned": False,
            "running": False,
            "icon": None,
        })
    return rows


def registry(running=frozenset()) -> list[dict]:
    """Every app the launcher can open, desk first (newest first), then the
    workspace + linked apps, then exported ``.fused`` files. Cached for a
    couple of seconds: the workspace walk is not free and a query arrives
    per keystroke. ``running`` marks the rows whose path a window shows.

    Rows: ``{path, url, name, title, kind, pinned, running, icon}``.
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
    return [{**r, "running": os.path.realpath(r["path"]) in running_abs} for r in rows]


def invalidate() -> None:
    """Drop the registry cache (tests; a desk mutation could call it too)."""
    global _cache
    with _cache_lock:
        _cache = None


def nth_pinned(n: int) -> str | None:
    """The folder of the ``n``-th desk app (1-based, newest first), or None."""
    rows = desk_rows()
    return rows[n - 1]["path"] if 1 <= n <= len(rows) else None


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
    """An empty query → the pinned (desk) rows, in order. Otherwise every
    row that matches, best match first, ties in registry order."""
    q = str(query or "").strip().lower()
    if not q:
        return [r for r in rows if r.get("pinned")][:limit]
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

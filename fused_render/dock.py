"""The menu-bar Dock's tiles and pins (menubar_dock.py hosts the page at
`/dock`; server/routers/dock.py is the HTTP face). Pure Python, no AppKit —
the same module answers `fused-render serve` and the tests.

What the tray shows depends on the FLAVOR (`_flavor.is_bot()`), not on a
preference:

    Fused Render    tiles are APPS — every app this machine can open
                    (the launcher's registry: workspace, linked folders,
                    exported `.fused` files)
    Fused Bot       tiles are BOTS — the bots page's list (bot.json rows)

Either way the tray is laid out like the Dock: a Home tile, then the PINNED
zone, a separator, then up to `RECENT_CAP` RECENT rows that are not pinned.

Pins:

* Apps pin into ``<home>/dock.json`` (`{"pinned": [<abs path>], "tilesize":
  <px>}`): the pinned app paths in tray order (left to right), each a folder
  with a declared entry page or a `.fused` file. `shell.storage.home_dir()`,
  so a branch build keeps its own pins and both flavors (one tree, owner's
  call) read the same file. A pinned path that is gone is skipped, never
  pruned — the row is the user's to remove. Paths are stored in the
  LISTING'S SPELLING (`canonical_fs_path` of the folder, as `app_dir_for`
  hands it back), not the real path: a window is keyed on the path the shell
  opened it with (window_policy.window_key_of), and in a symlinked workspace
  a real path would never match it, so a Dock click would open a second
  window instead of raising the one there. Real paths are used only to
  COMPARE (one pin per folder however it is spelled).
* Bots pin where the bots page's own pin already lives (bot.json `pinned`,
  written through the registry's Bot under its lock exactly as the sidebar's
  flag route does), so one pin means the same thing in both places.

Recents:

* Apps: `routers.apps.recent_apps` — the ONE recency clock on this machine
  (Home's strip and the ⌥Space launcher read it too, owner's rule).
* Bots: the most recently updated bots (bot.json `updated`).

Bot rows are read from disk (`store.list_ids` + `read_meta`) — a Bot the
registry already built is read from memory so its live status shows — and
never constructed here: building a Bot writes its bot.json and pulls in the
Chrome/CDP code, which a 1.5 s poll has no business doing. A `hidden` bot is
never listed. App rows are built with the launcher's row builders, so `icon`
is already the `/api/fs/raw` URL the page can put in an <img>. `entries()`
never walks the workspace (the launcher's registry does): pinned paths are
resolved one by one and the recents come from the stores.

Rows::

    {kind: "app",     path, url, name, title, icon|None, pinned, recent}
    {kind: "appfile", path, url, name, title, icon: None, pinned, recent}
    {kind: "bot",     id, name, face, status, running, updated, pinned}
"""
from __future__ import annotations

import logging
import os
import subprocess

from fused_render import _flavor

logger = logging.getLogger(__name__)

DOCK_FILE = "dock.json"
RECENT_CAP = 3
#: The tray's tile edge in px: the default, and the range a separator drag may set.
TILESIZE_DEFAULT = 52
TILESIZE_MIN = 16
TILESIZE_MAX = 128
#: Bot statuses a NEW Bot resets to idle (bots/bot.py: a status left over from
#: a process that died). A bot this process never loaded is shown that way too.
_STALE_STATUSES = ("running", "waiting", "paused")


def kind() -> str:
    """``"bots"`` under the bot flavor, else ``"apps"`` — what the tiles are."""
    return "bots" if _flavor.is_bot() else "apps"


# ------------------------------------------------------------------ store ---

def dock_path() -> str:
    from fused_render.shell import storage

    return os.path.join(storage.home_dir(), DOCK_FILE)


def _read() -> dict:
    from fused_render.shell import storage

    data = storage.read_json(dock_path())
    return data if isinstance(data, dict) else {}


def _write(key: str, value) -> None:
    """Set one key of dock.json, keeping the others."""
    from fused_render.shell import storage

    data = _read()
    data[key] = value
    storage.write_json(dock_path(), data)


def _stored_pins() -> list[str]:
    pins = _read().get("pinned")
    if not isinstance(pins, list):
        return []
    return [p for p in pins if isinstance(p, str) and os.path.isabs(p)]


def _norm(path: str) -> str:
    """The comparison key: the real path. The listings spell an app as the
    user linked or walked it; a symlinked workspace spells the same folder
    two ways, and a pin must match both — but the SPELLING kept is the
    listing's (see the module docstring)."""
    return os.path.realpath(path)


def pinned_paths() -> list[str]:
    """The pinned app paths (as stored, tray order) that still exist."""
    return [p for p in _stored_pins() if os.path.exists(p)]


def is_appfile(path: str) -> bool:
    return path.lower().endswith(".fused") and os.path.isfile(path)


def app_folder(path: str) -> str | None:
    """``path`` resolved to the APP FOLDER it belongs to (the folder itself,
    or the app owning a file inside it — `current_apps.app_dir_for`, the
    tasks' rule), else None. Never raises."""
    from fused_render import current_apps

    try:
        return current_apps.app_dir_for(path)
    except Exception:  # noqa: BLE001 — an unreadable folder is not an app here
        logger.debug("app_dir_for failed for %s", path, exc_info=True)
        return None


def resolve_pinnable(path) -> str:
    """``path`` as something the Dock can hold — an app folder with a
    declared entry page (in the listing's spelling, `app_dir_for`'s), or a
    `.fused` file (absolute) — else ValueError. A file inside an app pins
    the app."""
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise ValueError("path must be an absolute path")
    path = os.path.abspath(path)
    if is_appfile(path):
        return path
    folder = app_folder(path)
    if folder is None:
        raise ValueError(f"not an app: {path}")
    return folder


def set_pinned(path, pinned: bool) -> list[str]:
    """Pin (append) or unpin an app; returns `pinned_paths()`. Pinning needs
    an existing app; unpinning a path that is already gone is allowed (it
    only tidies dock.json)."""
    if pinned:
        target = resolve_pinnable(path)
    else:
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("path must be an absolute path")
        target = os.path.abspath(path)
        folder = app_folder(target) if not is_appfile(target) else None
        if folder is not None:
            target = folder
    key = _norm(target)
    pins = [p for p in _stored_pins() if _norm(p) != key]
    if pinned:
        pins.append(target)
    _write("pinned", pins)
    _invalidate_launcher()
    return pinned_paths()


def set_order(paths) -> list[str]:
    """Reorder the pinned apps after a drag in the tray: ``paths`` is the full
    left-to-right order (any spelling of each). Paths that are not pinned
    are ignored; a pinned path the list leaves out keeps its place relative
    to the others, after the listed ones. Returns `pinned_paths()`.
    ValueError when ``paths`` is not a list of strings."""
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("paths must be a list of app paths")
    pins = _stored_pins()
    by_key = {_norm(p): p for p in pins}  # any spelling of a pin → the stored one
    ordered: list[str] = []
    for p in paths:
        stored = by_key.get(_norm(p)) if os.path.isabs(p) else None
        if stored is not None and stored not in ordered:
            ordered.append(stored)
    ordered += [p for p in pins if p not in ordered]
    _write("pinned", ordered)
    return pinned_paths()


def _invalidate_launcher() -> None:
    # The launcher's registry caches its rows for a couple of seconds; a pin
    # does not change the rows, but keep the two surfaces honest anyway.
    try:
        from fused_render import launcher

        launcher.invalidate()
    except Exception:  # noqa: BLE001
        pass


def clamp_tilesize(value) -> int:
    """``value`` as a tile size in range; ValueError when it is not a number."""
    if isinstance(value, bool):
        raise ValueError("tilesize must be a number")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError("tilesize must be a number") from None
    if n != n or n in (float("inf"), float("-inf")):
        raise ValueError("tilesize must be a number")
    return int(min(max(round(n), TILESIZE_MIN), TILESIZE_MAX))


def tilesize() -> int:
    """The stored tile size (clamped), else the default."""
    try:
        return clamp_tilesize(_read().get("tilesize", TILESIZE_DEFAULT))
    except ValueError:
        return TILESIZE_DEFAULT


def set_tilesize(value) -> int:
    """Clamp, persist and return the tile size; ValueError for a non-number."""
    n = clamp_tilesize(value)
    _write("tilesize", n)
    return n


# ------------------------------------------------------------------- bots ---

def set_bot_pinned(bid: str, pinned: bool) -> bool:
    """Pin or unpin a bot: `pinned` in its bot.json, through the registry's
    Bot under its lock — exactly what the sidebar's flag route does
    (bots/routes._flag), so a loaded bot's live meta and the file agree.
    ValueError for an unknown bot. Returns the new value."""
    from fused_render.bots import registry

    b = registry.get(bid)
    with b.lock:
        b.meta["pinned"] = bool(pinned)
        b.save()
    return bool(pinned)


def bot_exists(bid) -> bool:
    """Whether ``bid`` names a bot on disk (no Bot is built). A plain id only:
    anything with a path separator or a dot-name is not one."""
    from fused_render.bots import store

    if not isinstance(bid, str) or not bid or bid != os.path.basename(bid) or bid in (".", ".."):
        return False
    return os.path.isfile(store.meta_path(bid))


def bot_view_path(bid: str) -> str:
    """The shell path that selects bot ``bid`` on the bots page."""
    import urllib.parse

    return "/bots?bot=" + urllib.parse.quote(bid, safe="")


def _bot_rows() -> list[dict]:
    from fused_render.bots import registry, store

    live = {}
    for b in registry.loaded():
        bid = getattr(b, "id", None)
        if bid:
            live[bid] = b
    rows = []
    for bid in store.list_ids():
        b = live.get(bid)
        if b is not None:
            meta, status = dict(b.meta), b.meta.get("status") or "idle"
        else:
            try:
                meta = store.read_meta(bid)
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            status = meta.get("status") or "idle"
            if status in _STALE_STATUSES:
                status = "idle"
        if meta.get("hidden"):
            continue
        face = meta.get("face") if isinstance(meta.get("face"), dict) else {}
        rows.append({"kind": "bot", "id": bid, "name": str(meta.get("name") or "Bot"),
                     "face": face, "status": str(status), "running": str(status) != "idle",
                     "updated": float(meta.get("updated") or 0), "pinned": bool(meta.get("pinned"))})
    return rows


def _bot_entries() -> dict:
    bots = _bot_rows()
    pinned = sorted((b for b in bots if b["pinned"]), key=lambda b: (b["name"].casefold(), b["id"]))
    recent = sorted((b for b in bots if not b["pinned"]), key=lambda b: -b["updated"])[:RECENT_CAP]
    return {"pinned": pinned, "recent": recent}


# ------------------------------------------------------------------- apps ---

def _pinned_app_row(path: str) -> dict | None:
    """One pinned path (as stored) as a tray row, or None when it is no
    longer an app."""
    from fused_render import app_listing, launcher

    if is_appfile(path):
        from fused_render import exported_apps

        try:
            row = exported_apps._row(path, os.path.getmtime(path), None)  # noqa: SLF001 — the listing's own shape
        except OSError:
            return None
        return launcher._appfile_row(row)  # noqa: SLF001
    try:
        entry = app_listing.app_entry(path)
    except OSError:
        return None
    if not entry:
        return None
    icon = None
    try:
        icon = app_listing.app_icon(path)
    except OSError:
        pass
    a = {"path": path, "name": os.path.basename(path), "title": app_listing.entry_title(entry),
         "icon": icon["icon"] if icon else None, "icon_mtime": icon["mtime"] if icon else None}
    return launcher._folder_row(a, pinned=True)  # noqa: SLF001


def _app_entries() -> dict:
    from fused_render import launcher

    pins = pinned_paths()
    pinned_rows = []
    for p in pins:
        row = _pinned_app_row(p)
        if row is not None:
            pinned_rows.append({**row, "pinned": True, "recent": False})
    pinned_set = {_norm(p) for p in pins}
    recent_rows = []
    try:
        candidates = launcher.recent_rows(RECENT_CAP + len(pins))
    except Exception:  # noqa: BLE001 — a store that cannot be read is zero recents
        logger.debug("recent rows failed", exc_info=True)
        candidates = []
    for row in candidates:
        if _norm(row["path"]) in pinned_set:
            continue
        recent_rows.append({**row, "pinned": False, "recent": True})
        if len(recent_rows) >= RECENT_CAP:
            break
    return {"pinned": pinned_rows, "recent": recent_rows}


# ---------------------------------------------------------------- entries ---

def entries() -> dict:
    """``{"kind": "apps"|"bots", "pinned": [...], "recent": [≤RECENT_CAP]}``
    (see the module docstring for the row shapes)."""
    out = _bot_entries() if _flavor.is_bot() else _app_entries()
    out["kind"] = kind()
    return out


# ------------------------------------------------------------------- open ---

def row_for_path(path) -> dict | None:
    """The app row the Dock would show for ``path`` (any spelling), or None
    when it is not an app this machine knows: used to validate an open or a
    reveal without trusting the wire. The row's `path` is the listing's
    spelling, which is what a window of the app is keyed on."""
    try:
        target = resolve_pinnable(path)
    except ValueError:
        return None
    return _pinned_app_row(target)


def _open_reveal(path: str) -> None:
    subprocess.Popen(["open", "-R", path])  # noqa: S603,S607 — fixed argv


def reveal(path) -> str:
    """Show an app in Finder (`open -R`); apps only (ValueError otherwise).
    Returns the path revealed."""
    target = resolve_pinnable(path)
    _open_reveal(target)
    return target

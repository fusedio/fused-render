"""Where the bots keep their state (docs/bots.md §1).

`<home>/bots/` is laid out exactly like the `.fused/` folder of the OpenBot
fused-render app this product grew out of:

    <home>/bots/data/bots/<id>/     bot.json, events.jsonl, memory.md, skills/, profile/, …
    <home>/bots/data/usage.jsonl    the usage ledger
    <home>/bots/data/builds.json    the Builds panel's list
    <home>/bots/data/imessage*.json the iMessage bridge's cursor, state and lock
    <home>/bots/cache/bots/<id>/    shot.png, session.json, steps/ (deletable)
    <home>/bots/cache/slow.jsonl    slow-call log

`<home>` is fused-render's `shell.storage.home_dir()` (`~/.fused-render`,
`FUSED_RENDER_HOME` overrides, branch-nested in a dev worktree). The mirror is
deliberate: moving from OpenBot is `rsync -a <OpenBot>/.fused/
~/.fused-render/bots/` and nothing else (owner's call, 2026-10-02). Only
the root moved, from beside OpenBot's code to under the app home. The two
user-visible folders stay where OpenBot put them: the Inbox under
`~/Fused/bots/` and the apps under `~/Fused/apps/` (`FUSED_RENDER_DIR`
overrides `~/Fused`, as everywhere in fused-render). The apps folder was
`~/Fused/app/` (singular) through 0.6.32; `migrate_apps_root()` renames it
once at startup and re-points every stored build path.

FusedBot 0.11.x shipped a flatter layout (`bots/data/<id>`, `bots/cache/<id>`,
the ledgers at `bots/`); `migrate_layout()` moves such a tree over once at
startup, before anything reads the bots (a 0.11.x tree copied under
`~/.fused-render/bots/` by hand is the only way one gets here now).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil

from fused_render.shell import storage as _storage


def root() -> str:
    """`<home>/bots` — made on first use."""
    p = os.path.join(_storage.home_dir(), "bots")
    os.makedirs(p, exist_ok=True)
    return p


def data_root() -> str:
    """`<home>/bots/data`: the bots folder plus the ledgers (OpenBot `.fused/data`)."""
    p = os.path.join(root(), "data")
    os.makedirs(p, exist_ok=True)
    return p


def cache_root() -> str:
    """`<home>/bots/cache`: the bots' caches plus slow.jsonl (OpenBot `.fused/cache`)."""
    p = os.path.join(root(), "cache")
    os.makedirs(p, exist_ok=True)
    return p


def data_dir() -> str:
    """`<home>/bots/data/bots/<id>/…`: bot.json, events.jsonl, memory.md, skills/, profile/, downloads/, files/, inbox/."""
    p = os.path.join(data_root(), "bots")
    os.makedirs(p, exist_ok=True)
    return p


def cache_dir() -> str:
    """`<home>/bots/cache/bots/<id>/…`: shot.png, session.json, steps/*.jpg, badjson/. Deletable any time."""
    p = os.path.join(cache_root(), "bots")
    os.makedirs(p, exist_ok=True)
    return p


def bot_dir(bid: str) -> str:
    return os.path.join(data_dir(), bid)


def bot_cache_dir(bid: str) -> str:
    return os.path.join(cache_dir(), bid)


def usage_path() -> str:
    """One line per model call (the usage ledger): `<home>/bots/data/usage.jsonl`."""
    return os.path.join(data_root(), "usage.jsonl")


def slow_log_path() -> str:
    """`<home>/bots/cache/slow.jsonl` (beside the bots' caches, not inside them)."""
    return os.path.join(cache_root(), "slow.jsonl")


def builds_path() -> str:
    """The Builds panel's list (`GET/POST /api/bots/builds`): `<home>/bots/data/builds.json`."""
    return os.path.join(data_root(), "builds.json")


def imessage_dir() -> str:
    """imessage.json (cursor), imessage.lock, imessage-state.json: `<home>/bots/data`."""
    return data_root()


# ----------------------------------------------------- 0.11.x layout move ---
_LEDGERS = ("usage.jsonl", "builds.json", "imessage.json", "imessage-state.json")
# Written under bots/ once a pass found nothing left to move: the sweep never runs again on that install, so a
# folder a later feature adds under data/ or cache/ (browsers/, #1483) can never be mistaken for a 0.11.x bot folder.
_LAYOUT_DONE = ".layout-v2"


def migrate_layout() -> int:
    """One-shot: move a 0.11.x home (`bots/data/<id>`, `bots/cache/<id>`,
    ledgers at `bots/`) to the OpenBot layout above. Returns how many entries
    moved. Idempotent; never overwrites (an occupied destination is logged and
    the source left alone); never raises. Runs at server startup (the
    `_startup_bots` hook in fused_render/server/app.py) BEFORE
    `registry.start()`. No bot runs yet at that point, so no Chrome holds a
    profile open."""
    log = logging.getLogger(__name__)
    moved = 0
    try:
        base = os.path.join(_storage.home_dir(), "bots")
        if not os.path.isdir(base) or os.path.exists(os.path.join(base, _LAYOUT_DONE)):
            return 0
        for sub in ("data", "cache"):
            old = os.path.join(base, sub)
            if not os.path.isdir(old):
                continue
            new = os.path.join(old, "bots")
            for name in sorted(os.listdir(old)):
                src = os.path.join(old, name)
                # `browsers` is a folder of the NEW layout (browsers.py: data/browsers/<id>/profile, cache/browsers/<id>/
                # session.json); moving it under cache/bots/ loses every shared browser's session.json, and the next
                # launch then collides with the Chrome still holding the profile ("Chrome did not come up").
                if name == "bots" or name.startswith(".") or not os.path.isdir(src):
                    continue
                # Only a bot's own folder is a 0.11.x leftover: under data/ it carries bot.json; under cache/ it is
                # named for a bot that exists (data/ moved first). `browsers` (#1483) and any folder a later feature
                # adds stay where they are.
                if sub == "data" and not os.path.exists(os.path.join(src, "bot.json")):
                    continue  # not a bot folder; leave it
                if sub == "cache" and not (os.path.isdir(os.path.join(base, "data", "bots", name))
                                           or os.path.isfile(os.path.join(base, "data", name, "bot.json"))):
                    continue  # not a bot's cache; leave it
                os.makedirs(new, exist_ok=True)
                dst = os.path.join(new, name)
                if os.path.exists(dst):
                    log.warning("bots layout: %s exists, leaving %s in place", dst, src)
                    continue
                shutil.move(src, dst)
                moved += 1
        for name in _LEDGERS:
            src, dst = os.path.join(base, name), os.path.join(base, "data", name)
            if not os.path.isfile(src):
                continue
            if os.path.exists(dst):
                log.warning("bots layout: %s exists, leaving %s in place", dst, src)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            moved += 1
        stale_lock = os.path.join(base, "imessage.lock")
        if os.path.isfile(stale_lock):
            os.remove(stale_lock)  # the lock holder recreates it at the new path
        if moved:
            log.info("bots layout: moved %d entries to the OpenBot layout under %s", moved, base)
        else:
            # Nothing left of the old layout: mark it so this never runs again (a pass that moved something runs
            # once more at the next boot, in case an occupied destination left a source behind).
            with open(os.path.join(base, _LAYOUT_DONE), "w") as f:
                f.write("1\n")
    except Exception:  # noqa: BLE001
        log.exception("bots layout: migration failed (continuing with what moved)")
    return moved


def workspace_dir() -> str:
    """fused-render's workspace root: `FUSED_RENDER_DIR`, else `~/Fused`
    (`fused_render.shell.seed.fused_dir`, the one resolution every other
    fused-render reader uses)."""
    from fused_render.shell.seed import fused_dir

    return fused_dir()


APPS_DIRNAME = "apps"
#: The pre-0.6.33 name. A literal, not a call: it is history, and must keep
#: naming the old folder even if the default moves again.
LEGACY_APPS_DIRNAME = "app"


def apps_root() -> str:
    """`<workspace>/apps`: where builds land and every bot's APPS live."""
    return os.path.join(workspace_dir(), APPS_DIRNAME)


def legacy_apps_root() -> str:
    """`<workspace>/app` — where the apps lived before `migrate_apps_root`."""
    return os.path.join(workspace_dir(), LEGACY_APPS_DIRNAME)


# ------------------------------------------------------ app/ -> apps/ move ---
# Written under bots/ once the stored paths are re-pointed: the move never
# runs again on that install.
_APPS_ROOT_DONE = ".apps-root-v2"


def _remap(path, old: str, new: str):
    """`path` re-rooted from `old` to `new` (directory-boundary guarded), or
    None when it is not a string under `old`. OS-native spellings only: every
    value here was written by this process with `os.path.join`."""
    if not isinstance(path, str):
        return None
    if path == old:
        return new
    if path.startswith(old + os.sep):
        return new + path[len(old):]
    return None


def migrate_apps_root() -> bool:
    """One-shot: `<workspace>/app` becomes `<workspace>/apps` (owner's call,
    2026-10-11), then every stored absolute path under it is re-pointed —
    each bot's `meta["builds"][].dir`, the Builds panel's `builds.json`, the
    menu-bar tray's `dock.json` pins, and fused-render's own stores
    (bookmarks, recents, scheduled targets, the pin, the desk, registered
    apps, app recents — `app_state_move.rewrite_stores`, the D548 radius).

    The rules are `workspace_migration`'s (D337): the folder is RENAMED, only
    into empty space — old present and new absent — and nothing is ever
    deleted or merged; both present is logged and left to the user, with no
    stamp, so the one stat retries each start. `FUSED_RENDER_DIR` is honoured
    the way every reader honours it: the override moves the root, not the
    subfolder's name. Re-entrant: the stamp is written only after the paths
    rewrote, so a process that died between the rename and the bookkeeping
    heals on the next start (old gone, stamp absent → rewrite again; every
    rewrite is an idempotent prefix remap). Returns True when the folder
    moved this call. Never raises. Runs from `_startup_bots` AFTER
    `fusedbot_import.import_once` (an imported FusedBot tree's ledgers name
    `~/Fused/app/...` too) and BEFORE `registry.start()` (bots load the
    rewritten bot.json; no build watcher holds a stale dir yet)."""
    log = logging.getLogger(__name__)
    moved = False
    try:
        stamp = os.path.join(root(), _APPS_ROOT_DONE)
        if os.path.exists(stamp):
            return False
        old, new = legacy_apps_root(), apps_root()
        if os.path.isdir(old) and not os.path.lexists(new):
            os.rename(old, new)
            moved = True
            log.info("bots apps root: moved %s -> %s", old, new)
        elif os.path.isdir(old):
            log.warning("bots apps root: both %s and %s exist; leaving both (merge by hand, "
                        "builds keep landing under %s)", old, new, new)
            return False
        # Here `old` is gone: moved just now, moved by a run that died before
        # stamping, or never there (a fresh install). Re-point, then stamp.
        _rewrite_apps_paths(old, new)
        with open(stamp, "w") as f:
            f.write("1\n")
    except Exception:  # noqa: BLE001
        log.exception("bots apps root: migration failed (continuing)")
    return moved


def _rewrite_apps_paths(old: str, new: str) -> None:
    """Every store that names a folder under the old apps root, re-pointed.
    Best-effort per store: one failing is logged and the next still runs."""
    log = logging.getLogger(__name__)
    from fused_render.bots import store as _store

    def bots_meta():
        n = 0
        for bid in _store.list_ids():
            try:
                meta = _store.read_meta(bid)
            except (OSError, ValueError):
                continue
            changed = False
            for bd in meta.get("builds") or []:
                if isinstance(bd, dict):
                    r = _remap(bd.get("dir"), old, new)
                    if r is not None:
                        bd["dir"], changed = r, True
            if changed:
                _store.write_meta(bid, meta)
                n += 1
        return n

    def builds_panel():
        builds = _store.builds_read()
        changed = False
        for bd in builds:
            r = _remap(bd.get("dir"), old, new)
            if r is not None:
                bd["dir"], changed = r, True
        if changed:
            _store.builds_write(builds)
        return int(changed)

    def dock_pins():
        from fused_render import dock
        from fused_render.shell import storage

        data = storage.read_json(dock.dock_path())
        if not isinstance(data, dict) or not isinstance(data.get("pinned"), list):
            return 0
        pinned = [(_remap(p, old, new) or p) for p in data["pinned"]]
        if pinned == data["pinned"]:
            return 0
        data["pinned"] = pinned
        storage.write_json(dock.dock_path(), data)
        return 1

    def render_stores():
        from fused_render import app_state_move

        app_state_move.rewrite_stores(old, new)
        return 1

    for label, fn in (("bot.json builds", bots_meta), ("builds.json", builds_panel),
                      ("dock.json pins", dock_pins), ("fused-render stores", render_stores)):
        try:
            n = fn()
            if n:
                log.info("bots apps root: re-pointed %s", label)
        except Exception:  # noqa: BLE001
            log.exception("bots apps root: could not re-point %s", label)


def artifacts_root() -> str:
    """`<workspace>/bots`: the Inbox, one folder per bot, one subfolder per task."""
    return os.path.join(workspace_dir(), "bots")


def slug(name: str | None) -> str:
    """OpenBot `_slug`: lower-case, `-` for runs of anything else, 40 chars, `app` when empty."""
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", (name or "").lower()))[:40] or "app"


def server_json_path() -> str:
    """`<home>/server.json`, the file `fused_render.server.app.write_server_json`
    writes at startup (same join as its `_server_json_path`, not imported: that
    module is the whole server)."""
    return os.path.join(_storage.home_dir(), "server.json")


def server_origin() -> str:
    """Where this server listens (`FUSED_RENDER_ORIGIN`, which
    `fused_render.server.app.set_server_origin_env` exports before serving),
    else what `server.json` says. Raises RuntimeError when neither is known."""
    o = os.environ.get("FUSED_RENDER_ORIGIN")
    if o:
        return o.rstrip("/")
    try:
        with open(server_json_path(), encoding="utf-8") as f:
            o = (json.load(f).get("origin") or "").rstrip("/")
    except (OSError, ValueError):
        o = ""
    if not o:
        raise RuntimeError("the fused-render server is not running (no origin known)")
    return o


def server_origin_quiet() -> str:
    try:
        return server_origin()
    except RuntimeError:
        return ""

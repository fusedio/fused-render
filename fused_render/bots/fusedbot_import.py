"""One-shot import of a FusedBot install's bots into fused-render (docs/bots.md).

FusedBot (the fused-bot repo, `FusedBot.app`) keeps its bots under
`~/.fused-render-app/bots/` (`FUSED_RENDER_APP_HOME` overrides); fused-render's
Bots sub-app keeps the same tree, byte-for-byte the same layout, under
`<home>/bots/` (`fused_render.bots.paths`). Both share the Inbox
(`~/Fused/bots/`), the apps root (`~/Fused/app/`) and the Keychain item that
unlocks an encrypted profile (`browser.KEY_SERVICE`/`KEY_ACCOUNT` are the same
string in both trees), so moving a user over is a rooted copy and nothing else.

`import_once()` does that copy the first time fused-render's bots start on a
machine that has a FusedBot tree (owner's ask, 2026-10-06):

* **Copy, never move, never overwrite.** FusedBot may still be installed and
  running; its tree is left untouched. A bot id, ledger or cache that already
  exists on our side stays as it is (the rule `paths.migrate_layout` follows).
* **Stamp, not emptiness.** `<home>/bots/imported-from-fusedbot.json` records
  what was copied; its presence is the "done" witness. An empty-destination
  test would never fire on a machine that already made one fused-render bot.
  No FusedBot tree -> no stamp, so a FusedBot installed later still imports
  once. Dev branches nest `home_dir()` per branch ref, so each imports on its
  own first start; the baseline DMG imports once.
* **Only `data/` and `cache/`.** The FusedBot root also carries `dock.json`
  (menu-bar dock, not ported) and, on a tree that `migrate_layout` upgraded
  from 0.11.x, stale ledgers beside `data/` that lost to the ones inside it.
  Ledgers are taken from `data/<name>`, falling back to the root copy only
  when `data/` has none (a never-upgraded 0.11.x tree). Bot folders are
  recognised the way `migrate_layout` does, by a `bot.json`, under
  `data/bots/<id>` or flat `data/<id>`.
* **Chrome's runtime files are skipped.** `profile/SingletonSocket` is a Unix
  socket (copytree raises on it), `SingletonLock`/`SingletonCookie` are
  symlinks naming the Chrome that held the profile, `RunningChromeVersion`
  likewise; `*.lock`/`*.tmp` are ours. A profile whose SingletonLock names a
  live pid is copied anyway (skipping it would silently drop that bot's
  logins) and listed under `hot` in the stamp and the log: Chrome may have
  had a write in flight, worst case that bot signs in again.
* A bot whose copy fails is removed half-copied and recorded under `failed`;
  the stamp is written only when nothing failed, so the next start retries
  just those (everything else is skip-if-exists).

Runs from `_startup_bots` in fused_render/server/app.py, in the worker thread
that runs `paths.migrate_layout()` (so the destination is in today's layout
first) and before `registry.start()` (so the scheduler's first pass sees the
imported bots). Never raises.
"""
from __future__ import annotations

import logging
import os
import shutil
import time

from fused_render.bots import paths as _bpaths
from fused_render.bots.store import write_json_atomic

log = logging.getLogger(__name__)

STAMP = "imported-from-fusedbot.json"
LEDGERS = ("usage.jsonl", "builds.json", "imessage.json", "imessage-state.json")
_IGNORE = shutil.ignore_patterns("Singleton*", "RunningChromeVersion", "*.lock", "*.tmp")


def fusedbot_home() -> str:
    """`FUSED_RENDER_APP_HOME`, else `~/.fused-render-app` (fused_render_app.paths.home)."""
    return os.environ.get("FUSED_RENDER_APP_HOME") or os.path.expanduser("~/.fused-render-app")


def source_root() -> str | None:
    """FusedBot's `bots/` tree when there is one and it is not ours."""
    src = os.path.join(fusedbot_home(), "bots")
    if not os.path.isdir(src):
        return None
    if os.path.realpath(src) == os.path.realpath(_bpaths.root()):
        return None  # FUSED_RENDER_APP_HOME pointed at our own home
    return src


def stamp_path() -> str:
    return os.path.join(_bpaths.root(), STAMP)


def _subdirs(base: str, marker: str | None) -> dict[str, str]:
    """`{id: path}` for every folder under `base/bots/`, then flat under
    `base/` (the 0.11.x layout). With `marker`, only folders holding that
    file count (bot folders by their bot.json); caches have no marker."""
    out: dict[str, str] = {}
    for parent in (os.path.join(base, "bots"), base):
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for name in sorted(names):
            p = os.path.join(parent, name)
            if name.startswith(".") or not os.path.isdir(p) or name in out:
                continue
            if parent == base and name == "bots":
                continue
            if marker and not os.path.exists(os.path.join(p, marker)):
                continue
            out[name] = p
    return out


def _hot_pid(bot_dir: str) -> int | None:
    """The pid of a Chrome that holds this profile, if SingletonLock names a live one."""
    try:
        target = os.readlink(os.path.join(bot_dir, "profile", "SingletonLock"))
        pid = int(target.rsplit("-", 1)[1])
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, IndexError):
        return None


def _copy_tree(sdir: str, ddir: str) -> bool:
    try:
        shutil.copytree(sdir, ddir, symlinks=True, ignore=_IGNORE, ignore_dangling_symlinks=True)
        return True
    except Exception:  # noqa: BLE001
        log.exception("fusedbot import: %s not copied", sdir)
        shutil.rmtree(ddir, ignore_errors=True)
        return False


def import_once() -> dict | None:
    """Copy a FusedBot tree in, once. Returns the stamp's content (what moved),
    or None when there was nothing to do."""
    try:
        return _import_once()
    except Exception:  # noqa: BLE001 - a bots import must not block serving
        log.exception("fusedbot import: failed (continuing without it)")
        return None


def _import_once() -> dict | None:
    if os.path.exists(stamp_path()):
        return None
    src = source_root()
    if not src:
        return None
    dst = _bpaths.root()
    report: dict = {
        "source": src,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "bots": [], "skipped": [], "hot": [], "failed": [],
        "cache": [], "ledgers": [],
    }

    # Bots: data/bots/<id> (or flat data/<id>) -> data/bots/<id>.
    dst_bots = _bpaths.data_dir()
    for bid, sdir in _subdirs(os.path.join(src, "data"), "bot.json").items():
        ddir = os.path.join(dst_bots, bid)
        if os.path.exists(ddir):
            report["skipped"].append(bid)
            continue
        pid = _hot_pid(sdir)
        if pid:
            report["hot"].append(bid)
            log.warning("fusedbot import: bot %s's Chrome is running (pid %d); copying its profile anyway", bid, pid)
        report["bots" if _copy_tree(sdir, ddir) else "failed"].append(bid)

    # Caches: deletable, but the last screenshot and session are worth having.
    dst_cache = _bpaths.cache_dir()
    for bid, sdir in _subdirs(os.path.join(src, "cache"), None).items():
        ddir = os.path.join(dst_cache, bid)
        if not os.path.exists(ddir) and _copy_tree(sdir, ddir):
            report["cache"].append(bid)

    # Ledgers: data/<name>, else the pre-0.12 root copy; never over ours.
    for name in LEDGERS:
        sfile = os.path.join(src, "data", name)
        if not os.path.isfile(sfile):
            sfile = os.path.join(src, name)
        dfile = os.path.join(_bpaths.data_root(), name)
        if os.path.isfile(sfile) and not os.path.exists(dfile):
            shutil.copy2(sfile, dfile)
            report["ledgers"].append(name)
    sfile = os.path.join(src, "cache", "slow.jsonl")
    if os.path.isfile(sfile) and not os.path.exists(_bpaths.slow_log_path()):
        shutil.copy2(sfile, _bpaths.slow_log_path())
        report["ledgers"].append("slow.jsonl")

    log.info("fusedbot import: %d bots copied, %d already here, %d hot, %d failed, %d ledgers, from %s to %s",
             len(report["bots"]), len(report["skipped"]), len(report["hot"]), len(report["failed"]),
             len(report["ledgers"]), src, dst)
    if report["failed"]:
        return report  # no stamp: the next start retries the failed ones
    write_json_atomic(stamp_path(), report)
    return report

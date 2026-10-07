"""Browsers: the Chrome profiles bots run on, one `BrowserProcess` each (docs/bots.md §1, §5).

A browser is a folder, `<home>/bots/data/browsers/<bid>/` (profile/, profile.enc,
browser.json: id, name, encrypt, created, chrome_profile), plus a cache folder
`<home>/bots/cache/browsers/<bid>/` (session.json, Chrome's live handle). A bot
names its browser in bot.json (`browser_id`); several bots may name the same one
and then share its logins: one Chrome process, a window per bot. A bot made
without one gets a browser of its own, with the bot's id.

Bots from before browsers existed kept their profile under the bot folder
(`bots/<id>/profile`); `adopt()` moves it into `browsers/<id>/` the first time
that bot loads, so nothing is copied and an upgrade changes no login.

The process objects are kept here (one per folder, like the bot registry) so
two bots sharing a browser share the lock and the Chrome handle.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time

from fused_render.bots import paths as bpaths
from fused_render.bots.browser import BrowserProcess, write_json_atomic

log = logging.getLogger(__name__)

_lock = threading.Lock()
_procs: dict = {}  # browser dir -> BrowserProcess


def data_dir() -> str:
    p = os.path.join(bpaths.data_root(), "browsers")
    os.makedirs(p, exist_ok=True)
    return p


def cache_dir() -> str:
    p = os.path.join(bpaths.cache_root(), "browsers")
    os.makedirs(p, exist_ok=True)
    return p


def browser_dir(bid: str) -> str:
    return os.path.join(data_dir(), bid)


def browser_cache_dir(bid: str) -> str:
    return os.path.join(cache_dir(), bid)


def meta_path(bid: str) -> str:
    return os.path.join(browser_dir(bid), "browser.json")


def read_meta(bid: str) -> dict:
    try:
        with open(meta_path(bid), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_meta(bid: str, meta: dict) -> None:
    os.makedirs(browser_dir(bid), exist_ok=True)
    write_json_atomic(meta_path(bid), meta)


def exists(bid: str) -> bool:
    return bool(bid) and os.path.isfile(meta_path(bid))


def ensure(bid: str, name: str = "", encrypt: bool = False) -> dict:
    """The browser's browser.json, written on first use."""
    m = read_meta(bid)
    if not m:
        m = {"id": bid, "name": name or "", "encrypt": bool(encrypt), "created": time.time()}
        write_meta(bid, m)
    return m


def get(bid: str) -> BrowserProcess:
    """The process for browser `bid` (built on first ask, then kept)."""
    bid = os.path.basename(bid or "")
    if not bid:
        raise ValueError("no browser id")
    key = browser_dir(bid)
    with _lock:
        p = _procs.get(key)
        if p is None:
            p = BrowserProcess(key, browser_cache_dir(bid))
            p.encrypt = bool(read_meta(bid).get("encrypt"))
            _procs[key] = p
        return p


def forget(bid: str) -> None:
    with _lock:
        _procs.pop(browser_dir(bid), None)


def set_encrypt(bid: str, on: bool) -> None:
    m = ensure(bid)
    m["encrypt"] = bool(on)
    write_meta(bid, m)
    get(bid).encrypt = bool(on)


def set_field(bid: str, key: str, value) -> None:
    m = ensure(bid)
    m[key] = value
    write_meta(bid, m)


def remove(bid: str) -> None:
    """Delete a browser's folders (its logins). The caller stops Chrome first."""
    forget(bid)
    shutil.rmtree(browser_dir(bid), ignore_errors=True)
    shutil.rmtree(browser_cache_dir(bid), ignore_errors=True)


# ------------------------------------------------------------------ adopt ---
_MOVE = ("profile", "profile.enc")


def adopt(bot_id: str, bot_dir: str, bot_cache_dir: str, meta: dict) -> str:
    """A bot without `browser_id`: give it a browser of its own (same id) and
    move its old per-bot profile there. A Chrome still holding the old profile
    (left from before the upgrade) is stopped first. Returns the browser id."""
    bid = bot_id
    old_profile = os.path.join(bot_dir, "profile")
    old_sealed = os.path.join(bot_dir, "profile.enc")
    if os.path.exists(old_profile) or os.path.exists(old_sealed):
        old = BrowserProcess(bot_dir, bot_cache_dir)
        try:
            if old.alive():
                old.stop(seal=False)
        except Exception:  # noqa: BLE001
            log.warning("browser %s: could not stop the pre-upgrade Chrome", bid, exc_info=True)
        dst = browser_dir(bid)
        os.makedirs(dst, exist_ok=True)
        for name in _MOVE:
            src, to = os.path.join(bot_dir, name), os.path.join(dst, name)
            if os.path.exists(src) and not os.path.exists(to):
                try:
                    shutil.move(src, to)
                except OSError:
                    log.warning("browser %s: could not move %s", bid, src, exc_info=True)
        try:
            os.remove(os.path.join(bot_cache_dir, "session.json"))
        except OSError:
            pass
    ensure(bid, name=meta.get("name") or "", encrypt=bool(meta.get("encrypt")))
    if meta.get("chrome_profile"):
        set_field(bid, "chrome_profile", meta["chrome_profile"])
    return bid


# ------------------------------------------------------------------ listing ---
def users(bid: str, bots) -> list:
    """The bots (objects) whose browser is `bid`."""
    return [b for b in bots if (b.meta.get("browser_id") or b.id) == bid]


def _metas_on_disk() -> list[dict] | None:
    """Every bot.json as written (no Bot built), or None when one is unreadable:
    a decision about deleting logins must not be made on a partial list."""
    from fused_render.bots import store
    out = []
    for bid in store.list_ids():
        try:
            m = store.read_meta(bid)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(m, dict):
            return None
        out.append({**m, "id": m.get("id") or bid})
    return out


def used_on_disk(bid: str, except_id: str = "") -> bool | None:
    """True when a bot other than `except_id` names browser `bid` in its
    bot.json (loaded or not, this app or the other one sharing the home),
    None when a bot.json could not be read."""
    metas = _metas_on_disk()
    if metas is None:
        return None
    return any(((m.get("browser_id") or m["id"]) == bid) and m["id"] != except_id for m in metas)


def listing(bots) -> list[dict]:
    """[{id, name, encrypt, running, bots: [{id, name}]}] for every browser a bot
    uses (orphan folders are left out; `sweep` removes them)."""
    out = {}
    for b in bots:
        bid = b.meta.get("browser_id") or b.id
        row = out.get(bid)
        if row is None:
            m = read_meta(bid)
            p = get(bid)
            row = out[bid] = {"id": bid, "name": m.get("name") or "", "encrypt": bool(m.get("encrypt")),
                              "chrome_profile": m.get("chrome_profile") or "",
                              "running": bool((p.session() or {}).get("pid")), "bots": []}
        row["bots"].append({"id": b.id, "name": b.meta.get("name") or ""})
    return list(out.values())


def sweep(bots) -> int:
    """Remove browser folders no bot names (a bot deleted while its Chrome was
    busy, or a crash between detach and remove). Returns how many went."""
    metas = _metas_on_disk()
    if metas is None:
        log.warning("browser sweep skipped: a bot.json is unreadable")
        return 0
    used = {(m.get("browser_id") or m["id"]) for m in metas} | {b.id for b in bots}
    gone = 0
    try:
        names = os.listdir(data_dir())
    except OSError:
        return 0
    for n in names:
        if n in used or n.startswith("."):
            continue
        p = _procs.get(browser_dir(n)) or BrowserProcess(browser_dir(n), browser_cache_dir(n))
        if p.views or p.alive():  # a Chrome still up on it (the other app's, say) is not an orphan
            continue
        remove(n)
        gone += 1
    return gone

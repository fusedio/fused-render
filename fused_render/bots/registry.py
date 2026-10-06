"""The bot registry and the bots' background threads (docs/bots.md §1, §7).

One process, one registry: `get(bid)` builds a `Bot` the first time it is
asked for and keeps it (its task thread, seq counter and inbox live on it).
`start()` (from the server's `@on_startup _startup_bots`) runs the scheduler — every 20 s each bot's
`tick_routines()`, which also drains its file inbox (botsend / iMessage) — and
the iMessage bridge thread. `shutdown()` (from `@on_shutdown_always
_shutdown_bots`) stops every bot's task and Chrome. Both hooks live in
fused_render/server/app.py beside the router include.

`bot.py` is imported lazily so importing this module (and `routes.py`, and so
the server) never pulls in Chrome/CDP code until a bot is actually touched.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from fused_render.bots import paths as bpaths

logger = logging.getLogger(__name__)

SCHED_EVERY_S = 20
SLOW_CALL_MS = 1500

_lock = threading.Lock()
_bots: dict = {}           # bot dir -> Bot (keyed by dir so a redirected home never serves a stale Bot)
_sched = {"thread": None, "stop": None}
_imsg = {"bridge": None, "stop": None}


def _botmod():
    from fused_render.bots import bot
    return bot


def _key(bid: str) -> str:
    return bpaths.bot_dir(bid)


def get(bid: str):
    """The Bot for `bid`; ValueError when there is no such bot."""
    bid = os.path.basename(bid or "")
    if not bid:
        raise ValueError("no bot id given")
    key = _key(bid)
    with _lock:
        b = _bots.get(key)
        if b is None:
            if not os.path.isfile(os.path.join(key, "bot.json")):
                raise ValueError(f"no such bot {bid}")
            b = _botmod().Bot(bid)
            _bots[key] = b
        return b


def forget(bid: str) -> None:
    with _lock:
        _bots.pop(_key(bid), None)


def ids() -> list[str]:
    from fused_render.bots import store
    return store.list_ids()


def all():  # noqa: A001 — the registry's own spelling (OpenBot `_all`)
    out = []
    for bid in ids():
        try:
            out.append(get(bid))
        except Exception:  # noqa: BLE001 — a torn bot.json must not hide every other bot
            logger.warning("bot %s could not be loaded", bid, exc_info=True)
    return out


def loaded() -> list:
    """Bots built so far (no disk scan)."""
    with _lock:
        return list(_bots.values())


def create(name="", model="", effort="", instructions="", preset="", kind=""):
    return _botmod().create(name, model, effort, instructions, preset=preset, kind=kind)


def clone(src_id, name=""):
    return _botmod().clone(src_id, name)


def delete(bid):
    _botmod().delete(bid)


# ------------------------------------------------------------- threads ---
def _scheduler(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            for b in all():
                try:
                    b.tick_routines()
                except Exception:  # noqa: BLE001
                    logger.debug("routine tick failed for %s", getattr(b, "id", "?"), exc_info=True)
        except Exception:  # noqa: BLE001
            logger.debug("scheduler pass failed", exc_info=True)
        stop.wait(SCHED_EVERY_S)


def start() -> None:
    """Start the scheduler and the iMessage bridge (idempotent)."""
    with _lock:
        t = _sched["thread"]
        if t is None or not t.is_alive():
            stop = threading.Event()
            t = threading.Thread(target=_scheduler, args=(stop,), daemon=True, name="bots-routines")
            _sched.update(thread=t, stop=stop)
            t.start()
        need_bridge = _imsg["bridge"] is None
    if need_bridge:  # outside the lock: the bridge may look bots up as it starts
        try:
            from fused_render.bots import imessage
            br, stop = imessage.start_thread()
            with _lock:
                if _imsg["bridge"] is None:
                    _imsg.update(bridge=br, stop=stop)
                else:
                    stop.set()  # a concurrent start() won
        except Exception:  # noqa: BLE001 — no iMessage bridge is a missing feature, not a broken server
            logger.warning("iMessage bridge not started", exc_info=True)


def bridge():
    """The iMessage bridge object (its `.state`), or None when not started."""
    return _imsg["bridge"]


def imessage_state():
    br = _imsg["bridge"]
    if br is None:
        return None
    try:
        from fused_render.bots import imessage
        return imessage.current_state(br.state)
    except Exception:  # noqa: BLE001
        return None


def shutdown() -> None:
    """Stop the threads, then every loaded bot's task and Chrome."""
    with _lock:
        for slot in (_sched, _imsg):
            if slot.get("stop") is not None:
                slot["stop"].set()
        _sched.update(thread=None, stop=None)
        _imsg.update(bridge=None, stop=None)
    for b in loaded():
        try:
            b.shutdown()
        except Exception:  # noqa: BLE001
            logger.warning("bot %s did not shut down cleanly", getattr(b, "id", "?"), exc_info=True)


def reset_for_tests() -> None:
    """Forget every Bot and thread handle (tests; never in the app)."""
    with _lock:
        for slot in (_sched, _imsg):
            if slot.get("stop") is not None:
                slot["stop"].set()
        _sched.update(thread=None, stop=None)
        _imsg.update(bridge=None, stop=None)
        _bots.clear()


# ------------------------------------------------------------ slow log ---
def slow_log(action: str, ms: int, id: str = "", fast: bool = False) -> None:  # noqa: A002
    """A call that held the page ≥ SLOW_CALL_MS: one line in cache/slow.jsonl,
    so a UI that looked frozen can be traced to the call that held it."""
    if ms < SLOW_CALL_MS:
        return
    try:
        with open(bpaths.slow_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time() - ms / 1000, 3), "ms": int(ms), "action": action or "status",
                                "id": id or "", "fast": bool(fast), "pid": os.getpid()}) + "\n")
    except OSError:
        pass

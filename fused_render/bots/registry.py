"""The bot registry and the bots' background threads (docs/bots.md §1, §7).

One MACHINE, one owner (2026-10-13, the owner-process design): the installed
app and a dev checkout on main (and, before it was retired, the separate Fused
Bot app) all share `<home>/bots`, and before this
each of them built its own `Bot` per bot, ran engines on it and wrote bot.json
from its own memory — a routine added in one app never fired (the other app
owned the scheduler and never re-read the file), two engines could land on one
Chrome, and the losing app showed a card that said "next 10:00" while nothing
ticked. Now the flock on `<home>/bots/data/routines.lock` (its old name, so an
installed older app still contends for the same file) elects the ONE process
that holds every Bot, every engine, every Chrome, the scheduler and the
channels. Every other process forwards `/api/bots/*` and `/api/bot-apps/*` to
it over local HTTP (`routes.forward_middleware`) and never constructs a Bot:
`get()` / `all()` / `create()` refuse with `NotOwner`. The winner writes
`{pid, origin, app, version, boot_id, ticked_at}` into the lock file so a
forwarder knows where to send and the page can say who runs the bots
(`owner_info()`, `/api/bots/setup`). A lean `fused-render open` never elects
(`set_lean`): with an owner it forwards, without one it answers 503.

Within the owner: `get(bid)` builds a `Bot` the first time it is asked for and
keeps it (its task thread, seq counter and inbox live on it). `start()` (from
the server's `@on_startup _startup_bots`) starts the scheduler thread, whose
every pass first makes sure this process owns (re-trying the flock while it
does not), then runs `handoffs.sweep` (docs §11) and every bot's
`tick_routines()` (routines plus the file inbox) every 20 s. Becoming owner
(`_become_owner`) runs what used to run at every start: the layout migration,
the FusedBot import, the v3 folder migration, the Super Bot seed, the orphan-browser sweep, the
channels router, plus the takeover walk (`_reconcile`: a bot whose bot.json
still says running from an owner that quit goes idle, a routine stuck on
"started" gets its outcome) and the routines wake agent (`sync_wake`, the
LaunchAgent `schedule_wake` keeps for Tasks: daily and once routines relaunch
a quit app; interval ones wait for the next launch, owner's call Q1).
One WORKSPACE, one ticker. The owner lock above is per home (machine state —
browsers, channels, forwarding — stays per home), but two homes can share one
workspace: the main app and a `FUSED_RENDER_BRANCH` install both default to
`~/Fused/bots`, each wins its own home's `routines.lock`, and both would tick
the same bots, firing a due routine twice. So an owner also flocks
`<bots_root>/.routines.lock` (the workspace claim, beside `.migrate.lock`) and
keeps it for its lifetime. Without the claim it still serves its home (reads,
forwarding, the dock, the export) but runs NO bot engine and is READ-ONLY for
bot state: every door that starts an engine (a send, an app send, a phone
message, a channel text — the router texts back —, a routine run, a new bot's
greeting, a hand-off start) and every POST that writes a bot's files (settings,
routines, skills, flags, reactions, the read mark, attachments, the browser
drive, take-over, delete, clone, install) refuses through `require_workspace()`
with `WorkspaceBusy` (a 409 on the routes), since the claimant may be running
that very bot's engine on the same bot folder and its next save would revert the
edit. A Bot built here then loads as-is (`Bot.read_only`: no load-time reconcile,
no event, nothing saved) and `get()` rebuilds it from disk once the claim is won. It also SKIPS `tick_routines()`, the hand-off sweep (its
rows live in the Super Bot's bot.json in the workspace: two sweepers would
start one queued hand-off twice) and the steps that read or write the shared
workspace (the v3 folder migration, the Super Bot seed, the takeover walk — it
would stop the claimant's running tasks, which have no thread here — and the
browser sweep), logs "another fused-render owns routines for <bots_root>" once,
and re-tries the claim every pass, so a quitting main app hands routines over.
The claim drops with the owner lock (`_drop_routines_lock`).

The claim can be MOVED without quitting (2026-11-06): several dev servers on one
workspace (each its own `FUSED_RENDER_BRANCH` home) used to leave every window
but the first unable to test bots. The claimant writes the same record as the
owner lock into `.routines.lock` (`_write_ws_record`), so a refused process can
name it (`workspace_info()`, the 409's `holder`) and ask it to let go:
`POST /api/bots/workspace/take` (routes.py) posts `/api/bots/workspace/yield`
to the holder's origin, which stops its engines and Chromes, drops the claim
(`yield_workspace`), forgets its Bots (they rebuild read-only) and skips
re-claiming for `WS_YIELD_COOLDOWN_S`, so the asker's claim (`take_workspace`)
lands before the holder's next pass. The page's Override button is that call.

`shutdown()` (from `@on_shutdown_always _shutdown_bots`) stops every bot's
task and Chrome and drops the lock LAST, so a successor never builds Bots
while this process's engines still write. Both hooks live in
fused_render/server/app.py beside the router include.

`bot.py` is imported lazily so importing this module (and `routes.py`, and so
the server) never pulls in Chrome/CDP code until a bot is actually touched.
(One exception: the onboarding meter imports `browser.py` for its
CHROME_CANDIDATES walk on `/api/config` — a module load, no Chrome spawned.)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from fused_render import _platform
from fused_render.bots import paths as bpaths, store

logger = logging.getLogger(__name__)

SCHED_EVERY_S = 20
SLOW_CALL_MS = 1500
STALL_S = 90               # an owner whose `ticked_at` is older than this while its pid lives is "stalled" (page banner only)
OLD_OWNER_WAIT_S = 10.0    # after winning the flock: wait this long for the previous owner's pid to be gone (an older
                           # app dropped the lock BEFORE stopping its engines; see shutdown())

_lock = threading.Lock()
_bots: dict = {}           # bot id -> Bot (a redirected home/workspace clears it: shutdown/reset)
_sched = {"thread": None, "stop": None}
_chan = {"router": None}   # the channels router (bots/channels/router.py); None until start()
# Ownership (module docstring). `fh`: the flock'd lock file while this process owns; `lean`: never elect;
# `ready`: _become_owner ran (seed, import, sweep, router, reconcile) — get() in a test process that never
# called start() elects lazily and skips that, so tests see the registry they always did.
# `ws_fh`: the flock'd `<bots_root>/.routines.lock` (the workspace claim, module docstring); `ws_ready`: the
# shared-workspace steps ran under it; `ws_logged`: the "another process owns routines" line went out.
_OWNER = {"fh": None, "lean": False, "ready": False, "elect_lock": threading.Lock(),
          "ws_fh": None, "ws_ready": False, "ws_logged": False,
          # `ws_yield_until`: after yield_workspace(), no claim attempt until this monotonic time (the taker's claim
          # must land before this process's next pass). `ws_lock`: the workspace steps run under it, whether from
          # the pass or from take_workspace() on a request thread, so they never run twice at once (an RLock:
          # take_workspace holds it while _workspace_steps takes it again).
          "ws_yield_until": 0.0, "ws_lock": threading.RLock()}
WS_LOCK = ".routines.lock"
WS_YIELD_COOLDOWN_S = 30.0
WS_YIELD_BUDGET_S = 8.0


class NotOwner(RuntimeError):
    """This process does not own the bots (another Fused app does, or none does).
    `info` is the owner's lock-file record when one is readable, else None."""

    def __init__(self, info: dict | None):
        self.info = info
        who = _owner_words(info)
        super().__init__(f"the bots run in {who}" if info else "no Fused app is running the bots")


class WorkspaceBusy(ValueError):
    """This process owns its home but not the workspace claim (module docstring): another Fused Render instance
    runs the bots in this workspace, so nothing here may start a bot engine. A 409 on the routes."""

    def __init__(self, root: str = "", holder: dict | None = None):
        self.root = root
        self.holder = holder
        who = _owner_words(holder) if holder and holder.get("known") else "another Fused Render instance"
        where = f" at {holder['origin']}" if holder and holder.get("origin") else ""
        super().__init__(f"{who}{where} owns the bots in this workspace ({root}); "
                         "use that window, quit it, or take them over here")


def _owner_words(info: dict | None) -> str:
    if not info:
        return "another Fused app"
    app = info.get("app") or "another Fused app"
    ver = info.get("version") or ""
    return f"{app} {ver}".strip()


def _botmod():
    from fused_render.bots import bot
    return bot


def _key(bid: str) -> str:
    # By id, not by folder: resolving the folder (paths.bot_folder) raises for an unknown bid, and the id is
    # the one name a bot keeps for life (its folder is fixed at creation, but the id is what callers hold).
    return bid


def get(bid: str):
    """The Bot for `bid`; ValueError when there is no such bot; NotOwner when this
    process may not hold Bots (the forward middleware answers that before any
    route reaches here, so a NotOwner out of a route is a bug, not a state)."""
    bid = os.path.basename(bid or "")
    # `.`/`..` survive basename and would make
    # the bots root's parent a "bot" whenever a bot.json sits there (CodeRabbit,
    # #1637). The page's route parser refuses them too (apps/bots/routes.ts
    # isBotId); this is the guard every API caller passes through.
    if not bid or bid in (".", ".."):
        raise ValueError("no bot id given")
    require_owner()
    key = _key(bid)
    with _lock:
        b = _bots.get(key)
        if b is not None and getattr(b, "read_only", False) and not workspace_read_only():
            # Built while another home held the workspace claim (Bot.read_only): its meta is the snapshot it loaded
            # and it never ran the load-time reconcile. Now that this process may write, rebuild it from disk, or its
            # first save would put that stale snapshot over everything the claimant wrote since. It holds no thread
            # (every engine door refused); its view leaves the shared Chrome process, the Chrome stays up.
            try:
                b.browser.proc.detach(b.browser)
            except Exception:  # noqa: BLE001
                pass
            _bots.pop(key, None)
            b = None
        if b is None:
            try:
                exists = os.path.isfile(store.meta_path(bid))
            except LookupError:
                exists = False  # no folder carries this id
            if not exists:
                raise ValueError(f"no such bot {bid}")
            b = _botmod().Bot(bid)
            _bots[key] = b
        return b


def forget(bid: str) -> None:
    with _lock:
        _bots.pop(_key(bid), None)


def ids() -> list[str]:
    return store.list_ids()


def all():  # noqa: A001 — the registry's own spelling (OpenBot `_all`)
    from fused_render.bots import browsers
    require_owner()
    out = []
    try:
        bids = store.list_ids(strict=True)
        failed = False
    except OSError:  # the data dir could not be listed: an empty scan is not a complete one
        bids, failed = [], True
    for bid in bids:
        try:
            out.append(get(bid))
        except Exception:  # noqa: BLE001 — a torn bot.json must not hide every other bot
            failed = True
            logger.warning("bot %s could not be loaded", bid, exc_info=True)
    # Every view attached: a window nobody owns is an orphan from here on. A scan that missed a bot says so.
    browsers.mark_complete(not failed)
    return out


def loaded() -> list:
    """Bots built so far (no disk scan)."""
    with _lock:
        return list(_bots.values())


def create(name="", model="", effort="", instructions="", preset="", kind="", browser=""):
    require_owner()
    b = _botmod().create(name, model, effort, instructions, preset=preset, kind=kind, browser=browser)
    sync_wake()
    return b


def clone(src_id, name="", share=True):
    require_owner()
    b = _botmod().clone(src_id, name, share=share)
    sync_wake()
    return b


def delete(bid):
    require_owner()
    _botmod().delete(bid)
    sync_wake()


# ------------------------------------------------------------ ownership ---
def set_lean(lean: bool) -> None:
    """A lean `fused-render open` (server/app.py create_app) never elects: it
    forwards when an owner exists and answers 503 when none does."""
    _OWNER["lean"] = bool(lean)


def owns() -> bool:
    return _OWNER["fh"] is not None


def require_owner() -> None:
    """Elect lazily when nothing forbids it, then insist. Lazy: a process that
    never ran start() (every test; a route hit before the startup hook) still
    gets a registry on the first touch, exactly as before; a lean server and a
    process another app beat to the flock get NotOwner instead of a Bot."""
    if owns():
        return
    if not _OWNER["lean"]:
        _elect()
    if not owns():
        raise NotOwner(owner_info())


def retry_election() -> None:
    """A forward was refused (the owner just quit): try the flock now rather
    than on the scheduler's next pass, so the page's next poll lands here."""
    if not _OWNER["lean"]:
        _elect()


def workspace_claimed() -> bool:
    """May this process run bot engines? True when it holds the workspace claim (module docstring) and, in an
    owner that ran `_become_owner`, the workspace steps (migration, seed, takeover walk) have run under it. A miss
    re-tries the claim (one non-blocking flock), so a quitting main app frees the doors at once; a claim won here
    between passes still answers False until the scheduler's next pass has run those steps (≤ SCHED_EVERY_S): they
    must never run inline on a request thread racing the pass. Never elects: a non-owner is False."""
    if not owns():
        return False
    if _OWNER["ws_fh"] is None and not _claim_workspace():
        return False
    return bool(_OWNER["ws_ready"] or not _OWNER["ready"])


def workspace_read_only() -> bool:
    """Is bot state read-only here (`Bot.read_only`, module docstring)? True in an owner that cannot hold the
    workspace claim. A Bot built in a non-owner (a test's bare Bot) is not read-only, and no lock file yet means no
    claimant: answered without creating one (a bare `bot.create` must not leave `.routines.lock` behind)."""
    if not owns():
        return False
    if _OWNER["ws_fh"] is None and not os.path.exists(os.path.join(bpaths.bots_root(), WS_LOCK)):
        return False
    return not workspace_claimed()


def require_workspace() -> None:
    """The one refusal for every door that STARTS a bot engine or a routine run (module docstring). Elects lazily
    first, like require_owner (NotOwner when another app owns this home), then raises WorkspaceBusy when another
    home's owner holds the workspace claim."""
    require_owner()
    if not workspace_claimed():
        raise WorkspaceBusy(bpaths.bots_root(), workspace_info())


def _elect() -> bool:
    """Try the flock. True when this process owns afterwards. On a win: wait for
    the previous owner's pid to be gone (an older app drops the lock before its
    engines stop), write our record, and in a process that runs start() the
    scheduler pass does the rest (`_become_owner`)."""
    with _OWNER["elect_lock"]:
        if owns():
            return True
        if not _take_routines_lock():
            return False
        prev = _read_lock_record()
        _wait_for_pid(prev.get("pid") if prev else None)
        _write_lock_record()
        logger.info("bots: this process owns the bots now (pid %d)", os.getpid())
        return True


def _wait_for_pid(pid, budget_s: float = OLD_OWNER_WAIT_S) -> None:
    """Block until `pid` is gone, up to `budget_s`. The pid comes from the
    record the previous owner wrote; an older app wrote none (nothing to wait
    for) and our own pid is never waited on."""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return
    if pid <= 0 or pid == os.getpid():
        return
    end = time.monotonic() + budget_s
    while time.monotonic() < end:
        try:
            _platform.probe_pid(pid)
        except OSError:
            return
        time.sleep(0.2)
    logger.warning("bots: previous owner pid %d still alive after %.0fs; proceeding", pid, budget_s)


def _record() -> dict:
    from fused_render import __version__, _flavor
    try:
        origin = bpaths.server_origin_quiet()
    except Exception:  # noqa: BLE001
        origin = ""
    try:
        from fused_render.health import boot_id
        bid = boot_id()
    except Exception:  # noqa: BLE001
        bid = ""
    return {"pid": os.getpid(), "origin": origin, "app": _flavor.display_name(), "version": __version__,
            "boot_id": bid, "ticked_at": time.time()}


def _write_lock_record() -> None:
    fh = _OWNER["fh"]
    if fh is None:
        return
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(_record()))
        fh.flush()
    except OSError:
        logger.debug("bots: could not write the lock record", exc_info=True)


def _read_lock_record() -> dict | None:
    """The owner's record from the lock file; None when the file is empty or
    unreadable (no owner, or an owner from before records existed)."""
    try:
        with open(os.path.join(bpaths.data_root(), "routines.lock"), encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        _platform.probe_pid(pid)
        return True
    except (OSError, TypeError, ValueError):
        return False


def owner_info() -> dict | None:
    """Who owns the bots, for a forwarder and for `/api/bots/setup.owner`:
    `{this, pid, origin, app, version, stalled, known}` or None when nobody
    does. `known` False = an older app holds the flock but wrote no record
    (we know someone owns, not who or where). `stalled` = the owner's pid
    lives but its last pass is older than STALL_S: flagged, never taken over
    (a flock cannot be stolen from a live process, and should not be)."""
    if owns():
        rec = _record()
        return {**rec, "this": True, "stalled": False, "known": True}
    if _try_probe_lock():
        return None  # the flock is free: whatever record is in the file is from an owner that is gone
    rec = _read_lock_record()
    if rec is None or not _pid_alive(rec.get("pid")):
        # Held, but no usable record: an older app (wrote nothing), or a record left by a crashed owner while
        # a different, older process holds the flock now.
        return {"this": False, "pid": None, "origin": "", "app": "", "version": "", "stalled": False, "known": False}
    ticked = float(rec.get("ticked_at") or 0)
    return {**rec, "this": False, "stalled": (time.time() - ticked) > STALL_S, "known": True}


def _try_probe_lock() -> bool:
    """True when the flock is free right now (nobody owns). Takes and releases
    it without keeping it: a probe, not an election — a lean server asks this."""
    try:
        fh = open(os.path.join(bpaths.data_root(), "routines.lock"), "a+", encoding="utf-8")
    except OSError:
        return False
    if not _platform.flock(fh, blocking=False):
        fh.close()
        return False
    try:
        _platform.funlock(fh)
    finally:
        fh.close()
    return True


def _become_owner() -> None:
    """What every start() used to do, now once, in the winner, before its first
    tick: layout migration, the FusedBot import, the v3 folder migration, the Super Bot seed, the orphan
    browser sweep, the channels router, the takeover walk, the wake agent.
    Idempotent per ownership; errors in one step never block the next. The steps that read or write the shared
    workspace (`_WS_STEPS`) run only under the workspace claim (module docstring): when another home holds it
    they wait for the pass that wins it (`_workspace_steps`)."""
    if _OWNER["ready"]:
        _workspace_steps()
        return
    _OWNER["ready"] = True
    with _OWNER["ws_lock"]:
        claimed = _claim_workspace()
        if claimed:
            _OWNER["ws_ready"] = True
    # Router BEFORE anything that builds a Bot (the takeover walk, the sweep): a Bot's __init__ may emit an
    # interrupted hand-off's result card that must reach the phone, and on_event skips a router that is None.
    steps = [("layout migration", lambda: bpaths.migrate_layout()),
             ("FusedBot import", _import_fusedbot),
             ("v3 folder migration", _migrate_v3),
             ("Super Bot seed", _seed_super),
             ("channels router", _start_router),
             ("takeover walk", _reconcile),
             ("browser sweep", _sweep_browsers),
             ("routines wake agent", sync_wake)]
    _run_steps([x for x in steps if claimed or x[0] not in _WS_STEPS])


_WS_STEPS = ("v3 folder migration", "Super Bot seed", "takeover walk", "browser sweep")


def _run_steps(steps) -> None:
    for label, step in steps:
        try:
            step()
        except Exception:  # noqa: BLE001 — a missing piece is not a broken owner
            logger.warning("bots owner: %s failed", label, exc_info=True)


def _workspace_steps() -> None:
    """The shared-workspace steps, once, on the first pass that holds the workspace claim after an ownership
    that started without it (a main app on the same workspace quit). The router is already up (`_become_owner`),
    so the takeover walk's Bots can reach the phone as they would at first ownership."""
    with _OWNER["ws_lock"]:
        if _OWNER["ws_ready"] or not _claim_workspace():
            return
        _OWNER["ws_ready"] = True
    _run_steps((("v3 folder migration", _migrate_v3), ("Super Bot seed", _seed_super),
                ("takeover walk", _reconcile), ("browser sweep", _sweep_browsers)))


def _claim_workspace() -> bool:
    """The workspace claim: a non-blocking flock on `<bots_root>/.routines.lock`, kept while this process owns
    (module docstring). True when held. A miss logs once and is re-tried by the caller's next pass."""
    if _OWNER["ws_fh"] is not None:
        return True
    if time.monotonic() < _OWNER["ws_yield_until"]:
        return False  # just yielded: the taker's claim goes first (module docstring)
    try:
        root = bpaths.bots_root()
        fh = open(os.path.join(root, WS_LOCK), "a+", encoding="utf-8")
    except OSError:
        logger.warning("bots: could not open the workspace routines lock", exc_info=True)
        return False
    try:
        held = _platform.flock(fh, blocking=False)
    except OSError:
        held = False
    if not held:
        fh.close()  # re-tried every pass: never leak the fd
        if not _OWNER["ws_logged"]:
            _OWNER["ws_logged"] = True
            logger.info("bots: another fused-render owns routines for %s; serving this home without ticking them",
                        root)
        return False
    _OWNER["ws_fh"] = fh
    _write_ws_record()
    if _OWNER["ws_logged"]:
        logger.info("bots: took over routines for %s", root)
    _OWNER["ws_logged"] = False
    return True


def _write_ws_record() -> None:
    """Our record into the workspace lock file (the owner record's shape plus `home`), so another home's server
    can name and reach the claimant (workspace_info). Written on the claim and refreshed by every pass."""
    fh = _OWNER["ws_fh"]
    if fh is None:
        return
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({**_record(), "home": bpaths.root()}))
        fh.flush()
    except OSError:
        logger.debug("bots: could not write the workspace lock record", exc_info=True)


def _read_ws_record() -> dict | None:
    try:
        with open(os.path.join(bpaths.bots_root(), WS_LOCK), encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    try:
        rec = json.loads(raw) if raw else None
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def workspace_info() -> dict | None:
    """Who holds the workspace claim: `{this, known, pid, origin, app, version, home}`; None when nobody does
    (the flock is free) or when this process holds it. `known` False = held by an older build that wrote no
    record (nothing to post a yield to)."""
    if _OWNER["ws_fh"] is not None:
        return None
    path = os.path.join(bpaths.bots_root(), WS_LOCK)
    if not os.path.exists(path):
        return None
    try:
        fh = open(path, "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        if _platform.flock(fh, blocking=False):
            _platform.funlock(fh)
            return None
    finally:
        fh.close()
    rec = _read_ws_record()
    if rec is None or not _pid_alive(rec.get("pid")):
        return {"this": False, "known": False, "pid": None, "origin": "", "app": "", "version": "", "home": ""}
    return {"this": False, "known": True, "pid": rec.get("pid"), "origin": rec.get("origin") or "",
            "app": rec.get("app") or "", "version": rec.get("version") or "", "home": rec.get("home") or ""}


def yield_workspace(budget_s: float = WS_YIELD_BUDGET_S) -> bool:
    """Let another home's server take the workspace claim (module docstring): stop every loaded bot's task and
    every Chrome this process launched, join the task threads on the budget, drop the claim, forget the Bots (the
    next `get()` rebuilds them read-only from disk, so nothing here writes over the taker's state) and skip
    re-claiming for WS_YIELD_COOLDOWN_S. True when this process held the claim; False (nothing done) otherwise."""
    from fused_render.bots import browsers
    with _OWNER["ws_lock"]:
        if _OWNER["ws_fh"] is None:
            return False
        started = time.monotonic()
        bots = loaded()
        for b in bots:
            try:
                b.stop()
            except Exception:  # noqa: BLE001
                logger.warning("bot %s: stop failed while yielding the workspace", getattr(b, "id", "?"), exc_info=True)
        try:
            browsers.stop_all(budget_s)
        except Exception:  # noqa: BLE001
            logger.warning("stopping the bots' Chromes failed while yielding the workspace", exc_info=True)
        for b in bots:
            t = getattr(b, "thread", None)
            if t is not None and t.is_alive():
                t.join(max(0.0, started + budget_s - time.monotonic()))
        _OWNER["ws_yield_until"] = time.monotonic() + WS_YIELD_COOLDOWN_S
        _drop_workspace_claim()
        with _lock:
            _bots.clear()
        logger.info("bots: yielded the workspace claim for %s on request", bpaths.bots_root())
        return True


def take_workspace(post_yield) -> dict:
    """Take the workspace claim for this process (the page's Override): `post_yield(origin)` asks the holder to
    yield (routes.py does the HTTP), then the claim is tried here and the workspace steps run at once under
    `ws_lock` (the pass takes the same lock), so the doors open when this returns rather than a pass later.
    Returns `{taken, holder, error}`; raises nothing into the route."""
    require_owner()
    if workspace_claimed():
        return {"taken": True, "holder": None, "error": ""}
    holder = workspace_info()
    if holder is not None:
        if not holder.get("origin"):
            return {"taken": False, "holder": holder,
                    "error": "the holder is an older build that cannot be asked to let go; quit it"}
        err = post_yield(holder["origin"])
        if err:
            return {"taken": False, "holder": holder, "error": err}
    with _OWNER["ws_lock"]:
        _OWNER["ws_yield_until"] = 0.0  # a server that yielded earlier may take it back from its own window
        if not _claim_workspace():
            return {"taken": False, "holder": workspace_info(),
                    "error": "the claim is still held; try again in a moment"}
        if _OWNER["ready"]:
            _workspace_steps()
    logger.info("bots: took the workspace claim for %s on request", bpaths.bots_root())
    return {"taken": True, "holder": holder, "error": ""}


def _drop_workspace_claim() -> None:
    fh, _OWNER["ws_fh"] = _OWNER["ws_fh"], None
    _OWNER["ws_ready"] = False
    _OWNER["ws_logged"] = False
    if fh is None:
        return
    try:  # an empty file reads as "nobody" to the next workspace_info(); our record must not outlive the claim
        fh.seek(0)
        fh.truncate()
        fh.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        _platform.funlock(fh)
    except Exception:  # noqa: BLE001
        pass
    try:
        fh.close()
    except Exception:  # noqa: BLE001
        pass


def _import_fusedbot() -> None:
    from fused_render.bots import fusedbot_import
    fusedbot_import.import_once()


def _migrate_v3() -> None:
    # After the FusedBot import (which writes the legacy layout this lifts) and before the Super Bot seed and
    # anything that builds a Bot: from here on only the per-bot folders under the workspace are read.
    from fused_render.bots import migrate_v3
    migrate_v3.migrate_once()


def _seed_super() -> None:
    from fused_render.bots import migrate_v3
    if not os.path.exists(migrate_v3.stamp_path()):
        # Until the v3 migration has stamped, a legacy Super Bot is still in the legacy tree, invisible to
        # super_id(): a seed now would make a second one, and super-seeded.json would keep it for good.
        logger.info("Super Bot seed skipped: the v3 folder migration has not completed")
        return
    if _legacy_bots_remain():
        # Legacy bots after the stamp (a later FusedBot import whose lift failed, or a parked unplaceable bot): one
        # of them may be a Super Bot super_id() cannot see yet, and a seed now would make a second one for good.
        logger.info("Super Bot seed skipped: legacy bots are still waiting to be migrated")
        return
    seed_super()


def _legacy_bots_remain() -> bool:
    """A BOT still waiting: a legacy data dir holding a bot.json, or a parked `<bots_root>/.unmigrated/<id>/bot.json`.
    Strays (`.unmigrated/stray-<ts>/`, parked from every real tree: `cache/bots/browsers` and the like) and empty
    dirs never count, or the seed would wait forever on an install without a seed stamp."""
    from fused_render.bots import migrate_v3
    if migrate_v3._legacy_bots():
        return True
    parked = os.path.join(bpaths.bots_root(), migrate_v3.UNMIGRATED)
    try:
        names = os.listdir(parked)
    except OSError:
        return False
    return any(not n.startswith("stray-") and os.path.isfile(os.path.join(parked, n, store.BOT_FILE)) for n in names)


def _sweep_browsers() -> None:
    from fused_render.bots import browsers, migrate_v3
    if not os.path.exists(migrate_v3.stamp_path()):
        # Until the v3 migration has stamped, the bots still in the legacy tree are invisible to list_ids(), so
        # the sweep would read their browsers as orphans and delete logins nothing has backed up yet.
        logger.info("browser sweep skipped: the v3 folder migration has not completed")
        return
    n = browsers.sweep(all())
    if n:
        logger.info("removed %d orphan browser folder(s)", n)


def _start_router() -> None:
    with _lock:
        need = _chan["router"] is None
    if not need:
        return
    from fused_render.bots import channels
    from fused_render.bots.channels.router import Router
    import fused_render.bots.registry as me
    r = Router(me)
    for ch in channels.available():
        r.add(ch)
    with _lock:
        if _chan["router"] is None:
            _chan["router"] = r
        else:
            r = None  # a concurrent start won
    if r is not None:
        r.start()


def _reconcile() -> None:
    """The takeover walk: disk state an owner that quit left behind. A bot.json
    that still says running / waiting / paused with no thread here goes idle with a
    system line; a routine stuck on "started" gets outcome "stopped" so its
    card and the breaker tell the truth. Runs once per workspace claim (a
    `_WS_STEPS` step), which also covers a plain restart after a crash; never
    without the claim, where the claimant's running tasks have no thread here
    and would be stopped as stale."""
    for b in all():
        try:
            meta = b.meta
            stale_task = meta.get("status") in store.BUSY_STATUSES and not b.running()
            stuck = [r for r in meta.get("routines") or [] if r.get("last_result") == "started"]
            if not stale_task and not stuck:
                continue
            if stale_task:
                b.emit("system", "Stopped: the app quit while this task ran.")
                b.set_status("idle", note="", waiting_on=None, task_via=None)
            for r in stuck:
                b._routine_outcome(r.get("task") or "", "stopped", "the app quit")
        except Exception:  # noqa: BLE001
            logger.debug("reconcile failed for %s", getattr(b, "id", "?"), exc_info=True)


def sync_wake() -> None:
    """Point the routines' share of the wake agent (schedule_wake, the
    LaunchAgent Tasks already keep) at the soonest due daily and once routines
    across every bot. Interval routines are left out on purpose (Q1, 2026-10-13):
    a 5-minute routine would relaunch a quit app five minutes after every Quit.
    Owner only; best-effort; never raises into a route or a tick."""
    if not owns():
        return
    try:
        from fused_render import schedule_wake
        schedule_wake.sync(routines_due(), source="bots")
    except Exception:  # noqa: BLE001
        logger.debug("bots: could not sync the routines wake agent", exc_info=True)


def routines_due() -> list:
    """Every enabled daily / once routine's `next` across every bot ON DISK, as
    UTC datetimes. Disk, not `loaded()`: a bot nobody has built yet (right after
    a takeover, before the first tick) has routines too, and every writer saves
    bot.json before syncing. Needs no ownership — schedule_wake seeds the bots'
    share of the plist from here in any process that syncs the Tasks share."""
    from datetime import datetime, timezone
    due = []
    for bid in store.list_ids():
        try:
            meta = store.read_meta(bid)
        except (OSError, ValueError):
            continue
        for r in meta.get("routines") or []:
            if r.get("enabled") and r.get("kind") in ("daily", "once") and r.get("next"):
                try:
                    due.append(datetime.fromtimestamp(float(r["next"]), tz=timezone.utc))
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
    return due


# ------------------------------------------------------------- threads ---
def _sweep_handoffs() -> None:
    """One handoffs.sweep pass (queued starts, timeouts, gone targets). Never raises."""
    try:
        from fused_render.bots import handoffs
        import fused_render.bots.registry as me
        handoffs.sweep(me)
    except Exception:  # noqa: BLE001
        logger.debug("hand-off sweep failed", exc_info=True)


def _scheduler(stop: threading.Event) -> None:
    # One owner per machine (module docstring). Each pass first makes sure this
    # process owns; a forwarder keeps looping and re-trying the flock, so when
    # the owner quits the bots fall to it on its next pass. Every outcome is an
    # INFO line: "did the scheduler run" must be answerable from the log.
    standing_down = False
    while not stop.is_set():
        if not owns() and not _elect():
            if not standing_down:
                standing_down = True
                logger.info("bots: %s owns the bots on this machine; forwarding until it quits",
                            _owner_words(_read_lock_record()))
            stop.wait(SCHED_EVERY_S)
            continue
        if standing_down:
            standing_down = False
            logger.info("bots: took over the bots (the previous owner quit)")
        try:
            _pass()
        except Exception:  # noqa: BLE001
            logger.warning("scheduler pass failed", exc_info=True)
        stop.wait(SCHED_EVERY_S)


def _pass() -> None:
    """One owner pass: the owner steps (once), the heartbeat, then the hand-off sweep and every bot's routine
    tick only while this process holds the workspace claim (module docstring)."""
    _become_owner()
    _write_lock_record()  # the heartbeat (`ticked_at`), read by owner_info() in other processes
    if not _claim_workspace():
        return  # another home's owner sweeps and ticks this workspace; re-tried next pass
    _write_ws_record()  # the claimant's heartbeat, read by workspace_info() in other homes
    _sweep_handoffs()
    for b in all():
        try:
            b.tick_routines()
        except Exception:  # noqa: BLE001
            logger.warning("routine tick failed for %s", getattr(b, "id", "?"), exc_info=True)


SEED_STAMP = "super-seeded.json"


def seed_stamp_path() -> str:
    return os.path.join(bpaths.root(), SEED_STAMP)


def seed_super() -> str | None:
    """Super Bot exists from the first run (owner's ask, 2026-10-07): the one bot
    the app is built around should not be a card among blank starters. Runs from
    `start()`, after `fusedbot_import.import_once` (an imported tree may carry
    one). Witness, not emptiness: `<home>/bots/super-seeded.json` records the
    seed, so a Super Bot the user deletes stays deleted (the chooser's card is
    the way back). The greeting is a fixed line (bot.SUPER_GREETING), never a
    model call: Claude may not be linked yet on a fresh install.
    Returns the new id, or None when nothing was seeded."""
    from fused_render.bots import bot as bm
    stamp = seed_stamp_path()
    if os.path.exists(stamp) or bm.super_id() is not None:
        return None
    b = bm.create(kind="super", greet=False)  # meta["setup"] waits for Claude (bot._maybe_super_setup)
    b.emit("done", bm.SUPER_GREETING, source="seed")
    try:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as f:
            json.dump({"id": b.id, "at": time.time()}, f)
    except OSError:
        logger.warning("could not write %s", stamp, exc_info=True)
    return b.id


def start() -> None:
    """Start the scheduler thread (idempotent). Its first pass elects; the
    winner then runs `_become_owner` (seed, import, sweep, router, takeover
    walk, wake agent) before its first tick, a loser forwards and keeps trying.
    Nothing that touches the shared tree runs here any more: a process that
    never wins never writes. A lean server never calls this (server/app.py)."""
    if _OWNER["lean"]:
        return
    with _lock:
        t = _sched["thread"]
        if t is not None and t.is_alive():
            return  # already running: no second election
    # One synchronous election first (the startup hook awaits this off the loop): a win runs _become_owner
    # BEFORE the first request can pass require_owner() and build Bots mid-migration, and the page's first
    # paint sees an owner that is ready rather than one up to 20 s away.
    if _elect():
        _become_owner()
    with _lock:
        t = _sched["thread"]
        if t is None or not t.is_alive():
            stop = threading.Event()
            t = threading.Thread(target=_scheduler, args=(stop,), daemon=True, name="bots-routines")
            _sched.update(thread=t, stop=stop)
            t.start()


def has_been_used() -> bool:
    """Has this machine used bots before? Plain filesystem checks, nothing
    heavy imported: the Super Bot seed stamp exists, the bots home holds at
    least one bot, or a FusedBot tree is waiting to be imported (a legacy bot
    must not be stranded until someone happens to open the Bots page). The
    boot hook starts the scheduler only when this is true; otherwise the first
    bots API request does (`ensure_started`)."""
    from fused_render.bots import fusedbot_import
    try:
        if os.path.exists(seed_stamp_path()):
            return True
        if store.list_ids():
            return True
        return bool(fusedbot_import.source_root()) and not os.path.exists(
            fusedbot_import.stamp_path())
    except Exception:  # noqa: BLE001 - unreadable home: treat as unused, a request starts it
        return False


def needs_start() -> bool:
    """True when `start()` would do something: not lean and no live scheduler."""
    if _OWNER["lean"]:
        return False
    with _lock:
        t = _sched["thread"]
        return t is None or not t.is_alive()


_start_lock = threading.Lock()


def ensure_started() -> None:
    """Idempotent, serialised `start()` for the lazy path (first bots request).
    Blocking (a win runs the migration/import/seed): call off the event loop."""
    if not needs_start():
        return
    with _start_lock:
        start()


def _take_routines_lock() -> bool:
    """The ballot: one owner per machine (module docstring). The file keeps its
    pre-2026-10-13 name so an installed older app, which takes the same flock
    to tick routines, and this one never both own. Same flock pattern the
    iMessage channel uses (bots/channels/imessage.py; the owner holds that one
    too, through the router). The fd is kept until shutdown()."""
    path = os.path.join(bpaths.data_root(), "routines.lock")
    try:
        fh = open(path, "a+", encoding="utf-8")
    except OSError:
        return False
    if not _platform.flock(fh, blocking=False):
        fh.close()  # re-tried every pass while standing down: never leak the fd
        return False
    _OWNER["fh"] = fh
    return True


def _drop_routines_lock() -> None:
    _drop_workspace_claim()  # the workspace claim lives and dies with the ownership (module docstring)
    fh, _OWNER["fh"] = _OWNER["fh"], None
    _OWNER["ready"] = False
    if fh is None:
        return
    try:  # an empty file reads as "no owner" to the next reader; our record must not outlive us
        fh.seek(0)
        fh.truncate()
        fh.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        _platform.funlock(fh)
    except Exception:  # noqa: BLE001
        pass
    try:
        fh.close()
    except Exception:  # noqa: BLE001
        pass


def router():
    """The channels router, or None when start() has not run (tests, a lean `open`)."""
    return _chan["router"]


def on_event(bot, ev: dict) -> None:
    """bot.emit() -> the hand-off state machine (always: tests and a lean `open`
    have no router), then the router, when there is one. Never raises into a
    task thread."""
    if ((ev or {}).get("via") or {}).get("kind") == "handoff":  # channels.base.HANDOFF_KIND; cheap for every other event
        try:
            from fused_render.bots import handoffs
            handoffs.on_event(bot, ev)
        except Exception:  # noqa: BLE001
            logger.debug("hand-off on_event failed", exc_info=True)
    r = _chan["router"]
    if r is None:
        return
    try:
        r.on_event(bot, ev)
    except Exception:  # noqa: BLE001
        logger.debug("channels on_event failed", exc_info=True)


def channel_states() -> dict | None:
    r = _chan["router"]
    if r is None:
        return None
    try:
        return r.states()
    except Exception:  # noqa: BLE001
        return None


def imessage_state():
    """The iMessage channel's state (the old status key; `channels` carries every channel)."""
    r = _chan["router"]
    if r is None:
        return None
    try:
        return r.state("imessage")
    except Exception:  # noqa: BLE001
        return None


def shutdown(budget_s: float = 8.0) -> None:
    """Stop the threads, then every loaded bot's task and every Chrome this
    process launched. Two callers: the server lifespan (`_shutdown_bots`) and
    the packaged app's quit (app.py `_stop_children`), where the lifespan never
    runs — a quit used to leave every bot's Chrome running with ppid 1.

    Order, and why it is not `Bot.shutdown()` per bot: that joins the task
    thread for 5 s BEFORE quitting Chrome, and quits Chromes one after another
    (up to 8 s each), so under the quit's budget the first Chrome was barely
    signalled and the rest never were. Here the launch latch goes first (a
    route or the scheduler mid-tick must not spawn a replacement), every task
    is flagged to stop (releasing CDP waits and interrupting `claude`), every
    Chrome is told to close at once (`browsers.stop_all`), and only then are
    the task threads joined on whatever budget is left."""
    # A module load, no Chrome spawned: at shutdown the "import browser.py
    # lazily" rule in the module docstring has nothing left to protect.
    from fused_render.bots import browser as browser_mod
    from fused_render.bots import browsers

    browser_mod.refuse_launches()
    with _lock:
        if _sched.get("stop") is not None:
            _sched["stop"].set()
        r, _chan["router"] = _chan["router"], None
    if r is not None:
        try:
            r.stop()
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        _sched.update(thread=None, stop=None)
    # The lock is dropped LAST (below): a successor that wins it builds Bots from disk at once, and this
    # process's engines are still writing statuses and routine outcomes until the joins below are done.
    started = time.monotonic()
    bots = loaded()
    for b in bots:
        try:
            b.stop()
        except Exception:  # noqa: BLE001
            logger.warning("bot %s: stop failed", getattr(b, "id", "?"), exc_info=True)
    try:
        n = browsers.stop_all(budget_s)
    except Exception:  # noqa: BLE001
        n = -1
        logger.warning("stopping the bots' Chromes failed", exc_info=True)
    for b in bots:
        t = getattr(b, "thread", None)
        if t is not None and t.is_alive():
            t.join(max(0.0, started + budget_s - time.monotonic()))
    logger.info("bots shutdown: %d Chrome process(es) told to quit in %.1fs", n, time.monotonic() - started)
    _drop_routines_lock()


def reset_for_tests() -> None:
    """Forget every Bot and thread handle (tests; never in the app)."""
    try:
        from fused_render.bots import browser as browser_mod
        browser_mod.allow_launches()  # a test that ran shutdown() must not latch the next one
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        if _sched.get("stop") is not None:
            _sched["stop"].set()
        _sched.update(thread=None, stop=None)
        r, _chan["router"] = _chan["router"], None
        _bots.clear()
    if r is not None:
        try:
            r.stop()
        except Exception:  # noqa: BLE001
            pass
    # The flock too: the next test's home is a new path, and a flock this process still holds on the
    # old file would be harmless, but a test that re-uses a home must be able to elect again.
    _drop_routines_lock()
    _OWNER["lean"] = False


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

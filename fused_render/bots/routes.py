"""`/api/bots/*` and `/api/bot-apps/*` — the Bots sub-app's HTTP API (docs/bots.md §3).

A FastAPI router included from fused_render/server/app.py. FusedBot served the
gallery under `/api/apps/*`; fused-render already owns that prefix (its own
apps hub), and a `/api/bots/apps/*` spelling would be swallowed by the
`/api/bots/{bid}/{op}` catch-all, so the gallery lives at `/api/bot-apps/*`.

Each route is one branch of OpenBot's `agents._main` (the page used to call
`main(action=…)`; here every action is a route). Reads are plain GETs; every
POST/DELETE needs `X-Fused: 1`. Errors are `{"error": "<sentence>"}`:
`ValueError` / `RuntimeError` from the bot layer -> 400, an unknown bot -> 404.

The bot layer (`registry`, `bot`, `store`, `apps`) is imported lazily so the
server imports this router without pulling in Chrome/CDP code.
"""
from __future__ import annotations

import base64
import functools
import json
import os
import threading
import time

from fastapi import APIRouter, Body, Header, Query, Response

from fused_render.bots import paths as bpaths
from fused_render.bots import registry
from fused_render.server.common import _error, _require_fused

router = APIRouter()

ATTACH_MAX = 8 * 1024 * 1024
APP_IMPORT_MAX = 64 * 1024 * 1024
IDLE_SHOT_TIMEOUT_S = 3


def _bot(bid):
    return registry.get(bid)


def _store():
    from fused_render.bots import store
    return store


def _botmod():
    from fused_render.bots import bot
    return bot


def _apps():
    from fused_render.bots import apps
    return apps


def _apptools():
    from fused_render.bots import apptools
    return apptools


def _presets():
    from fused_render.bots import presets
    return presets


def _starters():
    from fused_render.bots import starters
    return starters


def _handled(fn):
    """ValueError/RuntimeError -> 400 (404 for an unknown bot), plus the slow-call log."""
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        t0 = time.time()
        try:
            return fn(*args, **kwargs)
        except (ValueError, RuntimeError) as e:
            msg = str(e) or e.__class__.__name__
            return _error(msg, 404 if msg.startswith("no such bot") else 400)
        finally:
            registry.slow_log(fn.__name__, int((time.time() - t0) * 1000),
                              kwargs.get("bid") or kwargs.get("shot_for") or "", bool(kwargs.get("fast") in ("1", "true", True)))
    return wrap


def _truthy(v) -> bool:
    return v in (True, 1, "1", "true", "True", "yes")


# ------------------------------------------------------------------ status ---
def _status_bot(b, shot_for: str, fast: bool, cursors: dict) -> dict:
    """OpenBot `_main`'s status branch for one bot: idle shot refresh, idle
    sleep, popup recovery, then the summary."""
    st = b.meta.get("status")
    idle = st in ("idle", "waiting", "paused", "error")
    running = b.thread is not None and b.thread.is_alive()
    if shot_for == b.id:
        b.last_looked = time.time()  # a shared browser's idle sleep (bot._may_sleep) must not quit under the bot on screen
    # A popped-out window the user closed -> back to headless. Any bot on the (possibly shared) process may notice: the
    # one that popped out gets control back, the others just carry on. Non-blocking: a poll mid-relaunch skips.
    try:
        if b.browser.headed() and b.browser.window_closed() and b.browser.lock.acquire(blocking=False):
            try:
                if b.browser.window_closed():
                    b.dock(closed=True)
            except Exception:  # noqa: BLE001
                pass
            finally:
                b.browser.lock.release()
    except Exception:  # noqa: BLE001
        pass
    if b.idle_sleep_due(shot_for == b.id):
        # Quitting Chrome (and sealing for encrypted bots) takes seconds: off the poll
        # thread. The sleep thread takes the browser RLock itself (a lock taken here
        # would stay owned by this finished request thread forever).
        def _sleep(bot=b):
            if not bot.browser.lock.acquire(blocking=False):
                return  # a step or take-over is using the browser; try again next idle period
            try:
                bot.idle_sleep()
            finally:
                bot.browser.lock.release()
        threading.Thread(target=_sleep, daemon=True, name=f"sleep-{b.id}").start()
        b.meta["updated"] = time.time()  # one attempt per idle period
    if shot_for == b.id and b.meta.get("control") and not running and not b.recovering:
        b.recovering = True

        def _recover(bot=b):
            try:
                if bot.browser.lock.acquire(blocking=False):
                    try:
                        bot._recover_popup()
                    finally:
                        bot.browser.lock.release()
            except Exception:  # noqa: BLE001
                pass
            finally:
                bot.recovering = False
        threading.Thread(target=_recover, daemon=True, name=f"recover-{b.id}").start()
    if shot_for == b.id and idle and not b.shooting and b.browser.alive() and time.time() - b.browser.shot_ts() > 4:
        b.shooting = True

        def _shoot(bot=b):
            failed = False
            try:
                if bot.browser.lock.acquire(blocking=False):
                    try:
                        bot.browser.screenshot(timeout=IDLE_SHOT_TIMEOUT_S)
                    except Exception:  # noqa: BLE001
                        failed = True
                    finally:
                        bot.browser.lock.release()
                if failed:
                    time.sleep(IDLE_SHOT_TIMEOUT_S * 3)
            finally:
                bot.shooting = False
        threading.Thread(target=_shoot, daemon=True, name=f"shot-{b.id}").start()
    s = b.summary(light=fast and b.id != shot_for, detail=b.id == shot_for)
    try:
        cur = int(cursors.get(b.id, 0) or 0)
    except (TypeError, ValueError):
        cur = 0
    s["events"] = b.events_since(cur)
    # After the read: events_since lifts the counter over lines another writer appended, and the page stores this
    # `seq` as its cursor. The pre-read value would make the next poll hand those lines over a second time.
    s["seq"] = b.seq
    return s


@router.get("/api/bots")
@_handled
def bots_status(cursors: str = Query(default=""), shot_for: str = Query(default=""), fast: str = Query(default="0")):
    """Every bot's summary plus its events since the page's cursor (docs §2)."""
    try:
        cur = json.loads(cursors) if cursors else {}
    except ValueError:
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    quick = _truthy(fast)
    out = []
    for b in registry.all():
        try:
            out.append(_status_bot(b, shot_for, quick, cur))
        except Exception:  # noqa: BLE001 — one broken bot must not blank the list
            continue
    # Full-screen polls run 2-3x/s; they skip the usage summary like the liveness probe.
    return {"bots": out, "ts": time.time(),
            "usage": None if quick else _store().usage_summary(),
            "imessage": None if quick else registry.imessage_state(),   # the old key; `channels` carries every channel (§10)
            "channels": None if quick else registry.channel_states()}


@router.post("/api/bots")
@_handled
def bots_create(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bm = _botmod()
    preset = body.get("preset") or ""
    if not isinstance(preset, str):
        raise ValueError("preset must be a preset key")
    kind = body.get("kind") or "bot"
    if kind not in bm.KINDS:
        raise ValueError(f"kind must be one of {', '.join(bm.KINDS)}")
    browser = body.get("browser_id") or ""
    if not isinstance(browser, str):
        raise ValueError("browser_id must be a browser id")
    b = registry.create(body.get("name") or "", body.get("model") or "", body.get("effort") or "", body.get("instructions") or "",
                        preset=preset, kind=kind, browser=browser)
    if body.get("approval") in ("ask", "auto"):
        b.meta["approval"] = body["approval"]
    if body.get("build_access") in bm.BUILD_MODES:
        b.meta["build_access"] = body["build_access"]
    if isinstance(body.get("trusted_apps"), list):
        b.meta["trusted_apps"] = _apptools().clean_trusted_apps(body["trusted_apps"])
    if body.get("engine") in bm.ENGINES and not bm.is_super(b.meta):
        b.meta["engine"] = body["engine"]
    if bm.is_super(b.meta) and body.get("super_access") in bm.SUPER_ACCESS:
        b.meta["super_access"] = body["super_access"]
    b.save()
    if body.get("encrypt"):
        b.set_encrypt(True)
    return {"ok": True, "id": b.id}


@router.get("/api/bots/browsers")
@_handled
def bots_browsers():
    """Every browser (set of logins) a bot uses, with the bots on it (docs §5)."""
    from fused_render.bots import browsers
    return {"ok": True, "browsers": browsers.listing(registry.all())}


@router.post("/api/bots/browsers/{bid}")
@_handled
def bots_browser_op(bid: str, body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Settings > Browsers: {op: rename {name} | encrypt {on} | profile {profile} | signin | delete}.
    Encrypt, profile import and sign-in go through a bot on the browser (any: the flag and the
    profile belong to the browser; sign-in hands that bot's tab to the user in the live view, `bot`
    in the reply says which). Delete moves every bot on it
    to a fresh browser of its own, which takes the old one down with the last of them."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render.bots import browsers
    bm = _botmod()
    bid = os.path.basename(bid or "")
    bots = [b for b in registry.all() if b.browser_id == bid]
    if not bots or not browsers.exists(bid):
        raise ValueError("no such browser")
    op = body.get("op") or ""
    if op == "rename":
        return {"ok": True, "name": browsers.rename(bid, body.get("name") or "")}
    if op == "encrypt":
        bots[0].set_encrypt(_truthy(body.get("on")))
        return {"ok": True}
    if op == "profile":
        bm.import_profile(bots[0], body.get("profile") or "")
        return {"ok": True}
    if op == "signin":
        b = next((x for x in bots if x.meta.get("control")), None) or bots[0]
        b.takeover()
        return {"ok": True, "bot": b.id}
    if op == "delete":
        busy = [b.meta.get("name") or b.id for b in bots if b.thread and b.thread.is_alive()]
        if busy:  # all or nothing: a refusal half-way would leave some bots moved and some on the old logins
            raise ValueError(f"stop {', '.join(busy)}'s task first, then delete the browser")
        for b in bots:
            bm.set_browser(b, "", fresh=True)  # each gets a logged-out browser of its own; the last move removes this one
        return {"ok": True}
    raise ValueError("op must be rename, encrypt, profile, signin, dock or delete")


@router.get("/api/bots/profiles")
@_handled
def bots_profiles():
    return {"ok": True, "profiles": _botmod().chrome_profiles()}


@router.get("/api/bots/presets")
@_handled
def bots_presets():
    """The "+" chooser's catalog (OpenBot `presets`): playbooks as titles only."""
    return {"ok": True, "presets": [{**{k: v for k, v in p.items() if k != "skills"}, "skills": [s["title"] for s in p["skills"]]}
                                    for p in _presets().presets()]}


def _chrome_snapshot() -> dict:
    """`{found, path}`: the first browser from the bots' own candidate list
    (browser.CHROME_CANDIDATES) that exists on this machine — the list
    `find_chrome` walks when a bot starts, so this and the bot cannot disagree.
    FusedBot's `onboarding.chrome_snapshot()`; `found: null` = the probe failed."""
    try:
        from fused_render.bots.browser import CHROME_CANDIDATES

        for p in CHROME_CANDIDATES:
            if p and os.path.exists(p):
                return {"found": True, "path": p}
    except Exception:  # noqa: BLE001
        return {"found": None, "path": None}
    return {"found": False, "path": None}


def _claude_snapshot() -> dict:
    """fused_render.claude_health's CACHED snapshot (minus its fingerprint, as
    `/api/claude/health` answers it), and NEVER a measure: a GET must not
    spawn the CLI or the login shell. A cold or stale cache answers
    `{found: null, cached: false}`; `/api/claude/health` is the live probe."""
    try:
        from fused_render import claude_health

        c = claude_health.cached()
    except Exception:  # noqa: BLE001
        c = None
    if not c:
        return {"found": None, "cached": False}
    return {**{k: v for k, v in c.items() if k != "fingerprint"}, "cached": True}


@router.get("/api/bots/setup")
@_handled
def bots_setup():
    """What a bot needs on this Mac, reported only (replaces FusedBot's
    `/api/onboarding` chrome probe): `{chrome: {found, path}, claude: {...}}`."""
    return {"chrome": _chrome_snapshot(), "claude": _claude_snapshot()}


@router.get("/api/bots/usage")
@_handled
def bots_usage():
    return _store().usage_summary()


@router.get("/api/bots/imessage")
@_handled
def bots_imessage():
    """The bridge's state plus the switch and the stored handle (Settings > Phone reads this one reply;
    `own_handles`, this Mac's Messages accounts, comes from the channel state once chat.db was read)."""
    from fused_render.bots import imessage
    out = dict(registry.imessage_state() or {})
    sid, m = imessage.super_meta()
    out.update({"super_id": sid, "enabled": imessage.super_switch(m) if sid else False,
                "handle": imessage.norm_handle(m.get("imessage")) if sid else ""})
    return out


@router.post("/api/bots/imessage/test")
@_handled
def bots_imessage_test():
    """Settings > Phone "Send a test text": one text from this Mac to Super Bot's handle. Proves the send path and
    triggers macOS's Automation consent for Messages at a moment the user expects a prompt, instead of during
    their first real task. The echo window keeps the text from coming back as a command."""
    from fused_render.bots import imessage
    sid, m = imessage.super_meta()
    handle = imessage.norm_handle(m.get("imessage")) if sid else ""
    if not sid:
        raise ValueError("there is no Super Bot to text from")
    if not handle:
        raise ValueError("set your number first")
    # Through the channel when the router runs, so the bridge's `last_out` moves: Settings > Phone takes that as
    # the durable "a text went out once" and shows the connected view on every later open. A lean `open` (no
    # router) sends directly; the test then proves only the send path.
    r = registry.router()
    ch = r.channels.get("imessage") if r is not None else None
    try:
        if ch is not None:
            ch.send(handle, "Connected. Text me a task any time.")
        else:
            imessage.send_text(handle, "Connected. Text me a task any time.")
    except Exception as e:  # noqa: BLE001
        msg = str(e).strip() or e.__class__.__name__
        low = msg.lower()
        if "not permitted" in low or "not allowed" in low or "-1743" in low:
            msg = ("macOS blocked this app from controlling Messages. Allow it under System Settings > Privacy & Security > "
                   "Automation, then try again.")
        elif "account" in low or "service" in low:
            msg = "Messages is not signed in with an iMessage account on this Mac. Open Messages, sign in, then try again."
        raise ValueError(msg)
    return {"ok": True, "handle": handle}


@router.get("/api/bots/builds")
@_handled
def bots_builds_get():
    return {"builds": _store().builds_read()}


@router.post("/api/bots/builds")
@_handled
def bots_builds_post(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    builds = body.get("builds")
    if not isinstance(builds, list):
        raise ValueError("builds must be a list")
    _store().builds_write(builds)
    return {"ok": True}


# ------------------------------------------------------------- per bot ---
@router.post("/api/bots/{bid}/send")
@_handled
def bot_send(bid: str, body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    text = (body.get("text") or "").strip()
    if not text:
        raise ValueError("empty message")
    _bot(bid).send(text, reply_to=body.get("reply_to"))
    return {"ok": True}


_CONTROL = {"pause": "pause", "resume": "resume", "stop": "stop", "takeover": "takeover",
            "giveback": "giveback", "popout": "popout", "dock": "dock", "wake": "wake_browser", "open": "opened"}


@router.post("/api/bots/{bid}/{op}")
@_handled
def bot_control(bid: str, op: str, body: dict = Body(default=None), x_fused: str | None = Header(default=None)):
    """pause | resume | stop | takeover | giveback | popout | dock | wake | open (the user clicked the bot open: Super Bot's
    first task, docs §5), and the rest of the
    one-segment POSTs (goto, nav, tab, attach, react, flag, settings,
    profile, clone, routines, skills, reveal, tool)."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    body = body if isinstance(body, dict) else {}
    if op in _CONTROL:
        out = getattr(_bot(bid), _CONTROL[op])()
        return {"ok": True, **(out if isinstance(out, dict) else {})}
    fn = _POSTS.get(op)
    if fn is None:
        return _error(f"unknown bot action {op!r}", 404)
    return fn(bid, body)


def _goto(bid, body):
    url = (body.get("url") or "").strip()
    if not url:
        raise ValueError("no url")
    info = _bot(bid).browser.goto(url) or {}
    return {"ok": True, "url": info.get("url")}


def _nav(bid, body):
    op = body.get("op")
    if op not in ("back", "forward", "reload"):
        raise ValueError("op must be back|forward|reload")
    info = getattr(_bot(bid).browser, op)() or {}
    return {"ok": True, "url": info.get("url")}


def _tab(bid, body):
    b = _bot(bid)
    tab, index = body.get("tab"), body.get("index")
    if tab == "new":
        info = b.browser.tab_new(body.get("url") or "about:blank")
    elif tab == "switch":
        info = b.browser.tab_switch(index if index is not None else 0)
    elif tab == "close":
        info = b.browser.tab_close(index)
    else:
        raise ValueError("tab must be new|switch|close")
    return {"ok": True, "url": (info or {}).get("url"), "tabs": b.browser.tabs()}


def _attach(bid, body):
    name = body.get("name") or ""
    raw = body.get("data") if body.get("data") is not None else body.get("text")
    try:
        data = base64.b64decode(raw or "", validate=False)
    except (ValueError, TypeError):
        raise ValueError(f"{name}: not base64") from None
    if len(data) > ATTACH_MAX:
        raise ValueError(f"{name}: too large ({len(data) // 1048576} MB; limit {ATTACH_MAX // 1048576} MB)")
    return {"ok": True, "name": _bot(bid).save_bytes(name, data)}


def _react(bid, body):
    """One emoji per message, keyed by seq in meta["reactions"] ("" clears)."""
    b = _bot(bid)
    try:
        seq = int(body.get("seq"))
    except (TypeError, ValueError):
        raise ValueError("react needs a seq") from None
    emoji = body.get("emoji") or ""
    with b.lock:
        rx = dict(b.meta.get("reactions") or {})
        if emoji:
            rx[str(seq)] = str(emoji)[:8]
        else:
            rx.pop(str(seq), None)
        b.meta["reactions"] = rx
        b.save()
    return {"ok": True, "reactions": rx}


def _flag(bid, body):
    """Sidebar bookkeeping: pinned / hidden / avatar face."""
    b = _bot(bid)
    with b.lock:
        if body.get("pinned") is not None:
            b.meta["pinned"] = bool(body["pinned"])
        if body.get("hidden") is not None:
            b.meta["hidden"] = bool(body["hidden"])
        face = body.get("face")
        if isinstance(face, dict):
            b.meta["face"] = _botmod().check_face(b.meta, face)  # the picker's own write: shape/colour taken as sent
        b.save()
    return {"ok": True}


def _settings(bid, body):
    """OpenBot `rename`: the Settings dialog."""
    from fused_render.bots import imessage
    bm = _botmod()
    b = _bot(bid)
    name = (body.get("name") or "").strip()
    model, effort = body.get("model") or "", body.get("effort") or ""
    bm.check_settings(b.meta, model, effort)  # shared with Super Bot's `bot_settings` (docs §12)
    if name and name != b.meta.get("name") and b.meta.get("artifacts_dir") and not os.path.isdir(b.meta["artifacts_dir"]):
        b.meta.pop("artifacts_dir", None)  # never used on disk: let the new name pick the folder
    b.meta["name"] = name or b.meta.get("name")
    if body.get("instructions") is not None:
        b.meta["instructions"] = str(body["instructions"]).strip()
    if body.get("approval") in ("ask", "auto"):
        b.meta["approval"] = body["approval"]
    if bm.is_super(b.meta):
        # The phone is Super Bot's alone (docs §10): on any other bot these keys are dropped on load, and a stale
        # client sending them back must not plant them again.
        if body.get("imessage_handle") is not None:
            b.meta["imessage"] = imessage.norm_handle(body["imessage_handle"])
        if body.get("imessage_to") is not None:
            b.meta["imessage_to"] = str(body["imessage_to"]).strip()
        if body.get("imessage_enabled") is not None:
            b.meta["imessage_enabled"] = bool(body["imessage_enabled"])
    if body.get("build_access") in bm.BUILD_MODES:
        b.meta["build_access"] = body["build_access"]
    if isinstance(body.get("trusted_apps"), list):
        b.meta["trusted_apps"] = _apptools().clean_trusted_apps(body["trusted_apps"])
    if body.get("engine") in bm.ENGINES and not bm.is_super(b.meta):
        b.meta["engine"] = body["engine"]
    if body.get("super_access") in bm.SUPER_ACCESS and bm.is_super(b.meta):
        b.meta["super_access"] = body["super_access"]
    if body.get("memory") is not None:
        b.set_memory(body["memory"])
    moved = body.get("browser_id") is not None and bm.set_browser(b, str(body["browser_id"]))
    enc = body.get("encrypt")
    if not moved and enc is not None and bool(enc) != bool(b.meta.get("encrypt")):
        b.set_encrypt(enc)  # after a move the dialog's flag was the OLD browser's: the new one keeps its own
    if model:
        b.meta["model"] = model
    if effort:
        b.meta["effort"] = effort
    b.save()
    return {"ok": True}


def _profile(bid, body):
    _botmod().import_profile(_bot(bid), body.get("profile") or "")
    return {"ok": True}


def _clone(bid, body):
    share = body.get("share")
    b = registry.clone(bid, body.get("name") or "", share=True if share is None else _truthy(share))
    return {"ok": True, "id": b.id}


def _routines(bid, body):
    b = _bot(bid)
    op = body.get("op") or ""
    if op == "add":
        r = b.routine_add(body.get("text") or "", body.get("kind") or "", minutes=body.get("minutes"),
                          time_s=body.get("time") or body.get("url") or "", weekdays=body.get("weekdays"), at=body.get("at"))
        return {"ok": True, "routine": r}
    rid = body.get("rid") or ""
    if op == "delete":
        b.routine_update(rid, delete=True)
    elif op in ("enable", "disable"):
        b.routine_update(rid, enabled=op == "enable")
    elif op == "run":
        r = next((x for x in b.routines() if x["id"] == rid), None)
        if not r:
            raise ValueError("no such routine")
        b.routine_fire(r, manual=True)
    else:
        raise ValueError("op must be add|delete|enable|disable|run")
    return {"ok": True}


def _skills(bid, body):
    b = _bot(bid)
    op = body.get("op") or ""
    if op == "save":
        b.skill_save(body.get("name"), body.get("trigger") if body.get("trigger") is not None else body.get("url"),
                     body.get("text"), name=body.get("rid") or None)
    elif op == "delete":
        b.skill_delete(body.get("rid") or "")
    elif op == "learn":
        b.learn_from_last()
    else:
        raise ValueError("op must be save|delete|learn")
    return {"ok": True, "skills": b.skills()}


def _reveal(bid, body):
    return {"ok": True, "path": _bot(bid).reveal(body.get("path") or None)}


def _tool(bid, body):
    """botmcp's tools/call (docs §5): delegated to the agent engine."""
    try:
        from fused_render.bots import agent_engine
    except Exception:  # noqa: BLE001
        return _error("agent engine unavailable", 503)
    b = _bot(bid)
    args = body.get("args")
    try:
        return agent_engine.handle_tool(b, body.get("token") or "", body.get("name") or "", args if isinstance(args, dict) else {})
    except agent_engine.StaleToken as e:
        return _error(str(e) or "stale task token", 409)


_POSTS = {"goto": _goto, "nav": _nav, "tab": _tab, "attach": _attach, "react": _react,
          "flag": _flag, "settings": _settings, "profile": _profile, "clone": _clone, "routines": _routines,
          "skills": _skills, "reveal": _reveal, "tool": _tool}


@router.delete("/api/bots/{bid}")
@_handled
def bot_delete(bid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    _bot(bid)  # 404 for an unknown id before anything is touched
    registry.delete(bid)
    return {"ok": True}


@router.get("/api/bots/{bid}/export")
@_handled
def bot_export(bid: str):
    """The transcript as Markdown."""
    b = _bot(bid)
    name = b.meta.get("name") or "bot"
    return {"ok": True, "name": f"{name}-transcript.md", "text": _store().export_markdown(name, b.events_path)}


@router.get("/api/bots/{bid}/shot")
@_handled
def bot_shot(bid: str):
    """The latest screenshot (PNG); 404 when there is none yet."""
    p = getattr(_bot(bid).browser, "shot_path", "") or ""
    try:
        with open(p, "rb") as f:
            data = f.read()
    except OSError:
        return _error("no screenshot yet", 404)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.get("/api/bots/{bid}/steps/{name}")
@_handled
def bot_step_thumb(bid: str, name: str):
    """One step thumbnail, `<seq>.jpg`, from cache/<id>/steps/."""
    stem = name[:-4] if name.endswith(".jpg") else ""
    if not stem.isdigit():
        return _error("not found", 404)
    p = os.path.join(_bot(bid).steps_dir, f"{int(stem)}.jpg")
    try:
        with open(p, "rb") as f:
            data = f.read()
    except OSError:
        return _error("thumbnail gone", 404)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@router.get("/api/bots/{bid}/tools")
@_handled
def bot_tools(bid: str, token: str = Query(default="")):
    """botmcp's tools/list (docs §5): the roster the agent engine builds for this task."""
    try:
        from fused_render.bots import agent_engine
    except Exception:  # noqa: BLE001
        return _error("agent engine unavailable", 503)
    b = _bot(bid)
    try:
        roster = agent_engine.roster_for(b, token)
    except agent_engine.StaleToken as e:
        return _error(str(e) or "stale task token", 409)
    return roster if isinstance(roster, dict) else {"tools": roster}


# ------------------------------------------------------------------- apps ---
def _under_apps_root(d: str) -> str:
    """`d` as an absolute real path inside the apps root, else ValueError."""
    root = os.path.realpath(bpaths.apps_root())
    if not d or not os.path.isabs(d):
        raise ValueError("dir must be an absolute path")
    real = os.path.realpath(d)
    if real != root and not real.startswith(root + os.sep):
        raise ValueError(f"dir must be under {root}")
    return real


@router.get("/api/bot-apps")
@_handled
def apps_list():
    return _apps().list_apps(bpaths.apps_root())


@router.post("/api/bot-apps/import")
@_handled
def apps_import(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    data = body.get("data") or ""
    if len(data) * 3 // 4 > APP_IMPORT_MAX:
        raise ValueError(f"too large (limit {APP_IMPORT_MAX // 1048576} MB)")
    root = bpaths.apps_root()
    os.makedirs(root, exist_ok=True)
    return _apps().import_app(root, body.get("name") or "", data)


@router.post("/api/bot-apps/mkdir")
@_handled
def apps_mkdir(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    d = body.get("dir") or ""
    _under_apps_root(d)  # refuse anything outside the apps root
    return _apps().mkbuild(os.path.normpath(d))  # echo the caller's spelling (the Builds panel reuses it as `target`)


@router.post("/api/bot-apps/reveal")
@_handled
def apps_reveal(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    # Same root guard as mkdir and icon: `open <dir>` on an arbitrary path
    # would LAUNCH a .app bundle (a directory to the fs), not reveal a folder.
    d = _under_apps_root(body.get("dir") or "")
    return _apps().reveal(d)


@router.get("/api/bot-apps/icon")
@_handled
def apps_icon(dir: str = Query(default="")):  # noqa: A002 — the query key
    d = _under_apps_root(dir)
    for name, mime in (("icon.svg", "image/svg+xml"), ("icon.png", "image/png")):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return Response(content=f.read(), media_type=mime, headers={"Cache-Control": "no-cache"})
    return _error("no icon", 404)


# --------------------------------------------------------- starter apps ---
# Apps that ship inside the package (bots/starters/<key>), installed into the
# apps root on demand (docs §5). Exact paths, all of them more specific than
# any other /api/bot-apps route, so nothing above shadows them.
@router.get("/api/bot-apps/starters")
@_handled
def apps_starters():
    return _starters().list_state(bpaths.apps_root())


@router.get("/api/bot-apps/starters/status")
@_handled
def apps_starters_status():
    """Each installed starter's setup tool, run through the app-tool runner (8 s cap each)."""
    ready, why = _starters().status(bpaths.apps_root())
    return {"ok": True, "ready": ready, "why": why}


def _starter_install(key, x_fused, update):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return {"ok": True, **_starters().install(key, bpaths.apps_root(), update=update)}


@router.post("/api/bot-apps/starters/{key}/install")
@_handled
def apps_starter_install(key: str, x_fused: str | None = Header(default=None)):
    """Copy the starter into the apps root; an existing folder is left alone (`existed`)."""
    return _starter_install(key, x_fused, False)


@router.post("/api/bot-apps/starters/{key}/update")
@_handled
def apps_starter_update(key: str, x_fused: str | None = Header(default=None)):
    """Replace the installed copy's shipped files; its .fused/, .venv and extra files stay."""
    return _starter_install(key, x_fused, True)


@router.get("/api/bot-apps/starters/{key}/icon")
@_handled
def apps_starter_icon(key: str):
    st = _starters()
    s = st.get(key)  # catalog lookup: the URL segment never becomes a path on its own
    if not s or not s["icon"]:
        return _error("no icon", 404)
    base = os.path.realpath(st.STARTERS_DIR)
    p = os.path.realpath(os.path.join(s["src"], s["icon"]))
    if not p.startswith(base + os.sep) or not os.path.isfile(p):
        return _error("no icon", 404)
    mime = "image/svg+xml" if p.endswith(".svg") else "image/png"
    with open(p, "rb") as f:
        return Response(content=f.read(), media_type=mime, headers={"Cache-Control": "max-age=3600"})

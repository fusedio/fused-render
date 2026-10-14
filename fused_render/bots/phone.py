"""`/api/phone/bots/*` — the bots as the Fused Render iOS app sees them (docs/bots.md §14).

A FastAPI router included from fused_render/server/app.py beside the bots
router, and an owned prefix (`routes.OWNED_PREFIXES`) so a process that does
not hold the bots forwards it like `/api/bots`. The LAN listener (lan.py)
forwards this prefix, and only this one of the bots' prefixes, for a paired
device, with `X-Fused-Device: <device id>` stamped from the pairing record
(a client-sent one is dropped). On loopback the routes answer too (D3:
loopback is trusted); the header is then optional and the via addr is "".

Not the desktop page's routes: `GET /api/bots` runs side effects per poll
(idle sleep, popup recovery, screenshot threads) and returns every bot.json
key, and `/api/bots/{bid}/{op}` holds the Mac-screen operations (take-over,
pop-out, profile import, reveal, browsers, encryption, trusted apps, allow
rules, build access, Super Bot's access, the engine, iMessage settings). The
phone gets a chat, the live shot, pause / resume / stop / clear, the small
settings sheet and routines — nothing that acts on the Mac's screen (docs §14).

A message from here is `via {kind: "phone", addr: <device id>}`
(channels.base.phone_via): outside like a text (`is_outside`), so Super Bot
runs it under the phone posture and a plain yes never settles a card. The one
exception is the card the phone is showing: a send with `answer: <seq>` while
the bot waits on that approval card is stamped `via["answers"] = seq`, the
texted-card mechanism (`channels.base.answers_card`), and the gate takes it as
the verdict. "Approve, don't ask again" (`Forever`) never comes from here.

Errors are `{"error": "<sentence>"}` through routes._handled: ValueError /
RuntimeError -> 400, an unknown bot -> 404; an unknown or desktop-only op ->
404; a POST/DELETE without `X-Fused: 1` -> 403 (`_require_fused`).
"""
from __future__ import annotations

import collections
import json
import os
import re
import threading
import time

from fastapi import APIRouter, Body, Header, Query

from fused_render.bots import registry, routes
from fused_render.server.common import _error, _require_fused

router = APIRouter()

PREFIX = "/api/phone/bots"
TEXT_MAX = 200        # a row's task / last line
DETAIL_MAX = 600      # an event's `detail` (a read's page text, a tool's raw result) on a phone screen
LIMIT_DEFAULT = 80
LIMIT_MAX = 400
# The event keys the phone draws (docs §2 BotEvent). Withheld: `trace` (a stack for the desktop's details
# disclosure), `artifacts` (paths on the Mac), `forever` (the third button, never offered on the phone),
# `ignored_seq` and the rest of the bookkeeping. `detail` is kept, cut to DETAIL_MAX.
EVENT_KEYS = ("seq", "ts", "role", "text", "result", "thumb", "options", "multi", "offer", "app", "reply", "via",
              "source", "handoff", "build", "link", "summary", "ref", "channel", "addr", "error", "login")
# The roles a list row's "last line" is taken from: what the user said and what the bot said back.
LAST_ROLES = ("user", "done", "question", "approval", "error")
# The settings sheet's keys (routes._settings with nothing else from the desktop dialog).
SETTINGS_KEYS = ("name", "instructions", "model", "effort", "approval", "max_steps", "memory")
SUPER_MAC_KEYS = ("instructions", "approval", "memory")  # Super Bot: Mac only (see _settings)
ROUTINE_KEYS = ("id", "task", "kind", "minutes", "time", "weekdays", "at", "enabled", "next", "last", "last_result",
                "last_message")
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_last_cache: dict = {}  # bid -> ((size, mtime), last row): the list polls every 3 s; most bots did not move
_last_lock = threading.Lock()


def _botmod():
    from fused_render.bots import bot
    return bot


def _chan():
    from fused_render.bots.channels import base
    return base


def _flat(text, n=TEXT_MAX) -> str:
    return " ".join(str(text or "").split())[:n]


def _device(x_fused_device) -> str:
    """The paired device's id lan.py stamped; "" on loopback (or for anything that is not an id)."""
    v = str(x_fused_device or "").strip()
    return v if _DEVICE_RE.match(v) else ""


def _last(b) -> dict | None:
    """The newest user / done / question / approval / error line, `{seq, role, text, ts}`. Cached per file
    size + mtime: the list polls every bot every 3 s and most transcripts did not change."""
    path = b.events_path
    try:
        st = os.stat(path)
        key = (st.st_size, st.st_mtime_ns)
    except OSError:
        return None
    with _last_lock:
        hit = _last_cache.get(b.id)
    if hit and hit[0] == key:
        return hit[1]
    from fused_render.bots import store
    last = None
    for _i, ev in store.iter_events(path):
        if ev.get("role") in LAST_ROLES:
            last = ev
    row = None if last is None else {"seq": last.get("seq"), "role": last.get("role"),
                                     "text": _flat(last.get("text")), "ts": last.get("ts")}
    with _last_lock:
        _last_cache[b.id] = (key, row)
    return row


def phone_bot(b) -> dict:
    """One list row (PhoneBot). `face` is the bot.json `face` key ({shape, color, icon} or null): with `id`,
    `name` and `status` it is all the desktop's face drawing reads (frontend lib/face.ts `faceOf` / `moodOf`;
    no face = shape and colour hashed from the id, Super Bot's is its fixed mark)."""
    bm = _botmod()
    m = b.meta
    a = bm.activity(b.id, m, b)  # the one reading of "busy" / "blocked" (routes._app_bot, Super Bot's BOTS NOW)
    return {
        "id": b.id,
        "name": m.get("name") or "",
        "kind": "super" if bm.is_super(m) else "bot",
        "status": "idle" if a["status"] == "ended" else a["status"],  # a word that outlived its thread is idle
        "busy": a["busy"],
        "waitingOn": (a["blocked"] or {}).get("kind"),
        "step": int(m.get("step") or 0),
        "task": _flat(m.get("task")),
        "last": _last(b),
        "seq": b.seq,
        "updated": float(m.get("updated") or m.get("created") or 0),  # a bot that never ran has only `created`
        "control": bool(m.get("control")),  # the user holds the browser at the Mac: a send is parked, not run
        "routines": len(m.get("routines") or []),
        "face": m.get("face") if isinstance(m.get("face"), dict) else None,
        "hidden": bool(m.get("hidden")),
        "pinned": bool(m.get("pinned")),
    }


def _event(bid, ev) -> dict:
    out = {k: ev[k] for k in EVENT_KEYS if k in ev}
    if out.get("thumb"):
        out["thumb"] = f"{PREFIX}/{bid}/steps/{os.path.basename(str(out['thumb']))}"
    if ev.get("detail") is not None:
        d = ev["detail"]
        out["detail"] = (d if isinstance(d, str) else json.dumps(d, ensure_ascii=False, default=str))[:DETAIL_MAX]
    return out


def _int(v, default, name):
    if v in (None, ""):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number") from None


# ------------------------------------------------------------------ reads ---
@router.get(PREFIX)
@routes._handled
def phone_bots():
    """Every bot (Super Bot first, then the most recently updated) plus what the list's setup note needs."""
    bm = _botmod()
    rows = []
    for b in registry.all():
        try:
            # A hidden bot is off the desktop's list too (meta["hidden"]): the phone shows the same roster.
            if not b.deleted and not b.meta.get("hidden"):
                rows.append(phone_bot(b))
        except Exception:  # noqa: BLE001 — one broken bot must not blank the list
            continue
    rows.sort(key=lambda r: (r["kind"] != "super", -r["updated"]))
    return {"bots": rows, "ts": time.time(),
            "setup": {"claude": routes._claude_snapshot(), "owner": registry.owner_info(),
                      "models": list(bm.MODELS), "efforts": list(bm.EFFORTS)}}


@router.get(PREFIX + "/presets")
@routes._handled
def phone_presets():
    """The create sheet's preset picker: /api/bots/presets' answer."""
    return routes.bots_presets()


@router.get(PREFIX + "/{bid}")
@routes._handled
def phone_bot_detail(bid: str, since: str = Query(default="0"), before: str = Query(default=""),
                     limit: str = Query(default=""), detail: str = Query(default="0")):
    """One bot (PhoneBotDetail) and its events: `since=0` the last `limit`, `before=<seq>` (with since=0) the
    `limit` before it ("Load earlier"), `since=<seq>` everything after the cursor (`events_since`)."""
    b = routes._bot(bid)
    cur = max(0, _int(since, 0, "since"))
    lim = min(LIMIT_MAX, max(1, _int(limit, LIMIT_DEFAULT, "limit")))
    upto = _int(before, 0, "before")
    # The next cursor is what this read consumed, not `b.seq` read afterwards: an event the task emits between
    # the read and that later read (`_detail` runs in between) would move the cursor past a line never sent.
    if cur:
        events = b.events_since(cur)
        nxt = cur + len(events)
    else:
        tail = collections.deque(maxlen=lim)
        every = b.events_since(0)  # lifts b.seq over lines another writer appended, as the desktop's read does
        nxt = len(every)
        for ev in every:
            if not upto or int(ev.get("seq") or 0) < upto:
                tail.append(ev)
        events = list(tail)
    rows = [_event(b.id, ev) for ev in events]
    return {"bot": _detail(b, routes._truthy(detail)), "events": rows, "seq": nxt,
            "first_seq": rows[0].get("seq") if rows else None, "ts": time.time()}


def _detail(b, full: bool) -> dict:
    m = b.meta
    s = b.summary(light=True)  # light: no liveness probe per poll (status_cached), as the desktop's fast polls
    bs = s.get("browser") or {}
    out = phone_bot(b)
    out.update({
        "instructions": str(m.get("instructions") or ""),
        "model": m.get("model") or "",
        "effort": m.get("effort") or "",
        "approval": "auto" if m.get("approval") == "auto" else "ask",
        "engine": m.get("engine") or "auto",
        "max_steps": int(m["max_steps"]) if m.get("max_steps") else None,
        # The ONE card whose buttons are live: an approval or question the task is blocked on right now.
        "waiting_seq": m.get("waiting_on") if m.get("status") == "waiting" else None,
        "shot": f"{PREFIX}/{b.id}/shot" if s.get("shot") else None,
        "shot_ts": s.get("shot_ts"),
        "viewport": s.get("viewport"),
        "browser": {"running": bool(bs.get("running")), "url": bs.get("url") or None, "title": bs.get("title") or None},
        "routines": [{k: r.get(k) for k in ROUTINE_KEYS} for r in (m.get("routines") or []) if isinstance(r, dict)],
        "memory": b.memory() if full else None,
        "skills": [sk.get("name") for sk in b.skills()] if full else None,
    })
    return out


@router.get(PREFIX + "/{bid}/shot")
@routes._handled
def phone_shot(bid: str):
    return routes.bot_shot(bid)


@router.get(PREFIX + "/{bid}/steps/{name}")
@routes._handled
def phone_step_thumb(bid: str, name: str):
    return routes.bot_step_thumb(bid, name)


# ----------------------------------------------------------------- writes ---
@router.post(PREFIX)
@routes._handled
def phone_create(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """A new ordinary bot: never Super Bot, never encrypted, on a browser of its own (routes.bots_create's
    checks for the fields the sheet sends)."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    preset = body.get("preset") or ""
    if not isinstance(preset, str):
        raise ValueError("preset must be a preset key")
    b = registry.create(body.get("name") or "", body.get("model") or "", body.get("effort") or "",
                        body.get("instructions") or "", preset=preset, kind="bot", browser="")
    if body.get("approval") in ("ask", "auto"):
        b.meta["approval"] = body["approval"]
    b.save()
    return {"ok": True, "id": b.id}


@router.delete(PREFIX + "/{bid}")
@routes._handled
def phone_delete(bid: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    b = routes._bot(bid)  # 404 for an unknown id before anything is touched
    if _botmod().is_super(b.meta):
        raise ValueError("Super Bot cannot be deleted from the phone")
    registry.delete(bid)
    return {"ok": True}


def _send(b, body, device):
    """A message from the phone (`receive`, the one entry point of every channel)."""
    chan = _chan()
    text = str(body.get("text") or "").strip()
    files = body.get("files") if isinstance(body.get("files"), list) else []
    if not text and not files:
        raise ValueError("empty message")
    via = chan.phone_via(device)
    if body.get("answer") is not None:
        # The approval card the phone shows, and only while the bot still waits on it: then the yes or no is the
        # verdict (answers_card; _texted_approval lets it through). A stale or wrong seq is refused rather than
        # sent as a plain message, which the hold would answer with "use the card at the Mac".
        seq = body["answer"]
        with b.lock:
            waiting = b.meta.get("waiting_on") if b.meta.get("status") == "waiting" else None
        ev = b.event_by_seq(seq) if isinstance(seq, int) and not isinstance(seq, bool) and seq == waiting else None
        if not ev or ev.get("role") != "approval":
            raise ValueError("that card is not waiting on an answer")
        from fused_render.bots.agent_engine import NO, YES
        if not (YES.match(text) or NO.match(text)):
            raise ValueError("an approval card is answered with approve or deny")
        via["answers"] = seq
    # An answer is the bare yes or no: a quoted reply would put "Replying to …" in front of it, and the gate's
    # YES / NO would no longer see the verdict.
    reply_to = None if via.get("answers") is not None else body.get("reply_to")
    if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int)):
        raise ValueError("reply_to must be a message seq")
    decoded = routes.decode_files(files)  # every file checked before any is stored
    names = []
    text, images = routes.save_files(b, text, decoded, names)
    with b.lock:
        held = bool(b.meta.get("control"))  # receive parks it in `held` until the Mac hands the browser back
    try:
        # `answers` binds the click to its card in receive too (bot.CardSettled): a card that settled between the
        # check above and here refuses the send before any line is written, instead of a held-back "yes".
        b.receive(text, via=via, reply_to=reply_to or None, images=images or None, answers=via.get("answers"))
    except _botmod().CardSettled:
        raise ValueError("that card is not waiting on an answer") from None
    return {"ok": True, "seq": b.seq, "held": held, "files": names}


def _settings(b, body, _device):
    if _botmod().is_super(b.meta) and any(k in body for k in SUPER_MAC_KEYS):
        # Super Bot's standing instructions, memory and approval go into every task it runs, a routine's
        # included, and a routine runs with its own access (channels.base.is_local): written from here they
        # would steer an unattended run the way a routine's text would (docs §14).
        raise ValueError("Super Bot's instructions, memory and approval are set at the Mac")
    return routes._settings(b.id, {k: body[k] for k in SETTINGS_KEYS if k in body})


def _routines(b, body, _device):
    if _botmod().is_super(b.meta) and body.get("op") in ("add", "edit"):
        # A routine is the user's own task from the Mac (channels.base.is_local): Super Bot runs it with its own
        # access, not the phone posture, so a task text written here would skip every card (docs §5, §14).
        raise ValueError("Super Bot's routines are written at the Mac")
    return routes._routines(b.id, body)


def _control(name):
    def run(b, _body, _device):
        getattr(b, name)()
        return {"ok": True}
    return run


# Everything the phone may do to a bot. Not here on purpose (docs §14): takeover, giveback, popout, dock, wake,
# open, goto, nav, tab, viewport, profile, clone, flag, react, skills, reveal, tool, failures, the app door.
_OPS = {"send": _send, "settings": _settings, "routines": _routines,
        "pause": _control("pause"), "resume": _control("resume"), "stop": _control("stop"),
        "clear": _control("clear_context")}


@router.post(PREFIX + "/{bid}/{op}")
@routes._handled
def phone_op(bid: str, op: str, body: dict = Body(default=None), x_fused: str | None = Header(default=None),
             x_fused_device: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    fn = _OPS.get(op)
    if fn is None:
        return _error(f"unknown bot action {op!r}", 404)
    return fn(routes._bot(bid), body if isinstance(body, dict) else {}, _device(x_fused_device))

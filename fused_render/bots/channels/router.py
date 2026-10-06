"""The router: every channel's inbound becomes `bot.receive(text, via)`, and
every outbound-worthy event a bot emits is delivered under one policy
(docs/bots.md §10).

    inbound   poll thread, every POLL_S: channel.poll() -> resolve the bot
              (owners of the sender, `@name` prefix, else the bot last addressed
              from that sender, else the first) -> map a numbered answer back to
              its option -> bot.receive(text, via)
    outbound  bot.emit() calls on_event(); a queue + one send thread, so a slow
              osascript never blocks a task thread. targets() is the policy:
              reply to the channel the task came from, always; otherwise only
              what the bot's forwards for that channel opt into.

The router exists only while `registry.start()` ran (the full server): tests
and a lean `fused-render open` have none, and `on_event` is a no-op then.
"""
from __future__ import annotations

import logging
import queue
import re
import threading

from fused_render.bots.channels import base
from fused_render.bots.channels.base import FORWARDS_DEFAULT, OUT_ROLES, Caps, Channel, Inbound

logger = logging.getLogger(__name__)

POLL_S = 3
SEND_RETRIES = 3
SEND_RETRY_WAIT_S = 5
_AT_NAME = re.compile(r"^\s*@([\w][\w .-]{0,40}?)\s*[:,]?\s+(.*)\Z", re.S)
_AT_ONLY = re.compile(r"^\s*@([\w][\w .-]{0,40})\s*[:,]?\s*\Z")


def category(ev: dict) -> str:
    """Which forwarding switch an event falls under."""
    v = ev.get("via") or {}
    if ev.get("source") == "build":
        return "builds"
    if v.get("kind") == base.ROUTINE_KIND:
        return "routines"
    role = ev.get("role")
    if role == "done":
        return "results"
    if role in ("question", "approval"):
        return "questions"
    return "errors"


def forwards_for(meta: dict, kind: str) -> tuple:
    fw = (meta.get("channel_forwards") or {}).get(kind)
    if isinstance(fw, list):
        return tuple(str(x) for x in fw)
    return FORWARDS_DEFAULT


def render(ev: dict, caps: Caps) -> tuple[str, list[str]]:
    """The text a surface gets for one event, and the options it was numbered
    with (empty when the surface shows buttons itself). A phone has only the
    reply box, so choices become "Reply 1 … · 2 …" and approvals "Reply yes or no"."""
    text = (ev.get("text") or "").strip()
    opts = [str(o).strip() for o in (ev.get("options") or []) if str(o).strip()]
    numbered: list[str] = []
    if opts and not caps.options:
        numbered = opts
        text += "\n\nReply " + " · ".join(f"{i + 1} {o}" for i, o in enumerate(opts))
    elif ev.get("role") == "approval" and not caps.buttons:
        text += "\n\nReply yes or no."
    if caps.max_len and len(text) > caps.max_len:
        head = text[:caps.max_len]
        cut = max(head.rfind("\n"), head.rfind(". "), head.rfind("! "), head.rfind("? "))
        if cut < caps.max_len // 2:
            cut = caps.max_len
        text = head[:cut + 1].rstrip() + " … Full answer in the app."
    return text, numbered


def map_answer(text: str, options: list[str]) -> str:
    """"2" or "b" -> the second option the user was shown; anything else unchanged."""
    t = (text or "").strip().rstrip(".)")
    if not options or not t:
        return text
    n = None
    if t.isdigit():
        n = int(t)
    elif len(t) == 1 and t.isalpha():
        n = ord(t.lower()) - ord("a") + 1
    if n is not None and 1 <= n <= len(options):
        return options[n - 1]
    return text


def split_at_name(text: str, names: dict[str, str]) -> tuple[str | None, str]:
    """("@scout do x", {"scout": id}) -> (id, "do x"). The name may be a prefix
    of a bot's name ("@sc"); no match leaves the text untouched."""
    m = _AT_NAME.match(text or "") or _AT_ONLY.match(text or "")
    if not m:
        return None, text
    want = m.group(1).strip().lower()
    rest = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""
    hit = names.get(want)
    if hit is None:
        cands = [bid for n, bid in names.items() if n.startswith(want)]
        hit = cands[0] if len(cands) == 1 else None
    if hit is None:
        return None, text
    return hit, rest


class Router:
    def __init__(self, registry):
        self._registry = registry
        self.channels: dict[str, Channel] = {}
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sticky: dict[tuple, str] = {}      # (kind, addr) -> bot id last addressed from there
        self._pending: dict[str, list[str]] = {}  # bot id -> options of the last numbered question sent out
        self._errors: dict[str, str] = {}         # kind -> last router-side error for Settings
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    def add(self, ch: Channel) -> None:
        self.channels[ch.kind] = ch

    def start(self) -> None:
        for ch in self.channels.values():
            try:
                ch.start()
            except Exception as e:  # noqa: BLE001
                self._errors[ch.kind] = str(e)[:300]
        for name, fn in (("channels-poll", self._poll_loop), ("channels-send", self._send_loop)):
            t = threading.Thread(target=fn, daemon=True, name=name)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        for ch in self.channels.values():
            try:
                ch.stop()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ inbound
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            for ch in list(self.channels.values()):
                try:
                    for m in ch.poll():
                        try:
                            self.dispatch(ch, m)
                        except Exception:  # noqa: BLE001 — one bad text must not stall the channel
                            logger.warning("channel %s: dispatch failed", ch.kind, exc_info=True)
                    self._errors.pop(ch.kind, None)
                except Exception as e:  # noqa: BLE001
                    self._errors[ch.kind] = str(e).strip()[:300] or e.__class__.__name__
            self._stop.wait(POLL_S)

    def _names(self, bids: list[str]) -> dict[str, str]:
        from fused_render.bots import store
        out = {}
        for bid in bids:
            try:
                n = (store.read_meta(bid).get("name") or "").strip().lower()
            except Exception:  # noqa: BLE001
                n = ""
            if n:
                out.setdefault(n, bid)
        return out

    def resolve(self, ch: Channel, m: Inbound) -> tuple[str | None, str]:
        """(bot id, text for the bot) for one inbound, or (None, text) when the
        sender owns no bot on this channel."""
        bids = ch.owners().get(m.addr) or []
        if not bids:
            return None, m.text
        key = (ch.kind, m.addr)
        bid, text = split_at_name(m.text, self._names(bids))
        if bid is None:
            bid = self._sticky.get(key)
            if bid not in bids:
                bid = bids[0]
        self._sticky[key] = bid
        return bid, text

    def dispatch(self, ch: Channel, m: Inbound) -> None:
        bid, text = self.resolve(ch, m)
        if bid is None:
            return
        if not text.strip():
            return  # a bare "@scout": the address is now sticky, nothing to run
        with self._lock:
            opts = self._pending.pop(bid, [])
        text = map_answer(text, opts)
        bot = self._registry.get(bid)
        bot.receive(text, via=base.via(ch.kind, m.addr))

    # ----------------------------------------------------------------- outbound
    def on_event(self, bot, ev: dict) -> None:
        """Called by bot.emit() for every event; cheap: enqueue or ignore."""
        if ev.get("role") not in OUT_ROLES or not (ev.get("text") or "").strip():
            return
        if not self.channels:
            return
        self._q.put((bot, dict(ev)))

    def targets(self, ch: Channel, bot, ev: dict) -> list[str]:
        """Who on `ch` gets this event: the sender it answers, or the bot's owners
        on that channel when their forwards include the event's category."""
        v = ev.get("via") or {}
        if v.get("kind") == ch.kind and v.get("addr"):
            return [v["addr"]]
        if category(ev) not in forwards_for(bot.meta, ch.kind):
            return []
        return [addr for addr, bids in ch.owners().items() if bot.id in bids]

    def deliver(self, ch: Channel, bot, ev: dict) -> list[str]:
        """Send one event on one channel; returns the addresses it went to."""
        addrs = self.targets(ch, bot, ev)
        if not addrs:
            return []
        text, numbered = render(ev, ch.caps)
        if ch.identity().get("mode") == "own":
            # The bot speaks through the user's own account: the name in front is
            # the one token that tells its lines from theirs (and addresses it back).
            text = f"@{(bot.meta.get('name') or 'bot').strip()} {text}"
        sent = []
        for addr in addrs:
            for attempt in range(SEND_RETRIES):
                try:
                    ch.send(addr, text, ev)
                    sent.append(addr)
                    self._errors.pop(ch.kind, None)
                    break
                except Exception as e:  # noqa: BLE001
                    self._errors[ch.kind] = f"send failed: {str(e).strip()[:200]}"
                    if attempt + 1 < SEND_RETRIES:
                        self._stop.wait(SEND_RETRY_WAIT_S)
        if sent and numbered:
            with self._lock:
                self._pending[bot.id] = numbered
        return sent

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                break
            bot, ev = item
            for ch in list(self.channels.values()):
                try:
                    self.deliver(ch, bot, ev)
                except Exception:  # noqa: BLE001
                    logger.warning("channel %s: deliver failed", ch.kind, exc_info=True)

    # ------------------------------------------------------------------- status
    def state(self, kind: str) -> dict | None:
        ch = self.channels.get(kind)
        if ch is None:
            return None
        try:
            s = dict(ch.status())
        except Exception as e:  # noqa: BLE001
            s = {"running": False, "error": str(e)[:300]}
        err = self._errors.get(kind)
        if err and not s.get("error"):
            s["error"] = err
            s["running"] = False
        try:
            s["identity"] = ch.identity()
        except Exception:  # noqa: BLE001
            s["identity"] = {"mode": "", "label": ""}
        s["caps"] = ch.caps.__dict__
        return s

    def states(self) -> dict:
        return {k: self.state(k) for k in self.channels}

"""The router: every channel's inbound becomes `bot.receive(text, via)` on
Super Bot, and every outbound-worthy event a bot emits is delivered under one
policy (docs/bots.md §10).

    inbound   poll thread, every POLL_S: channel.poll() -> the channel's door
              (Super Bot, and only from the address set on it; anything else
              is dropped) -> map a numbered answer back to its option, stamp
              the answer to a texted approval card -> bot.receive(text, via)
    outbound  bot.emit() calls on_event(); a queue + one send thread, so a slow
              osascript never blocks a task thread. targets() is the policy:
              reply to the channel the task came from, and nothing else (no
              forwards). An approval card of a phone-started task is texted as
              "Reply 1 Approve · 2 Deny" when Super Bot's "Approve by text"
              switch is on and the card fits in one text; the reply is stamped
              for that one card (`via["answers"]`), which is the only way a
              texted yes or no ever settles a card (channels.base.answers_card).

The router exists only while `registry.start()` ran (the full server): tests
and a lean `fused-render open` have none, and `on_event` is a no-op then.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from fused_render.bots.channels import base
from fused_render.bots.channels.base import OUT_ROLES, Caps, Channel, Inbound

logger = logging.getLogger(__name__)

POLL_S = 3
SEND_RETRIES = 3
SEND_RETRY_WAIT_S = 5


def render(ev: dict, caps: Caps, approvals: bool = True) -> tuple[str, list[str]]:
    """The text a surface gets for one event, and the options it was numbered
    with (empty when the surface shows buttons itself). A phone has only the
    reply box, so choices become "Reply 1 … · 2 …". An approval card is numbered
    Approve / Deny when `approvals` (Super Bot's "Approve by text" switch) and
    the whole card fits in one text; a card the phone cannot read in full, or
    one sent with the switch off, is never numbered and says where to answer.
    The fit is decided before the Reply line is added: a cut drops the options,
    so a number never maps to a choice the phone did not see."""
    text = (ev.get("text") or "").strip()
    if caps.max_len and (ev.get("summary") or "").strip():
        text = str(ev["summary"]).strip()  # D11: the bot wrote the phone-sized version itself
    opts = [str(o).strip() for o in (ev.get("options") or []) if str(o).strip()]
    numbered: list[str] = []
    approval = ev.get("role") == "approval" and not caps.buttons
    if ev.get("offer") and not caps.buttons:
        # An app offer is a yes that starts a build: answered at the Mac, never by a texted number.
        opts = []
        text += "\n\nAnswer it in the app."
    if approval:
        opts = list(base.APPROVAL_OPTIONS) if approvals else []
    suffix = "\n\nReply " + " · ".join(f"{i + 1} {o}" for i, o in enumerate(opts)) if opts and not caps.options else ""
    if caps.max_len and len(text) + len(suffix) > caps.max_len:
        head = text[:caps.max_len]
        cut = max(head.rfind("\n"), head.rfind(". "), head.rfind("! "), head.rfind("? "))
        if cut < caps.max_len // 2:
            cut = caps.max_len
        text = head[:cut + 1].rstrip() + " … Full answer in the app."
        suffix = ""
    elif suffix:
        numbered = opts
    text += suffix
    if approval and not numbered:
        text += "\n\nNeeds your OK at the Mac."
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


class Router:
    def __init__(self, registry):
        self._registry = registry
        self.channels: dict[str, Channel] = {}
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pending: dict[str, tuple] = {}  # bot id -> (seq, options) of the numbered question still awaiting a texted answer
        # bot id -> (seq, options, sent_at) of the approval card texted with its Approve / Deny numbering. Kept apart
        # from _pending, which any user event pops: a Mac-typed instruction ("wait, check the price") leaves the card
        # up, and the phone's later "1" must still map and still count for that card. `sent_at` is the clock the
        # reply's own time is held against: a reply written before the card went out answers an older one, not this.
        # Cleared when the task ends.
        self._texted_card: dict[str, tuple] = {}
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

    def resolve(self, ch: Channel, m: Inbound) -> tuple[str | None, str]:
        """(Super Bot's id, text) when the sender is the address set on Super
        Bot, else (None, text): the message is dropped (the channel's status
        still shows `last_in`)."""
        bid, addr = ch.door()
        if not bid or not addr or m.addr != addr:
            return None, m.text
        return bid, m.text

    def dispatch(self, ch: Channel, m: Inbound) -> None:
        bid, text = self.resolve(ch, m)
        if bid is None or not (text or "").strip():
            return
        bot = self._registry.get(bid)
        with self._lock:
            seq, opts = self._pending.pop(bid, (None, []))
            card = self._texted_card.get(bid)
        waiting = (getattr(bot, "meta", None) or {}).get("waiting_on")
        # Only while that question is still what the bot waits on: answered at the Mac meanwhile, a "2" is just a "2".
        if seq is not None and waiting != seq:
            opts = []
        via = base.via(ch.kind, m.addr)
        if card and waiting == card[0] and (m.ts or 0) >= card[2]:
            # The bot still waits on the card this sender was texted, and the reply was written after the card went
            # out: it is that card's answer and nothing else's (docs §10). A reply older than the card (typed for an
            # earlier card the Mac then answered, delivered late) is left unstamped, so the gate treats it as an
            # instruction and _texted_approval holds a bare yes/no. "1" maps to Approve even after a Mac instruction
            # popped _pending; the words are then read by the gate as the web's are (YES approves, NO denies,
            # anything else is an instruction).
            opts = opts or list(card[1])
            via["answers"] = card[0]
        text = map_answer(text, opts)
        bot.receive(text, via=via)

    # ----------------------------------------------------------------- outbound
    def on_event(self, bot, ev: dict) -> None:
        """Called by bot.emit() for every event; cheap: enqueue or ignore."""
        role = ev.get("role")
        ended = role in ("done", "error") and not ev.get("source")
        if self._pending and (role == "user" or ended):
            # The numbered question was answered (at the Mac or here) or its task ended: numbers mean nothing
            # now. A hand-off or build card from another thread is not the task's end.
            with self._lock:
                self._pending.pop(getattr(bot, "id", None), None)
        if self._texted_card and ended:
            with self._lock:
                self._texted_card.pop(getattr(bot, "id", None), None)
        if ev.get("role") not in OUT_ROLES or not (ev.get("text") or "").strip():
            return
        if ((ev.get("via") or {}).get("kind") or "") == base.HANDOFF_KIND:
            return  # a handed-off task reports to Super Bot, never to a channel (targets() agrees)
        if not self.channels:
            return
        self._q.put((bot, dict(ev)))

    def targets(self, ch: Channel, bot, ev: dict) -> list[str]:
        """Who on `ch` gets this event: the sender of the task it answers, and
        only that (the origin rule). Everything else stays on the page."""
        v = ev.get("via") or {}
        if v.get("kind") == ch.kind and v.get("addr"):
            return [v["addr"]]
        return []

    def deliver(self, ch: Channel, bot, ev: dict) -> list[str]:
        """Send one event on one channel; returns the addresses it went to."""
        addrs = self.targets(ch, bot, ev)
        if not addrs:
            return []
        text, numbered = render(ev, ch.caps, approvals=base.approvals_by_text(getattr(bot, "meta", None)))
        if ch.identity().get("mode") == "own":
            # The bot speaks through the user's own account: the name in front is
            # the one token that tells its lines from theirs.
            text = f"@{(bot.meta.get('name') or 'bot').strip()} {text}"
        sent = []
        for addr in addrs:
            err = ""
            for attempt in range(SEND_RETRIES):
                try:
                    ch.send(addr, text, ev)
                    sent.append(addr)
                    err = ""
                    self._errors.pop(ch.kind, None)
                    break
                except Exception as e:  # noqa: BLE001
                    err = str(e).strip()[:200] or e.__class__.__name__
                    self._errors[ch.kind] = f"send failed: {err}"
                    if attempt + 1 < SEND_RETRIES:
                        self._stop.wait(SEND_RETRY_WAIT_S)
            # D12: one delivery row per channel send, in the same log, joined by `ref`. It carries the
            # exact text that went out (prefix, numbering, summary or cut), or the error when nothing did.
            # Not an OUT_ROLE, so it never triggers a delivery of its own.
            try:
                bot.emit("delivery", text, ref=ev.get("seq"), channel=ch.kind, addr=addr, via=None,
                         **({"error": err} if err else {}))
            except Exception:  # noqa: BLE001
                logger.debug("delivery row not written", exc_info=True)
        if sent and numbered:
            with self._lock:
                self._pending[bot.id] = (ev.get("seq"), numbered)
                if ev.get("role") == "approval":
                    # The one card a texted yes may settle, and the clock a reply must postdate (dispatch).
                    self._texted_card[bot.id] = (ev.get("seq"), numbered, time.time())
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
        try:
            s["super_handle"] = ch.door()[1] or None   # the one address that reaches Super Bot here
        except Exception:  # noqa: BLE001
            s["super_handle"] = None
        s["caps"] = ch.caps.__dict__
        return s

    def states(self) -> dict:
        return {k: self.state(k) for k in self.channels}

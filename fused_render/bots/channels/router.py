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
              A hand-off target's card comes the same way, relayed through Super
              Bot's chat (handoffs._relay_card, docs §11 relay): the record names
              the TARGET, and the reply goes to the target's gate, never into
              Super Bot's inbox (_relay). One live card per door; the next waits.

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
CLOCK_SLACK_S = 2  # the phone's clock may run a little behind the Mac's: a reply this much older than the card still counts


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
    if (approval or (base.relay_of(ev) is not None and not caps.buttons)) and not numbered:
        text += "\n\nNeeds your OK at the Mac."
    return text, numbered


def _yes():
    from fused_render.bots.agent_engine import YES  # lazy: the router imports without the engine's dependencies
    return YES


def _no():
    from fused_render.bots.agent_engine import NO
    return NO


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
        # door bot id -> the one card texted with a numbering a reply may settle: {"bot": the bot that waits on it
        # (Super Bot itself for its own approval card, the hand-off target for a relayed card, docs §11 relay), "seq":
        # that card's seq on THAT bot, "opts", "sent_at", "text"}. Kept apart from _pending, which any user event pops:
        # a Mac-typed instruction ("wait, check the price") leaves the card up, and the phone's later "1" must still
        # map and still count for that card. `sent_at` is the clock the reply's own time is held against: a reply
        # written before the card went out answers an older one, not this. One live card per door: a second one
        # waits in _parked and is texted when this one settles (_settle), so a "1" never has two readings.
        self._texted_card: dict[str, dict] = {}
        self._parked: dict[str, list] = {}  # door bot id -> [(bot, ev)] cards waiting their turn, FIFO
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
        if card and card["bot"] != bid and not opts:
            # A relayed card (docs §11 relay): the reply settles the TARGET's card, and only a reply that reads as a
            # verdict (a number of the card's, a yes, a no). It never enters Super Bot's inbox: the target's own gate
            # reads it as the web's, under the same stamp. Super Bot's own live numbered question, when it has one
            # (`opts`), takes the reply instead: it went out later and is what the phone is looking at.
            if self._relay(ch, bot, card, text, m.ts or 0, via):
                return
        elif card and card["bot"] == bid and waiting == card["seq"] and (m.ts or 0) >= card["sent_at"]:
            # (card["bot"] == bid: a relay's seq is the TARGET's counter and may collide with Super Bot's own.)
            # The bot still waits on the card this sender was texted, and the reply was written after the card went
            # out: it is that card's answer and nothing else's (docs §10). A reply older than the card (typed for an
            # earlier card the Mac then answered, delivered late) is left unstamped, so the gate treats it as an
            # instruction and _texted_approval holds a bare yes/no. "1" maps to Approve even after a Mac instruction
            # popped _pending; the words are then read by the gate as the web's are (YES approves, NO denies,
            # anything else is an instruction).
            opts = opts or list(card["opts"])
            via["answers"] = card["seq"]
        text = map_answer(text, opts)
        bot.receive(text, via=via)

    def _relay(self, ch: Channel, door, card: dict, text: str, ts: float, via: dict) -> bool:
        """A reply while a relayed card is the live texted card. True when it was
        taken (answered the target's card, or held because that card is gone);
        False when the words are not a verdict at all (Super Bot's instruction
        or new task, as ever: free text never redirects)."""
        verdict = map_answer(text, list(card["opts"]))
        if verdict == text and not (_yes().match(text) or _no().match(text)):
            return False
        try:
            target = self._registry.get(card["bot"])
        except Exception:  # noqa: BLE001 — the target is gone (deleted mid-wait)
            target = None
        twait = (getattr(target, "meta", None) or {}).get("waiting_on") if target is not None else None
        who = card.get("name") or "the bot"
        if target is None or twait != card["seq"]:
            # Answered at the Mac already (or the task ended): one text back, nothing starts. Without this an idle
            # Super Bot would start a task named "1".
            door.emit("system", f"Texted \"{text.strip()[:80]}\" for {who}'s card after it was settled; not applied.", via=None)
            door.emit("error", f"That card of {who}'s was answered already (at the Mac, or the task ended).", via=dict(via), source="hold")
            self._settle(door.id, card)
            return True
        if ts < card["sent_at"]:
            # Written before the card went out (typed for an earlier one, delivered late): not this card's answer. The
            # card is still up and still waited on, so the record STAYS: the next "1" must still reach it.
            door.emit("system", f"Texted \"{text.strip()[:80]}\" before {who}'s card went out; not applied.", via=None)
            door.emit("error", f"That reply was sent before {who}'s card reached you. Reply again to answer it.", via=dict(via), source="hold")
            return True
        via["answers"] = card["seq"]
        target.receive(verdict, via=via)
        self._settle(door.id, card)
        # Super Bot's transcript records that the USER answered, as a plain line the model reads as data.
        did = "approved" if _yes().match(verdict) else "denied" if _no().match(verdict) else f"answered \"{verdict.strip()[:80]}\" to"
        # source "handoff" without a `handoff` ref: the page shows it as a quiet hand-off line (not a card), and
        # past_conversation labels it as the user's act.
        door.emit("system", f"You {did} {card.get('name') or 'the bot'}'s card from your phone: {card.get('text') or ''}"[:400],
                  via=None, source="handoff")
        return True

    def _settle(self, door: str, card: dict | None = None) -> None:
        """The live texted card of `door` is answered or gone: forget it, and text
        the next card waiting its turn (deliver re-checks that one is still live)."""
        with self._lock:
            cur = self._texted_card.get(door)
            if card is not None and cur is not card:
                return
            self._texted_card.pop(door, None)
            nxt = self._parked.get(door) or []
            item = nxt.pop(0) if nxt else None
        if item is not None:
            b, ev = item
            self._q.put((b, {**ev, "parked": True}))  # deliver re-checks a parked card is still waited on

    # ----------------------------------------------------------------- outbound
    def on_event(self, bot, ev: dict) -> None:
        """Called by bot.emit() for every event; cheap: enqueue or ignore."""
        role = ev.get("role")
        bid = getattr(bot, "id", None)
        ended = role in ("done", "error") and not ev.get("source")
        if self._pending and (role == "user" or ended):
            # The numbered question was answered (at the Mac or here) or its task ended: numbers mean nothing
            # now. A hand-off or build card from another thread is not the task's end.
            with self._lock:
                self._pending.pop(bid, None)
        if self._texted_card:
            # The live texted card follows the bot that WAITS on it (docs §11 relay): Super Bot's own card goes when
            # its task ends; a relayed card when the target acts again (its wait is over, answered at the Mac or by
            # the relay), ends, or is stopped. Checked before the hand-off early return below: a target's events
            # all carry the hand-off via. Super Bot finishing an unrelated task leaves a relay alone.
            stopped = role == "system" and (ev.get("text") or "").strip() == "Stopped"
            fatal = role == "error" and ev.get("trace")
            done = role == "done" and ev.get("source") != "build"
            with self._lock:
                hits = [(d, c) for d, c in self._texted_card.items() if c["bot"] == bid
                        and ((c["bot"] == d and ended) or (c["bot"] != d and (done or fatal or stopped or role in ("action", "thought"))))]
            for d, c in hits:
                self._settle(d, c)
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
        relay = base.relay_of(ev)
        if numbered and (relay is not None or ev.get("role") == "approval"):
            # A card a reply may settle takes the door's one slot. A card flushed from _parked may have been answered
            # at the Mac while it waited: then it is dropped, the Mac saw it. A FRESH card is never checked: it is
            # queued inside the waiting bot's own emit(), before that bot has written waiting_on (the engines emit
            # first, set_status next), so a check here would drop every card. Slot taken by another card: park it,
            # _settle texts it when that one is answered. The same reply must never have two readings.
            wbot, wseq = (relay["bot"], relay["seq"]) if relay else (bot.id, ev.get("seq"))
            if ev.pop("parked", False):
                try:
                    waiter = bot if wbot == bot.id else self._registry.get(wbot)
                except Exception:  # noqa: BLE001
                    return []
                if (getattr(waiter, "meta", None) or {}).get("waiting_on") != wseq:
                    return []
            with self._lock:
                cur = self._texted_card.get(bot.id)
                if cur is not None and (cur["bot"], cur["seq"]) != (wbot, wseq):
                    self._parked.setdefault(bot.id, []).append((bot, ev))
                    return []
        if ch.identity().get("mode") == "own":
            # The bot speaks through the user's own account: the name in front is
            # the one token that tells its lines from theirs.
            text = f"@{(bot.meta.get('name') or 'bot').strip()} {text}"
        sent = []
        # The clock a texted approval's reply must postdate: the moment the channel ACCEPTED the send (Messages has
        # it; the phone sees it later still), so a reply the phone wrote before this card existed can never be its
        # answer. A failed attempt sets nothing; the retry that succeeds does. Less CLOCK_SLACK_S for a phone clock
        # a little behind the Mac's (two clocks; the slack is the only width of the window, not the send's duration).
        sent_at = 0.0
        for addr in addrs:
            err = ""
            for attempt in range(SEND_RETRIES):
                try:
                    ch.send(addr, text, ev)
                    sent_at = time.time() - CLOCK_SLACK_S
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
                if relay is not None or ev.get("role") == "approval":
                    # The one card a texted reply may settle, and the clock a reply must postdate (dispatch). A relayed
                    # card records the TARGET and its seq: the reply goes to that bot's gate (dispatch → _relay).
                    name = (ev.get("handoff") or {}).get("target_name") if relay else (bot.meta.get("name") or "")
                    self._texted_card[bot.id] = {"bot": relay["bot"] if relay else bot.id, "seq": relay["seq"] if relay else ev.get("seq"),
                                                 "opts": numbered, "sent_at": sent_at, "name": name or "",
                                                 "text": " ".join((ev.get("text") or "").split())[:120]}
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

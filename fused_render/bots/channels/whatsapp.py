"""WhatsApp as a channel — ROUGH POC (docs/bots.md §10).

Linked-device route: the server pairs as one more device on the OWNER'S OWN
WhatsApp account (QR scan, like WhatsApp Web) through `neonize` (Python
binding of whatsmeow, the Go multi-device client). No Meta business account,
no webhook, no second number — and no blessing from WhatsApp's terms either:
this is the unofficial protocol, the owner accepts that.

The shape being tried out here: ONE GROUP PER BOT, named after the bot, with
the owner as its only member. The owner types in the "scout" group, the
message becomes a task for scout; scout's `done` / `question` / `error` are
posted back into that same group. Groups the bot creates itself on first
connect; a group the owner made by hand with the bot's name is picked up by
name. Mapping lives in `<home>/bots/data/whatsapp/groups.json`.

Since the bot speaks as the owner (a linked device), every message in the
group — the owner's and the bot's — is "from me". Telling them apart:
the IDs of messages this process sent are remembered and skipped on the way
back in; everything else from-me in a bot's group is a command.

Enable with FUSED_BOTS_WHATSAPP=1 (+ `pip install neonize`, libmagic on PATH:
`brew install libmagic`). On first start the QR shows in the server's
terminal and is written to `<data>/whatsapp/qr.png` — scan it from WhatsApp >
Linked devices. The session persists in `<data>/whatsapp/session.db`.

Not here yet (on purpose, POC): Settings UI, pairing from the page, media,
per-group forwards, a sidecar binary instead of the in-process Go lib.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import queue
import threading
import time

from fused_render.bots import paths as bpaths
from fused_render.bots.channels.base import Caps, Channel, Inbound

logger = logging.getLogger(__name__)

MAX_TEXT = 1500
CAPS = Caps(options=False, buttons=False, max_len=MAX_TEXT, media=False, can_login=False)
GROUP_SYNC_S = 60
GROUP_SERVER = "g.us"


def data_dir() -> str:
    p = os.path.join(bpaths.data_root(), "whatsapp")
    os.makedirs(p, exist_ok=True)
    return p


def enabled() -> bool:
    return os.environ.get("FUSED_BOTS_WHATSAPP", "").strip() not in ("", "0", "false", "no")


def jid_str(j) -> str:
    """proto JID -> "user@server" (what we key groups by)."""
    return f"{j.User}@{j.Server}"


def text_of(msg) -> str:
    """The plain text of a WhatsApp Message proto, '' for media etc."""
    try:
        if msg.conversation:
            return msg.conversation
        if msg.extendedTextMessage and msg.extendedTextMessage.text:
            return msg.extendedTextMessage.text
    except Exception:  # noqa: BLE001
        pass
    return ""


class WhatsappChannel(Channel):
    kind = "whatsapp"
    caps = CAPS

    def __init__(self):
        self.state = {"running": False, "error": "", "last_in": None, "last_out": None, "groups": 0,
                      "paired": False, "qr_png": "", "me": ""}
        self._q: queue.Queue = queue.Queue()
        self._client = None
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._sent_ids: collections.deque = collections.deque(maxlen=500)
        self._sent_set: set[str] = set()
        self._last_sent: dict[str, float] = {}
        self._groups: dict[str, str] = {}     # bot id -> group jid
        self._by_group: dict[str, str] = {}   # group jid -> bot id
        self._group_sync_ts = 0.0
        self._lock = threading.Lock()
        self._load_groups()

    # ----------------------------------------------------------------- groups
    def _groups_path(self) -> str:
        return os.path.join(data_dir(), "groups.json")

    def _load_groups(self) -> None:
        try:
            with open(self._groups_path()) as f:
                g = json.load(f)
            if isinstance(g, dict):
                self._groups = {str(k): str(v) for k, v in g.items()}
        except (OSError, ValueError):
            self._groups = {}
        self._by_group = {v: k for k, v in self._groups.items()}
        self.state["groups"] = len(self._groups)

    def _save_groups(self) -> None:
        self._by_group = {v: k for k, v in self._groups.items()}
        self.state["groups"] = len(self._groups)
        try:
            tmp = self._groups_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._groups, f, indent=1)
            os.replace(tmp, self._groups_path())
        except OSError:
            logger.debug("whatsapp groups.json not written", exc_info=True)

    def _bot_names(self) -> dict[str, str]:
        """bot id -> name for every bot that is not Super Bot (which refuses non-web via)."""
        from fused_render.bots import store
        out = {}
        for bid in store.list_ids():
            try:
                m = store.read_meta(bid)
            except Exception:  # noqa: BLE001
                continue
            if m.get("kind") == "super":
                continue
            n = (m.get("name") or bid).strip()
            if n:
                out[bid] = n
        return out

    def _sync_groups(self, force: bool = False) -> None:
        """Pair every bot with a group named after it: match a joined group by
        name, else create one with the owner as sole member. Poll thread only."""
        now = time.time()
        if not force and now - self._group_sync_ts < GROUP_SYNC_S:
            return
        self._group_sync_ts = now
        c = self._client
        if c is None or not self._connected.is_set():
            return
        names = self._bot_names()
        if all(bid in self._groups for bid in names) and not force:
            return
        joined = {}
        for g in c.get_joined_groups():
            try:
                joined.setdefault((g.GroupName.Name or "").strip().lower(), jid_str(g.JID))
            except Exception:  # noqa: BLE001
                continue
        changed = False
        for bid, name in names.items():
            if bid in self._groups:
                continue
            gj = joined.get(name.lower())
            if gj is None:
                try:
                    info = c.create_group(name, [])
                    gj = jid_str(info.JID)
                    logger.info("whatsapp: created group %r for bot %s", name, bid)
                except Exception as e:  # noqa: BLE001
                    self.state["error"] = f"create group {name!r}: {str(e)[:200]}"
                    logger.warning("whatsapp: create_group failed", exc_info=True)
                    continue
            self._groups[bid] = gj
            changed = True
        if changed:
            self._save_groups()

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="channels-whatsapp")
        self._thread.start()

    def _run(self) -> None:
        try:
            from neonize.client import NewClient
            from neonize.events import ConnectedEv, DisconnectedEv, LoggedOutEv, MessageEv, PairStatusEv
        except Exception as e:  # noqa: BLE001
            self.state["error"] = f"neonize not importable: {str(e)[:200]}"
            return
        db = os.path.join(data_dir(), "session.db")
        client = NewClient(db)
        self._client = client

        def on_qr(_c, data: bytes) -> None:
            try:
                import segno
                png = os.path.join(data_dir(), "qr.png")
                segno.make_qr(data).save(png, scale=6)
                self.state["qr_png"] = png
                print(f"\n[whatsapp] scan this QR from WhatsApp > Linked devices (also at {png}):", flush=True)
                segno.make_qr(data).terminal(compact=True)
            except Exception:  # noqa: BLE001
                logger.warning("whatsapp: qr render failed", exc_info=True)

        client.event.qr(on_qr)

        @client.event(PairStatusEv)
        def on_pair(_c, ev) -> None:
            self.state["paired"] = True
            self.state["qr_png"] = ""
            logger.info("whatsapp: paired")

        @client.event(ConnectedEv)
        def on_connected(_c, _ev) -> None:
            self._connected.set()
            self.state.update(running=True, error="", paired=True, qr_png="")
            try:
                me = client.me
                self.state["me"] = jid_str(me.JID) if me is not None else ""
            except Exception:  # noqa: BLE001
                pass
            self._group_sync_ts = 0.0  # poll thread syncs groups on its next tick
            logger.info("whatsapp: connected as %s", self.state["me"])

        @client.event(DisconnectedEv)
        def on_disconnected(_c, _ev) -> None:
            self._connected.clear()
            self.state["running"] = False

        @client.event(LoggedOutEv)
        def on_logged_out(_c, _ev) -> None:
            self._connected.clear()
            self.state.update(running=False, paired=False, error="logged out on the phone; delete session.db and restart to pair again")

        @client.event(MessageEv)
        def on_message(_c, ev) -> None:
            try:
                src = ev.Info.MessageSource
                if not src.IsGroup:
                    return
                chat = jid_str(src.Chat)
                if chat not in self._by_group:
                    return
                mid = str(ev.Info.ID or "")
                with self._lock:
                    if mid and mid in self._sent_set:
                        return  # our own send coming back
                text = text_of(ev.Message).strip()
                if not text:
                    return
                self._q.put(Inbound(addr=chat, text=text, service="whatsapp", ts=time.time(), ext_id=mid))
                self.state["last_in"] = time.time()
            except Exception:  # noqa: BLE001
                logger.warning("whatsapp: message handler failed", exc_info=True)

        try:
            client.connect()  # blocks until stop()
        except Exception as e:  # noqa: BLE001
            self.state["running"] = False
            self.state["error"] = str(e)[:300] or e.__class__.__name__
            logger.warning("whatsapp: connect ended", exc_info=True)

    def stop(self) -> None:
        c = self._client
        if c is not None:
            try:
                c.stop()
            except Exception:  # noqa: BLE001
                pass
        self.state["running"] = False

    # ------------------------------------------------------------------- poll
    def owners(self) -> dict[str, list[str]]:
        return {gj: [bid] for bid, gj in self._groups.items()}

    def poll(self) -> list[Inbound]:
        if self._connected.is_set():
            try:
                self._sync_groups()
            except Exception as e:  # noqa: BLE001
                self.state["error"] = f"group sync: {str(e)[:200]}"
                logger.warning("whatsapp: group sync failed", exc_info=True)
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    # ------------------------------------------------------------------- send
    def send(self, addr: str, text: str, event: dict | None = None) -> None:
        c = self._client
        if c is None or not self._connected.is_set():
            raise RuntimeError("whatsapp not connected")
        from neonize.utils.jid import build_jid
        user, _, server = addr.partition("@")
        resp = c.send_message(build_jid(user, server or GROUP_SERVER), text)
        now = time.time()
        with self._lock:
            mid = str(resp.ID or "")
            if mid:
                if len(self._sent_ids) == self._sent_ids.maxlen:
                    self._sent_set.discard(self._sent_ids[0])
                self._sent_ids.append(mid)
                self._sent_set.add(mid)
            self._last_sent[addr] = now
        self.state["last_out"] = now

    def sent_since(self, addr: str, ts: float) -> bool:
        return self._last_sent.get(addr, 0) >= ts

    # --------------------------------------------------------------- identity
    def identity(self) -> dict:
        # A linked device on the owner's own account: the bot's lines are "from me" like
        # the owner's, so the router's `@name` prefix is what tells them apart.
        return {"mode": "own", "label": self.state.get("me") or ""}

    def status(self) -> dict:
        return dict(self.state)

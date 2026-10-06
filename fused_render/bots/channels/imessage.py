"""iMessage as a channel (docs/bots.md §10): texts from a bot's owner handle
become `bot.receive(...)` calls, the bot's replies go back through Messages.app.

The mechanics are the old bridge's (fused_render/bots/imessage.py keeps the
pure helpers: chat.db queries, osascript send, handle parsing): a 3 s
read-only poll of ~/Library/Messages/chat.db, a cursor so nothing is replayed,
an echo window because texting your own number makes every reply come back as
an incoming row, and one flock per Mac so a second process stands down.

What is new here
  - `poll()` returns Inbound rows; the router resolves the bot and calls
    receive(). No inbox files, no scheduler hop.
  - Only rows whose handle service is iMessage count as the owner's commands:
    a forwarded SMS (spoofable sender) never answers an approval.
  - `identity()`: "own" when the owner handle is one of this Mac's own Messages
    accounts (message.account of sent rows), so the router puts `@name` in
    front of every text; "dedicated" when Messages is signed into a separate
    bot Apple ID.

State files (`<home>/bots/data/`): imessage.json (cursor), imessage.lock,
imessage-state.json (this process' view, for a future worker / --status).
"""
from __future__ import annotations

import fcntl
import json
import os
import time

from fused_render.bots import imessage as im
from fused_render.bots.channels.base import Caps, Channel, Inbound

ECHO_WINDOW_S = 10 * 60
IDENTITY_TTL_S = 60
MAX_TEXT = 600   # one phone-sized text; the router shortens and points at the app

CAPS = Caps(options=False, buttons=False, max_len=MAX_TEXT, media=False, can_login=False)


class ImessageChannel(Channel):
    kind = "imessage"
    caps = CAPS

    def __init__(self):
        self.state = {"running": False, "error": "", "last_in": None, "last_out": None, "handles": 0, "holder": "", "echoes": 0}
        self.lock_fh = None
        self.db = None          # the poll thread's connection; never used from another thread
        self._own: set[str] = set()   # this Mac's own Messages handles, refreshed by the poll thread (identity() only reads)
        self._own_label = ""
        self._own_ts = 0.0
        # poll() runs on the router's poll thread; senders (router send thread, a bot's `text`
        # action) run elsewhere. All of them hold imessage.CURSOR_LOCK around the cursor file.

    # ------------------------------------------------------------- lock/lifecycle
    def acquire(self) -> bool:
        lp = im.lock_path()
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        fh = open(lp, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            self.state["holder"] = fh.read().strip()
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()}")
        fh.flush()
        self.lock_fh = fh
        self.state["holder"] = ""
        return True

    def start(self) -> None:
        self.acquire()

    def stop(self) -> None:
        if self.lock_fh:
            try:
                self.lock_fh.seek(0)
                self.lock_fh.truncate()
                self.lock_fh.flush()
                fcntl.flock(self.lock_fh, fcntl.LOCK_UN)
                self.lock_fh.close()
            except OSError:
                pass
            self.lock_fh = None
        if self.db:
            self.db.close()
            self.db = None
        self.state["running"] = False
        self._publish()

    # --------------------------------------------------------------------- owners
    def owners(self) -> dict[str, list[str]]:
        return im.handles_to_bots()

    # ----------------------------------------------------------------------- poll
    def poll(self) -> list[Inbound]:
        if self.lock_fh is None and not self.acquire():
            self.state["running"] = False
            self.state["error"] = f"another bridge is running ({self.state['holder'] or 'standalone'})"
            return []
        try:
            with im.CURSOR_LOCK:
                out = self._tick()
            self.state["running"] = True
            self.state["error"] = ""
            return out
        except Exception as e:  # noqa: BLE001
            self.state["running"] = False
            self.state["error"] = str(e).strip()[:300] or e.__class__.__name__
            if self.db:
                self.db.close()
                self.db = None
            raise
        finally:
            self._publish()

    def _tick(self) -> list[Inbound]:
        owners = self.owners()
        self.state["handles"] = len(owners)
        if not owners:
            return []
        cur = im.load_cursor()
        if self.db is None:
            self.db = im.open_db()
        if cur.get("rowid") is None:  # first run: start from now, never replay history
            cur["rowid"] = self.db.execute("select coalesce(max(ROWID), 0) from message").fetchone()[0]
            im.save_cursor(cur)
        self._refresh_own_handles()
        now = time.time()
        sent = {t: ts for t, ts in (cur.get("sent") or {}).items() if now - ts < ECHO_WINDOW_S}
        cur["sent"] = sent
        out = []
        for rowid, handle, text, service in im.new_messages(self.db, int(cur["rowid"])):
            cur["rowid"] = rowid
            if handle not in owners:
                continue
            if service and service != "iMessage":
                continue  # SMS / RCS rows: the sender is not authenticated
            if text.strip() in sent or self._is_echo(text, sent):
                self.state["echoes"] = self.state.get("echoes", 0) + 1
                continue
            out.append(Inbound(addr=handle, text=text, service=service or "", ts=now, ext_id=str(rowid)))
            self.state["last_in"] = now
        im.save_cursor(cur)
        return out

    @staticmethod
    def _is_echo(text: str, sent: dict) -> bool:
        """A long reply came back split: any sent text that starts with this row is ours."""
        t = text.strip()
        return any(s.startswith(t) or t.startswith(s) for s in sent if len(s) > 40 and len(t) > 40)

    # ----------------------------------------------------------------------- send
    def send(self, addr: str, text: str, event: dict | None = None) -> None:
        im.send_text(addr, text)  # records the echo window itself, under CURSOR_LOCK
        self.state["last_out"] = time.time()

    def sent_since(self, addr: str, ts: float) -> bool:
        """Did anything in this process (a reply, or a bot's `text` action) text `addr` after `ts`?"""
        return im.LAST_SENT.get(im.norm_handle(addr), 0) >= ts

    # ------------------------------------------------------------------- identity
    def _refresh_own_handles(self) -> None:
        """Handles this Mac's Messages is signed in as (the `account` of rows it
        sent). Poll thread only: it is the one that owns `self.db` (sqlite
        connections are thread-bound); identity() just reads the cached set."""
        now = time.time()
        if self.db is None or now - self._own_ts < IDENTITY_TTL_S:
            return
        self._own_ts = now
        try:
            rows = self.db.execute("select distinct account from message where is_from_me = 1 and account is not null "
                                   "and account != '' order by ROWID desc limit 50").fetchall()
        except Exception:  # noqa: BLE001 — identity stays unknown; the status line carries the poll error
            return
        own = set()
        for (acct,) in rows:
            a = str(acct or "")
            if a[:2].upper() in ("P:", "E:"):
                a = a[2:]
            h = im.norm_handle(a)
            if h:
                own.add(h)
        self._own = own
        self._own_label = next(iter(sorted(own)), "")

    def identity(self) -> dict:
        own = self._own
        if not own:
            return {"mode": "own", "label": ""}  # unknown yet: the owner said "own, always"; detection only upgrades
        owners = set(self.owners())
        if owners & own:
            return {"mode": "own", "label": self._own_label}
        return {"mode": "dedicated", "label": self._own_label}

    # --------------------------------------------------------------------- status
    def status(self) -> dict:
        return dict(self.state)

    def _publish(self) -> None:
        try:
            sp = im.state_path()
            tmp = sp + ".tmp"
            with open(tmp, "w") as f:
                json.dump({**self.state, "pid": os.getpid(), "ts": time.time()}, f)
            os.replace(tmp, sp)
        except OSError:
            pass

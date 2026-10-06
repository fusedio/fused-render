#!/usr/bin/env python3
"""iMessage bridge: texts from an allowlisted number become bot tasks, and the
bot's answers go back as texts (port of OpenBot imessage.py; docs/bots.md §1).

Runs as a thread inside the fused-render server (registry.py starts it whenever
any bot has an `imessage` handle set in Settings), or standalone from a Terminal:

    python -m fused_render.bots.imessage            # run the bridge in the foreground (Ctrl+C stops)
    python -m fused_render.bots.imessage --status   # what the bridge can see, and why it can't

Only one bridge runs at a time: whichever process holds <home>/bots/data/imessage.lock.
Start the standalone one when fused-render itself has no Full Disk Access; the
server's thread notices the lock and stands down until it is released.

Inbound  ~/Library/Messages/chat.db (read-only sqlite): new rows from the bot's
         handle, not from me, not in a group chat -> <home>/bots/data/bots/<id>/inbox/
         imessage-<rowid>.txt, which the bot runs like a typed message.
Outbound tail of each bot's events.jsonl: done / question / approval / error
         events after the cursor -> `osascript` tells Messages.app to send.
Cursor   <home>/bots/data/imessage.json {rowid, line: {bot_id: transcript lines handled}, sent: {text: ts}}.
         On the very first run it starts at "now", so nothing old is replayed. `sent`
         drops echoes: texting your own number makes every reply arrive as incoming too.
State    <home>/bots/data/imessage-state.json, rewritten every poll by the lock holder
         so the Settings dialog shows the real status whichever process answers.

Paths resolve on every use (functions below, plus the OpenBot names DATA,
BOTS, CURSOR, LOCK, STATE as lazy module attributes) so FUSED_RENDER_HOME
can be redirected after import.

Needs: Messages signed in on this Mac; Full Disk Access for the process that
reads chat.db (System Settings > Privacy & Security > Full Disk Access); and
Automation consent for Messages the first time a text is sent.
"""
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

from fused_render.bots import paths as _bpaths

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
POLL_S = 3
OUT_ROLES = ("done", "question", "approval", "error")
MAX_TEXT = 3000          # one iMessage; longer replies are split at line breaks
SEND_TIMEOUT_S = 30
ECHO_WINDOW_S = 10 * 60   # an incoming text identical to one we sent this recently is our own echo, not a task

_SEND_SCRIPT = """
on run argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send (item 2 of argv) to participant (item 1 of argv) of svc
  end tell
end run
"""


# -------------------------------------------------------------------- paths ---
def data_dir():
    """Where the cursor, lock and state files live (`<home>/bots/data`)."""
    return _bpaths.imessage_dir()


def bots_dir():
    """`<home>/bots/data/bots`: one folder per bot."""
    return _bpaths.data_dir()


def cursor_path():
    return os.path.join(data_dir(), "imessage.json")


def lock_path():
    return os.path.join(data_dir(), "imessage.lock")


def state_path():
    """The lock holder's state, for every other process to report."""
    return os.path.join(data_dir(), "imessage-state.json")


_LAZY = {"DATA": data_dir, "BOTS": bots_dir, "CURSOR": cursor_path, "LOCK": lock_path, "STATE": state_path}


def __getattr__(name):  # PEP 562: OpenBot's module constants, resolved now
    fn = _LAZY.get(name)
    if fn is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return fn()


# ------------------------------------------------------------------ helpers ---
def norm_handle(h):
    """'+1 (555) 123-4567' -> '+15551234567'; emails lower-cased."""
    h = (h or "").strip()
    if "@" in h:
        return h.lower()
    d = re.sub(r"[^\d+]", "", h)
    if d and not d.startswith("+"):
        d = "+" + (d if len(d) > 10 else "1" + d)
    return d


_HANDLE_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+|\+?\(?\d[\d\s().-]{6,}\d")
_BIDI_RE = re.compile("[‎‏‪-‮⁦-⁩]")


def parse_contacts(text, owner=""):
    """Settings' "Contacts the bot may text" (one per line or comma-separated,
    e.g. "Ali +1 555 123 4567" or "mom@icloud.com") -> [(label, handle)].
    `owner` is the bot's own allowlisted sender, always included as "the user"."""
    out, seen = [], set()
    if norm_handle(owner):
        out.append(("the user", norm_handle(owner)))
        seen.add(norm_handle(owner))
    for raw in re.split(r"[\n,;]+", text or ""):
        raw = _BIDI_RE.sub("", raw).strip()  # numbers pasted from Contacts carry invisible bidi isolates
        m = _HANDLE_RE.search(raw)
        if not m:
            continue
        h = norm_handle(m.group(0))
        label = (raw[:m.start()] + raw[m.end():]).strip(" :<>()-") or h
        if h and h not in seen:
            out.append((label, h))
            seen.add(h)
    return out


def resolve_contact(who, contacts):
    """(label, handle) for a name or handle the model gave, or None if it is not allowlisted."""
    w = (who or "").strip()
    wl, wh = w.lower(), norm_handle(w)
    for label, h in contacts:
        if wl == label.lower() or (wh and wh == h):
            return label, h
    for label, h in contacts:  # "Ali" matches "Ali Rahimi"
        if wl and wl in label.lower():
            return label, h
    return None


def bots_with_handles():
    """{normalized handle: bot id} for every bot with an iMessage handle set."""
    out = {}
    bots = bots_dir()
    try:
        ids = sorted(os.listdir(bots))
    except OSError:
        return out
    for bid in ids:
        try:
            with open(os.path.join(bots, bid, "bot.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        h = norm_handle(m.get("imessage"))
        if h and h not in out:
            out[h] = bid
    return out


def decode_attributed_body(blob):
    """The text of an NSAttributedString typedstream (chat.db's attributedBody).
    Newer macOS leaves message.text NULL and keeps the body here. Best effort:
    the string sits right after the NSString class marker as a length-prefixed
    UTF-8 run."""
    if not blob:
        return ""
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    j = i + len(b"NSString")
    # skip the class-ref tail: '\x01\x94\x84\x01+' then the length
    k = blob.find(b"+", j)
    if k < 0 or k - j > 12:
        return ""
    k += 1
    n = blob[k]
    if n == 0x81:            # 2-byte little-endian length
        n = int.from_bytes(blob[k + 1:k + 3], "little")
        k += 3
    elif n == 0x82:          # 4-byte
        n = int.from_bytes(blob[k + 1:k + 5], "little")
        k += 5
    else:
        k += 1
    return blob[k:k + n].decode("utf-8", "replace")


def load_cursor():
    try:
        with open(cursor_path()) as f:
            c = json.load(f)
            c.pop("seq", None)  # the first version's cursor; "line" replaced it (see events_after)
            c.setdefault("line", {})
            return c
    except (OSError, ValueError):
        return {"rowid": None, "line": {}}


def save_cursor(c):
    p = cursor_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f)
    os.replace(tmp, p)


def open_db():
    if not os.path.exists(CHAT_DB):
        raise RuntimeError("Messages database not found; is Messages signed in on this Mac?")
    try:
        c = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
        c.execute("select 1 from message limit 1")
        return c
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "unable to open" in msg or "authorization" in msg or "not a database" in msg:
            raise RuntimeError("no Full Disk Access to Messages: grant it to fused-render in System Settings > "
                               "Privacy & Security, or run `python -m fused_render.bots.imessage` from a "
                               "Terminal that has it") from e
        raise


def new_messages(db, after_rowid):
    """[(rowid, handle, text)] for 1:1 texts from others after `after_rowid`."""
    rows = db.execute(
        """select m.ROWID, h.id, m.text, m.attributedBody, c.chat_identifier
           from message m
           left join handle h on h.ROWID = m.handle_id
           left join chat_message_join j on j.message_id = m.ROWID
           left join chat c on c.ROWID = j.chat_id
           where m.ROWID > ? and m.is_from_me = 0 order by m.ROWID""", (after_rowid,)).fetchall()
    out, seen = [], set()
    for rowid, handle, text, body, chat_id in rows:
        if rowid in seen:
            continue
        seen.add(rowid)
        if (chat_id or "").startswith("chat"):
            continue  # group chat
        t = (text or "").strip() or decode_attributed_body(body).strip()
        if t and t != "￼":  # U+FFFC = attachment-only message
            out.append((rowid, norm_handle(handle), t))
    return out


_APPLE_EPOCH = 978307200  # chat.db dates: ns since 2001-01-01


def recent_texts(handle, limit=20, after_rowid=0):
    """The 1:1 thread with `handle`, oldest first: [{rowid, ts, me, text}].
    `after_rowid` > 0 returns only newer rows (used to wait for a reply)."""
    db = open_db()
    try:
        rows = db.execute(
            """select m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody
               from message m
               join chat_message_join j on j.message_id = m.ROWID
               join chat c on c.ROWID = j.chat_id
               where c.chat_identifier = ? and m.ROWID > ?
               order by m.ROWID desc limit ?""", (handle, after_rowid, limit)).fetchall()
    finally:
        db.close()
    out = []
    for rowid, date, me, text, body in reversed(rows):
        t = (text or "").strip() or decode_attributed_body(body).strip()
        if not t or t == "￼":
            continue
        ts = (date / 1e9 if date and date > 1e12 else float(date or 0)) + _APPLE_EPOCH
        out.append({"rowid": rowid, "ts": ts, "me": bool(me), "text": t})
    return out


def format_texts(label, rows):
    if not rows:
        return f"no texts with {label} yet"
    lines = [f"[{time.strftime('%b %d %H:%M', time.localtime(r['ts']))}] {'me' if r['me'] else label}: {r['text'][:300]}" for r in rows]
    return f"TEXTS with {label} (oldest first):\n" + "\n".join(lines)


def send_text(handle, text):
    for chunk in chunks(text):
        r = subprocess.run(["osascript", "-"] + [handle, chunk], input=_SEND_SCRIPT, capture_output=True,
                           text=True, timeout=SEND_TIMEOUT_S, close_fds=False)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["osascript failed"]
            raise RuntimeError(err[0])


def chunks(text):
    text = text.strip()
    while len(text) > MAX_TEXT:
        cut = text.rfind("\n", 0, MAX_TEXT)
        if cut < MAX_TEXT // 2:
            cut = MAX_TEXT
        yield text[:cut].rstrip()
        text = text[cut:].lstrip()
    if text:
        yield text


def outbound_text(ev):
    """The text to send for one event: the message plus, when it waits on the user, how
    to answer it (the page shows buttons; a phone only has the reply box)."""
    text = (ev.get("text") or "").strip()
    opts = [str(o).strip() for o in (ev.get("options") or []) if str(o).strip()]
    if opts:
        text += "\n\nReply with one of: " + " / ".join(opts)
    elif ev.get("role") == "approval":
        text += "\n\nReply yes or no."
    return text


def events_after(bid, line_no):
    """Outbound-worthy events after line `line_no` of the bot's transcript, and
    the new line count. Cursor by line, not `seq`: several writers append to the
    same file with their own counters, so seq numbers interleave (…40, 90, 41…)."""
    path = os.path.join(bots_dir(), bid, "events.jsonl")
    out, n = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if n <= line_no:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("role") in OUT_ROLES and (ev.get("text") or "").strip():
                    out.append((n, ev))
    except OSError:
        pass
    return out, max(n, line_no)


def drop_task(bid, rowid, text):
    inbox = os.path.join(bots_dir(), bid, "inbox")
    os.makedirs(inbox, exist_ok=True)
    name = f"imessage-{rowid}"
    with open(os.path.join(inbox, name + ".tmp"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(os.path.join(inbox, name + ".tmp"), os.path.join(inbox, name + ".txt"))


# ------------------------------------------------------------------- bridge ---
class Bridge:
    """One poll loop. `state` is what the page shows in Settings."""

    def __init__(self):
        self.state = {"running": False, "error": "", "last_in": None, "last_out": None, "handles": 0, "holder": ""}
        self.lock_fh = None
        self.db = None

    def acquire(self):
        lp = lock_path()
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

    def release(self):
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

    def tick(self):
        handles = bots_with_handles()
        self.state["handles"] = len(handles)
        if not handles:
            return
        cur = load_cursor()
        if self.db is None:
            self.db = open_db()
        if cur.get("rowid") is None:  # first run: start from now, never replay history
            cur["rowid"] = self.db.execute("select coalesce(max(ROWID), 0) from message").fetchone()[0]
            save_cursor(cur)
        # Texts we sent recently, to drop their echoes: when the allowlisted number is this
        # Mac's own iMessage account, every reply also lands in chat.db as an incoming row.
        now = time.time()
        sent = {t: ts for t, ts in (cur.get("sent") or {}).items() if now - ts < ECHO_WINDOW_S}
        cur["sent"] = sent
        # inbound
        for rowid, handle, text in new_messages(self.db, int(cur["rowid"])):
            bid = handles.get(handle)
            if bid and text.strip() in sent:
                self.state["echoes"] = self.state.get("echoes", 0) + 1
            elif bid:
                drop_task(bid, rowid, text)
                self.state["last_in"] = time.time()
            cur["rowid"] = rowid
        # outbound
        for handle, bid in handles.items():
            evs, last = events_after(bid, int(cur["line"].get(bid) or 0))
            if bid not in cur["line"]:  # handle just set (or cursor upgraded): don't replay that bot's past
                cur["line"][bid] = last
                continue
            for n, ev in evs:
                text = outbound_text(ev)
                if text in sent and ev.get("role") == "error":
                    cur["line"][bid] = n
                    continue  # the same error again within the window: one text is enough
                send_text(handle, text)
                sent[text] = time.time()
                self.state["last_out"] = time.time()
                cur["line"][bid] = n
            cur["line"][bid] = last
        save_cursor(cur)

    def publish(self):
        """Write state for the processes that don't hold the lock (see current_state)."""
        try:
            sp = state_path()
            tmp = sp + ".tmp"
            with open(tmp, "w") as f:
                json.dump({**self.state, "pid": os.getpid(), "ts": time.time()}, f)
            os.replace(tmp, sp)
        except OSError:
            pass

    def run(self, stop):
        """Loop until `stop` is set. Retries on error; the error text is shown in Settings."""
        while not stop.is_set():
            if self.lock_fh is None and not self.acquire():  # flock is per open file: re-opening would block on our own lock
                self.state["running"] = False
                self.state["error"] = f"another bridge is running ({self.state['holder'] or 'standalone'})"
                stop.wait(POLL_S * 3)
                continue
            try:
                self.tick()
                self.state["running"] = True
                self.state["error"] = ""
            except Exception as e:  # noqa: BLE001
                self.state["running"] = False
                self.state["error"] = str(e).strip()[:300] or e.__class__.__name__
                if self.db:
                    self.db.close()
                    self.db = None
            self.publish()
            stop.wait(POLL_S)
        self.release()


def current_state(local):
    """What Settings shows: the lock holder's published state when it is fresh
    (another process may run the loop), else `local`."""
    if local.get("holder"):
        try:
            with open(state_path()) as f:
                s = json.load(f)
            if time.time() - float(s.get("ts") or 0) < POLL_S * 5:
                return s
        except (OSError, ValueError):
            pass
    return dict(local)


def start_thread():
    """Used by registry.py: returns (Bridge, stop Event)."""
    b, stop = Bridge(), threading.Event()
    threading.Thread(target=b.run, args=(stop,), daemon=True, name="imessage").start()
    return b, stop


# ---------------------------------------------------------------------- cli ---
def main(argv):
    if argv[:1] == ["--status"]:
        handles = bots_with_handles()
        print(f"bots with a handle: {', '.join(f'{h} -> {b}' for h, b in handles.items()) or 'none'}")
        try:
            db = open_db()
            print(f"chat.db: readable, {db.execute('select count(*) from message').fetchone()[0]} messages")
        except Exception as e:  # noqa: BLE001
            print(f"chat.db: {e}")
        print(f"cursor: {load_cursor()}")
        return 0
    b, stop = Bridge(), threading.Event()
    print("bridge running; Ctrl+C stops")
    t = threading.Thread(target=b.run, args=(stop,), daemon=True)
    try:
        last = None
        t.start()
        while t.is_alive():
            s = json.dumps(b.state, sort_keys=True)
            if s != last:
                print(s, flush=True)
                last = s
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        t.join(5)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

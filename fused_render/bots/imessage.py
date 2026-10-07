#!/usr/bin/env python3
"""iMessage helpers: chat.db queries, the osascript send, handle and contact
parsing (docs/bots.md §10). The poll loop that turns texts into tasks is the
channel in fused_render/bots/channels/imessage.py; the delivery policy is the
router. This module has no threads.

    python -m fused_render.bots.imessage --status   # what this Mac can see, and why it can't

Inbound  ~/Library/Messages/chat.db (read-only sqlite): 1:1 rows not from me,
         with the handle's service, so the channel can keep SMS out.
Outbound `osascript` tells Messages.app to send to a participant of the
         iMessage account.
Cursor   <home>/bots/data/imessage.json {rowid, sent: {text: ts}}; the lock
         and state files sit beside it (paths.imessage_dir).

Needs: Messages signed in on this Mac; Full Disk Access for the process that
reads chat.db (System Settings > Privacy & Security > Full Disk Access); and
Automation consent for Messages the first time a text is sent.
"""
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
MAX_TEXT = 3000          # one iMessage; longer texts are split at line breaks
SEND_TIMEOUT_S = 30

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


def super_meta():
    """(Super Bot's id, its bot.json dict) or (None, {}), read from disk on every call (no Bot objects)."""
    bots = bots_dir()
    try:
        ids = sorted(os.listdir(bots))
    except OSError:
        return None, {}
    for bid in ids:
        try:
            with open(os.path.join(bots, bid, "bot.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("kind") == "super":
            return bid, m
    return None, {}


def super_switch(m=None):
    """Whether Super Bot's phone switch (Settings > Phone, `imessage_enabled`) is on.
    A bot.json written before the switch existed has no key: then a handle means on
    (the same rule Bot.__init__ stamps in)."""
    if m is None:
        _, m = super_meta()
    if not m:
        return False
    if "imessage_enabled" in m:
        return bool(m.get("imessage_enabled"))
    return bool(norm_handle(m.get("imessage")))


def super_door():
    """(Super Bot's id, its normalized iMessage handle) or (None, ""): the one bot
    a text reaches, and the one sender it takes texts from (docs §10). Other
    bots' `imessage` keys are ignored. With the phone switch off the handle is
    "" whatever is stored: the number is remembered for the next "on", but no
    text reaches or leaves Super Bot meanwhile."""
    bid, m = super_meta()
    if bid is None or not super_switch(m):
        return bid, ""
    return bid, norm_handle(m.get("imessage"))


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
            c.pop("seq", None)   # the first version's cursor
            c.pop("line", None)  # the second's per-bot transcript cursor; the router delivers live now
            c.setdefault("sent", {})
            return c
    except (OSError, ValueError):
        return {"rowid": None, "sent": {}}


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
    """[(rowid, handle, text, service)] for 1:1 texts from others after `after_rowid`.
    `service` is the handle's ("iMessage", "SMS", "RCS"); the channel trusts only iMessage."""
    rows = db.execute(
        """select m.ROWID, h.id, m.text, m.attributedBody, c.chat_identifier, h.service
           from message m
           left join handle h on h.ROWID = m.handle_id
           left join chat_message_join j on j.message_id = m.ROWID
           left join chat c on c.ROWID = j.chat_id
           where m.ROWID > ? and m.is_from_me = 0 order by m.ROWID""", (after_rowid,)).fetchall()
    out, seen = [], set()
    for rowid, handle, text, body, chat_id, service in rows:
        if rowid in seen:
            continue
        seen.add(rowid)
        if (chat_id or "").startswith("chat"):
            continue  # group chat
        t = (text or "").strip() or decode_attributed_body(body).strip()
        if t and t != "￼":  # U+FFFC = attachment-only message
            out.append((rowid, norm_handle(handle), t, service or ""))
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


# The cursor file is shared by the channel's poll (router poll thread) and every
# sender (router send thread, a bot's `text` action on its task thread): one lock.
CURSOR_LOCK = threading.RLock()


def send_text(handle, text):
    """Send one text (split at MAX_TEXT). Every chunk is first recorded in the
    cursor's `sent` window, so its echo (texting your own number makes each
    reply come back as an incoming row) is never read as a command — whoever
    sends: the channel's replies or a bot's `text` action."""
    now = time.time()
    with CURSOR_LOCK:
        cur = load_cursor()
        sent = cur.setdefault("sent", {})
        for chunk in chunks(text):
            sent[chunk] = now
        save_cursor(cur)
    for chunk in chunks(text):
        r = subprocess.run(["osascript", "-"] + [handle, chunk], input=_SEND_SCRIPT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=SEND_TIMEOUT_S, close_fds=False)
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


# ---------------------------------------------------------------------- cli ---
def main(argv):
    if argv[:1] != ["--status"]:
        print("The iMessage bridge runs inside the fused-render server (bots/channels/imessage.py).\n"
              "  python -m fused_render.bots.imessage --status   # what this Mac can see")
        return 2
    sid, handle = super_door()
    print(f"Super Bot: {sid or 'none'}; texts it from: {handle or 'no handle set'}")
    try:
        db = open_db()
        print(f"chat.db: readable, {db.execute('select count(*) from message').fetchone()[0]} messages")
        own = [r[0] for r in db.execute("select distinct account from message where is_from_me = 1 and account != '' "
                                        "order by ROWID desc limit 10")]
        print(f"this Mac sends as: {', '.join(own) or 'unknown'}")
    except Exception as e:  # noqa: BLE001
        print(f"chat.db: {e}")
    print(f"cursor: {load_cursor()}")
    try:
        with open(state_path()) as f:
            print(f"bridge state: {f.read().strip()}")
    except OSError:
        print("bridge state: none written yet (server not running, or Super Bot has no handle)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

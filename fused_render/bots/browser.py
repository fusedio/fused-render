"""Headless Chrome driven over the DevTools protocol (stdlib only).

Two classes. A `BrowserProcess` owns one Chrome process: its profile
directory, debugging port, the lock every action takes, encryption at rest,
start/stop. Chrome is always headless and is launched once per profile; a
take-over never relaunches it (the user drives the same tab from the page's
live view, which forwards input over CDP), so the process, port, target ids
and screencast sessions survive every hand-off. A `Browser` is one bot's view
of a process: the tabs that bot opened, its screenshots, its downloads and
every page action. Several bots may share one process (one set of logins,
docs/bots.md §1, §5); each then drives its own tabs, opened in their own
windows so headless Chrome composites every bot's foreground tab. A bot on a
process of its own drives every tab in it, exactly as before the split.
Bots never share cookies with the user's real Chrome. Port of OpenBot's
`browser.py`, plus an accessibility-tree snapshot.

    proc = BrowserProcess(data_dir=browsers/<bid>, cache_dir=cache/browsers/<bid>)
    b = Browser(bots.paths.bot_dir(id), bots.paths.bot_cache_dir(id), proc=proc)
    b.goto("https://example.com")
    b.observe()      -> {url, title, elements[], text, tabs, downloads}
    b.click(ref="sb3"); b.type("hi", ref="sb5", submit=True)
    b.screenshot()   -> absolute path of shot.png (the page fetches /api/bots/<id>/shot)
    b.screenshot_jpeg() -> JPEG bytes of the current frame (the agent engine's `screenshot` tool)

Elements carry `ref` (a `data-sb-ref="sb<n>"` stamp on the DOM node) and,
when the accessibility tree found them, `backend` (the node's
backendDOMNodeId). Every element method takes both: the stamped ref is tried
first (plain JS, exactly as OpenBot), then the backend node (reaches closed
shadow roots and same-process iframes, where `document.querySelector` cannot),
then OpenBot's text / coordinate guesses.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import urllib.request

from fused_render.bots import paths as bpaths

log = logging.getLogger(__name__)

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
VIEWPORT = (1280, 800)
# Headless pages render at this device scale so the live view is crisp on a
# Retina display. The window is VIEWPORT; screenshots for the agent are scaled
# back to VIEWPORT width, aspect kept (see _shoot); the live view streams at full scale.
LIVE_SCALE = 1.5
STUCK_POPUP_URL = "https://accounts.google.com/gsi/select"
RECOVER_TIMEOUT_S = 5
INTERACT_TIMEOUT_S = 5
# Longest a navigation waits for the page to settle before reporting where it is. Only reached when no settle
# signal comes (a page that never finishes loading). NAV_SETTLE_S: back / forward / reload, and every navigation
# the user asks for from the live view (the stream shows the page meanwhile). GOTO_SETTLE_S: the bot's own goto,
# which reads the page right after. ACTION_GRACE_S: how long a click / Enter waits for a navigation to begin
# before deciding it only ran a script.
NAV_SETTLE_S = 5
GOTO_SETTLE_S = 15
ACTION_GRACE_S = 0.5
# A reply the renderer withholds behind a JS dialog is abandoned this soon, so the dialog gets settled instead.
DIALOG_GRACE_S = 1
# The Page events wait_loaded reads. Nothing else a call() drains is kept (a heavy page emits hundreds).
_NAV_EVENTS = frozenset({"Page.loadEventFired", "Page.frameStartedNavigating", "Page.frameStartedLoading",
                         "Page.frameStoppedLoading", "Page.navigatedWithinDocument", "Page.windowOpen"})
# Where the fused-render server listens when neither FUSED_RENDER_ORIGIN nor
# server.json says: the bare `fused-render` port (`_branch.branch_port()`,
# 1777 on the baseline, a per-branch port in a dev worktree). FusedBot's was 2777.
def _default_origin() -> str:
    from fused_render._branch import branch_port

    return f"http://127.0.0.1:{branch_port()}"
# Elements an observation lists at most (tools.FULL_ELEMENTS).
MAX_ELEMENTS = 160
# AX candidates resolved per frame at most: bounds the snapshot's cost on
# pages with thousands of links (the DOM scan still covers the viewport).
MAX_AX_CANDIDATES = 2000
# US-layout key table for Input.dispatchKeyEvent (Puppeteer's USKeyboardLayout): code -> (Windows virtual-key
# code, unshifted char, shifted char). The VK must come from this table, never from ord(char): Blink maps the VK to
# an editing command BEFORE it reads `text`: ord(".") is 46 = VK_DELETE (deletes forward instead of typing),
# ord("'") is 39 = VK_RIGHT, ord("-") 45 = VK_INSERT. Verified against Chrome 155.
_KEYS = {f"Digit{i}": (48 + i, str(i), s) for i, s in enumerate(")!@#$%^&*(")}
_KEYS.update({f"Key{c}": (ord(c), c.lower(), c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
_KEYS.update({"Minus": (189, "-", "_"), "Equal": (187, "=", "+"), "BracketLeft": (219, "[", "{"), "BracketRight": (221, "]", "}"),
              "Backslash": (220, "\\", "|"), "Semicolon": (186, ";", ":"), "Quote": (222, "'", '"'), "Backquote": (192, "`", "~"),
              "Comma": (188, ",", "<"), "Period": (190, ".", ">"), "Slash": (191, "/", "?"), "Space": (32, " ", " ")})
_CHAR_CODE = {}
for _c, (_vk, _a, _b) in _KEYS.items():
    _CHAR_CODE.setdefault(_a, _c)
    _CHAR_CODE.setdefault(_b, _c)
# Non-printable keys: Windows virtual-key codes.
_NAMED_VK = {"Enter": 13, "Tab": 9, "Backspace": 8, "Delete": 46, "Escape": 27, "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39,
             "ArrowDown": 40, "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34, "Insert": 45, "Shift": 16, "Control": 17,
             "Alt": 18, "Meta": 91, "CapsLock": 20, **{f"F{n}": 111 + n for n in range(1, 13)}}
# macOS editing commands (Playwright's macEditingCommands). CDP key events bypass Cocoa key bindings, so without
# `commands` Cmd+A/C/V/X/Z and Cmd/Option+arrow do nothing in a Chrome on macOS.
_CMD_LETTER = {"a": "SelectAll", "c": "Copy", "x": "Cut", "v": "Paste", "z": "Undo"}
_CMD_ARROW = {"ArrowLeft": "MoveToBeginningOfLine", "ArrowRight": "MoveToEndOfLine",
              "ArrowUp": "MoveToBeginningOfDocument", "ArrowDown": "MoveToEndOfDocument"}
_WORD_ARROW = {"ArrowLeft": "MoveWordLeft", "ArrowRight": "MoveWordRight",
               "ArrowUp": "MoveToBeginningOfParagraph", "ArrowDown": "MoveToEndOfParagraph"}
# URL schemes `goto` passes through as-is (OpenBot prefixed https:// to any
# url without "://", which broke data:/about: urls).
_PASS_SCHEMES = ("about:", "data:", "file:", "blob:", "chrome:", "javascript:")


def find_chrome():
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    raise RuntimeError("No Chrome/Chromium found in /Applications")


def write_json_atomic(path, obj):
    """Write to a temp file beside `path` and rename it into place. The temp name
    carries pid + thread id: a shared "<path>.tmp" let two writers (a bot's task
    thread and the poll thread both calling save()) race, and the loser's
    os.replace failed with FileNotFoundError after the winner had renamed it."""
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        # On Windows, replacing a destination another thread is renaming onto
        # at the same instant can raise a transient PermissionError (a sharing
        # violation) even though the two never touch each other's own temp
        # file — POSIX rename() has no such restriction. Retried briefly
        # rather than failing a write that only lost a benign race.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.005)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def page_origin():
    """Origin the bots page is served from. The live view opens a DevTools
    WebSocket to Chrome straight from that page, and Chrome refuses a handshake
    carrying a browser Origin unless launched with that origin allowed
    (`--remote-allow-origins`), so a wrong value silently breaks the live view.
    FUSED_RENDER_ORIGIN (exported by the server before it serves), else
    `<home>/server.json`, else the default port."""
    try:
        return bpaths.server_origin_quiet() or _default_origin()
    except Exception:  # noqa: BLE001 — an unreadable server.json is "unknown"
        return _default_origin()


def _http(port, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode() or "null")


def key_events(key, code="", mods=0):
    """One key stroke as Input.dispatchKeyEvent params: `(down, up)`, or
    `("insert", text)` for a printable character the US table does not know
    (é, 日, emoji, a non-US layout): those go through Input.insertText, where no
    virtual key has to be guessed. CDP modifiers: Alt=1 Ctrl=2 Meta=4 Shift=8.
    Same table and rules as the live view's `keyAction` (frontend lib/live.ts). Never sets
    `nativeVirtualKeyCode`: on macOS that flips the tab hidden and the screencast stops."""
    shift, ctrl, alt, meta = bool(mods & 8), bool(mods & 2), bool(mods & 1), bool(mods & 4)
    prim = ctrl or meta  # Ctrl counts like Cmd: the target Chrome is macOS whatever keyboard the caller has
    sel = "AndModifySelection" if shift else ""

    def mk(vk, text=None, commands=()):
        base = {"key": key, "code": code, "modifiers": mods}
        if vk:
            base["windowsVirtualKeyCode"] = vk
        down = dict(base, type="keyDown" if text else "rawKeyDown")
        if text:
            down["text"] = down["unmodifiedText"] = text
        if commands:
            down["commands"] = list(commands)
        return down, dict(base, type="keyUp")

    if len(key) > 1 and key.isascii():  # a named key (Enter, ArrowLeft, F5); a non-ASCII string is text to insert
        cmds = ()
        if key in _CMD_ARROW and prim:
            cmds = (_CMD_ARROW[key] + sel,)
        elif key in _WORD_ARROW and alt:
            cmds = (_WORD_ARROW[key] + sel,)
        elif key == "Backspace" and prim:
            cmds = ("DeleteToBeginningOfLine",)
        elif key == "Backspace" and alt:
            cmds = ("DeleteWordBackward",)
        elif key == "Delete" and alt:
            cmds = ("DeleteWordForward",)
        elif key in ("Home", "End"):
            cmds = (("MoveToBeginningOfLine" if key == "Home" else "MoveToEndOfLine") + sel,)
        return mk(_NAMED_VK.get(key, 0), "\r" if key == "Enter" else None, cmds)
    c = code if code in _KEYS else _CHAR_CODE.get(key, "")
    if (ctrl or meta) and not (ctrl and alt):  # a chord: no text, a command where macOS needs one
        cmd = _CMD_LETTER.get(key.lower())
        cmds = (("Redo" if key.lower() == "z" and shift else cmd),) if cmd and not alt else ()
        return mk(_KEYS[c][0] if c else 0, None, cmds)
    if c and key in _KEYS[c][1:]:
        return mk(_KEYS[c][0], key)
    return "insert", key


def _sips(src, dst, *args):
    """Resize/convert an image with macOS's built-in sips. False on failure."""
    try:
        cmd = ["sips", *args, src] + ([] if dst == src else ["--out", dst])
        return subprocess.run(cmd, capture_output=True, timeout=10, close_fds=False).returncode == 0
    except Exception:
        return False


def _sips_width(path):
    """Pixel width of an image (sips -g), or 0 when unknown."""
    try:
        out = subprocess.run(["sips", "-g", "pixelWidth", path], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, close_fds=False).stdout
        m = re.search(r"pixelWidth:\s*(\d+)", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def _clean_user_agent(chrome):
    ver = "0.0.0.0"
    try:
        out = subprocess.run([chrome, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5, close_fds=False).stdout
        m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
        if m:
            ver = m.group(1) + ".0.0.0"
    except Exception:
        pass
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver} Safari/537.36")


def _with_scheme(url):
    """OpenBot's rule (https:// when the url has no "://"), except for the
    schemes that never carry "://" (data:, about:, …)."""
    if "://" not in url and not url.lower().startswith(_PASS_SCHEMES):
        return "https://" + url
    return url


# ------------------------------------------------------ minimal websocket ---
class WS:
    def __init__(self, url, timeout=30):
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake failed")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket upgrade refused")
        self.timeout = timeout
        self._id = 0
        self.dialog = None       # Page.javascriptDialogOpening params while one is open
        self.dialog_seen = None  # the last one that opened on this connection, kept past its close (the action's report)
        self.dom_enabled = False
        # Navigation events read while a call() waited for its reply. A bfcache restore (back/forward) commits and
        # stops loading before Chrome even acks navigateToHistoryEntry, so wait_loaded must see what call() drained.
        self.events = []
        # The top frame's id (= the page target's id), set by whoever connects. "" = unknown: frame events are then
        # not trusted and only the load event settles a wait.
        self.main_frame = ""

    def _note(self, msg):
        method = msg.get("method")
        if method == "Page.javascriptDialogOpening":
            self.dialog = self.dialog_seen = msg.get("params") or {}
        elif method == "Page.javascriptDialogClosed":
            self.dialog = None
        if method in _NAV_EVENTS:
            self.events.append(msg)
            del self.events[:-200]

    def _recv_exact(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise RuntimeError("websocket closed")
            out += chunk
        return out

    def send(self, text):
        data = text.encode()
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(head) + masked)

    def recv(self):
        """One text message. Reassembles fragmented messages (FIN unset +
        continuation frames) so a large reply (a full AX tree) never comes
        back cut."""
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin = b1 & 0x80
            op = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(n)
            if op == 0x8:
                raise RuntimeError("websocket closed by peer")
            if op in (0x9, 0xA):
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode()

    def notify(self, method, **params):
        """Send a command without waiting for its reply (replies are skipped by
        later call()s). Used for pointer events, whose acks Chrome can withhold
        for seconds (mouseWheel) or which simply do not need confirmation."""
        self._id += 1
        self.send(json.dumps({"id": self._id, "method": method, "params": params}))

    def call(self, method, _timeout=None, **params):
        """One command, its result. `_timeout` bounds this call alone (the dialog probe uses 3 s)."""
        self._id += 1
        mid = self._id
        self.send(json.dumps({"id": mid, "method": method, "params": params}))
        base = _timeout or self.timeout
        try:
            while True:
                # A JS dialog is open: the renderer withholds this reply until it closes (browser-side calls still
                # answer within this). Give up soon (socket.timeout) so _run can settle the dialog, instead of
                # sitting out the socket timeout. Re-evaluated per message: the live view may close it meanwhile.
                blocked = self.dialog and method != "Page.handleJavaScriptDialog"
                self.sock.settimeout(min(base, DIALOG_GRACE_S) if blocked else base)
                msg = json.loads(self.recv())
                self._note(msg)
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                    return msg.get("result", {})
        finally:
            self.sock.settimeout(self.timeout)

    def call_many(self, calls, chunk=100):
        """Pipeline [(method, params)…]: send a chunk, then collect its replies.
        Returns one item per call, the result dict or a RuntimeError (never
        raises for a single failed command). The AX snapshot resolves hundreds
        of nodes; one round trip each would dominate observe()."""
        out = []
        for i in range(0, len(calls), chunk):
            ids = {}
            for j, (method, params) in enumerate(calls[i:i + chunk]):
                self._id += 1
                ids[self._id] = (j, method)
                self.send(json.dumps({"id": self._id, "method": method, "params": params}))
            res = [None] * len(ids)
            while ids:
                msg = json.loads(self.recv())
                self._note(msg)
                hit = ids.pop(msg.get("id"), None) if "id" in msg else None
                if hit is None:
                    continue
                j, method = hit
                res[j] = (RuntimeError(f"{method}: {msg['error'].get('message')}") if "error" in msg
                          else msg.get("result", {}))
            out += res
        return out

    def mark(self):
        """Forget the navigation events seen so far. Call right before the action whose navigation wait_loaded
        will watch, so nothing the previous page did (a late load, its abort) counts as the new one settling."""
        self.events = []

    def wait_loaded(self, timeout=15, grace=None, url=None):
        """Block until the navigation the last action started has settled, or `timeout` s pass (True / False).

        Settled, top frame only (child frames never count: an ad iframe's replaceState must not end a wait):
        - the load event, after a start was seen on this socket (a load with no start is the previous page's);
        - the frame stopping after a start (a bfcache restore commits and stops in ~20 ms and never fires load);
        - a same-document move with no cross-document start in flight (back/forward over pushState history, a
          hash goto, an SPA route change); `url` narrows it to that destination, so the page's own replaceState
          timer cannot pass for the traversal;
        - Page.windowOpen: the action opened a new tab, nothing more happens here.
        Gives up at once when a JS dialog opens (nothing proceeds until _run settles it). With `grace`, gives up
        that many seconds in if no navigation has started: a click that only ran a script is done.
        A stop with no start seen is the previous page being aborted and is ignored. Events call() drained
        while waiting for the command's reply are read first: a bfcache restore is done before the reply."""
        main = self.main_frame
        started = False

        def settled(msg):
            nonlocal started
            m, p = msg.get("method"), msg.get("params") or {}
            if m == "Page.loadEventFired":
                return started or not main  # no frame filter possible: load is the only signal left
            if m == "Page.windowOpen":
                return True
            if not main or p.get("frameId") != main:
                return False
            if m == "Page.frameStartedNavigating":
                # Chrome sends this for same-document moves too (navigationType sameDocument / historySameDocument);
                # those never stop or load, so only a cross-document one arms the stop.
                if not (p.get("navigationType") or "").lower().endswith("samedocument"):
                    started = True
            elif m == "Page.frameStartedLoading":
                started = True
            elif m == "Page.frameStoppedLoading":
                return started
            elif m == "Page.navigatedWithinDocument":
                return not started and (url is None or p.get("url") == url)
            return False

        t0 = time.time()
        try:
            if self.dialog:
                return False
            if any(settled(msg) for msg in list(self.events)):
                return True
            while True:
                until = t0 + timeout if started or grace is None else min(t0 + timeout, t0 + grace)
                left = until - time.time()
                if left <= 0:
                    return False
                self.sock.settimeout(left)
                msg = json.loads(self.recv())
                self._note(msg)
                if self.dialog:
                    return False
                if settled(msg):
                    return True
        except socket.timeout:
            return False
        finally:
            self.events = []
            self.sock.settimeout(self.timeout)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# The DOM scan. Clears the previous pass's stamps first (a stale sb5 on a
# node this pass no longer lists would otherwise shadow the new sb5 in
# `querySelector`), stamps the first 160 matches, returns them plus the stamp
# counter (the AX pass continues numbering from it) and the CSS viewport.
SNAPSHOT_JS = r"""
(() => {
  document.querySelectorAll('[data-sb-ref]').forEach(e => e.removeAttribute('data-sb-ref'));
  const sel = 'a, button, input, textarea, select, [role=button], [role=link], [role=textbox], [contenteditable=true], [onclick], summary';
  const out = [];
  const seen = new Set();
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
      && r.bottom > -200 && r.top < innerHeight + 600; };
  // Dropdown/hover menus: their links are display:none until hovered, but
  // anchor.click() still navigates, so expose nav/header links even when hidden.
  const inMenu = el => !!el.closest('nav, header, [role=navigation], [role=menu], [aria-haspopup]');
  let n = 0;
  document.querySelectorAll(sel + ', [aria-haspopup], [aria-expanded], label').forEach(el => {
    if (n >= 160 || seen.has(el)) return;  // only what the observation can list gets a stamp
    // Labels count only when they stand in for a hidden file input (the usual "Upload" button).
    if (el.tagName === 'LABEL' && !(el.control && el.control.type === 'file')) return;
    const shown = vis(el);
    const isLink = el.tagName === 'A' && el.href;
    const isFile = el.tagName === 'INPUT' && el.type === 'file';  // usually hidden behind a styled button
    if (!shown && !(isLink && inMenu(el)) && !isFile) return;
    if (el.tagName === 'A' && !el.href && !(el.innerText || '').trim()) return;  // decorative anchors
    seen.add(el);
    const ref = 'sb' + (++n);
    el.setAttribute('data-sb-ref', ref);
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.placeholder || el.alt || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    const rc = el.getBoundingClientRect();
    const o = { ref, tag, text, x: Math.round(rc.left + rc.width / 2), y: Math.round(rc.top + rc.height / 2) };
    if (!shown && !isFile) o.menu = true;
    if (isFile) { o.upload = true; if (el.accept) o.accept = el.accept.slice(0, 60); }
    if (el.tagName === 'LABEL') { o.upload = true; if (el.control.accept) o.accept = el.control.accept.slice(0, 60); }
    if (el.closest('[role=dialog], [role=alertdialog], [aria-modal="true"], dialog[open]')) o.dialog = true;
    if (el.type && tag !== 'a' && tag !== 'button') o.type = el.type;
    if (tag === 'select') o.options = [...el.options].map(x => x.text.trim()).filter(Boolean).slice(0, 12);
    if (tag === 'select' && el.selectedIndex >= 0) o.value = (el.options[el.selectedIndex].text || '').trim().slice(0, 40);
    if ((el.type === 'checkbox' || el.type === 'radio') && 'checked' in el) o.checked = el.checked;
    if (el.getAttribute('aria-checked')) o.checked = el.getAttribute('aria-checked') === 'true';
    // Text fields: say whether they hold anything. An empty input reads as its
    // placeholder above, so without this a click that only focused it looks
    // like it changed nothing and the model clicks it again instead of typing.
    const typed = ['text', 'search', 'email', 'url', 'tel', 'password', 'number', ''];
    if ((tag === 'input' && typed.includes(el.type || '')) || tag === 'textarea' || el.isContentEditable || el.getAttribute('role') === 'textbox') {
      const v = (tag === 'input' || tag === 'textarea') ? (el.value || '') : (el.innerText || '').trim();
      o.empty = !v;
      if (v) o.value = v.slice(0, 40);
      if (document.activeElement === el) o.focused = true;
    }
    if (tag === 'a' && el.href) o.href = el.href.slice(0, 120);
    if (el.name) o.name = el.name;
    out.push(o);
  });
  return {elements: out.slice(0, 160), n, vw: innerWidth, vh: innerHeight};
})()
"""

TEXT_JS = r"""(document.body ? document.body.innerText : '').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').slice(0, 30000)"""

# Offset of the running frame inside the top viewport, for nodes in a
# same-process child frame (their rects are frame-relative; Input events and
# the observation are top-viewport CSS px). 0,0 in the main frame.
_FRAME_OFF_JS = r"""let ox = 0, oy = 0;
  try { let w = window; while (w !== w.top && w.frameElement) { const fe = w.frameElement, fr = fe.getBoundingClientRect();
    ox += fr.left + fe.clientLeft; oy += fr.top + fe.clientTop; w = w.parent; } } catch (e) {}"""

# Stamps the AX pass's nodes: `Runtime.callFunctionOn(opts, ...elements)`
# once per frame group. A node the DOM scan stamped THIS pass (light DOM of
# the top document, whose old stamps SNAPSHOT_JS just cleared) keeps its ref;
# any other stamp (shadow roots, frames: never cleared) is stale and replaced.
# Zero-size nodes and nodes far outside the viewport (the DOM scan's window)
# are skipped unless already stamped.
STAMP_FN = r"""function(opts, ...els) {
  """ + _FRAME_OFF_JS + r"""
  const isTop = window === window.top;
  const typed = ['text', 'search', 'email', 'url', 'tel', 'password', 'number', ''];
  let n = opts.n;
  const out = els.map(el => {
    if (!el || el.nodeType !== 1) return null;
    const rc = el.getBoundingClientRect();
    const had = el.getAttribute('data-sb-ref');
    const fresh = !!(had && isTop && el.getRootNode() === document);
    if (!fresh) {
      if (!(rc.width > 0 && rc.height > 0)) return null;
      if (rc.bottom + oy < -200 || rc.top + oy > opts.vh + 600) return null;
    }
    const ref = fresh ? had : 'sb' + (++n);
    if (!fresh) el.setAttribute('data-sb-ref', ref);
    const tag = el.tagName.toLowerCase();
    const o = {ref, tag, x: Math.round(ox + rc.left + rc.width / 2), y: Math.round(oy + rc.top + rc.height / 2)};
    if (el.type && typeof el.type === 'string' && tag !== 'a' && tag !== 'button') o.type = el.type;
    if (tag === 'a' && el.href) o.href = String(el.href).slice(0, 120);
    if (el.name && typeof el.name === 'string') o.name = el.name;
    if (tag === 'input' && el.type === 'file') { o.upload = true; if (el.accept) o.accept = el.accept.slice(0, 60); }
    if ((tag === 'input' && typed.includes(el.type || '')) || tag === 'textarea' || el.isContentEditable) {
      const v = (tag === 'input' || tag === 'textarea') ? (el.value || '') : (el.innerText || '').trim();
      o.empty = !v;
      if (v) o.value = v.slice(0, 40);
    }
    return o;
  });
  return {els: out, n};
}"""


def _find_js(selector, ref, text, x=None, y=None, strict=False):
    """JS expression resolving the target element: stamped ref first; else a
    VISIBLE element with the same text, nearest to where the ref was (pages
    like Gmail keep several 'Reply' buttons in the DOM, most of them hidden);
    else whatever is at the ref's old coordinates. `strict` keeps only the
    ref/selector lookups (used before trying the AX backend node, which is
    exact, ahead of the text/coordinate guesses)."""
    if strict:
        text, x, y = "", None, None
    return f"""(() => {{
      let el = null;
      const ref = {json.dumps(ref)}, sel = {json.dumps(selector)}, txt = {json.dumps(text)};
      const px = {json.dumps(x)}, py = {json.dumps(y)};
      const vis = e => {{ const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; }};
      if (ref) el = document.querySelector('[data-sb-ref=' + JSON.stringify(ref) + ']');
      if (el && !vis(el)) el = null;
      if (!el && sel) {{ try {{ el = document.querySelector(sel); }} catch (e) {{}} }}
      if (!el && txt) {{
        const cands = document.querySelectorAll('a, button, input, textarea, select, [role=button], [role=link], [role=textbox], [contenteditable=true], summary');
        let best = null, bd = Infinity;
        for (const c of cands) {{
          if (!vis(c)) continue;
          const t = (c.innerText || c.value || c.getAttribute('aria-label') || c.placeholder || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
          if (t !== txt) continue;
          const r = c.getBoundingClientRect();
          const d = (px == null) ? 0 : Math.hypot(r.left + r.width / 2 - px, r.top + r.height / 2 - py);
          if (d < bd) {{ bd = d; best = c; }}
        }}
        el = best;
      }}
      if (!el && px != null) {{
        const at = document.elementFromPoint(px, py);
        if (at) el = at.closest('a, button, input, textarea, select, [role=button], [role=link], [role=textbox], [contenteditable=true], summary') || at;
      }}
      return el;
    }})()"""


# Per-action element functions: `this` is the target element, whether it was
# found by `_find_js` (JS path) or resolved from its backend node
# (`Runtime.callFunctionOn`). One body serves both paths.
_CLICK_FN = r"""function() {
  """ + _FRAME_OFF_JS + r"""
  this.scrollIntoView({block: 'center'});
  const r = this.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const root = this.getRootNode();
  const top = (root && root.elementFromPoint ? root : document).elementFromPoint(cx, cy);
  if (!top || !(this === top || this.contains(top) || top.contains(this))) { this.click(); return {clicked: true}; }
  return {x: cx + ox, y: cy + oy};
}"""

_HOVER_FN = r"""function() {
  """ + _FRAME_OFF_JS + r"""
  this.scrollIntoView({block: 'center'});
  const r = this.getBoundingClientRect();
  return {x: r.left + r.width / 2 + ox, y: r.top + r.height / 2 + oy};
}"""

_TYPE_FN = r"""function() {
  this.scrollIntoView({block: 'center'}); this.focus();
  if ('value' in this) this.value = ''; return true;
}"""

_FOCUS_FN = r"""function() { this.scrollIntoView({block: 'center'}); this.focus(); return true; }"""

_SCROLL_FN = r"""function() { this.scrollIntoView({block: 'center'}); return true; }"""

_READ_FN = r"""function() { return (this.innerText || this.textContent || this.value || '').trim(); }"""

_SELECT_FN = r"""function(value) {
  const sel = this.tagName === 'SELECT' ? this : this.querySelector('select');
  if (!sel) return {error: 'not a <select> element; click it and pick the option instead'};
  const want = String(value).trim().toLowerCase();
  const opts = [...sel.options];
  let o = opts.find(o => o.value.toLowerCase() === want || o.text.trim().toLowerCase() === want)
       || opts.find(o => o.text.trim().toLowerCase().includes(want));
  if (!o) return {error: 'no option matching ' + JSON.stringify(want) + '; options: ' + opts.map(o => o.text.trim()).slice(0, 30).join(' | ')};
  sel.value = o.value;
  sel.dispatchEvent(new Event('input', {bubbles: true}));
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return {chosen: o.text.trim()};
}"""

# upload's backend path: the file input for `this` (itself, the input it
# labels, one inside it / its form, the nearest one in its tree), returned
# as an object for DOM.setFileInputFiles(objectId=…).
_UPLOAD_PICK_FN = r"""function() {
  const isFile = e => e && e.tagName === 'INPUT' && e.type === 'file';
  const el = this, root = el.getRootNode();
  if (isFile(el)) return el;
  if (el.tagName === 'LABEL' && el.control && isFile(el.control)) return el.control;
  let inp = el.querySelector('input[type=file]');
  if (!inp && el.closest('form')) inp = el.closest('form').querySelector('input[type=file]');
  if (!inp) {
    const r = el.getBoundingClientRect(); let best = null, bd = Infinity;
    (root.querySelectorAll ? root : document).querySelectorAll('input[type=file]').forEach(i => { const q = i.getBoundingClientRect();
      const d = Math.hypot(q.left - r.left, q.top - r.top); if (d < bd) { bd = d; best = i; } });
    inp = best;
  }
  return inp || document.querySelector('input[type=file]');
}"""

_MISSING = "__sb_missing__"


# --------------------------------------------------- accessibility snapshot ---
# AX roles an observation lists (docs §5): the interactive ones plus the
# landmarks a model needs to read a page (headings, dialogs, tab lists,
# menus) and images that carry a name. Chrome reports ARIA names in lower
# case and its own internal roles in CamelCase; aliases fold the latter.
AX_KEEP = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch", "slider",
    "spinbutton", "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "listbox", "treeitem",
    "heading", "dialog", "alertdialog", "tablist", "menu", "image",
})
AX_ROLE_ALIASES = {
    "img": "image", "popupbutton": "combobox", "menulistoption": "option", "listboxoption": "option",
    "disclosuretriangle": "button", "togglebutton": "button", "textfield": "textbox", "searchbox": "searchbox",
}
# Roles kept only when the node has a name (an unnamed heading or image says nothing).
AX_NEED_NAME = frozenset({"heading", "image"})
AX_CHECKABLE = frozenset({"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"})
AX_DIALOG = frozenset({"dialog", "alertdialog"})
# Roles a DOM-scan record may be folded into by text + position (never a
# heading/dialog/landmark, whose text a nested link or button repeats).
_AX_ACTIONABLE = AX_KEEP - {"heading", "dialog", "alertdialog", "tablist", "menu", "image"}
# DOM-scan keys the merged record takes from the DOM side when present.
_FIELD_TAGS = frozenset({"input", "select", "textarea"})
DOM_EXTRA_KEYS = ("options", "value", "empty", "focused", "upload", "accept", "menu", "dialog", "type", "name", "href")


def _norm_role(role):
    r = (role or "").strip()
    low = r.lower()
    return AX_ROLE_ALIASES.get(low, low)


def _implicit_role(tag, typ=None):
    """The role an element has without saying so (`<a>` is a link), so the
    element line stays OpenBot's `sb3 a "Home"` and not `sb3 a role=link …`."""
    t = (tag or "").lower()
    if t == "a":
        return "link"
    if t in ("button", "summary"):
        return "button"
    if t == "select":
        return "combobox"
    if t == "textarea":
        return "textbox"
    if t == "img":
        return "image"
    if t in ("option", "dialog"):
        return t
    if re.fullmatch(r"h[1-6]", t):
        return "heading"
    if t == "input":
        return {"": "textbox", "text": "textbox", "email": "textbox", "tel": "textbox", "url": "textbox",
                "password": "textbox", "search": "searchbox", "checkbox": "checkbox", "radio": "radio",
                "range": "slider", "number": "spinbutton", "button": "button", "submit": "button",
                "reset": "button", "image": "button"}.get((typ or "").lower())
    return None


def _ax_value(v):
    return (v or {}).get("value") if isinstance(v, dict) else None


def ax_candidates(nodes, limit=MAX_AX_CANDIDATES):
    """The AX nodes worth listing, in document order (a DFS over childIds from
    the root), one per DOM node: [{backend, role, text, dialog?, checked?,
    expanded?, selected?, disabled?, focused?}]. Ignored nodes are skipped
    (their children still count); a node inside a dialog carries dialog=True."""
    by_id = {n.get("nodeId"): n for n in nodes or [] if n.get("nodeId") is not None}
    if not by_id:
        return []
    roots = [n for n in nodes if not n.get("parentId") or n.get("parentId") not in by_id]
    out, seen_backend, seen_node = [], set(), set()
    stack = [(r.get("nodeId"), False) for r in reversed(roots)]
    while stack and len(out) < limit:
        nid, in_dialog = stack.pop()
        if nid in seen_node or nid not in by_id:
            continue
        seen_node.add(nid)
        n = by_id[nid]
        role = _norm_role(_ax_value(n.get("role")))
        here_dialog = in_dialog or (role in AX_DIALOG and not n.get("ignored"))
        for c in reversed(n.get("childIds") or []):
            stack.append((c, here_dialog))
        if n.get("ignored") or role not in AX_KEEP:
            continue
        backend = n.get("backendDOMNodeId")
        if backend is None or backend in seen_backend:
            continue
        name = " ".join(str(_ax_value(n.get("name")) or "").split())[:80]
        if role in AX_NEED_NAME and not name:
            continue
        seen_backend.add(backend)
        rec = {"backend": backend, "role": role, "text": name}
        if here_dialog:
            rec["dialog"] = True
        for p in n.get("properties") or []:
            pn, pv = p.get("name"), _ax_value(p.get("value"))
            if pn == "checked" and role in AX_CHECKABLE:
                rec["checked"] = pv in ("true", True, "mixed")
            elif pn == "expanded" and pv is not None:
                rec["expanded"] = bool(pv)
            elif pn in ("selected", "disabled", "focused") and pv in (True, "true"):
                rec[pn] = True
        out.append(rec)
    return out


def _near(ax, taken, d, tol=6):
    """Index of an unmatched actionable AX record with the DOM record's text
    within `tol` px of it, closest first; None when there is none."""
    t = (d.get("text") or "").strip()
    if not t or d.get("x") is None or d.get("y") is None:
        return None
    best, bd = None, None
    for i, a in enumerate(ax):
        if i in taken or (a.get("text") or "").strip() != t or a.get("role") not in _AX_ACTIONABLE:
            continue
        if a.get("x") is None or a.get("y") is None:
            continue
        dx, dy = abs(a["x"] - d["x"]), abs(a["y"] - d["y"])
        if dx <= tol and dy <= tol and (bd is None or dx + dy < bd):
            best, bd = i, dx + dy
    return best


def _combine(a, d):
    """One record from an AX record and the DOM-scan record of the same
    control. Same element (same stamp): the DOM dict is the base (OpenBot's
    tag/text/extras, which `_find_js`'s text fallback matches) plus the AX
    role, backend and states. Matched by text + position (two nodes): the AX
    node stays the target, the DOM side lends its extras."""
    same = a.get("ref") == d.get("ref")
    if same:
        out = dict(d)
        out["text"] = d.get("text") or a.get("text") or ""
        if d.get("tag") in _FIELD_TAGS and a.get("text"):
            # A field's DOM text is its value / its options run together; the
            # accessible name is its label ("Email", "Colour"). The value
            # stays in `value`.
            out["text"] = a["text"]
        for k in ("role", "backend", "expanded", "selected", "disabled"):
            if k in a:
                out[k] = a[k]
        if "checked" in a:
            out["checked"] = a["checked"]
    else:
        out = dict(a)
        for k in DOM_EXTRA_KEYS:
            if k in d and k not in ("focused", "dialog"):
                out[k] = d[k]
    if a.get("focused") or d.get("focused"):
        out["focused"] = True
    if a.get("dialog") or d.get("dialog"):
        out["dialog"] = True
    return out


def _by_position(a, b):
    """Merge two document-ordered lists by (y, x); `a` first on a tie."""
    key = lambda e: (e.get("y") if e.get("y") is not None else 0, e.get("x") if e.get("x") is not None else 0)  # noqa: E731
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        if key(b[j]) < key(a[i]):
            out.append(b[j])
            j += 1
        else:
            out.append(a[i])
            i += 1
    return out + a[i:] + b[j:]


def merge_elements(ax_nodes, dom_nodes, viewport, cap=MAX_ELEMENTS):
    """The observation's element list from the AX pass and the DOM scan (pure).

    ax_nodes   resolved AX records in document order: {ref, role, tag, text, x, y, backend, states…}
    dom_nodes  SNAPSHOT_JS records in document order
    viewport   (css width, css height)

    A DOM record merges into the AX record carrying the same stamp, else into
    an actionable AX record with the same text within a few px; otherwise it
    is kept on its own, placed after the record its DOM predecessor merged
    into (so document order survives; records before the first merge are
    interleaved with the AX records ahead of it by position). Every record
    gets `offscreen` when its
    centre is outside the viewport; the result is the on-screen records then
    the off-screen ones, each in document order, capped."""
    vw, vh = viewport
    ax = [dict(a) for a in ax_nodes or [] if a.get("ref")]
    by_ref = {}
    for i, a in enumerate(ax):
        by_ref.setdefault(a["ref"], i)
    taken, extras, last = set(), {}, -1
    for d in dom_nodes or []:
        if not d.get("ref"):
            continue
        i = by_ref.get(d["ref"])
        if i is None or i in taken:
            i = _near(ax, taken, d)
        if i is None:
            extras.setdefault(last, []).append(dict(d))
            continue
        taken.add(i)
        ax[i] = _combine(ax[i], d)
        last = i
    # DOM-only records with no matched predecessor have no anchor: interleave
    # them with the AX records before the first match by reading position.
    first = min(taken) if taken else len(ax)
    merged = _by_position(ax[:first], extras.get(-1, []))
    for i in range(first, len(ax)):
        merged.append(ax[i])
        merged.extend(extras.get(i, []))
    on, off = [], []
    for e in merged:
        if e.get("role") and e.get("role") == _implicit_role(e.get("tag"), e.get("type")):
            e.pop("role")
        if e.get("tag") == "select":
            # Chrome reports every native <select> as collapsed; printed, that
            # invites click-to-expand instead of the `select` tool.
            e.pop("expanded", None)
        x, y = e.get("x"), e.get("y")
        e.pop("offscreen", None)
        if x is not None and y is not None and not (0 <= x <= vw and 0 <= y <= vh):
            e["offscreen"] = True
            off.append(e)
        else:
            on.append(e)
    return (on + off)[:cap]


# ------------------------------------------------------- profile at rest ---
# Opt-in per bot: while Chrome is closed, the profile folder lives as one
# AES-256 blob (profile.enc) and the plaintext folder is gone. The key is a
# random 256-bit secret kept in the user's login Keychain (one per machine).
# Uses the system `tar`, `openssl` and `security` so nothing is installed.
# Chrome already wraps cookies and passwords with its own Keychain key; this
# additionally covers history, local storage, IndexedDB and session files.
KEY_SERVICE = "fused browser-bot"
KEY_ACCOUNT = "profile-key"
SEAL_SKIP = ["Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "DawnCache",
             "SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile", "DevToolsActivePort"]


def _sec(*args):
    return subprocess.run(["security", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=30, close_fds=False)


def profile_key():
    """The machine's profile key from the Keychain, created on first use."""
    r = _sec("find-generic-password", "-s", KEY_SERVICE, "-a", KEY_ACCOUNT, "-w")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    key = secrets.token_hex(32)
    r = _sec("add-generic-password", "-s", KEY_SERVICE, "-a", KEY_ACCOUNT, "-w", key,
             "-j", "Encrypts browser-bot Chrome profiles at rest. Deleting it makes sealed profiles unreadable.")
    if r.returncode:
        raise RuntimeError(f"could not store the profile key in the Keychain: {r.stderr.strip() or r.returncode}")
    r = _sec("find-generic-password", "-s", KEY_SERVICE, "-a", KEY_ACCOUNT, "-w")  # another worker may have won the race
    return (r.stdout.strip() if r.returncode == 0 else "") or key


def _openssl_cmd(*args):
    return ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt", "-pass", "env:BB_PROFILE_KEY", *args]


# ---------------------------------------------------------------- process ---
class BrowserProcess:
    """One Chrome process: profile folder, debugging port, lock, encryption at
    rest. `views` are the `Browser`s (bots) driving it; with more than one the
    process is shared and each view keeps to its own tabs."""

    def __init__(self, data_dir, cache_dir):
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.profile = os.path.join(data_dir, "profile")
        self.session_path = os.path.join(cache_dir, "session.json")
        self.sealed_path = os.path.join(data_dir, "profile.enc")
        self.encrypt = False  # from browser.json (browsers.py); seal the profile whenever Chrome stops
        self.lock = threading.RLock()
        self._proc = None
        self.views = []  # Browser views attached; len > 1 = shared
        # The window size, following the live view's stage (set_viewport). Its own file: session.json is
        # rewritten on every launch and removed on stop, and the size has to outlive both.
        self.viewport_path = os.path.join(cache_dir, "viewport.json")
        self.viewport = self._read_viewport()

    def shared(self):
        return len(self.views) > 1

    # -- viewport --------------------------------------------------------------
    VIEWPORT_W = (640, 3840)
    VIEWPORT_H = (480, 2400)

    @classmethod
    def _clamp_viewport(cls, w, h):
        return (max(cls.VIEWPORT_W[0], min(cls.VIEWPORT_W[1], int(w))),
                max(cls.VIEWPORT_H[0], min(cls.VIEWPORT_H[1], int(h))))

    def _read_viewport(self):
        try:
            with open(self.viewport_path, encoding="utf-8") as f:
                w, h = json.load(f)["viewport"]
            return self._clamp_viewport(w, h)
        except Exception:  # noqa: BLE001 — none saved yet (or unreadable): the default
            return tuple(VIEWPORT)

    def set_viewport(self, w, h):
        """Size this Chrome's windows to (w, h), clamped, and remember it for the next launch.
        A popped-out (headed) window is the user's and is never resized; the size still applies
        once it docks back (relaunch). Returns the stored size."""
        size = self._clamp_viewport(w, h)
        with self.lock:
            if size == self.viewport:
                return size
            sess = self.session()
        if sess and not sess.get("headed") and self.alive(sess):
            # Resize first, remember after: a failed resize raises, so the caller (the live view) tries again
            # instead of believing a size Chrome never took.
            self._resize_windows(sess["port"], size)
        with self.lock:
            self.viewport = size
            try:
                write_json_atomic(self.viewport_path, {"viewport": list(size)})
            except OSError:
                log.warning("viewport not saved", exc_info=True)
        return size

    @staticmethod
    def _resize_windows(port, size):
        """Every page window to `size`, as a CSS viewport: the window is set, the viewport measured
        (Page.getLayoutMetrics), and any UI strip Chrome keeps between the two is added once. Raises
        when no window could be resized."""
        bws = WS(_http(port, "/json/version")["webSocketDebuggerUrl"], timeout=3)
        done = 0
        try:
            seen = set()
            for t in _http(port, "/json/list"):
                if t.get("type") != "page":
                    continue
                try:
                    wid = bws.call("Browser.getWindowForTarget", targetId=t["id"])["windowId"]
                    if wid in seen:
                        continue
                    seen.add(wid)
                    want = {"width": size[0], "height": size[1]}
                    bws.call("Browser.setWindowBounds", windowId=wid, bounds={**want, "windowState": "normal"})
                    pws = WS(t["webSocketDebuggerUrl"], timeout=3)
                    try:
                        vp = (pws.call("Page.getLayoutMetrics").get("cssLayoutViewport") or {})
                    finally:
                        pws.close()
                    dw = size[0] - int(vp.get("clientWidth") or size[0])
                    dh = size[1] - int(vp.get("clientHeight") or size[1])
                    if 0 < dw < 200 or 0 < dh < 200:
                        bws.call("Browser.setWindowBounds", windowId=wid,
                                 bounds={"width": size[0] + max(0, dw), "height": size[1] + max(0, dh)})
                    done += 1
                except Exception:  # noqa: BLE001 — a tab closing mid-loop; the others still resize
                    log.warning("resizing a bot window failed", exc_info=True)
        finally:
            bws.close()
        if not done:
            raise RuntimeError("no bot window could be resized")

    def attach(self, view):
        """Add a view. When the first view gets company, every tab open now is
        the first view's (it drove them all while alone): claimed into its own
        set, so turning shared never orphans a page it is working on."""
        if view in self.views:
            return
        if len(self.views) == 1:
            first = self.views[0]
            sess = self.session()
            try:
                if sess and self.alive(sess):
                    ids = [t["id"] for t in _http(sess["port"], "/json/list") if t.get("type") == "page"]
                    first._own = [i for i in first._own if i in ids] + [i for i in ids if i not in first._own]
                    first._write_own(url=sess.get("url") or "")
            except Exception:  # noqa: BLE001
                log.debug("claiming tabs for the first view failed", exc_info=True)
        self.views.append(view)

    def detach(self, view):
        self.views = [v for v in self.views if v is not view]

    # -- profile at rest -----------------------------------------------------
    def sealed(self):
        return os.path.exists(self.sealed_path) and not os.path.isdir(self.profile)

    def seal(self):
        """Encrypt profile/ into profile.enc and remove the plaintext. Chrome must
        be stopped. Caches are skipped (Chrome rebuilds them)."""
        with self.lock:
            if self.alive():
                raise RuntimeError("cannot seal while Chrome is running")
            if not os.path.isdir(self.profile):
                return False
            key = profile_key()
            tmp = self.sealed_path + ".tmp"
            excludes = [f"--exclude=profile/{n}" for n in SEAL_SKIP] + [f"--exclude=profile/*/{n}" for n in SEAL_SKIP]
            tar = subprocess.Popen(["tar", "-C", self.data_dir, *excludes, "-cf", "-", "profile"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=False)
            enc = subprocess.Popen(_openssl_cmd("-out", tmp), stdin=tar.stdout, stderr=subprocess.PIPE,
                                   env={**os.environ, "BB_PROFILE_KEY": key}, close_fds=False)
            tar.stdout.close()  # openssl holds the read end
            _, err = enc.communicate(timeout=600)
            _, tar_err = tar.communicate(timeout=60)
            if enc.returncode or tar.returncode not in (0, 1):  # tar 1 = a file changed while reading; fine
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise RuntimeError("encrypting the profile failed: "
                                   f"{err.decode(errors='replace').strip() or tar_err.decode(errors='replace').strip()}")
            os.replace(tmp, self.sealed_path)
            shutil.rmtree(self.profile, ignore_errors=True)
            return True

    def unseal(self):
        """Restore profile/ from profile.enc. A plaintext folder always wins
        (it is the newer state), in which case a stale blob is dropped."""
        with self.lock:
            if os.path.isdir(self.profile):
                try:
                    os.remove(self.sealed_path)
                except FileNotFoundError:
                    pass
                return False
            if not os.path.exists(self.sealed_path):
                return False
            key = profile_key()
            tmpdir = self.profile + ".unseal"
            shutil.rmtree(tmpdir, ignore_errors=True)
            os.makedirs(tmpdir)
            dec = subprocess.Popen(_openssl_cmd("-d", "-in", self.sealed_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={**os.environ, "BB_PROFILE_KEY": key}, close_fds=False)
            tar = subprocess.Popen(["tar", "-C", tmpdir, "-xf", "-"], stdin=dec.stdout, stderr=subprocess.PIPE, close_fds=False)
            dec.stdout.close()
            _, tar_err = tar.communicate(timeout=600)
            _, dec_err = dec.communicate(timeout=60)
            if dec.returncode or tar.returncode:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise RuntimeError("decrypting the profile failed (wrong or missing Keychain key?): "
                                   f"{dec_err.decode(errors='replace').strip() or tar_err.decode(errors='replace').strip()}")
            os.replace(os.path.join(tmpdir, "profile"), self.profile)
            shutil.rmtree(tmpdir, ignore_errors=True)
            os.remove(self.sealed_path)
            return True

    # -- process -----------------------------------------------------------
    # -- process -----------------------------------------------------------
    def session(self):
        try:
            with open(self.session_path) as f:
                return json.load(f)
        except Exception:
            return None

    def alive(self, sess=None):
        sess = sess or self.session()
        if not sess:
            return False
        try:
            _http(sess["port"], "/json/version")
            return True
        except Exception:
            return False

    def start(self, url="", view=None):
        """Launch headless Chrome for this profile unless it is already up with the
        live view's origin allowed; returns the session. Chrome is never relaunched
        for a take-over: the user drives the same tab from the live view. A profile
        popped out as a real window (`popout`) is left as it is: `dock` ends that.
        `url` is the page to come back to on a cold start (the asking `view`'s last
        page): session.json went with the quit, so a wake knows it only from the caller."""
        origin = page_origin()
        with self.lock:
            sess = self.session()
            restore = url or ""
            if self.alive(sess):
                if sess.get("origin") == origin or sess.get("headed"):
                    return sess
                restore = sess.get("url") or restore  # a relaunch just to pick up the flag keeps the page
                self.stop(seal=False)
            return self._launch(origin, False, restore, view)

    def headed(self):
        sess = self.session() or {}
        return bool(sess.get("headed") or sess.get("visible"))  # `visible`: a window popped out before this flag existed

    def popout(self, url="", view=None):
        """The one deliberate relaunch left: the same profile as a real Chrome window
        on the desktop, for what no screencast can carry (passkeys, the password
        manager, print). `view` is the bot that asked: its page is the one restored.
        Returns the session; a no-op when already headed."""
        return self._relaunch(True, url, view)

    def dock(self, url="", view=None):
        """Back to headless after `popout` (the window may already be gone). Returns the session."""
        return self._relaunch(False, url, view)

    def _relaunch(self, headed, url, view):
        with self.lock:
            sess = self.session()
            if self.alive(sess) and bool(sess.get("headed")) == headed:
                return sess
            url = url or (sess or {}).get("url") or ""
            if sess:
                self.stop(seal=False)  # clean: Chrome unlinks its Singleton* files, so the launch is never forwarded to it
            return self._launch(page_origin(), headed, url, view)

    def window_closed(self):
        """A popped-out profile whose user closed the window: Chrome quit, or (macOS)
        is still running with no page target left. False for a headless profile."""
        sess = self.session()
        if not sess or not sess.get("headed"):
            return False
        if not self.alive(sess):
            return True
        try:
            return not any(t.get("type") == "page" for t in _http(sess["port"], "/json/list"))
        except Exception:  # noqa: BLE001
            return False  # a slow answer is not a closed window; the next poll asks again

    def _launch(self, origin, headed, restore, view=None):
        """Start Chrome on this profile (the caller holds the lock). `restore` is the page to come back
        to; `view` the bot whose page it is (default: the first attached), which also claims the launch
        tab on a shared process so the window Chrome opened is driven, not orphaned beside a new one."""
        with self.lock:
            restore = restore if restore and restore != "about:blank" else ""
            self.unseal()
            os.makedirs(self.profile, exist_ok=True)
            os.makedirs(self.cache_dir, exist_ok=True)
            # Chrome picks the port (--remote-debugging-port=0) and writes it to <profile>/DevToolsActivePort
            # (line 1 the port, line 2 the browser socket path); picking a free port here first is a race.
            active = os.path.join(self.profile, "DevToolsActivePort")
            try:
                os.remove(active)  # a stale file from the previous run would read as "up" too early
            except FileNotFoundError:
                pass
            chrome = find_chrome()
            # Headless renders at LIVE_SCALE natively (crisp live view) instead of a per-session Emulation
            # override: overrides die with the DevTools session that set them, so every screenshot connection
            # closing made the viewport snap back to 1x and the live view flip size. A popped-out window is a
            # plain Chrome; occluded windows must keep compositing or the live view's mirror of it freezes.
            mode = ["--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"] if headed else [
                "--headless=new", f"--user-agent={_clean_user_agent(chrome)}", f"--force-device-scale-factor={LIVE_SCALE}"]
            args = [
                chrome, *mode,
                "--remote-debugging-port=0",
                # MEASURED (Chrome 155): `--remote-debugging-port=0` alone makes Chrome report navigator.webdriver
                # = true (an explicit port does not), and Google's sign-in refuses such a browser ("this browser or
                # app may not be secure"). This Blink switch turns that signal off; nothing else in the launch sets it.
                "--disable-blink-features=AutomationControlled",
                f"--remote-allow-origins={origin}",  # lets the page's live view connect directly
                f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-sync", "--hide-scrollbars",
                # A crash (SIGKILL, power loss) leaves exit_type "Crashed" in the profile, which only a human
                # dismissing the restore bubble resets; headless never can, so the bubble must never be asked for.
                "--hide-crash-restore-bubble",
                f"--window-size={self.viewport[0]},{self.viewport[1]}",
                _with_scheme(restore) if headed and restore else "about:blank",
            ]
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True, close_fds=False)
            self._proc = proc
            sess = {"port": 0, "pid": proc.pid, "started": time.time(), "origin": origin, "headed": headed}
            for _ in range(100):
                if not sess["port"]:
                    try:
                        with open(active, encoding="utf-8") as f:
                            sess["port"] = int(f.readline().strip() or 0)
                    except (OSError, ValueError):
                        pass
                if sess["port"] and self.alive(sess):
                    break
                if proc.poll() is not None:
                    raise RuntimeError(f"Chrome exited at launch (code {proc.returncode})")
                time.sleep(0.1)
            else:
                try:
                    proc.kill()  # never leave a half-started Chrome holding the profile
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError("Chrome did not come up")
            port = sess["port"]
            # The window Chrome opened on launch: the first view to act adopts it
            # (_page_target). The page appears a beat after DevTools answers.
            sess["launch_tab"] = ""
            for _ in range(20):
                try:
                    launch = [t for t in _http(port, "/json/list") if t.get("type") == "page"]
                except Exception:  # noqa: BLE001
                    launch = []
                if launch:
                    sess["launch_tab"] = launch[0]["id"]
                    break
                time.sleep(0.1)
            view = view if view in self.views else (self.views[0] if self.views else None)
            for v in self.views:
                v._main_tab = None
                if self.shared():
                    v._own = []
            if headed and self.shared() and view is not None and sess["launch_tab"]:
                # The headed window opened straight on the asking bot's page: that tab is its own, or its first
                # action would open a second window beside it (_adopt_launch_tab only takes a blank launch tab).
                view._own = [sess["launch_tab"]]
                view._write_own(url=restore)
            write_json_atomic(self.session_path, sess)
            # Headless Chrome has no permission prompt: every request is silently denied. Clipboard access is granted
            # browser-wide so a page's own Copy buttons work (this Chrome's clipboard is private to it, measured, so a
            # site reading it sees only what was copied inside the bot's browser). Geolocation, camera, microphone and
            # notifications stay denied: granting those without a prompt would be a privacy decision made for the user.
            try:
                bws = WS(_http(port, "/json/version")["webSocketDebuggerUrl"], timeout=3)
                try:
                    bws.call("Browser.grantPermissions", permissions=["clipboardReadWrite", "clipboardSanitizedWrite"])
                finally:
                    bws.close()
            except Exception:  # noqa: BLE001 — a Chrome without the permission names just keeps denying
                log.debug("clipboard permission grant failed", exc_info=True)
            if restore:
                # The page comes back (headed: Chrome opened on it; headless: navigated below, not awaited). Recorded now so
                # status / the live view's URL bar show it before the first action rewrites the session.
                sess["url"] = restore
                write_json_atomic(self.session_path, sess)
            if view is not None and restore and not headed:
                try:
                    tab, _ = view._page_target(port)  # shared: adopts the launch tab on the view's last page already
                    ws = WS(tab["webSocketDebuggerUrl"])
                    ws.call("Page.navigate", url=restore)  # do not wait for the load
                    ws.close()
                except Exception:
                    pass
            return sess

    def stop(self, seal=None):
        """Quit Chrome. With encryption on (or seal=True) the profile is sealed
        once the process is gone; seal=False skips that (relaunching)."""
        with self.lock:
            self._stop_proc()
            if self.encrypt if seal is None else seal:
                self.seal()

    def _stop_proc(self):
        with self.lock:
            sess = self.session()
            if sess:
                proc = self._proc if (self._proc and self._proc.pid == sess["pid"]) else None
                closing = False
                try:
                    info = _http(sess["port"], "/json/version")
                    ws = WS(info["webSocketDebuggerUrl"], timeout=3)
                    try:
                        ws.notify("Browser.close")  # no reply ever comes: Chrome tears the socket down while closing (measured)
                        closing = True
                    finally:
                        ws.close()
                except Exception:
                    pass
                # Browser.close first and alone: Chrome then unlinks its Singleton* files on the way out, so the next
                # launch is never forwarded to a dying pid (ProcessSingleton). SIGTERM only when that did not end it in
                # 3 s (it would race the clean exit and leave stale lock files Chrome has to detect itself), then SIGKILL.
                def gone():
                    if proc is not None:
                        return proc.poll() is not None
                    try:  # not our child (the server restarted since): launchd reaps it, so the pid probe is exact
                        os.kill(sess["pid"], 0)
                        return False
                    except ProcessLookupError:
                        return True
                    except OSError:
                        return False
                exited = False
                for step, grace in ((("close", 3.0),) if closing else ()) + (("term", 3.0), ("kill", 2.0)):
                    if step == "term":
                        try:
                            os.kill(sess["pid"], signal.SIGTERM)
                        except Exception:
                            pass
                    elif step == "kill":
                        try:
                            os.killpg(sess["pid"], signal.SIGKILL)
                        except Exception:
                            try:
                                os.kill(sess["pid"], signal.SIGKILL)
                            except Exception:
                                pass
                    end = time.time() + grace
                    while time.time() < end:
                        if gone():
                            exited = True
                            break
                        time.sleep(0.05)
                    if exited:
                        break
                if proc is not None:
                    try:
                        proc.wait(timeout=1)  # reap
                    except Exception:
                        pass
                self._proc = None
                try:
                    os.remove(self.session_path)
                except FileNotFoundError:
                    pass

    def stop_if_idle(self):
        """Idle sleep for a shared process: quit only once every view may sleep
        NOW (`Browser.may_sleep()`: its bot's live check when it set one, else the
        flag its own sleep attempt left). Returns True when Chrome was stopped."""
        with self.lock:
            if any(not v.may_sleep() for v in self.views):
                return False
            self.stop()
            return True

# ---------------------------------------------------------------- browser ---
class Browser:
    """One bot's view of a `BrowserProcess`: its tabs, screenshots, downloads
    and page actions. `Browser(data_dir, cache_dir)` without `proc` makes a
    private process at the old per-bot paths (profile/ under data_dir), which
    is what tests and a lone bot get."""

    def __init__(self, data_dir, cache_dir, proc=None):
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.proc = proc or BrowserProcess(data_dir, cache_dir)
        self.proc.attach(self)
        self.shot_path = os.path.join(cache_dir, "shot.png")
        self.downloads = os.path.join(data_dir, "downloads")
        self.tabs_path = os.path.join(cache_dir, "tabs.json")  # own tab ids + last url, for a shared process
        self._tab_order = []      # tab ids in open order (Chrome reports newest-first)
        self._own = self._read_own()  # target ids this view opened (shared process only)
        self._main_tab = None  # target id of the tab we drive
        self.idle = False      # set by the bot's idle sleep; cleared on the next action (stop_if_idle)
        self.idle_check = None  # bot.py sets a callable: "may this bot's browser sleep right now?" (looked at, task, control)
        self.live_seen = 0.0
        self.thumb_bytes = None  # JPEG of the last screenshot, for step thumbnails
        # Screenshot after every `_run` action. The agent engine turns this off for its
        # task: it observes after every browser step anyway, and each `_shoot` costs a
        # PNG capture plus two sips conversions. observe/screenshot always capture.
        self.shoot_actions = True
        self._ax_warned = False  # the AX snapshot failure is logged once per browser

    # -- process delegation (the API bot.py and routes.py drive) -----------
    @property
    def lock(self):
        return self.proc.lock

    @property
    def profile(self):
        return self.proc.profile

    @property
    def encrypt(self):
        return self.proc.encrypt

    @encrypt.setter
    def encrypt(self, on):
        self.proc.encrypt = bool(on)

    @property
    def session_path(self):
        return self.proc.session_path

    @property
    def sealed_path(self):
        return self.proc.sealed_path

    def sealed(self):
        return self.proc.sealed()

    def seal(self):
        return self.proc.seal()

    def unseal(self):
        return self.proc.unseal()

    def session(self):
        return self.proc.session()

    def alive(self, sess=None):
        return self.proc.alive(sess)

    def start(self):
        """Up, or relaunched on this bot's last page. A wake after idle sleep used to come up on about:blank
        for a lone bot: the launch tab is adopted as-is (only the shared branch of _page_target navigates it)."""
        self.idle = False
        return self.proc.start(url=self.last_url(), view=self)

    def headed(self):
        return self.proc.headed()

    @property
    def viewport(self):
        return self.proc.viewport

    def set_viewport(self, w, h):
        return self.proc.set_viewport(w, h)

    def popout(self):
        self.idle = False
        return self.proc.popout(url=self.last_url() if self.shared() else "", view=self)

    def dock(self):
        self.idle = False
        return self.proc.dock(url=self.last_url() if self.shared() else "", view=self)

    def window_closed(self):
        return self.proc.window_closed()

    def stop(self, seal=None):
        """Quit the process. On a shared process this closes every bot's tabs;
        idle sleep uses `sleep()` instead."""
        self.proc.stop(seal)

    def sleep(self):
        """Idle sleep: mark this view idle and quit Chrome once every bot on
        the process is. Returns True when Chrome actually stopped."""
        self.idle = True
        return self.proc.stop_if_idle()

    def may_sleep(self):
        """Whether this view's bot would let the shared Chrome go right now: its
        live check (set by bot.py) when there is one, else the sticky flag."""
        if self.idle_check is not None:
            try:
                return bool(self.idle_check())
            except Exception:  # noqa: BLE001
                return False
        return self.idle

    def shared(self):
        return self.proc.shared()

    # -- own tabs (shared process) -----------------------------------------
    def _read_own(self):
        try:
            with open(self.tabs_path) as f:
                d = json.load(f)
            return list(d.get("tabs") or [])
        except Exception:
            return []

    def _write_own(self, url=None, title=None):
        d = {"tabs": self._own}
        try:
            with open(self.tabs_path) as f:
                old = json.load(f)
        except Exception:
            old = {}
        d["url"] = url if url is not None else old.get("url")
        d["title"] = title if title is not None else old.get("title")
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            write_json_atomic(self.tabs_path, d)
        except Exception:
            pass

    def last_url(self):
        try:
            with open(self.tabs_path) as f:
                return json.load(f).get("url") or ""
        except Exception:
            return ""

    def _browser_ws(self, port, timeout=10):
        info = _http(port, "/json/version")
        return WS(info["webSocketDebuggerUrl"], timeout=timeout)

    def _own_targets(self, port):
        """This view's page targets on a shared process: the tabs it opened plus
        any popup whose opener chain leads to one of them. Prunes closed ids."""
        ws = self._browser_ws(port)
        try:
            infos = [t for t in ws.call("Target.getTargets")["targetInfos"] if t.get("type") == "page"]
        finally:
            ws.close()
        by_id = {t["targetId"]: t for t in infos}
        own = set(i for i in self._own if i in by_id)
        changed = True
        while changed:
            changed = False
            for t in infos:
                if t["targetId"] not in own and t.get("openerId") in own:
                    own.add(t["targetId"])
                    changed = True
        if own != set(self._own):
            self._own = [i for i in self._own if i in own] + [i for i in own if i not in self._own]
            self._write_own()
        return [by_id[i] for i in self._own]

    def _new_window(self, port, url="about:blank"):
        """A tab in a window of its own (headless composites each window's
        foreground tab, so bots sharing a process never throttle each other)."""
        ws = self._browser_ws(port)
        try:
            tid = ws.call("Target.createTarget", url=url or "about:blank", newWindow=True)["targetId"]
        finally:
            ws.close()
        self._own.append(tid)
        self._write_own()
        for _ in range(20):
            t = next((x for x in _http(port, "/json/list") if x.get("id") == tid), None)
            if t:
                return t
            time.sleep(0.1)
        raise RuntimeError("the new tab did not appear")

    def _adopt_launch_tab(self, port, url):
        """Chrome opens one window on about:blank at launch. The first view to act
        on a shared process claims it instead of opening a window beside it,
        so the profile does not keep a blank tab nobody drives. Only that
        launch tab is taken (a blank tab the user opened from the live view is theirs)."""
        with self.proc.lock:  # two views cold-starting together must not both claim it
            tid = (self.session() or {}).get("launch_tab")
            if not tid or any(tid in v._own for v in self.proc.views if v is not self):
                return None
            t = next((x for x in _http(port, "/json/list") if x.get("id") == tid and x.get("type") == "page"), None)
            if t is None or (t.get("url") or "about:blank") != "about:blank":
                return None
            self._own.append(tid)
            self._write_own()
        if url and url != "about:blank":
            ws = WS(t["webSocketDebuggerUrl"])
            try:
                ws.call("Page.navigate", url=url)  # do not wait for the load
            finally:
                ws.close()
        return t

    def _page_target(self, port):
        """The one tab we drive. Stays the same tab across calls (popups from
        target=_blank links would otherwise become tabs[0] and hijack us).
        On a shared process only this view's own tabs count; with none left a
        fresh one opens in its own window, back on the bot's last page."""
        if self.shared():
            tabs = [t for t in _http(port, "/json/list") if t.get("type") == "page"]
            own = {t["targetId"] for t in self._own_targets(port)}
            tabs = [t for t in tabs if t.get("id") in own]
        else:
            tabs = [t for t in _http(port, "/json/list") if t.get("type") == "page"]
        if not tabs:
            if self.shared():
                last = self.last_url()
                url = _with_scheme(last) if last and last != "about:blank" else "about:blank"
                tabs = [self._adopt_launch_tab(port, url) or self._new_window(port, url)]
            else:
                tabs = [_http(port, "/json/new?about:blank", method="PUT")]
        main = next((t for t in tabs if t.get("id") == self._main_tab), None) or tabs[-1]
        self._main_tab = main.get("id")
        return main, tabs

    def _connect(self, timeout=30):
        self.idle = False
        sess = self.start()
        tab, _ = self._page_target(sess["port"])
        ws = WS(tab["webSocketDebuggerUrl"], timeout=timeout)
        ws.main_frame = tab["id"]  # a page target's id is its top frame's id
        ws.call("Page.enable")
        self._foreground(ws)
        ws.call("Runtime.enable")
        # Downloads land in this bot's own folder (not ~/Downloads), so the bot
        # and the page can list what arrived.
        os.makedirs(self.downloads, exist_ok=True)
        # On a shared process the browser-wide setting is last-writer-wins across bots, so the per-page one
        # (older API, still honoured) goes first there; a lone bot keeps the browser-wide call.
        order = (("Page.setDownloadBehavior", {}), ("Browser.setDownloadBehavior", {"eventsEnabled": False})) if self.shared() \
            else (("Browser.setDownloadBehavior", {"eventsEnabled": False}), ("Page.setDownloadBehavior", {}))
        for method, extra in order:
            try:
                ws.call(method, behavior="allow", downloadPath=self.downloads, **extra)
                break
            except Exception:
                continue
        ws.mark()
        return ws, sess

    def _foreground(self, ws):
        """Make the driven tab the window's foreground tab. Headless Chrome only
        composites the foreground tab: a hidden one (another tab opened in front
        of it, or a fresh worker binding to tabs[-1]) sends no screencast frames
        and throttles its timers, so the live view freezes and input drags.
        A popped-out window is the user's: nothing here may raise or resize their tabs."""
        if self.headed():
            return
        try:
            ws.call("Page.bringToFront")
            # bringToFront alone can leave a tab hidden (a popup that lost a focus fight with its opener
            # stays unpainted); focus emulation on this session reliably makes Chrome treat it as visible.
            ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
            # Popups open at the size the site asked for (Google's sign-in: 534x400). Give them the full
            # window so the live view is sharp and the bot's screenshots match the usual viewport.
            w = ws.call("Browser.getWindowForTarget")
            b = w.get("bounds") or {}
            vw, vh = self.proc.viewport
            if b.get("windowState", "normal") == "normal" and (b.get("width", 0) < vw or b.get("height", 0) < vh):
                ws.call("Browser.setWindowBounds", windowId=w["windowId"], bounds={"width": vw, "height": vh})
        except Exception:
            pass

    @staticmethod
    def _dom(ws):
        """DOM.enable + DOM.getDocument once per connection (backend node
        lookups and DOM.querySelector need the document pushed)."""
        if not getattr(ws, "dom_enabled", False):
            ws.call("DOM.enable")
            ws.call("DOM.getDocument", depth=0)
            ws.dom_enabled = True

    # -- tabs --------------------------------------------------------------
    def tabs(self):
        """[{i, title, url, active}] for every real page tab, in Chrome's order."""
        sess = self.session()
        if not self.alive(sess):
            return []
        try:
            main, tabs = self._page_target(sess["port"])
        except Exception:
            return []
        # Chrome lists tabs newest-first; keep our own stable open-order so a
        # tab's index never shifts under the bot (or the strip) mid-task.
        by_id = {t.get("id"): t for t in tabs}
        self._tab_order = [i for i in self._tab_order if i in by_id] + [i for i in reversed(list(by_id)) if i not in self._tab_order]
        return [{"i": i, "id": tid, "title": (by_id[tid].get("title") or "")[:80], "url": by_id[tid].get("url") or "",
                 "active": tid == main.get("id"), "ws": by_id[tid].get("webSocketDebuggerUrl")} for i, tid in enumerate(self._tab_order)]

    def _switch_to(self, tab_id):
        """Make tab_id the tab we drive; the live view, screenshots and the
        take-over input session follow it."""
        with self.lock:
            self._main_tab = tab_id
            def f(ws):
                try:
                    ws.call("Page.bringToFront")
                except Exception:
                    pass
            return self._run(f, timeout=INTERACT_TIMEOUT_S)[1]

    def tab_switch(self, i):
        tabs = self.tabs()
        if not tabs:
            raise RuntimeError("browser not running")
        i = int(i)
        if not 0 <= i < len(tabs):
            raise RuntimeError(f"no tab {i}; tabs are 0..{len(tabs) - 1}")
        return self._switch_to(tabs[i]["id"])

    def tab_new(self, url="about:blank"):
        sess = self.start()
        if url and url != "about:blank":
            url = _with_scheme(url)
        if self.shared():
            t = self._new_window(sess["port"], url or "about:blank")
        else:
            t = _http(sess["port"], "/json/new?" + (url or "about:blank"), method="PUT")
        time.sleep(0.3)
        info = self._switch_to(t["id"])
        if url and url != "about:blank" and (info.get("url") or "about:blank") == "about:blank":
            info = self.goto(url)
        return info

    def tab_close(self, i=None):
        """Close tab i (default: the current one). Never closes the last tab."""
        tabs = self.tabs()
        if len(tabs) <= 1:
            raise RuntimeError("cannot close the only tab")
        cur = next(t for t in tabs if t["active"])
        target = cur if i is None or i == "" else (tabs[int(i)] if 0 <= int(i) < len(tabs) else None)
        if not target:
            raise RuntimeError(f"no tab {i}; tabs are 0..{len(tabs) - 1}")
        sess = self.session()
        try:
            _http(sess["port"], f"/json/close/{target['id']}")  # replies with plain text
        except ValueError:
            pass
        time.sleep(0.3)
        if target["id"] == cur["id"]:
            rest = [t for t in tabs if t["id"] != target["id"]]
            nxt = rest[min(target["i"], len(rest) - 1)]
            return self._switch_to(nxt["id"])
        return self._run(lambda ws: None, timeout=INTERACT_TIMEOUT_S)[1]

    def recover_stuck_google_popup(self):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            sess = self.session()
            if not self.alive(sess):
                return False
            try:
                tab, tabs = self._page_target(sess["port"])
                ws = WS(tab["webSocketDebuggerUrl"], timeout=RECOVER_TIMEOUT_S)
            except Exception:
                return False
            try:
                targets = ws.call("Target.getTargets").get("targetInfos", [])
                ours = {t.get("id") for t in tabs}
                stuck = next((t for t in targets if t.get("type") == "page"
                              and t.get("openerId") in ours
                              and t.get("url", "").startswith(STUCK_POPUP_URL)
                              and t.get("canAccessOpener") is False), None)
                if not stuck:
                    return False
                opener = next((t for t in tabs if t.get("id") == stuck["openerId"]), None)
                if not opener:
                    return False
                ws.call("Target.closeTarget", targetId=stuck["targetId"])
                if stuck["targetId"] == self._main_tab:
                    self._main_tab = opener["id"]
                    ws.close()
                    ws = WS(opener["webSocketDebuggerUrl"], timeout=RECOVER_TIMEOUT_S)
                    ws.main_frame = opener["id"]
                try:
                    ws.call("Page.enable")
                    self._foreground(ws)
                    ws.mark()
                    ws.call("Page.reload")
                    ws.wait_loaded(NAV_SETTLE_S)
                except Exception:
                    pass
                return True
            finally:
                ws.close()
        finally:
            self.lock.release()

    # -- element resolution --------------------------------------------------
    def _on_node(self, ws, backend, fn, args=(), by_value=True):
        """Run `fn` (a `function(...)` declaration) with the backend DOM node
        as `this`: DOM.resolveNode + Runtime.callFunctionOn. Reaches closed
        shadow roots and same-process frames. Returns the value (or the
        remote object when not by_value); raises when the node is gone.
        (resolveNode by backend id needs no DOM.enable.)"""
        obj = ws.call("DOM.resolveNode", backendNodeId=int(backend))["object"]
        r = ws.call("Runtime.callFunctionOn", objectId=obj["objectId"], functionDeclaration=fn,
                    arguments=[{"value": a} for a in args], returnByValue=by_value, awaitPromise=True)
        if "exceptionDetails" in r:
            ex = r["exceptionDetails"]
            raise RuntimeError(ex.get("exception", {}).get("description") or ex.get("text"))
        return r.get("result", {}).get("value") if by_value else r.get("result", {})

    def _js_on(self, ws, find, fn, args=()):
        """`fn` applied to the element `find` (a `_find_js` expression)
        resolves to, or _MISSING when it resolves to nothing."""
        return self._eval(ws, f"""(() => {{ const el = {find};
            if (!el) return {json.dumps(_MISSING)};
            return ({fn}).apply(el, {json.dumps(list(args))}); }})()""")

    def _on_element(self, ws, fn, args=(), selector="", ref="", text="", x=None, y=None, backend=None):
        """Apply `fn` to the target: the stamped ref (JS, as OpenBot); then,
        when the AX snapshot gave one, the backend node; then `_find_js`'s
        text / coordinate guesses. Raises 'Element not found' when all miss."""
        if backend is not None:
            out = self._js_on(ws, _find_js(selector, ref, "", strict=True), fn, args)
            if out != _MISSING:
                return out
            try:
                return self._on_node(ws, backend, fn, args)
            except Exception:
                pass  # the node is gone (re-render); fall back to text / position
        out = self._js_on(ws, _find_js(selector, ref, text, x, y), fn, args)
        if out == _MISSING:
            raise RuntimeError(f"Element not found: {ref or selector or text!r}")
        return out

    def upload(self, path, ref="", text="", x=None, y=None, backend=None):
        """Put a local file into a file input. With ref: that element if it is a
        file input, else the file input it labels / the nearest one in its form.
        Without ref: the first file input on the page."""
        if not os.path.isfile(path):
            raise RuntimeError(f"no such file: {path}")
        files = [os.path.abspath(path)]

        def by_backend(ws):
            """The backend node's file input, set by object id; False when the
            ref is reachable from the document (the JS path handles it) or the
            node cannot be resolved."""
            if backend is None or self._eval(ws, f"!!{_find_js('', ref, '', strict=True)}"):
                return False
            try:
                inp = self._on_node(ws, backend, _UPLOAD_PICK_FN, by_value=False)
            except Exception:
                return False
            if not inp.get("objectId") or inp.get("subtype") == "null":
                return False
            ws.call("DOM.setFileInputFiles", files=files, objectId=inp["objectId"])
            return True

        def f(ws):
            if not by_backend(ws):
                found = self._eval(ws, f"""(() => {{
                    document.querySelectorAll('[data-sb-upload]').forEach(e => e.removeAttribute('data-sb-upload'));
                    let el = {_find_js("", ref, text, x, y) if (ref or text) else "null"};
                    const isFile = e => e && e.tagName === 'INPUT' && e.type === 'file';
                    let inp = isFile(el) ? el : null;
                    if (!inp && el) {{
                      if (el.tagName === 'LABEL' && el.control && isFile(el.control)) inp = el.control;
                      if (!inp) inp = el.querySelector('input[type=file]');
                      if (!inp && el.closest('form')) inp = el.closest('form').querySelector('input[type=file]');
                      if (!inp) {{  // nearest file input on the page
                        const r = el.getBoundingClientRect(); let best = null, bd = Infinity;
                        document.querySelectorAll('input[type=file]').forEach(i => {{ const q = i.getBoundingClientRect();
                          const d = Math.hypot(q.left - r.left, q.top - r.top); if (d < bd) {{ bd = d; best = i; }} }});
                        inp = best;
                      }}
                    }}
                    if (!inp) inp = document.querySelector('input[type=file]');
                    if (!inp) return false;
                    inp.setAttribute('data-sb-upload', '1'); return true; }})()""")
                if not found:
                    raise RuntimeError("no file input on this page; click the upload button first so the site adds one")
                self._dom(ws)
                root = ws.call("DOM.getDocument", depth=0)["root"]["nodeId"]
                node = ws.call("DOM.querySelector", nodeId=root, selector="[data-sb-upload]")["nodeId"]
                ws.call("DOM.setFileInputFiles", files=files, nodeId=node)
            time.sleep(1.0)  # let the site's change handler run (preview, auto-submit)
        return self._run(f)[1]

    def list_files(self, folder, limit=20):
        """[{name, size, mtime, path}] newest first; `path` is absolute."""
        try:
            names = [n for n in os.listdir(folder) if not n.endswith(".crdownload") and not n.startswith(".")]
        except OSError:
            return []
        out = []
        for n in names:
            p = os.path.join(folder, n)
            try:
                st = os.stat(p)
                out.append({"name": n, "size": st.st_size, "mtime": st.st_mtime, "path": os.path.abspath(p)})
            except OSError:
                pass
        out.sort(key=lambda d: -d["mtime"])
        return out[:limit]

    def list_downloads(self, limit=20):
        return self.list_files(self.downloads, limit)

    def _run(self, fn, timeout=30, shoot=None):
        """Open a CDP connection, run fn(ws), record url + screenshot, close.
        shoot=None follows `self.shoot_actions`; True/False forces it."""
        if shoot is None:
            shoot = self.shoot_actions
        with self.lock:
            ws, sess = self._connect(timeout)
            try:
                try:
                    out = fn(ws)
                except Exception:
                    if ws.dialog:  # the action's dialog withheld a reply: settle it now or every later action hangs on it too
                        self._settle_dialog(ws)
                    raise
                dialog = self._settle_dialog(ws)
                info = self._where(ws)
                if dialog:
                    info["dialog"] = dialog
                sess["url"] = info.get("url")
                sess["title"] = info.get("title")
                write_json_atomic(self.session_path, sess)
                # tabs.json too, lone bot included: session.json is removed with the quit, so this is the only
                # record of the page a wake (Browser.start) brings back.
                self._write_own(url=info.get("url") or "", title=info.get("title") or "")
                if shoot:
                    self._shoot(ws)
                return out, info
            finally:
                ws.close()

    def _where(self, ws):
        """url + title after an action. A navigation still in flight (the wait gave up on a slow page) can
        destroy the context under the first evaluate; one short retry, then Chrome's history as the fallback."""
        expr = "({url: location.href, title: document.title})"
        for attempt in range(2):
            try:
                return self._eval(ws, expr) or {}
            except RuntimeError:
                if attempt:
                    break
                time.sleep(0.2)
        try:
            h = ws.call("Page.getNavigationHistory")
            e = h["entries"][h["currentIndex"]]
            return {"url": e.get("url"), "title": e.get("title")}
        except Exception:
            return {}

    @staticmethod
    def _settle_dialog(ws):
        """If the last action opened alert()/confirm()/prompt(), every evaluate
        would hang until it is closed. Probe briefly (skipped when the socket already saw it open);
        accept it and return its message so the model learns what popped up."""
        if not ws.dialog:
            try:
                ws.call("Runtime.evaluate", _timeout=3, expression="1", returnByValue=True)
                if ws.dialog_seen:  # it opened during the action and the live view (watching) already accepted it: still worth telling
                    d, ws.dialog_seen = ws.dialog_seen, None
                    return f"{d.get('type', 'dialog')}: {d.get('message', '')}".strip()
                return None
            except socket.timeout:
                pass
        d = ws.dialog or {}
        try:
            ws.call("Page.handleJavaScriptDialog", accept=True)
        except Exception:
            return None
        ws.dialog = ws.dialog_seen = None
        return f"{d.get('type', 'dialog')}: {d.get('message', '')}".strip()

    @staticmethod
    def _eval(ws, expr):
        r = ws.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            ex = r["exceptionDetails"]
            raise RuntimeError(ex.get("exception", {}).get("description") or ex.get("text"))
        return r.get("result", {}).get("value")

    def _nav(self, ws, url, settle=GOTO_SETTLE_S):
        """Navigate and wait for the page to settle. Returns the error text when Chrome refused the URL
        (net error) or turned it into a download: no load is coming then, so there is nothing to wait for."""
        ws.mark()
        r = ws.call("Page.navigate", url=_with_scheme(url))
        if r.get("errorText") or r.get("isDownload"):
            return r.get("errorText") or "the URL is a download, not a page"
        # No loaderId = a same-document move (a hash on the current page): the page never reloads.
        ws.wait_loaded(settle if r.get("loaderId") else 1)
        time.sleep(0.4)
        return None

    def _shoot(self, ws):
        os.makedirs(self.cache_dir, exist_ok=True)
        # Never pass a clip. A clipped/scaled capture makes Chrome apply a
        # temporary viewport emulation and undo it; the screencast shows that as
        # the page shrinking and snapping back on every screenshot. Take the plain
        # viewport at native device pixels and normalise to VIEWPORT afterwards
        # with sips (macOS built-in), so the agent's coordinates and vision cost
        # do not depend on LIVE_SCALE.
        r = ws.call("Page.captureScreenshot", format="png")
        tmp = self.shot_path + ".tmp.png"
        with open(tmp, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        # Fit to the viewport width only: the CSS viewport is shorter than the window (Chrome's UI takes
        # the rest, 1280x713 today), so forcing 1280x800 stretched the image and the model's clicks landed low.
        _sips(tmp, tmp, "-Z", str(VIEWPORT[0]))
        os.replace(tmp, self.shot_path)
        # Small JPEG of the same frame for the transcript (bot.py files it per step).
        self.thumb_bytes = None
        tj = self.shot_path + ".thumb.jpg"
        try:
            if _sips(self.shot_path, tj, "-s", "format", "jpeg", "-s", "formatOptions", "50",
                     "-Z", str(int(VIEWPORT[0] * 0.4))):
                with open(tj, "rb") as f:
                    self.thumb_bytes = f.read()
        except Exception:
            self.thumb_bytes = None

    def shot_ts(self):
        try:
            return os.path.getmtime(self.shot_path)
        except OSError:
            return 0

    # -- actions -----------------------------------------------------------
    def goto(self, url, settle=GOTO_SETTLE_S):
        """`settle`: longest wait for the page; the live view passes NAV_SETTLE_S (the user sees it stream)."""
        err, info = self._run(lambda ws: self._nav(ws, url, settle))
        if err:
            info["error"] = err
        return info

    def _history(self, delta):
        def f(ws):
            h = ws.call("Page.getNavigationHistory")
            i = h["currentIndex"] + delta
            if 0 <= i < len(h["entries"]):
                ws.mark()
                ws.call("Page.navigateToHistoryEntry", entryId=h["entries"][i]["id"])
                ws.wait_loaded(NAV_SETTLE_S, url=h["entries"][i].get("url"))
                time.sleep(0.3)
        return self._run(f, timeout=INTERACT_TIMEOUT_S)[1]

    def forward(self):
        return self._history(+1)

    def back(self):
        return self._history(-1)

    def reload(self):
        def f(ws):
            ws.mark()
            ws.call("Page.reload")
            ws.wait_loaded(NAV_SETTLE_S)
        return self._run(f, timeout=INTERACT_TIMEOUT_S)[1]

    def scroll(self, direction="down", ref="", text="", x=None, y=None, backend=None):
        """Scroll like a user: a mouse wheel at the viewport centre, so apps whose
        body is overflow:hidden and whose feed lives in an inner scroller
        (LinkedIn, Gmail, Twitter) move too. If nothing moved, fall back to the
        largest scrollable container, then to the window. With ref/text, scroll
        that element into view instead (direction ignored)."""
        dy = 600 if direction == "down" else -600
        probe = """(() => { const els = [document.scrollingElement, ...document.querySelectorAll('*')];
            return els.reduce((a, e) => a + (e ? e.scrollTop : 0), 0); })()"""
        def f(ws):
            if ref or text or backend is not None:
                self._on_element(ws, _SCROLL_FN, (), "", ref, text, x, y, backend)
                time.sleep(0.4)
                return
            before = self._eval(ws, probe) or 0
            cx, cy = self.proc.viewport[0] // 2, self.proc.viewport[1] // 2
            ws.call("Input.dispatchMouseEvent", type="mouseMoved", x=cx, y=cy)
            ws.notify("Input.dispatchMouseEvent", type="mouseWheel", x=cx, y=cy, deltaX=0, deltaY=dy)
            time.sleep(0.4)
            if (self._eval(ws, probe) or 0) != before:
                return
            self._eval(ws, f"""(() => {{
                const cands = [...document.querySelectorAll('*')].filter(e => {{
                  const s = getComputedStyle(e);
                  return /(auto|scroll)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200; }});
                cands.sort((a, b) => b.clientHeight * b.clientWidth - a.clientHeight * a.clientWidth);
                const el = cands[0] || document.scrollingElement;
                el.scrollBy(0, {dy}); window.scrollBy(0, {dy}); }})()""")
            time.sleep(0.4)
        return self._run(f)[1]

    def hover(self, ref="", selector="", text="", x=None, y=None, backend=None):
        """Real mouse move over an element (opens hover menus), no click."""
        def f(ws):
            box = self._on_element(ws, _HOVER_FN, (), selector, ref, text, x, y, backend)
            ws.call("Input.dispatchMouseEvent", type="mouseMoved", x=box["x"], y=box["y"])
            time.sleep(0.7)
        return self._run(f)[1]

    def click(self, ref="", selector="", text="", x=None, y=None, backend=None):
        """Real mouse click at the element's centre (what a user does; works for
        rich editors and JS frameworks that ignore synthetic .click()). Falls
        back to el.click() when something else is layered over the element."""
        def f(ws):
            ws.mark()
            box = self._on_element(ws, _CLICK_FN, (), selector, ref, text, x, y, backend)
            if not box.get("clicked"):
                for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                    ws.call("Input.dispatchMouseEvent", type=t, x=box["x"], y=box["y"], button="left", clickCount=1)
            ws.wait_loaded(3, grace=ACTION_GRACE_S)
            time.sleep(0.5)
        return self._run(f)[1]

    def type(self, text, ref="", selector="", label="", submit=False, x=None, y=None, backend=None):
        def f(ws):
            self._on_element(ws, _TYPE_FN, (), selector, ref, label, x, y, backend)
            ws.call("Input.insertText", text=text)
            if submit:
                ws.mark()
                for t in ("keyDown", "keyUp"):
                    ws.call("Input.dispatchKeyEvent", type=t, key="Enter", code="Enter",
                            windowsVirtualKeyCode=13, text="\r" if t == "keyDown" else "")
                ws.wait_loaded(8, grace=ACTION_GRACE_S)
                time.sleep(0.5)
        return self._run(f)[1]

    def press(self, key, ref="", text="", x=None, y=None, backend=None):
        """Press one key, optionally with modifiers: "Escape", "Enter", "Tab",
        "ArrowDown", "Control+a", "Meta+Enter", "Shift+Tab". With ref, focus
        that element first."""
        parts = [p for p in re.split(r"[+\-](?=.)", key.strip()) if p] or [key]
        mods_map = {"control": 2, "ctrl": 2, "alt": 1, "option": 1, "meta": 4, "cmd": 4, "command": 4, "shift": 8}
        mods = 0
        for p in parts[:-1]:
            mods |= mods_map.get(p.lower(), 0)
        k = parts[-1]
        aliases = {"esc": "Escape", "return": "Enter", "space": " ", "spacebar": " ", "del": "Delete",
                   "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
                   "pageup": "PageUp", "pagedown": "PageDown", "backspace": "Backspace", "tab": "Tab", "enter": "Enter",
                   "escape": "Escape", "home": "Home", "end": "End", "delete": "Delete"}
        k = aliases.get(k.lower(), k) if len(k) > 1 else k
        code = k if len(k) > 1 else _CHAR_CODE.get(k, "")
        if len(k) == 1 and mods & 8 and code in _KEYS:
            k = _KEYS[code][2]  # "Shift+a" types "A", "Shift+1" types "!", as the keyboard would
        events = key_events(k, code, mods)
        def f(ws):
            if ref or text or backend is not None:
                self._on_element(ws, _FOCUS_FN, (), "", ref, text, x, y, backend)
            ws.mark()
            if events[0] == "insert":
                ws.call("Input.insertText", text=events[1])
            else:
                for params in events:
                    ws.call("Input.dispatchKeyEvent", **params)
            ws.wait_loaded(3 if k == "Enter" else 0.5, grace=ACTION_GRACE_S)
            time.sleep(0.4)
        return self._run(f)[1]

    def select(self, value, ref="", text="", x=None, y=None, backend=None):
        """Choose an option in a <select> by value or (case-insensitive, partial)
        label. Returns the chosen label in info['chosen']."""
        def f(ws):
            try:
                return self._on_element(ws, _SELECT_FN, (value,), "", ref, text, x, y, backend)
            except RuntimeError as e:
                if str(e).startswith("Element not found"):
                    return {"error": "not found"}
                raise
        out, info = self._run(f)
        if out.get("error"):
            raise RuntimeError(out["error"])
        info["chosen"] = out.get("chosen")
        return info

    def wait_for(self, text="", timeout=10):
        """Wait until `text` appears in the page (or just timeout seconds when
        no text). Returns info with found=True/False."""
        timeout = max(0.5, min(float(timeout or 10), 25))
        def f(ws):
            if not text:
                time.sleep(timeout)
                return True
            t0 = time.time()
            js = f"(document.body ? document.body.innerText : '').toLowerCase().includes({json.dumps(text.lower())})"
            while time.time() - t0 < timeout:
                try:
                    if self._eval(ws, js):
                        return True
                except RuntimeError:
                    pass  # a navigation finishing mid-poll destroys the context: keep polling the new page
                time.sleep(0.5)
            return False
        found, info = self._run(f)
        info["found"] = found
        return info

    def read(self, ref="", text="", x=None, y=None, limit=6000, backend=None):
        """Full text of one element (or the whole page when no ref), beyond the
        excerpt in the observation."""
        def f(ws):
            if ref or text or backend is not None:
                r = self._on_element(ws, _READ_FN, (), "", ref, text, x, y, backend)
                return r or ""
            return self._eval(ws, "(document.body ? document.body.innerText : '')") or ""
        out, info = self._run(f)
        out = re.sub(r"[ \t]+\n", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        info["text"] = out[:limit] + (f"\n…[{len(out) - limit} more chars]" if len(out) > limit else "")
        return info

    # -- observation -------------------------------------------------------
    def _ax_elements(self, ws, n, vh):
        """The AX pass: Accessibility.getFullAXTree on the main frame and on
        every same-process child frame (out-of-process frames refuse and are
        skipped), the kept nodes resolved to DOM objects and stamped in one
        `callFunctionOn` per frame. Returns resolved records in document
        order (main frame, then child frames). Raises when the main tree
        cannot be read."""
        trees = [ws.call("Accessibility.getFullAXTree").get("nodes") or []]
        try:
            ft = ws.call("Page.getFrameTree").get("frameTree") or {}
            stack, kids = list(ft.get("childFrames") or []), []
            while stack and len(kids) < 8:
                fr = stack.pop(0)
                kids.append((fr.get("frame") or {}).get("id"))
                stack.extend(fr.get("childFrames") or [])
            for fid in kids:
                if not fid:
                    continue
                try:
                    trees.append(ws.call("Accessibility.getFullAXTree", frameId=fid).get("nodes") or [])
                except Exception:
                    pass  # out-of-process frame: its own target, not reachable from this session
        except Exception:
            pass
        group = "sb-snapshot"
        out, seen = [], set()
        # No DOM.enable here: resolveNode by backend id works without it, and
        # an enabled DOM agent streams a mutation event per change on busy pages.
        try:
            for nodes in trees:
                cands = [c for c in ax_candidates(nodes) if c["backend"] not in seen]
                seen.update(c["backend"] for c in cands)
                if not cands:
                    continue
                res = ws.call_many([("DOM.resolveNode", {"backendNodeId": c["backend"], "objectGroup": group})
                                    for c in cands])
                live = [(c, r["object"]["objectId"]) for c, r in zip(cands, res)
                        if isinstance(r, dict) and (r.get("object") or {}).get("objectId")]
                for i in range(0, len(live), 300):
                    chunk = live[i:i + 300]
                    r = ws.call("Runtime.callFunctionOn", objectId=chunk[0][1], functionDeclaration=STAMP_FN,
                                arguments=[{"value": {"n": n, "vh": vh}}] + [{"objectId": o} for _, o in chunk],
                                returnByValue=True)
                    if "exceptionDetails" in r:
                        continue
                    val = (r.get("result") or {}).get("value") or {}
                    n = int(val.get("n") or n)
                    for (c, _), el in zip(chunk, val.get("els") or []):
                        if not el:
                            continue
                        rec = dict(c)
                        rec.update(el)
                        rec["text"] = c.get("text") or ""
                        out.append(rec)
        finally:
            try:
                ws.call("Runtime.releaseObjectGroup", objectGroup=group)
            except Exception:
                pass
        return out

    def _snapshot(self, ws):
        """The observation's elements (docs §5): the DOM scan, then the AX
        pass, merged. The DOM scan alone when the AX pass fails."""
        scan = self._eval(ws, SNAPSHOT_JS) or {}
        dom = scan.get("elements") or []
        vw, vh = scan.get("vw") or VIEWPORT[0], scan.get("vh") or VIEWPORT[1]
        try:
            ax = self._ax_elements(ws, int(scan.get("n") or len(dom)), vh)
        except Exception as e:  # noqa: BLE001
            if not self._ax_warned:
                log.warning("bots.browser: accessibility snapshot failed, using the DOM scan alone: %s", e)
                self._ax_warned = True
            ax = []
        return merge_elements(ax, dom, (vw, vh))

    def observe(self):
        """URL, title, interactive elements (with refs), visible text, tabs and
        downloads."""
        def f(ws):
            return {"elements": self._snapshot(ws),
                    "text": self._eval(ws, TEXT_JS) or ""}
        out, info = self._run(f, shoot=True)
        out.update(info)
        out["tabs"] = self.tabs()
        out["downloads"] = self.list_downloads(8)
        return out

    def screenshot(self, timeout=30):
        """Take a fresh screenshot; returns the absolute path of shot.png."""
        self._run(lambda ws: None, timeout=timeout, shoot=True)
        return self.shot_path

    def screenshot_jpeg(self, max_width=1280, quality=60):
        """The current frame as JPEG bytes (the agent engine's `screenshot`
        tool result): a fresh capture through `_shoot`, then sips converts a
        copy, at most `max_width` wide, aspect kept."""
        with self.lock:
            self._run(lambda ws: None, shoot=True)
            tmp = self.shot_path + f".{os.getpid()}-{threading.get_ident()}.jpg"
            try:
                args = ["-s", "format", "jpeg", "-s", "formatOptions", str(int(quality))]
                w = _sips_width(self.shot_path)
                if w and w > int(max_width):
                    args += ["--resampleWidth", str(int(max_width))]
                if not _sips(self.shot_path, tmp, *args):
                    raise RuntimeError("could not convert the screenshot to JPEG (sips failed)")
                with open(tmp, "rb") as f:
                    return f.read()
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _status(self, probe):
        sess = self.session() or {}
        url, title = sess.get("url"), sess.get("title")
        if self.shared():  # session.json is last-writer-wins across bots; this bot's page is in its tabs.json
            try:
                with open(self.tabs_path) as f:
                    own = json.load(f)
                url, title = own.get("url") or None, own.get("title") or None
            except Exception:
                url = title = None
        return {"running": self.alive(sess) if probe else bool(sess.get("pid")), "url": url,
                "title": title, "headed": bool(sess.get("headed")), "sealed": self.sealed(), "encrypt": self.encrypt}

    def status(self):
        return self._status(True)

    def status_cached(self):
        """Like status() but trusts the session file instead of probing Chrome."""
        return self._status(False)

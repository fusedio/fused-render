import json, os, struct, subprocess, sys, time, zlib, base64
HERE = os.path.dirname(os.path.abspath(__file__)); S = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(S, "lib")]
import cdp
EXT = os.path.join(HERE, "ext")
OUT = os.path.join(S, "results", "C"); os.makedirs(OUT, exist_ok=True)
SESSION = os.path.join(OUT, "session.json")
TEXT = "a.b,c-d/e;f'g[h]i Hello World! (x) {y} <z> ~!@#$%^&*_+=|:\"?"
HUMAN = "human.text's,here"

# US-layout key table: char -> (key, code, vk, shift)
_KEYS = {}
for c in "abcdefghijklmnopqrstuvwxyz":
    _KEYS[c] = (c, "Key" + c.upper(), ord(c.upper()), False); _KEYS[c.upper()] = (c.upper(), "Key" + c.upper(), ord(c.upper()), True)
for i, c in enumerate("0123456789"):
    _KEYS[c] = (c, "Digit" + c, 48 + i, False)
for c, d in zip(")!@#$%^&*(", "0123456789"):
    _KEYS[c] = (c, "Digit" + d, ord(d), True)
for un, sh, code, vk in [(";", ":", "Semicolon", 186), ("=", "+", "Equal", 187), (",", "<", "Comma", 188), ("-", "_", "Minus", 189),
                         (".", ">", "Period", 190), ("/", "?", "Slash", 191), ("`", "~", "Backquote", 192), ("[", "{", "BracketLeft", 219),
                         ("\\", "|", "Backslash", 220), ("]", "}", "BracketRight", 221), ("'", '"', "Quote", 222)]:
    _KEYS[un] = (un, code, vk, False); _KEYS[sh] = (sh, code, vk, True)
_KEYS[" "] = (" ", "Space", 32, False)
SPECIAL = {"Enter": ("Enter", "Enter", 13, "\r"), "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, None),
           "Backspace": ("Backspace", "Backspace", 8, None)}


def type_text(r, tab, s):
    for ch in s:
        key, code, vk, shift = _KEYS[ch]
        mods = 8 if shift else 0
        r.cdp(tab, "Input.dispatchKeyEvent", type="keyDown", key=key, code=code, windowsVirtualKeyCode=vk, text=ch, unmodifiedText=ch, modifiers=mods)
        r.cdp(tab, "Input.dispatchKeyEvent", type="keyUp", key=key, code=code, windowsVirtualKeyCode=vk, modifiers=mods)


def press(r, tab, name, n=1):
    key, code, vk, text = SPECIAL[name]
    for _ in range(n):
        kw = dict(type="keyDown", key=key, code=code, windowsVirtualKeyCode=vk)
        if text: kw["text"] = text
        r.cdp(tab, "Input.dispatchKeyEvent", **kw)
        r.cdp(tab, "Input.dispatchKeyEvent", type="keyUp", key=key, code=code, windowsVirtualKeyCode=vk)


def select_all(r, tab):
    r.cdp(tab, "Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=4, commands=["selectAll"])
    r.cdp(tab, "Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=4)


def png_stats(b64):
    """Return (w, h, unique_byte_values_in_pixels) — stdlib PNG decode, enough to tell blank from not."""
    b = base64.b64decode(b64); assert b[:8] == b"\x89PNG\r\n\x1a\n"
    i = 8; idat = b""; w = h = 0
    while i < len(b):
        n = struct.unpack(">I", b[i:i + 4])[0]; t = b[i + 4:i + 8]; d = b[i + 8:i + 8 + n]
        if t == b"IHDR": w, h = struct.unpack(">II", d[:8])
        if t == b"IDAT": idat += d
        i += 12 + n
    raw = zlib.decompress(idat)
    return w, h, len(set(raw[1:200000]))


def osa(script):
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    return (p.stdout.strip(), p.stderr.strip())


def frontmost_pid():
    return osa('tell application "System Events" to get unix id of first process whose frontmost is true')[0]


def human_type(chrome_pid, text):
    """Real OS keystrokes. Refuse unless OUR Chrome is frontmost (user's own Chrome may be running)."""
    osa(f'tell application "System Events" to set frontmost of (first process whose unix id is {chrome_pid}) to true')
    time.sleep(0.4)
    fp = frontmost_pid()
    if str(fp) != str(chrome_pid):
        return {"typed": False, "frontmost": fp}
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    o = osa(f'tell application "System Events" to keystroke "{esc}"')
    return {"typed": True, "frontmost": fp, "osa": o}


def screencap(path):
    p = subprocess.run(["screencapture", "-x", path], capture_output=True, text=True)
    return p.returncode, p.stderr.strip()


def launch_bridge(prof_name, extra=(), relay=None):
    prof = os.path.join(S, "profiles", prof_name)
    flags = ["--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-background-timer-throttling", *extra]
    t0 = time.time()
    sess = cdp.launch(prof, headless=False, extra=flags, window=(900, 650))
    ws = cdp.WS(cdp.http(sess["port"], "/json/version")["webSocketDebuggerUrl"])
    ext_id = ws.call("Extensions.loadUnpacked", path=EXT)["id"]; ws.close()
    ok = relay.wait_connected(20) if relay else None
    return sess, ext_id, ok, time.time() - t0


def serve_page():
    """chrome.debugger refuses data: navigations (net::ERR_ABORTED) -> serve the test page over http."""
    import http.server, threading, urllib.parse
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); h = q.get("h", ["bot"])[0]; bg = q.get("bg", ["#cde"])[0]
            body = f"<body style='background:{bg}'><h1>{h}</h1><textarea id=t autofocus></textarea></body>".encode()
            self.send_response(200); self.send_header("content-type", "text/html"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/"


class HumanCDP:
    """Fallback 'human' when osascript is TCC-denied: a SEPARATE CDP client over --remote-debugging-port."""
    def __init__(self, port, target_id):
        t = [x for x in cdp.http(port, "/json/list") if x["id"] == target_id][0]
        self.ws = cdp.WS(t["webSocketDebuggerUrl"])
    def cdp(self, _tab, method, **p): return self.ws.call(method, **p)
    def close(self): self.ws.close()


def human_input(chrome_pid, port, target_id, text):
    h = human_type(chrome_pid, text)
    if h.get("typed") and not (h.get("osa") or ("", ""))[1]:
        return {"path": "osascript", **h}
    hc = HumanCDP(port, target_id); type_text(hc, None, text); hc.close()
    return {"path": "cdp-port-fallback", "osascript_result": h}


def write_session(d):
    json.dump(d, open(SESSION, "w"), indent=1)


def kill_c():
    """Kill ONLY proto-C Chromes (kill_ours() matches the shared scratchpad tag and hits other prototypes)."""
    import signal
    for pid in subprocess.run(["pgrep", "-f", "scratchpad/profiles/C-"], capture_output=True, text=True).stdout.split():
        try: os.kill(int(pid), signal.SIGKILL)
        except Exception: pass

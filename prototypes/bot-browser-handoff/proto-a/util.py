import zlib, struct, base64, subprocess, time, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import WS, http, pages

S = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = f"{S}/results/A"


def png_stats(b64):
    """decode PNG (8-bit RGB/RGBA) -> (w, h, frac_nonwhite, frac_red)"""
    d = base64.b64decode(b64); assert d[:8] == b'\x89PNG\r\n\x1a\n'
    i = 8; idat = b''; w = h = ct = 0
    while i < len(d):
        n = struct.unpack('>I', d[i:i+4])[0]; t = d[i+4:i+8]; c = d[i+8:i+8+n]
        if t == b'IHDR': w, h, bd, ct = struct.unpack('>IIBB', c[:10])
        elif t == b'IDAT': idat += c
        i += 12 + n
    bpp = 4 if ct == 6 else 3; raw = zlib.decompress(idat); stride = w * bpp
    prev = bytearray(stride); p = 0; nw = red = blue = tot = 0
    for y in range(h):
        f = raw[p]; line = bytearray(raw[p+1:p+1+stride]); p += 1 + stride
        if f:
            for x in range(stride):
                a = line[x-bpp] if x >= bpp else 0; b = prev[x]; cc = prev[x-bpp] if x >= bpp else 0
                if f == 1: line[x] = (line[x] + a) & 255
                elif f == 2: line[x] = (line[x] + b) & 255
                elif f == 3: line[x] = (line[x] + (a + b) // 2) & 255
                elif f == 4:
                    pa, pb, pc = abs(b - cc), abs(a - cc), abs(a + b - 2*cc)
                    line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else cc)) & 255
        prev = line
        if y % 8: continue
        for x in range(0, stride, bpp*8):
            r, g, bl = line[x], line[x+1], line[x+2]; tot += 1
            if not (r > 245 and g > 245 and bl > 245): nw += 1
            if r > 180 and g < 80 and bl < 80: red += 1
            if bl > 180 and r < 80 and g < 80: blue += 1
    return {"w": w, "h": h, "nonwhite": round(nw/tot, 3), "red": round(red/tot, 3), "blue": round(blue/tot, 3)}


def osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def frontmost():
    return osa('tell application "System Events" to get {name, unix id} of (every process whose frontmost is true)')[1]


def activate_pid(pid):
    return osa(f'tell application "System Events" to set frontmost of (every process whose unix id is {pid}) to true')


PAGE = ("data:text/html,<body style='margin:0;background:%23d22'><textarea id=t autofocus style='width:600px;height:120px'></textarea>"
        "<div id=c style='font:40px sans-serif;color:white'>0</div><div id=i style='font:40px sans-serif;color:white'>tick</div><script>var n=0;function f(){n++;document.getElementById('c').textContent=n;requestAnimationFrame(f)}requestAnimationFrame(f);var k=0;setInterval(()=>{k++;document.getElementById('i').textContent='tick '+k},100);"
        "window.vis=[];document.addEventListener('visibilitychange',()=>vis.push(document.visibilityState))</script></body>")

US_VK = {".": (190, "Period"), ",": (188, "Comma"), "-": (189, "Minus"), "/": (191, "Slash"), ";": (186, "Semicolon"),
         "=": (187, "Equal"), "[": (219, "BracketLeft"), "]": (221, "BracketRight"), "'": (222, "Quote"), "`": (192, "Backquote"),
         "\\": (220, "Backslash"), " ": (32, "Space")}


def type_text(ws, text):
    for ch in text:
        vk, code = US_VK.get(ch, (ord(ch.upper()) if ch.isalnum() and ch.isascii() else 0, ""))
        kw = dict(key=ch, text=ch, unmodifiedText=ch)
        if vk: kw["windowsVirtualKeyCode"] = vk
        if code: kw["code"] = code
        ws.call("Input.dispatchKeyEvent", type="keyDown", **kw)
        ws.call("Input.dispatchKeyEvent", type="keyUp", key=ch, **({"windowsVirtualKeyCode": vk} if vk else {}))


SPECIAL = {"Enter": (13, "Enter", "\r"), "ArrowLeft": (37, "ArrowLeft", None), "Backspace": (8, "Backspace", None)}


def press(ws, name, modifiers=0):
    vk, code, text = SPECIAL[name]
    kw = dict(key=name, code=code, windowsVirtualKeyCode=vk, modifiers=modifiers)
    if text: kw["text"] = text
    ws.call("Input.dispatchKeyEvent", type="keyDown" if text else "rawKeyDown", **kw)
    ws.call("Input.dispatchKeyEvent", type="keyUp", key=name, code=code, windowsVirtualKeyCode=vk, modifiers=modifiers)


def select_all(ws):
    ws.call("Input.dispatchKeyEvent", type="rawKeyDown", key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=4, commands=["selectAll"])
    ws.call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=4)


def browser_ws(port):
    return WS(http(port, "/json/version")["webSocketDebuggerUrl"])


def page_ws(port, tid):
    for t in pages(port):
        if t["id"] == tid: return WS(t["webSocketDebuggerUrl"])
    raise KeyError(tid)


def shot(ws, save=None):
    t0 = time.time()
    r = ws.call("Page.captureScreenshot", format="png")
    ms = round((time.time() - t0) * 1000)
    if save:
        open(f"{OUT}/{save}.png", "wb").write(base64.b64decode(r["data"]))
    st = png_stats(r["data"]); st["ms"] = ms
    return st


def screencast(ws, secs=3):
    ws.events = [e for e in ws.events if e["method"] != "Page.screencastFrame"]
    ws.call("Page.startScreencast", format="jpeg", quality=50, maxWidth=640, maxHeight=400)
    n = 0; t0 = time.time(); sizes = []
    ws.sock.settimeout(0.3)
    while time.time() - t0 < secs:
        try: m = ws.recv()
        except Exception: continue
        if m.get("method") == "Page.screencastFrame":
            n += 1; sizes.append(len(m["params"]["data"])); ws.notify("Page.screencastFrameAck", sessionId=m["params"]["sessionId"])
        elif "method" in m: ws.events.append(m)
    ws.sock.settimeout(30)
    ws.call("Page.stopScreencast")
    return {"frames": n, "min_b64": min(sizes) if sizes else 0}


def raf_rate(ws, secs=1.0):
    """count rAF callbacks for secs in-page (works on any page); returns per-second rate"""
    v = ws.evaluate("new Promise(r=>{let c=0,t0=performance.now();function f(){c++;if(performance.now()-t0<%d)requestAnimationFrame(f)}requestAnimationFrame(f);setTimeout(()=>r(c),%d)})" % (secs*1000, secs*1000))
    return round((v or 0) / secs, 1)


def win(bws, tid):
    return bws.call("Browser.getWindowForTarget", targetId=tid)


def set_state(bws, wid, state, **bounds):
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"windowState": state})
    if bounds:
        bws.call("Browser.setWindowBounds", windowId=wid, bounds=bounds)


def log(fh, *a):
    s = " ".join(str(x) for x in a); print(s, flush=True); fh.write(s + "\n"); fh.flush()

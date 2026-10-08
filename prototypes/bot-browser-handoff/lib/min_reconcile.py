"""Reconcile: minimized headed window + Emulation.setFocusEmulationEnabled — order matters?
Variants: none | emu BEFORE minimize | emu AFTER minimize. Measures screencast frames in 3 s,
captureScreenshot time, and freshness (page flips colour via rAF; two shots 1 s apart must differ)."""
import sys, os, time, base64, hashlib, threading
sys.path.insert(0, os.path.dirname(__file__))
from cdp import launch, pages, WS, kill
S = os.path.dirname(os.path.dirname(__file__))
PAGE = "data:text/html,<body style='margin:0'><script>let i=0;function f(){i++;document.body.style.background='rgb('+((i*7)&255)+',100,'+((i*3)&255)+')';document.title=i;requestAnimationFrame(f)}f()</script>"
FLAGS = ["--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-background-timer-throttling"]

def shot(ws):
    t = time.time()
    try:
        r = ws.call("Page.captureScreenshot", format="jpeg", quality=50)
        return f"{len(r['data'])}B/{(time.time()-t)*1000:.0f}ms", hashlib.md5(r["data"].encode()).hexdigest()[:6]
    except Exception as e:
        return f"ERR {type(e).__name__}", "-"

def frames(ws, secs=3):
    ws.call("Page.startScreencast", format="jpeg", quality=30, maxWidth=640, maxHeight=480, everyNthFrame=1)
    n = 0; end = time.time() + secs
    while time.time() < end:
        try:
            ev = ws.wait_event("Page.screencastFrame", timeout=max(0.05, end - time.time()))
            n += 1; ws.notify("Page.screencastFrameAck", sessionId=ev["sessionId"])
        except Exception:
            break
    try: ws.call("Page.stopScreencast")
    except Exception: pass
    return n

def run(variant):
    sess = launch(f"{S}/profiles/recon-{variant}", headless=False, extra=FLAGS, url=PAGE, window=(600, 400))
    try:
        time.sleep(1.0)
        t = [p for p in pages(sess["port"]) if p["url"].startswith("data:")][0]
        ws = WS(t["webSocketDebuggerUrl"]); ws.sock.settimeout(8)
        ws.call("Page.enable")
        win = ws.call("Browser.getWindowForTarget", targetId=t["id"])["windowId"]
        if variant == "before":
            ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
        ws.call("Browser.setWindowBounds", windowId=win, bounds={"windowState": "minimized"})
        time.sleep(0.8)
        if variant == "after":
            ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
        state = ws.call("Browser.getWindowBounds", windowId=win)["bounds"].get("windowState")
        vis = ws.evaluate("document.visibilityState")
        s1, h1 = shot(ws); time.sleep(1.0); s2, h2 = shot(ws)
        n = frames(ws)
        print(f"{variant:7} state={state} vis={vis} shot1={s1} shot2={s2} fresh={h1 != h2} frames/3s={n}", flush=True)
        ws.close()
    finally:
        kill(sess)

for v in ["none", "before", "after"]:
    run(v)
